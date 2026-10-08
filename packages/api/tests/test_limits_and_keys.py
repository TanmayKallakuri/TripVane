from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from tripvane_api.cli import main
from tripvane_api.client import parse_ip
from tripvane_api.keys import KEY_PREFIX, create_api_key, hash_key, key_id_for, revoke_api_key
from tripvane_api.limits import KEY_DAILY_LIMIT, DailyLimiter, ip_client, key_client
from tripvane_core.models import ApiKey

LOOKUP = "/v1/lookup/ip/203.0.113.7"
PROXY = "172.30.0.2"


def new_key(engine: Engine, label: str = "test") -> str:
    with engine.begin() as conn:
        return create_api_key(conn, label)[1]


def test_public_limit_is_50_per_ip_per_day(
    make_client: Callable[[str], TestClient], clock: list[datetime]
) -> None:
    client = make_client("198.51.100.77")
    for _ in range(50):
        assert client.get(LOOKUP).status_code == 200
    response = client.get(LOOKUP, headers={"Origin": "null"})
    assert response.status_code == 429
    # 10:00 UTC: the limit resets at midnight UTC, 14 hours later.
    assert response.headers["retry-after"] == str(14 * 3600)
    assert response.headers["access-control-allow-origin"] == "*"
    # Every lookup kind shares the one limit.
    assert client.get("/v1/lookup/domain/collect.exfil.example").status_code == 429
    # Another address has its own count.
    assert make_client("198.51.100.78").get(LOOKUP).status_code == 200
    # The next UTC day starts again.
    clock[0] = datetime(2026, 10, 9, 0, 0, 1, tzinfo=UTC)
    assert client.get(LOOKUP).status_code == 200


def test_public_limit_counts_invalid_requests(make_client: Callable[[str], TestClient]) -> None:
    client = make_client("198.51.100.79")
    for _ in range(50):
        assert client.get("/v1/lookup/ip/not-an-ip").status_code == 422
    assert client.get(LOOKUP).status_code == 429


def test_ipv6_clients_are_counted_per_slash_64(make_client: Callable[[str], TestClient]) -> None:
    for host in range(50):
        assert make_client(f"2001:db8:1:2::{host + 1:x}").get(LOOKUP).status_code == 200
    assert make_client("2001:db8:1:2:ffff::1").get(LOOKUP).status_code == 429
    assert make_client("2001:db8:1:3::1").get(LOOKUP).status_code == 200


def test_limit_uses_the_forwarded_address_only_behind_the_trusted_proxy(
    make_client: Callable[[str], TestClient],
) -> None:
    proxy = make_client(PROXY)
    for _ in range(50):
        proxy.get(LOOKUP, headers={"X-Forwarded-For": "203.0.113.200"})
    limited = proxy.get(LOOKUP, headers={"X-Forwarded-For": "203.0.113.200"})
    assert limited.status_code == 429
    assert proxy.get(LOOKUP, headers={"X-Forwarded-For": "203.0.113.201"}).status_code == 200
    # A direct client cannot pick its address with the header.
    direct = make_client("198.51.100.80")
    assert direct.get(LOOKUP, headers={"X-Forwarded-For": "203.0.113.202"}).status_code == 200
    for _ in range(49):
        direct.get(LOOKUP, headers={"X-Forwarded-For": f"203.0.113.{_}"})
    assert direct.get(LOOKUP).status_code == 429


def test_api_key_limit_is_5000_per_day(
    api: FastAPI,
    make_client: Callable[[str], TestClient],
    grid_engine: Engine,
    clock: list[datetime],
) -> None:
    key = new_key(grid_engine)
    with grid_engine.connect() as conn:
        key_id = key_id_for(conn, key)
    assert key_id is not None
    client = make_client("198.51.100.81")
    headers = {"X-Api-Key": key}
    # 60 requests: past the public limit of 50 without touching it.
    for _ in range(60):
        assert client.get(LOOKUP, headers=headers).status_code == 200
    # Count the next 4,930 directly rather than make them over HTTP.
    limiter: DailyLimiter = api.state.limiter
    for _ in range(4930):
        assert limiter.allow(key_client(key_id), KEY_DAILY_LIMIT)
    for _ in range(10):
        assert client.get(LOOKUP, headers=headers).status_code == 200
    # Request 5,001.
    assert client.get(LOOKUP, headers=headers).status_code == 429
    # The key's requests did not use the address's public allowance.
    assert client.get(LOOKUP).status_code == 200
    # Nor does another address get round the key's limit.
    assert make_client("198.51.100.82").get(LOOKUP, headers=headers).status_code == 429
    clock[0] += timedelta(days=1)
    assert client.get(LOOKUP, headers=headers).status_code == 200


@pytest.mark.parametrize("path", [LOOKUP, "/v1/feed/recent"])
def test_unknown_or_revoked_key_is_refused(
    client: TestClient, grid_engine: Engine, path: str
) -> None:
    assert client.get(path, headers={"X-Api-Key": "tripvane_not-a-key"}).status_code == 401
    key = new_key(grid_engine)
    with grid_engine.begin() as conn:
        key_id = key_id_for(conn, key)
        assert key_id is not None
        assert revoke_api_key(conn, key_id, datetime(2026, 10, 8, tzinfo=UTC))
    assert client.get(path, headers={"X-Api-Key": key}).status_code == 401


def test_feed_requires_a_key(client: TestClient) -> None:
    response = client.get("/v1/feed/recent")
    assert response.status_code == 401
    assert response.json() == {"detail": "API key required"}


def test_feed_lists_attack_payloads_from_the_last_24_hours(
    client: TestClient,
    grid_engine: Engine,
    payload_ids: dict[str, int],
    payload_hashes: dict[str, str],
) -> None:
    response = client.get("/v1/feed/recent", headers={"X-Api-Key": new_key(grid_engine)})
    assert response.status_code == 200
    payloads = response.json()["payloads"]
    # C is benign and D was last seen three days ago; the rest were seen an hour ago,
    # so they are ordered by hash.
    assert [p["payload_hash"] for p in payloads] == sorted(
        payload_hashes[letter] for letter in "ABEFG"
    )
    by_hash = {p["payload_hash"]: p for p in payloads}
    assert by_hash[payload_hashes["A"]] == {
        "payload_hash": payload_hashes["A"],
        "tags": {
            "technique": "direct_instruction",
            "objective": "exfiltrate_secrets",
            "target_tool": "http",
        },
        "campaign_id": payload_ids["A"],
    }
    assert list(by_hash[payload_hashes["A"]]["tags"]) == ["technique", "objective", "target_tool"]
    assert by_hash[payload_hashes["F"]]["campaign_id"] == payload_ids["B"]
    # E is an attack that has not been tagged yet.
    assert by_hash[payload_hashes["E"]]["tags"] == {}


def test_feed_window_moves_with_the_clock(
    client: TestClient, grid_engine: Engine, clock: list[datetime]
) -> None:
    clock[0] += timedelta(hours=24)
    response = client.get("/v1/feed/recent", headers={"X-Api-Key": new_key(grid_engine)})
    assert response.json() == {"payloads": []}


def test_keys_are_stored_hashed(grid_engine: Engine) -> None:
    with grid_engine.begin() as conn:
        key_id, key = create_api_key(conn, "partner")
        row = conn.execute(select(ApiKey).where(ApiKey.id == key_id)).one()
    assert key.startswith(KEY_PREFIX) and len(key) > 40
    assert row.key_hash == hash_key(key) and key not in row.key_hash
    assert (row.label, row.revoked_at) == ("partner", None)
    with grid_engine.begin() as conn:
        assert revoke_api_key(conn, key_id, datetime(2026, 10, 8, tzinfo=UTC))
        assert not revoke_api_key(conn, key_id, datetime(2026, 10, 9, tzinfo=UTC))
        assert key_id_for(conn, key) is None


def test_cli_creates_and_revokes_keys(
    grid_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'grid.db'}")
    assert main(["keys", "create", "plugin users"]) == 0
    out = capsys.readouterr().out.strip()
    key_id = int(out.split()[0].removeprefix("id="))
    key = out.split()[1].removeprefix("key=")
    with grid_engine.connect() as conn:
        assert key_id_for(conn, key) == key_id
    assert main(["keys", "revoke", str(key_id)]) == 0
    assert main(["keys", "revoke", str(key_id)]) == 1
    with grid_engine.connect() as conn:
        assert key_id_for(conn, key) is None


def test_cli_needs_a_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["keys", "create", "x"]) == 2


def test_limiter_counts_per_client_and_day() -> None:
    now = [datetime(2026, 10, 8, 23, 59, 30, tzinfo=UTC)]
    limiter = DailyLimiter(lambda: now[0])
    assert [limiter.allow("a", 2) for _ in range(3)] == [True, True, False]
    assert limiter.allow("b", 2)
    assert limiter.seconds_until_reset() == 30
    now[0] += timedelta(seconds=31)
    assert limiter.allow("a", 2)


def test_ipv6_limiter_key_is_the_slash_64() -> None:
    v6 = parse_ip("2001:db8:a:b:1:2:3:4")
    v4 = parse_ip("203.0.113.9")
    assert v6 is not None and v4 is not None
    assert ip_client(v6) == "ip:2001:db8:a:b::/64"
    assert ip_client(v4) == "ip:203.0.113.9"
