#!/bin/bash
set -euo pipefail
umask 077

# Canonical AzerothCore backup. Shared by the host cron (install-azerothcore.sh
# Phase 7 copies this to the stack dir) and the admin container (bundled into
# its image at /app/scripts/backup.sh, run with STACK_DIR=/ac). Produces ONE
# archive: azerothcore-backup-<label>-<stamp>.tar.gz with all DB dumps + all
# configs + manifest.json. See docs/superpowers/specs/2026-05-29-admin-backup-restore-design.md.

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
    daily|manual|prerestore|preclear) ;;
    *) echo "Invalid --label: $LABEL (expected daily|manual|prerestore|preclear)" >&2; exit 2 ;;
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

# One mysqldump invocation creates one InnoDB transaction snapshot spanning
# all four schemas. Separate --single-transaction invocations can otherwise
# capture cross-database rows at different moments.
docker exec "${DB_CONTAINER}" mysqldump -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" \
    --single-transaction --routines --triggers --events --databases "${DATABASES[@]}" \
    > "${STAGE}/sql/azerothcore.sql"
log "Dumped all four databases from one consistent transaction snapshot"

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
# than RETENTION_DAYS, plus any legacy multi-file backups from before cutover.
if [ "${LABEL}" = "daily" ]; then
    find "${BACKUP_DIR}" -name 'azerothcore-backup-*.tar.gz' -mtime +"${RETENTION_DAYS}" -delete
    find "${BACKUP_DIR}" -name '*.sql' -mtime +"${RETENTION_DAYS}" -delete
    find "${BACKUP_DIR}" -name 'azerothcore-config-*.tar.gz' -mtime +"${RETENTION_DAYS}" -delete
    find "${BACKUP_DIR}" -name 'git-revisions-*.txt' -mtime +"${RETENTION_DAYS}" -delete
    log "Pruned archives older than ${RETENTION_DAYS} days."
fi

log "Backup complete."
