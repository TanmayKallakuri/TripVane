# Vercel projects for the site, the lookup API and the collector

Week-one hosting that needs no payment card: three Vercel Hobby projects built from this
repository, with the database on Supabase. The sensors are not deployed from here.

| Project | Root Directory | Serves |
|---|---|---|
| `tripvane-api` | `deploy/vercel/api` | the lookup page at `/` (from `public/`) and the lookup API under `/v1/` |
| `tripvane-collector` | `deploy/vercel/collector` | sensor ingest at `/ingest` |
| `tripvane-site` | `deploy/vercel/site` | the public site at `/`, a static page with no build step |

`requirements.txt` in each folder is pinned from `uv.lock` and installs `packages/core` and
the service package from this repository; `public/index.html` is a copy of
`web/lookup.html`. Both come from `make vercel-files`, and `make test` fails while they are
out of date. Served from its own host, the lookup page calls that host; opened from disk,
it calls `https://tripvane-api.vercel.app`.

`site/index.html` is a copy of `web/index.html`, also written by `make vercel-files`. Its
lookup form calls `API_BASE_URL`, set near the top of the page's script to
`https://tripvane-api.vercel.app`; change it there if the API moves to another host.

## Database

Generate the schema without a database connection and apply it once, in the Supabase SQL
editor or through the Supabase connector:

```
make migration-sql > schema.sql
```

The output also creates `alembic_version` at the latest revision, so later migrations can
run with `alembic upgrade head` as usual.

## Creating the projects

For the `tripvane-api` and `tripvane-collector` rows: Add New, Project, import
`TanmayKallakuri/TripVane`, set Root Directory to the folder, leave the framework preset as detected (FastAPI), keep "Include
files outside the root directory in the Build Step" on (the requirements install
`../../../packages`), and add the environment variables below before the first deploy.

| Variable | Project | Value |
|---|---|---|
| `DATABASE_URL` | api and collector | Supabase session pooler string, port 5432 |
| `CLIENT_IP_HEADER` | api | `X-Real-IP` |
| `CANARY_HMAC_KEY` | collector | the same key the canaries were minted with |

Do not set `TRUSTED_PROXY` on Vercel; the API refuses to start with both.

For `tripvane-site`: import the same repository, set Root Directory to `deploy/vercel/site`,
choose the framework preset Other, and leave the build command and output directory empty.
It needs no environment variables. To serve it on your own domain, add the domain under the
project's Settings, Domains, and create the DNS record Vercel shows there: an `A` record
for the apex domain or a `CNAME` record for `www`.

## Checks after deploying

- `https://tripvane-api.vercel.app/` shows the lookup page, and a lookup of any address
  answers `"seen": false` until the sensors ship events.
- `https://tripvane-collector.vercel.app/ingest` without a token answers 401.
- The site's address shows the site, and its lookup form answers `not seen on the grid`
  for any address once the API is up.
