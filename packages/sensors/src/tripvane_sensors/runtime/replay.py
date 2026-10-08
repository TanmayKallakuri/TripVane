"""Replay harness: run recorded sessions through DecoyAgent with a scripted model.

Usage: python -m tripvane_sensors.runtime.replay FIXTURES_DIR

For every FIXTURES_DIR/<name>.jsonl (a recorded session, one event per line) the
input_received event is run through a DecoyAgent whose model client is a
MockModelClient playing <name>.script.json. The events the agent emits are compared
with <name>.expected.jsonl, ignoring ts and session_id. Any difference, an unused
script turn or a script that runs out makes the exit status non-zero.

<name>.script.json is a JSON list with one entry per model response:
{"content": [content blocks], "stop_reason": "...", "usage": {token counts}}. Content
blocks use the Messages API shapes; tool_use blocks need only name and input, and get
deterministic ids.
"""

import copy
import difflib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anthropic.types import Message
from pydantic import TypeAdapter

from tripvane_core.events import Event, InputReceived
from tripvane_sensors.runtime.budget import Budget
from tripvane_sensors.runtime.canary import Canaries
from tripvane_sensors.runtime.decoy_agent import AnyEvent, DecoyAgent
from tripvane_sensors.runtime.tools import STANDARD_TOOLS

# Fake canary values for replay only. They are not credentials for anything.
REPLAY_CANARIES = Canaries(
    api_key="tvk_live_7Hq2Xr9LmB4cN8sW1zKp3D6f",
    db_password="Wintergreen-Replica-0471",
)
REPLAY_BUDGET = 1_000_000
IGNORED_FIELDS = ("ts", "session_id")
SYSTEM_PROMPT = "system.md"

_EVENTS = TypeAdapter(Event)


class ScriptExhausted(Exception):
    """The agent asked for more model responses than the script holds."""


class _ScriptedMessages:
    def __init__(self, turns: list[dict[str, Any]]) -> None:
        self._turns = list(turns)
        self.requests: list[dict[str, Any]] = []

    @property
    def remaining(self) -> int:
        return len(self._turns)

    def create(self, **request: Any) -> Message:  # noqa: ANN401
        self.requests.append(copy.deepcopy(request))
        if not self._turns:
            raise ScriptExhausted(f"no scripted response for model call {len(self.requests)}")
        turn = self._turns.pop(0)
        content = []
        for index, block in enumerate(turn["content"]):
            if block["type"] == "tool_use":
                block = {"id": f"toolu_replay_{len(self.requests)}_{index}", **block}
            content.append(block)
        return Message.model_validate(
            {
                "id": f"msg_replay_{len(self.requests)}",
                "type": "message",
                "role": "assistant",
                "model": request["model"],
                "content": content,
                "stop_reason": turn["stop_reason"],
                "stop_sequence": None,
                "usage": turn["usage"],
            }
        )


class MockModelClient:
    """Stands in for anthropic.Anthropic: messages.create() plays a script in order."""

    def __init__(self, turns: list[dict[str, Any]]) -> None:
        self.messages = _ScriptedMessages(turns)


class ListSink:
    def __init__(self) -> None:
        self.events: list[AnyEvent] = []

    def emit(self, event: AnyEvent) -> None:
        self.events.append(event)


def comparable(event: dict[str, Any]) -> str:
    """One event as a stable JSON line without the fields replay ignores."""
    kept = {key: value for key, value in event.items() if key not in IGNORED_FIELDS}
    return json.dumps(kept, sort_keys=True)


def load_input(session_path: Path) -> InputReceived:
    lines = session_path.read_text(encoding="utf-8").splitlines()
    events = [_EVENTS.validate_json(line) for line in lines if line.strip()]
    inputs = [event for event in events if isinstance(event, InputReceived)]
    if len(inputs) != 1:
        raise ValueError(f"{session_path.name}: expected one input_received, found {len(inputs)}")
    return inputs[0]


def replay_session(session_path: Path, system_prompt: Path) -> list[str]:
    """Replay one recorded session. Returns the problems found; empty means it matches."""
    name = session_path.name.removesuffix(".jsonl")
    script_path = session_path.with_name(f"{name}.script.json")
    expected_path = session_path.with_name(f"{name}.expected.jsonl")
    input_event = load_input(session_path)
    client = MockModelClient(json.loads(script_path.read_text(encoding="utf-8")))
    sink = ListSink()
    agent = DecoyAgent(
        system_prompt,
        STANDARD_TOOLS,
        client,
        sink,
        Budget(REPLAY_BUDGET),
        canaries=REPLAY_CANARIES,
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )
    try:
        agent.run(input_event.session_id, input_event)
    except ScriptExhausted as exc:
        return [f"{name}: {exc}"]

    produced = [comparable(event.model_dump(mode="json")) for event in sink.events]
    expected = [
        comparable(json.loads(line))
        for line in expected_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    problems = list(
        difflib.unified_diff(
            expected, produced, f"{name}.expected.jsonl", f"{name} (replayed)", lineterm=""
        )
    )
    if client.messages.remaining:
        problems.append(f"{name}: {client.messages.remaining} scripted responses were not used")
    return problems


def session_files(fixtures_dir: Path) -> list[Path]:
    return sorted(
        path for path in fixtures_dir.glob("*.jsonl") if not path.name.endswith(".expected.jsonl")
    )


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m tripvane_sensors.runtime.replay FIXTURES_DIR", file=sys.stderr)
        return 2
    fixtures_dir = Path(argv[0])
    sessions = session_files(fixtures_dir)
    if not sessions:
        print(f"replay: no sessions in {fixtures_dir}", file=sys.stderr)
        return 1
    failed = 0
    for session in sessions:
        problems = replay_session(session, fixtures_dir / SYSTEM_PROMPT)
        status = "FAIL" if problems else "ok"
        print(f"{status:4} {session.name}")
        for line in problems:
            print(f"     {line}")
        failed += bool(problems)
    print(f"replay: {len(sessions) - failed} of {len(sessions)} sessions match")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
