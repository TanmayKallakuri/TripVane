"""The analyst gate: a claude-haiku-5-5 yes/no classifier, run once per unique payload.

It asks whether the input attempts to instruct an AI agent to take an action its
operator did not request. The verdict and the taxonomy_version current when it was given
are stored on the payloads row. `gate` handles payloads never gated; payloads gated
under an older taxonomy are re-gated through the Batch API (batch.py).
"""

import logging
from collections import Counter
from pathlib import Path
from typing import Any

import anthropic
from sqlalchemy import Connection, Engine, select, update

from tripvane_analyst.common import (
    ModelClient,
    UnusableResponse,
    model_texts,
    payloads,
    structured_output,
)
from tripvane_core.taxonomy import Taxonomy

log = logging.getLogger(__name__)

MODEL = "claude-haiku-5-5"
MAX_TOKENS = 128
PROMPT = (Path(__file__).parent / "gate.md").read_text(encoding="utf-8")
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string", "enum": ["yes", "no"]}},
    "required": ["answer"],
    "additionalProperties": False,
}


def request(text: str) -> dict[str, Any]:
    """The Messages API request body for one payload, used live and in batches."""
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": PROMPT,
        "thinking": {"type": "disabled"},
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
        "messages": [{"role": "user", "content": text}],
    }


def parse(message: anthropic.types.Message) -> bool:
    """True when the gate says the payload is an attack."""
    answer = structured_output(message).get("answer")
    if answer not in ("yes", "no"):
        raise UnusableResponse(f"answer {answer!r}")
    return answer == "yes"


def pending(conn: Connection) -> list[int]:
    """Payloads that have never been gated."""
    rows = conn.execute(
        select(payloads.c.id).where(payloads.c.gate_version.is_(None)).order_by(payloads.c.id)
    )
    return list(rows.scalars())


def store(conn: Connection, payload_id: int, is_attack: bool, taxonomy_version: str) -> None:
    conn.execute(
        update(payloads)
        .where(payloads.c.id == payload_id)
        .values(is_attack=is_attack, gate_version=taxonomy_version)
    )


def run(engine: Engine, client: ModelClient, taxonomy: Taxonomy) -> Counter[str]:
    """Gate every pending payload; each verdict is committed as soon as it arrives."""
    with engine.connect() as conn:
        texts = model_texts(conn, pending(conn))
    counts: Counter[str] = Counter()
    for payload_id, text in texts.items():
        try:
            is_attack = parse(client.messages.create(**request(text)))
        except anthropic.APIError as exc:
            log.warning("gate: payload %d: API error %s", payload_id, type(exc).__name__)
            counts["errors"] += 1
            continue
        except UnusableResponse as exc:
            log.warning("gate: payload %d: unusable response: %s", payload_id, exc)
            counts["unusable"] += 1
            continue
        with engine.begin() as conn:
            store(conn, payload_id, is_attack, taxonomy.version)
        counts["gated"] += 1
        counts["attacks" if is_attack else "not_attacks"] += 1
    return counts
