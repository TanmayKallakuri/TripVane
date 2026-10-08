# Week one without a payment card

How Tripvane goes live on free plans that need no payment card, and what that costs
compared with the server design in `docs/go-live.md`. That design stays the target; this
path is for the first week. Research and sources:
`/mnt/project-files/tripvane-golive/no-card-hosting.md` in the project files.

## 1. Decisions

1. **Week-one exception to the sensor egress rule (Phoenix, 2026-10-08 22:04Z).** CLAUDE.md
   requires sensor egress to be limited to the Anthropic API and the collector, enforced
   by the network (an internal Docker network whose only exit is a Squid allowlist, as in
   `infra/compose.support.yml`). Render's free plan has no such network and no outbound
   firewall, and its containers cannot set firewall rules. For week one the rule is
   enforced inside the sensor process instead:
   - `EGRESS_ALLOWED_HOSTS` turns on an audit hook (`tripvane_sensors.runtime.egress`)
     that refuses name lookups for any other host, and connections to any address those
     lookups did not return or to any port but 443. Python code cannot remove an audit
     hook once installed; native code in the process could bypass it, which the network
     rule would not allow. That is the accepted gap.
   - The decoy tools make no network calls under either design; the rule guards against
     a compromised sensor process.
   - **Expiry:** the exception ends on 2026-10-15 or when the sensors move to the servers in
     `docs/go-live.md`, whichever comes first. Extending it is a new decision.
   - What else differs from the compose deployment: no read-only root filesystem, no
     dropped capabilities, no DNS blackhole. The image still runs as a non-root user.
2. **Hosting.** No payment card needed for any of it.

   | Component | Host | Hostname |
   |---|---|---|
   | Lookup page and lookup API | Vercel Hobby project `tripvane-api` | `tripvane-api.vercel.app` |
   | Collector | Vercel Hobby project `tripvane-collector` | `tripvane-collector.vercel.app` |
   | Support chatbot sensor (`support-1`) | Render free web service `quillstone-support` | `quillstone-support.onrender.com` |
   | MCP server sensor (`mcp-1`) | Render free web service `quillstone-mcp` | `quillstone-mcp.onrender.com` |
   | Database | Supabase free project, session pooler | |

   If a hostname is taken, Vercel or Render picks another one. Then change
   `COLLECTOR_URL` and `EGRESS_ALLOWED_HOSTS` in `render.yaml`, and the from-disk address
   in `web/lookup.html` (followed by `make vercel-files`), to match.
3. **Not deployed in week one.** The infrastructure lookalike sensor needs raw ports on its
   own address, which no free platform offers. The GitHub sensor and the canary host stay
   deferred, as in `docs/go-live.md`.
4. **Token budgets** are unchanged (support 200000, mcp 100000), but they are counted in
   memory and Render restarts a free service whenever it wakes from idle. The real ceiling
   is the 50 USD monthly limit on the key in the Anthropic Console. Confirm it is set
   before the sensors start.
5. **Limits that come with the free plans:**
   - Render free services sleep after 15 idle minutes and take about a minute to wake;
     the first visitor sees Render's loading page.
   - The two services share 750 instance hours a month and 5 GB of bandwidth. Without
     a card, Render suspends them for the rest of the month when either runs out.
   - Events not yet shipped when a service sleeps are lost with its disk.
   - The API's daily limits count per Vercel instance.
   - Vercel's Hobby plan is for non-commercial use.
   - Platform subdomains share wildcard certificates, so the decoys do not appear in
     Certificate Transparency logs and draw little scanner traffic.

## 2. Accounts

| Account | How |
|---|---|
| Vercel, Hobby plan | Sign up with GitHub and choose Hobby, not the Pro trial. Install the Vercel GitHub app on `TanmayKallakuri/TripVane` only. |
| Render, Hobby workspace | Sign up with GitHub and install the Render GitHub app on the repository. If Render asks for a card, stop: Back4app Containers is the card-free fallback for one sensor. |
| Supabase, Anthropic | As in `docs/go-live.md` 2.1 and 2.2. A key that is not scoped to a workspace also needs `ANTHROPIC_CUSTOM_HEADERS=anthropic-workspace-id: <workspace id>` on each sensor (3.4); the Anthropic SDK reads that variable itself. |

## 3. Sequence

### 3.1 Settings and registration SQL

Run this from a checkout on the machine doing the deployment. It writes the secret values to
a file readable only by you, outside the repository, and prints the registration SQL.
Canaries are invalid credentials in realistic formats, so the SQL is not secret.

```
uv run --locked --all-packages python - <<'EOF'
import hashlib, os, secrets
from pathlib import Path
from tripvane_core.canary_formats import make_canary_secret

hmac_key = secrets.token_hex(32)
values = {"collector CANARY_HMAC_KEY": hmac_key}
sensors, canaries = [], []
for sensor_id, archetype in (("support-1", "support"), ("mcp-1", "mcp")):
    token = secrets.token_urlsafe(32)
    api_key = make_canary_secret("api_key", hmac_key)
    db_password = make_canary_secret("db_password", hmac_key)
    values |= {
        f"{sensor_id} COLLECTOR_TOKEN": token,
        f"{sensor_id} CANARY_API_KEY": api_key,
        f"{sensor_id} CANARY_DB_PASSWORD": db_password,
    }
    sensors.append(f"('{sensor_id}', '{sensor_id}', '{archetype}', "
                   f"'{hashlib.sha256(token.encode()).hexdigest()}')")
    canaries += [f"('{api_key}', 'sensor', '{sensor_id}', null)",
                 f"('{db_password}', 'sensor', '{sensor_id}', null)"]
path = Path.home() / "tripvane-week-one-values.txt"
path.touch(mode=0o600, exist_ok=False)
path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
print("insert into sensors (id, name, archetype, token_hash) values\n"
      + ",\n".join(sensors) + ";")
print("insert into canaries (token, owner_kind, owner_id, document_id) values\n"
      + ",\n".join(canaries) + ";")
print(f"secret values written to {path}")
EOF
```

Delete the values file once every value is in the Vercel and Render dashboards. Never
paste it into chat or project files.

### 3.2 Database

```
make migration-sql > schema.sql
```

Run `schema.sql` in the Supabase SQL editor (or through the Supabase connector), then
the registration SQL from 3.1. Check: `select version_num from alembic_version;`
returns `0004`, and `select id, archetype from sensors order by id;` lists `mcp-1` and
`support-1`. Turn the project's Data API off, because the tables have no row-level security.

### 3.3 Vercel: lookup API, page and collector

Follow `deploy/vercel/README.md`: two projects, Root Directory `deploy/vercel/api` and
`deploy/vercel/collector`. Set `DATABASE_URL` on both, `CLIENT_IP_HEADER=X-Real-IP` on the
API, and `CANARY_HMAC_KEY` from 3.1 on the collector. Check:

```
curl -sS https://tripvane-api.vercel.app/v1/lookup/ip/192.0.2.1       # "seen":false
curl -sS https://tripvane-collector.vercel.app/ingest \
  -H "Authorization: Bearer <support-1 COLLECTOR_TOKEN>" \
  -H 'Content-Type: application/json' -d '[]'                          # {"inserted":0,"duplicates":0}
```

Opening `https://tripvane-api.vercel.app/` shows the lookup page. As in `docs/go-live.md`
decision 8, do not announce the API or the page until a privacy notice is written.

### 3.4 Render: the two decoys

In Render, choose New, then Blueprint, select the repository, and apply `render.yaml`.
When prompted, enter each service's `COLLECTOR_TOKEN`, `CANARY_API_KEY` and
`CANARY_DB_PASSWORD` from 3.1, the Anthropic key as `ANTHROPIC_API_KEY`, and
`ANTHROPIC_CUSTOM_HEADERS` if the key needs it (section 2). Both services
build `infra/Dockerfile.sensor`; the first build takes several minutes.

### 3.5 Checks

Start every test message with `tripvane smoke test`, so test traffic is easy to exclude
when triaging.

```
curl -sS https://quillstone-support.onrender.com/chat -H 'Content-Type: application/json' \
  -d '{"conversation_id":"smoke-1","message":"tripvane smoke test: how do I reset my password?"}'
curl -sS -D - https://quillstone-mcp.onrender.com/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"tripvane-smoke-test","version":"0"}}}'
curl -si https://quillstone-support.onrender.com/health    # 404 from outside
```

What the replies mean, and the events query, are in `docs/go-live.md` 4.6. Also check the
logs of each Render service: there should be no lines containing `egress:`. Such a line
means the guard refused a destination, which is either a wrong `EGRESS_ALLOWED_HOSTS` or
something to investigate.

### 3.6 The client address on Render

Until `CLIENT_IP_HEADER` is set, the sensors record the address of Render's proxy rather
than the visitor's. Render does not document which header carries the client address and
cannot be spoofed, so test each candidate on `quillstone-support`:

1. Set `CLIENT_IP_HEADER=CF-Connecting-IP` in the service's environment (Render redeploys).
2. Send a chat that forges the header: add `-H 'CF-Connecting-IP: 192.0.2.12'` to the
   support check in 3.5.
3. Look at the recorded address. Once the shipper has sent the events, the newest row is
   this request's:

   ```sql
   select s.ip, s.last_seen from sources s
   join session_sources ss on ss.source_id = s.id
   join sessions se on se.id = ss.session_id
   where se.sensor_id = 'support-1' order by s.last_seen desc limit 3;
   ```

   - If it is your own public address, Render overwrote the forged header: keep the
     setting and set the same on `quillstone-mcp`.
   - If it is `192.0.2.12`, the header can be spoofed: try `True-Client-IP`, then remove
     the setting if neither candidate works, and record the gap.

### 3.7 Kill switches

- Set `DAILY_TOKEN_BUDGET=0` in a service's environment. Render redeploys it; inputs
  are still recorded, but the model is never called.
- Suspend a service from its Render settings page. This stops the decoy entirely.

## 4. Leaving week one

Once a card is acceptable, deploy `docs/go-live.md` as written. Then delete the Render
services and point nothing at the Vercel collector any more. Move the API to the core
server, or keep it on Vercel Pro.
