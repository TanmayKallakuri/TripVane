"""tripvane-api keys create LABEL | keys revoke ID. Reads DATABASE_URL."""

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from tripvane_api.keys import create_api_key, revoke_api_key
from tripvane_core.config import Settings
from tripvane_core.db import make_engine


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tripvane-api")
    commands = parser.add_subparsers(dest="command", required=True)
    keys = commands.add_parser("keys", help="manage lookup API keys")
    actions = keys.add_subparsers(dest="action", required=True)
    create = actions.add_parser("create", help="create a key and print it once")
    create.add_argument("label", help="who the key is for")
    revoke = actions.add_parser("revoke", help="revoke a key by id")
    revoke.add_argument("id", type=int)
    args = parser.parse_args(argv)

    database_url = Settings.from_env().database_url
    if database_url is None:
        print("tripvane-api: DATABASE_URL is not set", file=sys.stderr)
        return 2
    engine = make_engine(database_url)
    try:
        with engine.begin() as conn:
            if args.action == "create":
                key_id, key = create_api_key(conn, args.label)
                print(f"id={key_id} key={key}")
                return 0
            if revoke_api_key(conn, args.id, datetime.now(UTC)):
                print(f"revoked id={args.id}")
                return 0
            print(f"tripvane-api: no active key with id {args.id}", file=sys.stderr)
            return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
