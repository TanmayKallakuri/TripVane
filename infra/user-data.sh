#!/usr/bin/env bash
# Print the cloud-init user data that sets up one server on its first boot, for hosts that
# cannot be reached over ssh. The server builds its own images, so nothing is pushed to it.
#
#   infra/user-data.sh NAME > user-data.yaml
#
# NAME is core or a sensor, as for infra/stage.sh, with settings in .env.NAME at the
# repository root; the settings are checked here before anything is printed. Pass the
# output as the user data of a new Ubuntu 24.04 server whose provider runs cloud-init.
#
# On first boot the server writes .env.NAME (mode 600), fetches the repository at REF from
# REPO_URL, installs Docker (infra/bootstrap-droplet.sh), stages its files with
# infra/stage.sh, builds its images, waits until its hostnames resolve (so Caddy can get
# certificates), applies the database migrations (core only) and starts the stack. It logs
# to /var/log/tripvane-install.log.
#
# REF defaults to the commit checked out here, which must already be pushed. REPO_URL
# defaults to the public GitHub repository; a private repository needs a URL carrying a
# read-only token, which then also sits in the user data.
#
# The output contains every secret in .env.NAME. Do not commit or share it. Needs GNU
# base64 (Linux).
set -euo pipefail

[ $# -eq 1 ] && [ -n "$1" ] || { echo "usage: $0 NAME" >&2; exit 2; }
NAME=$1

ROOT=$(cd "$(dirname "$0")/.." && pwd)
ENV_FILE="$ROOT/.env.$NAME"
REF=${REF:-$(git -C "$ROOT" rev-parse HEAD)}
REPO_URL=${REPO_URL:-https://github.com/TanmayKallakuri/TripVane.git}

CHECK=$(mktemp -d)
trap 'rm -rf "$CHECK"' EXIT
"$ROOT/infra/stage.sh" "$NAME" "$CHECK"

# Read one KEY=value line from the env file without sourcing it.
env_value() {
    sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

if [ "$NAME" = core ]; then
    HOSTNAMES="$(env_value API_HOSTNAME) $(env_value CANARY_HOSTNAME) $(env_value COLLECTOR_HOSTNAME)"
    BUILD='for service in api collector; do
    docker build -f infra/Dockerfile.service --build-arg SERVICE="$service" -t "tripvane-service:$service" .
done'
    MIGRATE="docker compose -p tripvane-core run --rm --no-deps collector alembic -c /app/alembic.ini upgrade head"
else
    ARCHETYPE=${NAME%%-*}
    HOSTNAMES=$(env_value SENSOR_HOSTNAME)
    BUILD="docker build -f infra/Dockerfile.sensor --build-arg ARCHETYPE=$ARCHETYPE -t tripvane-sensor:$ARCHETYPE ."
    MIGRATE=":"
fi

INSTALL=$(cat <<EOF
#!/usr/bin/env bash
# Written by infra/user-data.sh and run once by cloud-init on first boot.
set -euo pipefail
exec >>/var/log/tripvane-install.log 2>&1
echo "tripvane-install: started \$(date -u +%FT%TZ)"

DIR=/opt/tripvane/$NAME
SRC=/opt/tripvane/src
chmod 700 /opt/tripvane "\$DIR"

command -v git >/dev/null || { apt-get update && apt-get install -y git; }
rm -rf "\$SRC"
git init -q "\$SRC"
git -C "\$SRC" fetch -q --depth 1 '$REPO_URL' '$REF'
git -C "\$SRC" checkout -q FETCH_HEAD

bash "\$SRC/infra/bootstrap-droplet.sh"

(umask 077 && cp "\$DIR/.env" "\$SRC/.env.$NAME")
"\$SRC/infra/stage.sh" '$NAME' "\$DIR"
rm -f "\$SRC/.env.$NAME"

cd "\$SRC"
$BUILD

# Caddy asks for certificates when it starts, so wait (up to 30 minutes) for DNS.
for host in $HOSTNAMES; do
    for _ in \$(seq 180); do
        getent hosts "\$host" >/dev/null && break
        sleep 10
    done
done

cd "\$DIR"
$MIGRATE
docker compose -p 'tripvane-$NAME' up -d --remove-orphans
docker compose -p 'tripvane-$NAME' ps
echo "tripvane-install: done \$(date -u +%FT%TZ)"
EOF
)

cat <<EOF
#cloud-config
ssh_pwauth: false
write_files:
  - path: /opt/tripvane/$NAME/.env
    owner: root:root
    permissions: '0600'
    encoding: b64
    content: $(base64 -w0 < "$ENV_FILE")
  - path: /opt/tripvane/install.sh
    owner: root:root
    permissions: '0700'
    encoding: b64
    content: $(printf '%s\n' "$INSTALL" | base64 -w0)
runcmd:
  - [bash, /opt/tripvane/install.sh]
EOF
