# Tripvane

Deception grid for AI agents. Decoy agents record attempted actions; they never execute them.

## Non-negotiable rules

- Decoy tools NEVER perform real side effects. They log the call and return a plausible fake result.
- Sensors have no real outbound capability except: the Anthropic API and the collector ingest endpoint. Enforce this in compose with egress rules.
- Canary secrets must be invalid credentials in realistic formats. Never use real keys anywhere in fixtures or code.
- Never attack back, never probe sources, never store more than: source IP, ASN, a fixed subset of headers, account handle, payload, agent transcript.
- Every sensor has a daily token budget. The model is called only after the cheap gate passes.
- Nothing is published automatically. Briefs carry a DRAFT marker until a human removes it.

## Conventions

- Python 3.12, uv workspaces, ruff, pytest. Type hints everywhere. No new dependencies without a one-line reason in the PR description.
- One package per concern (see layout in README). Cross-package imports only through `packages/core`.
- Database changes go through Alembic migrations. Never edit a migration that has been applied.
- Models: `claude-haiku-5-5` in sensors, `claude-sonnet-5-5` in the tagger, `claude-opus-5-5` only in `analyst/novel.py` and `analyst/brief.py`.
- Prompts live in `*.md` files next to the code that uses them, loaded at import time. No prompts inline in Python.
- Tests: every package has `tests/`. Sensor logic is tested with a mocked model client and `fixtures/` sessions. No network in tests.
- Keep it simple. No abstractions for things that exist once. No plugin systems, no config frameworks, no dependency injection containers.

## Layout

```
packages/core        shared event models, taxonomy loader, payload hashing, config, prices
packages/sensors     decoy agent runtime + archetypes (support, mcp, infra, github)
packages/collector   ingest API + database schema + migrations
packages/analyst     Haiku gate, Sonnet tagger, Batch reprocessing, campaigns, brief drafter
packages/api         public lookup API, canary endpoints, rate limiting, API keys
packages/plugin      Claude Code plugin: MCP server that queries the lookup API
web/                 static landing + lookup page + briefs
infra/               Dockerfiles, compose files, deploy.sh, droplet bootstrap
fixtures/            recorded sessions for replay tests
taxonomy/            tags.yaml
```

## Commands

- `make test` runs all tests.
- `make replay` replays `fixtures/` through the sensor runtime with a mocked model and diffs against expected events.
- `make deploy SENSOR=support HOST=1.2.3.4` builds, pushes, and restarts one sensor.
- `make cost-report` prints yesterday's token spend per sensor from the collector.

## Workflow

- Read the task prompt fully. Implement only what it specifies. Do not add features, options, or extras.
- Run `make test` and `make replay` before declaring done.
- If a decision is genuinely open, stop and ask. Do not guess on anything touching the rules above.
