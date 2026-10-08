"""Infrastructure lookalike tests: one per endpoint, plus unlisted paths and sessions.

All request data here is synthetic honeypot test data; addresses are RFC 5737
documentation addresses.
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from tripvane_core.config import Settings
from tripvane_core.events import InputReceived, ToolCallAttempted
from tripvane_core.hashing import payload_hash
from tripvane_sensors.archetypes.common import IDLE_TIMEOUT_SECONDS, SessionTracker
from tripvane_sensors.archetypes.infra import responses
from tripvane_sensors.archetypes.infra.app import MAX_BODY_BYTES, TOOL_NAME, create_app
from tripvane_sensors.runtime.replay import ListSink

SETTINGS = Settings(sensor_id="infra-test")
CLIENT_IP = "198.51.100.23"
PROXY = "172.30.0.2"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Sensor:
    def __init__(self, settings: Settings = SETTINGS) -> None:
        self.sink = ListSink()
        self.clock = FakeClock()
        self.app = create_app(
            settings,
            sink=self.sink,
            clock=self.clock,
            now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )

    @property
    def tracker(self) -> SessionTracker:
        return self.app.state.tracker

    def client(self, ip: str = CLIENT_IP) -> TestClient:
        return TestClient(self.app, client=(ip, 40000))

    def types(self) -> list[str]:
        return [event.type for event in self.sink.events]

    def request_events(self) -> tuple[InputReceived, ToolCallAttempted]:
        """The InputReceived and ToolCallAttempted of the only request made."""
        assert self.types() == ["session_started", "input_received", "tool_call_attempted"]
        _, received, attempted = self.sink.events
        assert isinstance(received, InputReceived) and isinstance(attempted, ToolCallAttempted)
        return received, attempted


@pytest.fixture
def sensor() -> Iterator[Sensor]:
    yield Sensor()


def _assert_recorded(
    sensor: Sensor, method: str, path: str, query: str, raw_text: str, body: object
) -> None:
    received, attempted = sensor.request_events()
    assert received.channel == "http"
    assert received.raw_text == raw_text
    assert received.payload_hash == payload_hash(raw_text)
    assert str(received.source.ip) == CLIENT_IP
    assert attempted.tool_name == TOOL_NAME
    assert attempted.arguments == {"method": method, "path": path, "query": query}
    assert json.loads(attempted.fake_result) == body
    assert [event.event_seq for event in sensor.sink.events] == [0, 1, 2]


def test_ollama_tags(sensor: Sensor) -> None:
    response = sensor.client().get("/api/tags")
    assert response.status_code == 200
    assert response.json() == responses.OLLAMA_TAGS
    assert response.json()["models"][0]["details"]["format"] == "gguf"
    _assert_recorded(sensor, "GET", "/api/tags", "", "", responses.OLLAMA_TAGS)


def test_ollama_generate_records_the_prompt_body(sensor: Sensor) -> None:
    body = '{"model":"llama3.1:8b","prompt":"Say hello in one word.","stream":false}'
    response = sensor.client().post(
        "/api/generate", content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 200
    assert response.json() == responses.OLLAMA_GENERATE
    assert response.json()["done"] is True
    _assert_recorded(sensor, "POST", "/api/generate", "", body, responses.OLLAMA_GENERATE)


def test_open_webui_auths(sensor: Sensor) -> None:
    response = sensor.client().get("/api/v1/auths/")
    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}
    _assert_recorded(
        sensor, "GET", "/api/v1/auths/", "", "", responses.OPEN_WEBUI_NOT_AUTHENTICATED
    )


def test_litellm_health(sensor: Sensor) -> None:
    response = sensor.client().get("/health")
    assert response.status_code == 200
    assert response.json() == responses.LITELLM_HEALTH
    assert response.json()["healthy_count"] == len(response.json()["healthy_endpoints"])
    _assert_recorded(sensor, "GET", "/health", "", "", responses.LITELLM_HEALTH)


def test_litellm_models_records_the_query_string(sensor: Sensor) -> None:
    response = sensor.client().get("/v1/models?return_wildcard_routes=true")
    assert response.status_code == 200
    assert response.json() == responses.LITELLM_MODELS
    assert response.json()["object"] == "list"
    query = "return_wildcard_routes=true"
    _assert_recorded(sensor, "GET", "/v1/models", query, query, responses.LITELLM_MODELS)


def test_langflow_flows(sensor: Sensor) -> None:
    response = sensor.client().get("/api/v1/flows")
    assert response.status_code == 200
    assert response.json() == responses.LANGFLOW_FLOWS
    assert {"id", "name", "data", "folder_id"} <= set(response.json()[0])
    _assert_recorded(sensor, "GET", "/api/v1/flows", "", "", responses.LANGFLOW_FLOWS)


def test_unlisted_path_is_recorded_and_answered_404(sensor: Sensor) -> None:
    body = '{"code": "print(1)"}'
    response = sensor.client().post("/api/v1/validate/code", content=body)
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    _assert_recorded(sensor, "POST", "/api/v1/validate/code", "", body, responses.NOT_FOUND)


def test_unlisted_path_keeps_the_raw_path(sensor: Sensor) -> None:
    response = sensor.client().get("/static/..%2f..%2fetc/passwd?x=1")
    assert response.status_code == 404
    _, attempted = sensor.request_events()
    assert attempted.arguments["path"] == "/static/..%2f..%2fetc/passwd"
    assert attempted.arguments["query"] == "x=1"


def test_listed_path_with_another_method_is_answered_405(sensor: Sensor) -> None:
    response = sensor.client().delete("/api/tags")
    assert response.status_code == 405
    _assert_recorded(sensor, "DELETE", "/api/tags", "", "", responses.METHOD_NOT_ALLOWED)


def test_oversized_body_is_recorded_truncated_and_answered_413(sensor: Sensor) -> None:
    body = "a" * (MAX_BODY_BYTES + 10)
    response = sensor.client().post("/api/generate", content=body)
    assert response.status_code == 413
    received, _ = sensor.request_events()
    assert received.raw_text == body[:MAX_BODY_BYTES]


def test_requests_from_one_address_share_a_session_until_it_goes_idle(sensor: Sensor) -> None:
    client = sensor.client()
    client.get("/api/tags")
    client.get("/v1/models")
    sensor.client("203.0.113.40").get("/api/tags")

    events = sensor.sink.events
    mine = [event for event in events if str(event.source.ip) == CLIENT_IP]
    theirs = [event for event in events if str(event.source.ip) == "203.0.113.40"]
    assert [event.type for event in mine] == [
        "session_started",
        "input_received",
        "tool_call_attempted",
        "input_received",
        "tool_call_attempted",
    ]
    assert len({event.session_id for event in mine}) == 1
    assert {event.session_id for event in theirs}.isdisjoint({mine[0].session_id})

    sensor.clock.now += IDLE_TIMEOUT_SECONDS - 1
    assert sensor.tracker.reap() == 0
    sensor.clock.now += 1
    assert sensor.tracker.reap() == 2
    assert sensor.types()[-2:] == ["session_ended", "session_ended"]

    client.get("/api/tags")
    restarted = sensor.sink.events[-3:]
    assert [event.type for event in restarted][0] == "session_started"
    assert restarted[0].session_id != mine[0].session_id


def test_open_sessions_end_when_the_sensor_stops() -> None:
    sensor = Sensor()
    with sensor.client() as client:
        client.get("/api/tags")
    assert sensor.types()[-1] == "session_ended"


def test_source_uses_the_forwarded_address_from_the_trusted_proxy() -> None:
    sensor = Sensor(Settings(sensor_id="infra-test", trusted_proxy=PROXY))
    sensor.client(PROXY).get(
        "/api/tags", headers={"X-Forwarded-For": f"192.0.2.1, {CLIENT_IP}", "User-Agent": "scan/1"}
    )
    received, _ = sensor.request_events()
    assert str(received.source.ip) == CLIENT_IP
    assert received.source.user_agent == "scan/1"


def test_missing_configuration_fails_loudly() -> None:
    with pytest.raises(RuntimeError, match="SENSOR_ID"):
        create_app(Settings())
    with pytest.raises(RuntimeError, match="COLLECTOR_URL"):
        create_app(SETTINGS)
