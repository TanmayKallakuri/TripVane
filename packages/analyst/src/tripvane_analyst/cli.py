"""tripvane-analyst: run one analyst step over what is pending, then exit.

Subcommands: gate, tag, novel, batch-submit, batch-collect, campaigns, and
`brief --days N`, which drafts the brief for the N full UTC days before today into
web/briefs/. Each reads DATABASE_URL; the model steps also read ANTHROPIC_API_KEY. The
taxonomy is taxonomy/tags.yaml in the checkout.
"""

import argparse
import logging
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, date, datetime

import anthropic
from sqlalchemy import Engine

from tripvane_analyst import batch, brief, campaign, gate, novel, tagger
from tripvane_analyst.common import UnusableResponse
from tripvane_core.config import Settings
from tripvane_core.db import make_engine
from tripvane_core.taxonomy import load_taxonomy

COMMANDS = ("gate", "tag", "novel", "batch-submit", "batch-collect", "campaigns")


def _positive(value: str) -> int:
    days = int(value)
    if days < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return days


def main(argv: Sequence[str] | None = None, *, today: date | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tripvane-analyst", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        commands.add_parser(name)
    brief_parser = commands.add_parser("brief", help="draft the brief into web/briefs/")
    brief_parser.add_argument("--days", type=_positive, default=7)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    settings = Settings.from_env()
    if settings.database_url is None:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    engine = make_engine(settings.database_url)
    counts: Counter[str]
    if args.command == "campaigns":
        counts = campaign.run(engine)
    else:
        if settings.anthropic_api_key is None:
            print("ANTHROPIC_API_KEY is not set", file=sys.stderr)
            return 2
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        if args.command == "brief":
            return _brief(engine, client, args.days, today or datetime.now(UTC).date())
        taxonomy = load_taxonomy()
        match args.command:
            case "gate":
                counts = gate.run(engine, client, taxonomy)
            case "tag":
                counts = tagger.run(engine, client, taxonomy)
            case "novel":
                counts = novel.run(engine, client, taxonomy)
            case "batch-submit":
                counts = batch.submit(engine, client, taxonomy)
            case _:
                counts = batch.collect(engine, client, taxonomy)
    summary = " ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    print(f"{args.command}: {summary or 'nothing pending'}")
    return 0


def _brief(engine: Engine, client: anthropic.Anthropic, days: int, today: date) -> int:
    try:
        result = brief.run(engine, client, days, today)
    except (FileExistsError, UnusableResponse, brief.UncitedNumbers) as exc:
        print(f"brief: not written: {exc}", file=sys.stderr)
        return 1
    except anthropic.APIError as exc:
        print(f"brief: not written: API error {type(exc).__name__}", file=sys.stderr)
        return 1
    print(
        f"brief: wrote {result.path} "
        f"input_tokens={result.input_tokens} output_tokens={result.output_tokens}"
    )
    return 0
