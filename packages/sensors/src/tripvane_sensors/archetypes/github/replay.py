"""Replay recorded webhook deliveries through the GitHub sensor with a scripted model.

Usage: python -m tripvane_sensors.archetypes.github.replay FIXTURES_DIR

Every FIXTURES_DIR/<event>.<action>.json is a recorded webhook body. It is signed with a
replay-only secret and posted to the sensor app, exactly as GitHub would deliver it. The
app's model client plays <name>.script.json, and for a pull request its diff source
returns <name>.diff instead of calling GitHub. The events the app emits are compared
with <name>.expected.jsonl, ignoring ts and session_id, as in runtime.replay.
"""

import difflib
import hashlib
import hmac
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from tripvane_core.config import Settings
from tripvane_sensors.archetypes.github.app import create_app
from tripvane_sensors.runtime.replay import (
    REPLAY_BUDGET,
    REPLAY_CANARIES,
    ListSink,
    MockModelClient,
    comparable,
)

# Signs the fixtures for this replay only; it is not a webhook secret anywhere.
REPLAY_WEBHOOK_SECRET = "replay-only-webhook-secret"
REPLAY_SENSOR_ID = "replay-github-1"
# A documentation address (RFC 5737) standing in for GitHub's webhook sender.
REPLAY_GITHUB_ADDRESS = "192.0.2.30"


class FileDiff:
    def __init__(self, path: Path) -> None:
        self.path = path

    def pull_request_diff(
        self, installation_id: int, repository: str, number: int, limit: int
    ) -> str:
        return self.path.read_text(encoding="utf-8")[:limit]


def replay_delivery(body_path: Path) -> list[str]:
    """Replay one recorded delivery. Returns the problems found; empty means it matches."""
    name = body_path.name.removesuffix(".json")
    event = name.split(".", 1)[0]
    client = MockModelClient(
        json.loads(body_path.with_name(f"{name}.script.json").read_text(encoding="utf-8"))
    )
    sink = ListSink()
    app = create_app(
        Settings(
            sensor_id=REPLAY_SENSOR_ID,
            github_webhook_secret=REPLAY_WEBHOOK_SECRET,
            daily_token_budget=REPLAY_BUDGET,
        ),
        client=client,
        sink=sink,
        canaries=REPLAY_CANARIES,
        diff_source=FileDiff(body_path.with_name(f"{name}.diff")),
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )
    body = body_path.read_bytes()
    signature = hmac.new(REPLAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    response = TestClient(app, client=(REPLAY_GITHUB_ADDRESS, 443)).post(
        "/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "GitHub-Hookshot/replay",
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": f"replay-{name.replace('.', '-').replace('_', '-')}",
            "X-Hub-Signature-256": f"sha256={signature}",
        },
    )
    if response.status_code != 202:
        return [f"{name}: webhook answered {response.status_code}, expected 202"]

    produced = [comparable(e.model_dump(mode="json")) for e in sink.events]
    expected_path = body_path.with_name(f"{name}.expected.jsonl")
    expected = [
        comparable(json.loads(line))
        for line in expected_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    problems = list(
        difflib.unified_diff(
            expected, produced, expected_path.name, f"{name} (replayed)", lineterm=""
        )
    )
    if client.messages.remaining:
        problems.append(f"{name}: {client.messages.remaining} scripted responses were not used")
    return problems


def delivery_files(fixtures_dir: Path) -> list[Path]:
    return sorted(
        path for path in fixtures_dir.glob("*.json") if not path.name.endswith(".script.json")
    )


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(
            "usage: python -m tripvane_sensors.archetypes.github.replay FIXTURES_DIR",
            file=sys.stderr,
        )
        return 2
    deliveries = delivery_files(Path(argv[0]))
    if not deliveries:
        print(f"replay: no deliveries in {argv[0]}", file=sys.stderr)
        return 1
    failed = 0
    for delivery in deliveries:
        problems = replay_delivery(delivery)
        print(f"{'FAIL' if problems else 'ok':4} {delivery.name}")
        for line in problems:
            print(f"     {line}")
        failed += bool(problems)
    print(f"replay: {len(deliveries) - failed} of {len(deliveries)} deliveries match")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
