#!/usr/bin/env bash
# Build one sensor image locally, push it to a droplet over ssh, write its settings and
# restart its compose stack.
#
#   infra/deploy.sh SENSOR HOST        (or: make deploy SENSOR=support HOST=203.0.113.10)
#
# SENSOR is an archetype name (support), optionally with a suffix for further droplets of
# the same archetype (support-2). Settings come from .env.SENSOR at the repository root,
# which is never committed; it must set SENSOR_ID, SENSOR_HOSTNAME, COLLECTOR_URL (https,
# default port) and COLLECTOR_TOKEN, plus whatever the archetype needs (for support:
# ANTHROPIC_API_KEY, DAILY_TOKEN_BUDGET, CANARY_API_KEY, CANARY_DB_PASSWORD).
# The droplet needs Docker and compose (infra/bootstrap-droplet.sh) and ssh access as
# DEPLOY_USER (default root).
set -euo pipefail

usage() {
    echo "usage: $0 SENSOR HOST" >&2
    exit 2
}

[ $# -eq 2 ] || usage
SENSOR=$1
HOST=$2
[ -n "$SENSOR" ] && [ -n "$HOST" ] || usage

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DEPLOY_USER=${DEPLOY_USER:-root}
TARGET="$DEPLOY_USER@$HOST"
REMOTE_DIR="/opt/tripvane/$SENSOR"

if [[ ! "$SENSOR" =~ ^[a-z]+(-[a-z0-9]+)?$ ]]; then
    echo "deploy: SENSOR must look like 'support' or 'support-2'" >&2
    exit 2
fi
ARCHETYPE=${SENSOR%%-*}
COMPOSE_FILE="$ROOT/infra/compose.$ARCHETYPE.yml"
ENV_FILE="$ROOT/.env.$SENSOR"
IMAGE="tripvane-sensor:$ARCHETYPE"

[ -d "$ROOT/packages/sensors/src/tripvane_sensors/archetypes/$ARCHETYPE" ] || {
    echo "deploy: no archetype named $ARCHETYPE" >&2
    exit 2
}
[ -f "$COMPOSE_FILE" ] || { echo "deploy: missing $COMPOSE_FILE" >&2; exit 2; }
[ -f "$ENV_FILE" ] || { echo "deploy: missing $ENV_FILE" >&2; exit 2; }

# Read one KEY=value line from the env file without sourcing it.
env_value() {
    sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

for key in SENSOR_ID SENSOR_HOSTNAME COLLECTOR_URL COLLECTOR_TOKEN; do
    if [ -z "$(env_value "$key")" ]; then
        echo "deploy: $key is not set in $ENV_FILE" >&2
        exit 2
    fi
done

# The egress proxy only tunnels to port 443, so the collector must be plain https.
COLLECTOR_URL=$(env_value COLLECTOR_URL)
if [[ ! "$COLLECTOR_URL" =~ ^https://([A-Za-z0-9.-]+)(/.*)?$ ]]; then
    echo "deploy: COLLECTOR_URL must be https://host[/path] with no port" >&2
    exit 2
fi
COLLECTOR_HOST=${BASH_REMATCH[1]}

echo "deploy: building $IMAGE"
docker build --platform linux/amd64 -f "$ROOT/infra/Dockerfile.sensor" \
    --build-arg ARCHETYPE="$ARCHETYPE" -t "$IMAGE" "$ROOT"

echo "deploy: pushing $IMAGE to $HOST"
ssh -o BatchMode=yes "$TARGET" "mkdir -p '$REMOTE_DIR' && chmod 700 '$REMOTE_DIR'"
docker save "$IMAGE" | gzip | ssh -o BatchMode=yes "$TARGET" "gunzip | docker load"

echo "deploy: writing configuration to $REMOTE_DIR"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
cp "$COMPOSE_FILE" "$STAGE/compose.yml"
cp "$ROOT/infra/Caddyfile" "$ROOT/infra/squid.conf" "$STAGE/"
printf 'api.anthropic.com\n%s\n' "$COLLECTOR_HOST" > "$STAGE/egress-allowed-hosts.txt"
(umask 077 && cp "$ENV_FILE" "$STAGE/.env")
tar -C "$STAGE" -czf - compose.yml Caddyfile squid.conf egress-allowed-hosts.txt .env \
    | ssh -o BatchMode=yes "$TARGET" "tar -C '$REMOTE_DIR' -xzf - && chmod 600 '$REMOTE_DIR/.env'"

echo "deploy: restarting tripvane-$SENSOR"
ssh -o BatchMode=yes "$TARGET" \
    "cd '$REMOTE_DIR' && docker compose -p 'tripvane-$SENSOR' up -d --force-recreate --remove-orphans && docker compose -p 'tripvane-$SENSOR' ps"
