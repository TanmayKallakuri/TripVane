"""Read-only GitHub access as the sensor's GitHub App: the diff of a pull request.

The App is created with read-only permissions on issues, pull requests and contents
and nothing else, so its tokens cannot write to GitHub whatever this code did. This
module makes exactly two kinds of request, both to api.github.com:

- POST /app/installations/{id}/access_tokens: exchanges a short-lived App JWT for an
  installation token, narrowed further to pull_requests read. This creates a token for
  the App; it writes nothing in any repository.
- GET /repos/{owner}/{repo}/pulls/{number} with the diff media type: the diff text,
  read only up to the caller's limit.
"""

import base64
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Protocol, Self

import httpx2
import jwt
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from tripvane_core.config import Settings

API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
# Renew an installation token this long before GitHub says it expires.
TOKEN_MARGIN_SECONDS = 300.0
REQUEST_TIMEOUT_SECONDS = 10.0

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


class DiffSource(Protocol):
    def pull_request_diff(
        self, installation_id: int, repository: str, number: int, limit: int
    ) -> str: ...


class GitHubApp:
    def __init__(
        self,
        app_id: str,
        private_key_pem: bytes,
        *,
        http_client: httpx2.Client | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        # Fails here, at startup, if the key is not a PEM private key.
        self._private_key = load_pem_private_key(private_key_pem, password=None)
        self._app_id = app_id
        self._http = http_client or httpx2.Client(timeout=REQUEST_TIMEOUT_SECONDS)
        self._clock = clock
        self._tokens: dict[int, tuple[str, float]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        if settings.github_app_id is None or settings.github_app_private_key_b64 is None:
            raise RuntimeError("GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_B64 must be set")
        return cls(settings.github_app_id, base64.b64decode(settings.github_app_private_key_b64))

    def pull_request_diff(
        self, installation_id: int, repository: str, number: int, limit: int
    ) -> str:
        """Up to limit characters of the pull request's diff."""
        if not _REPOSITORY.match(repository) or number < 1:
            raise ValueError(f"not a pull request: {repository!r} #{number}")
        headers = {
            "Accept": "application/vnd.github.diff",
            "Authorization": f"Bearer {self._installation_token(installation_id)}",
            "X-GitHub-Api-Version": API_VERSION,
        }
        url = f"{API_URL}/repos/{repository}/pulls/{number}"
        parts: list[str] = []
        size = 0
        with self._http.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            for chunk in response.iter_text():
                parts.append(chunk)
                size += len(chunk)
                if size >= limit:
                    break
        return "".join(parts)[:limit]

    def _installation_token(self, installation_id: int) -> str:
        with self._lock:
            cached = self._tokens.get(installation_id)
            if cached is not None and self._clock() < cached[1] - TOKEN_MARGIN_SECONDS:
                return cached[0]
            response = self._http.post(
                f"{API_URL}/app/installations/{installation_id}/access_tokens",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self._app_jwt()}",
                    "X-GitHub-Api-Version": API_VERSION,
                },
                json={"permissions": {"pull_requests": "read"}},
            )
            response.raise_for_status()
            body = response.json()
            expires_at = datetime.fromisoformat(body["expires_at"]).timestamp()
            self._tokens[installation_id] = (body["token"], expires_at)
            return body["token"]

    def _app_jwt(self) -> str:
        """The App's own JWT: valid for nine minutes, backdated a minute for clock drift."""
        now = int(self._clock())
        claims = {"iat": now - 60, "exp": now + 540, "iss": self._app_id}
        return jwt.encode(claims, self._private_key, algorithm="RS256")  # type: ignore[arg-type]
