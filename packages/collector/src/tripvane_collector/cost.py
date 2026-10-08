"""Daily token spend per sensor, summed from the model_turn events in the collector.

    python -m tripvane_collector.cost [--date YYYY-MM-DD]

prints one UTC day's tokens and USD spend per sensor and the total; the day defaults to
yesterday. Reads DATABASE_URL. Every registered sensor is listed, including those that
spent nothing (the infrastructure lookalikes never call a model).

Only sensor spend is covered: the analyst and the brief drafter call models directly and
record no model_turn events.
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import Connection, case, func, select

from tripvane_core.config import Settings
from tripvane_core.db import make_engine
from tripvane_core.models import Event, Sensor
from tripvane_core.prices import CHECKED_ON, LONG_PROMPT_TOKENS, UnknownModel, cost

USD = Decimal("0.000001")


@dataclass(frozen=True)
class SensorSpend:
    sensor_id: str
    archetype: str
    model_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    usd: Decimal = Decimal(0)


def daily_spend(conn: Connection, day: date) -> list[SensorSpend]:
    """Tokens and cost per sensor for the UTC day, every sensor included, by sensor id."""
    start = datetime.combine(day, time(), tzinfo=UTC)
    payload = Event.payload
    input_tokens = payload["input_tokens"].as_integer()
    cache_write = payload["cache_creation_input_tokens"].as_integer()
    cache_read = payload["cache_read_input_tokens"].as_integer()
    # One row per model turn; grouping the subquery's plain columns keeps Postgres from
    # comparing JSON path expressions between the select list and GROUP BY.
    turns = (
        select(
            Event.sensor_id.label("sensor_id"),
            payload["model"].as_string().label("model"),
            # Haiku 5.5's price depends on each request's prompt size.
            case((input_tokens + cache_write + cache_read > LONG_PROMPT_TOKENS, 1), else_=0).label(
                "long_prompt"
            ),
            input_tokens.label("input_tokens"),
            payload["output_tokens"].as_integer().label("output_tokens"),
            cache_write.label("cache_write"),
            cache_read.label("cache_read"),
        )
        .where(Event.type == "model_turn", Event.ts >= start, Event.ts < start + timedelta(1))
        .subquery()
    )
    rows = conn.execute(
        select(
            turns.c.sensor_id,
            turns.c.model,
            turns.c.long_prompt,
            func.count(),
            func.sum(turns.c.input_tokens),
            func.sum(turns.c.output_tokens),
            func.sum(turns.c.cache_write),
            func.sum(turns.c.cache_read),
        ).group_by(turns.c.sensor_id, turns.c.model, turns.c.long_prompt)
    ).all()
    spend = {
        sensor_id: SensorSpend(sensor_id, archetype)
        for sensor_id, archetype in conn.execute(select(Sensor.id, Sensor.archetype))
    }
    for sensor_id, model, is_long, turns, inp, out, written, read in rows:
        current = spend[sensor_id]
        spend[sensor_id] = SensorSpend(
            sensor_id,
            current.archetype,
            current.model_turns + turns,
            current.input_tokens + inp,
            current.output_tokens + out,
            current.cache_write_tokens + written,
            current.cache_read_tokens + read,
            current.usd
            + cost(
                model,
                input_tokens=inp,
                output_tokens=out,
                cache_write_tokens=written,
                cache_read_tokens=read,
                long_prompt=bool(is_long),
            ),
        )
    return [spend[sensor_id] for sensor_id in sorted(spend)]


def render(day: date, spend: Sequence[SensorSpend]) -> str:
    header = ("sensor", "archetype", "turns", "input", "output", "cache_write", "cache_read")
    rows = [
        (
            s.sensor_id,
            s.archetype,
            str(s.model_turns),
            str(s.input_tokens),
            str(s.output_tokens),
            str(s.cache_write_tokens),
            str(s.cache_read_tokens),
            f"{s.usd.quantize(USD, ROUND_HALF_UP)}",
        )
        for s in spend
    ]
    total = (
        "total",
        "",
        str(sum(s.model_turns for s in spend)),
        str(sum(s.input_tokens for s in spend)),
        str(sum(s.output_tokens for s in spend)),
        str(sum(s.cache_write_tokens for s in spend)),
        str(sum(s.cache_read_tokens for s in spend)),
        f"{sum((s.usd for s in spend), Decimal(0)).quantize(USD, ROUND_HALF_UP)}",
    )
    table = [(*header, "usd"), *rows, total]
    widths = [max(len(row[i]) for row in table) for i in range(len(header) + 1)]
    lines = [
        f"Sensor token spend for {day.isoformat()} (UTC); prices checked {CHECKED_ON.isoformat()}"
    ]
    for row in table:
        cells = [row[0].ljust(widths[0]), row[1].ljust(widths[1])]
        cells += [cell.rjust(width) for cell, width in zip(row[2:], widths[2:], strict=True)]
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, *, today: date | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cost-report", description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, help="UTC day (default: yesterday)")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    if settings.database_url is None:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    day = args.date or (today or datetime.now(UTC).date()) - timedelta(1)
    engine = make_engine(settings.database_url)
    try:
        with engine.connect() as conn:
            spend = daily_spend(conn, day)
    except UnknownModel as exc:
        print(f"cost-report: {exc.args[0]}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print(render(day, spend))
    return 0


if __name__ == "__main__":
    sys.exit(main())
