from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from anthropic.types import Message
from sqlalchemy import Engine, select

from tripvane_analyst import batch, gate, tagger
from tripvane_core.models import AnalystBatch, Payload, PayloadTag, Tag
from tripvane_core.taxonomy import Taxonomy

OLD = "0" * 64
NOW = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)


def _stale_payloads(
    add_payload: Callable[..., int], payload_texts: dict[str, str], taxonomy: Taxonomy
) -> tuple[int, int, int]:
    """Payloads 1 and 2 were attacks and 3 was not, all under an older taxonomy."""
    one = add_payload(payload_texts["direct_injection"], is_attack=True, gate_version=OLD)
    two = add_payload(payload_texts["encoded_injection"], is_attack=True, gate_version=OLD)
    three = add_payload(payload_texts["benign_question"], is_attack=False, gate_version=OLD)
    # Gated under the current taxonomy, or never gated: not stale.
    add_payload(payload_texts["campaign_a1"], is_attack=True, gate_version=taxonomy.version)
    add_payload(payload_texts["campaign_a2"])
    return one, two, three


def test_batch_submit_builds_request_bodies_without_sending(
    engine: Engine,
    add_payload: Callable[..., int],
    payload_texts: dict[str, str],
    taxonomy: Taxonomy,
) -> None:
    one, two, three = _stale_payloads(add_payload, payload_texts, taxonomy)

    with engine.connect() as conn:
        requests = batch.build_requests(conn, taxonomy)

    texts = {
        one: payload_texts["direct_injection"],
        two: payload_texts["encoded_injection"],
        three: payload_texts["benign_question"],
    }
    assert requests == [
        {"custom_id": f"gate-{one}", "params": gate.request(texts[one])},
        {"custom_id": f"gate-{two}", "params": gate.request(texts[two])},
        {"custom_id": f"gate-{three}", "params": gate.request(texts[three])},
        {"custom_id": f"tag-{one}", "params": tagger.request(texts[one], taxonomy)},
        {"custom_id": f"tag-{two}", "params": tagger.request(texts[two], taxonomy)},
    ]
    gate_params = requests[0]["params"]
    assert gate_params["model"] == "claude-haiku-5-5"
    assert gate_params["messages"] == [{"role": "user", "content": texts[one]}]
    tag_params = requests[3]["params"]
    assert tag_params["model"] == "claude-sonnet-5-5"
    assert tag_params["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_batch_submit_records_the_batch_and_waits_for_collect(
    engine: Engine,
    add_payload: Callable[..., int],
    payload_texts: dict[str, str],
    fake_client: Callable[..., Any],
    taxonomy: Taxonomy,
) -> None:
    _stale_payloads(add_payload, payload_texts, taxonomy)
    client = fake_client()

    counts = batch.submit(engine, client, taxonomy, now=lambda: NOW)

    assert counts == {"submitted_batches": 1, "requests": 5}
    assert len(client.messages.batches.created) == 1
    assert client.messages.requests == []
    with engine.connect() as conn:
        rows = conn.execute(
            select(AnalystBatch.id, AnalystBatch.taxonomy_version, AnalystBatch.request_count)
        ).all()
    assert [tuple(row) for row in rows] == [("msgbatch_rec_01", taxonomy.version, 5)]
    # Nothing more is submitted while that batch is uncollected.
    assert batch.submit(engine, client, taxonomy) == {"waiting_for_collect": 1}
    assert len(client.messages.batches.created) == 1


def test_batch_submit_with_nothing_stale_sends_nothing(
    engine: Engine, fake_client: Callable[..., Any], taxonomy: Taxonomy
) -> None:
    client = fake_client()
    assert batch.submit(engine, client, taxonomy) == {}
    assert client.messages.batches.created == []


def test_batch_collect_writes_results_once_the_batch_has_ended(
    engine: Engine,
    add_payload: Callable[..., int],
    payload_texts: dict[str, str],
    fake_client: Callable[..., Any],
    taxonomy: Taxonomy,
) -> None:
    one, two, three = _stale_payloads(add_payload, payload_texts, taxonomy)
    client = fake_client()
    batch.submit(engine, client, taxonomy, now=lambda: NOW)

    assert batch.collect(engine, client, taxonomy) == {"in_progress": 1}

    client.messages.batches.status = "ended"
    counts = batch.collect(engine, client, taxonomy, now=lambda: NOW)

    # gate-3 errored; tag-2 is dropped because payload 2 is no longer an attack.
    assert counts == {
        "gated": 2,
        "gate_failed": 1,
        "tagged": 1,
        "tags_skipped": 1,
        "collected_batches": 1,
    }
    with engine.connect() as conn:
        verdicts = {
            payload_id: is_attack
            for payload_id, is_attack in conn.execute(
                select(Payload.id, Payload.is_attack).where(
                    Payload.gate_version == taxonomy.version
                )
            )
        }
        stale = conn.scalars(select(Payload.id).where(Payload.gate_version == OLD)).all()
        tags = conn.execute(
            select(PayloadTag.payload_id, Tag.axis, Tag.name)
            .join(Tag, Tag.id == PayloadTag.tag_id)
            .where(PayloadTag.taxonomy_version == taxonomy.version)
            .order_by(Tag.axis)
        ).all()
        collected = conn.scalar(select(AnalystBatch.collected_at))
    assert verdicts[one] is True and verdicts[two] is False
    assert stale == [three]
    assert [tuple(row) for row in tags] == [
        (one, "objective", "exfiltrate_secrets"),
        (one, "target_tool", "email"),
        (one, "technique", "direct_instruction"),
    ]
    assert collected is not None
    assert batch.collect(engine, client, taxonomy) == {}


def test_a_batch_from_an_older_taxonomy_is_discarded(
    engine: Engine,
    add_payload: Callable[..., int],
    payload_texts: dict[str, str],
    fake_client: Callable[..., Any],
    taxonomy: Taxonomy,
) -> None:
    _stale_payloads(add_payload, payload_texts, taxonomy)
    client = fake_client()
    batch.submit(engine, client, taxonomy, now=lambda: NOW)
    client.messages.batches.status = "ended"
    newer = Taxonomy(version="f" * 64, axes=taxonomy.axes)

    counts = batch.collect(engine, client, newer, now=lambda: NOW)

    assert counts["discarded_batches"] == 1
    with engine.connect() as conn:
        assert conn.scalars(select(Payload.gate_version).distinct()).all().count(OLD) == 1
        assert conn.scalars(select(PayloadTag.payload_id)).all() == []


def test_failed_gate_results_are_resubmitted(
    engine: Engine,
    add_payload: Callable[..., int],
    payload_texts: dict[str, str],
    fake_client: Callable[..., Any],
    taxonomy: Taxonomy,
    recorded: Callable[[str], Message],
) -> None:
    _, _, three = _stale_payloads(add_payload, payload_texts, taxonomy)
    client = fake_client()
    batch.submit(engine, client, taxonomy, now=lambda: NOW)
    client.messages.batches.status = "ended"
    batch.collect(engine, client, taxonomy, now=lambda: NOW)

    with engine.connect() as conn:
        requests = batch.build_requests(conn, taxonomy)
    assert [request["custom_id"] for request in requests] == [f"gate-{three}"]
