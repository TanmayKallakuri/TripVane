"""What the analyst steps share: the model client shape, payload text and response parsing.

The text sent to a model is the raw_text of the payload's earliest input_received event,
not payloads.normalized_text: normalization lowercases, which destroys case-sensitive
encodings such as base64 that the tagger must recognise. Identical payloads differ only
in case and whitespace, so any one of their inputs represents them.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from anthropic.types import Message
from sqlalchemy import Connection, Table, select

from tripvane_core import models

payloads: Table = models.Payload.__table__  # type: ignore[assignment]
events: Table = models.Event.__table__  # type: ignore[assignment]

# The longest payload text sent to a model; a longer one is cut here to bound spend.
# The GitHub sensor already caps its raw_text at the same length.
MAX_PAYLOAD_CHARS = 20_000

_IN_CHUNK = 500


class ModelClient(Protocol):
    """The part of anthropic.Anthropic the analyst uses; tests pass a fake."""

    @property
    def messages(self) -> Any: ...  # noqa: ANN401


class UnusableResponse(Exception):
    """The model answered, but not with a result we can store (refusal, cut off, invalid)."""


@dataclass(frozen=True)
class FirstInput:
    raw_text: str
    asn: int | None


def first_inputs(conn: Connection, payload_ids: Iterable[int]) -> dict[int, FirstInput]:
    """The earliest input_received event of each payload: its raw_text and source ASN."""
    ids = list(payload_ids)
    found: dict[int, FirstInput] = {}
    for start in range(0, len(ids), _IN_CHUNK):
        rows = conn.execute(
            select(events.c.payload_id, events.c.payload)
            .where(
                events.c.type == "input_received",
                events.c.payload_id.in_(ids[start : start + _IN_CHUNK]),
            )
            .order_by(events.c.payload_id, events.c.ts, events.c.id)
        )
        for payload_id, event in rows:
            if payload_id not in found:
                found[payload_id] = FirstInput(event["raw_text"], event["source"].get("asn"))
    return found


def model_texts(conn: Connection, payload_ids: Iterable[int]) -> dict[int, str]:
    """The text each payload is classified on, cut at MAX_PAYLOAD_CHARS."""
    ids = list(payload_ids)
    inputs = first_inputs(conn, ids)
    texts = {pid: inputs[pid].raw_text for pid in ids if pid in inputs}
    missing = [pid for pid in ids if pid not in texts]
    if missing:
        # A payload always has an input event; fall back to the stored text regardless.
        rows = conn.execute(
            select(payloads.c.id, payloads.c.normalized_text).where(payloads.c.id.in_(missing))
        )
        texts.update({payload_id: text for payload_id, text in rows})
    return {pid: text[:MAX_PAYLOAD_CHARS] for pid, text in texts.items()}


def structured_output(message: Message) -> dict[str, Any]:
    """The JSON object a structured-output response carries.

    Raises UnusableResponse for a refusal, a response cut off at max_tokens, or text that
    is not a JSON object.
    """
    if message.stop_reason != "end_turn":
        raise UnusableResponse(f"stop_reason {message.stop_reason}")
    text = next((block.text for block in message.content if block.type == "text"), None)
    if text is None:
        raise UnusableResponse("no text block")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UnusableResponse("text is not JSON") from exc
    if not isinstance(data, dict):
        raise UnusableResponse("JSON is not an object")
    return data


def system_block(text: str) -> list[dict[str, Any]]:
    """A system prompt as one block with a cache breakpoint."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]
