"""MCP server sensor: the internal operations MCP server of a fictional SaaS company.

Run with: uvicorn tripvane_sensors.archetypes.mcp.app:create_app --factory

/mcp is a streamable HTTP MCP endpoint served by the MCP Python SDK. It offers the
standard decoy tools plus list_customers and get_invoice, and one prompt. No tool runs:
every tools/call is answered with the tool's fake result.

Recording happens in front of the SDK, on each HTTP request, so it sees exactly what the
client sent:
- initialize, server/discover and tools/list each become an InputReceived on channel
  mcp whose raw_text is the method and params as compact JSON.
- tools/call becomes a ToolCallAttempted with the caller's arguments and the fake
  result. If any string in the arguments is longer than LONG_ARGUMENT characters, those
  strings are run through the DecoyAgent, so instructions injected into tool arguments
  are recorded together with what a model does with them.
- prompts/get runs the prompt's argument text through the DecoyAgent.

Sessions: a client that opens an MCP session with initialize gets a session keyed on
the MCP session id, which starts with a SessionStarted and ends with a SessionEnded when
the client deletes it or after IDLE_TIMEOUT_SECONDS without a request. Requests without
a session the sensor issued (the 2026-07-28 protocol has no sessions; also unknown or
expired ids) are grouped per client address under the same inactivity rule.

GET /health reports the sensor id and archetype.
"""

import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import Message, Receive, Scope, Send

from tripvane_core.config import Settings
from tripvane_core.events import InputReceived, ToolCallAttempted
from tripvane_core.events import Source as EventSource
from tripvane_core.hashing import payload_hash
from tripvane_sensors.archetypes.common import (
    IDLE_TIMEOUT_SECONDS,
    IPAddress,
    RateLimiter,
    SessionTracker,
    TrackedSession,
    anthropic_client,
    background,
    client_source,
    spooled_sink,
    trusted_proxy_address,
    utc_now,
)
from tripvane_sensors.archetypes.mcp.tools import MCP_TOOLS
from tripvane_sensors.runtime.budget import Budget
from tripvane_sensors.runtime.canary import Canaries, inject_into_tools
from tripvane_sensors.runtime.decoy_agent import DecoyAgent, ModelClient, Sink
from tripvane_sensors.runtime.tools import DecoyTool

ARCHETYPE = "mcp"
SERVER_NAME = "quillstone-ops"
SERVER_VERSION = "2.14.0"
# A tools/call string argument longer than this is sent through the DecoyAgent.
LONG_ARGUMENT = 40
# Text routed to the model in one run is capped like a support chat message; longer text
# is recorded as an InputReceived without a model call.
MAX_ROUTED_LENGTH = 4000
# Model runs per client address per hour, as in the support archetype.
RATE_LIMIT = 20
RATE_WINDOW_SECONDS = 3600.0
# Methods recorded as an InputReceived carrying the request itself.
LOGGED_METHODS = frozenset({"initialize", "server/discover", "tools/list"})
RECORDED_METHODS = LOGGED_METHODS | {"tools/call", "prompts/get"}

_HERE = Path(__file__).parent
SYSTEM_PROMPT = _HERE / "system.md"
DRAFT_REPLY = (_HERE / "draft_reply.md").read_text(encoding="utf-8")

PROMPT = types.Prompt(
    name="draft_customer_reply",
    description="Draft a reply to a customer message for the support queue.",
    arguments=[
        types.PromptArgument(
            name="customer_message", description="The customer's message.", required=True
        )
    ],
)

logger = logging.getLogger(__name__)


def answer(tools: dict[str, DecoyTool], name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
    """The fake result of a tool call, and whether the tool is unknown."""
    tool = tools.get(name)
    if tool is None:
        return f"Error: unknown tool {name}", True
    return tool.fake_result(arguments), False


def long_strings(value: object) -> list[str]:
    """Every string longer than LONG_ARGUMENT in value, searching nested lists and dicts."""
    found: list[str] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if len(item) > LONG_ARGUMENT:
                found.append(item)
        elif isinstance(item, dict):
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            stack.extend(reversed(item))
    return found


def prompt_text(params: object) -> str:
    """The text of a prompts/get request: its string arguments, or else the prompt name."""
    if not isinstance(params, dict):
        return ""
    arguments = params.get("arguments")
    if isinstance(arguments, dict):
        text = "\n\n".join(value for value in arguments.values() if isinstance(value, str))
        if text:
            return text
    name = params.get("name")
    return name if isinstance(name, str) else ""


def request_text(method: str, params: object) -> str:
    """A request as compact JSON with sorted keys, so equal requests hash equally."""
    return json.dumps(
        {"method": method, "params": params},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def jsonrpc_messages(body: bytes) -> list[dict[str, Any]]:
    """The JSON-RPC requests and notifications in a POST body; empty if it is not JSON-RPC."""
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return []
    items = data if isinstance(data, list) else [data]
    return [
        item for item in items if isinstance(item, dict) and isinstance(item.get("method"), str)
    ]


def build_server(tools: dict[str, DecoyTool]) -> Server[Any]:
    """The MCP server the SDK serves. Its handlers only return fake results."""
    tool_defs = [
        types.Tool(name=tool.name, description=tool.description, input_schema=tool.input_schema)
        for tool in tools.values()
    ]

    async def list_tools(
        ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tool_defs)

    async def call_tool(
        ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        text, unknown = answer(tools, params.name, params.arguments or {})
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)], is_error=unknown
        )

    async def list_prompts(
        ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListPromptsResult:
        return types.ListPromptsResult(prompts=[PROMPT])

    async def get_prompt(
        ctx: ServerRequestContext[Any], params: types.GetPromptRequestParams
    ) -> types.GetPromptResult:
        if params.name != PROMPT.name:
            raise MCPError(code=types.INVALID_PARAMS, message=f"Unknown prompt: {params.name}")
        message = (params.arguments or {}).get("customer_message", "")
        content = types.TextContent(type="text", text=DRAFT_REPLY + message)
        return types.GetPromptResult(
            description=PROMPT.description,
            messages=[types.PromptMessage(role="user", content=content)],
        )

    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_list_prompts=list_prompts,
        on_get_prompt=get_prompt,
    )


def _mcp_key(session_id: str) -> str:
    """Tracker key of an MCP session. Prefixed so a client-sent session id cannot name
    a session grouped by address ("ip:...")."""
    return f"mcp:{session_id}"


class _ResponseStart:
    """Captures the status and session id header of the response the SDK sends."""

    def __init__(self, send: Send) -> None:
        self._send = send
        self.status: int | None = None
        self.session_id: str | None = None

    async def __call__(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            for name, value in message.get("headers", []):
                if name.decode("latin-1").lower() == MCP_SESSION_ID_HEADER:
                    self.session_id = value.decode("latin-1")
        await self._send(message)


async def _read_body(receive: Receive, limit: int) -> bytes | None:
    """The whole request body, or None if it is larger than limit or the client left."""
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return None
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


def _replay(body: bytes, receive: Receive) -> Receive:
    """A receive callable that yields the already-read body once, then defers to receive."""
    sent = False

    async def replay() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


class Recorder:
    """ASGI app in front of the SDK's streamable HTTP endpoint that records each request."""

    def __init__(
        self,
        manager: StreamableHTTPSessionManager,
        tracker: SessionTracker,
        agent: DecoyAgent,
        tools: dict[str, DecoyTool],
        limiter: RateLimiter,
        trusted_proxy: IPAddress | None,
    ) -> None:
        self.manager = manager
        self.tracker = tracker
        self.agent = agent
        self.tools = tools
        self.limiter = limiter
        self.trusted_proxy = trusted_proxy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        source = client_source(request, self.trusted_proxy)
        messages: list[dict[str, Any]] = []
        if request.method == "POST":
            body = await _read_body(receive, DEFAULT_MAX_REQUEST_BODY_SIZE)
            if body is None:
                await Response(status_code=413)(scope, receive, send)
                return
            messages = [m for m in jsonrpc_messages(body) if m["method"] in RECORDED_METHODS]
            receive = _replay(body, receive)

        header_id = request.headers.get(MCP_SESSION_ID_HEADER)
        session: TrackedSession | None = None
        if header_id is not None and _mcp_key(header_id) in self.tracker:
            session = self.tracker.acquire(_mcp_key(header_id), source)
        response = _ResponseStart(send)
        try:
            await self.manager.handle_request(scope, receive, response)
            if session is None and messages:
                session = self._session_for(header_id, response, messages, source)
            if session is not None and messages:
                await anyio.to_thread.run_sync(self._record, session, messages, source)
        finally:
            if session is not None:
                self.tracker.release(session)
        deleted = request.method == "DELETE" and (response.status or 500) < 400
        if deleted and header_id is not None and session is not None:
            await anyio.to_thread.run_sync(self.tracker.close, _mcp_key(header_id))

    def _session_for(
        self,
        header_id: str | None,
        response: _ResponseStart,
        messages: list[dict[str, Any]],
        source: EventSource,
    ) -> TrackedSession:
        """A new MCP session if this request opened one, else the client address's session."""
        opened = (
            header_id is None
            and response.session_id is not None
            and (response.status or 500) < 400
            and any(m["method"] == "initialize" for m in messages)
        )
        if opened and response.session_id is not None:
            return self.tracker.acquire(
                _mcp_key(response.session_id), source, session_id=response.session_id
            )
        return self.tracker.acquire(f"ip:{source.ip}", source)

    def _record(
        self, session: TrackedSession, messages: list[dict[str, Any]], source: EventSource
    ) -> None:
        """Record the messages of one request. Runs in a worker thread."""
        with session.lock:
            for message in messages:
                method = message["method"]
                params = message.get("params")
                if method in LOGGED_METHODS:
                    self._input(session, source, request_text(method, params))
                elif method == "tools/call":
                    self._tool_call(session, source, params)
                else:
                    text = prompt_text(params)
                    if text:
                        self._route(session, source, text)

    def _input(self, session: TrackedSession, source: EventSource, text: str) -> None:
        self.tracker.emit(
            InputReceived(
                **self.tracker.stamp(session, source),
                raw_text=text,
                channel="mcp",
                payload_hash=payload_hash(text),
            )
        )

    def _tool_call(self, session: TrackedSession, source: EventSource, params: object) -> None:
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return
        name = params["name"]
        raw_arguments = params.get("arguments")
        if raw_arguments is None:
            arguments: dict[str, Any] = {}
        elif isinstance(raw_arguments, dict):
            arguments = raw_arguments
        else:
            arguments = {"input": raw_arguments}
        fake, _ = answer(self.tools, name, arguments)
        self.tracker.emit(
            ToolCallAttempted(
                **self.tracker.stamp(session, source),
                tool_name=name,
                arguments=arguments,
                fake_result=fake,
            )
        )
        texts = long_strings(arguments)
        if texts:
            self._route(session, source, "\n\n".join(texts))

    def _route(self, session: TrackedSession, source: EventSource, text: str) -> None:
        """Record text as an InputReceived and, within the limits, run the DecoyAgent on it."""
        event = InputReceived(
            **self.tracker.stamp(session, source),
            raw_text=text,
            channel="mcp",
            payload_hash=payload_hash(text),
        )
        if len(text) > MAX_ROUTED_LENGTH or not self.limiter.allow(str(source.ip)):
            self.tracker.emit(event)
            return
        try:
            self.agent.run(session.session_id, event)
        except Exception:
            # The response has already gone out; whatever the run emitted stays recorded
            # and the session's sequence continues after it.
            logger.exception("decoy agent run failed")


def create_app(
    settings: Settings | None = None,
    *,
    client: ModelClient | None = None,
    sink: Sink | None = None,
    canaries: Canaries | None = None,
    clock: Callable[[], float] | None = None,
    now: Callable[[], datetime] = utc_now,
) -> Starlette:
    """Build the sensor app. With no arguments everything comes from the environment.

    Tests pass a mock model client, an in-memory sink and a fake clock. The tracker is
    exposed as app.state.tracker.
    """
    if settings is None:
        settings = Settings.from_env()
    if settings.sensor_id is None:
        raise RuntimeError("SENSOR_ID is not set")
    sensor_id = settings.sensor_id
    trusted_proxy = trusted_proxy_address(settings)
    budget = Budget.from_settings(settings)
    if canaries is None:
        canaries = Canaries.from_env()

    shipper = None
    if sink is None:
        shipper = spooled_sink(settings)
        sink = shipper
    if client is None:
        client = anthropic_client(settings)

    clock_args = {} if clock is None else {"clock": clock}
    tracker = SessionTracker(sensor_id, sink, now=now, **clock_args)
    tools = {tool.name: tool for tool in inject_into_tools(MCP_TOOLS, canaries)}
    agent = DecoyAgent(
        SYSTEM_PROMPT, MCP_TOOLS, client, tracker, budget, canaries=canaries, now=now
    )
    manager = StreamableHTTPSessionManager(
        app=build_server(tools), session_idle_timeout=IDLE_TIMEOUT_SECONDS
    )
    recorder = Recorder(
        manager,
        tracker,
        agent,
        tools,
        RateLimiter(RATE_LIMIT, RATE_WINDOW_SECONDS, **clock_args),
        trusted_proxy,
    )
    shippers = [] if shipper is None else [shipper.ship_forever]

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        # The reaper stops first and ends every open session; the shipper then sends that.
        async with manager.run():
            with background(*shippers), background(tracker.reap_forever):
                yield

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"sensor_id": sensor_id, "archetype": ARCHETYPE})

    app = Starlette(routes=[Route("/health", health), Route("/mcp", recorder)], lifespan=lifespan)
    app.state.tracker = tracker
    return app
