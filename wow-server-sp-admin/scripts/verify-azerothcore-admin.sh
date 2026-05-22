#!/bin/bash
# Intentionally `set -u` only -- every check runs even after a failure.
set -u

STACK_DIR=/opt/stacks/azerothcore-admin
AC_STACK_DIR=/opt/stacks/azerothcore
PASS=0
FAIL=0

ok()   { echo "[OK]   $*"; PASS=$((PASS+1)); }
fail() { echo "[FAIL] $*"; FAIL=$((FAIL+1)); }
info() { echo "[INFO] $*"; }

# 1. stack dir exists with installer-user ownership
if [ -d "$STACK_DIR" ]; then
    ok "stack dir exists: $STACK_DIR"
    expected_owner="$(id -un):$(id -gn)"
    actual_owner="$(stat -c '%U:%G' "$STACK_DIR" 2>/dev/null || echo unknown)"
    if [ "$actual_owner" = "$expected_owner" ]; then
        ok "stack dir owned by $expected_owner"
    else
        fail "stack dir owner is $actual_owner; expected $expected_owner"
    fi
else
    fail "stack dir missing: $STACK_DIR"
fi

# 2. admin container running and healthy
status="$(docker inspect --format='{{.State.Status}}' azerothcore-admin 2>/dev/null || echo missing)"
if [ "$status" = "running" ]; then
    ok "azerothcore-admin container is running"
else
    fail "azerothcore-admin container status: $status"
fi
health="$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' azerothcore-admin 2>/dev/null || echo missing)"
if [ "$health" = "healthy" ]; then
    ok "azerothcore-admin container health is healthy"
else
    fail "azerothcore-admin container health: $health"
fi

# 3. listening on tailscale interface
TAILSCALE_IP=""
ADMIN_PORT=""
if [ -f "$STACK_DIR/.env" ]; then
    # shellcheck disable=SC1090,SC1091
    source "$STACK_DIR/.env"
fi
if [ -z "${TAILSCALE_IP:-}" ]; then
    fail "TAILSCALE_IP not set in $STACK_DIR/.env -- cannot verify bind"
elif [ -z "${ADMIN_PORT:-}" ]; then
    fail "ADMIN_PORT not set in $STACK_DIR/.env -- cannot verify bind"
else
    bind="${TAILSCALE_IP}:${ADMIN_PORT}"
    # Read all listening sockets and look for the exact local-address match.
    # Using `ss -ltn -H` with awk avoids fragile filter-syntax differences
    # across iproute2 versions, and ensures a 127.0.0.1-bound port can't
    # accidentally satisfy the check.
    if ss -ltn -H 2>/dev/null | awk '{print $4}' | grep -Fxq "$bind"; then
        ok "admin port listening on Tailscale interface ($bind)"
    else
        fail "admin port NOT listening on $bind"
    fi
fi

# 4. COMPOSE_FILE includes admin.yml
if grep -qE '^COMPOSE_FILE=.*docker-compose\.admin\.yml' "$AC_STACK_DIR/.env" 2>/dev/null; then
    ok "COMPOSE_FILE in AC .env includes docker-compose.admin.yml"
else
    fail "COMPOSE_FILE in AC .env does not include docker-compose.admin.yml"
fi

# 5. admin.yml file exists
if [ -f "$AC_STACK_DIR/docker-compose.admin.yml" ]; then
    ok "$AC_STACK_DIR/docker-compose.admin.yml exists"
else
    fail "$AC_STACK_DIR/docker-compose.admin.yml missing"
fi

# 6. health endpoint
if [ -n "${TAILSCALE_IP:-}" ] && [ -n "${ADMIN_PORT:-}" ]; then
    if curl -fsS "http://${TAILSCALE_IP}:${ADMIN_PORT}/healthz" >/dev/null; then
        ok "/healthz returns 200"
    else
        fail "/healthz unreachable at ${TAILSCALE_IP}:${ADMIN_PORT}"
    fi
else
    info "TAILSCALE_IP/ADMIN_PORT not set -- skipping /healthz check"
fi

# 7. systemd unit, if installed, must be enabled (spec §Verification item 7)
if [ -f /etc/systemd/system/azerothcore-admin.service ]; then
    if systemctl is-enabled --quiet azerothcore-admin.service 2>/dev/null; then
        ok "azerothcore-admin.service is enabled"
    else
        fail "azerothcore-admin.service is installed but NOT enabled"
    fi
else
    info "azerothcore-admin.service not installed (skip)"
fi

# 8. AC stack still functional (delegate)
if [ -x "$(dirname "$0")/../../scripts/verify-azerothcore.sh" ]; then
    info "delegating to AC verify script (exit code preserved)"
    # shellcheck disable=SC2015
    "$(dirname "$0")/../../scripts/verify-azerothcore.sh" >/dev/null 2>&1 \
        && ok "AC verify passed" \
        || fail "AC verify reported issues"
else
    info "AC verify script not found at expected path"
fi

echo ""
echo "Summary: ${PASS} OK / ${FAIL} FAIL"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
