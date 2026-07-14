#!/bin/bash
set -euo pipefail

if [ "$EUID" -eq 0 ]; then
    echo "ERROR: do not run as root; sudo is invoked internally where needed." >&2
    exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="${STACK_DIR:-/opt/stacks/azerothcore-admin}"
VERIFY_SCRIPT="${VERIFY_SCRIPT:-${SCRIPT_DIR}/verify-azerothcore-admin.sh}"
CANDIDATE_IMAGE="azerothcore-admin:redeploy-$(date -u +%Y%m%dT%H%M%SZ)"
STAGE_DIR=""
PREVIOUS_COMPOSE=""

cleanup() {
    [ -n "$STAGE_DIR" ] && rm -rf "$STAGE_DIR"
}
trap cleanup EXIT
trap 'echo "Redeploy failed at line $LINENO." >&2' ERR

if [ ! -d "$STACK_DIR" ]; then
    echo "ERROR: Admin stack not found at $STACK_DIR." >&2
    echo "Run install-azerothcore-admin.sh first." >&2
    exit 1
fi
for f in docker-compose.yml .env build; do
    if [ ! -e "$STACK_DIR/$f" ]; then
        echo "ERROR: $STACK_DIR/$f missing — is this a complete admin stack?" >&2
        exit 1
    fi
done

wait_for_healthy() {
    local timeout=60 elapsed=0 health
    while [ "$elapsed" -lt "$timeout" ]; do
        health="$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' azerothcore-admin 2>/dev/null || echo missing)"
        if [ "$health" = "healthy" ]; then
            echo "    healthy after ${elapsed}s"
            return 0
        fi
        if [ "$health" = "unhealthy" ] || [ "$health" = "missing" ]; then
            echo "    container health is ${health}" >&2
            return 1
        fi
        sleep 5
        elapsed=$((elapsed + 5))
    done
    echo "    timed out waiting for healthy state" >&2
    return 1
}

rollback() {
    echo "==> Replacement failed; restoring the previous admin app..." >&2
    docker compose -f "$STACK_DIR/docker-compose.yml" --env-file "$STACK_DIR/.env" down || true
    cp "$PREVIOUS_COMPOSE" "$STACK_DIR/docker-compose.yml"
    docker compose -f "$STACK_DIR/docker-compose.yml" --env-file "$STACK_DIR/.env" up -d || {
        echo "ERROR: automatic rollback could not start the previous admin app." >&2
        echo "Recovery: cd $STACK_DIR && docker compose --env-file .env up -d" >&2
        return 1
    }
    if wait_for_healthy; then
        echo "    previous admin app restored." >&2
        return 0
    fi
    echo "ERROR: rollback container did not become healthy." >&2
    echo "Recovery: cd $STACK_DIR && docker compose --env-file .env up -d" >&2
    return 1
}

echo "==> Staging admin redeploy while the current app remains running..."
STAGE_DIR="$(mktemp -d "${STACK_DIR}/.redeploy.XXXXXX")"
PREVIOUS_COMPOSE="${STAGE_DIR}/previous-docker-compose.yml"
mkdir -p "$STAGE_DIR/build/app/static" "$STAGE_DIR/build/dist"
cp "$STACK_DIR/docker-compose.yml" "$PREVIOUS_COMPOSE"

# Vendor assets are deliberately absent from the repo. Carry the installed
# copies into the isolated candidate build context before syncing source.
for vendor in htmx.min.js htmx-sse.js; do
    if [ -f "$STACK_DIR/build/app/static/$vendor" ]; then
        cp -a "$STACK_DIR/build/app/static/$vendor" "$STAGE_DIR/build/app/static/$vendor"
    fi
done

echo "==> Syncing candidate code..."
rsync -a --delete \
    --exclude='app/static/htmx.min.js' \
    --exclude='app/static/htmx-sse.js' \
    "$REPO_DIR/" "$STAGE_DIR/build/"

# The repository intentionally has no dist/ directory. rsync --delete removes
# the pre-created candidate directory, so recreate it before staging configs.
mkdir -p "$STAGE_DIR/build/dist"

for vendor in htmx.min.js htmx-sse.js; do
    vendor_path="$STAGE_DIR/build/app/static/$vendor"
    vendor_size="$(wc -c < "$vendor_path" 2>/dev/null || echo 0)"
    if [ "$vendor_size" -le 200 ]; then
        echo "ERROR: $vendor is a placeholder (${vendor_size} bytes) — cannot build." >&2
        echo "Run install-azerothcore-admin.sh to vendor HTMX, then retry." >&2
        exit 1
    fi
done

cp "$REPO_DIR/../scripts/backup.sh" "$STAGE_DIR/build/backup.sh"
cp "$REPO_DIR/../docs/configs/"*.conf.dist "$STAGE_DIR/build/dist/"
cp "$REPO_DIR/docker-compose.yml" "$STAGE_DIR/docker-compose.yml"

echo "==> Building candidate image $CANDIDATE_IMAGE..."
ADMIN_IMAGE="$CANDIDATE_IMAGE" docker compose -f "$STAGE_DIR/docker-compose.yml" \
    --project-directory "$STAGE_DIR/build" --env-file "$STACK_DIR/.env" build

# Everything above is fallible but non-disruptive. Only now replace the
# running app, retaining the old compose file/image for automatic rollback.
sudo mkdir -p "$STACK_DIR/data"
sudo chown "$(id -u):$(id -g)" "$STACK_DIR/data"
cp "$STAGE_DIR/docker-compose.yml" "$STACK_DIR/docker-compose.yml"

echo "==> Starting candidate admin container..."
if ! ADMIN_IMAGE="$CANDIDATE_IMAGE" docker compose -f "$STACK_DIR/docker-compose.yml" \
    --env-file "$STACK_DIR/.env" up -d --force-recreate; then
    rollback || true
    exit 1
fi

echo "==> Waiting for candidate container to be healthy..."
if ! wait_for_healthy; then
    rollback || true
    exit 1
fi

if ! "$VERIFY_SCRIPT"; then
    rollback || true
    exit 1
fi

echo ""
TAILSCALE_IP=""
ADMIN_PORT=""
# shellcheck disable=SC1090,SC1091
source "$STACK_DIR/.env"
if [ -n "${TAILSCALE_IP:-}" ] && [ -n "${ADMIN_PORT:-}" ]; then
    echo "Admin app available at http://${TAILSCALE_IP}:${ADMIN_PORT}/"
fi
