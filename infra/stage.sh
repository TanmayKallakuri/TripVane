#!/usr/bin/env bash
# Check one stack's settings and write the files its server needs into DIR. deploy.sh,
# deploy-core.sh and user-data.sh all stage through this script, so they agree on the
# checks and on the sensor's egress allowlist.
#
#   infra/stage.sh NAME DIR
#
# NAME is core (settings from .env.core) or a sensor: an archetype name (support, mcp,
# infra, github), optionally with a suffix for further servers of the same archetype
# (support-2), with settings from .env.NAME. DIR receives compose.yml, Caddyfile and .env
# (mode 600), plus squid.conf and egress-allowed-hosts.txt for a sensor.
#
# Core settings must include API_HOSTNAME, CANARY_HOSTNAME and COLLECTOR_HOSTNAME (each
# with a DNS A record pointing at the core server; CANARY_HOSTNAME must not name Tripvane,
# because canary URLs are planted in decoy documents), DATABASE_URL, CANARY_HMAC_KEY and
# CANARY_BASE_URL (https://CANARY_HOSTNAME). Sensor settings must include SENSOR_ID,
# SENSOR_HOSTNAME, COLLECTOR_URL (https, default port) and COLLECTOR_TOKEN, plus whatever
# the archetype needs (support and mcp: ANTHROPIC_API_KEY, DAILY_TOKEN_BUDGET,
# CANARY_API_KEY, CANARY_DB_PASSWORD; github: those plus GITHUB_WEBHOOK_SECRET,
# GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_B64; infra: nothing more).
set -euo pipefail

[ $# -eq 2 ] && [ -n "$1" ] && [ -n "$2" ] || { echo "usage: $0 NAME DIR" >&2; exit 2; }
NAME=$1
DIR=$2

ROOT=$(cd "$(dirname "$0")/.." && pwd)
ENV_FILE="$ROOT/.env.$NAME"

if [[ ! "$NAME" =~ ^[a-z]+(-[a-z0-9]+)?$ ]]; then
    echo "stage: NAME must be core or look like 'support' or 'support-2'" >&2
    exit 2
fi
[ -f "$ENV_FILE" ] || { echo "stage: missing $ENV_FILE" >&2; exit 2; }
[ -d "$DIR" ] || { echo "stage: missing directory $DIR" >&2; exit 2; }

# Read one KEY=value line from the env file without sourcing it.
env_value() {
    sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

require() {
    for key in "$@"; do
        if [ -z "$(env_value "$key")" ]; then
            echo "stage: $key is not set in $ENV_FILE" >&2
            exit 2
        fi
    done
}

if [ "$NAME" = core ]; then
    require API_HOSTNAME CANARY_HOSTNAME COLLECTOR_HOSTNAME DATABASE_URL CANARY_HMAC_KEY \
        CANARY_BASE_URL
    if [ "$(env_value CANARY_BASE_URL)" != "https://$(env_value CANARY_HOSTNAME)" ]; then
        echo "stage: CANARY_BASE_URL must be https://CANARY_HOSTNAME" >&2
        exit 2
    fi
    cp "$ROOT/infra/compose.core.yml" "$DIR/compose.yml"
    cp "$ROOT/infra/Caddyfile.core" "$DIR/Caddyfile"
    (umask 077 && cp "$ENV_FILE" "$DIR/.env")
    exit 0
fi

ARCHETYPE=${NAME%%-*}
COMPOSE_FILE="$ROOT/infra/compose.$ARCHETYPE.yml"
CADDYFILE="$ROOT/infra/Caddyfile.$ARCHETYPE"
[ -f "$CADDYFILE" ] || CADDYFILE="$ROOT/infra/Caddyfile"

[ -d "$ROOT/packages/sensors/src/tripvane_sensors/archetypes/$ARCHETYPE" ] || {
    echo "stage: no archetype named $ARCHETYPE" >&2
    exit 2
}
[ -f "$COMPOSE_FILE" ] || { echo "stage: missing $COMPOSE_FILE" >&2; exit 2; }

require SENSOR_ID SENSOR_HOSTNAME COLLECTOR_URL COLLECTOR_TOKEN

# The egress proxy only tunnels to port 443, so the collector must be plain https.
COLLECTOR_URL=$(env_value COLLECTOR_URL)
if [[ ! "$COLLECTOR_URL" =~ ^https://([A-Za-z0-9.-]+)(/.*)?$ ]]; then
    echo "stage: COLLECTOR_URL must be https://host[/path] with no port" >&2
    exit 2
fi
COLLECTOR_HOST=${BASH_REMATCH[1]}
# The egress proxy's allowlist: the collector, plus the Anthropic API for archetypes that
# call a model. The infrastructure lookalikes never do; the GitHub sensor also reads pull
# request diffs from api.github.com.
if [ "$ARCHETYPE" = infra ]; then
    EGRESS_HOSTS="$COLLECTOR_HOST"
elif [ "$ARCHETYPE" = github ]; then
    EGRESS_HOSTS="api.anthropic.com
api.github.com
$COLLECTOR_HOST"
else
    EGRESS_HOSTS="api.anthropic.com
$COLLECTOR_HOST"
fi

cp "$COMPOSE_FILE" "$DIR/compose.yml"
cp "$CADDYFILE" "$DIR/Caddyfile"
cp "$ROOT/infra/squid.conf" "$DIR/"
printf '%s\n' "$EGRESS_HOSTS" > "$DIR/egress-allowed-hosts.txt"
(umask 077 && cp "$ENV_FILE" "$DIR/.env")
