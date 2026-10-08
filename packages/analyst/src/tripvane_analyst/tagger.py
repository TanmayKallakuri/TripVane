"""The tagger: claude-sonnet-5-5 assigns exactly one tag per taxonomy axis to an attack.

Structured output constrains each axis to the taxonomy's tag names. The instructions
and the taxonomy form one cached system prompt, loaded from tagger.md. Results are
payload_tags rows with a confidence and the taxonomy_version.
"""

import logging
from collections import Counter
from pathlib import Path
from typing import Any

import anthropic
from sqlalchemy import Connection, Engine, Table, and_, exists, insert, select

from tripvane_analyst.common import (
    ModelClient,
    UnusableResponse,
    model_texts,
    payloads,
    structured_output,
    system_block,
)
from tripvane_core import models
from tripvane_core.taxonomy import Taxonomy

log = logging.getLogger(__name__)

tags_table: Table = models.Tag.__table__  # type: ignore[assignment]
payload_tags: Table = models.PayloadTag.__table__  # type: ignore[assignment]

MODEL = "claude-sonnet-5-5"
MAX_TOKENS = 4096
EFFORT = "low"
PROMPT = (Path(__file__).parent / "tagger.md").read_text(encoding="utf-8")


def system_prompt(taxonomy: Taxonomy) -> str:
    sections = []
    for axis, tags in taxonomy.axes.items():
        lines = [f"## {axis}"] + [f"- {name}: {description}" for name, description in tags.items()]
        sections.append("\n".join(lines))
    return PROMPT.replace("{taxonomy}", "\n\n".join(sections))


def schema(taxonomy: Taxonomy) -> dict[str, Any]:
    axis_schema = {
        axis: {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "enum": taxonomy.tags(axis)},
                "confidence": {"type": "number"},
            },
            "required": ["tag", "confidence"],
            "additionalProperties": False,
        }
        for axis in taxonomy.axes
    }
    return {
        "type": "object",
        "properties": axis_schema,
        "required": list(taxonomy.axes),
        "additionalProperties": False,
    }


def request(text: str, taxonomy: Taxonomy) -> dict[str, Any]:
    """The Messages API request body for one payload, used live and in batches."""
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system_block(system_prompt(taxonomy)),
        "output_config": {
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": schema(taxonomy)},
        },
        "messages": [{"role": "user", "content": text}],
    }


def parse(message: anthropic.types.Message, taxonomy: Taxonomy) -> dict[str, tuple[str, float]]:
    """axis -> (tag name, confidence), exactly one per axis of the taxonomy."""
    data = structured_output(message)
    result = {}
    for axis in taxonomy.axes:
        value = data.get(axis)
        if not isinstance(value, dict):
            raise UnusableResponse(f"missing axis {axis}")
        tag, confidence = value.get("tag"), value.get("confidence")
        if tag not in taxonomy.axes[axis]:
            raise UnusableResponse(f"{axis}: unknown tag {tag!r}")
        if not isinstance(confidence, int | float) or not 0 <= confidence <= 1:
            raise UnusableResponse(f"{axis}: confidence {confidence!r} outside 0 to 1")
        result[axis] = (tag, float(confidence))
    return result


def sync_tags(conn: Connection, taxonomy: Taxonomy) -> dict[tuple[str, str], int]:
    """Insert the taxonomy's tags missing from the tags table; map (axis, name) to id.

    Tags removed from tags.yaml stay in the table, because older payload_tags refer to them.
    """
    existing = {(axis, name): tag_id for tag_id, axis, name in conn.execute(select(tags_table))}
    missing = [
        {"axis": axis, "name": name}
        for axis, tags in taxonomy.axes.items()
        for name in tags
        if (axis, name) not in existing
    ]
    if missing:
        conn.execute(insert(tags_table), missing)
        existing = {(axis, name): tag_id for tag_id, axis, name in conn.execute(select(tags_table))}
    return existing


def pending(conn: Connection, taxonomy_version: str) -> list[int]:
    """Attacks gated under the current taxonomy that have no tags under it yet."""
    tagged = exists().where(
        and_(
            payload_tags.c.payload_id == payloads.c.id,
            payload_tags.c.taxonomy_version == taxonomy_version,
        )
    )
    rows = conn.execute(
        select(payloads.c.id)
        .where(
            payloads.c.is_attack.is_(True),
            payloads.c.gate_version == taxonomy_version,
            ~tagged,
        )
        .order_by(payloads.c.id)
    )
    return list(rows.scalars())


def store(
    conn: Connection,
    payload_id: int,
    result: dict[str, tuple[str, float]],
    tag_ids: dict[tuple[str, str], int],
    taxonomy_version: str,
) -> None:
    conn.execute(
        insert(payload_tags),
        [
            {
                "payload_id": payload_id,
                "tag_id": tag_ids[(axis, tag)],
                "taxonomy_version": taxonomy_version,
                "confidence": confidence,
            }
            for axis, (tag, confidence) in result.items()
        ],
    )


def run(engine: Engine, client: ModelClient, taxonomy: Taxonomy) -> Counter[str]:
    """Tag every pending attack; each payload's tags are committed as they arrive."""
    with engine.begin() as conn:
        tag_ids = sync_tags(conn, taxonomy)
        texts = model_texts(conn, pending(conn, taxonomy.version))
    counts: Counter[str] = Counter()
    for payload_id, text in texts.items():
        try:
            result = parse(client.messages.create(**request(text, taxonomy)), taxonomy)
        except anthropic.APIError as exc:
            log.warning("tag: payload %d: API error %s", payload_id, type(exc).__name__)
            counts["errors"] += 1
            continue
        except UnusableResponse as exc:
            log.warning("tag: payload %d: unusable response: %s", payload_id, exc)
            counts["unusable"] += 1
            continue
        with engine.begin() as conn:
            store(conn, payload_id, result, tag_ids, taxonomy.version)
        counts["tagged"] += 1
    return counts
