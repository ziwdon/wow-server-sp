#!/bin/bash
set -euo pipefail
umask 077

# Canonical AzerothCore backup. Shared by the host cron (install-azerothcore.sh
# Phase 7 copies this to the stack dir) and the admin container (bundled into
# its image at /app/scripts/backup.sh, run with STACK_DIR=/ac). Produces ONE
# archive: azerothcore-backup-<label>-<stamp>.tar.gz with all DB dumps + all
# configs + manifest.json. Labels: daily|manual|prerestore|preclear|preupgrade.
# See docs/superpowers/specs/2026-05-29-admin-backup-restore-design.md.

STACK_DIR="${STACK_DIR:-/opt/stacks/azerothcore}"
BACKUP_DIR="${BACKUP_DIR:-${STACK_DIR}/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
DB_CONTAINER="${DB_CONTAINER:-ac-database}"
LABEL="daily"

while [ $# -gt 0 ]; do
    case "$1" in
        --label) LABEL="${2:-}"; shift 2 ;;
        --label=*) LABEL="${1#*=}"; shift ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

case "$LABEL" in
    daily|manual|prerestore|preclear|preupgrade) ;;
    *) echo "Invalid --label: $LABEL (expected daily|manual|prerestore|preclear|preupgrade)" >&2; exit 2 ;;
esac

if [ "$LABEL" = "daily" ]; then
    STAMP="$(date +%F)"
else
    STAMP="$(date +%FT%H-%M-%S)"
fi
ARCHIVE="${BACKUP_DIR}/azerothcore-backup-${LABEL}-${STAMP}.tar.gz"
TMP_ARCHIVE=""

DATABASES=(acore_auth acore_characters acore_world acore_playerbots)
log() { echo "[$(date '+%F %T')] $*"; }

# shellcheck disable=SC1091
source "${STACK_DIR}/.env"

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"

# Host cron and the admin container share this mount.  One writer at a time
# prevents daily-name collisions and makes the archive publication below safe.
LOCK_FILE="${BACKUP_DIR}/.backup.lock"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    log "ERROR: another backup is already running; retry after it completes." >&2
    exit 75
fi

if ! docker inspect "${DB_CONTAINER}" >/dev/null 2>&1; then
    log "ERROR: ${DB_CONTAINER} container does not exist."
    exit 1
fi

STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"; [ -n "${TMP_ARCHIVE}" ] && rm -f "${TMP_ARCHIVE}"' EXIT
mkdir -p "${STAGE}/sql" "${STAGE}/config"

log "Starting backup (label=${LABEL})..."

for DB in "${DATABASES[@]}"; do
    if ! docker exec "${DB_CONTAINER}" mysql -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" \
        -e "USE ${DB};" >/dev/null 2>&1; then
        log "ERROR: ${DB} is missing; no backup was published." >&2
        exit 1
    fi
done

validate_v2_dump() {
    python3 - "$1" <<'PY'
import re
import sys

DATABASES = ("acore_auth", "acore_characters", "acore_world", "acore_playerbots")
HEADER_LIMIT = 4096
TAIL_LIMIT = 8192
path = sys.argv[1]
found = []
prefix = b"-- Current Database: `"

with open(path, "rb") as stream:
    at_line_start = True
    while chunk := stream.readline(HEADER_LIMIT + 1):
        if at_line_start and chunk.startswith(b"-- Current Database:"):
            if len(chunk) > HEADER_LIMIT or not chunk.endswith(b"\n"):
                raise SystemExit("oversized database section header")
            line = chunk.rstrip(b"\r\n")
            if not (line.startswith(prefix) and line.endswith(b"`") and line.count(b"`") == 2):
                raise SystemExit("malformed database section header")
            try:
                found.append(line.split(b"`", 2)[1].decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise SystemExit("unreadable database section header") from exc
        at_line_start = chunk.endswith(b"\n")

    if tuple(found) != DATABASES:
        raise SystemExit("database sections are not exactly canonical and ordered")
    size = stream.tell()
    if size <= 0:
        raise SystemExit("empty SQL stream")
    stream.seek(max(0, size - TAIL_LIMIT))
    tail = stream.read(TAIL_LIMIT)
    if re.search(rb"(?:^|\n)-- Dump completed on [^\r\n]+\s*\Z", tail) is None:
        raise SystemExit("missing terminal mysqldump completion footer")
PY
}

# One mysqldump invocation creates one InnoDB transaction snapshot spanning
# all four schemas. Separate --single-transaction invocations can otherwise
# capture cross-database rows at different moments.
docker exec "${DB_CONTAINER}" mysqldump -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" \
    --single-transaction --routines --triggers --events --databases "${DATABASES[@]}" \
    > "${STAGE}/sql/azerothcore.sql"
log "Dumped all four databases from one consistent transaction snapshot"

if ! validation_detail="$(validate_v2_dump "${STAGE}/sql/azerothcore.sql" 2>&1)"; then
    log "ERROR: SQL stream failed canonical validation: ${validation_detail}" >&2
    exit 1
fi

TMP_ARCHIVE="${BACKUP_DIR}/.${ARCHIVE##*/}.tmp.$$"

# Stage config files (we only READ from STACK_DIR — safe on the admin's ro mount).
for item in .env docker-compose.override.yml docker-compose.admin.yml; do
    if [ -f "${STACK_DIR}/${item}" ]; then
        cp -a "${STACK_DIR}/${item}" "${STAGE}/config/${item}"
    fi
done
if [ -d "${STACK_DIR}/configs" ]; then
    cp -a "${STACK_DIR}/configs" "${STAGE}/config/configs"
fi
# Generated server configs live outside configs/. They are normally identical
# to their .dist templates, but a hand edit there must never be lost.
if [ -d "${STACK_DIR}/env/dist/etc" ]; then
    mkdir -p "${STAGE}/config/etc"
    etc_count=0
    for conf in "${STACK_DIR}"/env/dist/etc/*.conf; do
        if [ -f "$conf" ]; then
            cp -a "$conf" "${STAGE}/config/etc/"
            etc_count=$((etc_count + 1))
        fi
    done
    log "Captured ${etc_count} generated server config(s) from env/dist/etc"
else
    log "No env/dist/etc under ${STACK_DIR}; no generated server configs captured."
fi
# Admin app runtime settings (maintenance schedule, login announcer toggle).
# The admin container sets ADMIN_DATA_DIR=/admin-data; host cron derives it from
# STACK_DIR (/opt/stacks/azerothcore -> /opt/stacks/azerothcore-admin/data), so a
# relocated or sandboxed STACK_DIR never points at the live admin data.
ADMIN_DATA_DIR="${ADMIN_DATA_DIR:-$(dirname "$STACK_DIR")/azerothcore-admin/data}"
if [ -d "${ADMIN_DATA_DIR}" ]; then
    mkdir -p "${STAGE}/config/admin-data"
    admin_count=0
    for item in maintenance.json presence.json; do
        if [ -f "${ADMIN_DATA_DIR}/${item}" ]; then
            cp -a "${ADMIN_DATA_DIR}/${item}" "${STAGE}/config/admin-data/${item}"
            admin_count=$((admin_count + 1))
        fi
    done
    log "Captured ${admin_count} admin setting file(s) from ${ADMIN_DATA_DIR}"
else
    log "Admin data dir ${ADMIN_DATA_DIR} not found; no admin settings captured."
fi

git_rev() {
    local dir="$1"
    if [ -d "${dir}/.git" ]; then
        git -C "${dir}" -c safe.directory='*' rev-parse HEAD 2>/dev/null || echo unknown
    else
        echo unknown
    fi
}
core_rev="$(git_rev "${STACK_DIR}")"
pb_rev="$(git_rev "${STACK_DIR}/modules/mod-playerbots")"
ahbot_rev="$(git_rev "${STACK_DIR}/modules/mod-ah-bot-plus")"
ip_rev="$(git_rev "${STACK_DIR}/modules/mod-individual-progression")"
ac_image="${DOCKER_IMAGE_TAG:-unknown}"
image_id() {
    local id
    id="$(docker image inspect -f '{{.Id}}' "$1" 2>/dev/null || true)"
    echo "${id:-unknown}"
}
# Both values are interpolated into manifest.json below: keep the first line
# and a JSON-safe charset, and never let a lookup failure abort the backup.
client_data_version="$(docker exec ac-worldserver sh -c \
    'sed -n "s/^INSTALLED_VERSION=//p" /azerothcore/env/dist/data/data-version' 2>/dev/null \
    | head -n 1 | tr -cd 'A-Za-z0-9._-' || true)"
script_sha="$(sha256sum "${BASH_SOURCE[0]}" | cut -d' ' -f1 || true)"

json_array() {
    local out="" first=1 x
    for x in "$@"; do
        if [ "$first" = 1 ]; then out="\"${x}\""; first=0; else out="${out}, \"${x}\""; fi
    done
    echo "[${out}]"
}

cat > "${STAGE}/manifest.json" <<MANIFEST
{
  "format_version": 2,
  "created_at": "$(date -u +%FT%TZ)",
  "label": "${LABEL}",
  "databases": $(json_array "${DATABASES[@]}"),
  "skipped_databases": [],
  "dump_layout": "single-multi-database",
  "git_revisions": {
    "core": "${core_rev}",
    "mod-playerbots": "${pb_rev}",
    "mod-ah-bot-plus": "${ahbot_rev}",
    "mod-individual-progression": "${ip_rev}"
  },
  "ac_image": "${ac_image}",
  "images": {
    "worldserver": "$(image_id "acore/ac-wotlk-worldserver:${ac_image}")",
    "authserver": "$(image_id "acore/ac-wotlk-authserver:${ac_image}")",
    "db-import": "$(image_id "acore/ac-wotlk-db-import:${ac_image}")",
    "client-data": "$(image_id "acore/ac-wotlk-client-data:${ac_image}")"
  },
  "client_data_version": "${client_data_version:-unknown}",
  "backup_script_sha256": "${script_sha:-unknown}",
  "stack_dir": "${STACK_DIR}"
}
MANIFEST

tar -czf "${TMP_ARCHIVE}" -C "${STAGE}" manifest.json sql config
if ! tar -tzf "${TMP_ARCHIVE}" >/dev/null 2>&1 \
    || ! tar -xOzf "${TMP_ARCHIVE}" manifest.json 2>/dev/null | grep -Eq '"format_version"[[:space:]]*:[[:space:]]*2([[:space:]]*[,}])'; then
    log "ERROR: generated archive failed validation; existing backups were left untouched." >&2
    exit 1
fi
chmod 600 "${TMP_ARCHIVE}"
mv -f "${TMP_ARCHIVE}" "${ARCHIVE}"
TMP_ARCHIVE=""
log "Wrote ${ARCHIVE}"

# Prune ONLY in daily mode (the cron's nightly run). Deletes EVERY label older
# than RETENTION_DAYS (except preupgrade), plus any legacy multi-file backups
# from before cutover. Pre-upgrade archives are rollback points and are never
# pruned; finalize the upgrade, then delete them by hand.
if [ "${LABEL}" = "daily" ]; then
    find "${BACKUP_DIR}" -name 'azerothcore-backup-*.tar.gz' ! -name 'azerothcore-backup-preupgrade-*' \
        -mtime +"${RETENTION_DAYS}" -delete
    find "${BACKUP_DIR}" -name '*.sql' -mtime +"${RETENTION_DAYS}" -delete
    find "${BACKUP_DIR}" -name 'azerothcore-config-*.tar.gz' -mtime +"${RETENTION_DAYS}" -delete
    find "${BACKUP_DIR}" -name 'git-revisions-*.txt' -mtime +"${RETENTION_DAYS}" -delete
    log "Pruned archives older than ${RETENTION_DAYS} days."
fi

log "Backup complete."
