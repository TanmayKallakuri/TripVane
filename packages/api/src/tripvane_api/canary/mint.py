"""Minting canaries: a canaries row whose token is a canary secret, and its URL."""

from dataclasses import dataclass

from sqlalchemy import Connection, insert

from tripvane_core.canary_formats import make_canary_secret
from tripvane_core.config import Settings
from tripvane_core.models import Canary


@dataclass(frozen=True)
class MintedCanary:
    token: str
    url: str


def mint_canary(
    conn: Connection,
    owner_kind: str,
    owner_id: str,
    document_id: str | None,
    *,
    kind: str = "api_key",
    base_url: str | None = None,
) -> MintedCanary:
    """Create a canaries row and return its token and canary URL.

    The token is a canary secret of the given kind (canary_formats.py), so the same
    token works as a URL planted in a document and as a fake credential planted in a
    decoy: the URL endpoint finds it by token, and the collector recognises it when it
    shows up in a tool call. base_url defaults to CANARY_BASE_URL.
    """
    if base_url is None:
        base_url = Settings.from_env().canary_base_url
    if not base_url:
        raise RuntimeError("CANARY_BASE_URL is not set")
    token = make_canary_secret(kind)
    conn.execute(
        insert(Canary).values(
            token=token, owner_kind=owner_kind, owner_id=owner_id, document_id=document_id
        )
    )
    return MintedCanary(token=token, url=f"{base_url.rstrip('/')}/c/{token}")
