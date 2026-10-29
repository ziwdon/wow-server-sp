#!/bin/bash
# Intentionally `set -u` only -- every check runs even after a failure.
set -u

STACK_DIR="${STACK_DIR:-/opt/stacks/azerothcore-admin}"
AC_STACK_DIR="${AC_STACK_DIR:-/opt/stacks/azerothcore}"
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
LAN_IP=""
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
# 3b. optional LAN bind (LAN_IP in .env). The LAN bind is best-effort: when
# LAN_IP is not currently assigned, LAN access is down but Tailscale is not,
# so that state is advisory. ip_nonlocal_bind=1 is what lets the container
# start at all in that state.
if [ -n "${LAN_IP:-}" ] && [ -n "${ADMIN_PORT:-}" ]; then
    lan_bind="${LAN_IP}:${ADMIN_PORT}"
    if [ "$(sysctl -n net.ipv4.ip_nonlocal_bind 2>/dev/null || echo 0)" = 1 ]; then
        info "net.ipv4.ip_nonlocal_bind=1 -- a missing LAN IP cannot block container start"
    else
        info "net.ipv4.ip_nonlocal_bind is not 1 -- if $LAN_IP disappears the admin container will not start (Tailscale included)"
    fi
    if ! ip -4 -o addr show 2>/dev/null | awk '{split($4, a, "/"); print a[1]}' | grep -Fxq "$LAN_IP"; then
        info "LAN_IP $LAN_IP is not assigned to this host -- LAN access unavailable (Tailscale unaffected)"
    else
        if ss -ltn -H 2>/dev/null | awk '{print $4}' | grep -Fxq "$lan_bind"; then
            ok "admin port listening on LAN interface ($lan_bind)"
        else
            fail "admin port NOT listening on LAN bind $lan_bind"
        fi
        if curl -fsS "http://${lan_bind}/healthz" >/dev/null; then
            ok "/healthz returns 200 on LAN bind"
        else
            fail "/healthz unreachable at $lan_bind"
        fi
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

# 7. Docker-backed readiness. These run in the non-root admin container so a
# wrong DOCKER_GID or inaccessible socket fails verification without making
# the cheap /healthz liveness probe trigger a restart loop.
if timeout 5 docker exec azerothcore-admin docker inspect ac-worldserver >/dev/null 2>&1; then
    ok "Docker socket usable from admin container"
else
    fail "Docker readiness failed: cannot inspect ac-worldserver from admin container"
fi

# DNS and TCP checks are intentionally separate. A stopped database is a
# DB_DOWN readiness failure, not an admin liveness failure: the admin remains
# available to diagnose or start the AC stack.
if timeout 5 docker exec azerothcore-admin getent hosts ac-database >/dev/null 2>&1; then
    ok "ac-database DNS resolves from admin container"
    if timeout 5 docker exec azerothcore-admin python -c 'import socket; s = socket.create_connection(("ac-database", 3306), timeout=2); s.close()' >/dev/null 2>&1; then
        ok "ac-database accepts TCP connections from admin container"
    else
        fail "DB_DOWN: ac-database:3306 is unavailable from admin container (liveness remains OK)"
    fi
else
    fail "DB_DNS_UNAVAILABLE: ac-database does not resolve from admin container"
fi

# 8. systemd unit, if installed, must be enabled (spec §Verification item 7)
if [ -f /etc/systemd/system/azerothcore-admin.service ]; then
    if systemctl is-enabled --quiet azerothcore-admin.service 2>/dev/null; then
        ok "azerothcore-admin.service is enabled"
    else
        fail "azerothcore-admin.service is installed but NOT enabled"
    fi
else
    info "azerothcore-admin.service not installed (skip)"
fi

# 8b. Cloud backup (Proton Drive) -- advisory only (spec 6.11). The feature is
# optional and an outage at Proton must never fail a verify: redeploy rolls the
# admin back when this script fails. Only `info` is used here, so the PASS/FAIL
# counts cannot change. The two files are read with one bounded python3 call
# (paths as argv, never interpolated into code) that prints exactly one short
# line built from four fields -- enabled, connection, last_result, last_success --
# each checked against a fixed shape; nothing else from the files (folder name,
# error text, archive names) can reach the output. The last success is shown with
# its age (spec 6.11): minutes under 1 h, hours up to 48 h, days after that.
cloud_dir="$STACK_DIR/data/cloud"
cloud_line=""
cloud_why="python3 could not read the status files"
if command -v python3 >/dev/null 2>&1; then
    cloud_line="$(timeout 5 python3 - "$cloud_dir/config.json" "$cloud_dir/state.json" 2>/dev/null <<'PY'
import datetime
import json
import re
import sys

CONNECTIONS = {"disconnected", "connected", "vault_error"}
RESULTS = {"ok", "warning", "partial", "error"}
STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]{8,16}(Z|[+-][0-9]{2}:[0-9]{2})?")


def load(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (ValueError, RecursionError):
        raise SystemExit("invalid JSON")
    except OSError:
        raise SystemExit("cannot read file")
    if not isinstance(data, dict):
        raise SystemExit("unexpected content")
    return data


def age(stamp):
    """How long ago ``stamp`` was ("5h ago"), or None when it is not a real time."""
    text = stamp[:-1] + "+00:00" if stamp.endswith("Z") else stamp  # Python < 3.11 wants an offset
    try:
        moment = datetime.datetime.fromisoformat(text)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=datetime.timezone.utc)
        seconds = int((datetime.datetime.now(datetime.timezone.utc) - moment).total_seconds())
    except (ValueError, OverflowError):
        return None
    seconds = max(seconds, 0)  # a clock that went back: "just now"
    if seconds < 3600:
        return "%dm ago" % (seconds // 60)
    if seconds <= 48 * 3600:
        return "%dh ago" % (seconds // 3600)
    return "%dd ago" % (seconds // 86400)


try:
    config, state = load(sys.argv[1]), load(sys.argv[2])
except SystemExit as exc:
    print("status unreadable (%s)" % exc.code)
    raise SystemExit(0)

if config.get("enabled") is not True:
    print("off")
    raise SystemExit(0)

connection = state.get("connection", "disconnected")
connection = connection if isinstance(connection, str) and connection in CONNECTIONS else "unknown"
result = state.get("last_result")
if result is None:
    result = "none"
elif not (isinstance(result, str) and result in RESULTS):
    result = "unknown"
if connection == "connected":
    success = state.get("last_success")
    if success is None:
        success = "never"
    elif not (isinstance(success, str) and len(success) <= 40 and STAMP.fullmatch(success)):
        success = "unknown"
    else:
        ago = age(success)
        success = "%s (%s)" % (ago, success) if ago else "unknown"
    line = "connected, last success " + success
    if result in ("partial", "error"):
        line += ", last result " + result
    print(line)
else:
    print("%s, last result %s" % (connection, result))
PY
)" || cloud_line=""
else
    cloud_why="python3 not found"
fi
case "$cloud_line" in
    '' | *$'\n'*) info "cloud backup: status unreadable ($cloud_why)" ;;
    *) info "cloud backup: $cloud_line" ;;
esac

# 9. AC stack still functional (delegate)
# The admin install/redeploy never modifies the running AC server, so AC runtime
# noise in Errors.log (e.g. graveyard fallback lines that accumulate over hours
# of uptime) must not fail admin verification. VERIFY_ERRORS_LOG_ADVISORY
# downgrades only that check; every other AC check still gates.
if [ -x "$(dirname "$0")/../../scripts/verify-azerothcore.sh" ]; then
    info "delegating to AC verify script (exit code preserved)"
    # shellcheck disable=SC2015
    VERIFY_ERRORS_LOG_ADVISORY=1 "$(dirname "$0")/../../scripts/verify-azerothcore.sh" >/dev/null 2>&1 \
        && ok "AC verify passed" \
        || fail "AC verify reported issues"
else
    info "AC verify script not found at expected path"
fi

echo ""
echo "Summary: ${PASS} OK / ${FAIL} FAIL"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
