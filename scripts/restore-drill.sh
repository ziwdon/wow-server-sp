#!/bin/bash
set -euo pipefail

# ============================================================================
# restore-drill.sh
# ----------------------------------------------------------------------------
# Prove a backup archive restores, without touching the live stack. Imports
# the archive's SQL into a throwaway, resource-capped MySQL container that has
# no published ports, no bind mounts, and either no network or an --internal
# Docker network, then checks the table inventory against the dump itself.
#
# Never touches ac-* containers, /opt/stacks (other than reading the archive),
# or the Docker socket from inside a container. The archive is streamed to the
# drill container over stdin.
#
# Usage:
#   restore-drill.sh ARCHIVE [--compare-live]
#   restore-drill.sh ARCHIVE --keep --name NAME --network NET --password-file FILE
#     (rehearsal mode: leave the imported container running on NET)
# Exit: 0 drill passed, 1 drill failed or refused, 2 usage.
# ============================================================================

if [ "$EUID" -eq 0 ]; then
    echo "ERROR: do not run as root; docker is invoked as your user (docker group)." >&2
    exit 1
fi

DATABASES=(acore_auth acore_characters acore_world acore_playerbots)
MYSQL_IMAGE="${DRILL_MYSQL_IMAGE:-mysql:8.4}"
LIVE_DB_CONTAINER="ac-database"
NAME="acup-drill-db"
NETWORK=""
KEEP=false
COMPARE_LIVE=false
PASSWORD_FILE=""
ARCHIVE=""
WORK=""
STARTED=false
SUCCESS=false
FAILED=0

usage() {
    sed -n '/^# Usage:/,/^# Exit:/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --compare-live) COMPARE_LIVE=true; shift ;;
        --keep) KEEP=true; shift ;;
        --name) NAME="${2:?}"; shift 2 ;;
        --network) NETWORK="${2:?}"; shift 2 ;;
        --password-file) PASSWORD_FILE="${2:?}"; shift 2 ;;
        -h|--help) usage ;;
        -*) echo "Unknown argument: $1" >&2; usage ;;
        *) [ -z "$ARCHIVE" ] || usage; ARCHIVE="$1"; shift ;;
    esac
done

[ -n "$ARCHIVE" ] || usage
[ -f "$ARCHIVE" ] || { echo "ERROR: archive not found: $ARCHIVE" >&2; exit 1; }
case "$NAME" in
    acup-*) ;;
    *) echo "ERROR: drill container names must start with 'acup-' (got '$NAME')." >&2; exit 1 ;;
esac
if [ "$KEEP" = true ] && { [ -z "$NETWORK" ] || [ -z "$PASSWORD_FILE" ]; }; then
    echo "ERROR: --keep requires --network and --password-file." >&2
    exit 2
fi

log() { echo "[$(date '+%F %T')] $*"; }
ok() { echo "  [OK]   $*"; }
bad() { echo "  [FAIL] $*"; FAILED=1; }
info() { echo "  [INFO] $*"; }

# The drill container survives only a --keep run that reached "DRILL PASSED";
# every other exit path (failure, set -e abort, signal) removes it, plus the
# password file that would otherwise be useless without it.
cleanup() {
    if [ "$STARTED" = true ] && ! { [ "$KEEP" = true ] && [ "$SUCCESS" = true ]; }; then
        docker rm -f -v "$NAME" >/dev/null 2>&1 || true
        if [ "$KEEP" = true ] && [ -n "$PASSWORD_FILE" ]; then
            rm -f "$PASSWORD_FILE"
        fi
    fi
    [ -n "$WORK" ] && rm -rf "$WORK"
    return 0
}
trap cleanup EXIT

if docker container inspect "$NAME" >/dev/null 2>&1; then
    echo "ERROR: container '$NAME' already exists (leftover drill?). Remove it with: docker rm -f -v $NAME" >&2
    exit 1
fi
if [ -n "$NETWORK" ]; then
    internal="$(docker network inspect -f '{{.Internal}}' "$NETWORK" 2>/dev/null || echo missing)"
    if [ "$internal" != "true" ]; then
        echo "ERROR: network '$NETWORK' must exist and be --internal (got: $internal)." >&2
        exit 1
    fi
fi

WORK="$(mktemp -d)"
chmod 700 "$WORK"

log "Reading archive manifest..."
if ! layout="$(python3 -I - "$ARCHIVE" <<'PY'
import json
import sys
import tarfile

with tarfile.open(sys.argv[1], "r:gz") as tf:
    manifest = json.load(tf.extractfile("manifest.json"))
    names = set(tf.getnames())
fmt = manifest.get("format_version")
dbs = manifest.get("databases")
if dbs != ["acore_auth", "acore_characters", "acore_world", "acore_playerbots"]:
    raise SystemExit(f"manifest databases are not canonical: {dbs}")
if manifest.get("skipped_databases") not in ([], None):
    raise SystemExit("archive is partial")
if fmt == 2 and "sql/azerothcore.sql" in names:
    print("v2")
elif fmt == 1 and all(f"sql/{db}.sql" in names for db in dbs):
    print("v1")
else:
    raise SystemExit(f"unsupported archive layout (format_version={fmt})")
PY
)"; then
    echo "ERROR: archive manifest/layout check failed." >&2
    exit 1
fi
ok "archive layout ${layout}"

PASSWORD="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)"
if [ -n "$PASSWORD_FILE" ]; then
    (umask 077; printf '%s\n' "$PASSWORD" > "$PASSWORD_FILE")
fi

run_args=(
    --detach --name "$NAME"
    --memory "${DRILL_MEMORY:-1536m}" --cpus "${DRILL_CPUS:-2}"
    -e MYSQL_ROOT_PASSWORD="$PASSWORD"
)
if [ -n "$NETWORK" ]; then
    run_args+=(--network "$NETWORK")
else
    run_args+=(--network none)
fi
log "Starting isolated drill database '$NAME'..."
# Set before `docker run`: it can create the container and still fail to start
# it. The name was verified free above, so cleanup can only remove our own.
STARTED=true
docker run "${run_args[@]}" "$MYSQL_IMAGE" \
    --innodb-buffer-pool-size=512M --skip-log-bin --max-allowed-packet=256M >/dev/null

# Fail closed unless the container is exactly as isolated as intended.
isolation="$(docker inspect -f '{{.HostConfig.NetworkMode}}|{{json .HostConfig.PortBindings}}|{{range .Mounts}}{{.Type}}:{{.Source}};{{end}}' "$NAME")"
expected_net="${NETWORK:-none}"
case "$isolation" in
    "${expected_net}|{}|volume:"*|"${expected_net}|null|volume:"*) ;;
    *) bad "drill container is not isolated: $isolation"; exit 1 ;;
esac
case "$isolation" in
    *bind:*|*docker.sock*|*/opt/stacks*) bad "drill container has a host mount: $isolation"; exit 1 ;;
esac
ok "drill container isolated (network=${expected_net}, no ports, no host mounts)"

# TCP on loopback, never the socket: the mysql image's entrypoint first runs a
# socket-only temporary server (--skip-networking) and then restarts, so a
# socket probe can pass and the import then die. Loopback exists under
# --network none, and the image's default MYSQL_ROOT_HOST=% provides root@'%'.
dmysql() { docker exec -i -e MYSQL_PWD="$PASSWORD" "$NAME" mysql -uroot -h127.0.0.1 --protocol=TCP "$@"; }
for _ in $(seq 1 90); do
    if dmysql -N -e 'SELECT 1' >/dev/null 2>&1; then break; fi
    sleep 2
done
dmysql -N -e 'SELECT 1' >/dev/null 2>&1 || { bad "drill database did not become ready"; exit 1; }

started_at=$(date +%s)
import_member() {
    local member="$1"; shift
    local rc
    set +e
    tar -xOzf "$ARCHIVE" "$member" | dmysql "$@" 2> "$WORK/import.err"
    rc=("${PIPESTATUS[@]}")
    set -e
    if [ "${rc[0]}" -ne 0 ] || [ "${rc[1]}" -ne 0 ]; then
        bad "import of ${member} failed (tar=${rc[0]} mysql=${rc[1]}): $(grep -v 'Using a password' "$WORK/import.err" | head -3)"
        return 1
    fi
}
if [ "$layout" = v2 ]; then
    import_member sql/azerothcore.sql || exit 1
else
    for db in "${DATABASES[@]}"; do
        dmysql -e "CREATE DATABASE \`${db}\`" 2>/dev/null
        import_member "sql/${db}.sql" "$db" || exit 1
    done
fi
ok "all four databases imported in $(( $(date +%s) - started_at ))s"

# Table inventory: tables created in the drill vs CREATE TABLE statements per
# database section in the dump itself.
python3 -I - "$ARCHIVE" "$layout" > "$WORK/expected.tsv" <<'PY'
import re
import sys
import tarfile

archive, layout = sys.argv[1:3]
dbs = ["acore_auth", "acore_characters", "acore_world", "acore_playerbots"]
counts = dict.fromkeys(dbs, 0)
section = re.compile(rb"^-- Current Database: `([^`]+)`")
with tarfile.open(archive, "r:gz") as tf:
    members = ["sql/azerothcore.sql"] if layout == "v2" else [f"sql/{db}.sql" for db in dbs]
    for member in members:
        current = None if layout == "v2" else member[4:-4]
        for line in tf.extractfile(member):
            m = section.match(line)
            if m:
                current = m.group(1).decode()
            elif line.startswith(b"CREATE TABLE ") and current in counts:
                counts[current] += 1
for db in dbs:
    print(f"{db}\t{counts[db]}")
PY
dmysql -N -B -e "SELECT table_schema, COUNT(*) FROM information_schema.tables WHERE table_schema IN ('acore_auth','acore_characters','acore_world','acore_playerbots') AND table_type='BASE TABLE' GROUP BY table_schema ORDER BY table_schema" 2>/dev/null > "$WORK/actual.tsv"
if diff <(sort "$WORK/expected.tsv") <(sort "$WORK/actual.tsv") >/dev/null; then
    ok "table inventory matches the dump: $(tr '\t\n' '= ' < "$WORK/actual.tsv")"
else
    bad "table inventory differs (expected | actual):"
    diff <(sort "$WORK/expected.tsv") <(sort "$WORK/actual.tsv") | sed 's/^/         /' || true
fi

for spec in acore_auth.account acore_auth.realmlist acore_characters.characters acore_world.creature acore_world.updates; do
    n="$(dmysql -N -B -e "SELECT COUNT(*) FROM ${spec}" 2>/dev/null || echo error)"
    if [[ "$n" =~ ^[0-9]+$ ]] && [ "$n" -gt 0 ]; then
        ok "${spec} has ${n} rows"
    else
        bad "${spec} is empty or unreadable (${n})"
    fi
done

if [ "$COMPARE_LIVE" = true ]; then
    log "Comparing exact row counts with the live database (read-only)..."
    dmysql -N -B -e "SELECT CONCAT(table_schema,'.',table_name) FROM information_schema.tables WHERE table_schema LIKE 'acore\\_%' AND table_type='BASE TABLE'" 2>/dev/null > "$WORK/tables.raw"
    # Table names come from the archive and are sent, unescaped, to the live
    # database as root: allow only plain identifiers, and make the live session
    # read-only so only COUNT queries can ever run there.
    grep -E '^[A-Za-z0-9_]+\.[A-Za-z0-9_]+$' "$WORK/tables.raw" > "$WORK/tables.txt" || true
    [ -s "$WORK/tables.txt" ] || bad "no comparable tables found in the drill database"
    {
        echo 'SET SESSION TRANSACTION READ ONLY;'
        awk -F. '{printf "SELECT '\''%s.%s'\'', COUNT(*) FROM `%s`.`%s`;\n",$1,$2,$1,$2}' "$WORK/tables.txt"
    } > "$WORK/count.sql"
    dmysql -N -B < "$WORK/count.sql" 2>/dev/null > "$WORK/drill-counts.tsv"
    docker exec -i "$LIVE_DB_CONTAINER" sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -N -B 2>/dev/null' \
        < "$WORK/count.sql" > "$WORK/live-counts.tsv"
    python3 -I - "$WORK/drill-counts.tsv" "$WORK/live-counts.tsv" <<'PY'
import sys
drill = dict(l.rstrip("\n").split("\t") for l in open(sys.argv[1]) if l.strip())
live = dict(l.rstrip("\n").split("\t") for l in open(sys.argv[2]) if l.strip())
diff = sorted(k for k in drill if drill[k] != live.get(k))
print(f"  [INFO] {len(drill) - len(diff)}/{len(drill)} tables have identical row counts to live")
for k in diff:
    print(f"  [INFO]   {k}: backup={drill[k]} live={live.get(k, 'missing')}")
print("  [INFO] differences are expected only in tables that changed since the backup was taken")
PY
fi

if [ "$FAILED" -ne 0 ]; then
    echo "RESULT: DRILL FAILED"
    exit 1
fi
if [ "$KEEP" = true ]; then
    info "keeping '$NAME' on network '$NETWORK' (password in $PASSWORD_FILE)"
fi
SUCCESS=true
echo "RESULT: DRILL PASSED"
