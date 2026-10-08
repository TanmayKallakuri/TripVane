from collections.abc import Callable
from typing import Any

import anthropic
import httpx2
from anthropic.types import Message
from sqlalchemy import Engine, select

from tripvane_analyst import gate
from tripvane_core.models import Payload
from tripvane_core.taxonomy import Taxonomy


def _verdicts(engine: Engine) -> dict[int, tuple[bool | None, str | None]]:
    with engine.connect() as conn:
        rows = conn.execute(select(Payload.id, Payload.is_attack, Payload.gate_version))
        return {payload_id: (is_attack, version) for payload_id, is_attack, version in rows}


def test_gate_stores_verdict_and_taxonomy_version(
    engine: Engine,
    add_payload: Callable[..., int],
    payload_texts: dict[str, str],
    recorded: Callable[[str], Message],
    fake_client: Callable[..., Any],
    taxonomy: Taxonomy,
) -> None:
    attack = add_payload(payload_texts["direct_injection"])
    benign = add_payload(payload_texts["benign_question"])
    client = fake_client(recorded("gate_yes"), recorded("gate_no"))

    counts = gate.run(engine, client, taxonomy)

    assert counts == {"gated": 2, "attacks": 1, "not_attacks": 1}
    assert _verdicts(engine) == {
        attack: (True, taxonomy.version),
        benign: (False, taxonomy.version),
    }
    first = client.messages.requests[0]
    assert first["model"] == "claude-haiku-5-5"
    assert first["system"] == gate.PROMPT
    # The model sees the raw text the decoy received, not the lowercased normalized text.
    assert first["messages"] == [{"role": "user", "content": payload_texts["direct_injection"]}]
    assert first["output_config"]["format"]["schema"]["properties"]["answer"]["enum"] == [
        "yes",
        "no",
    ]


def test_gate_runs_once_per_payload(
    engine: Engine,
    add_payload: Callable[..., int],
    payload_texts: dict[str, str],
    recorded: Callable[[str], Message],
    fake_client: Callable[..., Any],
    taxonomy: Taxonomy,
) -> None:
    add_payload(payload_texts["direct_injection"])
    gate.run(engine, fake_client(recorded("gate_yes")), taxonomy)
    # Nothing is pending the second time, so the client is not called (it has no responses).
    assert gate.run(engine, fake_client(), taxonomy) == {}


def test_refusals_and_api_errors_leave_the_payload_pending(
    engine: Engine,
    add_payload: Callable[..., int],
    payload_texts: dict[str, str],
    recorded: Callable[[str], Message],
    fake_client: Callable[..., Any],
    taxonomy: Taxonomy,
) -> None:
    first = add_payload(payload_texts["direct_injection"])
    second = add_payload(payload_texts["encoded_injection"])
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.APIConnectionError(request=request)

    counts = gate.run(engine, fake_client(recorded("gate_refusal"), error), taxonomy)

    assert counts == {"unusable": 1, "errors": 1}
    assert _verdicts(engine) == {first: (None, None), second: (None, None)}


def test_long_payloads_are_cut(
    engine: Engine,
    add_payload: Callable[..., int],
    recorded: Callable[[str], Message],
    fake_client: Callable[..., Any],
    taxonomy: Taxonomy,
) -> None:
    add_payload("Synthetic honeypot test data. " + "x" * 30_000)
    client = fake_client(recorded("gate_no"))
    gate.run(engine, client, taxonomy)
    assert len(client.messages.requests[0]["messages"][0]["content"]) == 20_000
