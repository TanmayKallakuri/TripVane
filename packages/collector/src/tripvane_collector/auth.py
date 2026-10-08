import hashlib

from sqlalchemy import Connection, select

from tripvane_core.models import Sensor


def hash_token(token: str) -> str:
    """Sensor tokens are random and high-entropy, so an unsalted sha256 is enough."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def sensor_for_token(conn: Connection, token: str) -> str | None:
    """Return the id of the sensor that owns this bearer token, or None."""
    return conn.scalar(select(Sensor.id).where(Sensor.token_hash == hash_token(token)))
