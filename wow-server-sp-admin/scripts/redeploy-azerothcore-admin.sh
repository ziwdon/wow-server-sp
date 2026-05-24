#!/bin/bash
set -euo pipefail

if [ "$EUID" -eq 0 ]; then
    echo "ERROR: do not run as root; sudo is invoked internally where needed." >&2
    exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR=/opt/stacks/azerothcore-admin

trap 'echo "Redeploy failed at line $LINENO." >&2' ERR

if [ ! -d "$STACK_DIR" ]; then
    echo "ERROR: Admin stack not found at $STACK_DIR." >&2
    echo "Run install-azerothcore-admin.sh first." >&2
    exit 1
fi

echo "==> Stopping admin container..."
docker compose -f "$STACK_DIR/docker-compose.yml" --env-file "$STACK_DIR/.env" down || true

echo "==> Removing old image..."
docker rmi -f azerothcore-admin:local 2>/dev/null || true

echo "==> Syncing code..."
rsync -a --delete "$REPO_DIR/" "$STACK_DIR/build/"

echo "==> Copying dist configs..."
cp "$REPO_DIR/../docs/configs/"*.conf.dist "$STACK_DIR/build/dist/"

echo "==> Copying docker-compose.yml..."
cp "$REPO_DIR/docker-compose.yml" "$STACK_DIR/"

echo "==> Building image..."
docker compose -f "$STACK_DIR/docker-compose.yml" \
    --project-directory "$STACK_DIR/build" \
    --env-file "$STACK_DIR/.env" build

echo "==> Starting admin container..."
docker compose -f "$STACK_DIR/docker-compose.yml" --env-file "$STACK_DIR/.env" up -d

echo ""
"$SCRIPT_DIR/verify-azerothcore-admin.sh"

echo ""
TAILSCALE_IP=""
ADMIN_PORT=""
if [ -f "$STACK_DIR/.env" ]; then
    # shellcheck disable=SC1090,SC1091
    source "$STACK_DIR/.env"
fi
if [ -n "${TAILSCALE_IP:-}" ] && [ -n "${ADMIN_PORT:-}" ]; then
    echo "Admin app available at http://${TAILSCALE_IP}:${ADMIN_PORT}/"
fi
