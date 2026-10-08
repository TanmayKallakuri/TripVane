import hashlib
from collections.abc import Callable
from typing import Any

from anthropic.types import Message
from sqlalchemy import Engine, select

from tripvane_analyst import novel, tagger
from tripvane_core.models import TagProposal
from tripvane_core.taxonomy import DEFAULT_PATH, Taxonomy


def _tag(engine: Engine, payload_id: int, message: Message, taxonomy: Taxonomy) -> None:
    with engine.begin() as conn:
        tag_ids = tagger.sync_tags(conn, taxonomy)
        tagger.store(conn, payload_id, tagger.parse(message, taxonomy), tag_ids, taxonomy.version)


def test_novel_writes_a_proposal_and_never_touches_tags_yaml(
    engine: Engine,
    add_payload: Callable[..., int],
    payload_texts: dict[str, str],
    recorded: Callable[[str], Message],
    fake_client: Callable[..., Any],
    taxonomy: Taxonomy,
) -> None:
    before = hashlib.sha256(DEFAULT_PATH.read_bytes()).hexdigest()
    mtime = DEFAULT_PATH.stat().st_mtime_ns
    other = add_payload(
        payload_texts["encoded_injection"], is_attack=True, gate_version=taxonomy.version
    )
    direct = add_payload(
        payload_texts["direct_injection"], is_attack=True, gate_version=taxonomy.version
    )
    _tag(engine, other, recorded("tagger_other"), taxonomy)
    _tag(engine, direct, recorded("tagger_direct"), taxonomy)
    client = fake_client(recorded("novel_proposal"))

    assert novel.run(engine, client, taxonomy) == {"proposed": 1}

    with engine.connect() as conn:
        rows = conn.execute(
            select(
                TagProposal.payload_id,
                TagProposal.taxonomy_version,
                TagProposal.name,
                TagProposal.description,
            )
        ).all()
    assert [tuple(row) for row in rows] == [
        (
            other,
            taxonomy.version,
            "fictional_dialogue_framing",
            "The payload wraps its instruction in a fictional dialogue or story so the agent "
            "treats acting on it as role play.",
        )
    ]
    [request] = client.messages.requests
    assert request["model"] == "claude-opus-5-5"
    assert request["messages"] == [{"role": "user", "content": payload_texts["encoded_injection"]}]
    for name in taxonomy.tags("technique"):
        assert f"- {name}: " in request["system"]
    # Only payloads tagged technique/other, and only once per taxonomy version.
    assert novel.run(engine, fake_client(), taxonomy) == {}
    assert hashlib.sha256(DEFAULT_PATH.read_bytes()).hexdigest() == before
    assert DEFAULT_PATH.stat().st_mtime_ns == mtime
