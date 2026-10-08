"""The weekly brief: claude-opus-5-5 drafts it from the period's figures in the collector.

build_context() queries the database for the period and returns the figures as a dict;
the model sees only those figures, never captured payload text, so nothing an attacker
wrote reaches the brief. The draft is rendered to Markdown, every number in it is checked
against the context (UncitedNumbers if one is missing), and it is written to
web/briefs/YYYY-MM-DD.md with "DRAFT" as its first line. Nothing here publishes a brief
or removes the marker; a human does both.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from anthropic.types import Message
from sqlalchemy import Connection, Engine, Select, func, select

from tripvane_analyst.common import ModelClient, UnusableResponse, structured_output
from tripvane_core.models import (
    CanaryHit,
    Event,
    Payload,
    PayloadTag,
    Sensor,
    SessionSource,
    Source,
    Tag,
)
from tripvane_core.models import Session as SessionRow

MODEL = "claude-opus-5-5"
MAX_TOKENS = 16000
EFFORT = "high"
PROMPT = (Path(__file__).parent / "brief.md").read_text(encoding="utf-8")
BRIEFS_DIR = Path(__file__).resolve().parents[4] / "web" / "briefs"
MARKER = "DRAFT"
TOP_SOURCES = 5
TOP_CAMPAIGNS = 10
FIGURES = 3
FINDINGS = 3
MAX_RECOMMENDATIONS = 4

_STRING = {"type": "string"}
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": _STRING,
        "by_the_numbers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"figure": _STRING, "label": _STRING},
                "required": ["figure", "label"],
                "additionalProperties": False,
            },
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": _STRING, "body": _STRING},
                "required": ["title", "body"],
                "additionalProperties": False,
            },
        },
        "recommendations": {"type": "array", "items": _STRING},
    },
    "required": ["headline", "by_the_numbers", "findings", "recommendations"],
    "additionalProperties": False,
}

# What the figures cannot show, stated in every brief's context. No digits, so the gaps
# add no numbers the draft could cite.
DATA_GAPS = [
    "No component records source ASNs yet, so sources are grouped under asn null.",
    "Payloads the analyst has not classified yet are counted only in payloads_pending_gate.",
    "Campaigns group near-identical payload text only; looser variants are separate campaigns.",
]

# A number as written in prose: digits with optional thousands separators and decimals.
_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")


class UncitedNumbers(ValueError):
    """The draft contains numbers that are not in its context."""

    def __init__(self, numbers: list[str]) -> None:
        super().__init__(f"numbers not in the context: {', '.join(numbers)}")
        self.numbers = numbers


@dataclass(frozen=True)
class Draft:
    headline: str
    by_the_numbers: list[tuple[str, str]]
    findings: list[tuple[str, str]]
    recommendations: list[str]


def period(today: date, days: int) -> tuple[datetime, datetime]:
    """The `days` full UTC days before `today`: [start, end)."""
    end = datetime.combine(today, time(), tzinfo=UTC)
    return end - timedelta(days=days), end


def build_context(conn: Connection, start: datetime, end: datetime) -> dict[str, Any]:
    """The period's figures, as the model sees them."""
    in_period = Event.ts >= start, Event.ts < end
    # Every payload received during the period, through its input_received events.
    received = (
        select(Event.payload_id)
        .where(Event.type == "input_received", Event.payload_id.is_not(None), *in_period)
        .distinct()
    )
    attacks = select(Payload.id).where(Payload.id.in_(received), Payload.is_attack.is_(True))
    attack_count = conn.execute(select(func.count()).select_from(attacks.subquery())).scalar_one()
    new_attacks = conn.execute(
        select(func.count()).where(
            Payload.id.in_(attacks), Payload.first_seen >= start, Payload.first_seen < end
        )
    ).scalar_one()
    pending = conn.execute(
        select(func.count()).where(Payload.id.in_(received), Payload.gate_version.is_(None))
    ).scalar_one()
    attack_sessions = (
        select(Event.session_id)
        .join(Payload, Payload.id == Event.payload_id)
        .where(Event.type == "input_received", Payload.is_attack.is_(True), *in_period)
        .distinct()
    )
    return {
        "period": {
            "first_day": start.date().isoformat(),
            "last_day": (end - timedelta(days=1)).date().isoformat(),
            "days": (end - start).days,
        },
        "new_attack_payloads": new_attacks,
        "attack_payloads_received": attack_count,
        "payloads_pending_gate": pending,
        "tag_distribution": _tag_distribution(conn, attacks, attack_count),
        "top_sources_by_asn": _top_sources(conn, attack_sessions),
        **_campaigns(conn, attacks, start, end),
        "canary_hits": _canary_hits(conn, start, end),
        "sensor_coverage": _sensor_coverage(conn, start, end),
        "data_gaps": DATA_GAPS,
    }


def _tag_distribution(
    conn: Connection, attacks: Select[tuple[int]], attack_count: int
) -> dict[str, Any]:
    """Axis -> tag -> attack payloads carrying it (tags read under each gate_version)."""
    rows = conn.execute(
        select(Tag.axis, Tag.name, func.count(Payload.id.distinct()), func.min(Tag.id))
        .join(PayloadTag, PayloadTag.tag_id == Tag.id)
        .join(
            Payload,
            (Payload.id == PayloadTag.payload_id)
            & (PayloadTag.taxonomy_version == Payload.gate_version),
        )
        .where(Payload.id.in_(attacks))
        .group_by(Tag.axis, Tag.name)
    ).all()
    # Axes and tags in taxonomy file order, the order the tagger added them to tags.
    order = {
        axis: first
        for axis, first in conn.execute(select(Tag.axis, func.min(Tag.id)).group_by(Tag.axis))
    }
    distribution: dict[str, Any] = {}
    for axis, name, count, _ in sorted(rows, key=lambda r: (order[r[0]], -r[2], r[3])):
        distribution.setdefault(axis, {})[name] = count
    tagged = conn.execute(
        select(func.count(Payload.id.distinct()))
        .join(
            PayloadTag,
            (Payload.id == PayloadTag.payload_id)
            & (PayloadTag.taxonomy_version == Payload.gate_version),
        )
        .where(Payload.id.in_(attacks))
    ).scalar_one()
    return {"axes": distribution, "untagged": attack_count - tagged}


def _top_sources(conn: Connection, attack_sessions: Select[tuple[str]]) -> list[dict[str, Any]]:
    sessions = func.count(SessionSource.session_id.distinct())
    rows = conn.execute(
        select(Source.asn, sessions, func.count(Source.id.distinct()))
        .join(SessionSource, SessionSource.source_id == Source.id)
        .where(SessionSource.session_id.in_(attack_sessions))
        .group_by(Source.asn)
        .order_by(sessions.desc(), Source.asn.nulls_last())
        .limit(TOP_SOURCES)
    ).all()
    return [{"asn": asn, "sessions": n, "source_ips": ips} for asn, n, ips in rows]


def _campaigns(
    conn: Connection, attacks: Select[tuple[int]], start: datetime, end: datetime
) -> dict[str, Any]:
    payloads = func.count(Payload.id.distinct())
    sessions = func.count(Event.session_id.distinct())
    rows = conn.execute(
        select(Payload.campaign_id, payloads, sessions)
        .join(Event, Event.payload_id == Payload.id)
        .where(
            Payload.id.in_(attacks),
            Payload.campaign_id.is_not(None),
            Event.type == "input_received",
            Event.ts >= start,
            Event.ts < end,
        )
        .group_by(Payload.campaign_id)
        .order_by(payloads.desc(), sessions.desc(), Payload.campaign_id)
    ).all()
    return {
        "active_campaigns": len(rows),
        "campaigns": [
            {"campaign_id": campaign_id, "payloads": n, "sessions": s}
            for campaign_id, n, s in rows[:TOP_CAMPAIGNS]
        ],
    }


def _canary_hits(conn: Connection, start: datetime, end: datetime) -> dict[str, int]:
    """Canary hits: a URL hit has no secret, a credential hit carries the secret found."""
    total, credentials = conn.execute(
        select(func.count(CanaryHit.id), func.count(CanaryHit.secret)).where(
            CanaryHit.ts >= start, CanaryHit.ts < end
        )
    ).one()
    return {"total": total, "url_hits": total - credentials, "credential_hits": credentials}


def _sensor_coverage(conn: Connection, start: datetime, end: datetime) -> list[dict[str, Any]]:
    sensors = conn.execute(select(Sensor.archetype, func.count()).group_by(Sensor.archetype)).all()
    rows = conn.execute(
        select(Sensor.archetype, func.count(SessionRow.id))
        .join(SessionRow, SessionRow.sensor_id == Sensor.id)
        .where(SessionRow.started_at >= start, SessionRow.started_at < end)
        .group_by(Sensor.archetype)
    )
    # Iterated: a Result has keys(), so dict(result) would not read its rows.
    sessions = {archetype: count for archetype, count in rows}
    return [
        {"archetype": archetype, "sensors": n, "sessions": sessions.get(archetype, 0)}
        for archetype, n in sorted(sensors)
    ]


def request(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": PROMPT,
        "output_config": {
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": SCHEMA},
        },
        "messages": [{"role": "user", "content": context_text(context)}],
    }


def context_text(context: dict[str, Any]) -> str:
    return json.dumps(context, indent=2)


def parse(message: Message) -> Draft:
    """The draft in the required structure; UnusableResponse for anything else."""
    data = structured_output(message)
    try:
        draft = Draft(
            headline=_text(data["headline"]),
            by_the_numbers=[
                (_text(i["figure"]), _text(i["label"])) for i in data["by_the_numbers"]
            ],
            findings=[(_text(i["title"]), _text(i["body"])) for i in data["findings"]],
            recommendations=[_text(r) for r in data["recommendations"]],
        )
    except (KeyError, TypeError) as exc:
        raise UnusableResponse(f"draft does not match the schema: {exc!r}") from exc
    if len(draft.by_the_numbers) != FIGURES:
        raise UnusableResponse(f"{len(draft.by_the_numbers)} figures, expected {FIGURES}")
    if len(draft.findings) != FINDINGS:
        raise UnusableResponse(f"{len(draft.findings)} findings, expected {FINDINGS}")
    if not 1 <= len(draft.recommendations) <= MAX_RECOMMENDATIONS:
        raise UnusableResponse(f"{len(draft.recommendations)} recommendations")
    if not all(_NUMBER.search(figure) for figure, _ in draft.by_the_numbers):
        raise UnusableResponse("a figure has no number")
    return draft


def _text(value: Any) -> str:  # noqa: ANN401
    if not isinstance(value, str) or not value.strip():
        raise UnusableResponse("empty or non-string field")
    return " ".join(value.split())


def render(draft: Draft, context: dict[str, Any]) -> str:
    """The brief as Markdown, without the DRAFT marker."""
    first, last = context["period"]["first_day"], context["period"]["last_day"]
    lines = [f"# Tripvane brief: {first} to {last}", "", "## Headline finding", "", draft.headline]
    lines += ["", "## By the numbers", ""]
    lines += [f"- **{figure}** {label}" for figure, label in draft.by_the_numbers]
    lines += ["", "## Findings"]
    for title, body in draft.findings:
        lines += ["", f"### {title}", "", body]
    lines += ["", "## Recommendations", ""]
    lines += [f"- {recommendation}" for recommendation in draft.recommendations]
    return "\n".join(lines) + "\n"


def numbers(text: str) -> set[str]:
    """Every number in text, canonical (no separators, no leading or trailing zeros)."""
    found = set()
    for match in _NUMBER.findall(text):
        value = Decimal(match.replace(",", ""))
        found.add(format(value.normalize(), "f") if value else "0")
    return found


def assert_numbers_cited(markdown: str, context: dict[str, Any]) -> None:
    """Raise UncitedNumbers unless every number in the draft appears in the context."""
    missing = numbers(markdown) - numbers(context_text(context))
    if missing:
        raise UncitedNumbers(sorted(missing, key=Decimal))


def write(markdown: str, day: date, briefs_dir: Path = BRIEFS_DIR) -> Path:
    """Write the draft with the DRAFT marker as its first line; never overwrite a brief."""
    path = briefs_dir / f"{day.isoformat()}.md"
    briefs_dir.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as file:
        file.write(f"{MARKER}\n\n{markdown}")
    return path


@dataclass(frozen=True)
class Result:
    path: Path
    input_tokens: int
    output_tokens: int


def run(
    engine: Engine,
    client: ModelClient,
    days: int,
    today: date,
    briefs_dir: Path = BRIEFS_DIR,
) -> Result:
    """Draft the brief for the `days` full days before `today` and write it."""
    path = briefs_dir / f"{today.isoformat()}.md"
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    start, end = period(today, days)
    with engine.connect() as conn:
        context = build_context(conn, start, end)
    message = client.messages.create(**request(context))
    markdown = render(parse(message), context)
    assert_numbers_cited(markdown, context)
    return Result(
        write(markdown, today, briefs_dir),
        message.usage.input_tokens,
        message.usage.output_tokens,
    )
