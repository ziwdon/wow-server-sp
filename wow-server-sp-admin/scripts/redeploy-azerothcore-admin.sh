#!/bin/bash
set -euo pipefail

if [ "$EUID" -eq 0 ]; then
    echo "ERROR: do not run as root; sudo is invoked internally where needed." >&2
    exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="${STACK_DIR:-/opt/stacks/azerothcore-admin}"
AC_STACK_DIR="${AC_STACK_DIR:-/opt/stacks/azerothcore}"
VERIFY_SCRIPT="${VERIFY_SCRIPT:-${SCRIPT_DIR}/verify-azerothcore-admin.sh}"
CANDIDATE_IMAGE=""
STAGE_DIR=""
PREVIOUS_COMPOSE=""

# Stage the .conf.dist files the Settings key index is built from. Prefer the
# AzerothCore source tree the running server was built from, so the index
# always matches the deployed version; fall back to the offline docs copy.
stage_conf_dists() {
    local dest="$1" f
    local -a sources=(
        "$AC_STACK_DIR/src/server/apps/worldserver/worldserver.conf.dist"
        "$AC_STACK_DIR/modules/mod-playerbots/conf/playerbots.conf.dist"
        "$AC_STACK_DIR/modules/mod-ah-bot-plus/conf/mod_ahbot.conf.dist"
        "$AC_STACK_DIR/modules/mod-individual-progression/conf/individualProgression.conf.dist"
    )
    for f in "${sources[@]}"; do
        if [ ! -f "$f" ]; then
            echo "WARNING: $f not found; using docs/configs copies for the key index." >&2
            cp "$REPO_DIR/../docs/configs/"*.conf.dist "$dest/"
            return 0
        fi
    done
    cp "${sources[@]}" "$dest/"
}

cleanup() {
    # An if-statement (not "[ ... ] && rm ...") is required here: the EXIT
    # trap's own exit status can clobber an explicit `exit N` set earlier in
    # the script, and "[ -n "" ] && ..." leaves a false (1) status when
    # STAGE_DIR is still empty (e.g. an early exit before staging begins).
    if [ -n "$STAGE_DIR" ]; then
        rm -rf "$STAGE_DIR"
    fi
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

# Serialize redeploys: a second concurrent invocation must not collide on the
# candidate image tag or interleave with an in-progress rollback.
exec 9>"${STACK_DIR}/.redeploy.lock"
if ! flock -n 9; then
    echo "ERROR: another admin redeploy is already running." >&2
    exit 75
fi

# The Armory mounts AC's client-data volume (docker-compose.yml, external), which
# the AC installer's Phase 4 creates. Read-only check, before anything is staged.
if ! docker volume inspect azerothcore_ac-client-data >/dev/null 2>&1; then
    echo "ERROR: client-data volume azerothcore_ac-client-data not found — install AzerothCore first (Phase 4 creates it)" >&2
    exit 1
fi

CANDIDATE_IMAGE="azerothcore-admin:redeploy-$(date -u +%Y%m%dT%H%M%SZ)"

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
for vendor in htmx.min.js htmx-sse.js jquery.min.js; do
    if [ -f "$STACK_DIR/build/app/static/$vendor" ]; then
        cp -a "$STACK_DIR/build/app/static/$vendor" "$STAGE_DIR/build/app/static/$vendor"
    fi
done

echo "==> Syncing candidate code..."
rsync -a --delete \
    --exclude='app/static/htmx.min.js' \
    --exclude='app/static/htmx-sse.js' \
    --exclude='app/static/jquery.min.js' \
    "$REPO_DIR/" "$STAGE_DIR/build/"

# The repository intentionally has no dist/ directory. rsync --delete removes
# the pre-created candidate directory, so recreate it before staging configs.
mkdir -p "$STAGE_DIR/build/dist"

# Where install-azerothcore-admin.sh fetches each vendor file from (keep in sync;
# tests/test_armory_deploy.py compares the two).
vendor_url() {
    case "$1" in
        htmx.min.js) echo "https://unpkg.com/htmx.org@2.0.3/dist/htmx.min.js" ;;
        htmx-sse.js) echo "https://unpkg.com/htmx-ext-sse@2.2.2/sse.js" ;;
        jquery.min.js) echo "https://unpkg.com/jquery@3.7.1/dist/jquery.min.js" ;;
    esac
}

# One-time backfill, like backfill_tpm_env below (the other thing redeploy adds to
# an older install): an install from before the Armory has no jquery.min.js to
# carry over. Fetch it once from the installer's pinned URL into the candidate and
# keep a copy in $STACK_DIR/build, which later redeploys carry like the HTMX files.
# HTMX is never fetched here. A failed fetch falls through to the guard below.
jquery_path="$STAGE_DIR/build/app/static/jquery.min.js"
jquery_size=0
if [ -f "$jquery_path" ]; then
    jquery_size="$(wc -c < "$jquery_path")"
fi
if [ "$jquery_size" -le 200 ]; then
    echo "==> jquery.min.js is not vendored yet; fetching it once from $(vendor_url jquery.min.js)..."
    jquery_tmp="$(mktemp "$STAGE_DIR/jquery.min.js.XXXXXX")"
    if curl -sSfL -o "$jquery_tmp" "$(vendor_url jquery.min.js)"; then
        chmod 644 "$jquery_tmp"
        mv -f "$jquery_tmp" "$jquery_path"
        jquery_keep=""
        if jquery_keep="$(mktemp "$STACK_DIR/build/app/static/.jquery.min.js.XXXXXX")" \
            && cp "$jquery_path" "$jquery_keep" && chmod 644 "$jquery_keep" \
            && mv -f "$jquery_keep" "$STACK_DIR/build/app/static/jquery.min.js"; then
            echo "    kept a copy in $STACK_DIR/build/app/static/ for later redeploys."
        else
            if [ -n "$jquery_keep" ]; then
                rm -f -- "$jquery_keep"
            fi
            echo "WARNING: could not keep jquery.min.js in $STACK_DIR/build/app/static/; the next redeploy fetches it again." >&2
        fi
    else
        rm -f -- "$jquery_tmp"
        echo "WARNING: could not fetch jquery.min.js." >&2
    fi
fi

for vendor in htmx.min.js htmx-sse.js jquery.min.js; do
    vendor_path="$STAGE_DIR/build/app/static/$vendor"
    vendor_size="$(wc -c < "$vendor_path" 2>/dev/null || echo 0)"
    if [ "$vendor_size" -le 200 ]; then
        echo "ERROR: $vendor is a placeholder (${vendor_size} bytes) — cannot build." >&2
        echo "Fetch it, then retry: curl -sSfL -o \"$STACK_DIR/build/app/static/$vendor\" \"$(vendor_url "$vendor")\"" >&2
        exit 1
    fi
done

cp "$REPO_DIR/../scripts/backup.sh" "$STAGE_DIR/build/backup.sh"
stage_conf_dists "$STAGE_DIR/build/dist"
cp "$REPO_DIR/docker-compose.yml" "$STAGE_DIR/docker-compose.yml"

# Cloud backup needs TPM_DEVICE/TSS_GID in the admin .env for the compose file's
# TPM mapping. Installs that predate it have neither, so append what is missing
# (never editing existing lines). This is the one place redeploy changes .env.
# The feature is optional, so a failure here must not stop a redeploy.
# shellcheck disable=SC1091
source "$SCRIPT_DIR/tpm-env.sh"
if backfill_tpm_env "$STACK_DIR/.env"; then
    if [ -n "$TPM_ENV_ADDED" ]; then
        echo "==> Added $TPM_ENV_ADDED to $STACK_DIR/.env for the Cloud backup TPM mapping."
    fi
else
    echo "WARNING: could not add TPM settings to $STACK_DIR/.env; Cloud backup will report the TPM as unavailable." >&2
fi

echo "==> Building candidate image $CANDIDATE_IMAGE..."
ADMIN_IMAGE="$CANDIDATE_IMAGE" docker compose -f "$STAGE_DIR/docker-compose.yml" \
    --project-directory "$STAGE_DIR/build" --env-file "$STACK_DIR/.env" build

# Everything above is fallible but non-disruptive. Only now replace the
# running app, retaining the old compose file/image for automatic rollback.
# sudo is skipped when data/ is already correct: non-interactive callers (e.g.
# the upgrade cutover) have no TTY for a password prompt.
if ! { [ -d "$STACK_DIR/data" ] && [ "$(stat -c '%u:%g' "$STACK_DIR/data")" = "$(id -u):$(id -g)" ]; }; then
    sudo mkdir -p "$STACK_DIR/data"
    sudo chown "$(id -u):$(id -g)" "$STACK_DIR/data"
fi
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

echo "==> Promoting verified candidate to azerothcore-admin:local..."
if ! docker image tag "$CANDIDATE_IMAGE" azerothcore-admin:local; then
    rollback || true
    exit 1
fi
docker image rm "$CANDIDATE_IMAGE" >/dev/null 2>&1 || \
    echo "WARNING: could not remove temporary candidate tag $CANDIDATE_IMAGE" >&2

echo ""
TAILSCALE_IP=""
LAN_IP=""
ADMIN_PORT=""
# shellcheck disable=SC1090,SC1091
source "$STACK_DIR/.env"
if [ -n "${TAILSCALE_IP:-}" ] && [ -n "${ADMIN_PORT:-}" ]; then
    echo "Admin app available at http://${TAILSCALE_IP}:${ADMIN_PORT}/"
fi
if [ -n "${LAN_IP:-}" ] && [ -n "${ADMIN_PORT:-}" ]; then
    echo "Also on the LAN at http://${LAN_IP}:${ADMIN_PORT}/"
fi
