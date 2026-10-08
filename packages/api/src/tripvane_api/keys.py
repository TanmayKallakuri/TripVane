"""Lookup API keys: rows in api_keys holding the sha256 of a random key.

A key is shown once, when it is created; only its hash is stored. Keys are random and
high-entropy, so an unsalted sha256 is enough, as for sensor tokens.
"""

import hashlib
import secrets
from datetime import datetime

from sqlalchemy import Connection, insert, select, update

from tripvane_core.models import ApiKey

KEY_PREFIX = "tripvane_"


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def create_api_key(conn: Connection, label: str) -> tuple[int, str]:
    """Insert a key and return its id and the key itself."""
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    key_id = conn.execute(
        insert(ApiKey).values(key_hash=hash_key(key), label=label).returning(ApiKey.id)
    ).scalar_one()
    return key_id, key


def revoke_api_key(conn: Connection, key_id: int, now: datetime) -> bool:
    """Revoke a key; False if no unrevoked key has that id."""
    row = conn.execute(
        update(ApiKey)
        .where(ApiKey.id == key_id, ApiKey.revoked_at.is_(None))
        .values(revoked_at=now)
        .returning(ApiKey.id)
    ).first()
    return row is not None


def key_id_for(conn: Connection, key: str) -> int | None:
    """The id of the unrevoked key, or None."""
    return conn.scalar(
        select(ApiKey.id).where(ApiKey.key_hash == hash_key(key), ApiKey.revoked_at.is_(None))
    )
