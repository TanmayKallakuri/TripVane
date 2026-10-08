"""The cost report: spend per sensor for one UTC day, from model_turn events."""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, insert

from tripvane_collector import cost
from tripvane_core.models import Event, Sensor, Session

DAY = date(2026, 10, 7)


def _turn(
    engine: Engine,
    sensor_id: str,
    ts: datetime,
    tokens: tuple[int, int, int, int],
    model: str = "claude-haiku-5-5",
) -> None:
    """Store one model_turn event (input, output, cache write, cache read tokens)."""
    session_id = f"{sensor_id}-{ts.isoformat()}"
    event: dict[str, Any] = {
        "sensor_id": sensor_id,
        "session_id": session_id,
        "event_seq": 2,
        "ts": ts.isoformat(),
        "source": {"ip": "192.0.2.1", "asn": None, "headers_subset": {}},
        "type": "model_turn",
        "model": model,
        "input_tokens": tokens[0],
        "output_tokens": tokens[1],
        "cache_creation_input_tokens": tokens[2],
        "cache_read_input_tokens": tokens[3],
        "stop_reason": "end_turn",
        "assistant_text": "",
    }
    with engine.begin() as conn:
        conn.execute(insert(Session).values(id=session_id, sensor_id=sensor_id, started_at=ts))
        conn.execute(
            insert(Event).values(
                sensor_id=sensor_id,
                session_id=session_id,
                event_seq=2,
                type="model_turn",
                ts=ts,
                payload=event,
            )
        )


@pytest.fixture
def grid(engine: Engine) -> Engine:
    """support-1 and mcp-1 (from conftest) plus an infra sensor that never calls a model."""
    with engine.begin() as conn:
        conn.execute(
            insert(Sensor).values(id="infra-1", name="infra", archetype="infra", token_hash="x")
        )
    _turn(engine, "support-1", datetime(2026, 10, 7, 0, 0, tzinfo=UTC), (1000, 200, 300, 400))
    _turn(engine, "support-1", datetime(2026, 10, 7, 12, 0, tzinfo=UTC), (2000, 100, 0, 500))
    # Over 100,000 prompt tokens: Haiku's higher price.
    _turn(engine, "support-1", datetime(2026, 10, 7, 23, 59, tzinfo=UTC), (120_000, 1000, 0, 0))
    # Exactly 100,000 prompt tokens: the standard price.
    _turn(engine, "mcp-1", datetime(2026, 10, 7, 6, 0, tzinfo=UTC), (99_000, 0, 0, 1000))
    # Outside the day.
    _turn(engine, "mcp-1", datetime(2026, 10, 6, 23, 59, tzinfo=UTC), (5000, 5000, 0, 0))
    _turn(engine, "mcp-1", datetime(2026, 10, 8, 0, 0, tzinfo=UTC), (5000, 5000, 0, 0))
    return engine


def test_daily_spend_sums_tokens_and_cost_per_sensor(grid: Engine) -> None:
    with grid.connect() as conn:
        spend = cost.daily_spend(conn, DAY)

    assert spend == [
        cost.SensorSpend("infra-1", "infra"),
        cost.SensorSpend("mcp-1", "mcp", 1, 99_000, 0, 0, 1000, Decimal("0.00991")),
        # 3000 x 0.10 + 300 x 0.50 + 300 x 0.125 + 900 x 0.01 per million, standard price,
        # plus 120,000 x 0.50 + 1000 x 2.50 per million at the long-prompt price.
        cost.SensorSpend("support-1", "support", 3, 123_000, 1300, 300, 900, Decimal("0.0629965")),
    ]


def test_report_prints_each_sensor_and_the_total(
    grid: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'collector.db'}")

    assert cost.main([], today=date(2026, 10, 8)) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Sensor token spend for 2026-10-07 (UTC); prices checked 2026-10-08"
    assert lines[1].split() == [
        "sensor", "archetype", "turns", "input", "output", "cache_write", "cache_read", "usd"
    ]  # fmt: skip
    assert lines[2].split() == ["infra-1", "infra", "0", "0", "0", "0", "0", "0.000000"]
    assert lines[3].split() == ["mcp-1", "mcp", "1", "99000", "0", "0", "1000", "0.009910"]
    assert lines[4].split() == [
        "support-1", "support", "3", "123000", "1300", "300", "900", "0.062997"
    ]  # fmt: skip
    assert lines[5].split() == ["total", "4", "222000", "1300", "300", "1900", "0.072907"]


def test_report_for_a_given_date(
    grid: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'collector.db'}")

    assert cost.main(["--date", "2026-10-06"]) == 0

    out = capsys.readouterr().out
    assert "for 2026-10-06 (UTC)" in out
    assert out.splitlines()[-1].split() == ["total", "1", "5000", "5000", "0", "0", "0.003000"]


def test_unknown_model_fails_loudly(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _turn(engine, "support-1", datetime(2026, 10, 7, 1, 0, tzinfo=UTC), (1, 1, 0, 0), "claude-x")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'collector.db'}")

    assert cost.main(["--date", "2026-10-07"]) == 1
    assert "no price for model 'claude-x'" in capsys.readouterr().err


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert cost.main([]) == 2
