#!/usr/bin/env bash
# Build the lookup API and collector images locally, push them to the core droplet over
# ssh, write its settings, apply the database migrations and restart the stack.
#
#   infra/deploy-core.sh HOST          (or: make deploy-core HOST=203.0.113.20)
#
# Settings come from .env.core at the repository root, which is never committed; it must
# set API_HOSTNAME, CANARY_HOSTNAME and COLLECTOR_HOSTNAME (each with a DNS A record
# pointing at HOST; CANARY_HOSTNAME must not name Tripvane, because canary URLs are planted
# in decoy documents), DATABASE_URL, CANARY_HMAC_KEY and CANARY_BASE_URL
# (https://CANARY_HOSTNAME).
# The droplet needs Docker and compose (infra/bootstrap-droplet.sh) and ssh access as
# DEPLOY_USER (default root).
set -euo pipefail

[ $# -eq 1 ] && [ -n "$1" ] || { echo "usage: $0 HOST" >&2; exit 2; }
HOST=$1

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DEPLOY_USER=${DEPLOY_USER:-root}
TARGET="$DEPLOY_USER@$HOST"
REMOTE_DIR=/opt/tripvane/core
ENV_FILE="$ROOT/.env.core"

[ -f "$ENV_FILE" ] || { echo "deploy-core: missing $ENV_FILE" >&2; exit 2; }

# Read one KEY=value line from the env file without sourcing it.
env_value() {
    sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

for key in API_HOSTNAME CANARY_HOSTNAME COLLECTOR_HOSTNAME DATABASE_URL CANARY_HMAC_KEY \
    CANARY_BASE_URL; do
    if [ -z "$(env_value "$key")" ]; then
        echo "deploy-core: $key is not set in $ENV_FILE" >&2
        exit 2
    fi
done
if [ "$(env_value CANARY_BASE_URL)" != "https://$(env_value CANARY_HOSTNAME)" ]; then
    echo "deploy-core: CANARY_BASE_URL must be https://CANARY_HOSTNAME" >&2
    exit 2
fi

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
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
cp "$ROOT/infra/compose.core.yml" "$STAGE/compose.yml"
cp "$ROOT/infra/Caddyfile.core" "$STAGE/Caddyfile"
(umask 077 && cp "$ENV_FILE" "$STAGE/.env")
tar -C "$STAGE" -czf - compose.yml Caddyfile .env \
    | ssh -o BatchMode=yes "$TARGET" "tar -C '$REMOTE_DIR' -xzf - && chmod 600 '$REMOTE_DIR/.env'"

echo "deploy-core: applying migrations and restarting tripvane-core"
ssh -o BatchMode=yes "$TARGET" "cd '$REMOTE_DIR' \
    && docker compose -p tripvane-core run --rm --no-deps collector alembic -c /app/alembic.ini upgrade head \
    && docker compose -p tripvane-core up -d --force-recreate --remove-orphans \
    && docker compose -p tripvane-core ps"
