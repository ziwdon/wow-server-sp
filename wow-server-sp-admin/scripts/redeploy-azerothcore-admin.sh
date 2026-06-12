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
# Exclude vendor files that the install script downloads from unpkg.
# rsync --delete would otherwise overwrite them with the 51-byte repo
# placeholders, baking a broken image that only works from browser cache.
rsync -a --delete \
    --exclude='app/static/htmx.min.js' \
    --exclude='app/static/htmx-sse.js' \
    "$REPO_DIR/" "$STACK_DIR/build/"

# Guard: abort early if HTMX vendor files are still placeholders.
# This happens when the build dir is new or was cleaned.
# Fix: run install-azerothcore-admin.sh, or fetch manually:
#   curl -sSfL -o "$STACK_DIR/build/app/static/htmx.min.js" \
#       "https://unpkg.com/htmx.org@2.0.3/dist/htmx.min.js"
#   curl -sSfL -o "$STACK_DIR/build/app/static/htmx-sse.js" \
#       "https://unpkg.com/htmx-ext-sse@2.2.2/sse.js"
_htmx_size="$(wc -c < "$STACK_DIR/build/app/static/htmx.min.js" 2>/dev/null || echo 0)"
if [ "$_htmx_size" -le 200 ]; then
    echo "ERROR: htmx.min.js is a placeholder ($_htmx_size bytes) — cannot build." >&2
    echo "  Run install-azerothcore-admin.sh to vendor HTMX, or fetch manually (see comment above)." >&2
    exit 1
fi

mkdir -p "$STACK_DIR/build/dist"
# Stage the canonical backup script into the build context (Dockerfile COPYs it).
cp "$REPO_DIR/../scripts/backup.sh" "$STACK_DIR/build/backup.sh"

echo "==> Copying dist configs..."
cp "$REPO_DIR/../docs/configs/"*.conf.dist "$STACK_DIR/build/dist/"

echo "==> Copying docker-compose.yml..."
cp "$REPO_DIR/docker-compose.yml" "$STACK_DIR/"

echo "==> Building image..."
docker compose -f "$STACK_DIR/docker-compose.yml" \
    --project-directory "$STACK_DIR/build" \
    --env-file "$STACK_DIR/.env" build

# Ensure data dir exists and is writable by the container user. If Docker
# created it implicitly (bind-mount source missing), it would be root:root and
# the non-root container user cannot write maintenance scheduler state to it.
sudo mkdir -p "$STACK_DIR/data"
sudo chown "$(id -u):$(id -g)" "$STACK_DIR/data"

echo "==> Starting admin container..."
docker compose -f "$STACK_DIR/docker-compose.yml" --env-file "$STACK_DIR/.env" up -d

echo "==> Waiting for container to be healthy..."
timeout=60
elapsed=0
while [ "$elapsed" -lt "$timeout" ]; do
    health="$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' azerothcore-admin 2>/dev/null || echo missing)"
    if [ "$health" = "healthy" ]; then
        echo "    healthy after ${elapsed}s"
        break
    elif [ "$health" = "unhealthy" ]; then
        echo "    container is unhealthy after ${elapsed}s -- proceeding to verify for diagnostics"
        break
    fi
    sleep 5
    elapsed=$((elapsed + 5))
done
if [ "$elapsed" -ge "$timeout" ]; then
    echo "    timed out waiting for healthy state -- proceeding to verify for diagnostics"
fi

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
