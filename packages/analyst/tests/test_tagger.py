from collections.abc import Callable
from typing import Any

from anthropic.types import Message
from sqlalchemy import Engine, select

from tripvane_analyst import tagger
from tripvane_core.models import PayloadTag, Tag
from tripvane_core.taxonomy import Taxonomy


def _tags(engine: Engine) -> list[tuple[int, str, str, str, float]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                PayloadTag.payload_id,
                Tag.axis,
                Tag.name,
                PayloadTag.taxonomy_version,
                PayloadTag.confidence,
            )
            .join(Tag, Tag.id == PayloadTag.tag_id)
            .order_by(PayloadTag.payload_id, Tag.axis)
        )
        return [tuple(row) for row in rows]  # type: ignore[misc]


def test_tagger_writes_one_tag_per_axis(
    engine: Engine,
    add_payload: Callable[..., int],
    payload_texts: dict[str, str],
    recorded: Callable[[str], Message],
    fake_client: Callable[..., Any],
    taxonomy: Taxonomy,
) -> None:
    attack = add_payload(
        payload_texts["direct_injection"], is_attack=True, gate_version=taxonomy.version
    )
    # Not attacks, or gated under another taxonomy: not the tagger's to handle.
    add_payload(payload_texts["benign_question"], is_attack=False, gate_version=taxonomy.version)
    add_payload(payload_texts["encoded_injection"], is_attack=True, gate_version="0" * 64)
    client = fake_client(recorded("tagger_direct"))

    assert tagger.run(engine, client, taxonomy) == {"tagged": 1}

    v = taxonomy.version
    assert _tags(engine) == [
        (attack, "objective", "exfiltrate_secrets", v, 0.9),
        (attack, "target_tool", "email", v, 0.82),
        (attack, "technique", "direct_instruction", v, 0.93),
    ]
    assert len(client.messages.requests) == 1
    assert tagger.run(engine, fake_client(), taxonomy) == {}


def test_tagger_request_is_constrained_to_the_taxonomy_and_cached(taxonomy: Taxonomy) -> None:
    body = tagger.request("Synthetic honeypot test data.", taxonomy)
    assert body["model"] == "claude-sonnet-5-5"
    [system] = body["system"]
    assert system["cache_control"] == {"type": "ephemeral"}
    # The taxonomy is rendered into the cached prompt with every tag and its description.
    for tags in taxonomy.axes.values():
        for name, description in tags.items():
            assert f"- {name}: {description}" in system["text"]
    assert "{taxonomy}" not in system["text"]
    schema = body["output_config"]["format"]["schema"]
    assert schema["required"] == ["technique", "objective", "target_tool"]
    for axis in taxonomy.axes:
        assert schema["properties"][axis]["properties"]["tag"]["enum"] == taxonomy.tags(axis)
    assert body["messages"] == [{"role": "user", "content": "Synthetic honeypot test data."}]


def test_a_tag_outside_the_taxonomy_writes_nothing(
    engine: Engine,
    add_payload: Callable[..., int],
    payload_texts: dict[str, str],
    recorded: Callable[[str], Message],
    fake_client: Callable[..., Any],
    taxonomy: Taxonomy,
) -> None:
    add_payload(payload_texts["direct_injection"], is_attack=True, gate_version=taxonomy.version)
    counts = tagger.run(engine, fake_client(recorded("tagger_unknown_tag")), taxonomy)
    assert counts == {"unusable": 1}
    assert _tags(engine) == []


def test_sync_tags_adds_the_taxonomy_once(engine: Engine, taxonomy: Taxonomy) -> None:
    with engine.begin() as conn:
        first = tagger.sync_tags(conn, taxonomy)
        second = tagger.sync_tags(conn, taxonomy)
    assert first == second
    assert len(first) == sum(len(tags) for tags in taxonomy.axes.values())
