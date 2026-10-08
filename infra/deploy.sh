#!/usr/bin/env bash
# Build one sensor image locally, push it to a droplet over ssh, write its settings and
# restart its compose stack.
#
#   infra/deploy.sh SENSOR HOST        (or: make deploy SENSOR=support HOST=203.0.113.10)
#
# SENSOR is an archetype name (support, mcp, infra, github), optionally with a suffix for
# further droplets of the same archetype (support-2). Settings come from .env.SENSOR at the
# repository root, which is never committed; infra/stage.sh lists what it must set and
# checks it. The archetype's Caddyfile is infra/Caddyfile.ARCHETYPE when that file exists,
# otherwise infra/Caddyfile.
# The droplet needs Docker and compose (infra/bootstrap-droplet.sh) and ssh access as
# DEPLOY_USER (default root). Servers that cannot be reached over ssh are set up with
# infra/user-data.sh instead.
set -euo pipefail

usage() {
    echo "usage: $0 SENSOR HOST" >&2
    exit 2
}

[ $# -eq 2 ] || usage
SENSOR=$1
HOST=$2
[ -n "$SENSOR" ] && [ -n "$HOST" ] || usage
[ "$SENSOR" != core ] || { echo "deploy: use deploy-core.sh for core" >&2; exit 2; }

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DEPLOY_USER=${DEPLOY_USER:-root}
TARGET="$DEPLOY_USER@$HOST"
REMOTE_DIR="/opt/tripvane/$SENSOR"
ARCHETYPE=${SENSOR%%-*}
IMAGE="tripvane-sensor:$ARCHETYPE"

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
"$ROOT/infra/stage.sh" "$SENSOR" "$STAGE"

echo "deploy: building $IMAGE"
docker build --platform linux/amd64 -f "$ROOT/infra/Dockerfile.sensor" \
    --build-arg ARCHETYPE="$ARCHETYPE" -t "$IMAGE" "$ROOT"

echo "deploy: pushing $IMAGE to $HOST"
ssh -o BatchMode=yes "$TARGET" "mkdir -p '$REMOTE_DIR' && chmod 700 '$REMOTE_DIR'"
docker save "$IMAGE" | gzip | ssh -o BatchMode=yes "$TARGET" "gunzip | docker load"

echo "deploy: writing configuration to $REMOTE_DIR"
tar -C "$STAGE" -czf - compose.yml Caddyfile squid.conf egress-allowed-hosts.txt .env \
    | ssh -o BatchMode=yes "$TARGET" "tar -C '$REMOTE_DIR' -xzf - && chmod 600 '$REMOTE_DIR/.env'"

echo "deploy: restarting tripvane-$SENSOR"
ssh -o BatchMode=yes "$TARGET" \
    "cd '$REMOTE_DIR' && docker compose -p 'tripvane-$SENSOR' up -d --force-recreate --remove-orphans && docker compose -p 'tripvane-$SENSOR' ps"
