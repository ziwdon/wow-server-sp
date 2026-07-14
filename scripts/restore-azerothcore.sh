#!/bin/bash
set -euo pipefail

# Fresh-machine disaster recovery restore. Run this after reinstalling the
# AzerothCore stack, using a backup archive produced by scripts/backup.sh.

STACK_DIR="/opt/stacks/azerothcore"
DB_CONTAINER="ac-database"
WORLD_CONTAINER="ac-worldserver"
DATABASES=(
    acore_auth
    acore_characters
    acore_world
    acore_playerbots
)
YES=false
ARCHIVE=""
STOP_TIMEOUT_SECONDS="${RESTORE_STOP_TIMEOUT_SECONDS:-60}"
STOP_POLL_SECONDS="${RESTORE_STOP_POLL_SECONDS:-2}"
READY_TIMEOUT_SECONDS="${RESTORE_READY_TIMEOUT_SECONDS:-300}"
READY_POLL_SECONDS="${RESTORE_READY_POLL_SECONDS:-5}"

if [ "${EUID:-$(id -u)}" -eq 0 ]; then
    echo "ERROR: Do not run this script with sudo/root." >&2
    echo "Run it as the same normal user that ran the installer." >&2
    exit 1
fi

usage() {
    cat <<USAGE
Usage: ./scripts/restore-azerothcore.sh <archive.tar.gz> [--stack-dir DIR] [--yes]

Options:
  --stack-dir DIR  AzerothCore stack directory (default: /opt/stacks/azerothcore)
  --yes            Do not prompt for confirmation
  -h, --help       Show this help

Run against a freshly reinstalled stack as your normal user, not with sudo.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --stack-dir)
            STACK_DIR="${2:-}"
            shift 2
            ;;
        --stack-dir=*)
            STACK_DIR="${1#*=}"
            shift
            ;;
        --yes)
            YES=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        -*)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
        *)
            if [ -n "$ARCHIVE" ]; then
                echo "Unexpected extra archive argument: $1" >&2
                usage >&2
                exit 2
            fi
            ARCHIVE="$1"
            shift
            ;;
    esac
done

if [ -z "$ARCHIVE" ]; then
    echo "ERROR: Missing archive argument." >&2
    usage >&2
    exit 2
fi
if [ -z "$STACK_DIR" ]; then
    echo "ERROR: --stack-dir cannot be empty." >&2
    exit 2
fi
if [ ! -f "$ARCHIVE" ]; then
    echo "ERROR: Archive not found: $ARCHIVE" >&2
    exit 1
fi
if [ ! -f "${STACK_DIR}/.env" ]; then
    echo "ERROR: Fresh stack .env not found: ${STACK_DIR}/.env" >&2
    exit 1
fi

# shellcheck disable=SC1091
source "${STACK_DIR}/.env"

log() {
    echo "[$(date '+%F %T')] $*"
}

validate_archive_members() {
    local listing member
    if ! listing="$(tar -tzf "$ARCHIVE" 2>&1)"; then
        if printf '%s\n' "$listing" | grep -q "Member name contains '..'"; then
            echo "ERROR: Unsafe archive member in $ARCHIVE" >&2
        else
            echo "ERROR: Could not read archive member list from $ARCHIVE" >&2
            printf '%s\n' "$listing" >&2
        fi
        return 1
    fi
    while IFS= read -r member; do
        [ -n "$member" ] || continue
        case "$member" in
            /*|..|../*|*/..|*/../*)
                echo "ERROR: Unsafe archive member: $member" >&2
                return 1
                ;;
        esac
    done <<< "$listing"
}

if [ -z "${DOCKER_DB_ROOT_PASSWORD:-}" ]; then
    echo "ERROR: DOCKER_DB_ROOT_PASSWORD is not set in ${STACK_DIR}/.env" >&2
    exit 1
fi

STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT

validate_archive_members
tar -xzf "$ARCHIVE" -C "$STAGE"

if [ ! -f "${STAGE}/manifest.json" ]; then
    echo "ERROR: Archive is missing manifest.json" >&2
    exit 1
fi

validate_manifest() {
    local format
    if ! command -v python3 >/dev/null 2>&1; then
        echo "ERROR: Restore manifest validation requires python3." >&2
        return 1
    fi
    if ! format="$(python3 - "${STAGE}/manifest.json" <<'PY'
import json
import sys

DATABASES = ["acore_auth", "acore_characters", "acore_world", "acore_playerbots"]


def fail(message):
    print(f"ERROR: Archive manifest is incompatible: {message}", file=sys.stderr)
    raise SystemExit(1)


try:
    with open(sys.argv[1], encoding="utf-8") as manifest_file:
        manifest = json.load(manifest_file)
except UnicodeDecodeError:
    fail("not valid UTF-8")
except (OSError, json.JSONDecodeError):
    fail("not valid JSON")

if not isinstance(manifest, dict):
    fail("top level must be an object")

format_version = manifest.get("format_version")
if isinstance(format_version, bool) or not isinstance(format_version, int):
    fail("format_version is missing or is not an integer")
if format_version not in (1, 2):
    fail(f"format_version {format_version} is not supported")


def require_exact_inventory(required):
    if "databases" not in manifest:
        if required:
            fail("database inventory is missing")
        return
    if manifest["databases"] != DATABASES:
        fail("database inventory must exactly match the canonical databases")


def require_no_skips(required):
    if "skipped_databases" not in manifest:
        if required:
            fail("skipped_databases is missing")
        return
    if manifest["skipped_databases"] != []:
        fail("declares skipped databases")


if format_version == 1:
    # Every repository-produced v1 archive declares its canonical inventory.
    # Do not allow a manifest with omitted fields to bypass the compatibility
    # preflight before destructive restore work begins.
    require_exact_inventory(True)
    require_no_skips(True)
else:
    require_exact_inventory(True)
    require_no_skips(True)
    if manifest.get("dump_layout") != "single-multi-database":
        fail("dump_layout must be single-multi-database for format_version 2")

print(format_version)
PY
    )"; then
        return 1
    fi
    FORMAT_VERSION="$format"
}

validate_dump_set() {
    local db sql_file problems=""
    case "$FORMAT_VERSION" in
        2)
            sql_file="${STAGE}/sql/azerothcore.sql"
            if [ ! -s "$sql_file" ] || ! tail -n 20 "$sql_file" | grep -q -- '-- Dump completed'; then
                echo "ERROR: v2 multi-database dump failed pre-flight validation." >&2
                return 1
            fi
            if ! python3 - "$sql_file" <<'PY'
import sys

DATABASES = ["acore_auth", "acore_characters", "acore_world", "acore_playerbots"]

try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        sections = [
            line.rstrip("\r\n").split("`", 2)[1]
            for line in stream
            if line.rstrip("\r\n").startswith("-- Current Database: `")
            and line.rstrip("\r\n").endswith("`")
            and line.rstrip("\r\n").count("`") == 2
        ]
except (OSError, UnicodeDecodeError):
    print("ERROR: v2 multi-database dump has unreadable database sections.", file=sys.stderr)
    raise SystemExit(1)

if sections != DATABASES:
    print(
        "ERROR: v2 multi-database dump database sections must exactly match "
        "the canonical databases once each.",
        file=sys.stderr,
    )
    raise SystemExit(1)
PY
            then
                return 1
            fi
            return 0
            ;;
        1) ;;
    esac
    for db in "${DATABASES[@]}"; do
        sql_file="${STAGE}/sql/${db}.sql"
        if [ ! -s "$sql_file" ]; then
            problems="${problems} ${db}(missing-or-empty)"
        elif ! tail -n 20 "$sql_file" | grep -q -- '-- Dump completed'; then
            problems="${problems} ${db}(incomplete)"
        fi
    done
    if [ -n "$problems" ]; then
        echo "ERROR: Archive database dumps failed pre-flight validation:${problems}" >&2
        return 1
    fi
}

worldserver_status() {
    local status
    if status="$(docker inspect --format='{{.State.Status}}' "${WORLD_CONTAINER}" 2>/dev/null)"; then
        # Docker always emits a state for an extant container. Treat an empty
        # successful response as absent so older Docker stubs remain harmless.
        printf '%s\n' "${status:-missing}"
        return 0
    fi
    # `docker inspect` exits nonzero both for a missing container and for a
    # broken daemon connection. Prove the daemon is reachable before treating
    # this as the harmless missing-container case.
    if ! docker info >/dev/null 2>&1; then
        echo "ERROR: Cannot inspect ${WORLD_CONTAINER}; Docker is unavailable. Restore was not started." >&2
        return 1
    fi
    printf '%s\n' "missing"
}

stop_worldserver_for_restore() {
    local status elapsed=0
    if ! status="$(worldserver_status)"; then
        return 1
    fi
    case "$status" in
        missing|created|dead|exited)
            log "${WORLD_CONTAINER} is already ${status}; safe to restore."
            return 0
            ;;
    esac

    log "Stopping ${WORLD_CONTAINER} (up to ${STOP_TIMEOUT_SECONDS}s)..."
    if ! docker stop --time "${STOP_TIMEOUT_SECONDS}" "${WORLD_CONTAINER}" >/dev/null; then
        status="$(worldserver_status || true)"
        case "$status" in
            missing|created|dead|exited)
                log "${WORLD_CONTAINER} stopped while Docker stop returned an error (${status})."
                return 0
                ;;
        esac
        echo "ERROR: Could not stop ${WORLD_CONTAINER} (current state: ${status:-unknown}). Restore was not started." >&2
        return 1
    fi

    while [ "$elapsed" -le "$STOP_TIMEOUT_SECONDS" ]; do
        if ! status="$(worldserver_status)"; then
            return 1
        fi
        case "$status" in
            missing|created|dead|exited)
                log "${WORLD_CONTAINER} is ${status}; safe to restore."
                return 0
                ;;
        esac
        sleep "$STOP_POLL_SECONDS"
        elapsed=$((elapsed + STOP_POLL_SECONDS))
    done

    echo "ERROR: Timed out waiting for ${WORLD_CONTAINER} to stop (current state: ${status}). Restore was not started." >&2
    return 1
}

recreate_and_verify_worldserver() {
    local started_at status elapsed=0
    log "Recreating ${WORLD_CONTAINER} from the restored Compose configuration..."
    if ! (cd "${STACK_DIR}" && docker compose up -d --force-recreate --no-deps "${WORLD_CONTAINER}"); then
        echo "ERROR: Could not recreate ${WORLD_CONTAINER} from restored Compose files." >&2
        return 1
    fi
    if ! started_at="$(docker inspect --format='{{.State.StartedAt}}' "${WORLD_CONTAINER}" 2>/dev/null)" || [ -z "$started_at" ]; then
        echo "ERROR: Could not determine the recreated ${WORLD_CONTAINER} start time." >&2
        return 1
    fi

    while [ "$elapsed" -le "$READY_TIMEOUT_SECONDS" ]; do
        if ! status="$(worldserver_status)"; then
            return 1
        fi
        case "$status" in
            running)
                if docker logs --since "$started_at" "${WORLD_CONTAINER}" 2>&1 | grep -q 'WORLD: World Initialized'; then
                    log "${WORLD_CONTAINER} initialized after ${elapsed}s."
                    return 0
                fi
                ;;
            created|restarting)
                ;;
            *)
                echo "ERROR: ${WORLD_CONTAINER} entered ${status} during restore startup." >&2
                docker logs --tail 100 "${WORLD_CONTAINER}" >&2 || true
                return 1
                ;;
        esac
        sleep "$READY_POLL_SECONDS"
        elapsed=$((elapsed + READY_POLL_SECONDS))
    done

    echo "ERROR: ${WORLD_CONTAINER} did not reach World Initialized within ${READY_TIMEOUT_SECONDS}s." >&2
    docker logs --tail 100 "${WORLD_CONTAINER}" >&2 || true
    return 1
}

# Validate all destructive inputs before stopping the server or replacing data.
validate_manifest
validate_dump_set
rm -f "${STAGE}/config/configs/mysql/custom.cnf"

echo "AzerothCore restore preview"
echo "  Archive:   $ARCHIVE"
echo "  Stack dir: $STACK_DIR"
echo "  Manifest:  ${STAGE}/manifest.json"
echo ""
sed -n '1,80p' "${STAGE}/manifest.json"
echo ""
echo "This will overwrite restored configs except ${STACK_DIR}/.env and configs/mysql/custom.cnf."
echo "This will drop and recreate any backed-up AzerothCore databases found in the archive."

if [ "$YES" != true ]; then
    read -r -p "Type RESTORE to continue: " confirm
    if [ "$confirm" != "RESTORE" ]; then
        echo "Aborted."
        exit 1
    fi
fi

if ! fresh_realmlist="$(
    docker exec "${DB_CONTAINER}" mysql -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" -N -B \
        -e "SELECT address FROM acore_auth.realmlist LIMIT 1;" | head -n1
)"; then
    echo "ERROR: Unable to read fresh realmlist address from ${DB_CONTAINER}." >&2
    exit 1
fi
if [ -z "$fresh_realmlist" ]; then
    echo "ERROR: Fresh realmlist address is empty; aborting before restore." >&2
    exit 1
fi

stop_worldserver_for_restore

custom_cnf_backup=""
if [ -f "${STACK_DIR}/configs/mysql/custom.cnf" ]; then
    custom_cnf_backup="$(mktemp)"
    if ! cp -a "${STACK_DIR}/configs/mysql/custom.cnf" "$custom_cnf_backup"; then
        echo "ERROR: Could not copy restored configuration files. The server remains stopped; re-run restore with a known-good archive." >&2
        exit 1
    fi
fi

if [ -d "${STAGE}/config/configs" ]; then
    mkdir -p "${STACK_DIR}/configs"
    if ! cp -a "${STAGE}/config/configs/." "${STACK_DIR}/configs/"; then
        echo "ERROR: Could not copy restored configuration files. The server remains stopped; re-run restore with a known-good archive." >&2
        exit 1
    fi
fi
if [ -n "$custom_cnf_backup" ]; then
    mkdir -p "${STACK_DIR}/configs/mysql"
    if ! cp -a "$custom_cnf_backup" "${STACK_DIR}/configs/mysql/custom.cnf"; then
        echo "ERROR: Could not copy restored configuration files. The server remains stopped; re-run restore with a known-good archive." >&2
        exit 1
    fi
fi
if [ -f "${STAGE}/config/docker-compose.override.yml" ]; then
    if ! cp -a "${STAGE}/config/docker-compose.override.yml" "${STACK_DIR}/docker-compose.override.yml"; then
        echo "ERROR: Could not copy restored configuration files. The server remains stopped; re-run restore with a known-good archive." >&2
        exit 1
    fi
fi
if [ -f "${STAGE}/config/docker-compose.admin.yml" ]; then
    if ! cp -a "${STAGE}/config/docker-compose.admin.yml" "${STACK_DIR}/docker-compose.admin.yml"; then
        echo "ERROR: Could not copy restored configuration files. The server remains stopped; re-run restore with a known-good archive." >&2
        exit 1
    fi
fi

if [ "$FORMAT_VERSION" = "2" ]; then
    log "Restoring v2 consistent multi-database snapshot..."
    for DB in "${DATABASES[@]}"; do
        docker exec "${DB_CONTAINER}" mysql -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" \
            -e "DROP DATABASE IF EXISTS ${DB};"
    done
    if ! docker exec -i "${DB_CONTAINER}" mysql -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" \
        < "${STAGE}/sql/azerothcore.sql"; then
        echo "ERROR: v2 multi-database import failed. The server remains stopped; re-run restore with a known-good archive." >&2
        exit 1
    fi
else
for DB in "${DATABASES[@]}"; do
    sql_file="${STAGE}/sql/${DB}.sql"
    if [ ! -f "$sql_file" ]; then
        log "Skipping ${DB}; no sql/${DB}.sql in archive."
        continue
    fi

    log "Restoring ${DB}..."
    if ! docker exec "${DB_CONTAINER}" mysql -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" \
        -e "DROP DATABASE IF EXISTS ${DB}; CREATE DATABASE ${DB};"; then
        echo "ERROR: Could not drop and recreate ${DB}. The server remains stopped; re-run restore with a known-good archive." >&2
        exit 1
    fi
    if ! docker exec -i "${DB_CONTAINER}" mysql -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" "${DB}" < "$sql_file"; then
        echo "ERROR: Import of ${DB} failed. The server remains stopped; re-run restore with a known-good archive." >&2
        exit 1
    fi
done
fi

if [ -n "$fresh_realmlist" ]; then
    log "Re-applying fresh realmlist address: ${fresh_realmlist}"
    docker exec "${DB_CONTAINER}" mysql -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" \
        -e "UPDATE acore_auth.realmlist SET address='${fresh_realmlist}';"
fi

recreate_and_verify_worldserver

log "Restore complete."
