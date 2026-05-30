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

DATABASES="acore_auth acore_characters acore_world acore_playerbots"
log() { echo "[$(date '+%F %T')] $*"; }

# shellcheck disable=SC1091
source "${STACK_DIR}/.env"

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"

if ! docker inspect "${DB_CONTAINER}" >/dev/null 2>&1; then
    log "ERROR: ${DB_CONTAINER} container does not exist."
    exit 1
fi

STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT
mkdir -p "${STAGE}/sql" "${STAGE}/config"

log "Starting backup (label=${LABEL})..."

dumped=""
skipped=""
for DB in ${DATABASES}; do
    if docker exec "${DB_CONTAINER}" mysql -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" \
            -e "USE ${DB};" >/dev/null 2>&1; then
        docker exec "${DB_CONTAINER}" mysqldump -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" \
            --single-transaction --routines --triggers --events "${DB}" \
            > "${STAGE}/sql/${DB}.sql"
        dumped="${dumped} ${DB}"
        log "Dumped ${DB}"
    else
        skipped="${skipped} ${DB}"
        log "WARNING: ${DB} not present; skipping."
    fi
done

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
    for x in $1; do
        if [ "$first" = 1 ]; then out="\"${x}\""; first=0; else out="${out}, \"${x}\""; fi
    done
    echo "[${out}]"
}

cat > "${STAGE}/manifest.json" <<MANIFEST
{
  "format_version": 1,
  "created_at": "$(date -u +%FT%TZ)",
  "label": "${LABEL}",
  "databases": $(json_array "${dumped}"),
  "skipped_databases": $(json_array "${skipped}"),
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

tar -czf "${ARCHIVE}" -C "${STAGE}" manifest.json sql config
chmod 600 "${ARCHIVE}"
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
