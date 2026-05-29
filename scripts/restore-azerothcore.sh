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

log "Stopping ${WORLD_CONTAINER} if running..."
docker stop "${WORLD_CONTAINER}" >/dev/null 2>&1 || true

custom_cnf_backup=""
if [ -f "${STACK_DIR}/configs/mysql/custom.cnf" ]; then
    custom_cnf_backup="$(mktemp)"
    cp -a "${STACK_DIR}/configs/mysql/custom.cnf" "$custom_cnf_backup"
fi

if [ -d "${STAGE}/config/configs" ]; then
    mkdir -p "${STACK_DIR}/configs"
    cp -a "${STAGE}/config/configs/." "${STACK_DIR}/configs/"
fi
if [ -n "$custom_cnf_backup" ]; then
    mkdir -p "${STACK_DIR}/configs/mysql"
    cp -a "$custom_cnf_backup" "${STACK_DIR}/configs/mysql/custom.cnf"
fi
if [ -f "${STAGE}/config/docker-compose.override.yml" ]; then
    cp -a "${STAGE}/config/docker-compose.override.yml" "${STACK_DIR}/docker-compose.override.yml"
fi
if [ -f "${STAGE}/config/docker-compose.admin.yml" ]; then
    cp -a "${STAGE}/config/docker-compose.admin.yml" "${STACK_DIR}/docker-compose.admin.yml"
fi

for DB in "${DATABASES[@]}"; do
    sql_file="${STAGE}/sql/${DB}.sql"
    if [ ! -f "$sql_file" ]; then
        log "Skipping ${DB}; no sql/${DB}.sql in archive."
        continue
    fi

    log "Restoring ${DB}..."
    docker exec "${DB_CONTAINER}" mysql -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" \
        -e "DROP DATABASE IF EXISTS ${DB}; CREATE DATABASE ${DB};"
    docker exec -i "${DB_CONTAINER}" mysql -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" "${DB}" < "$sql_file"
done

if [ -n "$fresh_realmlist" ]; then
    log "Re-applying fresh realmlist address: ${fresh_realmlist}"
    docker exec "${DB_CONTAINER}" mysql -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" \
        -e "UPDATE acore_auth.realmlist SET address='${fresh_realmlist}';"
fi

log "Starting ${WORLD_CONTAINER}..."
docker start "${WORLD_CONTAINER}" >/dev/null 2>&1 || (
    cd "${STACK_DIR}"
    docker compose up -d "${WORLD_CONTAINER}"
)

log "Restore complete."
