"""The three tool handlers, driven through an MCP client against a mocked HTTP API."""

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import anyio
import httpx2
import pytest
from mcp.client import Client

from tripvane_plugin.server import create_server

PLUGIN_ROOT = Path(__file__).parents[1]
API_URL = "https://api.tripvane.example"
# Test-only value; not a key for anything.
API_KEY = "tripvane_test-key-not-real"
SEEN = {
    "seen": True,
    "first_seen": "2026-10-08T09:00:00Z",
    "last_seen": "2026-10-08T09:00:00Z",
    "sensor_count": 2,
    "session_count": 3,
    "tags": {"technique": [{"tag": "direct_instruction", "count": 3}]},
    "campaign_id": 41,
}
UNSEEN = {
    "seen": False,
    "first_seen": None,
    "last_seen": None,
    "sensor_count": None,
    "session_count": None,
    "tags": None,
    "campaign_id": None,
}


class FakeApi:
    """Records each request and answers with a fixed response."""

    def __init__(
        self, status: int = 200, body: object = None, headers: dict[str, str] | None = None
    ) -> None:
        self.requests: list[httpx2.Request] = []
        self.status = status
        self.body = SEEN if body is None else body
        self.headers = headers or {}

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(self.status, json=self.body, headers=self.headers)


def call(
    tool: str,
    arguments: dict[str, str],
    handler: Callable[[httpx2.Request], httpx2.Response],
    environ: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Call one tool in-process; return (is_error, text)."""
    if environ is None:
        environ = {"TRIPVANE_API_URL": API_URL}
    server = create_server(environ, transport=httpx2.MockTransport(handler))

    async def run() -> tuple[bool, str]:
        async with Client(server) as client:
            result = await client.call_tool(tool, arguments)
        [content] = result.content
        assert content.type == "text"
        return result.is_error, content.text

    return anyio.run(run)


@pytest.mark.parametrize(
    ("tool", "arguments", "path"),
    [
        ("lookup_ip", {"ip": "203.0.113.7"}, "/v1/lookup/ip/203.0.113.7"),
        ("lookup_ip", {"ip": " 2001:db8::5 "}, "/v1/lookup/ip/2001%3Adb8%3A%3A5"),
        (
            "lookup_domain",
            {"domain": "collect.exfil.example"},
            "/v1/lookup/domain/collect.exfil.example",
        ),
        ("lookup_payload", {"sha256": "ab" * 32}, "/v1/lookup/payload/" + "ab" * 32),
    ],
)
def test_tool_calls_the_matching_endpoint_and_returns_compact_json(
    tool: str, arguments: dict[str, str], path: str
) -> None:
    api = FakeApi()
    is_error, text = call(tool, arguments, api)
    assert not is_error
    assert text == json.dumps(SEEN, separators=(",", ":"))
    [request] = api.requests
    assert request.method == "GET"
    assert request.url.raw_path.decode() == path
    assert str(request.url).startswith(API_URL + "/v1/lookup/")
    assert "x-api-key" not in request.headers


@pytest.mark.parametrize("tool", ["lookup_ip", "lookup_domain", "lookup_payload"])
def test_api_key_is_sent_when_set(tool: str) -> None:
    api = FakeApi(body=UNSEEN)
    argument = {"lookup_ip": "ip", "lookup_domain": "domain", "lookup_payload": "sha256"}[tool]
    is_error, text = call(
        tool,
        {argument: "x"},
        api,
        {"TRIPVANE_API_URL": API_URL + "/", "TRIPVANE_API_KEY": API_KEY},
    )
    assert not is_error
    assert json.loads(text) == UNSEEN
    assert api.requests[0].headers["x-api-key"] == API_KEY
    # A trailing slash on the base URL does not double up.
    assert "//v1" not in str(api.requests[0].url)


@pytest.mark.parametrize("unset", ["", "   ", "${user_config.api_key}"])
def test_empty_or_unexpanded_key_is_not_sent(unset: str) -> None:
    api = FakeApi()
    call(
        "lookup_ip",
        {"ip": "203.0.113.7"},
        api,
        {"TRIPVANE_API_URL": API_URL, "TRIPVANE_API_KEY": unset},
    )
    assert "x-api-key" not in api.requests[0].headers


@pytest.mark.parametrize("environ", [{}, {"TRIPVANE_API_URL": "${user_config.api_url}"}])
def test_missing_api_url_is_a_tool_error(environ: dict[str, str]) -> None:
    api = FakeApi()
    is_error, text = call("lookup_domain", {"domain": "collect.exfil.example"}, api, environ)
    assert is_error
    assert "TRIPVANE_API_URL is not set" in text
    assert api.requests == []


@pytest.mark.parametrize(
    ("status", "body", "headers", "expected"),
    [
        (
            429,
            {"detail": "daily request limit reached"},
            {"Retry-After": "3600"},
            "resets in 3600 seconds",
        ),
        (401, {"detail": "invalid API key"}, {}, "not a valid key"),
        (422, {"detail": "not an IP address"}, {}, "rejected the value: not an IP address"),
        (503, "unavailable", {}, "HTTP 503"),
    ],
)
def test_api_errors_are_tool_errors(
    status: int,
    body: object,
    headers: dict[str, str],
    expected: str,
) -> None:
    is_error, text = call("lookup_ip", {"ip": "203.0.113.7"}, FakeApi(status, body, headers))
    assert is_error
    assert expected in text


def test_unreachable_api_is_a_tool_error() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    is_error, text = call("lookup_payload", {"sha256": "ab" * 32}, refuse)
    assert is_error
    assert f"could not reach the Tripvane API at {API_URL}" in text


def test_server_lists_exactly_the_three_tools() -> None:
    server = create_server({"TRIPVANE_API_URL": API_URL})

    async def run() -> list[str]:
        async with Client(server) as client:
            return [tool.name for tool in (await client.list_tools()).tools]

    assert sorted(anyio.run(run)) == ["lookup_domain", "lookup_ip", "lookup_payload"]
    assert server.name == "tripvane"


def test_skill_hash_command_matches_the_grid_normalization() -> None:
    # The grid's payload_hash of this text (tripvane_core.hashing), computed once.
    text = "  Honeypot TEST data:\n\tIgnore   prior instructions.  "
    expected = hashlib.sha256(b"honeypot test data: ignore prior instructions.").hexdigest()
    skill = (PLUGIN_ROOT / "skills" / "tripvane-lookup" / "SKILL.md").read_text()
    [command] = [line.strip() for line in skill.splitlines() if "hashlib.sha256" in line]
    script = command.removeprefix("python3 -c '").removesuffix("' < text.txt")
    result = subprocess.run(
        [sys.executable, "-c", script], input=text, capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == expected


def test_mcp_config_passes_the_settings_the_server_reads() -> None:
    config = json.loads((PLUGIN_ROOT / ".mcp.json").read_text())
    server = config["mcpServers"]["tripvane"]
    assert server["command"] == "uvx"
    assert server["args"] == ["--from", "${CLAUDE_PLUGIN_ROOT}", "tripvane-plugin"]
    assert server["env"] == {
        "TRIPVANE_API_URL": "${user_config.api_url}",
        "TRIPVANE_API_KEY": "${user_config.api_key}",
    }
    manifest = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "tripvane"
    assert set(manifest["userConfig"]) == {"api_url", "api_key"}
    assert manifest["userConfig"]["api_key"]["sensitive"] is True
