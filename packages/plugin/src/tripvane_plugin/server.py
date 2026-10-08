"""The plugin's MCP server, "tripvane": three lookup tools over the public lookup API.

Each tool calls GET {TRIPVANE_API_URL}/v1/lookup/{ip,domain,payload}/{value}, sending
TRIPVANE_API_KEY as X-Api-Key when it is set, and returns the API's answer as compact
JSON. The server runs over stdio, started by Claude Code from the plugin's .mcp.json.
"""

import json
import os
from collections.abc import Mapping
from urllib.parse import quote

import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

TIMEOUT_SECONDS = 10.0
NOT_SEEN_NOTE = (
    "seen false means only that the Tripvane grid has not recorded the value; it is not "
    "evidence that the value is safe."
)


def _env(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name, "").strip()
    # An unset plugin option can reach the server as its unexpanded ${...} reference.
    if not value or value.startswith("${"):
        return None
    return value


def create_server(
    environ: Mapping[str, str] = os.environ,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> MCPServer:
    """Build the server. Tests pass a transport in place of the network."""
    api_url = _env(environ, "TRIPVANE_API_URL")
    api_key = _env(environ, "TRIPVANE_API_KEY")
    server = MCPServer(
        "tripvane",
        instructions=(
            "Look up IP addresses, domains and payload hashes on the Tripvane deception "
            "grid, which records prompt-injection attempts against decoy AI agents. "
            + NOT_SEEN_NOTE
        ),
        # Stderr reaches Claude Code's logs; keep looked-up values out of them.
        log_level="WARNING",
    )

    async def lookup(kind: str, value: str) -> str:
        if api_url is None:
            raise ToolError("TRIPVANE_API_URL is not set; configure the tripvane plugin")
        headers = {"X-Api-Key": api_key} if api_key else {}
        path = f"/v1/lookup/{kind}/{quote(value.strip(), safe='')}"
        try:
            async with httpx2.AsyncClient(
                base_url=api_url.rstrip("/"),
                headers=headers,
                timeout=TIMEOUT_SECONDS,
                transport=transport,
            ) as client:
                response = await client.get(path)
        except httpx2.HTTPError as exc:
            raise ToolError(f"could not reach the Tripvane API at {api_url}: {exc}") from None
        if response.status_code == 200:
            return json.dumps(response.json(), separators=(",", ":"))
        raise ToolError(_error_message(response))

    @server.tool(structured_output=False)
    async def lookup_ip(ip: str) -> str:
        """Look up an IPv4 or IPv6 address on the Tripvane grid: whether it has sent
        prompt-injection attempts to decoy AI agents, when, to how many sensors and
        sessions, the attack tags seen, and the campaign id. seen false means only that
        the grid has not recorded it, not that it is safe."""
        return await lookup("ip", ip)

    @server.tool(structured_output=False)
    async def lookup_domain(domain: str) -> str:
        """Look up a domain (for example an MCP server's host, or a host named in a
        document) on the Tripvane grid: whether it appeared in prompt-injection attempts
        against decoy AI agents, in their text or in the tool calls they tried to cause.
        The match is exact: a subdomain is a different domain. seen false means only
        that the grid has not recorded it, not that it is safe."""
        return await lookup("domain", domain)

    @server.tool(structured_output=False)
    async def lookup_payload(sha256: str) -> str:
        """Look up a payload on the Tripvane grid by sha256: the hex sha256 of the text
        after lowercasing it, collapsing every run of whitespace to one space and
        stripping both ends. seen false means only that the grid has not recorded this
        exact text, not that it is safe."""
        return await lookup("payload", sha256)

    return server


def _error_message(response: httpx2.Response) -> str:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if response.status_code == 429:
        message = "the Tripvane API's daily lookup limit is reached"
        if retry := response.headers.get("retry-after"):
            message += f"; it resets in {retry} seconds"
        return message + " (an API key in TRIPVANE_API_KEY raises the limit)"
    if response.status_code == 401:
        return "the Tripvane API refused TRIPVANE_API_KEY: it is not a valid key"
    if response.status_code == 422:
        return f"the Tripvane API rejected the value: {detail or 'invalid'}"
    return f"the Tripvane API answered HTTP {response.status_code}"


def main() -> None:
    create_server().run("stdio")
