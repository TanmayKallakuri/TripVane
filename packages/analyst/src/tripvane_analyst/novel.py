"""Novel-tag proposals: claude-opus-5-5 proposes a technique tag for technique/other payloads.

Each payload tagged technique "other" under the current taxonomy gets one proposal, a
tag name and a one-sentence description, written to tag_proposals. Nothing here reads or
writes taxonomy/tags.yaml; a human decides what becomes a tag.
"""

import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any

import anthropic
from sqlalchemy import Connection, Engine, Table, and_, exists, insert, select

from tripvane_analyst.common import ModelClient, UnusableResponse, model_texts, structured_output
from tripvane_core import models
from tripvane_core.taxonomy import Taxonomy

log = logging.getLogger(__name__)

tags_table: Table = models.Tag.__table__  # type: ignore[assignment]
payload_tags: Table = models.PayloadTag.__table__  # type: ignore[assignment]
tag_proposals: Table = models.TagProposal.__table__  # type: ignore[assignment]

MODEL = "claude-opus-5-5"
MAX_TOKENS = 8192
EFFORT = "medium"
PROMPT = (Path(__file__).parent / "novel.md").read_text(encoding="utf-8")
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "description": {"type": "string"}},
    "required": ["name", "description"],
    "additionalProperties": False,
}
MAX_NAME = 64


def request(text: str, taxonomy: Taxonomy) -> dict[str, Any]:
    techniques = "\n".join(
        f"- {name}: {description}" for name, description in taxonomy.axes["technique"].items()
    )
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": PROMPT.replace("{techniques}", techniques),
        "output_config": {
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": SCHEMA},
        },
        "messages": [{"role": "user", "content": text}],
    }


def parse(message: anthropic.types.Message) -> tuple[str, str]:
    """(name, description); the name is coerced to snake_case."""
    data = structured_output(message)
    name, description = data.get("name"), data.get("description")
    if not isinstance(name, str) or not isinstance(description, str):
        raise UnusableResponse("name and description must be strings")
    name = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:MAX_NAME]
    description = " ".join(description.split())
    if not name or not description:
        raise UnusableResponse("empty name or description")
    return name, description


def pending(conn: Connection, taxonomy_version: str) -> list[int]:
    """Payloads tagged technique/other under the current taxonomy with no proposal yet."""
    proposed = exists().where(
        and_(
            tag_proposals.c.payload_id == payload_tags.c.payload_id,
            tag_proposals.c.taxonomy_version == taxonomy_version,
        )
    )
    rows = conn.execute(
        select(payload_tags.c.payload_id)
        .join(tags_table, tags_table.c.id == payload_tags.c.tag_id)
        .where(
            payload_tags.c.taxonomy_version == taxonomy_version,
            tags_table.c.axis == "technique",
            tags_table.c.name == "other",
            ~proposed,
        )
        .order_by(payload_tags.c.payload_id)
    )
    return list(rows.scalars())


def store(
    conn: Connection, payload_id: int, name: str, description: str, taxonomy_version: str
) -> None:
    conn.execute(
        insert(tag_proposals).values(
            payload_id=payload_id,
            taxonomy_version=taxonomy_version,
            name=name,
            description=description,
        )
    )


def run(engine: Engine, client: ModelClient, taxonomy: Taxonomy) -> Counter[str]:
    """Write one proposal per pending payload, committed as each arrives."""
    with engine.connect() as conn:
        texts = model_texts(conn, pending(conn, taxonomy.version))
    counts: Counter[str] = Counter()
    for payload_id, text in texts.items():
        try:
            name, description = parse(client.messages.create(**request(text, taxonomy)))
        except anthropic.APIError as exc:
            log.warning("novel: payload %d: API error %s", payload_id, type(exc).__name__)
            counts["errors"] += 1
            continue
        except UnusableResponse as exc:
            log.warning("novel: payload %d: unusable response: %s", payload_id, exc)
            counts["unusable"] += 1
            continue
        with engine.begin() as conn:
            store(conn, payload_id, name, description, taxonomy.version)
        counts["proposed"] += 1
    return counts
