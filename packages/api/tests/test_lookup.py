from typing import Any

import pytest
from fastapi.testclient import TestClient

UNSEEN = {
    "seen": False,
    "first_seen": None,
    "last_seen": None,
    "sensor_count": None,
    "session_count": None,
    "tags": None,
    "campaign_id": None,
}
ONE_HOUR_AGO = "2026-10-08T09:00:00Z"


def tags(**axes: list[tuple[str, int]]) -> dict[str, list[dict[str, Any]]]:
    return {axis: [{"tag": t, "count": c} for t, c in top] for axis, top in axes.items()}


@pytest.mark.parametrize("ip", ["203.0.113.7", "::ffff:203.0.113.7"])
def test_known_ip(client: TestClient, payload_ids: dict[str, int], ip: str) -> None:
    response = client.get(f"/v1/lookup/ip/{ip}")
    assert response.status_code == 200
    # s1 and s2; payload C in s1 is benign, so only A's tags count.
    assert response.json() == {
        "seen": True,
        "first_seen": ONE_HOUR_AGO,
        "last_seen": ONE_HOUR_AGO,
        "sensor_count": 2,
        "session_count": 2,
        "tags": tags(
            technique=[("direct_instruction", 2)],
            objective=[("exfiltrate_secrets", 2)],
            target_tool=[("http", 2)],
        ),
        "campaign_id": payload_ids["A"],
    }


def test_ip_tags_are_the_top_three_per_axis_and_the_campaign_is_the_most_common(
    client: TestClient, payload_ids: dict[str, int]
) -> None:
    body = client.get("/v1/lookup/ip/198.51.100.4").json()
    assert (body["session_count"], body["sensor_count"]) == (3, 3)
    # Axes come in taxonomy order (the order their tags were added to the tags table).
    assert list(body["tags"]) == ["technique", "objective", "target_tool"]
    # Four techniques were seen; role_override (1 session) loses the alphabetical tie-break.
    assert body["tags"] == tags(
        technique=[("encoded", 2), ("direct_instruction", 1), ("hidden_in_document", 1)],
        objective=[("exfiltrate_secrets", 2), ("destructive_action", 1)],
        target_tool=[("secrets", 2), ("email", 1), ("http", 1)],
    )
    # Campaign B covers s3 (B) and s5, s6 (F); A, E and G one session each.
    assert body["campaign_id"] == payload_ids["B"]


def test_ipv6_lookup_uses_the_canonical_form(client: TestClient) -> None:
    body = client.get("/v1/lookup/ip/2001:DB8:0::5").json()
    assert body["seen"] is True
    assert body["first_seen"] == "2026-10-05T10:00:00Z"
    assert body["tags"] == tags(
        technique=[("direct_instruction", 1)], objective=[("recon", 1)], target_tool=[("http", 1)]
    )


def test_unknown_ip(client: TestClient) -> None:
    response = client.get("/v1/lookup/ip/192.0.2.99")
    assert response.status_code == 200
    assert response.json() == UNSEEN


def test_known_domain(client: TestClient, payload_ids: dict[str, int]) -> None:
    body = client.get("/v1/lookup/domain/Collect.Exfil.Example.").json()
    assert body == {
        "seen": True,
        "first_seen": ONE_HOUR_AGO,
        "last_seen": ONE_HOUR_AGO,
        "sensor_count": 1,
        "session_count": 2,
        "tags": tags(
            technique=[("direct_instruction", 1), ("role_override", 1)],
            objective=[("destructive_action", 1), ("exfiltrate_secrets", 1)],
            target_tool=[("http", 1), ("shell", 1)],
        ),
        # A and B one session each: the tie goes to the older campaign.
        "campaign_id": payload_ids["A"],
    }


@pytest.mark.parametrize(
    "domain", ["unknown.example", "exfil.example", "sub.collect.exfil.example"]
)
def test_unknown_domain(client: TestClient, domain: str) -> None:
    response = client.get(f"/v1/lookup/domain/{domain}")
    assert response.status_code == 200
    assert response.json() == UNSEEN


def test_known_payload(
    client: TestClient, payload_ids: dict[str, int], payload_hashes: dict[str, str]
) -> None:
    body = client.get(f"/v1/lookup/payload/{payload_hashes['A']}").json()
    # Only A's own tags, although s5 also received F and G; the older taxonomy's
    # role_override tag on A is history and is not shown.
    assert body == {
        "seen": True,
        "first_seen": ONE_HOUR_AGO,
        "last_seen": ONE_HOUR_AGO,
        "sensor_count": 2,
        "session_count": 3,
        "tags": tags(
            technique=[("direct_instruction", 3)],
            objective=[("exfiltrate_secrets", 3)],
            target_tool=[("http", 3)],
        ),
        "campaign_id": payload_ids["A"],
    }


def test_payload_hash_is_case_insensitive(
    client: TestClient, payload_ids: dict[str, int], payload_hashes: dict[str, str]
) -> None:
    body = client.get(f"/v1/lookup/payload/{payload_hashes['B'].upper()}").json()
    assert body["seen"] is True
    assert body["campaign_id"] == payload_ids["B"]


def test_benign_payload_is_seen_without_tags_or_campaign(
    client: TestClient, payload_hashes: dict[str, str]
) -> None:
    body = client.get(f"/v1/lookup/payload/{payload_hashes['C']}").json()
    assert body["seen"] is True
    assert (body["session_count"], body["tags"], body["campaign_id"]) == (1, {}, None)


def test_unknown_payload(client: TestClient) -> None:
    response = client.get("/v1/lookup/payload/" + "0" * 64)
    assert response.status_code == 200
    assert response.json() == UNSEEN


@pytest.mark.parametrize(
    "path",
    [
        "/v1/lookup/ip/203.0.113",
        "/v1/lookup/ip/not-an-ip",
        "/v1/lookup/domain/localhost",
        "/v1/lookup/domain/203.0.113.7",
        "/v1/lookup/payload/abc123",
        "/v1/lookup/payload/" + "g" * 64,
    ],
)
def test_invalid_values_are_rejected(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 422


def test_lookups_allow_any_origin(client: TestClient) -> None:
    # web/lookup.html opened from disk sends Origin: null.
    response = client.get("/v1/lookup/ip/203.0.113.7", headers={"Origin": "null"})
    assert response.headers["access-control-allow-origin"] == "*"


def test_openapi_docs_are_served(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert set(schema["paths"]) == {
        "/v1/lookup/ip/{ip}",
        "/v1/lookup/domain/{domain}",
        "/v1/lookup/payload/{sha256}",
        "/v1/feed/recent",
    }
    assert schema["components"]["securitySchemes"]["APIKeyHeader"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-Api-Key",
    }
