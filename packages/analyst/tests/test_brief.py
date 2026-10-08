"""The brief drafter: context from fixture rows, the number check, and the DRAFT file."""

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from anthropic.types import Message
from sqlalchemy import Engine, insert

from tripvane_analyst import brief, tagger
from tripvane_analyst.cli import main
from tripvane_analyst.common import UnusableResponse
from tripvane_core.hashing import normalize, payload_hash
from tripvane_core.models import (
    CanaryHit,
    Event,
    Payload,
    PayloadTag,
    Sensor,
    SessionSource,
    Source,
)
from tripvane_core.models import Session as SessionRow
from tripvane_core.taxonomy import Taxonomy

TODAY = date(2026, 10, 8)
REPO = Path(__file__).resolve().parents[3]


def _at(day: int, month: int = 10) -> datetime:
    return datetime(2026, month, day, 12, 0, tzinfo=UTC)


class Grid:
    """A small synthetic grid, written as the collector and analyst would leave it."""

    def __init__(self, engine: Engine, taxonomy: Taxonomy) -> None:
        self.engine = engine
        self.version = taxonomy.version
        self.sources: dict[str, int] = {}
        self.ids: dict[str, int] = {}
        with engine.begin() as conn:
            conn.execute(
                insert(Sensor),
                [
                    {"id": "mcp-1", "name": "m", "archetype": "mcp", "token_hash": "1"},
                    {"id": "infra-1", "name": "i", "archetype": "infra", "token_hash": "2"},
                ],
            )
            self.tag_ids = tagger.sync_tags(conn, taxonomy)

    def payload(self, label: str, first_seen: datetime, **columns: Any) -> int:  # noqa: ANN401
        text = f"Synthetic honeypot test data. Payload {label}."
        with self.engine.begin() as conn:
            return conn.execute(
                insert(Payload)
                .values(
                    payload_hash=payload_hash(text),
                    normalized_text=normalize(text),
                    first_seen=first_seen,
                    last_seen=first_seen,
                    seen_count=1,
                    **columns,
                )
                .returning(Payload.id)
            ).scalar_one()

    def session(
        self, name: str, sensor: str, ts: datetime, ip: str | None, asn: int | None = None
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(SessionRow).values(id=name, sensor_id=sensor, started_at=ts))
            if ip is None:
                return
            if ip not in self.sources:
                self.sources[ip] = conn.execute(
                    insert(Source)
                    .values(ip=ip, asn=asn, first_seen=ts, last_seen=ts)
                    .returning(Source.id)
                ).scalar_one()
            conn.execute(insert(SessionSource).values(source_id=self.sources[ip], session_id=name))

    def receive(self, session: str, sensor: str, ts: datetime, payload_id: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(Event).values(
                    sensor_id=sensor,
                    session_id=session,
                    event_seq=1,
                    type="input_received",
                    ts=ts,
                    payload={"type": "input_received", "raw_text": "honeypot test data"},
                    payload_id=payload_id,
                )
            )

    def tag(self, payload_id: int, tags: dict[str, str], version: str | None = None) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(PayloadTag),
                [
                    {
                        "payload_id": payload_id,
                        "tag_id": self.tag_ids[(axis, name)],
                        "taxonomy_version": version or self.version,
                        "confidence": 0.9,
                    }
                    for axis, name in tags.items()
                ],
            )

    def canary_hit(self, ts: datetime, secret: str | None) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(CanaryHit).values(ts=ts, source={"ip": "192.0.2.99"}, secret=secret)
            )


@pytest.fixture
def grid(engine: Engine, taxonomy: Taxonomy) -> Grid:
    """The period is 2026-10-01 to 2026-10-07; rows outside it must not count."""
    g = Grid(engine, taxonomy)
    v = taxonomy.version
    new = g.payload("one", _at(2), is_attack=True, gate_version=v)
    recurring = g.payload("two", _at(20, 9), is_attack=True, gate_version=v)
    untagged = g.payload("three", _at(5), is_attack=True, gate_version=v)
    benign = g.payload("four", _at(5), is_attack=False, gate_version=v)
    pending = g.payload("five", _at(6))
    outside = g.payload("six", _at(25, 9), is_attack=True, gate_version=v)
    with engine.begin() as conn:
        for payload_id, campaign in [(new, new), (recurring, new), (untagged, untagged)]:
            conn.execute(
                Payload.__table__.update()
                .where(Payload.id == payload_id)
                .values(campaign_id=campaign)
            )
    g.tag(new, {"technique": "direct_instruction", "objective": "exfiltrate_secrets",
                "target_tool": "email"})  # fmt: skip
    g.tag(recurring, {"technique": "hidden_in_document", "objective": "exfiltrate_secrets",
                      "target_tool": "shell"})  # fmt: skip
    # A tag from an older taxonomy version is history and must not be counted.
    g.tag(recurring, {"technique": "role_override"}, version="0" * 64)
    g.tag(outside, {"technique": "encoded"})

    g.session("s1", "support-1", _at(2), "192.0.2.10")
    g.receive("s1", "support-1", _at(2), new)
    g.session("s2", "mcp-1", _at(3), "192.0.2.11", asn=64500)
    g.receive("s2", "mcp-1", _at(3), new)
    g.session("s3", "support-1", _at(4), "192.0.2.10")
    g.receive("s3", "support-1", _at(4), recurring)
    g.session("s4", "mcp-1", _at(5), "192.0.2.12", asn=64500)
    g.receive("s4", "mcp-1", _at(5), untagged)
    g.session("s5", "support-1", _at(5), "192.0.2.13")
    g.receive("s5", "support-1", _at(5), benign)
    g.session("s6", "support-1", _at(6), "192.0.2.14")
    g.receive("s6", "support-1", _at(6), pending)
    g.session("s7", "support-1", _at(25, 9), "192.0.2.15", asn=64501)
    g.receive("s7", "support-1", _at(25, 9), outside)
    g.session("s8", "infra-1", _at(6), None)
    g.session("s9", "infra-1", _at(8), None)

    g.canary_hit(_at(3), None)
    g.canary_hit(_at(4), "tvk_live_TESTONLY000000000000000")
    g.canary_hit(_at(30, 9), None)
    g.ids = {"new": new, "untagged": untagged}
    return g


def _context(engine: Engine) -> dict[str, Any]:
    start, end = brief.period(TODAY, 7)
    with engine.connect() as conn:
        return brief.build_context(conn, start, end)


def test_context_assembly_from_fixture_rows(grid: Grid) -> None:
    ids = grid.ids

    assert _context(grid.engine) == {
        "period": {"first_day": "2026-10-01", "last_day": "2026-10-07", "days": 7},
        "new_attack_payloads": 2,
        "attack_payloads_received": 3,
        "payloads_pending_gate": 1,
        "tag_distribution": {
            "axes": {
                "technique": {"direct_instruction": 1, "hidden_in_document": 1},
                "objective": {"exfiltrate_secrets": 2},
                "target_tool": {"email": 1, "shell": 1},
            },
            "untagged": 1,
        },
        "top_sources_by_asn": [
            {"asn": 64500, "sessions": 2, "source_ips": 2},
            {"asn": None, "sessions": 2, "source_ips": 1},
        ],
        "active_campaigns": 2,
        "campaigns": [
            {"campaign_id": ids["new"], "payloads": 2, "sessions": 3},
            {"campaign_id": ids["untagged"], "payloads": 1, "sessions": 1},
        ],
        "canary_hits": {"total": 2, "url_hits": 1, "credential_hits": 1},
        "sensor_coverage": [
            {"archetype": "infra", "sensors": 1, "sessions": 1},
            {"archetype": "mcp", "sensors": 1, "sessions": 2},
            {"archetype": "support", "sensors": 1, "sessions": 4},
        ],
        "data_gaps": brief.DATA_GAPS,
    }


def test_context_of_an_empty_grid_is_all_zeros(engine: Engine) -> None:
    context = _context(engine)

    assert context["new_attack_payloads"] == 0
    assert context["tag_distribution"] == {"axes": {}, "untagged": 0}
    assert context["top_sources_by_asn"] == []
    assert context["campaigns"] == []
    assert context["canary_hits"] == {"total": 0, "url_hits": 0, "credential_hits": 0}
    assert context["sensor_coverage"] == [{"archetype": "support", "sensors": 1, "sessions": 0}]


def test_brief_is_drafted_by_opus_and_written_with_the_draft_marker(
    grid: Grid,
    recorded: Callable[[str], Message],
    fake_client: Callable[..., Any],
    tmp_path: Path,
) -> None:
    client = fake_client(recorded("brief_draft"))

    result = brief.run(grid.engine, client, 7, TODAY, briefs_dir=tmp_path)

    assert result.path == tmp_path / "2026-10-08.md"
    assert (result.input_tokens, result.output_tokens) == (1450, 980)
    text = result.path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "DRAFT"
    assert lines[2] == "# Tripvane brief: 2026-10-01 to 2026-10-07"
    headings = [line for line in lines if line.startswith("#")]
    assert headings[1:3] == ["## Headline finding", "## By the numbers"]
    assert headings[3] == "## Findings"
    assert len([h for h in headings if h.startswith("### ")]) == 3
    assert headings[-1] == "## Recommendations"
    section = lines[lines.index("## By the numbers") + 2 : lines.index("## Findings") - 1]
    assert section == [
        "- **2** new attack payloads",
        "- **3** distinct attack payloads received",
        "- **2** canary hits",
    ]

    [request] = client.messages.requests
    assert request["model"] == "claude-opus-5-5"
    assert request["output_config"]["effort"] == "high"
    assert request["output_config"]["format"]["schema"] == brief.SCHEMA
    assert "only numbers that appear in the JSON" in request["system"]
    # The model sees the figures only, never captured payload text.
    assert json.loads(request["messages"][0]["content"]) == _context(grid.engine)
    assert "honeypot test data" not in request["messages"][0]["content"]


def test_a_draft_with_an_invented_figure_fails_loudly_and_writes_nothing(
    grid: Grid,
    recorded: Callable[[str], Message],
    fake_client: Callable[..., Any],
    tmp_path: Path,
) -> None:
    client = fake_client(recorded("brief_invented_figure"))

    with pytest.raises(brief.UncitedNumbers) as raised:
        brief.run(grid.engine, client, 7, TODAY, briefs_dir=tmp_path / "briefs")

    assert raised.value.numbers == ["41"]
    assert "41" in str(raised.value)
    assert not (tmp_path / "briefs").exists()


def test_number_check() -> None:
    context = {"a": 1234, "b": 0.5, "day": "2026-10-07", "c": 0}

    assert brief.numbers("1,234 and 0.50 and 07, then 2026 and 0.") == {
        "1234",
        "0.5",
        "7",
        "2026",
        "0",
    }
    brief.assert_numbers_cited("Saw 1,234 payloads on 2026-10-07, 0 hits, 0.5 share.", context)
    with pytest.raises(brief.UncitedNumbers, match="12, 99.5"):
        brief.assert_numbers_cited("12 sessions and 99.5 percent and 1234 payloads", context)


def test_a_draft_in_the_wrong_structure_is_unusable(
    recorded: Callable[[str], Message],
) -> None:
    with pytest.raises(UnusableResponse, match="2 findings, expected 3"):
        brief.parse(recorded("brief_two_findings"))


def test_an_existing_brief_is_never_overwritten(
    grid: Grid,
    recorded: Callable[[str], Message],
    fake_client: Callable[..., Any],
    tmp_path: Path,
) -> None:
    existing = tmp_path / "2026-10-08.md"
    existing.write_text("Reviewed by a human.\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        brief.run(grid.engine, fake_client(recorded("brief_draft")), 7, TODAY, tmp_path)

    assert existing.read_text(encoding="utf-8") == "Reviewed by a human.\n"


def test_brief_command_needs_the_key_and_a_positive_day_count(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'analyst.db'}")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["brief", "--days", "7"]) == 2
    with pytest.raises(SystemExit):
        main(["brief", "--days", "0"])


def test_opus_is_called_only_from_the_brief_and_novel_modules() -> None:
    # prices.py names every model in its price table but calls none.
    sources = sorted(REPO.glob("packages/*/src/**/*.py"))
    naming = [p.relative_to(REPO).as_posix() for p in sources if "claude-opus" in p.read_text()]

    assert naming == [
        "packages/analyst/src/tripvane_analyst/brief.py",
        "packages/analyst/src/tripvane_analyst/novel.py",
        "packages/core/src/tripvane_core/prices.py",
    ]
