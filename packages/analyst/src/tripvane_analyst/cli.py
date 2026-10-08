"""tripvane-analyst: run one analyst step over what is pending, then exit.

Subcommands: gate, tag, novel, batch-submit, batch-collect, campaigns. Each reads
DATABASE_URL; the model steps also read ANTHROPIC_API_KEY. The taxonomy is
taxonomy/tags.yaml in the checkout.
"""

import argparse
import logging
import sys
from collections import Counter
from collections.abc import Sequence

import anthropic

from tripvane_analyst import batch, campaign, gate, novel, tagger
from tripvane_core.config import Settings
from tripvane_core.db import make_engine
from tripvane_core.taxonomy import load_taxonomy

COMMANDS = ("gate", "tag", "novel", "batch-submit", "batch-collect", "campaigns")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tripvane-analyst", description=__doc__)
    parser.add_argument("command", choices=COMMANDS)
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
