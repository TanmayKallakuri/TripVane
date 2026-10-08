"""Batch API reprocessing after a taxonomy change, in two explicit steps.

batch-submit: every payload whose gate_version differs from the current taxonomy_version
is stale. It gets a gate request and, if it was an attack, a tagger request; they are
submitted as one Message Batch, recorded in analyst_batches. Nothing else is submitted
while a batch is uncollected, so no payload is ever in two batches.

batch-collect: for each uncollected batch that has ended, write the gate verdicts, then
the tags of payloads that are still attacks, and mark the batch collected. Results that
failed leave their payloads stale (gate) or untagged (tag), so the next submit or `tag`
run picks them up.
"""

import logging
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime

from anthropic.types import Message
from anthropic.types.messages.batch_create_params import Request
from sqlalchemy import Connection, Engine, Table, and_, exists, insert, select, update

from tripvane_analyst import gate, tagger
from tripvane_analyst.common import ModelClient, UnusableResponse, model_texts, payloads
from tripvane_core import models
from tripvane_core.taxonomy import Taxonomy

log = logging.getLogger(__name__)

batches: Table = models.AnalystBatch.__table__  # type: ignore[assignment]

# Stale payloads per batch. Each needs at most two requests of up to about 100 KB, which
# keeps a batch well under the API's 256 MB limit; the rest go in the next batch.
MAX_PAYLOADS = 1000


def _now() -> datetime:
    return datetime.now(UTC)


def stale(conn: Connection, taxonomy_version: str) -> list[tuple[int, bool]]:
    """(payload id, is_attack) for payloads gated under another taxonomy version."""
    rows = conn.execute(
        select(payloads.c.id, payloads.c.is_attack)
        .where(payloads.c.gate_version.is_not(None), payloads.c.gate_version != taxonomy_version)
        .order_by(payloads.c.id)
        .limit(MAX_PAYLOADS)
    )
    return [(payload_id, bool(is_attack)) for payload_id, is_attack in rows]


def build_requests(conn: Connection, taxonomy: Taxonomy) -> list[Request]:
    """The batch requests for the stale payloads; custom ids are gate-<id> and tag-<id>."""
    found = stale(conn, taxonomy.version)
    texts = model_texts(conn, [payload_id for payload_id, _ in found])
    requests = [
        Request(custom_id=f"gate-{payload_id}", params=gate.request(texts[payload_id]))
        for payload_id, _ in found
    ]
    requests += [
        Request(custom_id=f"tag-{payload_id}", params=tagger.request(texts[payload_id], taxonomy))
        for payload_id, was_attack in found
        if was_attack
    ]
    return requests


def submit(
    engine: Engine,
    client: ModelClient,
    taxonomy: Taxonomy,
    *,
    now: Callable[[], datetime] = _now,
) -> Counter[str]:
    counts: Counter[str] = Counter()
    with engine.connect() as conn:
        if conn.scalar(select(batches.c.id).where(batches.c.collected_at.is_(None)).limit(1)):
            log.warning("batch-submit: a batch is still uncollected; run batch-collect first")
            counts["waiting_for_collect"] += 1
            return counts
        requests = build_requests(conn, taxonomy)
    if not requests:
        return counts
    submitted = client.messages.batches.create(requests=requests)
    with engine.begin() as conn:
        conn.execute(
            insert(batches).values(
                id=submitted.id,
                taxonomy_version=taxonomy.version,
                request_count=len(requests),
                submitted_at=now(),
            )
        )
    counts["submitted_batches"] += 1
    counts["requests"] += len(requests)
    return counts


def collect(
    engine: Engine,
    client: ModelClient,
    taxonomy: Taxonomy,
    *,
    now: Callable[[], datetime] = _now,
) -> Counter[str]:
    counts: Counter[str] = Counter()
    with engine.connect() as conn:
        pending = conn.execute(
            select(batches.c.id, batches.c.taxonomy_version)
            .where(batches.c.collected_at.is_(None))
            .order_by(batches.c.submitted_at)
        ).all()
    for batch_id, version in pending:
        if client.messages.batches.retrieve(batch_id).processing_status != "ended":
            counts["in_progress"] += 1
            continue
        gate_results: dict[int, Message] = {}
        tag_results: dict[int, Message] = {}
        for item in client.messages.batches.results(batch_id):
            kind, _, payload_id = item.custom_id.partition("-")
            if item.result.type != "succeeded":
                log.warning("batch %s: %s %s", batch_id, item.custom_id, item.result.type)
                counts[f"{kind}_failed"] += 1
                continue
            target = gate_results if kind == "gate" else tag_results
            target[int(payload_id)] = item.result.message
        with engine.begin() as conn:
            if version == taxonomy.version:
                _write(conn, gate_results, tag_results, taxonomy, counts)
            else:
                # tags.yaml changed again while the batch ran; everything in it is stale.
                counts["discarded_batches"] += 1
            conn.execute(update(batches).where(batches.c.id == batch_id).values(collected_at=now()))
        counts["collected_batches"] += 1
    return counts


def _write(
    conn: Connection,
    gate_results: dict[int, Message],
    tag_results: dict[int, Message],
    taxonomy: Taxonomy,
    counts: Counter[str],
) -> None:
    for payload_id, message in gate_results.items():
        try:
            gate.store(conn, payload_id, gate.parse(message), taxonomy.version)
        except UnusableResponse as exc:
            log.warning("batch: gate %d: unusable response: %s", payload_id, exc)
            counts["gate_unusable"] += 1
            continue
        counts["gated"] += 1
    tag_ids = tagger.sync_tags(conn, taxonomy)
    tagged = exists().where(
        and_(
            tagger.payload_tags.c.payload_id == payloads.c.id,
            tagger.payload_tags.c.taxonomy_version == taxonomy.version,
        )
    )
    # Only payloads that are attacks under the new verdict and not tagged meanwhile.
    taggable = set(
        conn.execute(
            select(payloads.c.id).where(
                payloads.c.id.in_(list(tag_results)),
                payloads.c.is_attack.is_(True),
                payloads.c.gate_version == taxonomy.version,
                ~tagged,
            )
        ).scalars()
    )
    for payload_id, message in tag_results.items():
        if payload_id not in taggable:
            counts["tags_skipped"] += 1
            continue
        try:
            result = tagger.parse(message, taxonomy)
        except UnusableResponse as exc:
            log.warning("batch: tag %d: unusable response: %s", payload_id, exc)
            counts["tag_unusable"] += 1
            continue
        tagger.store(conn, payload_id, result, tag_ids, taxonomy.version)
        counts["tagged"] += 1
