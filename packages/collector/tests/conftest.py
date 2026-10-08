from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, insert

from tripvane_collector.app import create_app
from tripvane_collector.auth import hash_token
from tripvane_core.config import Settings
from tripvane_core.db import make_engine
from tripvane_core.models import Sensor

ALEMBIC_INI = Path(__file__).parents[1] / "alembic.ini"

# Test-only bearer tokens; not credentials for anything.
SENSOR_ID = "support-1"
SENSOR_TOKEN = "test-token-support-1"
OTHER_SENSOR_ID = "mcp-1"
OTHER_SENSOR_TOKEN = "test-token-mcp-1"
# Test-only HMAC key for canary checksums; it protects nothing.
CANARY_HMAC_KEY = "test-canary-hmac-key"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """A temporary SQLite database built by running the Alembic migrations."""
    engine = make_engine(f"sqlite:///{tmp_path / 'collector.db'}")
    config = Config(str(ALEMBIC_INI))
    config.attributes["configure_logger"] = False
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "head")
        conn.execute(
            insert(Sensor),
            [
                {
                    "id": SENSOR_ID,
                    "name": "support sensor",
                    "archetype": "support",
                    "token_hash": hash_token(SENSOR_TOKEN),
                },
                {
                    "id": OTHER_SENSOR_ID,
                    "name": "mcp sensor",
                    "archetype": "mcp",
                    "token_hash": hash_token(OTHER_SENSOR_TOKEN),
                },
            ],
        )
    yield engine
    engine.dispose()


@pytest.fixture
def client(engine: Engine) -> TestClient:
    return TestClient(create_app(engine, Settings(canary_hmac_key=CANARY_HMAC_KEY)))


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {SENSOR_TOKEN}"}


@pytest.fixture
def other_auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {OTHER_SENSOR_TOKEN}"}
