# Going live: the Tripvane runbook

This runbook takes the grid from "built and merged" to "live and operated for a week". Follow it top to bottom. Everything in it was checked against the code on `main`; where an earlier review note says something different, the code wins and the difference is listed in [Where the notes and the code differ](#where-the-notes-and-the-code-differ).

Placeholders used throughout:

| Placeholder | Meaning |
|---|---|
| `<brand-domain>` | The Tripvane domain (lookup API, collector, lookup page) |
| `<decoy-domain>` | A domain in the decoys' story (the fictional Quillstone Software), used only by sensors |
| `<canary-domain>` | A neutral domain used only for canary URLs |
| `203.0.113.x` | Droplet addresses (documentation range; use the real ones) |
| `<...>` | Any other value you supply |

Never put a real credential in this file, in a commit, in an issue or in a chat. Secrets live only in the gitignored `.env.*` files at the repository root and in your password manager.

---

## 1. Decisions only you can make

Each question has a recommended default. The rest of the runbook assumes the defaults; where a different answer changes a step, the step says so.

1. **Should the repository stay public?** It is public today. Anyone can read the decoy prompts, the fictional company and assistant names, the infrastructure lookalike responses and the canary formats, which lets an attacker recognise every decoy on sight. **Default: make it private before the first sensor goes live.** Cost: the Claude Code plugin marketplace (step 4.15) then installs only for people with access; a public plugin can move to its own repository later.
2. **Which domains and hostnames?** Certificates for every hostname are published in Certificate Transparency logs, and names that resolve to one address can be linked through passive DNS. Sensors must therefore never share a domain or an address with anything branded Tripvane. **Default: three domains.**

   | Role | Default hostname | Droplet |
   |---|---|---|
   | Lookup API (`API_HOSTNAME`) | `api.<brand-domain>` | core |
   | Collector (`COLLECTOR_HOSTNAME`) | `collector.<brand-domain>` | core |
   | Canary URLs (`CANARY_HOSTNAME`) | `files.<canary-domain>` | core |
   | Support chatbot sensor | `help.<decoy-domain>` | support |
   | MCP server sensor | `mcp.<decoy-domain>` | mcp |
   | Infrastructure lookalike sensor | `llm.<decoy-domain>` | infra |
   | GitHub triage sensor (webhook receiver) | `hooks.<decoy-domain>` | github |

   The canary host is served from the core droplet, so it shares an address with `api.<brand-domain>`. A canary host on `<decoy-domain>` would let anyone tie the decoy domain, and every sensor on it, to Tripvane. A separate inexpensive `<canary-domain>` limits that link to the canary host alone. Register `<decoy-domain>` and `<canary-domain>` with WHOIS privacy, and check trademarks before picking a Quillstone name (milestone 3 note).
3. **Which sensors first, and in what order?** **Default: core, then infra, then support, then mcp, then github, one at a time, confirming events reach the database before starting the next.** Infra calls no model, so it proves the ingest path at zero token cost; support then proves the model path and the budget.
4. **Region and droplet size?** **Default: DigitalOcean NYC3, Basic droplet with 1 GB of memory, Ubuntu 24.04, one droplet per row of the table above (five in all), and the Supabase project in the nearest region (East US).** Only the core droplet talks to the database, so it is the one whose region matters. Images are built on your machine, not on the droplet.
5. **Daily token budget per sensor (`DAILY_TOKEN_BUDGET`)?** **Default: 500000 for support, mcp and github; infra has none.** At the prices in `packages/core/src/tripvane_core/prices.py`, 500,000 Haiku tokens cost at most USD 0.25 even if every token were output, so the three model sensors are capped near USD 0.75 a day. The count includes cache reads and writes, and a decoy session is at most six turns of 400 output tokens each.
6. **Anthropic spend limit?** **Default: a dedicated Console workspace for Tripvane with a monthly spend limit of USD 50, and one API key per sensor plus one for the analyst.** The sensors are capped by their budgets; the analyst and the brief drafter have no budget and record no usage (see section 6), so the workspace limit is their only cap. Separate keys let you revoke one sensor without stopping the rest.
7. **Which GitHub identity owns the decoy repository and the GitHub App?** Your personal account also owns the Tripvane repository, so a decoy under it is linked to Tripvane. **Default: a new free GitHub organization in the Quillstone story, with your membership set to private, owning both the decoy repository and the App.**
8. **When does the lookup page become public, and with what privacy notice?** The API answers, for any IP address, whether it talked to a decoy (milestone 7 open point). **Default: deploy the API and the page in week one but do not announce either until a privacy notice is written and brief number one is published.**
9. **Supabase plan?** The database is the only copy of everything the grid captures. **Default: the Pro plan.** The free plan pauses inactive projects and offers no downloadable backups; confirm the current terms on Supabase's pricing page before choosing.
10. **Who runs the daily loop, and from where?** Nothing in the code schedules the analyst, the cost report or the brief. **Default: you run them by hand once a day from a checkout on your machine** (section 5).

Values the code leaves to configuration, with the defaults this runbook uses: `SENSOR_ID` is `<archetype>-1` (`support-1`, `mcp-1`, `infra-1`, `github-1`); further droplets of one archetype use a suffix (`support-2`, read from `.env.support-2`); `DEPLOY_USER` defaults to `root`; `make cost-report` reports yesterday (UTC) unless `DATE=YYYY-MM-DD` is given; `tripvane-analyst brief` covers 7 days unless `--days N` is given.

---

## 2. Accounts and resources to create

### 2.1 Your machine (the deploy machine)

- A clone of `https://github.com/TanmayKallakuri/TripVane` on `main`.
- uv 0.11.x (`pyproject.toml` requires `>=0.11,<0.12`), then `uv sync --locked --all-packages`.
- Docker that can build `linux/amd64` images (`infra/deploy.sh` and `infra/deploy-core.sh` run `docker build --platform linux/amd64`; on Apple Silicon this runs under emulation and is slower).
- `ssh`, `openssl`, `curl`, and `psql` or the Supabase SQL editor for the checks.
- An SSH key loaded in your agent. The deploy scripts use `ssh -o BatchMode=yes`, which cannot answer prompts, so connect to each new droplet once by hand (the bootstrap step does this) before deploying to it.

### 2.2 Anthropic

Purpose: the decoy agents (`claude-haiku-5-5`), the analyst gate, tagger and novel-tag step, and the brief drafter.

1. In the Console, create a workspace named for Tripvane and set its monthly spend limit (decision 6).
2. Create keys in that workspace, named after their user: `support-1`, `mcp-1`, `github-1`, `analyst`. The infra sensor needs none.
3. Store each key in your password manager. Each sensor key goes only into its own `.env.<sensor>`; the analyst key stays on your machine.

Least privilege: one key per consumer, so a leak or a runaway sensor is stopped by revoking one key.

### 2.3 Supabase (Postgres)

Purpose: the collector's database, read by the lookup API, the analyst, the brief drafter and the cost report.

1. Create a project in the region chosen in decision 4. Use a long database password made of letters and digits only, so it needs no escaping inside a URL.
2. Copy the **session pooler** connection string (Supavisor, port 5432). Its shape:
   ```
   postgresql://postgres.<project-ref>:<db-password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
   ```
   - Not the direct connection (`db.<project-ref>.supabase.co`): Supabase serves it over IPv6 unless the paid IPv4 add-on is enabled, and the compose networks on the droplets are IPv4 only.
   - Not the transaction pooler (port 6543): psycopg 3, which `tripvane_core.db.make_engine` uses, prepares statements after repeated use, which transaction pooling does not support.
   - `make_engine` accepts the plain `postgresql://` form and selects the psycopg driver itself. Append `?sslmode=require` so the connection fails rather than falling back to plain text.
3. Turn off Supabase's Data API for the project (in the project's API settings), or remove `public` from its exposed schemas. Tripvane never uses it, and the Alembic migrations create tables without row-level security, so with the Data API on, anyone holding the project's anon key could read captured source addresses and payloads. Afterwards, the Security Advisor should report no exposed tables.

Least privilege: Tripvane uses one `DATABASE_URL` for migrations and runtime, so it needs a role that can create tables. Keep that URL on the core droplet and your machine only; no sensor has it.

### 2.4 DigitalOcean

Purpose: one core droplet (lookup API, collector, canary URLs) and one droplet per sensor.

1. Add your SSH public key to the account.
2. Create the five droplets from decision 4, each with that SSH key and no password.
3. Create a Cloud Firewall per role. Inbound rules:

   | Droplets | Inbound |
   |---|---|
   | core, support, mcp, github | TCP 22 from your address only; TCP 80 and 443, UDP 443 from anywhere |
   | infra | the above, plus TCP 3000, 4000, 7860, 8080 and 11434 from anywhere |

   Leave outbound open: each droplet needs it for packages, Docker images and certificates. A sensor's own outbound traffic is restricted inside compose, where the sensor container has no route out except a Squid proxy that only tunnels to `api.anthropic.com` (not for infra), `api.github.com` (github only) and the collector host (`infra/compose.*.yml`, `infra/squid.conf`).

### 2.5 DNS

Purpose: Caddy obtains a certificate for each hostname on first start, which needs the A record to resolve to the right droplet and ports 80 and 443 to be open.

- One A record per hostname in decision 2, pointing at its droplet. Create them before deploying the droplet behind them.
- DNS only: no CDN or proxy (for example, Cloudflare's proxy must be off). Behind a proxy every recorded source address would be the proxy's, and Caddy's certificate request can fail.
- No AAAA records, unless you enable IPv6 on the droplet.

### 2.6 GitHub: decoy repository, App and webhook secret

Purpose: the GitHub triage sensor receives issue, comment and pull request events from a public decoy repository and reads pull request diffs. It never writes to GitHub.

1. Create the organization from decision 7 and, in it, a public repository for the decoy: a small Python client library for Quillstone Ledger, with a README and a little code so issues there look natural. Issues must be enabled.
2. Generate the webhook secret: `openssl rand -hex 32`.
3. Create a GitHub App owned by that organization:
   - Homepage URL: anything in the decoy story.
   - Webhook: active; URL `https://hooks.<decoy-domain>/webhook`; secret from step 2; SSL verification on.
   - Repository permissions: **Issues: read-only**, **Pull requests: read-only**, Metadata: read-only (mandatory). Nothing else, and no write permission of any kind.
   - Subscribe to events: **Issues**, **Issue comment**, **Pull request**.
   - Where can this App be installed: **Only on this account.** The sensor accepts any correctly signed delivery and does not pin a repository.
4. Note the **App ID** (the number, not the client ID) and generate a **private key** (a `.pem` download).
5. Install the App on the decoy repository only.

GitHub sends a ping when the App is created. It fails until the sensor is deployed; step 4.11 redelivers it.

### 2.7 Vercel

Purpose: hosts the static lookup page, `web/lookup.html`, which calls the lookup API from the browser.

- One project connected to the Tripvane repository (Git integration), root directory `web`, framework preset "Other", no build command, production branch `main`. The page is served at `/lookup.html`.
- `brief.py` writes drafts to `web/briefs/`, inside that root. Never commit a brief that still carries its `DRAFT` line, on any branch: a pushed branch becomes a deployment. Do not deploy with the Vercel CLI from your working copy either, because it uploads uncommitted files, drafts included.

---

## 3. The environment files

All live at the repository root and are gitignored (`.env` and `.env.*` in `.gitignore`). One `KEY=value` per line, no spaces around `=`, no quotes needed. `deploy.sh` and `deploy-core.sh` copy the file to the droplet as `/opt/tripvane/<name>/.env` with mode 600.

Do not set `TRUSTED_PROXY`, `SPOOL_DIR` or `HTTPS_PROXY` in these files; the compose files set them.

### 3.1 `.env.ops`: your machine only

Not read by any deploy script. Load it into a shell with `set -a; . ./.env.ops; set +a` before the commands in sections 4 and 5.

```
DATABASE_URL=postgresql://postgres.<project-ref>:<db-password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
ANTHROPIC_API_KEY=<the analyst key>
CANARY_HMAC_KEY=<output of: openssl rand -hex 32>
CANARY_BASE_URL=https://files.<canary-domain>
```

| Variable | Read by | Notes |
|---|---|---|
| `DATABASE_URL` | Alembic, sensor registration, canary minting, `tripvane-api keys`, `tripvane-analyst`, `make cost-report` | Section 2.3 |
| `ANTHROPIC_API_KEY` | `tripvane-analyst` (every subcommand except `campaigns`) | The analyst key |
| `CANARY_HMAC_KEY` | Canary minting | Generate once; the same value goes in `.env.core`. Changing it later makes every existing canary unrecognisable |
| `CANARY_BASE_URL` | Canary minting | Must equal `https://` plus `CANARY_HOSTNAME` |

### 3.2 `.env.core`: the core droplet (lookup API and collector)

```
API_HOSTNAME=api.<brand-domain>
CANARY_HOSTNAME=files.<canary-domain>
COLLECTOR_HOSTNAME=collector.<brand-domain>
DATABASE_URL=<same as .env.ops>
CANARY_HMAC_KEY=<same as .env.ops>
CANARY_BASE_URL=https://files.<canary-domain>
```

`deploy-core.sh` refuses to run unless all six are set and `CANARY_BASE_URL` is exactly `https://` plus `CANARY_HOSTNAME`. The collector refuses to start without `CANARY_HMAC_KEY`; both services refuse to start without `DATABASE_URL`. The three hostnames are read by Caddy (`infra/Caddyfile.core`).

### 3.3 `.env.infra`

```
SENSOR_ID=infra-1
SENSOR_HOSTNAME=llm.<decoy-domain>
COLLECTOR_URL=https://collector.<brand-domain>
COLLECTOR_TOKEN=<printed for infra-1 in step 4.3>
```

| Variable | Notes |
|---|---|
| `SENSOR_ID` | Must match the `sensors` row registered in step 4.3 |
| `SENSOR_HOSTNAME` | Caddy's certificate name (`infra/Caddyfile.infra`) |
| `COLLECTOR_URL` | `https://host` with no port: the egress proxy only tunnels to port 443, and `deploy.sh` rejects anything else. The sensor posts to `COLLECTOR_URL/ingest` |
| `COLLECTOR_TOKEN` | The bearer token for this sensor id; the collector stores only its SHA-256 |

### 3.4 `.env.support` and `.env.mcp`

The four variables of 3.3 (with `SENSOR_ID=support-1`, `SENSOR_HOSTNAME=help.<decoy-domain>`, or `mcp-1` and `mcp.<decoy-domain>`), plus:

```
ANTHROPIC_API_KEY=<the support-1 or mcp-1 key>
DAILY_TOKEN_BUDGET=500000
CANARY_API_KEY=<minted api_key canary for this sensor, step 4.4>
CANARY_DB_PASSWORD=<minted db_password canary for this sensor, step 4.4>
```

| Variable | Notes |
|---|---|
| `ANTHROPIC_API_KEY` | That sensor's own key |
| `DAILY_TOKEN_BUDGET` | A non-negative integer; `0` stops all model calls (section 5.6). Kept in memory and reset at midnight UTC and on every restart |
| `CANARY_API_KEY`, `CANARY_DB_PASSWORD` | Planted in the decoy's prompt and in its fake `read_file` and `list_secrets` results. Use only canaries minted in step 4.4, never a value typed by hand and never a real credential: the minted values are invalid credentials whose checksum lets the collector recognise and attribute them when they come back in a tool call |

### 3.5 `.env.github`

Everything in 3.4 (with `SENSOR_ID=github-1`, `SENSOR_HOSTNAME=hooks.<decoy-domain>` and the `github-1` key and canaries), plus:

```
GITHUB_WEBHOOK_SECRET=<the secret from section 2.6>
GITHUB_APP_ID=<the App ID>
GITHUB_APP_PRIVATE_KEY_B64=<the .pem file base64-encoded on one line>
```

Encode the key with `base64 -w0 <app>.private-key.pem` on Linux, or `base64 -i <app>.private-key.pem | tr -d '\n'` on macOS. The sensor parses it at startup, so a bad value fails the deploy rather than the first pull request. Delete the `.pem` download once it is in your password manager.

---

## 4. Bringing the grid up, in order

Run everything from the repository root with `.env.ops` loaded (`set -a; . ./.env.ops; set +a`). Each step ends with how to confirm it worked; do not continue past a failed confirmation.

Your own test traffic is recorded like anyone else's. Start every test message with `tripvane smoke test` and note your public address, so both are easy to exclude when you triage.

### 4.1 Database migrations

```
uv run --all-packages alembic -c packages/collector/alembic.ini upgrade head
uv run --all-packages alembic -c packages/collector/alembic.ini current
```

Confirm: `current` prints `0004 (head)`. `make deploy-core` runs the same upgrade again from the collector image, which is then a no-op.

### 4.2 Shared secret

If `.env.ops` does not yet hold `CANARY_HMAC_KEY`, generate it with `openssl rand -hex 32`, put the same value in `.env.ops` and `.env.core`, and store it in your password manager.

### 4.3 Register the sensors and issue collector tokens

There is no command for this; the collector only reads the `sensors` table. This inserts one row per sensor and prints each token once:

```
uv run --all-packages python - <<'EOF'
import secrets

from sqlalchemy import insert

from tripvane_collector.auth import hash_token
from tripvane_core.config import Settings
from tripvane_core.db import make_engine
from tripvane_core.models import Sensor

SENSORS = {"infra-1": "infra", "support-1": "support", "mcp-1": "mcp", "github-1": "github"}

engine = make_engine(Settings.from_env().database_url)
with engine.begin() as conn:
    for sensor_id, archetype in SENSORS.items():
        token = secrets.token_urlsafe(32)
        conn.execute(
            insert(Sensor).values(
                id=sensor_id, name=sensor_id, archetype=archetype, token_hash=hash_token(token)
            )
        )
        print(f"{sensor_id}: COLLECTOR_TOKEN={token}")
EOF
```

Copy each token into its `.env.<sensor>` straight away; only the hash is stored. The `archetype` column feeds the brief's sensor coverage figures.

Confirm: `select id, archetype from sensors;` lists the four sensors.

### 4.4 Mint the decoys' canary credentials

```
uv run --all-packages python - <<'EOF'
from tripvane_api.canary.mint import mint_canary
from tripvane_core.config import Settings
from tripvane_core.db import make_engine

engine = make_engine(Settings.from_env().database_url)
with engine.begin() as conn:
    for sensor_id in ("support-1", "mcp-1", "github-1"):
        api_key = mint_canary(conn, "sensor", sensor_id, None, kind="api_key").token
        db_password = mint_canary(conn, "sensor", sensor_id, None, kind="db_password").token
        print(f"{sensor_id}: CANARY_API_KEY={api_key} CANARY_DB_PASSWORD={db_password}")
EOF
```

Copy each pair into its `.env.<sensor>`. These values are invalid credentials in realistic formats; each is also a `canaries` row, so when one comes back in a tool call the collector records a `canary_hits` row linked to the sensor that leaked it.

Confirm: `select owner_id, count(*) from canaries group by owner_id;` shows 2 for each of the three sensors.

### 4.5 Core droplet: lookup API, collector, canary host

1. DNS: the three core A records point at the core droplet (section 2.5).
2. Bootstrap (installs Docker and compose; safe to repeat):
   ```
   ssh root@203.0.113.20 'bash -s' < infra/bootstrap-droplet.sh
   ```
3. Deploy (builds both images, ships them over ssh, writes `/opt/tripvane/core/`, runs migrations, starts the stack):
   ```
   make deploy-core HOST=203.0.113.20
   ```

Confirm:

- The command ends with `docker compose ps`; `api` and `collector` become `healthy` within about a minute. Check again with `ssh root@203.0.113.20 'cd /opt/tripvane/core && docker compose -p tripvane-core ps'`.
- `curl -sS https://api.<brand-domain>/v1/lookup/ip/192.0.2.1` answers with `"seen":false`.
- `https://api.<brand-domain>/docs` loads in a browser.
- `curl -si https://files.<canary-domain>/c/x` is a 404 with an empty body, and so is `curl -si https://api.<brand-domain>/c/x`.
- `curl -si https://collector.<brand-domain>/health` is a 404 from outside (the healthcheck runs inside the droplet).
- The collector accepts a sensor token: `curl -sS https://collector.<brand-domain>/ingest -H "Authorization: Bearer <infra-1 token>" -H 'Content-Type: application/json' -d '[]'` answers `{"inserted":0,"duplicates":0}`, and with a wrong token answers 401 `unknown sensor`.

If Caddy cannot get a certificate, `ssh root@203.0.113.20 'cd /opt/tripvane/core && docker compose -p tripvane-core logs caddy'` says why; it is almost always DNS or a closed port 80.

### 4.6 Sensor deploy, common to every sensor

For each sensor, in the order of decision 3:

1. DNS: its A record points at its droplet.
2. Bootstrap: `ssh root@<ip> 'bash -s' < infra/bootstrap-droplet.sh`.
3. Deploy: `make deploy SENSOR=<sensor> HOST=<ip>`. This builds `tripvane-sensor:<archetype>` with `infra/Dockerfile.sensor`, ships it, writes `/opt/tripvane/<sensor>/` (compose file, Caddyfile, `squid.conf`, the egress allowlist and `.env`) and starts compose project `tripvane-<sensor>`.
4. Confirm the stack: `ssh root@<ip> 'cd /opt/tripvane/<sensor> && docker compose -p tripvane-<sensor> ps'` shows `sensor` healthy and `egress` and `caddy` running.
5. Confirm events arrive, in the Supabase SQL editor or with `psql "$DATABASE_URL"`, after sending the sensor's test request:
   ```sql
   select sensor_id, type, count(*) as events, max(ts) as latest
   from events
   where ts > now() - interval '1 hour'
   group by sensor_id, type
   order by sensor_id, type;
   ```
6. If nothing arrives, read the sensor's log: `ssh root@<ip> 'cd /opt/tripvane/<sensor> && docker compose -p tripvane-<sensor> logs --tail 50 sensor'`. `ingest attempt ... failed` or `HTTP 401` means the collector URL or token is wrong; the events wait in the spool and are sent once it is fixed. Batches the collector can never accept are moved to `rejected-*.jsonl` in the spool: `ssh root@<ip> 'cd /opt/tripvane/<sensor> && docker compose -p tripvane-<sensor> exec sensor ls -l /var/spool/tripvane'`.

### 4.7 Infra sensor

`make deploy SENSOR=infra HOST=203.0.113.31`, then:

- `curl -sS http://203.0.113.31:11434/api/tags` returns a model list (Ollama lookalike).
- `curl -sS https://llm.<decoy-domain>/v1/models` returns a model list (LiteLLM lookalike).
- The events query shows `infra-1` with `session_started`, `input_received` and `tool_call_attempted`, and no `model_turn`: this sensor never calls a model.

### 4.8 Support sensor

`make deploy SENSOR=support HOST=203.0.113.32`, then open `https://help.<decoy-domain>/` and send `tripvane smoke test: how do I reset my password?`, or:

```
curl -sS https://help.<decoy-domain>/chat -H 'Content-Type: application/json' \
  -d '{"conversation_id":"smoke-1","message":"tripvane smoke test: how do I reset my password?"}'
```

Confirm: the reply is an answer from the help desk assistant. The fixed replies mean something else: `Sorry, our assistant is busy right now...` is a spent budget, `Sorry, something went wrong on our side...` is a failed model call (check the key), and `Could you tell me a little more...` is the cheap gate refusing the input, which happens to a message repeated word for word, so vary the test text. The events query shows `support-1` with `model_turn` rows. On those first live model turns, check `cache_read_input_tokens` (milestone 3 note): the prompt may be below the model's minimum cacheable length, which only affects cost.

### 4.9 MCP sensor

`make deploy SENSOR=mcp HOST=203.0.113.33`, then:

```
curl -sS -D - https://mcp.<decoy-domain>/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"tripvane-smoke-test","version":"0"}}}'
```

Confirm: HTTP 200 with an `mcp-session-id` header and a `result` naming the decoy server. To list the tools, send `{"jsonrpc":"2.0","id":2,"method":"tools/list"}` the same way with `-H 'Mcp-Session-Id: <that id>' -H 'MCP-Protocol-Version: 2025-06-18'`. The events query shows `mcp-1` with `input_received`.

### 4.10 Canary URL test hit

With the core deployed, mint a document canary and request it:

```
uv run --all-packages python - <<'EOF'
from tripvane_api.canary.mint import mint_canary
from tripvane_core.config import Settings
from tripvane_core.db import make_engine

with make_engine(Settings.from_env().database_url).begin() as conn:
    print(mint_canary(conn, "test", "smoke", "smoke-test-document").url)
EOF
curl -si <the printed URL>
```

Confirm: an empty 404, and a new row from:

```sql
select h.id, h.ts, c.owner_kind, c.owner_id, h.secret is not null as credential_hit,
       h.source->>'ip' as ip
from canary_hits h left join canaries c on c.id = h.canary_id
order by h.id desc limit 20;
```

### 4.11 GitHub sensor and webhook

1. `make deploy SENSOR=github HOST=203.0.113.34`.
2. In the App's settings, under Advanced, open the failed ping from section 2.6 and choose Redeliver. Confirm it is answered **204** (a verified ping is acknowledged and not recorded). A 401 means `GITHUB_WEBHOOK_SECRET` does not match the App's secret.
3. Open a test issue on the decoy repository, titled `tripvane smoke test`. Confirm the delivery is answered **202** and the events query shows `github-1` with `input_received` and `model_turn`.
4. Open a test pull request. Confirm its `input_received` text contains the diff: `select payload->>'raw_text' from events where sensor_id = 'github-1' and type = 'input_received' order by id desc limit 1;`. If the diff is missing, the sensor log names the failed fetch; check the App ID, the private key and the installation.

There is nothing to register on GitHub beyond the App: the App's webhook is the registration.

### 4.12 API keys for the feed

```
uv run --all-packages tripvane-api keys create "<who the key is for>"
```

It prints `id=<n> key=tripvane_...` once; only the hash is stored. Confirm:

```
curl -sS https://api.<brand-domain>/v1/feed/recent -H 'X-Api-Key: <key>'
```

answers `{"payloads":[...]}` (empty until the analyst has run), and the same request without the header is refused. Revoke a key with `uv run --all-packages tripvane-api keys revoke <id>`. Limits: 50 lookups per UTC day per client address without a key, 5,000 with a key.

### 4.13 Lookup page

1. In `web/lookup.html`, set `const API_BASE_URL = "https://api.<brand-domain>";` (currently the placeholder `https://api.tripvane.example`) and land it on `main` through a pull request with CI green.
2. Vercel deploys `main` (section 2.7).

Confirm: `https://<vercel host>/lookup.html` looks up `192.0.2.1` and shows "not seen on the grid", and an address that hit a sensor shows its record once the analyst has run.

### 4.14 Where things stand

At this point every sensor is recording, events reach Supabase, canary hits are recorded, and the API and page answer. The plan's milestone 3 to 7 "done when" conditions are met once the analyst has run once (section 5.2) and a lookup of an address that hit a sensor answers `seen: true` with tags.

### 4.15 Claude Code plugin (optional in week one)

The marketplace is the repository root. With access to the repository (decision 1), in Claude Code: `/plugin marketplace add TanmayKallakuri/TripVane`, then `/plugin install tripvane@tripvane`, and give `api_url` as `https://api.<brand-domain>` and optionally an API key. Users need uv. `packages/plugin/README.md` still shows `<github-org>/tripvane`; the line above is the real path.

---

## 5. The first week

### 5.1 Every morning: spend

```
make cost-report
```

Prints yesterday's (UTC) turns, tokens and USD per sensor and the total; `make cost-report DATE=YYYY-MM-DD` reports another day. It covers sensors only (section 6). Compare the total with the Console's usage for the workspace: the difference is what the analyst and the brief drafter spent.

### 5.2 Every morning: the analyst

From the checkout, in this order:

```
uv run --all-packages tripvane-analyst gate
uv run --all-packages tripvane-analyst tag
uv run --all-packages tripvane-analyst novel
uv run --all-packages tripvane-analyst campaigns
```

Each processes only what is pending and prints counts (for example `gate: attacks=4 gated=6 not_attacks=2`); a second run prints `nothing pending`. Failures stay pending and are retried on the next run. Never run two copies of one step at once. The analyst reads `taxonomy/tags.yaml` from the checkout, so it must run from a checkout, not an installed package.

### 5.3 Every morning: triage

Attack payloads seen in the last day, with their tags under the current verdict:

```sql
select p.id, p.last_seen, p.seen_count, p.campaign_id,
       string_agg(t.axis || ':' || t.name, ', ' order by t.axis, t.name) as tags,
       left(p.normalized_text, 160) as excerpt
from payloads p
left join payload_tags pt on pt.payload_id = p.id and pt.taxonomy_version = p.gate_version
left join tags t on t.id = pt.tag_id
where p.is_attack and p.last_seen > now() - interval '1 day'
group by p.id
order by p.last_seen desc;
```

What is still waiting for the analyst:

```sql
select count(*) filter (where gate_version is null) as ungated,
       count(*) filter (where is_attack) as attacks,
       count(*) filter (where is_attack is false) as not_attacks
from payloads;
```

New tag proposals from the novel step:

```sql
select tp.id, tp.payload_id, tp.name, tp.description
from tag_proposals tp
join payloads p on p.id = tp.payload_id and p.gate_version = tp.taxonomy_version
order by tp.id;
```

Also look at the canary hits query (4.10) and each sensor's `docker compose ps`. For each day, note what was caught, what was misclassified (a benign message gated as an attack, or the reverse) and what looked wrong in a decoy's answers; that list is the input for brief number one and for "fix what the data shows was wrong".

Adding an accepted proposal to `taxonomy/tags.yaml` changes the taxonomy version, and any edit to that file, even a comment, re-gates and re-tags every payload through the Batch API: commit the change (pull request), then run `batch-submit`, and once the batch has ended `batch-collect`, `tag`, `novel` and `campaigns`. Collect proposals over the week and make one taxonomy change, not several.

### 5.4 End of week: brief number one

```
uv run --all-packages tripvane-analyst brief --days 7
```

It covers the 7 full UTC days before today and writes `web/briefs/<today>.md` with `DRAFT` as its first line. It never overwrites an existing file (delete the draft to redraft) and writes nothing if the model cites a number that is not in its context (`brief: not written: ...` names the numbers). On this first live run, also check that the answer was not cut off and that the prose reads right (milestone 8 open point).

The DRAFT rule: only a person removes the `DRAFT` line, after checking every figure against the triage queries. Removing it and committing the file to `main` is the act of publishing, because Vercel serves `web/`. Until then, the file stays uncommitted on your machine.

### 5.5 During the week: ten scrubbed sessions for `fixtures/`

The plan asks for ten real sessions, scrubbed, added to `fixtures/` so `make replay` reflects real traffic. The repository is public (decision 1), so treat scrubbing as publishing.

Which sessions qualify: `make replay` runs each `fixtures/<name>.jsonl` through the decoy agent with the six standard decoy tools and `fixtures/system.md`, and needs exactly one `input_received` per file. Support sessions fit. An MCP agent run fits only if every tool it called is one of the six standard tools (inferred from `runtime/replay.py`, which loads `STANDARD_TOOLS`). GitHub sessions do not fit the GitHub replay, which needs the original webhook body, and the database stores only the text.

Candidates:

```sql
select s.id, s.sensor_id, s.started_at,
       count(e.id) filter (where e.type = 'tool_call_attempted') as tool_calls
from sessions s join events e on e.session_id = s.id
where s.sensor_id = 'support-1'
group by s.id
having count(e.id) filter (where e.type = 'input_received') = 1
order by tool_calls desc, s.started_at desc
limit 30;
```

One session's events, in order (each `payload` is the full event JSON):

```sql
select payload from events where session_id = '<session id>' order by event_seq;
```

For each chosen session, following the shape of the existing fixtures and `fixtures/README.md`:

1. `<name>.jsonl`: the `session_started` and `input_received` events.
2. `<name>.script.json`: one entry per `model_turn`, with `content` holding a `text` block of `assistant_text` (when it is not empty) followed by a `tool_use` block (`name`, `input`) for each `tool_call_attempted` that follows that turn, plus `stop_reason` and `usage` with the four token counts.
3. `<name>.expected.jsonl`: the events after `input_received`, as recorded, with `model` set to `claude-haiku-5-5`.
4. Scrub every file: source addresses to documentation ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24); account handles, names, emails and domains to invented values under `.example`; any credential an attacker pasted to an obviously fake value; every minted canary to the replay values in `runtime/replay.py` (`REPLAY_CANARIES`), never a live one; `sensor_id` to `replay-1`. Where `raw_text` changes, recompute `payload_hash` with `tripvane_core.hashing.payload_hash`.
5. Update `fixtures/README.md`, which currently says none of the fixtures were captured from a real source.
6. Run `make test` and `make replay`; a mismatch prints the difference. Land the files through a pull request.

### 5.6 Kill switches

| Situation | Action | Effect |
|---|---|---|
| A sensor spends too fast | Set `DAILY_TOKEN_BUDGET=0` in its `.env.<sensor>` and `make deploy` again | Inputs are still recorded; the model is never called and the decoy answers with its busy line |
| Stop a sensor completely | `ssh root@<ip> 'cd /opt/tripvane/<sensor> && docker compose -p tripvane-<sensor> down'` (start again with `up -d`) | Nothing listens; unsent events stay in the spool volume |
| A sensor key leaks or runs away | Revoke that key in the Console | That sensor's model calls fail; recording continues |
| All model spend | The workspace spend limit | Caps sensors, analyst and brief together |
| Cut a sensor off from the collector | `update sensors set token_hash = md5(random()::text) \|\| md5(random()::text) where id = '<sensor id>';` | The collector answers 401; the sensor keeps events in its spool and retries. To reconnect it, run the 4.3 snippet for that one sensor with `update(Sensor).where(Sensor.id == ...)` in place of `insert`, put the new token in `.env.<sensor>` and deploy |
| A lookup API key is abused | `uv run --all-packages tripvane-api keys revoke <id>` | The key gets 401 |
| Take the public API down | `ssh root@<core-ip> 'cd /opt/tripvane/core && docker compose -p tripvane-core stop api'` | Lookups and canary URLs stop; ingest continues |
| GitHub deliveries must stop | Deactivate the App's webhook, or suspend the installation | No deliveries reach the sensor |

Edit `.env.<sensor>` on your machine, not on the droplet: every deploy overwrites the droplet's copy. The budget is counted in memory, so each restart of a sensor container (including the automatic restart after a crash) starts the day's count at zero; the workspace spend limit is the backstop.

---

## 6. Known gaps that affect the first week

- **Source ASNs are not captured.** Nothing fills `sources.asn`, so campaigns group payloads by text alone, and the brief's "top sources by ASN" is one unknown bucket. In brief number one, state that sources are not attributed to networks yet rather than reading anything into that figure.
- **The analyst and the brief drafter record no token use.** They call models directly and emit no `model_turn` events, so `make cost-report` shows sensor spend only, and the analyst has no daily budget. Read their spend from the Console (5.1); the workspace spend limit is their only cap.
- **Canary URLs need their own unbranded hostname.** The code enforces it (`Caddyfile.core` serves `/c/*` only on `CANARY_HOSTNAME`), but the canary host still shares the core droplet's address with the API, which is why decision 2 puts it on its own `<canary-domain>`. Separating the address would need a code change. Week one plants no document canaries unless you choose to, so the decoys' credential canaries (4.4) are the canaries that matter.
- **No scheduler.** The analyst, the cost report and the brief run only when you run them (decision 10).
- **Counts held in memory.** Sensor budgets and the API's daily limits reset when their container restarts.
- **Sensor registration has no command.** Use the snippet in 4.3.
- **GitHub sessions cannot become replay fixtures** (5.5).

---

## Where the notes and the code differ

The code on `main` is what this runbook follows.

- The Alembic migrations live in `packages/collector` (`packages/collector/alembic.ini`), not in `packages/core`, although the table definitions are in `tripvane_core.models`.
- The milestone 3 and 4 notes suggest a hand-typed `tvk_live_` value for `CANARY_API_KEY`; that is superseded by minting (4.4), as the milestone 5 note already says. `tvk_live_` survives only in the replay fixtures.
- The milestone 5 note gives the GitHub App Contents read access; the code narrows its installation token to `pull_requests: read` and never reads contents, so the App does not need it.
- The milestone 3 to 5 notes say nothing deploys the collector or the API; milestone 7 added `infra/deploy-core.sh` and `make deploy-core`, which this runbook uses.
- The milestone 7 note and `packages/plugin/README.md` use `<github-org>/tripvane` for the plugin marketplace; the repository is `TanmayKallakuri/TripVane`.
- The milestone 3 to 5 notes say the smallest droplet size is enough; this runbook recommends 1 GB as a judgement for headroom (Python, Caddy and Squid together), not because the code requires it.
