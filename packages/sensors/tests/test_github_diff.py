import base64
import json
from datetime import UTC, datetime

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tripvane_core.config import Settings
from tripvane_sensors.archetypes.github.diff import GitHubApp

# A throwaway key generated for these tests; it belongs to no GitHub App.
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)
NOW = 1_790_000_000.0
DIFF = "diff --git a/README.md b/README.md\n+Quick start:\n"


class FakeGitHub:
    """Answers the two requests the App may make and records every request."""

    def __init__(self, diff: str = DIFF, token_lifetime: float = 3600.0) -> None:
        self.diff = diff
        self.token_lifetime = token_lifetime
        self.requests: list[httpx2.Request] = []
        self.clock = NOW

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path == "/app/installations/50000001/access_tokens":
            expires = self.clock + self.token_lifetime
            body = {
                "token": f"ghs_test{len(self.requests)}",
                "expires_at": datetime.fromtimestamp(expires, UTC).isoformat(),
            }
            return httpx2.Response(201, json=body)
        if request.method == "GET" and path == "/repos/quillstone-software/ledger-python/pulls/132":
            return httpx2.Response(200, text=self.diff)
        return httpx2.Response(404)

    def app(self) -> GitHubApp:
        client = httpx2.Client(transport=httpx2.MockTransport(self.handler))
        return GitHubApp("123456", PEM, http_client=client, clock=lambda: self.clock)


def test_fetches_the_diff_with_a_read_only_installation_token() -> None:
    github = FakeGitHub()
    diff = github.app().pull_request_diff(50000001, "quillstone-software/ledger-python", 132, 100)
    assert diff == DIFF
    token_request, diff_request = github.requests
    assert token_request.url.host == diff_request.url.host == "api.github.com"
    assert json.loads(token_request.content) == {"permissions": {"pull_requests": "read"}}
    app_jwt = token_request.headers["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(
        app_jwt, KEY.public_key(), algorithms=["RS256"], options={"verify_exp": False}
    )
    assert claims == {"iat": int(NOW) - 60, "exp": int(NOW) + 540, "iss": "123456"}
    assert diff_request.method == "GET"
    assert diff_request.headers["Accept"] == "application/vnd.github.diff"
    assert diff_request.headers["Authorization"] == "Bearer ghs_test1"


def test_only_the_token_exchange_and_reads_are_requested() -> None:
    github = FakeGitHub()
    app = github.app()
    for _ in range(3):
        app.pull_request_diff(50000001, "quillstone-software/ledger-python", 132, 100)
    methods = [(r.method, r.url.path.rsplit("/", 1)[-1]) for r in github.requests]
    assert methods == [("POST", "access_tokens"), ("GET", "132"), ("GET", "132"), ("GET", "132")]


def test_token_is_renewed_before_it_expires() -> None:
    github = FakeGitHub(token_lifetime=600.0)
    app = github.app()
    app.pull_request_diff(50000001, "quillstone-software/ledger-python", 132, 100)
    github.clock += 299
    app.pull_request_diff(50000001, "quillstone-software/ledger-python", 132, 100)
    github.clock += 2
    app.pull_request_diff(50000001, "quillstone-software/ledger-python", 132, 100)
    tokens = [r for r in github.requests if r.method == "POST"]
    assert len(tokens) == 2


def test_diff_is_read_only_up_to_the_limit() -> None:
    github = FakeGitHub(diff="+" + "x" * 100_000)
    diff = github.app().pull_request_diff(50000001, "quillstone-software/ledger-python", 132, 20)
    assert diff == "+" + "x" * 19


@pytest.mark.parametrize(
    ("repository", "number"),
    [("../../app/installations", 1), ("owner/repo/extra", 1), ("owner/repo", 0), ("", 5)],
)
def test_unexpected_repository_or_number_is_refused(repository: str, number: int) -> None:
    github = FakeGitHub()
    with pytest.raises(ValueError, match="not a pull request"):
        github.app().pull_request_diff(50000001, repository, number, 100)
    assert github.requests == []


def test_http_errors_raise() -> None:
    github = FakeGitHub()
    with pytest.raises(httpx2.HTTPStatusError):
        github.app().pull_request_diff(50000001, "quillstone-software/other", 1, 100)


def test_from_settings() -> None:
    settings = Settings(
        github_app_id="123456", github_app_private_key_b64=base64.b64encode(PEM).decode()
    )
    assert isinstance(GitHubApp.from_settings(settings), GitHubApp)
    with pytest.raises(RuntimeError, match="GITHUB_APP_PRIVATE_KEY_B64"):
        GitHubApp.from_settings(Settings(github_app_id="123456"))
    with pytest.raises(ValueError):
        GitHubApp("123456", b"not a key")
