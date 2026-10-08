# Going live: the Tripvane runbook

This runbook takes the grid from "built and merged" to "live and operated for a week". Section 2 is what Phoenix sets up by hand; section 4 is the exact sequence a deployment session runs after that. Everything here was checked against the code on `main`; where an earlier review note says something different, the code wins and the difference is listed in the last section.

Placeholders used throughout:

| Placeholder | Meaning |
|---|---|
| `<decoy-domain>` | The `.com` in the decoys' story (the fictional Quillstone Software). Carries the sensor hostnames only |
| `<ops-domain>` | The plain, unrelated `.com`. Carries the lookup API, the collector and the canary host |
| `203.0.113.x` | Server addresses (documentation range; use the real ones) |
| `<...>` | Any other value you supply |

Never put a real credential in this file, in a commit, in an issue, in the project files or in a chat. Secrets live only in the cloud environment's variables, in the gitignored `.env.*` files of the deployment session's checkout (deleted when it ends), and on the servers.

---

## 1. Decisions

Working defaults, from the Coordinator's questions and the Domain and hosting options thread. Anything still open says so.

1. **The repository stays public (Phoenix's answer, 2026-10-08).** Anyone can read the decoy prompts, the fictional company and assistant names, the infrastructure lookalike responses and the canary formats, and recognise every decoy on sight. This is an accepted risk, not an open question. The servers fetch the code, including `infra/stage.sh` and `infra/user-data.sh`, from public `main` on first boot (section 3), so no GitHub token is needed.
2. **Domains: two `.com` domains at Cloudflare Registrar, all records DNS only.** Certificates for every hostname are published in Certificate Transparency logs, and the lookup API's own `/docs` page says "Tripvane lookup API". A Tripvane API on the decoy domain would therefore expose every sensor as a decoy. This runbook keeps the decoy domain for sensors only and puts the API with the collector and the canary host on the plain domain, which differs from the hosting thread's suggestion of API and sensors together on the decoy domain.

   | Role | Hostname | Server |
   |---|---|---|
   | Lookup API (`API_HOSTNAME`) | `api.<ops-domain>` | core |
   | Collector (`COLLECTOR_HOSTNAME`) | `collector.<ops-domain>` | core |
   | Canary URLs (`CANARY_HOSTNAME`) | `files.<ops-domain>` | core |
   | Support chatbot sensor | `help.<decoy-domain>` | support |
   | MCP server sensor | `mcp.<decoy-domain>` | mcp |
   | Infrastructure lookalike sensor | `llm.<decoy-domain>` | infra |
   | GitHub triage sensor (later) | `hooks.<decoy-domain>` | github |

   **Buy two domains, not three.** The canary host on `<ops-domain>` is a recorded choice for week one, not an oversight:
   - A canary URL on `<ops-domain>` can be tied to Tripvane twice over: through the API hostname in Certificate Transparency logs, and through passive DNS, because it shares the core server's address. A third domain alone fixes only the first, since the address would still be shared; the canary host on `<decoy-domain>` would be worse, because the shared address would then tie the decoy domain, and every sensor on it, to the Tripvane API.
   - The link only matters once a canary URL is planted in a document. Today's launch plants none: the decoys' canaries are fake credentials (4.1), which name no hostname.
   - Before the first document canary is planted, move the canary host to its own domain on its own server (about 10 USD a year and 5 USD a month). The code serves canary URLs from the API service, so that needs a canary-only compose and Caddy configuration, which does not exist yet.
3. **Hosting: Akamai Cloud (Linode); Vultr is the fallback.** Each sensor stack binds ports 80 and 443 (`infra/compose.*.yml`), so the code runs one sensor per server: today's scope needs **four servers**.

   | Server | Plan | Why | Monthly (as priced by the hosting thread) |
   |---|---|---|---|
   | core | 2 GB | builds two images and runs the API and the collector | 12 USD |
   | infra | 1 GB (Nanode) | one small Python service, Caddy, Squid; no model | 5 USD |
   | support | 1 GB (Nanode) | as infra, plus model calls | 5 USD |
   | mcp | 1 GB (Nanode) | as support | 5 USD |
   | **Total** | | | **27 USD** |

   Region: the one nearest the Supabase project (Newark, `us-east`, next to Supabase East US), since only the core server talks to the database.
4. **Launch scope today: core (API and collector), infra, support and mcp sensors, and the lookup page opened from disk.** The GitHub sensor and Vercel come later (section 7).
5. **Daily token budgets (`DAILY_TOKEN_BUDGET`): 200000 for support and 100000 for mcp**, the figures in the milestone 3 and 4 notes; the code itself has no default and refuses to start without a value. At the prices in `packages/core/src/tripvane_core/prices.py` that caps the two sensors near 0.15 USD a day even if every token were output. Raise them once the first days' spend is known.
6. **Anthropic spend limit: 50 USD a month on the key.** One key serves support, mcp and, from Phoenix's machine, the analyst. The analyst and the brief drafter have no budget and record no usage (section 8), so the Console limit is their only cap.
7. **Supabase: a new free-tier project, session pooler connection string.** The free plan pauses inactive projects and has no downloadable backups; the database is the only copy of what the grid captures, so move to a paid plan once the grid proves useful.
8. **The lookup page and the privacy notice.** The API answers, for any IP address, whether it talked to a decoy. Deploy it today, but do not announce the API or the page until a privacy notice is written and brief number one is published.
9. **The daily loop runs by hand** from a checkout on Phoenix's machine (section 6); nothing in the code schedules it.

Values the code leaves to configuration, with the defaults used here: `SENSOR_ID` is `<archetype>-1`; further servers of one archetype use a suffix (`support-2`, settings in `.env.support-2`); `make cost-report` reports yesterday (UTC) unless `DATE=YYYY-MM-DD` is given; `tripvane-analyst brief` covers 7 days unless `--days N` is given.

---

## 2. What Phoenix sets up

### 2.1 Anthropic

Set the key's monthly spend limit to 50 USD in the Console before handing it over. The key ends up in the support and mcp servers' settings and on Phoenix's machine for the analyst.

### 2.2 Supabase

1. Create a project in East US. Use a long database password of letters and digits only, so it needs no escaping inside a URL and no quoting in a shell.
2. Turn off the Data API in the project's API settings, or remove `public` from its exposed schemas. Tripvane never uses it, and the Alembic migrations create tables without row-level security, so with it on, anyone holding the project's anon key could read captured addresses and payloads.
3. Copy the **session pooler** connection string (port 5432) and add `?sslmode=require`:
   ```
   postgresql://postgres.<project-ref>:<db-password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
   ```
   Not the direct connection (`db.<project-ref>.supabase.co`), which Supabase serves over IPv6 only unless the paid IPv4 add-on is on, while the servers' compose networks are IPv4 only. Not the transaction pooler (port 6543): psycopg 3, which `tripvane_core.db.make_engine` uses, prepares statements after repeated use, which transaction pooling does not support.

### 2.3 Cloudflare

1. Register the two domains (decision 2) with Cloudflare Registrar.
2. Create an API token from the **Edit zone DNS** template, limited to those two zones, with an expiry date.
3. Do not create records by hand; the deployment creates them, all DNS only (grey cloud). Behind Cloudflare's proxy every recorded source address would be Cloudflare's.

### 2.4 Linode

1. Create the account (a new account can be held for manual review; Vultr is the fallback).
2. Create a personal access token with every scope at No Access except **Linodes: Read/Write**, with an expiry date.
3. Optional but useful: add your SSH public key to your Linode profile, so you can reach the servers yourself. The deployment session cannot (section 3), and you are the one who reads a server's logs if something fails.

### 2.5 The cloud environment

In [Project settings](#project-settings/environment), open the Cloud environment menu, choose Add cloud environment, and set:

| Variable | Value | Used for |
|---|---|---|
| `TRIPVANE_ANTHROPIC_API_KEY` | The Anthropic key | Written into the support and mcp servers as `ANTHROPIC_API_KEY` |
| `TRIPVANE_DATABASE_URL` | The session pooler string from 2.2 | Written into the core server as `DATABASE_URL` |
| `LINODE_TOKEN` | The Linode token | Creating the servers |
| `CLOUDFLARE_API_TOKEN` | The Cloudflare token | Creating the DNS records |

The `TRIPVANE_` prefix keeps Claude Code from taking the Anthropic key as its own login; the deployment renames the values to what the code reads.

Network access: Limited, with `api.linode.com` and `api.cloudflare.com` under Allowed domains and Allow package managers left ticked.

Save it. Variables reach only sessions started afterwards, so the deployment runs in a fresh session started once Phoenix confirms the environment is saved.

---

## 3. How the deployment works

The deployment session cannot open SSH or Postgres connections: its only way out is an HTTPS proxy (checked on 2026-10-08 in this project's built-in environment; the saved environment is expected to behave the same for those two, which section 4.0 checks). So:

- **Servers set themselves up.** `infra/user-data.sh NAME` prints cloud-init user data that carries the stack's `.env` and a first-boot script. On first boot the server fetches the repository at a fixed commit, installs Docker (`infra/bootstrap-droplet.sh`), stages its files with `infra/stage.sh` (the same staging `make deploy` uses, so the egress allowlist is identical), builds its own images, waits until its hostnames resolve, runs the migrations (core only) and starts the stack. It logs to `/var/log/tripvane-install.log`. The provider-specific part is one API call that creates a server with that user data (4.2).
- **The database work goes through the Supabase connector.** Migrations run on the core server, from the collector image, as `make deploy-core` does. Sensor registration and the decoys' canaries are SQL inserts the deployment session prepares locally and runs through the connector; collector tokens never leave the session in plain form, only their SHA-256.
- **Checks are public requests and SQL.** Each step is confirmed with `curl` against the public hostnames and with SQL through the connector.

---

## 4. The deployment sequence

Run in a fresh session, in a checkout of `main` that contains `infra/user-data.sh` (this runbook's pull request must be merged first). Do not print or echo any secret; the snippets below write them straight into files.

### 4.0 Preconditions

1. The variables of 2.5 are set: `for v in TRIPVANE_ANTHROPIC_API_KEY TRIPVANE_DATABASE_URL LINODE_TOKEN CLOUDFLARE_API_TOKEN; do [ -n "${!v:-}" ] && echo "$v set" || echo "$v MISSING"; done`
2. Both APIs answer: `curl -sS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $LINODE_TOKEN" https://api.linode.com/v4/profile` (expect 200) and `curl -sS -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" https://api.cloudflare.com/client/v4/user/tokens/verify` (expect `"status":"active"`).
3. The Supabase connector lists the project whose ref appears in `TRIPVANE_DATABASE_URL` (the user name is `postgres.<project-ref>`).
4. The commit the servers will fetch: `export REF=$(git rev-parse origin/main)`. Leave `REPO_URL` unset; the public repository URL is the default.

### 4.1 Settings files and registration SQL

From the repository root, with the two domains filled in. It writes `.env.core`, `.env.infra`, `.env.support` and `.env.mcp` (mode 600, gitignored) and a SQL file of the sensor rows and the decoys' canaries, and prints only the file names and the test canary URL:

```
export OPS_DOMAIN=<ops-domain> DECOY_DOMAIN=<decoy-domain> SQL_OUT=/tmp/tripvane-registration.sql
uv run --all-packages python - <<'EOF'
import hashlib
import os
import secrets
from pathlib import Path

from tripvane_core.canary_formats import make_canary_secret

OPS = os.environ["OPS_DOMAIN"]
DECOY = os.environ["DECOY_DOMAIN"]
SQL = Path(os.environ["SQL_OUT"])
HMAC_KEY = secrets.token_hex(32)
COLLECTOR_URL = f"https://collector.{OPS}"
SENSORS = {
    "infra": ("infra-1", f"llm.{DECOY}"),
    "support": ("support-1", f"help.{DECOY}"),
    "mcp": ("mcp-1", f"mcp.{DECOY}"),
}
BUDGETS = {"support": "200000", "mcp": "100000"}


def write_env(name: str, values: dict[str, str]) -> None:
    path = Path(f".env.{name}")
    path.touch(mode=0o600, exist_ok=False)
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))


def sql(value: str | None) -> str:
    return "null" if value is None else "'" + value.replace("'", "''") + "'"


write_env("core", {
    "API_HOSTNAME": f"api.{OPS}",
    "CANARY_HOSTNAME": f"files.{OPS}",
    "COLLECTOR_HOSTNAME": f"collector.{OPS}",
    "DATABASE_URL": os.environ["TRIPVANE_DATABASE_URL"],
    "CANARY_HMAC_KEY": HMAC_KEY,
    "CANARY_BASE_URL": f"https://files.{OPS}",
})
sensor_rows, canary_rows = [], []
for name, (sensor_id, hostname) in SENSORS.items():
    token = secrets.token_urlsafe(32)
    values = {
        "SENSOR_ID": sensor_id,
        "SENSOR_HOSTNAME": hostname,
        "COLLECTOR_URL": COLLECTOR_URL,
        "COLLECTOR_TOKEN": token,
    }
    if name in BUDGETS:
        api_key = make_canary_secret("api_key", HMAC_KEY)
        db_password = make_canary_secret("db_password", HMAC_KEY)
        values |= {
            "ANTHROPIC_API_KEY": os.environ["TRIPVANE_ANTHROPIC_API_KEY"],
            "DAILY_TOKEN_BUDGET": BUDGETS[name],
            "CANARY_API_KEY": api_key,
            "CANARY_DB_PASSWORD": db_password,
        }
        canary_rows += [(api_key, "sensor", sensor_id, None), (db_password, "sensor", sensor_id, None)]
    write_env(name, values)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    sensor_rows.append((sensor_id, sensor_id, name, token_hash))
test_canary = make_canary_secret("api_key", HMAC_KEY)
canary_rows.append((test_canary, "test", "smoke", "smoke-test-document"))

SQL.write_text(
    "insert into sensors (id, name, archetype, token_hash) values\n"
    + ",\n".join("(" + ", ".join(map(sql, row)) + ")" for row in sensor_rows)
    + ";\ninsert into canaries (token, owner_kind, owner_id, document_id) values\n"
    + ",\n".join("(" + ", ".join(map(sql, row)) + ")" for row in canary_rows)
    + ";\n"
)
print(f"wrote .env.core and .env.{{{','.join(SENSORS)}}}, and {SQL}")
print(f"test canary URL: https://files.{OPS}/c/{test_canary}")
EOF
for name in core infra support mcp; do infra/stage.sh "$name" "$(mktemp -d)" && echo "$name ok"; done
```

What it does, and why:

- `CANARY_HMAC_KEY` is generated once and goes only into `.env.core`; the collector uses it to recognise canaries in tool calls. The decoys' canaries are minted here with the same key, so the collector links a stolen canary to the sensor that leaked it. Canaries are invalid credentials in realistic formats, so the SQL file holding them is not secret.
- Each collector token goes into its sensor's file; the SQL holds only its SHA-256, which is what the collector compares (`tripvane_collector.auth.hash_token`).
- The last loop runs the same checks `make deploy` would and must print `ok` four times.

### 4.2 Creating a server (the one provider-specific call)

For a stack `NAME` (core, infra, support, mcp):

```
infra/user-data.sh NAME > /tmp/user-data-NAME.yaml
ROOT_PASS=$(openssl rand -base64 30)
jq -n --arg label "tv-NAME" --arg type "<plan>" --arg pass "$ROOT_PASS" \
      --arg ud "$(base64 -w0 < /tmp/user-data-NAME.yaml)" \
  '{label: $label, region: "us-east", type: $type, image: "linode/ubuntu24.04",
    root_pass: $pass, booted: true, metadata: {user_data: $ud}}' \
| curl -sS -X POST https://api.linode.com/v4/linode/instances \
    -H "Authorization: Bearer $LINODE_TOKEN" -H 'Content-Type: application/json' -d @- \
| jq '{id, label, status, ipv4}'
```

- Plans: `g6-standard-1` (2 GB) for core, `g6-nanode-1` (1 GB) for the sensors. Add `authorized_users: ["<Phoenix's Linode username>"]` to the body if Phoenix added an SSH key to the profile (2.4).
- The root password is random and discarded; the user data turns SSH password login off. Phoenix can reset it in Cloud Manager to use the Lish console.
- The user data holds every secret in that stack's `.env`, and Linode keeps it with the server. Delete `/tmp/user-data-*.yaml` once the server exists.
- Check the request against Linode's API reference on the first call (field names, plan ids, region id); this is the only provider-specific step, and on Vultr it is the equivalent create-instance call with the same user data.

### 4.3 Creating a DNS record

```
ZONE=$(curl -sS -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  "https://api.cloudflare.com/client/v4/zones?name=<domain>" | jq -r '.result[0].id')
curl -sS -X POST "https://api.cloudflare.com/client/v4/zones/$ZONE/dns_records" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"type":"A","name":"<hostname>","content":"<server ipv4>","ttl":1,"proxied":false}' \
| jq '{success, errors}'
```

`proxied` must be `false`. No AAAA records. Create the records as soon as the server's address is known: the server waits up to 30 minutes for its names to resolve before starting Caddy.

### 4.4 Core server

1. Create it (4.2, `NAME=core`, plan `g6-standard-1`).
2. Create `api`, `collector` and `files` A records on `<ops-domain>` (4.3).
3. Wait until it answers; first boot takes several minutes (Docker install and two image builds):
   ```
   until curl -fsS https://api.<ops-domain>/v1/lookup/ip/192.0.2.1; do sleep 30; done
   ```
   The answer has `"seen":false`.
4. Through the Supabase connector: `select version_num from alembic_version;` returns `0004`.
5. Further checks: `https://api.<ops-domain>/docs` answers 200; `curl -si https://files.<ops-domain>/c/x` and `curl -si https://api.<ops-domain>/c/x` are empty 404s; `curl -si https://collector.<ops-domain>/health` is a 404 from outside (it is checked inside the server).

### 4.5 Register the sensors and their canaries

Run the contents of `/tmp/tripvane-registration.sql` through the Supabase connector. Confirm:

```sql
select id, archetype from sensors order by id;
select owner_kind, owner_id, count(*) from canaries group by owner_kind, owner_id order by owner_id;
```

Three sensors; two canaries each for `mcp-1` and `support-1`, one for `smoke`. Then check one token against the collector: `curl -sS https://collector.<ops-domain>/ingest -H "Authorization: Bearer $(sed -n 's/^COLLECTOR_TOKEN=//p' .env.infra)" -H 'Content-Type: application/json' -d '[]'` answers `{"inserted":0,"duplicates":0}`.

### 4.6 Sensors, one at a time: infra, then support, then mcp

For each: create the server (4.2, plan `g6-nanode-1`), create its A record on `<decoy-domain>` (4.3), wait, then check. Do not start the next until events from this one are in the database:

```sql
select sensor_id, type, count(*) as events, max(ts) as latest
from events
where ts > now() - interval '1 hour'
group by sensor_id, type
order by sensor_id, type;
```

Mark test traffic: start every test message with `tripvane smoke test`, so it is easy to exclude when triaging.

**infra** (`llm.<decoy-domain>`):
- `until curl -fsS http://<infra ipv4>:11434/api/tags; do sleep 30; done` returns a model list (Ollama lookalike); `curl -sS https://llm.<decoy-domain>/v1/models` returns one too (LiteLLM lookalike).
- The events query shows `infra-1` with `session_started`, `input_received` and `tool_call_attempted`, and no `model_turn`: this sensor never calls a model.

**support** (`help.<decoy-domain>`):
```
curl -sS https://help.<decoy-domain>/chat -H 'Content-Type: application/json' \
  -d '{"conversation_id":"smoke-1","message":"tripvane smoke test: how do I reset my password?"}'
```
- The reply is an answer from the help desk assistant. The fixed replies mean something else: `Sorry, our assistant is busy right now...` is a spent budget, `Sorry, something went wrong on our side...` is a failed model call (check the key), and `Could you tell me a little more...` is the cheap gate refusing the input, which happens to a message repeated word for word, so vary the test text.
- The events query shows `support-1` with `model_turn` rows.

**mcp** (`mcp.<decoy-domain>`):
```
curl -sS -D - https://mcp.<decoy-domain>/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"tripvane-smoke-test","version":"0"}}}'
```
- HTTP 200 with an `mcp-session-id` header and a `result` naming the decoy server. The events query shows `mcp-1` with `input_received`.

If a server never answers, Phoenix reads `/var/log/tripvane-install.log` on it (over SSH with the profile key, or the Lish console) and `docker compose -p tripvane-NAME ps` and `logs` in `/opt/tripvane/NAME`. If events do not arrive, the sensor's log names the cause: `ingest attempt ... failed` or HTTP 401 means the collector URL or token is wrong; the events wait in the spool and are sent once it is fixed.

### 4.7 Canary URL test hit

`curl -si <the test canary URL printed in 4.1>` is an empty 404, and through the connector:

```sql
select h.id, h.ts, c.owner_kind, c.owner_id, h.secret is not null as credential_hit,
       h.source->>'ip' as ip
from canary_hits h left join canaries c on c.id = h.canary_id
order by h.id desc limit 20;
```

shows a row for `test` / `smoke`.

### 4.8 Lookup page

Set `const API_BASE_URL = "https://api.<ops-domain>";` in `web/lookup.html` (currently the placeholder `https://api.tripvane.example`) and land it on `main` through a pull request with CI green. The page works opened from disk (the API allows any origin); hosting it on Vercel is in section 7.

### 4.9 Wrap up

1. Delete `/tmp/user-data-*.yaml` and `/tmp/tripvane-registration.sql`, and the `.env.*` files in the session's checkout. The settings now live on the servers (`/opt/tripvane/NAME/.env`, mode 600) and in each server's Linode user data.
2. Tell Phoenix to copy `CANARY_HMAC_KEY` from the core server's `/opt/tripvane/core/.env` into a password manager. It is needed to mint more canaries, and changing it makes every existing canary unrecognisable.
3. Report each check's result.

At this point the plan's milestone 3, 4 and 7 "done when" conditions are met once the analyst has run once (6.2) and a lookup of an address that hit a sensor answers `seen: true` with tags.

---

## 5. Settings reference

All settings files are `KEY=value`, one per line, no quotes. On the servers they are `/opt/tripvane/NAME/.env`, mode 600. `TRUSTED_PROXY`, `SPOOL_DIR` and `HTTPS_PROXY` are set by the compose files, never in these files.

### 5.1 Core (`.env.core`)

| Variable | Read by | Notes |
|---|---|---|
| `API_HOSTNAME` | Caddy | `api.<ops-domain>` |
| `CANARY_HOSTNAME` | Caddy | `files.<ops-domain>`; serves only `/c/*`; must not name Tripvane |
| `COLLECTOR_HOSTNAME` | Caddy | `collector.<ops-domain>` |
| `DATABASE_URL` | API, collector, Alembic | The session pooler string; both services refuse to start without it |
| `CANARY_HMAC_KEY` | Collector (recognising canaries), canary minting | The collector refuses to start without it |
| `CANARY_BASE_URL` | Canary minting | Must be exactly `https://` plus `CANARY_HOSTNAME`; `infra/stage.sh` refuses anything else |

### 5.2 Every sensor (`.env.infra`, `.env.support`, `.env.mcp`)

| Variable | Notes |
|---|---|
| `SENSOR_ID` | Must match its `sensors` row (4.5) |
| `SENSOR_HOSTNAME` | Caddy's certificate name |
| `COLLECTOR_URL` | `https://collector.<ops-domain>`, no port: the egress proxy only tunnels to 443, and `infra/stage.sh` rejects anything else. The sensor posts to `COLLECTOR_URL/ingest` |
| `COLLECTOR_TOKEN` | The bearer token for this sensor id; the collector stores only its SHA-256 |

### 5.3 Model sensors (`.env.support`, `.env.mcp`), in addition

| Variable | Notes |
|---|---|
| `ANTHROPIC_API_KEY` | The key from 2.1 |
| `DAILY_TOKEN_BUDGET` | Non-negative integer; `0` stops all model calls (6.6). Counted in memory, reset at midnight UTC and on every restart |
| `CANARY_API_KEY`, `CANARY_DB_PASSWORD` | Planted in the decoy's prompt and in its fake `read_file` and `list_secrets` results. Only canaries minted with the core's `CANARY_HMAC_KEY` (4.1), never a value typed by hand and never a real credential |

### 5.4 GitHub sensor (later), in addition to 5.3

`GITHUB_WEBHOOK_SECRET`, `GITHUB_APP_ID` (the numeric App ID) and `GITHUB_APP_PRIVATE_KEY_B64` (the App's `.pem` base64-encoded on one line). See section 7.

---

## 6. The first week

Phoenix's machine needs a checkout, uv 0.11.x (`uv sync --locked --all-packages`), and `DATABASE_URL` and `ANTHROPIC_API_KEY` exported in the shell. Supabase accepts connections from anywhere, so the checkout reaches the same database as the core server.

### 6.1 Every morning: spend

```
make cost-report
```

Prints yesterday's (UTC) turns, tokens and USD per sensor and the total; `make cost-report DATE=YYYY-MM-DD` reports another day. It covers sensors only; the difference from the Console's usage for the key is what the analyst and the brief drafter spent.

### 6.2 Every morning: the analyst

```
uv run --all-packages tripvane-analyst gate
uv run --all-packages tripvane-analyst tag
uv run --all-packages tripvane-analyst novel
uv run --all-packages tripvane-analyst campaigns
```

In that order. Each processes only what is pending and prints counts (for example `gate: attacks=4 gated=6 not_attacks=2`); a second run prints `nothing pending`. Failures stay pending and are retried on the next run. Never run two copies of one step at once. The analyst reads `taxonomy/tags.yaml` from the checkout, so it runs from a checkout, not an installed package.

### 6.3 Every morning: triage

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

Also look at the canary hits query (4.7). For each day, note what was caught, what was misclassified (a benign message gated as an attack, or the reverse) and what looked wrong in a decoy's answers; that list is the input for brief number one and for "fix what the data shows was wrong".

Adding an accepted proposal to `taxonomy/tags.yaml` changes the taxonomy version, and any edit to that file, even a comment, re-gates and re-tags every payload through the Batch API: commit the change (pull request), then run `batch-submit`, and once the batch has ended `batch-collect`, `tag`, `novel` and `campaigns`. Collect proposals over the week and make one taxonomy change, not several.

### 6.4 End of week: brief number one

```
uv run --all-packages tripvane-analyst brief --days 7
```

It covers the 7 full UTC days before today and writes `web/briefs/<today>.md` with `DRAFT` as its first line. It never overwrites an existing file (delete the draft to redraft) and writes nothing if the model cites a number that is not in its context (`brief: not written: ...` names the numbers). On this first live run, also check that the answer was not cut off and that the prose reads right.

The DRAFT rule: only a person removes the `DRAFT` line, after checking every figure against the triage queries. Removing it and committing the file is the act of publishing; until then the file stays uncommitted on Phoenix's machine. Never commit a brief that still carries `DRAFT`, on any branch.

### 6.5 During the week: ten scrubbed sessions for `fixtures/`

The plan asks for ten real sessions, scrubbed, added to `fixtures/` so `make replay` reflects real traffic. The repository is public, so scrubbing is publishing.

Which sessions qualify: `make replay` runs each `fixtures/<name>.jsonl` through the decoy agent with the six standard decoy tools and `fixtures/system.md`, and needs exactly one `input_received` per file. Support sessions fit. An MCP agent run fits only if every tool it called is one of the six standard tools (inferred from `runtime/replay.py`, which loads `STANDARD_TOOLS`). GitHub sessions do not fit the GitHub replay, which needs the original webhook body; the database stores only the text.

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

### 6.6 Kill switches

| Situation | Action | Effect |
|---|---|---|
| A sensor spends too fast | On the server: set `DAILY_TOKEN_BUDGET=0` in `/opt/tripvane/NAME/.env`, then `cd /opt/tripvane/NAME && docker compose -p tripvane-NAME up -d --force-recreate` | Inputs are still recorded; the model is never called and the decoy answers with its busy line |
| Stop a sensor completely | On the server: `cd /opt/tripvane/NAME && docker compose -p tripvane-NAME down` (start again with `up -d`) | Nothing listens; unsent events stay in the spool volume |
| Stop everything from a browser | Power the server off in Linode Cloud Manager | As above; billing continues until the server is deleted |
| The Anthropic key leaks or runs away | Revoke it in the Console | Every model call fails; recording continues |
| All model spend | The key's spend limit | Caps sensors, analyst and brief together |
| Cut a sensor off from the collector | Through the connector: `update sensors set token_hash = md5(random()::text) \|\| md5(random()::text) where id = '<sensor id>';` | The collector answers 401; the sensor keeps events in its spool and retries. Reconnecting needs a new token in the server's `.env` and its hash in the row |
| A lookup API key is abused | `uv run --all-packages tripvane-api keys revoke <id>` | The key gets 401 |
| Take the public API down | On the core server: `cd /opt/tripvane/core && docker compose -p tripvane-core stop api` | Lookups and canary URLs stop; ingest continues |

"On the server" means over SSH with Phoenix's key, or the Lish console. The budget is counted in memory, so each restart of a sensor container (including the automatic restart after a crash) starts the day's count at zero; the key's spend limit is the backstop.

---

## 7. After the first launch

- **API keys for the feed**, from Phoenix's machine: `uv run --all-packages tripvane-api keys create "<who the key is for>"` prints `id=<n> key=tripvane_...` once; only the hash is stored. Check with `curl -sS https://api.<ops-domain>/v1/feed/recent -H 'X-Api-Key: <key>'`. Limits: 50 lookups per UTC day per client address without a key, 5,000 with one.
- **GitHub sensor.** Create a free GitHub organization in the Quillstone story (membership private), a public decoy repository in it (a small Python client library for Quillstone Ledger, issues enabled), and a GitHub App owned by the organization: webhook `https://hooks.<decoy-domain>/webhook`, secret from `openssl rand -hex 32`, SSL verification on; repository permissions Issues read-only, Pull requests read-only, Metadata read-only and nothing else; events Issues, Issue comment, Pull request; installable only on that account; installed on the decoy repository only. Then a fourth sensor server as in 4.6 with `.env.github` (5.4, `SENSOR_ID=github-1`, two minted canaries), its `sensors` row, and afterwards redeliver the App's first ping from its Advanced settings (expect 204) and open a test issue (expect 202 and a `github-1` session).
- **Vercel.** One project on the repository with root directory `web`, framework "Other", no build command, production branch `main`; the page is served at `/lookup.html`. `brief.py` writes drafts into `web/briefs/`, inside that root, so the DRAFT rule (6.4) also keeps drafts off the site. Do not deploy with the Vercel CLI from a working copy, which uploads uncommitted files.
- **Claude Code plugin.** With access to the repository: `/plugin marketplace add TanmayKallakuri/TripVane`, then `/plugin install tripvane@tripvane`, with `api_url` set to `https://api.<ops-domain>`. Users need uv.
- **Redeploying a changed sensor or core.** From a machine with Docker and SSH access to the server, `make deploy SENSOR=NAME HOST=<ip>` or `make deploy-core HOST=<ip>`, with the settings file copied back from the server. A new server from `infra/user-data.sh` at a newer `REF` is the alternative.

---

## 8. Known gaps that affect the first week

- **Source ASNs are not captured.** Nothing fills `sources.asn`, so campaigns group payloads by text alone, and the brief's "top sources by ASN" is one unknown bucket. Brief number one should say sources are not yet attributed to networks.
- **The analyst and the brief drafter record no token use.** They emit no `model_turn` events, so `make cost-report` shows sensor spend only, and the analyst has no daily budget. Read their spend from the Console; the key's spend limit is their only cap.
- **Canary URLs need their own unbranded hostname.** The code enforces it (`Caddyfile.core` serves `/c/*` only on `CANARY_HOSTNAME`), but the canary host shares a domain and the core server's address with the API (decision 2). Today's launch plants no document canaries; the decoys' credential canaries do not depend on a hostname. Before the first document canary, the canary host needs its own domain and server.
- **No scheduler.** The analyst, the cost report and the brief run only when someone runs them.
- **Counts held in memory.** Sensor budgets and the API's daily limits reset when their container restarts.
- **Sensor registration has no command.** 4.1 and 4.5 do it with SQL.
- **GitHub sessions cannot become replay fixtures** (6.5).

---

## Where the notes and the code differ

The code on `main` is what this runbook follows.

- The Alembic migrations live in `packages/collector` (`packages/collector/alembic.ini`), not in `packages/core`, although the table definitions are in `tripvane_core.models`.
- The milestone 3 and 4 notes suggest a hand-typed `tvk_live_` value for `CANARY_API_KEY`; that is superseded by minting (4.1), as the milestone 5 note already says. `tvk_live_` survives only in the replay fixtures.
- The milestone 5 note gives the GitHub App Contents read access; the code narrows its installation token to `pull_requests: read` and never reads contents, so the App does not need it.
- The milestone 3 to 5 notes say nothing deploys the collector or the API; milestone 7 added `infra/deploy-core.sh`, and this change adds `infra/user-data.sh` for servers that cannot be reached over SSH.
- The milestone 7 note and `packages/plugin/README.md` use `<github-org>/tripvane` for the plugin marketplace; the repository is `TanmayKallakuri/TripVane`.
- The milestone 3 to 5 notes say the smallest server size is enough; that holds for the sensors, but the core server builds two images and runs two services, so it gets 2 GB here.
