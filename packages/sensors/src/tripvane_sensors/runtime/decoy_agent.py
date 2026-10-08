"""The decoy agent: a tool-use loop in which no tool ever runs.

The model sees a normal set of tools. Every call it makes is recorded as a
ToolCallAttempted event and answered with the tool's static fake result. Every model
response is recorded as a ModelTurn. The model is called only after the cheap gate
passes and only while the daily budget allows it.
"""

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import anthropic
from anthropic.types import Message

from tripvane_core.events import (
    EndReason,
    InputReceived,
    ModelTurn,
    SessionEnded,
    SessionStarted,
    ToolCallAttempted,
)
from tripvane_sensors.runtime.budget import Budget
from tripvane_sensors.runtime.canary import Canaries, inject_into_prompt, inject_into_tools
from tripvane_sensors.runtime.gate import cheap_gate, new_recent_hashes
from tripvane_sensors.runtime.tools import DecoyTool

MODEL = "claude-haiku-5-5"
MAX_TURNS = 6
MAX_TOKENS = 400

AnyEvent = SessionStarted | InputReceived | ModelTurn | ToolCallAttempted | SessionEnded


class ModelClient(Protocol):
    """The part of anthropic.Anthropic the agent uses; MockModelClient implements it too."""

    @property
    def messages(self) -> Any: ...  # noqa: ANN401


class Sink(Protocol):
    def emit(self, event: AnyEvent) -> None: ...


def _now() -> datetime:
    return datetime.now(UTC)


class DecoyAgent:
    def __init__(
        self,
        system_prompt_path: Path,
        tools: Iterable[DecoyTool],
        client: ModelClient,
        sink: Sink,
        budget: Budget,
        *,
        canaries: Canaries | None = None,
        now: Callable[[], datetime] = _now,
    ) -> None:
        # Canaries come from CANARY_API_KEY and CANARY_DB_PASSWORD unless given explicitly.
        if canaries is None:
            canaries = Canaries.from_env()
        prompt = system_prompt_path.read_text(encoding="utf-8")
        self.system_prompt = inject_into_prompt(prompt, canaries)
        self.tools = {tool.name: tool for tool in inject_into_tools(tools, canaries)}
        self.client = client
        self.sink = sink
        self.budget = budget
        self.recent_hashes = new_recent_hashes()
        self._now = now
        # The system block carries the cache breakpoint, so the tool definitions
        # (rendered before it) and the system prompt are cached together.
        self._system = [
            {"type": "text", "text": self.system_prompt, "cache_control": {"type": "ephemeral"}}
        ]
        self._tool_defs = [tool.to_api() for tool in self.tools.values()]

    def run(self, session_id: str, input_event: InputReceived) -> list[AnyEvent]:
        """Record input_event, run the decoy loop on it, and return every event emitted.

        Events continue the session's sequence from input_event.event_seq. The last event
        is always a SessionEnded.
        """
        if input_event.session_id != session_id:
            raise ValueError("input_event belongs to a different session")
        recorder = _Recorder(input_event, self.sink, self._now)
        recorder.emit_input(input_event)
        recorder.end(self._loop(input_event.raw_text, recorder))
        return recorder.events

    def _loop(self, text: str, recorder: "_Recorder") -> EndReason:
        if not cheap_gate(text, self.recent_hashes):
            return "gate_rejected"
        messages: list[dict[str, Any]] = [{"role": "user", "content": text}]
        for _ in range(MAX_TURNS):
            if not self.budget.allows_call():
                return "budget_exhausted"
            try:
                response: Message = self.client.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=self._system,
                    tools=self._tool_defs,
                    messages=messages,
                    thinking={"type": "disabled"},
                )
            except anthropic.APIError:
                return "error"
            self.budget.record(_tokens_spent(response))
            recorder.model_turn(response)
            tool_results = [
                self._answer(block, recorder)
                for block in response.content
                if block.type == "tool_use"
            ]
            if response.stop_reason != "tool_use" or not tool_results:
                return "completed"
            assistant_content = [block.model_dump(exclude_none=True) for block in response.content]
            messages.append({"role": "assistant", "content": assistant_content})
            messages.append({"role": "user", "content": tool_results})
        return "completed"

    def _answer(self, block: Any, recorder: "_Recorder") -> dict[str, Any]:  # noqa: ANN401
        """Record one tool call and build its tool_result from the fake result."""
        arguments = block.input if isinstance(block.input, dict) else {"input": block.input}
        tool = self.tools.get(block.name)
        if tool is None:
            fake = f"Error: unknown tool {block.name}"
        else:
            fake = tool.fake_result(arguments)
        recorder.tool_call(block.name, arguments, fake)
        return {"type": "tool_result", "tool_use_id": block.id, "content": fake}


def _tokens_spent(response: Message) -> int:
    usage = response.usage
    return (
        usage.input_tokens
        + (usage.cache_creation_input_tokens or 0)
        + (usage.cache_read_input_tokens or 0)
        + usage.output_tokens
    )


class _Recorder:
    """Stamps events for one run with the session's identity and the next sequence number."""

    def __init__(self, input_event: InputReceived, sink: Sink, now: Callable[[], datetime]) -> None:
        self._common = {
            "sensor_id": input_event.sensor_id,
            "session_id": input_event.session_id,
            "source": input_event.source,
        }
        self._next_seq = input_event.event_seq
        self._sink = sink
        self._now = now
        self.events: list[AnyEvent] = []

    def emit_input(self, event: InputReceived) -> None:
        self._emit(event)

    def model_turn(self, response: Message) -> None:
        usage = response.usage
        text = "".join(block.text for block in response.content if block.type == "text")
        self._emit(
            ModelTurn(
                **self._stamp(),
                model=response.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
                cache_read_input_tokens=usage.cache_read_input_tokens or 0,
                stop_reason=response.stop_reason or "unknown",
                assistant_text=text,
            )
        )

    def tool_call(self, name: str, arguments: dict[str, Any], fake_result: str) -> None:
        self._emit(
            ToolCallAttempted(
                **self._stamp(), tool_name=name, arguments=arguments, fake_result=fake_result
            )
        )

    def end(self, reason: EndReason) -> None:
        self._emit(SessionEnded(**self._stamp(), reason=reason))

    def _stamp(self) -> dict[str, Any]:
        return {**self._common, "event_seq": self._next_seq, "ts": self._now()}

    def _emit(self, event: AnyEvent) -> None:
        self._sink.emit(event)
        self.events.append(event)
        self._next_seq = event.event_seq + 1
