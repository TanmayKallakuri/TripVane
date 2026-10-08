from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from tripvane_api.app import create_app
from tripvane_api.canary.mint import mint_canary
from tripvane_core.canary_formats import is_canary
from tripvane_core.config import Settings
from tripvane_core.db import make_engine
from tripvane_core.models import Base, Canary, CanaryHit

NOW = datetime(2026, 10, 8, 9, 30, tzinfo=UTC)
BASE_URL = "https://cdn.quillstone.example"
PROXY = "172.30.0.2"


@pytest.fixture(autouse=True)
def canary_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Test-only HMAC key; it protects nothing.
    monkeypatch.setenv("CANARY_HMAC_KEY", "test-canary-hmac-key")


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = make_engine(f"sqlite:///{tmp_path / 'api.db'}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def client(engine: Engine) -> TestClient:
    app = create_app(engine, Settings(trusted_proxy=PROXY), now=lambda: NOW)
    return TestClient(app, client=("198.51.100.23", 50000))


def mint(engine: Engine, document_id: str | None = "doc-7") -> str:
    with engine.begin() as conn:
        return mint_canary(conn, "sensor", "support-1", document_id, base_url=BASE_URL).token


def hits(engine: Engine) -> list[CanaryHit]:
    with engine.connect() as conn:
        return list(conn.execute(select(CanaryHit).order_by(CanaryHit.id)).all())


def test_mint_creates_a_row_and_returns_the_url(engine: Engine) -> None:
    with engine.begin() as conn:
        minted = mint_canary(conn, "document", "brief-1", "page-3", base_url=BASE_URL + "/")
    assert minted.url == f"{BASE_URL}/c/{minted.token}"
    assert is_canary(minted.token)
    assert minted.token.startswith("ckl_live_")
    with engine.connect() as conn:
        row = conn.execute(select(Canary)).one()
    assert (row.token, row.owner_kind, row.owner_id, row.document_id) == (
        minted.token,
        "document",
        "brief-1",
        "page-3",
    )


def test_mint_kind_and_base_url_from_the_environment(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CANARY_BASE_URL", BASE_URL)
    with engine.begin() as conn:
        minted = mint_canary(conn, "sensor", "support-1", None, kind="db_password")
    assert minted.token.startswith("xpg_")
    assert minted.url.startswith(f"{BASE_URL}/c/xpg_")


def test_mint_without_a_base_url_fails_loudly(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CANARY_BASE_URL", raising=False)
    with engine.begin() as conn, pytest.raises(RuntimeError, match="CANARY_BASE_URL"):
        mint_canary(conn, "sensor", "support-1", None)


def test_hit_is_recorded_with_the_source(engine: Engine, client: TestClient) -> None:
    token = mint(engine)
    response = client.get(
        f"/c/{token}",
        headers={
            "User-Agent": "python-requests/2.32",
            "Accept-Language": "en-US",
            "Referer": "https://docs.example/shared/7",
            "Cookie": "session=not-stored",
        },
    )
    assert response.status_code == 404
    assert response.content == b""
    [hit] = hits(engine)
    with engine.connect() as conn:
        canary_id = conn.scalar(select(Canary.id).where(Canary.token == token))
    assert hit.canary_id == canary_id
    assert hit.ts.replace(tzinfo=UTC) == NOW
    assert hit.secret is None
    assert hit.source == {
        "ip": "198.51.100.23",
        "asn": None,
        "user_agent": "python-requests/2.32",
        "headers_subset": {
            "Accept-Language": "en-US",
            "Referer": "https://docs.example/shared/7",
        },
        "account_handle": None,
    }


def test_every_request_is_a_separate_hit(engine: Engine, client: TestClient) -> None:
    token = mint(engine)
    for _ in range(3):
        assert client.get(f"/c/{token}").status_code == 404
    assert len(hits(engine)) == 3


@pytest.mark.parametrize("token", ["unknown", "ckl_live_" + "a" * 32, "x" * 300])
def test_unknown_token_returns_the_same_404_and_records_nothing(
    engine: Engine, client: TestClient, token: str
) -> None:
    mint(engine)
    response = client.get(f"/c/{token}")
    assert response.status_code == 404
    assert response.content == b""
    assert hits(engine) == []


def test_forwarded_address_is_used_only_behind_the_trusted_proxy(engine: Engine) -> None:
    token = mint(engine)
    app = create_app(engine, Settings(trusted_proxy=PROXY), now=lambda: NOW)
    behind_proxy = TestClient(app, client=(PROXY, 40000))
    behind_proxy.get(f"/c/{token}", headers={"X-Forwarded-For": "192.0.2.1, 203.0.113.9"})
    direct = TestClient(app, client=("198.51.100.23", 40000))
    direct.get(f"/c/{token}", headers={"X-Forwarded-For": "203.0.113.9"})
    assert [hit.source["ip"] for hit in hits(engine)] == ["203.0.113.9", "198.51.100.23"]


def test_client_ip_header_gives_the_address_behind_a_platform_proxy(engine: Engine) -> None:
    token = mint(engine)
    app = create_app(engine, Settings(client_ip_header="X-Real-IP"), now=lambda: NOW)
    platform = TestClient(app, client=("10.1.2.3", 40000))
    platform.get(
        f"/c/{token}", headers={"X-Real-IP": "203.0.113.9", "X-Forwarded-For": "192.0.2.1"}
    )
    # Without the header the peer address is all there is.
    platform.get(f"/c/{token}")
    assert [hit.source["ip"] for hit in hits(engine)] == ["203.0.113.9", "10.1.2.3"]


def test_trusted_proxy_and_client_ip_header_together_are_refused(engine: Engine) -> None:
    with pytest.raises(ValueError, match="not both"):
        create_app(engine, Settings(trusted_proxy=PROXY, client_ip_header="X-Real-IP"))


def test_canary_endpoint_is_not_in_the_schema(client: TestClient) -> None:
    assert "/c/{token}" not in client.get("/openapi.json").json()["paths"]


def test_missing_database_url_fails_loudly() -> None:
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        create_app(settings=Settings())
