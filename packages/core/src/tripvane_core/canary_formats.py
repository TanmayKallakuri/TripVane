"""Canary credential formats: fake secrets that only Tripvane can recognise.

A canary secret is an invented prefix in the style of a common API key, random base62
characters, and a 4-character checksum: a keyed hash (HMAC-SHA256 with CANARY_HMAC_KEY)
over the prefix and the random part. None of the prefixes belongs to a real vendor, so a
canary is never a working credential anywhere, and without CANARY_HMAC_KEY it cannot be
told apart from any other random string of the same shape.

The API (minting, the canary URL endpoint) and the collector (spotting canaries in tool
call arguments) both use this module, so it lives in core.
"""

import hashlib
import hmac
import re
import secrets
import string
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from tripvane_core.config import Settings

BASE62 = string.digits + string.ascii_uppercase + string.ascii_lowercase
CHECKSUM_LENGTH = 4


@dataclass(frozen=True)
class CanaryFormat:
    kind: str
    prefix: str
    random_length: int

    @property
    def length(self) -> int:
        return len(self.prefix) + self.random_length + CHECKSUM_LENGTH


FORMATS: dict[str, CanaryFormat] = {
    fmt.kind: fmt
    for fmt in (
        # A secret API key in live mode, like the keys payment and SaaS APIs issue.
        CanaryFormat("api_key", "ckl_live_", 28),
        # A personal access token, like the ones code hosts and registries issue.
        CanaryFormat("access_token", "vpat_", 36),
        # A generated database password, like the ones managed Postgres providers issue.
        CanaryFormat("db_password", "xpg_", 24),
    )
}

# A prefix that is not glued to a longer word, followed by the base62 tail.
_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    + "|".join(re.escape(fmt.prefix) for fmt in FORMATS.values())
    + r")[0-9A-Za-z]+"
)


def make_canary_secret(kind: str, key: str | None = None) -> str:
    """A new canary secret of the given kind. key defaults to CANARY_HMAC_KEY."""
    fmt = FORMATS.get(kind)
    if fmt is None:
        raise ValueError(f"unknown canary kind {kind!r}; known: {sorted(FORMATS)}")
    body = fmt.prefix + "".join(secrets.choice(BASE62) for _ in range(fmt.random_length))
    return body + _checksum(body, _key(key))


def is_canary(secret: str, key: str | None = None) -> bool:
    """True if secret has one of our shapes and its checksum matches under the key."""
    for fmt in FORMATS.values():
        if not secret.startswith(fmt.prefix) or len(secret) != fmt.length:
            continue
        tail = secret[len(fmt.prefix) :]
        if not all(char in BASE62 for char in tail):
            return False
        body, checksum = secret[:-CHECKSUM_LENGTH], secret[-CHECKSUM_LENGTH:]
        return hmac.compare_digest(checksum, _checksum(body, _key(key)))
    return False


def find_canary_secrets(value: Any, key: str | None = None) -> list[str]:  # noqa: ANN401
    """Every canary secret inside a string, or inside the strings of a JSON-like value.

    A canary usually arrives embedded in a longer string (a header in a shell command, a
    line of a config file), so each string is searched, not only compared whole.
    """
    found: list[str] = []
    for text in _strings(value):
        for match in _CANDIDATE.finditer(text):
            candidate = match.group(0)
            if candidate not in found and is_canary(candidate, key):
                found.append(candidate)
    return found


def _checksum(body: str, key: bytes) -> str:
    digest = hmac.new(key, body.encode("utf-8"), hashlib.sha256).digest()
    number = int.from_bytes(digest[:8], "big") % len(BASE62) ** CHECKSUM_LENGTH
    chars = []
    for _ in range(CHECKSUM_LENGTH):
        number, index = divmod(number, len(BASE62))
        chars.append(BASE62[index])
    return "".join(reversed(chars))


def _key(key: str | None) -> bytes:
    if key is None:
        key = Settings.from_env().canary_hmac_key
    if not key:
        raise RuntimeError("CANARY_HMAC_KEY is not set")
    return key.encode("utf-8")


def _strings(value: Any) -> Iterator[str]:  # noqa: ANN401
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, Iterable):
        for item in value:
            yield from _strings(item)
