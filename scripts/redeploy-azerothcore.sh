#!/bin/bash
set -euo pipefail

# ============================================================================
# redeploy-azerothcore.sh
# ----------------------------------------------------------------------------
# Recompile the AzerothCore worldserver from the LOCAL source tree and redeploy
# ONLY the ac-worldserver container, preserving all configuration and data.
#
# Use this after editing source under $STACK_DIR/modules/** or $STACK_DIR/src/**
# (e.g. a mod-playerbots patch). It is deliberately isolated from
# install-azerothcore.sh: `--resume-from=3` would run Phase 3 AND every phase
# after it (Phase 4 DB-init + client-data download, pause-2 account creation,
# networking, systemd ...), which is far more than a code redeploy needs.
#
# What this script does:
#   1. docker compose build ac-worldserver   (compiles local source; ccache-fast)
#   2. graceful stop of the running worldserver (SIGTERM -> World::StopNow saveall)
#   3. docker compose up -d ac-worldserver    (recreate with the new image)
#   4. lightweight verify (container up, World Initialized, Errors.log size)
#
# What this script NEVER touches:
#   .env, docker-compose.override.yml, docker-compose.admin.yml, the databases,
#   or any container other than ac-worldserver. The build only COPYs the local
#   source into the image (Dockerfile: `COPY modules ...`) — no git clone/reset —
#   so a local edit is exactly what gets compiled. `up -d` re-reads COMPOSE_FILE
#   from .env, so docker-compose.override.yml and docker-compose.admin.yml are
#   re-applied (last-precedence) unchanged.
#
# Build time: the FIRST build is 45-75 min on a Ryzen 5 7430U. A one-file change
# is much faster — ccache (Dockerfile CMAKE_*_COMPILER_LAUNCHER=ccache, BuildKit
# cache mount) restores unchanged objects, so typically only the edited
# translation unit recompiles plus a relink.
#
# Env overrides:
#   STACK_DIR           (default /opt/stacks/azerothcore)
#   STOP_TIMEOUT        seconds to allow a clean saveall before SIGKILL (default 120)
#   WORLD_INIT_TIMEOUT  seconds to wait for "World Initialized" (default 300)
#   SKIP_BUILD=1        redeploy the already-built image without recompiling
#
# Usage:
#   ./scripts/redeploy-azerothcore.sh
#   STOP_TIMEOUT=180 ./scripts/redeploy-azerothcore.sh
# ============================================================================

if [ "$EUID" -eq 0 ]; then
    echo "ERROR: do not run as root; docker is invoked as your user (docker group)." >&2
    exit 1
fi

STACK_DIR="${STACK_DIR:-/opt/stacks/azerothcore}"
SERVICE="ac-worldserver"
STOP_TIMEOUT="${STOP_TIMEOUT:-120}"
WORLD_INIT_TIMEOUT="${WORLD_INIT_TIMEOUT:-300}"

trap 'echo "" >&2; echo "Redeploy FAILED at line ${LINENO}." >&2' ERR

# --- Preflight ------------------------------------------------------------
if [ ! -d "$STACK_DIR" ]; then
    echo "ERROR: AC stack not found at $STACK_DIR." >&2
    exit 1
fi
cd "$STACK_DIR"
for f in docker-compose.yml .env; do
    if [ ! -f "$f" ]; then
        echo "ERROR: $STACK_DIR/$f missing — is this a complete AC stack?" >&2
        exit 1
    fi
done

# docker compose (run from $STACK_DIR) auto-reads .env, including its
# COMPOSE_FILE= line which merges override.yml + admin.yml.
if ! docker compose config --services 2>/dev/null | grep -qx "$SERVICE"; then
    echo "ERROR: service '$SERVICE' not found in the merged compose config." >&2
    exit 1
fi

echo "==> AzerothCore worldserver redeploy"
echo "    stack:   $STACK_DIR"
echo "    service: $SERVICE"
echo "    started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# --- 1. Build (while the old container keeps running) ---------------------
if [ "${SKIP_BUILD:-0}" = "1" ]; then
    echo "==> SKIP_BUILD=1 — using the existing image, not recompiling."
else
    echo "==> Building $SERVICE from local source (first build 45-75 min; incremental much faster)..."
    docker compose build "$SERVICE"
fi

# --- 2. Graceful stop -----------------------------------------------------
# SIGTERM -> AC's World::StopNow performs a clean shutdown with a final saveall.
# A generous -t avoids a Docker SIGKILL mid-save (bot-heavy saveall can take
# 30-45 s). No stop_grace_period is set in compose (Docker default is only 10 s),
# so we pass the timeout explicitly.
if [ -n "$(docker compose ps -q "$SERVICE" 2>/dev/null)" ]; then
    echo "==> Gracefully stopping $SERVICE (up to ${STOP_TIMEOUT}s for saveall)..."
    docker compose stop -t "$STOP_TIMEOUT" "$SERVICE"
else
    echo "==> $SERVICE not currently running; skipping stop."
fi

# --- 3. Recreate with the freshly built image ----------------------------
echo "==> Recreating $SERVICE..."
docker compose up -d "$SERVICE"

# --- 4. Verify ------------------------------------------------------------
echo "==> Verifying..."
sleep 3
status="$(docker inspect -f '{{.State.Status}}' "$SERVICE" 2>/dev/null || echo missing)"
echo "    container status: $status"
if [ "$status" != "running" ]; then
    echo "ERROR: $SERVICE is not running after redeploy." >&2
    docker compose logs --tail 50 "$SERVICE" || true
    exit 1
fi

# Server.log is a host-mounted file that may not yet have been truncated/
# rewritten by the freshly recreated container by the time we start polling,
# so a match there can be a STALE marker from the previous boot. Only trust
# "World Initialized" observed in this container's own logs since its actual
# current start time.
started_at="$(docker inspect -f '{{.State.StartedAt}}' "$SERVICE" 2>/dev/null || true)"
if [ -z "$started_at" ]; then
    echo "ERROR: could not determine $SERVICE current start time." >&2
    exit 1
fi
echo "    waiting for 'World Initialized' (up to ${WORLD_INIT_TIMEOUT}s)..."
deadline=$(( $(date +%s) + WORLD_INIT_TIMEOUT ))
init_ok=0
while [ "$(date +%s)" -lt "$deadline" ]; do
    status="$(docker inspect -f '{{.State.Status}}' "$SERVICE" 2>/dev/null || echo missing)"
    if [ "$status" != "running" ]; then
        echo "ERROR: $SERVICE entered state '$status' before initialization completed." >&2
        docker compose logs --tail 50 "$SERVICE" || true
        exit 1
    fi
    if docker logs --since "$started_at" "$SERVICE" 2>&1 | grep -q "World Initialized"; then
        init_ok=1
        break
    fi
    sleep 5
done
if [ "$init_ok" -eq 1 ]; then
    echo "    World Initialized — worldserver is up."
else
    echo "ERROR: did not observe 'World Initialized' within ${WORLD_INIT_TIMEOUT}s." >&2
    echo "      Inspect: docker logs --tail 60 ac-worldserver ; tail logs/Errors.log" >&2
    exit 1
fi

# Errors.log: 0 bytes = clean. (graveyard_zone lines are known-benign; see
# the Game Master skill's ref-troubleshooting.md.)
if [ -f logs/Errors.log ]; then
    sz="$(stat -c%s logs/Errors.log 2>/dev/null || echo '?')"
    echo "    Errors.log size: ${sz} bytes (0 = clean)"
fi

echo "==> Redeploy complete at $(date -u +%Y-%m-%dT%H:%M:%SZ)."
echo "    Next: docker logs --tail 30 ac-worldserver"
echo "    Full post-deploy check (optional): ./scripts/verify-azerothcore.sh"
