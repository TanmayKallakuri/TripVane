"""Campaigns: attack payloads grouped by near-identical text and source ASN. No model.

Two attack payloads are in the same campaign when they share a source ASN and the 64-bit
simhashes of their normalized text are within Hamming distance 3; campaigns are the
connected groups of that relation. A payload's ASN is the one on its earliest
input_received event. Payloads with no ASN are grouped with each other, as one unknown
ASN, so that text similarity still groups them.

campaign_id is the smallest payload id in the campaign, which keeps an id stable as later
payloads join; when a new payload links two campaigns, the merged one keeps the smaller id.
"""

import hashlib
from collections import Counter, defaultdict
from collections.abc import Iterable

from sqlalchemy import Engine, or_, select, update

from tripvane_analyst.common import first_inputs, payloads

BITS = 64
MAX_DISTANCE = 3
# Two hashes within distance 3 agree exactly on at least one of four 16-bit blocks.
_BLOCKS = MAX_DISTANCE + 1
_BLOCK_BITS = BITS // _BLOCKS


def simhash(text: str) -> int:
    """64-bit simhash over the words of the text, each weighted by its count."""
    weights = [0] * BITS
    for word, count in Counter(text.split()).items():
        digest = int.from_bytes(hashlib.blake2b(word.encode("utf-8"), digest_size=8).digest())
        for bit in range(BITS):
            weights[bit] += count if digest >> bit & 1 else -count
    return sum(1 << bit for bit in range(BITS) if weights[bit] > 0)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def cluster(items: Iterable[tuple[int, int | None, str]]) -> dict[int, int]:
    """Map each payload id to its campaign id, from (payload id, asn, normalized text)."""
    hashes = {payload_id: (asn, simhash(text)) for payload_id, asn, text in items}
    parent = {payload_id: payload_id for payload_id in hashes}

    def root(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    buckets: defaultdict[tuple[int | None, int, int], list[int]] = defaultdict(list)
    for payload_id, (asn, value) in hashes.items():
        for block in range(_BLOCKS):
            key = value >> (block * _BLOCK_BITS) & ((1 << _BLOCK_BITS) - 1)
            buckets[(asn, block, key)].append(payload_id)
    for members in buckets.values():
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                if hamming(hashes[a][1], hashes[b][1]) <= MAX_DISTANCE:
                    ra, rb = root(a), root(b)
                    if ra != rb:
                        parent[max(ra, rb)] = min(ra, rb)
    return {payload_id: root(payload_id) for payload_id in hashes}


def run(engine: Engine) -> Counter[str]:
    """Regroup when any attack has no campaign or any non-attack still has one.

    Grouping is recomputed over all attacks, because one new payload can join or merge
    existing campaigns; only rows whose campaign_id changes are written.
    """
    counts: Counter[str] = Counter()
    with engine.begin() as conn:
        due = conn.scalar(
            select(payloads.c.id)
            .where(
                or_(
                    payloads.c.is_attack.is_(True) & payloads.c.campaign_id.is_(None),
                    payloads.c.is_attack.is_not(True) & payloads.c.campaign_id.is_not(None),
                )
            )
            .limit(1)
        )
        if due is None:
            return counts
        cleared = conn.execute(
            update(payloads)
            .where(payloads.c.is_attack.is_not(True), payloads.c.campaign_id.is_not(None))
            .values(campaign_id=None)
            .returning(payloads.c.id)
        )
        counts["cleared"] = len(cleared.all())
        attacks = conn.execute(
            select(payloads.c.id, payloads.c.normalized_text, payloads.c.campaign_id).where(
                payloads.c.is_attack.is_(True)
            )
        ).all()
        inputs = first_inputs(conn, [row.id for row in attacks])
        campaigns = cluster(
            (row.id, inputs[row.id].asn if row.id in inputs else None, row.normalized_text)
            for row in attacks
        )
        for row in attacks:
            if row.campaign_id != campaigns[row.id]:
                conn.execute(
                    update(payloads)
                    .where(payloads.c.id == row.id)
                    .values(campaign_id=campaigns[row.id])
                )
                counts["assigned"] += 1
        counts["campaigns"] = len(set(campaigns.values()))
    return counts
