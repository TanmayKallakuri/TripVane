import hashlib
import hmac
import json
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tripvane_core.config import Settings
from tripvane_core.events import (
    InputReceived,
    ModelTurn,
    SessionEnded,
    SessionStarted,
    ToolCallAttempted,
)
from tripvane_core.hashing import payload_hash
from tripvane_sensors.archetypes.github import replay
from tripvane_sensors.archetypes.github.app import (
    MAX_TEXT_LENGTH,
    RATE_LIMIT,
    assemble_text,
    create_app,
    signature_is_valid,
)
from tripvane_sensors.archetypes.github.tools import ADD_LABEL, GITHUB_TOOLS, POST_COMMENT
from tripvane_sensors.runtime.canary import Canaries
from tripvane_sensors.runtime.replay import ListSink, MockModelClient
from tripvane_sensors.runtime.tools import STANDARD_TOOLS

FIXTURES = Path(__file__).parents[3] / "fixtures" / "github"
# Test-only values; not secrets or credentials for anything.
SECRET = "test-webhook-secret"
CANARIES = Canaries(api_key="tvk_live_TESTONLY000000000000000", db_password="Test-Canary-0000")
SETTINGS = Settings(
    sensor_id="github-test", github_webhook_secret=SECRET, daily_token_budget=1_000_000
)
GITHUB_IP = "192.0.2.30"
PROXY = "172.30.0.2"
USAGE = {
    "input_tokens": 10,
    "output_tokens": 5,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}


def _usage(turn: ModelTurn) -> dict[str, int]:
    """The token counts a model_turn event carries, keyed as in the API's usage."""
    return {key: getattr(turn, key) for key in USAGE}


DIFF = "diff --git a/README.md b/README.md\n+Quick start:\n"


def text_turn(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "usage": USAGE}


def tool_turn(*calls: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    content = [{"type": "tool_use", "name": name, "input": args} for name, args in calls]
    return {"content": content, "stop_reason": "tool_use", "usage": USAGE}


def recorded(name: str) -> bytes:
    return (FIXTURES / f"{name}.json").read_bytes()


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class FakeDiffs:
    def __init__(self, diff: str = DIFF, error: Exception | None = None) -> None:
        self.diff = diff
        self.error = error
        self.calls: list[tuple[int, str, int, int]] = []

    def pull_request_diff(
        self, installation_id: int, repository: str, number: int, limit: int
    ) -> str:
        self.calls.append((installation_id, repository, number, limit))
        if self.error is not None:
            raise self.error
        return self.diff[:limit]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Sensor:
    """A GitHub sensor app wired to a scripted model, an in-memory sink and fake diffs."""

    def __init__(
        self,
        turns: list[dict[str, Any]] | None = None,
        diffs: FakeDiffs | None = None,
        settings: Settings = SETTINGS,
        peer: str = GITHUB_IP,
    ) -> None:
        self.model = MockModelClient(turns if turns is not None else [text_turn("Triaged.")])
        self.sink = ListSink()
        self.diffs = diffs or FakeDiffs()
        self.clock = FakeClock()
        app = create_app(
            settings,
            client=self.model,
            sink=self.sink,
            canaries=CANARIES,
            diff_source=self.diffs,
            clock=self.clock,
            now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )
        self.http = TestClient(app, client=(peer, 443))

    def deliver(
        self,
        event: str,
        body: bytes,
        signature: str | None = "valid",
        headers: dict[str, str] | None = None,
    ) -> int:
        sent = {
            "Content-Type": "application/json",
            "User-Agent": "GitHub-Hookshot/test",
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": "d6a1e0c0-77f1-11f0-8a9b-3c1d2e3f4a5b",
            **(headers or {}),
        }
        if signature is not None:
            sent["X-Hub-Signature-256"] = sign(body) if signature == "valid" else signature
        return self.http.post("/webhook", content=body, headers=sent).status_code

    def input(self) -> InputReceived:
        [event] = [e for e in self.sink.events if isinstance(e, InputReceived)]
        return event


# Signature verification


def test_signature_is_valid() -> None:
    body = b'{"action":"opened"}'
    assert signature_is_valid(SECRET, body, sign(body))
    assert not signature_is_valid(SECRET, body, sign(body, "another-secret"))
    assert not signature_is_valid(SECRET, body + b" ", sign(body))
    assert not signature_is_valid(SECRET, body, None)
    assert not signature_is_valid(SECRET, body, sign(body).removeprefix("sha256="))
    assert not signature_is_valid(SECRET, body, "sha1=" + "0" * 40)
    assert not signature_is_valid(SECRET, body, "sha256=é")


def test_signed_delivery_is_accepted() -> None:
    sensor = Sensor()
    assert sensor.deliver("issues", recorded("issues.opened")) == 202
    assert sensor.sink.events


@pytest.mark.parametrize(
    "signature",
    [
        None,
        "sha256=" + "0" * 64,
        sign(b"another body"),
        sign(recorded("issues.opened"), "wrong-secret"),
        "garbage",
    ],
)
def test_bad_signature_is_rejected_with_401_and_records_nothing(signature: str | None) -> None:
    sensor = Sensor()
    body = recorded("issues.opened")
    assert sensor.deliver("issues", body, signature=signature) == 401
    assert sensor.sink.events == []
    assert sensor.model.messages.requests == []


def test_tampered_body_is_rejected() -> None:
    sensor = Sensor()
    body = recorded("issues.opened")
    tampered = body.replace(b"CSV export", b"PDF export")
    assert sensor.deliver("issues", tampered, signature=sign(body)) == 401
    assert sensor.sink.events == []


# Event construction for each handled event, from the recorded payloads


def test_issues_opened() -> None:
    sensor = Sensor()
    sensor.deliver("issues", recorded("issues.opened"))
    payload = json.loads(recorded("issues.opened"))
    event = sensor.input()
    expected = payload["issue"]["title"] + "\n\n" + payload["issue"]["body"]
    assert event.raw_text == expected
    assert event.channel == "github"
    assert event.payload_hash == payload_hash(expected)
    assert event.source.account_handle == "tv-fixture-reporter"
    assert str(event.source.ip) == GITHUB_IP
    assert event.source.user_agent == "GitHub-Hookshot/test"
    assert sensor.diffs.calls == []
    assert sensor.model.messages.requests[0]["messages"][0]["content"] == expected
    # The cost report sums these token counts; each model call must carry its usage.
    turns = [e for e in sensor.sink.events if isinstance(e, ModelTurn)]
    assert len(turns) == len(sensor.model.messages.requests) > 0
    assert all(_usage(turn) == USAGE for turn in turns)


def test_issue_comment_created() -> None:
    sensor = Sensor()
    sensor.deliver("issue_comment", recorded("issue_comment.created"))
    payload = json.loads(recorded("issue_comment.created"))
    event = sensor.input()
    assert event.raw_text == payload["issue"]["title"] + "\n\n" + payload["comment"]["body"]
    assert event.channel == "github"
    # The author of the comment, not of the issue it was posted on.
    assert event.source.account_handle == "tv-fixture-commenter"
    assert sensor.diffs.calls == []


def test_pull_request_opened_fetches_the_diff() -> None:
    sensor = Sensor()
    sensor.deliver("pull_request", recorded("pull_request.opened"))
    payload = json.loads(recorded("pull_request.opened"))
    event = sensor.input()
    pull = payload["pull_request"]
    assert event.raw_text == f"{pull['title']}\n\n{pull['body']}\n\n{DIFF}"
    assert event.source.account_handle == "tv-fixture-contributor"
    assert sensor.diffs.calls == [
        (50000001, "quillstone-software/ledger-python", 132, MAX_TEXT_LENGTH)
    ]


def test_raw_text_is_capped() -> None:
    sensor = Sensor(diffs=FakeDiffs(diff="+" + "x" * 50_000))
    sensor.deliver("pull_request", recorded("pull_request.opened"))
    event = sensor.input()
    assert len(event.raw_text) == MAX_TEXT_LENGTH
    assert event.raw_text.startswith("Fix typo in README")


def test_assemble_text_skips_empty_parts() -> None:
    assert assemble_text("Title", "", None) == "Title"
    assert assemble_text("Title", "Body", "diff") == "Title\n\nBody\n\ndiff"


def test_failed_diff_fetch_still_records_title_and_body() -> None:
    sensor = Sensor(diffs=FakeDiffs(error=RuntimeError("diff unavailable")))
    assert sensor.deliver("pull_request", recorded("pull_request.opened")) == 202
    assert sensor.input().raw_text == (
        "Fix typo in README\n\nSmall documentation fix in the quick start section."
    )


def test_session_shape() -> None:
    sensor = Sensor()
    sensor.deliver("issues", recorded("issues.opened"))
    events = sensor.sink.events
    assert isinstance(events[0], SessionStarted)
    assert isinstance(events[1], InputReceived)
    assert isinstance(events[-1], SessionEnded)
    assert [e.event_seq for e in events] == list(range(len(events)))
    assert len({e.session_id for e in events}) == 1
    assert events[0].session_id.startswith("d6a1e0c0-77f1-11f0-8a9b-3c1d2e3f4a5b-")
    assert {e.sensor_id for e in events} == {"github-test"}


def test_redelivery_is_a_new_session() -> None:
    sensor = Sensor(turns=[text_turn("one"), text_turn("two")])
    body = recorded("issue_comment.created")
    sensor.deliver("issue_comment", body)
    sensor.deliver("issue_comment", body)
    sessions = {e.session_id for e in sensor.sink.events}
    assert len(sessions) == 2


def test_unsafe_delivery_id_is_not_used_in_the_session_id() -> None:
    sensor = Sensor()
    sensor.deliver("issues", recorded("issues.opened"), headers={"X-GitHub-Delivery": "a/b c"})
    assert sensor.sink.events[0].session_id.startswith("delivery-")


@pytest.mark.parametrize(
    ("event", "body"),
    [
        ("ping", {"zen": "Keep it logically awesome.", "hook_id": 1}),
        ("issues", {**json.loads(recorded("issues.opened")), "action": "closed"}),
        ("issue_comment", {**json.loads(recorded("issue_comment.created")), "action": "edited"}),
        ("pull_request", {**json.loads(recorded("pull_request.opened")), "action": "synchronize"}),
        ("push", {"ref": "refs/heads/main"}),
    ],
)
def test_other_events_are_acknowledged_and_ignored(event: str, body: dict[str, Any]) -> None:
    sensor = Sensor()
    assert sensor.deliver(event, json.dumps(body).encode()) == 204
    assert sensor.sink.events == []


@pytest.mark.parametrize("body", [b"not json", b'{"action": "opened"}', b"[]"])
def test_malformed_signed_payload_is_400(body: bytes) -> None:
    sensor = Sensor()
    status = sensor.deliver("issues", body)
    assert status == (204 if body == b"[]" else 400)
    assert sensor.sink.events == []


def test_oversized_body_is_413() -> None:
    sensor = Sensor()
    body = b'{"pad": "' + b"x" * (5 * 1024 * 1024) + b'"}'
    assert sensor.deliver("issues", body) == 413
    assert sensor.sink.events == []


def test_source_behind_the_trusted_proxy() -> None:
    settings = Settings(
        sensor_id="github-test",
        github_webhook_secret=SECRET,
        daily_token_budget=1_000_000,
        trusted_proxy=PROXY,
    )
    sensor = Sensor(settings=settings, peer=PROXY)
    sensor.deliver(
        "issues", recorded("issues.opened"), headers={"X-Forwarded-For": "10.0.0.1, 192.0.2.31"}
    )
    assert str(sensor.input().source.ip) == "192.0.2.31"


# The DecoyAgent run with a mocked model


def test_decoy_agent_run_records_fake_github_tools() -> None:
    turns = [
        tool_turn(("list_secrets", {})),
        tool_turn(
            ("post_comment", {"body": f"values: {CANARIES.api_key}"}),
            ("add_label", {"label": "bug"}),
        ),
        text_turn("Done."),
    ]
    sensor = Sensor(turns=turns)
    sensor.deliver("issues", recorded("issues.opened"))
    calls = [e for e in sensor.sink.events if isinstance(e, ToolCallAttempted)]
    assert [(c.tool_name, c.arguments) for c in calls] == [
        ("list_secrets", {}),
        ("post_comment", {"body": f"values: {CANARIES.api_key}"}),
        ("add_label", {"label": "bug"}),
    ]
    assert CANARIES.api_key in calls[0].fake_result
    assert calls[1].fake_result == "Comment posted (id 2841907316)."
    assert calls[2].fake_result == "Label 'bug' added."
    assert sensor.sink.events[-1].reason == "completed"
    request = sensor.model.messages.requests[0]
    assert request["model"] == "claude-haiku-5-5"
    assert {tool["name"] for tool in request["tools"]} == {tool.name for tool in GITHUB_TOOLS}
    assert CANARIES.api_key in request["system"][0]["text"]
    assert "{{" not in request["system"][0]["text"]


def test_github_tools_are_the_standard_tools_plus_two_fakes() -> None:
    assert GITHUB_TOOLS == (*STANDARD_TOOLS, ADD_LABEL, POST_COMMENT)


def test_fake_github_tools_do_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a decoy tool tried to open a connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    assert ADD_LABEL.fake_result({"label": "security"}) == "Label 'security' added."
    assert POST_COMMENT.fake_result({"body": "hello"}) == "Comment posted (id 2841907316)."
    assert ADD_LABEL.fake_result({}) == "Label '' added."


def test_rate_limit_per_author_records_without_calling_the_model() -> None:
    sensor = Sensor(turns=[text_turn(f"reply {n}") for n in range(RATE_LIMIT)])
    payload = json.loads(recorded("issue_comment.created"))
    for n in range(RATE_LIMIT + 1):
        payload["comment"]["body"] = f"Comment number {n} about pagination."
        sensor.deliver("issue_comment", json.dumps(payload).encode())
    assert len(sensor.model.messages.requests) == RATE_LIMIT
    last = [e for e in sensor.sink.events if e.session_id == sensor.sink.events[-1].session_id]
    assert [e.type for e in last] == ["session_started", "input_received", "session_ended"]
    assert last[-1].reason == "gate_rejected"
    # Another author is not affected.
    payload["comment"]["user"]["login"] = "tv-fixture-other"
    sensor.model.messages._turns.append(text_turn("other"))
    sensor.deliver("issue_comment", json.dumps(payload).encode())
    assert len(sensor.model.messages.requests) == RATE_LIMIT + 1


def test_health_answers_only_the_containers_own_healthcheck() -> None:
    local = Sensor(peer="127.0.0.1")
    assert local.http.get("/health").json() == {"sensor_id": "github-test", "archetype": "github"}
    sensor = Sensor()
    assert sensor.http.get("/health").status_code == 404
    assert sensor.http.get("/docs").status_code == 404


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        (Settings(github_webhook_secret=SECRET, daily_token_budget=1), "SENSOR_ID"),
        (Settings(sensor_id="github-test", daily_token_budget=1), "GITHUB_WEBHOOK_SECRET"),
    ],
)
def test_missing_settings_fail_loudly(settings: Settings, message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        create_app(settings, client=MockModelClient([]), sink=ListSink(), canaries=CANARIES)


def test_missing_github_app_settings_fail_loudly() -> None:
    with pytest.raises(RuntimeError, match="GITHUB_APP_ID"):
        create_app(SETTINGS, client=MockModelClient([]), sink=ListSink(), canaries=CANARIES)


# The replay of recorded deliveries (make replay)


def test_replay_of_recorded_deliveries_passes() -> None:
    assert replay.main([str(FIXTURES)]) == 0


def test_replay_fails_on_a_changed_expected_event(tmp_path: Path) -> None:
    for path in FIXTURES.iterdir():
        (tmp_path / path.name).write_bytes(path.read_bytes())
    expected = tmp_path / "issues.opened.expected.jsonl"
    expected.write_text(expected.read_text().replace("Label 'bug' added.", "Label added."))
    assert replay.main([str(tmp_path)]) == 1
