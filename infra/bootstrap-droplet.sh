#!/usr/bin/env bash
# Install Docker Engine and the compose plugin on a fresh Ubuntu droplet, from Docker's
# own apt repository. Run as root on the droplet, for example:
#
#   ssh root@203.0.113.10 'bash -s' < infra/bootstrap-droplet.sh
#
# Safe to run again: it only installs what is missing.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "bootstrap: run as root" >&2
    exit 1
fi

# shellcheck source=/dev/null
. /etc/os-release
if [ "${ID:-}" != "ubuntu" ]; then
    echo "bootstrap: expected Ubuntu, found ${ID:-unknown}" >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive

if docker compose version >/dev/null 2>&1; then
    echo "bootstrap: Docker and compose are already installed"
else
    apt-get update
    apt-get install -y ca-certificates curl
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

systemctl enable --now docker
docker version --format 'bootstrap: Docker {{.Server.Version}}'
docker compose version
