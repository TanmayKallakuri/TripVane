#!/usr/bin/env bash
# Build the lookup API and collector images locally, push them to the core droplet over
# ssh, write its settings, apply the database migrations and restart the stack.
#
#   infra/deploy-core.sh HOST          (or: make deploy-core HOST=203.0.113.20)
#
# Settings come from .env.core at the repository root, which is never committed;
# infra/stage.sh lists what it must set and checks it.
# The droplet needs Docker and compose (infra/bootstrap-droplet.sh) and ssh access as
# DEPLOY_USER (default root). Servers that cannot be reached over ssh are set up with
# infra/user-data.sh instead.
set -euo pipefail

[ $# -eq 1 ] && [ -n "$1" ] || { echo "usage: $0 HOST" >&2; exit 2; }
HOST=$1

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DEPLOY_USER=${DEPLOY_USER:-root}
TARGET="$DEPLOY_USER@$HOST"
REMOTE_DIR=/opt/tripvane/core

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
"$ROOT/infra/stage.sh" core "$STAGE"

for service in api collector; do
    echo "deploy-core: building tripvane-service:$service"
    docker build --platform linux/amd64 -f "$ROOT/infra/Dockerfile.service" \
        --build-arg SERVICE="$service" -t "tripvane-service:$service" "$ROOT"
done

echo "deploy-core: pushing images to $HOST"
ssh -o BatchMode=yes "$TARGET" "mkdir -p '$REMOTE_DIR' && chmod 700 '$REMOTE_DIR'"
docker save tripvane-service:api tripvane-service:collector | gzip \
    | ssh -o BatchMode=yes "$TARGET" "gunzip | docker load"

echo "deploy-core: writing configuration to $REMOTE_DIR"
tar -C "$STAGE" -czf - compose.yml Caddyfile .env \
    | ssh -o BatchMode=yes "$TARGET" "tar -C '$REMOTE_DIR' -xzf - && chmod 600 '$REMOTE_DIR/.env'"

echo "deploy-core: applying migrations and restarting tripvane-core"
ssh -o BatchMode=yes "$TARGET" "cd '$REMOTE_DIR' \
    && docker compose -p tripvane-core run --rm --no-deps collector alembic -c /app/alembic.ini upgrade head \
    && docker compose -p tripvane-core up -d --force-recreate --remove-orphans \
    && docker compose -p tripvane-core ps"
