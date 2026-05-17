#!/bin/bash
# install-azerothcore.sh
# Single-file installer for AzerothCore + mod-playerbots + mod-ah-bot-plus.
#
# Design: single-script model; per-phase checkpointing in
# ~/.azerothcore-install-state; eleven interactive tunable prompts up front;
# three manual pauses (Tailscale auth, account creation, AH bot character
# creation); UFW + systemd conditional on user prompt.

set -euo pipefail

# This installer uses sudo internally and stores per-user state/config under $HOME.
# Running it with sudo would put state under /root, create root-owned files, and
# can break Docker group detection.
if [ "${EUID}" -eq 0 ]; then
    echo "ERROR: Do not run this installer with sudo or as root." >&2
    echo "Run it as your normal user; the script will ask for sudo when needed." >&2
    exit 2
fi

# ============================================================================
# Constants
# ============================================================================

STACK_DIR="/opt/stacks/azerothcore"
STATE_FILE="${HOME}/.azerothcore-install-state"
CONFIG_FILE="${HOME}/.azerothcore-install-config"

UNIX_TS="$(date +%s)"
LOG_FILE="/tmp/azerothcore-install-${UNIX_TS}.log"
RELOCATED_LOG_FILE="${STACK_DIR}/logs/install-${UNIX_TS}.log"

KEEPALIVE_PID=""
CURRENT_PHASE=""
CURRENT_PHASE_DESC=""
PROMPT_RESULT=""

# Phase ordering — must stay in execution order. Used for --resume-from
# index comparison and for the main run loop.
PHASES=(
    "0.0|Pre-flight checks"
    "0.1|OS version check"
    "0.2|System packages (apt)"
    "0.3|Docker Engine install + verify"
    "0.4|Tailscale install + authentication"
    "0.5|Directory structure"
    "1|Clone AzerothCore + modules"
    "2.1|Create .env"
    "2.2|Create data directories"
    "2.3|Clean Playerbots custom SQL duplicates"
    "2.4|MySQL tuning config"
    "2.5|docker-compose.override.yml"
    "2.6|Compose validation"
    "3|Docker compose build"
    "3.1|Install module conf templates"
    "4|First run + DB init + client data download"
    "pause-2|Account creation (GM + AHBOT) via worldserver console"
    "5|Networking — Tailscale realmlist"
    "5.1|UFW firewall (conditional)"
    "pause-3|AH bot character creation in WoW client"
    "6.1.4|Write GUID(s) into mod_ahbot.conf"
    "6.1.5|Worldserver restart + AH verify"
    "7|Backup script + cron"
    "8|Systemd auto-start (conditional)"
)

# ============================================================================
# Logging
# ============================================================================

mkdir -p "$(dirname "$LOG_FILE")"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"

# All output is tee'd to the log file. Keep original stdout/stderr open so
# relocating the log later does not nest tee processes and duplicate output.
exec 3>&1 4>&2
start_logging_to() {
    local target="$1"
    exec > >(tee -a "$target" >&3) 2> >(tee -a "$target" >&4)
}
start_logging_to "$LOG_FILE"

# Relocate log to /opt/stacks/azerothcore/logs/ once that directory exists.
relocate_log_if_possible() {
    if [ "$LOG_FILE" != "$RELOCATED_LOG_FILE" ] && [ -d "${STACK_DIR}/logs" ]; then
        mv "$LOG_FILE" "$RELOCATED_LOG_FILE"
        LOG_FILE="$RELOCATED_LOG_FILE"
        start_logging_to "$LOG_FILE"
        echo "Log relocated to: $LOG_FILE"
    fi
}

# ============================================================================
# Traps
# ============================================================================

cleanup_keepalive() {
    if [ -n "$KEEPALIVE_PID" ] && kill -0 "$KEEPALIVE_PID" 2>/dev/null; then
        kill "$KEEPALIVE_PID" 2>/dev/null || true
    fi
}

on_error() {
    local exit_code=$?
    local cmd="${BASH_COMMAND}"
    local lineno="${BASH_LINENO[0]:-?}"
    echo ""
    echo "════════════════════════════════════════════════════════════════"
    echo "✗ FAILED at ${CURRENT_PHASE:-(startup)} (line ${lineno}): ${cmd}"
    echo "  Phase: ${CURRENT_PHASE_DESC:-(none)}"
    echo "  Exit code: ${exit_code}"
    echo "  Log: ${LOG_FILE}"
    if [ -n "$CURRENT_PHASE" ]; then
        echo "  Resume with: $0 --resume-from=${CURRENT_PHASE}"
    fi
    echo "════════════════════════════════════════════════════════════════"
    cleanup_keepalive
    exit "$exit_code"
}

on_exit() {
    cleanup_keepalive
}

trap on_error ERR
trap on_exit EXIT

clean_exit() {
    # Exit without the error banner. Used for "user must take action and re-run".
    local code="${1:-0}"
    trap - ERR
    cleanup_keepalive
    trap - EXIT
    exit "$code"
}

# ============================================================================
# Helper: wait for a short-lived (run-once) container to exit, with timeout.
# ============================================================================
# Replaces `docker wait <c>`, which blocks forever if the container is wedged
# (download stall, deadlock, etc.). We instead poll docker inspect with an
# upper bound and a periodic progress dot.
#
# Args:
#   $1  container name
#   $2  timeout in seconds
#   $3  human-readable label for messages
# Returns:
#   0 on clean exit (exit code 0)
#   1 on non-zero exit, missing container, or timeout
wait_for_init_container() {
    local container="$1"
    local timeout="$2"
    local label="$3"
    local elapsed=0
    local poll_interval=10
    while true; do
        local state
        state="$(docker inspect --format='{{.State.Status}}' "$container" 2>/dev/null || echo missing)"
        case "$state" in
            exited)
                local code
                code="$(docker inspect --format='{{.State.ExitCode}}' "$container" 2>/dev/null || echo 1)"
                if [ "$code" = "0" ]; then
                    echo ""
                    echo "${label} (${container}) exited successfully."
                    return 0
                fi
                echo ""
                echo "ERROR: ${label} (${container}) exited with code ${code}"
                docker logs --tail 300 "$container" 2>&1 || true
                return 1
                ;;
            running|created|restarting)
                : # still going — keep polling
                ;;
            missing)
                echo ""
                echo "ERROR: ${label} container '${container}' does not exist."
                return 1
                ;;
            *)
                echo ""
                echo "ERROR: ${label} (${container}) in unexpected state: ${state}"
                docker logs --tail 100 "$container" 2>&1 || true
                return 1
                ;;
        esac
        if [ "$elapsed" -ge "$timeout" ]; then
            echo ""
            echo "ERROR: ${label} (${container}) did not finish within ${timeout}s."
            echo "Last 100 lines of its log:"
            docker logs --tail 100 "$container" 2>&1 || true
            return 1
        fi
        printf '.'
        sleep "$poll_interval"
        elapsed=$((elapsed + poll_interval))
    done
}

# ============================================================================
# Helper: compute "--scale svc=0" args for services we don't want running.
# ============================================================================
# The upstream mod-playerbots/azerothcore-wotlk compose file inherits services
# from acore-docker (phpmyadmin, ac-eluna-ts-dev) that this stack doesn't need
# and that would otherwise eat ports/resources. We scale them to 0 at every
# `docker compose up`. Done dynamically so the script still works if a future
# upstream removes one of them — `--scale` errors on unknown services.
#
# IMPORTANT: returns *nothing* (zero output) when there are no scale args, so
# that `mapfile -t` produces a zero-length array. Naively running
# `printf '%s\n' "${args[@]}"` with an empty array still prints a single
# newline (printf cycles the format once), which mapfile reads as one empty
# element — that would expand to a literal empty arg to docker compose and
# break it. The explicit guard avoids that.
compose_scale_args() {
    local args=()
    local svc services
    services="$(docker compose config --services 2>/dev/null || echo "")"
    for svc in phpmyadmin ac-eluna-ts-dev; do
        if echo "$services" | grep -qx "$svc"; then
            args+=("--scale" "$svc=0")
        fi
    done
    if [ "${#args[@]}" -gt 0 ]; then
        printf '%s\n' "${args[@]}"
    fi
}


# ============================================================================
# Helper: mod-playerbots SQL cleanup and runtime validation.
# ============================================================================
# mod-playerbots has used more than one SQL source layout over time. Current
# revisions generally use modules/mod-playerbots/data/sql, while older docs and
# revisions refer to modules/mod-playerbots/sql.
#
# IMPORTANT: do not copy mod-playerbots SQL into data/sql/custom. The Docker
# db-import image already sees module SQL in the source tree. If the same SQL
# basename is also present in data/sql/custom, AzerothCore aborts with:
# "Duplicate filename ... every name needs to be unique".
#
# This helper only removes Playerbots SQL files previously copied into
# data/sql/custom by older/broken installer runs. It deliberately matches only
# basenames that exist under the mod-playerbots SQL tree, so unrelated custom SQL
# is left alone.
#
# The separate acore_playerbots schema is intentionally NOT staged here; it is
# initialized by the mod-playerbots updater on worldserver start, so that updater
# must remain enabled.
cleanup_playerbots_custom_sql_files() {
    local root db kind src dst f base removed=0 found_root=0

    for root in modules/mod-playerbots/data/sql modules/mod-playerbots/sql; do
        if [ ! -d "$root" ]; then
            echo "Playerbots SQL root not present: $root"
            continue
        fi

        found_root=1
        echo "Scanning playerbots SQL root for custom-SQL cleanup: $root"
        for db in auth characters world; do
            case "$db" in
                auth)       dst="data/sql/custom/db_auth" ;;
                characters) dst="data/sql/custom/db_characters" ;;
                world)      dst="data/sql/custom/db_world" ;;
            esac
            mkdir -p "$dst"

            for kind in base updates; do
                src="$root/${db}/${kind}"
                if compgen -G "${src}/*.sql" > /dev/null; then
                    for f in "${src}"/*.sql; do
                        base="$(basename "$f")"
                        if [ -f "${dst}/${base}" ]; then
                            echo "Removing previously staged Playerbots SQL duplicate: ${dst}/${base}"
                            rm -f "${dst}/${base}"
                            removed=1
                        fi
                    done
                fi
            done
        done
    done

    if [ "$found_root" -eq 0 ]; then
        echo "WARNING: no mod-playerbots SQL root was found."
        echo "If db-import later misses module SQL, re-check the mod-playerbots SQL directory layout."
    elif [ "$removed" -eq 1 ]; then
        echo "Playerbots custom SQL cleanup complete."
    else
        echo "No previously staged Playerbots SQL duplicates found in data/sql/custom."
    fi

    echo "Playerbots SQL will be loaded from modules/mod-playerbots directly, not copied to data/sql/custom."
}

assert_no_playerbots_sql_duplicates_in_custom() {
    local custom_file base match fail=0

    while IFS= read -r custom_file; do
        [ -n "$custom_file" ] || continue
        base="$(basename "$custom_file")"
        match="$(find modules/mod-playerbots/data/sql modules/mod-playerbots/sql \
            -type f -name "$base" 2>/dev/null | head -1 || true)"

        if [ -n "$match" ]; then
            echo "ERROR: duplicate Playerbots SQL basename visible to db-import:"
            echo "  custom: $custom_file"
            echo "  module: $match"
            fail=1
        fi
    done < <(find data/sql/custom/db_auth data/sql/custom/db_characters data/sql/custom/db_world \
        -type f -name '*.sql' 2>/dev/null | sort)

    if [ "$fail" -ne 0 ]; then
        echo "Remove the duplicate custom SQL files above, then re-run this phase."
        exit 1
    fi
}

ensure_playerbots_updater_enabled_in_compose_override() {
    local file="${STACK_DIR}/docker-compose.override.yml"

    if [ ! -f "$file" ]; then
        echo "ERROR: $file is missing; cannot verify Playerbots updater setting."
        exit 1
    fi

    if grep -qE '^[[:space:]]*AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES:[[:space:]]*"?0"?[[:space:]]*$' "$file"; then
        echo "Updating AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES from 0 to 1 in docker-compose.override.yml."
        sed -i -E 's/^([[:space:]]*AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES:[[:space:]]*)"?0"?[[:space:]]*$/\1"1"/' "$file"
    elif ! grep -qE '^[[:space:]]*AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES:' "$file"; then
        echo "Adding AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES=1 to docker-compose.override.yml."
        sed -i '/AC_AI_PLAYERBOT_RANDOM_BOT_AUTOLOGIN:/a\      AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES: "1"' "$file"
    fi

    if ! grep -qE '^[[:space:]]*AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES:[[:space:]]*"?1"?[[:space:]]*$' "$file"; then
        echo "ERROR: AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES must be set to 1."
        echo "If it is 0, acore_playerbots may exist but its tables will not be initialized."
        exit 1
    fi
}

playerbots_schema_missing_tables() {
    docker exec ac-database mysql -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" -N -B -e "
SELECT required.table_name
FROM (
  SELECT 'playerbots_custom_strategy' AS table_name UNION ALL
  SELECT 'playerbots_db_store' UNION ALL
  SELECT 'playerbots_random_bots' UNION ALL
  SELECT 'playerbots_equip_cache' UNION ALL
  SELECT 'playerbots_travelnode' UNION ALL
  SELECT 'playerbots_travelnode_link' UNION ALL
  SELECT 'playerbots_travelnode_path' UNION ALL
  SELECT 'playerbots_item_info_cache'
) AS required
LEFT JOIN information_schema.tables t
  ON t.table_schema='acore_playerbots' AND t.table_name=required.table_name
WHERE t.table_name IS NULL;
" 2>/dev/null || true
}

playerbots_table_count() {
    docker exec ac-database mysql -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" -N -B -e \
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='acore_playerbots';" \
        2>/dev/null || echo 0
}

verify_playerbots_schema_now() {
    local table_count missing
    table_count="$(playerbots_table_count | tail -1)"
    missing="$(playerbots_schema_missing_tables)"

    if ! [[ "$table_count" =~ ^[0-9]+$ ]]; then
        table_count=0
    fi

    if [ "$table_count" -le 0 ]; then
        echo "  ✗ acore_playerbots exists but has no tables"
        return 1
    fi

    if [ -n "$missing" ]; then
        echo "  ✗ acore_playerbots is missing required table(s):"
        echo "$missing" | sed 's/^/    - /'
        return 1
    fi

    echo "  ✓ acore_playerbots has ${table_count} table(s), including required Playerbots tables"
    return 0
}

wait_for_playerbots_schema() {
    local timeout="${1:-300}"
    local elapsed=0
    local poll_interval=10
    local status

    echo "Waiting for Playerbots updater to initialize acore_playerbots tables (timeout ${timeout}s)..."
    while [ "$elapsed" -le "$timeout" ]; do
        if verify_playerbots_schema_now >/tmp/ac-playerbots-schema-check.out 2>&1; then
            cat /tmp/ac-playerbots-schema-check.out
            rm -f /tmp/ac-playerbots-schema-check.out
            return 0
        fi

        status="$(docker inspect --format='{{.State.Status}}' ac-worldserver 2>/dev/null || echo missing)"
        if [ "$status" != "running" ] && [ "$status" != "restarting" ]; then
            echo "  ✗ ac-worldserver is ${status}; Playerbots schema initialization cannot complete."
            cat /tmp/ac-playerbots-schema-check.out 2>/dev/null || true
            rm -f /tmp/ac-playerbots-schema-check.out
            docker logs --tail 200 ac-worldserver 2>&1 || true
            return 1
        fi

        sleep "$poll_interval"
        elapsed=$((elapsed + poll_interval))
    done

    echo "  ✗ Playerbots schema was not initialized within ${timeout}s."
    cat /tmp/ac-playerbots-schema-check.out 2>/dev/null || true
    rm -f /tmp/ac-playerbots-schema-check.out
    docker logs --tail 200 ac-worldserver 2>&1 || true
    return 1
}

wait_for_running_container() {
    local container="$1"
    local timeout="$2"
    local label="$3"
    local elapsed=0
    local poll_interval=5
    local status

    echo "Waiting for ${label} (${container}) to be running (timeout ${timeout}s)..."
    while [ "$elapsed" -le "$timeout" ]; do
        status="$(docker inspect --format='{{.State.Status}}' "$container" 2>/dev/null || echo missing)"
        if [ "$status" = "running" ]; then
            echo "${label} (${container}) is running."
            return 0
        fi
        if [ "$status" = "exited" ] || [ "$status" = "dead" ]; then
            echo "ERROR: ${label} (${container}) is ${status}."
            docker logs --tail 200 "$container" 2>&1 || true
            return 1
        fi
        sleep "$poll_interval"
        elapsed=$((elapsed + poll_interval))
    done

    echo "ERROR: ${label} (${container}) did not reach running state within ${timeout}s."
    docker logs --tail 200 "$container" 2>&1 || true
    return 1
}

write_mysql_custom_cnf() {
    local target="${STACK_DIR}/configs/mysql/custom.cnf"

    mkdir -p "$(dirname "$target")"
    cat > "$target" <<EOF
[mysqld]
# Single-server home setup: no MySQL replication/binlog needed.
# Reduces write pressure significantly under playerbot load.
skip-log-bin

# Tuned for 16 GB RAM with other services present — raise to 8G if memory allows.
innodb_buffer_pool_size        = ${INNODB_BUFFER_POOL_SIZE}
# Pool instance count is derived from the pool size in GB so each instance
# stays at the ~1 GB threshold above which MySQL actually honors instances.
innodb_buffer_pool_instances   = ${INNODB_BUFFER_POOL_INSTANCES}
innodb_io_capacity             = 500
innodb_io_capacity_max         = 2500
innodb_use_fdatasync           = ON
innodb_log_buffer_size         = 32M
transaction_isolation          = READ-COMMITTED

# Reduce disk flush frequency — acceptable for a home server with backups.
innodb_flush_log_at_trx_commit = 2
sync_binlog                    = 0
EOF
    chmod 0644 "$target"
}

mysql_custom_cnf_is_expected() {
    local target="${STACK_DIR}/configs/mysql/custom.cnf"

    [ -f "$target" ] || return 1
    grep -qE '^\[mysqld\][[:space:]]*$' "$target" || return 1
    grep -qE '^[[:space:]]*skip-log-bin[[:space:]]*$' "$target" || return 1
    grep -qE "^[[:space:]]*innodb_buffer_pool_size[[:space:]]*=[[:space:]]*${INNODB_BUFFER_POOL_SIZE}[[:space:]]*$" "$target" || return 1
    grep -qE "^[[:space:]]*innodb_buffer_pool_instances[[:space:]]*=[[:space:]]*${INNODB_BUFFER_POOL_INSTANCES}[[:space:]]*$" "$target" || return 1
    grep -qE '^[[:space:]]*innodb_io_capacity[[:space:]]*=[[:space:]]*500[[:space:]]*$' "$target" || return 1
    grep -qE '^[[:space:]]*innodb_io_capacity_max[[:space:]]*=[[:space:]]*2500[[:space:]]*$' "$target" || return 1
    grep -qE '^[[:space:]]*innodb_use_fdatasync[[:space:]]*=[[:space:]]*ON[[:space:]]*$' "$target" || return 1
    grep -qE '^[[:space:]]*innodb_log_buffer_size[[:space:]]*=[[:space:]]*32M[[:space:]]*$' "$target" || return 1
    grep -qE '^[[:space:]]*transaction_isolation[[:space:]]*=[[:space:]]*READ-COMMITTED[[:space:]]*$' "$target" || return 1
    grep -qE '^[[:space:]]*innodb_flush_log_at_trx_commit[[:space:]]*=[[:space:]]*2[[:space:]]*$' "$target" || return 1
    grep -qE '^[[:space:]]*sync_binlog[[:space:]]*=[[:space:]]*0[[:space:]]*$' "$target" || return 1
}

ensure_mysql_custom_cnf_file() {
    local target="${STACK_DIR}/configs/mysql/custom.cnf"
    local backup="${target}.bak.${UNIX_TS}"

    mkdir -p "$(dirname "$target")"

    # Docker creates a directory on the host if a file bind-mount source is
    # missing when the container is first created. A directory at this path is
    # readable inside the container, but MySQL ignores it because it is not a
    # .cnf file. Replace that broken state before ac-database is recreated.
    if [ -d "$target" ]; then
        echo "WARNING: $target is a directory; replacing it with a MySQL option file."
        sudo rm -rf "$target"
    elif [ -e "$target" ] && [ ! -f "$target" ]; then
        echo "WARNING: $target exists but is not a regular file; moving it aside."
        sudo mv "$target" "${target}.bad.${UNIX_TS}"
    fi

    if mysql_custom_cnf_is_expected; then
        echo "MySQL tuning config already exists and matches expected values: $target"
    else
        if [ -f "$target" ]; then
            echo "Existing MySQL tuning config differs from expected values; backing it up to $backup"
            cp -a "$target" "$backup"
        fi
        write_mysql_custom_cnf
    fi

    if ! mysql_custom_cnf_is_expected; then
        echo "ERROR: $target was written but does not contain the expected MySQL tuning values."
        exit 1
    fi

    if [ -d "$target" ] || [ ! -f "$target" ]; then
        echo "ERROR: $target must be a regular file, not a directory or special file."
        exit 1
    fi

    chmod 0644 "$target"
}

get_mysql_variable() {
    local var="$1"
    docker exec ac-database mysql -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" -N -B -e \
        "SHOW VARIABLES LIKE '${var}';" 2>/dev/null | awk '{print $2}' | tail -1
}

verify_mysql_tuning_active() {
    local fail=0
    local expected_buffer_g expected_buffer_bytes actual

    echo "VERIFY MySQL tuning is active:"

    if docker exec ac-database sh -c 'test -f /etc/mysql/conf.d/custom.cnf && test -r /etc/mysql/conf.d/custom.cnf'; then
        echo "  ✓ /etc/mysql/conf.d/custom.cnf is a readable file in ac-database"
        docker exec ac-database sh -c 'ls -l /etc/mysql/conf.d/custom.cnf; sed -n "1,80p" /etc/mysql/conf.d/custom.cnf' \
            2>/dev/null | sed 's/^/    /'
    else
        echo "  ✗ /etc/mysql/conf.d/custom.cnf is not a readable regular file in ac-database"
        docker exec ac-database sh -c 'ls -ld /etc/mysql/conf.d /etc/mysql/conf.d/custom.cnf 2>/dev/null || true' \
            2>/dev/null | sed 's/^/    /' || true
        fail=1
    fi

    expected_buffer_g="${INNODB_BUFFER_POOL_SIZE%G}"
    expected_buffer_bytes=$((expected_buffer_g * 1024 * 1024 * 1024))

    actual="$(get_mysql_variable innodb_buffer_pool_size)"
    if [ "$actual" = "$expected_buffer_bytes" ]; then
        echo "  ✓ innodb_buffer_pool_size=${actual}"
    else
        echo "  ✗ innodb_buffer_pool_size=${actual:-<unset>} expected ${expected_buffer_bytes} (${INNODB_BUFFER_POOL_SIZE})"
        fail=1
    fi

    actual="$(get_mysql_variable innodb_io_capacity)"
    if [ "$actual" = "500" ]; then
        echo "  ✓ innodb_io_capacity=500"
    else
        echo "  ✗ innodb_io_capacity=${actual:-<unset>} expected 500"
        fail=1
    fi

    actual="$(get_mysql_variable innodb_io_capacity_max)"
    if [ "$actual" = "2500" ]; then
        echo "  ✓ innodb_io_capacity_max=2500"
    else
        echo "  ✗ innodb_io_capacity_max=${actual:-<unset>} expected 2500"
        fail=1
    fi

    actual="$(get_mysql_variable innodb_use_fdatasync)"
    if [ "$actual" = "ON" ]; then
        echo "  ✓ innodb_use_fdatasync=ON"
    else
        echo "  ✗ innodb_use_fdatasync=${actual:-<unset>} expected ON"
        fail=1
    fi

    actual="$(get_mysql_variable innodb_buffer_pool_instances)"
    if [ "$actual" = "$INNODB_BUFFER_POOL_INSTANCES" ]; then
        echo "  ✓ innodb_buffer_pool_instances=${actual}"
    else
        echo "  ✗ innodb_buffer_pool_instances=${actual:-<unset>} expected ${INNODB_BUFFER_POOL_INSTANCES}"
        fail=1
    fi

    # MySQL reports innodb_log_buffer_size in bytes; 32M = 32 * 1024 * 1024.
    actual="$(get_mysql_variable innodb_log_buffer_size)"
    if [ "$actual" = "33554432" ]; then
        echo "  ✓ innodb_log_buffer_size=${actual} (32M)"
    else
        echo "  ✗ innodb_log_buffer_size=${actual:-<unset>} expected 33554432 (32M)"
        fail=1
    fi

    actual="$(get_mysql_variable transaction_isolation)"
    if [ -z "$actual" ]; then
        actual="$(get_mysql_variable tx_isolation)"
    fi
    if [ "$actual" = "READ-COMMITTED" ]; then
        echo "  ✓ transaction_isolation=READ-COMMITTED"
    else
        echo "  ✗ transaction_isolation=${actual:-<unset>} expected READ-COMMITTED"
        fail=1
    fi

    actual="$(get_mysql_variable log_bin)"
    if [ "$actual" = "OFF" ]; then
        echo "  ✓ log_bin=OFF"
    else
        echo "  ✗ log_bin=${actual:-<unset>} expected OFF"
        fail=1
    fi

    actual="$(get_mysql_variable sync_binlog)"
    if [ "$actual" = "0" ]; then
        echo "  ✓ sync_binlog=0"
    else
        echo "  ✗ sync_binlog=${actual:-<unset>} expected 0"
        fail=1
    fi

    actual="$(get_mysql_variable innodb_flush_log_at_trx_commit)"
    if [ "$actual" = "2" ]; then
        echo "  ✓ innodb_flush_log_at_trx_commit=2"
    else
        echo "  ✗ innodb_flush_log_at_trx_commit=${actual:-<unset>} expected 2"
        fail=1
    fi

    if [ "$fail" -ne 0 ]; then
        echo "  Last ac-database log lines, for MySQL option-file diagnostics:"
        docker logs --tail 120 ac-database 2>&1 | sed 's/^/    /' || true
    fi

    return "$fail"
}

# ==========================================================================
# Helper: patch live module .conf values used by the playerbot performance
# profile. This is intentionally separate from docker-compose environment
# variables so the final playerbot profile remains visible/editable in the
# mounted module config file.
# ==========================================================================
escape_conf_key_regex() {
    printf '%s' "$1" | sed 's/[.[\*^$()+?{}|]/\\&/g'
}

set_conf_key() {
    local key="$1"
    local value="$2"
    local file="$3"
    local escaped_key
    escaped_key="$(escape_conf_key_regex "$key")"

    # Canonicalize rather than in-place substitute: if earlier runs or manual
    # edits left the same key more than once, replacing every match would keep
    # duplicate active settings and AzerothCore would warn about duplicate keys.
    # Remove all commented/uncommented occurrences of the key, then append one
    # authoritative value.
    sed -i -E "/^[[:space:]]*#?[[:space:]]*${escaped_key}[[:space:]]*=/d" "$file"
    printf '\n%s = %s\n' "$key" "$value" >> "$file"
}

require_conf_key_once() {
    local key="$1"
    local value="$2"
    local file="$3"
    local escaped_key count expected
    escaped_key="$(escape_conf_key_regex "$key")"
    expected="${key} = ${value}"

    count="$(grep -Ec "^[[:space:]]*${escaped_key}[[:space:]]*=" "$file" || true)"
    if [ "$count" != "1" ]; then
        echo "ERROR: ${key} appears ${count} time(s) in ${file}; expected exactly 1."
        grep -nE "^[[:space:]]*${escaped_key}[[:space:]]*=" "$file" || true
        exit 1
    fi

    if ! grep -qFx "$expected" "$file"; then
        echo "ERROR: Expected exact config line not found in ${file}:"
        echo "  ${expected}"
        grep -nE "^[[:space:]]*${escaped_key}[[:space:]]*=" "$file" || true
        exit 1
    fi
}

playerbots_conf_path() {
    local conf dist

    for conf in configs/modules/playerbots.conf configs/modules/mod_playerbots.conf; do
        if [ -f "$conf" ]; then
            printf '%s\n' "$conf"
            return 0
        fi
    done

    for dist in configs/modules/playerbots.conf.dist configs/modules/mod_playerbots.conf.dist; do
        if [ -f "$dist" ]; then
            conf="${dist%.dist}"
            cp "$dist" "$conf"
            echo "Created live Playerbots config: $conf" >&2
            printf '%s\n' "$conf"
            return 0
        fi
    done

    echo "ERROR: Could not find playerbots.conf(.dist) or mod_playerbots.conf(.dist) in configs/modules." >&2
    return 1
}

ensure_playerbots_performance_config() {
    local conf
    conf="$(playerbots_conf_path)"

    echo "Applying Playerbots performance profile to: $conf"

    # BotActiveAlone = 0 turns off background AI for bots with no nearby real
    # player; the BotActiveAloneForceWhen* family below brings them back to
    # life as soon as a player is in radius / zone / friend list. Combined
    # with DisabledWithoutRealPlayer, this keeps the realm cheap when empty
    # and lively when someone is online.
    set_conf_key "AiPlayerbot.BotActiveAlone" "0" "$conf"
    set_conf_key "AiPlayerbot.botActiveAloneSmartScale" "1" "$conf"
    set_conf_key "AiPlayerbot.botActiveAloneSmartScaleWhenMinLevel" "1" "$conf"
    set_conf_key "AiPlayerbot.botActiveAloneSmartScaleWhenMaxLevel" "80" "$conf"

    # Reduce writes and background activity when the realm is empty.
    set_conf_key "AiPlayerbot.DisabledWithoutRealPlayer" "1" "$conf"

    # Random bot pool size. The compose override also passes the same value
    # as AC_AI_PLAYERBOT_MIN/MAX_RANDOM_BOTS; keeping it in playerbots.conf
    # makes the chosen size discoverable from the live config file as well.
    set_conf_key "AiPlayerbot.MinRandomBots" "${PLAYERBOT_COUNT}" "$conf"
    set_conf_key "AiPlayerbot.MaxRandomBots" "${PLAYERBOT_COUNT}" "$conf"

    # Rotate which bots are logged in over time so the same characters do
    # not always appear in the world.
    set_conf_key "AiPlayerbot.EnablePeriodicOnlineOffline" "1" "$conf"
    set_conf_key "AiPlayerbot.PeriodicOnlineOfflineRatio" "2.0" "$conf"

    # Activation triggers: a bot becomes fully active when a real player is
    # within 150 yards, in the same zone, or marked as the bot's friend.
    # Same map alone or same guild alone do NOT trigger activation here.
    set_conf_key "AiPlayerbot.BotActiveAloneForceWhenInRadius" "150" "$conf"
    set_conf_key "AiPlayerbot.BotActiveAloneForceWhenInZone" "1" "$conf"
    set_conf_key "AiPlayerbot.BotActiveAloneForceWhenInMap" "0" "$conf"
    set_conf_key "AiPlayerbot.BotActiveAloneForceWhenIsFriend" "1" "$conf"
    set_conf_key "AiPlayerbot.BotActiveAloneForceWhenInGuild" "0" "$conf"

    # Keep Playerbots DB threading conservative for a 6-core / 12-thread home box.
    set_conf_key "PlayerbotsDatabase.WorkerThreads" "1" "$conf"
    set_conf_key "PlayerbotsDatabase.SynchThreads" "2" "$conf"

    for expected in \
        "AiPlayerbot.BotActiveAlone = 0" \
        "AiPlayerbot.botActiveAloneSmartScale = 1" \
        "AiPlayerbot.botActiveAloneSmartScaleWhenMinLevel = 1" \
        "AiPlayerbot.botActiveAloneSmartScaleWhenMaxLevel = 80" \
        "AiPlayerbot.DisabledWithoutRealPlayer = 1" \
        "AiPlayerbot.MinRandomBots = ${PLAYERBOT_COUNT}" \
        "AiPlayerbot.MaxRandomBots = ${PLAYERBOT_COUNT}" \
        "AiPlayerbot.EnablePeriodicOnlineOffline = 1" \
        "AiPlayerbot.PeriodicOnlineOfflineRatio = 2.0" \
        "AiPlayerbot.BotActiveAloneForceWhenInRadius = 150" \
        "AiPlayerbot.BotActiveAloneForceWhenInZone = 1" \
        "AiPlayerbot.BotActiveAloneForceWhenInMap = 0" \
        "AiPlayerbot.BotActiveAloneForceWhenIsFriend = 1" \
        "AiPlayerbot.BotActiveAloneForceWhenInGuild = 0" \
        "PlayerbotsDatabase.WorkerThreads = 1" \
        "PlayerbotsDatabase.SynchThreads = 2"
    do
        if ! grep -qFx "$expected" "$conf"; then
            echo "ERROR: Failed to set expected Playerbots config line: $expected"
            exit 1
        fi
    done

    grep -E "^(AiPlayerbot\.(BotActiveAlone|botActiveAloneSmartScale|botActiveAloneSmartScaleWhenMinLevel|botActiveAloneSmartScaleWhenMaxLevel|DisabledWithoutRealPlayer|MinRandomBots|MaxRandomBots|EnablePeriodicOnlineOffline|PeriodicOnlineOfflineRatio|BotActiveAloneForceWhenInRadius|BotActiveAloneForceWhenInZone|BotActiveAloneForceWhenInMap|BotActiveAloneForceWhenIsFriend|BotActiveAloneForceWhenInGuild)|PlayerbotsDatabase\.(WorkerThreads|SynchThreads))[[:space:]]*=" "$conf"
}

worldserver_playerbots_fatal_pattern() {
    printf '%s' "Could not prepare statements of the Playerbots database|Table 'acore_playerbots\.|Unknown database 'acore_playerbots'"
}

worldserver_has_playerbots_fatal_logs() {
    local since="${1:-}"
    local logs_args=()

    if [ -n "$since" ]; then
        logs_args+=(--since "$since")
    fi

    docker logs "${logs_args[@]}" ac-worldserver 2>&1 | grep -qiE "$(worldserver_playerbots_fatal_pattern)"
}

print_worldserver_playerbots_fatal_logs() {
    local since="${1:-}"
    local logs_args=()

    if [ -n "$since" ]; then
        logs_args+=(--since "$since")
    fi

    docker logs "${logs_args[@]}" ac-worldserver 2>&1 \
        | grep -iE "$(worldserver_playerbots_fatal_pattern)" \
        | tail -30 || true
}

# ============================================================================
# Helper: safe idempotent repository cloning.
# ============================================================================

stack_dir_has_only_logs() {
    [ -d "$STACK_DIR" ] || return 1
    if find "$STACK_DIR" -mindepth 1 \
        ! -path "${STACK_DIR}/logs" \
        ! -path "${STACK_DIR}/logs/*" \
        -print -quit | grep -q .
    then
        return 1
    fi
    return 0
}

show_existing_path_summary() {
    local path="$1"
    if [ -d "$path" ]; then
        find "$path" -maxdepth 2 -mindepth 1 -print | sort | sed 's/^/  /' | head -80
    else
        ls -la "$path" 2>/dev/null || true
    fi
}

verify_git_checkout() {
    local dest="$1"
    local branch="$2"
    local remote_pattern="$3"
    local label="$4"
    local remote branch_name

    remote="$(cd "$dest" && git remote get-url origin 2>/dev/null || echo "")"
    branch_name="$(cd "$dest" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"

    if ! echo "$remote" | grep -q "$remote_pattern"; then
        echo "ERROR: existing ${label} checkout has unexpected origin: $remote"
        echo "Expected origin containing: $remote_pattern"
        exit 1
    fi
    if [ "$branch_name" != "$branch" ]; then
        echo "ERROR: existing ${label} checkout is on branch '$branch_name', expected '$branch'."
        exit 1
    fi
}

clone_or_verify_repo() {
    local url="$1"
    local branch="$2"
    local dest="$3"
    local label="$4"
    local remote_pattern="$5"

    if [ -d "${dest}/.git" ]; then
        verify_git_checkout "$dest" "$branch" "$remote_pattern" "$label"
        echo "${label} already cloned and verified; skipping."
        return 0
    fi

    if [ -e "$dest" ]; then
        if [ -d "$dest" ] && ! find "$dest" -mindepth 1 -print -quit | grep -q .; then
            rmdir "$dest"
        else
            echo "ERROR: ${label} destination exists but is not a git repository: $dest"
            echo "Contents:"
            show_existing_path_summary "$dest"
            echo "Move or remove that path, then re-run this phase."
            exit 1
        fi
    fi

    mkdir -p "$(dirname "$dest")"
    git clone "$url" --branch="$branch" --depth 1 "$dest"
}

clone_or_verify_core_repo() {
    local url="https://github.com/mod-playerbots/azerothcore-wotlk.git"
    local branch="Playerbot"
    local remote_pattern="mod-playerbots/azerothcore-wotlk"

    if [ -d "${STACK_DIR}/.git" ]; then
        verify_git_checkout "$STACK_DIR" "$branch" "$remote_pattern" "AzerothCore"
        echo "Existing AzerothCore checkout looks correct; skipping clone of core repo."
        return 0
    fi

    mkdir -p "$(dirname "$STACK_DIR")"

    if [ -d "$STACK_DIR" ]; then
        if stack_dir_has_only_logs; then
            echo "${STACK_DIR} exists but only contains logs/; cloning core into a temporary directory and preserving logs."
            local tmp_clone
            tmp_clone="$(dirname "$STACK_DIR")/.azerothcore-clone-${UNIX_TS}"
            rm -rf "$tmp_clone"
            git clone "$url" --branch="$branch" --depth 1 "$tmp_clone"
            cp -a "${tmp_clone}/." "$STACK_DIR/"
            rm -rf "$tmp_clone"
        else
            echo "ERROR: ${STACK_DIR} exists, is not a git checkout, and contains non-log files."
            echo "Contents:"
            show_existing_path_summary "$STACK_DIR"
            echo "Use --adopt for a valid existing install, or --force-fresh only if you intend to wipe it."
            exit 1
        fi
    else
        git clone "$url" --branch="$branch" --depth 1 "$STACK_DIR"
    fi
}

set_config_value() {
    local key="$1"
    local value="$2"
    local file="$3"
    umask 077
    touch "$file"
    chmod 600 "$file"
    if grep -qE "^${key}=" "$file"; then
        grep -vE "^${key}=" "$file" > "${file}.tmp" || true
        printf '%s=%s\n' "$key" "$value" >> "${file}.tmp"
        mv "${file}.tmp" "$file"
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
    chmod 600 "$file"
}

# ============================================================================
# Argument parsing
# ============================================================================

RESUME_FROM=""
FORCE_FRESH=false
ADOPT=false

for arg in "$@"; do
    case "$arg" in
        --resume-from=*|--force-from=*)
            # --force-from is aliased to --resume-from (per spec F#7).
            RESUME_FROM="${arg#*=}"
            ;;
        --force-fresh)
            FORCE_FRESH=true
            ;;
        --adopt)
            ADOPT=true
            ;;
        -h|--help)
            cat <<'HELP'
Usage: ./install-azerothcore.sh [OPTIONS]

  --resume-from=<phase_id>  Force re-run starting at the given phase
  --force-from=<phase_id>   Alias for --resume-from
  --force-fresh             Wipe state + stack dir + config, start over
  --adopt                   Adopt an existing install (run phase 0-4 VERIFY
                            blocks against disk before marking complete)
  -h, --help                Show this help

With no flags: auto-resume from last completed phase if state file exists;
otherwise start fresh.

Phase IDs (in order):
HELP
            for p in "${PHASES[@]}"; do
                printf "  %-9s %s\n" "${p%|*}" "${p#*|}"
            done
            clean_exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            echo "Run with --help for usage." >&2
            clean_exit 2
            ;;
    esac
done

phase_index() {
    local phase_id="$1"
    local i=0
    local p
    for p in "${PHASES[@]}"; do
        if [ "${p%|*}" = "$phase_id" ]; then
            echo "$i"
            return 0
        fi
        i=$((i+1))
    done
    return 1
}

if [ -n "$RESUME_FROM" ]; then
    if ! phase_index "$RESUME_FROM" >/dev/null; then
        echo "ERROR: --resume-from value '$RESUME_FROM' is not a known phase." >&2
        echo "Run with --help for the phase list." >&2
        clean_exit 2
    fi
fi

if [ "$FORCE_FRESH" = true ] && [ "$ADOPT" = true ]; then
    echo "ERROR: --force-fresh and --adopt are mutually exclusive." >&2
    clean_exit 2
fi

# ============================================================================
# State file helpers
# ============================================================================

is_phase_complete() {
    local phase_id="$1"
    [ -f "$STATE_FILE" ] || return 1
    grep -q "^${phase_id}|" "$STATE_FILE"
}

mark_phase_complete() {
    local phase_id="$1"
    local desc="$2"
    local iso8601
    iso8601="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if [ -f "$STATE_FILE" ]; then
        grep -v "^${phase_id}|" "$STATE_FILE" > "${STATE_FILE}.tmp" || true
        mv "${STATE_FILE}.tmp" "$STATE_FILE"
    fi
    echo "${phase_id}|${iso8601}|${desc}" >> "$STATE_FILE"
    chmod 600 "$STATE_FILE"
}

banner() {
    local phase_id="$1"
    local desc="$2"
    CURRENT_PHASE="$phase_id"
    CURRENT_PHASE_DESC="$desc"
    echo ""
    echo "════════════════════════════════════════════════════════════════"
    echo "[Phase ${phase_id}] ${desc}"
    echo "════════════════════════════════════════════════════════════════"
}

should_run_phase() {
    local phase_id="$1"
    local this_idx resume_idx
    if [ -n "$RESUME_FROM" ]; then
        this_idx=$(phase_index "$phase_id")
        resume_idx=$(phase_index "$RESUME_FROM")
        if [ "$this_idx" -ge "$resume_idx" ]; then
            return 0
        fi
    fi
    if is_phase_complete "$phase_id"; then
        echo "[Phase ${phase_id}] Already complete — skipping."
        return 1
    fi
    return 0
}

# ============================================================================
# Prompt helpers (input validation at PROMPT time — per spec refinement #2)
# ============================================================================

SAFE_PASSWORD_REGEX='^[A-Za-z0-9._@%+=,:-]{8,}$'
SAFE_PASSWORD_HINT="8+ chars; allowed: letters, numbers, . _ @ % + = , : -"

generate_hex_password() {
    # Prefer openssl when available; fall back to /dev/urandom + od so a minimal
    # Ubuntu CLI install does not fail before apt installs optional packages.
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 24
    else
        od -An -N24 -tx1 /dev/urandom | tr -d ' \n'
    fi
}

validate_password() {
    # Returns 0 if valid, prints reason on stderr if not.
    # Keep the accepted set shell-safe because these values are persisted in
    # source-able config files for resumable runs.
    local pw="$1"
    if [[ "$pw" =~ $SAFE_PASSWORD_REGEX ]]; then
        return 0
    fi
    if [ "${#pw}" -lt 8 ]; then
        echo "  ✗ Password must be at least 8 characters." >&2
    else
        echo "  ✗ Password may only contain: letters, numbers, . _ @ % + = , : -" >&2
    fi
    return 1
}

prompt_db_password() {
    local response
    while true; do
        read -rsp "DB root password (${SAFE_PASSWORD_HINT}; press Enter to generate random): " response || true
        echo ""
        if [ -z "$response" ]; then
            PROMPT_RESULT="$(generate_hex_password)"
            echo "  → Generated random password (48 hex chars)"
            return 0
        fi
        if validate_password "$response"; then
            PROMPT_RESULT="$response"
            return 0
        fi
    done
}

prompt_password() {
    local prompt_text="$1"
    local response
    while true; do
        read -rsp "${prompt_text} (${SAFE_PASSWORD_HINT}): " response || true
        echo ""
        if validate_password "$response"; then
            PROMPT_RESULT="$response"
            return 0
        fi
    done
}

prompt_string() {
    # Args: prompt_text, regex, error_msg, [default]
    local prompt_text="$1"
    local regex="$2"
    local error_msg="$3"
    local default="${4:-}"
    local response
    while true; do
        if [ -n "$default" ]; then
            read -rp "${prompt_text} [${default}]: " response || true
            response="${response:-$default}"
        else
            read -rp "${prompt_text}: " response || true
        fi
        if [[ "$response" =~ $regex ]]; then
            PROMPT_RESULT="$response"
            return 0
        fi
        echo "  ✗ $error_msg" >&2
    done
}

prompt_integer_range() {
    # Args: prompt_text, min, max, default
    local prompt_text="$1"
    local min="$2"
    local max="$3"
    local default="$4"
    local response
    while true; do
        read -rp "${prompt_text} [${default}]: " response || true
        response="${response:-$default}"
        if [[ "$response" =~ ^[0-9]+$ ]] && [ "$response" -ge "$min" ] && [ "$response" -le "$max" ]; then
            PROMPT_RESULT="$response"
            return 0
        fi
        echo "  ✗ Must be an integer between $min and $max." >&2
    done
}

prompt_yn() {
    # Args: prompt_text, default (y or n)
    local prompt_text="$1"
    local default="$2"
    local response
    local default_display
    if [ "$default" = "y" ]; then
        default_display="Y/n"
    else
        default_display="y/N"
    fi
    while true; do
        read -rp "${prompt_text} [${default_display}]: " response || true
        response="${response:-$default}"
        case "${response,,}" in
            y|yes) PROMPT_RESULT="y"; return 0 ;;
            n|no)  PROMPT_RESULT="n"; return 0 ;;
        esac
        echo "  ✗ Please answer y or n." >&2
    done
}

prompt_xp_rate() {
    local response
    while true; do
        read -rp "Server XP/progression rate (x1, x3, x5, x7) [x5]: " response || true
        response="${response:-x5}"
        response="${response,,}"
        case "$response" in
            x1|x3|x5|x7)
                PROMPT_RESULT="$response"
                return 0
                ;;
        esac
        echo "  ✗ Must be one of: x1, x3, x5, x7." >&2
    done
}

validate_xp_rate_choice() {
    case "${SERVER_XP_RATE:-}" in
        x1|x3|x5|x7) return 0 ;;
        *)
            echo "ERROR: SERVER_XP_RATE must be one of: x1, x3, x5, x7." >&2
            echo "Current value: ${SERVER_XP_RATE:-<unset>}" >&2
            return 1
            ;;
    esac
}

xp_rate_values() {
    # Emits: quest kill explore money reputation skill_discovery item_normal item_uncommon
    case "$1" in
        x3) printf '%s\n' "3 3 3 2 2 2 1 1" ;;
        x5) printf '%s\n' "5 3 3 3 3 3 1 1" ;;
        x7) printf '%s\n' "7 5 5 3 5 3 1.5 1.5" ;;
        *)
            echo "ERROR: xp_rate_values called for unsupported rate: $1" >&2
            return 1
            ;;
    esac
}

insert_xp_rate_overrides_into_compose() {
    local file="$1"
    local rate="${SERVER_XP_RATE:-x5}"
    local tmp
    local quest kill explore money reputation skill_discovery item_normal item_uncommon

    validate_xp_rate_choice

    tmp="$(mktemp /tmp/ac-xp-rate-overrides.XXXXXX)"
    if [ "$rate" = "x1" ]; then
        cat > "$tmp" <<'EOF'
      # Server XP rate: x1 selected; no Rate.* overrides are set.
EOF
    else
        read -r quest kill explore money reputation skill_discovery item_normal item_uncommon < <(xp_rate_values "$rate")
        cat > "$tmp" <<EOF
      # Server XP rate: ${rate}
      AC_RATE_XP_QUEST: "${quest}"
      AC_RATE_XP_KILL: "${kill}"
      AC_RATE_XP_EXPLORE: "${explore}"
      AC_RATE_DROP_MONEY: "${money}"
      AC_RATE_REPUTATION_GAIN: "${reputation}"
      AC_RATE_SKILL_DISCOVERY: "${skill_discovery}"
      AC_RATE_DROP_ITEM_NORMAL: "${item_normal}"
      AC_RATE_DROP_ITEM_UNCOMMON: "${item_uncommon}"
EOF
    fi

    if ! grep -qFx '      # ----- progression rate overrides -----' "$file"; then
        echo "ERROR: progression rate insertion marker not found in ${file}."
        rm -f "$tmp"
        return 1
    fi

    sed -i "/^      # ----- progression rate overrides -----$/r $tmp" "$file"
    rm -f "$tmp"
}

verify_xp_rate_overrides_in_compose() {
    local file="$1"
    local rate="${SERVER_XP_RATE:-x5}"
    local quest kill explore money reputation skill_discovery item_normal item_uncommon
    local fail=0
    local key escaped count expected

    validate_xp_rate_choice

    if [ "$rate" = "x1" ]; then
        for key in \
            AC_RATE_XP_QUEST \
            AC_RATE_XP_KILL \
            AC_RATE_XP_EXPLORE \
            AC_RATE_DROP_MONEY \
            AC_RATE_REPUTATION_GAIN \
            AC_RATE_SKILL_DISCOVERY \
            AC_RATE_DROP_ITEM_NORMAL \
            AC_RATE_DROP_ITEM_UNCOMMON
        do
            count="$(grep -Ec "^[[:space:]]*${key}[[:space:]]*:" "$file" || true)"
            if [ "$count" != "0" ]; then
                echo "ERROR: ${key} appears ${count} time(s) in ${file}; expected 0 for SERVER_XP_RATE=x1."
                fail=1
            fi
        done
        return "$fail"
    fi

    read -r quest kill explore money reputation skill_discovery item_normal item_uncommon < <(xp_rate_values "$rate")

    for expected in \
        "      AC_RATE_XP_QUEST: \"${quest}\"" \
        "      AC_RATE_XP_KILL: \"${kill}\"" \
        "      AC_RATE_XP_EXPLORE: \"${explore}\"" \
        "      AC_RATE_DROP_MONEY: \"${money}\"" \
        "      AC_RATE_REPUTATION_GAIN: \"${reputation}\"" \
        "      AC_RATE_SKILL_DISCOVERY: \"${skill_discovery}\"" \
        "      AC_RATE_DROP_ITEM_NORMAL: \"${item_normal}\"" \
        "      AC_RATE_DROP_ITEM_UNCOMMON: \"${item_uncommon}\""
    do
        key="${expected%%:*}"
        key="${key##* }"
        escaped="$(escape_conf_key_regex "$key")"
        count="$(grep -Ec "^[[:space:]]*${escaped}[[:space:]]*:" "$file" || true)"
        if [ "$count" != "1" ]; then
            echo "ERROR: ${key} appears ${count} time(s) in ${file}; expected exactly 1."
            fail=1
        fi
        if ! grep -qFx "$expected" "$file"; then
            echo "ERROR: Missing expected XP/progression override in ${file}: $expected"
            fail=1
        fi
    done

    return "$fail"
}

effective_compose_has_env_value() {
    local file="$1"
    local key="$2"
    local value="$3"
    local escaped_value

    escaped_value="$(escape_conf_key_regex "$value")"

    grep -Eq "(^|[[:space:]-])${key}:[[:space:]]*\"?${escaped_value}\"?([[:space:]]*$|[[:space:]])|(^|[[:space:]-])${key}=${escaped_value}([[:space:]]*$|[[:space:]])" "$file"
}

verify_xp_rate_overrides_in_effective_compose() {
    local file="$1"
    local rate="${SERVER_XP_RATE:-x5}"
    local quest kill explore money reputation skill_discovery item_normal item_uncommon
    local fail=0
    local key

    validate_xp_rate_choice

    if [ "$rate" = "x1" ]; then
        return 0
    fi

    read -r quest kill explore money reputation skill_discovery item_normal item_uncommon < <(xp_rate_values "$rate")

    while IFS='|' read -r key value; do
        [ -n "$key" ] || continue
        if ! effective_compose_has_env_value "$file" "$key" "$value"; then
            echo "INVALID or missing XP/progression env var: ${key}=${value}"
            fail=1
        fi
    done <<EOF
AC_RATE_XP_QUEST|${quest}
AC_RATE_XP_KILL|${kill}
AC_RATE_XP_EXPLORE|${explore}
AC_RATE_DROP_MONEY|${money}
AC_RATE_REPUTATION_GAIN|${reputation}
AC_RATE_SKILL_DISCOVERY|${skill_discovery}
AC_RATE_DROP_ITEM_NORMAL|${item_normal}
AC_RATE_DROP_ITEM_UNCOMMON|${item_uncommon}
EOF

    return "$fail"
}


# ============================================================================
# Config persistence
# ============================================================================

save_config() {
    umask 077
    cat > "$CONFIG_FILE" <<EOF
# Persisted prompt answers for resumable installer runs.
# This file contains plaintext passwords; chmod 600.
# Will be shredded on successful completion (or on --force-fresh).
DB_ROOT_PASSWORD=${DB_ROOT_PASSWORD}
GM_USERNAME=${GM_USERNAME}
GM_PASSWORD=${GM_PASSWORD}
AHBOT_PASSWORD=${AHBOT_PASSWORD}
PLAYERBOT_COUNT=${PLAYERBOT_COUNT}
SERVER_XP_RATE=${SERVER_XP_RATE}
INNODB_BUFFER_POOL_SIZE=${INNODB_BUFFER_POOL_SIZE}
MAP_UPDATE_THREADS=${MAP_UPDATE_THREADS}
AHBOT_CHARACTER_COUNT=${AHBOT_CHARACTER_COUNT}
INSTALL_UFW=${INSTALL_UFW}
ENABLE_SYSTEMD=${ENABLE_SYSTEMD}
TAILSCALE_IP=${TAILSCALE_IP:-}
EOF
    # Preserve AHBOT_GUIDS across save_config rewrites. Pause 3 sets this
    # value in-memory and appends it to the config file; without this block,
    # a later call to save_config (e.g. from --resume-from=0.4 retrying
    # Tailscale auth after Pause 3 has completed) would overwrite the file
    # and Phase 6.1.4 would then fail with no GUIDs.
    if [ -n "${AHBOT_GUIDS:-}" ]; then
        echo "AHBOT_GUIDS=${AHBOT_GUIDS}" >> "$CONFIG_FILE"
    fi
    chmod 600 "$CONFIG_FILE"
}

load_config() {
    # shellcheck disable=SC1090
    source "$CONFIG_FILE"
}

shred_config() {
    if [ -f "$CONFIG_FILE" ]; then
        if command -v shred >/dev/null 2>&1; then
            shred -u "$CONFIG_FILE" 2>/dev/null || rm -f "$CONFIG_FILE"
        else
            rm -f "$CONFIG_FILE"
        fi
        echo "Removed persisted config: $CONFIG_FILE"
    fi
}

# ============================================================================
# --force-fresh: wipe state and start over
# ============================================================================

if [ "$FORCE_FRESH" = true ]; then
    echo "════════════════════════════════════════════════════════════════"
    echo "--force-fresh: wiping install state and starting over"
    echo "════════════════════════════════════════════════════════════════"
    echo ""
    echo "This will:"
    echo "  - Remove $STATE_FILE"
    echo "  - Shred $CONFIG_FILE (if present)"
    if [ -d "$STACK_DIR" ]; then
        echo "  - Remove $STACK_DIR (containing the existing install)"
    fi
    echo ""
    read -rp "Type 'WIPE' to confirm: " confirm
    if [ "$confirm" != "WIPE" ]; then
        echo "Aborted."
        clean_exit 1
    fi
    if [ -d "$STACK_DIR" ]; then
        # Try to stop containers gracefully first
        if [ -f "${STACK_DIR}/docker-compose.yml" ] || [ -f "${STACK_DIR}/docker-compose.override.yml" ]; then
            (cd "$STACK_DIR" && docker compose down 2>/dev/null) || true
        fi
        sudo rm -rf "$STACK_DIR"
    fi
    rm -f "$STATE_FILE"
    shred_config
    echo "Wiped. Continuing with fresh install."
fi

# ============================================================================
# Existing-install detection (locked decision #7)
# ============================================================================

if [ ! -f "$STATE_FILE" ] && [ -d "$STACK_DIR" ] && [ "$ADOPT" = false ]; then
    if stack_dir_has_only_logs; then
        echo "Existing logs-only stack directory found; continuing fresh install and preserving logs."
    else
        echo "════════════════════════════════════════════════════════════════"
        echo "ERROR: Existing installation detected without state file"
        echo "════════════════════════════════════════════════════════════════"
        echo ""
        echo "  $STACK_DIR exists but $STATE_FILE does not."
        echo ""
        echo "  Re-run with one of:"
        echo "    --force-fresh   Wipe and start over"
        echo "    --adopt         Mark phases 0-4 complete (after VERIFY checks)"
        echo "                    and resume from Pause 2 (account creation)"
        clean_exit 1
    fi
fi

# ============================================================================
# Sudo prime + keep-alive (locked decision #2)
# ============================================================================

echo "════════════════════════════════════════════════════════════════"
echo "AzerothCore installer starting"
echo "Log file: $LOG_FILE"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "Priming sudo (you'll be prompted for your password once)..."
sudo -v
# Background keep-alive: refresh sudo timestamp every 60s.
( while true; do sudo -n true 2>/dev/null || exit; sleep 60; done ) &
KEEPALIVE_PID=$!
echo "Sudo keep-alive PID: $KEEPALIVE_PID"

# ============================================================================
# Prompts (only on fresh install; on resume, load saved config)
# ============================================================================

if [ -f "$CONFIG_FILE" ] && [ "$ADOPT" = false ]; then
    echo ""
    echo "Loading saved prompt answers from $CONFIG_FILE"
    load_config
elif [ "$ADOPT" = false ] && [ "${RESUME_FROM:-}" = "8" ] && [ -f "${STACK_DIR}/.env" ]; then
    echo ""
    echo "Phase 8-only resume detected and no saved prompt config exists."
    echo "Loading existing stack environment from ${STACK_DIR}/.env for systemd repair."
    # shellcheck disable=SC1091
    source "${STACK_DIR}/.env"
    DB_ROOT_PASSWORD="${DOCKER_DB_ROOT_PASSWORD:-}"
    GM_USERNAME="UNUSED"
    GM_PASSWORD="UnusedPass123"
    AHBOT_PASSWORD="UnusedPass123"
    PLAYERBOT_COUNT="${AC_AI_PLAYERBOT_MIN_RANDOM_BOTS:-1000}"
    SERVER_XP_RATE="x5"
    INNODB_BUFFER_POOL_SIZE="6G"
    MAP_UPDATE_THREADS="4"
    AHBOT_CHARACTER_COUNT="1"
    INSTALL_UFW="n"
    ENABLE_SYSTEMD="y"
    TAILSCALE_IP="${DOCKER_AUTH_EXTERNAL_PORT%%:*}"
else
    echo ""
    echo "════════════════════════════════════════════════════════════════"
    echo "Interactive configuration"
    echo "════════════════════════════════════════════════════════════════"
    echo ""
    if [ "$ADOPT" = true ]; then
        echo "Adopt mode: these prompts collect values needed for the manual"
        echo "pauses (account creation, AH character). Existing .env will be"
        echo "respected; the DB root password you provide here is only used"
        echo "for the SQL verifications that follow each pause."
        echo ""
    fi

    prompt_db_password
    DB_ROOT_PASSWORD="$PROMPT_RESULT"

    prompt_string \
        "GM admin username" \
        '^[a-zA-Z0-9]{4,16}$' \
        "Username must be 4-16 alphanumeric characters."
    GM_USERNAME="$PROMPT_RESULT"

    prompt_password "GM admin password (8+ chars, no spaces/quotes/\\/\$/\`)"
    GM_PASSWORD="$PROMPT_RESULT"

    prompt_password "AHBOT account password (8+ chars, no spaces/quotes/\\/\$/\`)"
    AHBOT_PASSWORD="$PROMPT_RESULT"

    prompt_integer_range "Random bot count (1-2000, applied to both MIN and MAX)" 1 2000 1000
    PLAYERBOT_COUNT="$PROMPT_RESULT"

    prompt_xp_rate
    SERVER_XP_RATE="$PROMPT_RESULT"

    prompt_string \
        "InnoDB buffer pool size (1G-32G; format <N>G)" \
        '^([1-9]|[12][0-9]|3[0-2])G$' \
        "Must be an integer 1-32 followed by G (e.g. 6G, 8G)." \
        "6G"
    INNODB_BUFFER_POOL_SIZE="$PROMPT_RESULT"

    prompt_integer_range "Map update threads (1-16)" 1 16 4
    MAP_UPDATE_THREADS="$PROMPT_RESULT"

    prompt_integer_range "AHBOT character count (1 or 2)" 1 2 1
    AHBOT_CHARACTER_COUNT="$PROMPT_RESULT"

    prompt_yn "Install and enable UFW firewall?" n
    INSTALL_UFW="$PROMPT_RESULT"

    prompt_yn "Enable systemd auto-start on boot?" y
    ENABLE_SYSTEMD="$PROMPT_RESULT"

    TAILSCALE_IP=""
    save_config

    echo ""
    echo "Configuration saved to $CONFIG_FILE (mode 600)."
fi

# Older saved prompt files do not contain SERVER_XP_RATE. Default to x5, which is
# the default for new prompt runs, then validate before any phase can use it.
SERVER_XP_RATE="${SERVER_XP_RATE:-x5}"
validate_xp_rate_choice

# Derive InnoDB buffer pool instance count from the chosen buffer pool size:
# MySQL only honors innodb_buffer_pool_instances when each instance has at
# least ~1 GB, so we set instances = floor(pool size in GB). Always recompute
# from INNODB_BUFFER_POOL_SIZE so a stale value in a saved config can't drift
# out of sync with the size.
INNODB_BUFFER_POOL_INSTANCES="${INNODB_BUFFER_POOL_SIZE%G}"

# ============================================================================
# --adopt: verify existing install and mark phases 0-4 complete
# (Spec refinement #1: no blind adoption — run VERIFY blocks first.)
# ============================================================================

adopt_existing_install() {
    echo ""
    echo "════════════════════════════════════════════════════════════════"
    echo "--adopt mode: verifying existing install before marking phases complete"
    echo "════════════════════════════════════════════════════════════════"

    local fail=0

    # VERIFY 0.3 — Docker working
    echo ""
    echo "[adopt] VERIFY 0.3: Docker"
    if docker --version >/dev/null 2>&1 \
       && docker compose version >/dev/null 2>&1 \
       && docker run --rm hello-world 2>&1 | grep -q "Hello from Docker"; then
        echo "  ✓ Docker + Compose working"
    else
        echo "  ✗ Docker check failed"
        fail=1
    fi

    # VERIFY 0.4 — Tailscale
    echo ""
    echo "[adopt] VERIFY 0.4: Tailscale"
    if tailscale ip -4 2>/dev/null | grep -Eq '^100\.'; then
        TAILSCALE_IP="$(tailscale ip -4 2>/dev/null | grep -E '^100\.' | head -1)"
        echo "  ✓ Tailscale IPv4: $TAILSCALE_IP"
    else
        echo "  ✗ tailscale ip -4 did not return a 100.x.x.x address"
        fail=1
    fi

    # VERIFY 0.5 / Phase 1 — Stack dir and git remote
    echo ""
    echo "[adopt] VERIFY Phase 1: stack dir + git remote + branch + modules"
    if [ -d "$STACK_DIR/.git" ]; then
        local remote branch
        remote="$(cd "$STACK_DIR" && git remote get-url origin 2>/dev/null || echo "")"
        branch="$(cd "$STACK_DIR" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
        if echo "$remote" | grep -q "mod-playerbots/azerothcore-wotlk" && [ "$branch" = "Playerbot" ]; then
            echo "  ✓ Stack is mod-playerbots/azerothcore-wotlk on Playerbot branch"
        else
            echo "  ✗ Stack is on wrong remote ($remote) or branch ($branch)"
            fail=1
        fi
        if [ -d "$STACK_DIR/modules/mod-playerbots" ] \
           && [ -d "$STACK_DIR/modules/mod-ah-bot-plus" ] \
           && [ -d "$STACK_DIR/modules/mod-individual-progression" ]; then
            echo "  ✓ All three modules present"
        else
            echo "  ✗ mod-playerbots and/or mod-ah-bot-plus and/or mod-individual-progression modules missing"
            fail=1
        fi
    else
        echo "  ✗ $STACK_DIR is not a git repository"
        fail=1
    fi

    # VERIFY 2.3 — Playerbots custom SQL cleanup
    echo ""
    echo "[adopt] VERIFY 2.3: Playerbots custom SQL cleanup"
    (cd "$STACK_DIR" && cleanup_playerbots_custom_sql_files && assert_no_playerbots_sql_duplicates_in_custom)
    echo "  ✓ No Playerbots SQL duplicates remain under data/sql/custom"

    # Source the existing .env to pick up DOCKER_DB_ROOT_PASSWORD and DOCKER_IMAGE_TAG
    if [ -f "$STACK_DIR/.env" ]; then
        # shellcheck disable=SC1091
        source "$STACK_DIR/.env"
        echo "  ✓ Sourced $STACK_DIR/.env"
    else
        echo "  ✗ $STACK_DIR/.env missing"
        fail=1
    fi

    if [ -f "$STACK_DIR/docker-compose.override.yml" ]; then
        (cd "$STACK_DIR" && ensure_playerbots_updater_enabled_in_compose_override)
        echo "  ✓ Playerbots updater is enabled in docker-compose.override.yml"
    else
        echo "  ✗ $STACK_DIR/docker-compose.override.yml missing"
        fail=1
    fi

    # VERIFY Phase 3 — built images present with the locked tag
    echo ""
    echo "[adopt] VERIFY Phase 3: built images with tag ${DOCKER_IMAGE_TAG:-playerbot-local}"
    local img
    for img in worldserver authserver db-import client-data; do
        if docker images --format '{{.Repository}}:{{.Tag}}' \
           | grep -qFx "acore/ac-wotlk-${img}:${DOCKER_IMAGE_TAG:-playerbot-local}"; then
            echo "  ✓ acore/ac-wotlk-${img}:${DOCKER_IMAGE_TAG:-playerbot-local}"
        else
            echo "  ✗ acore/ac-wotlk-${img}:${DOCKER_IMAGE_TAG:-playerbot-local} missing"
            fail=1
        fi
    done

    # VERIFY 3.1 — conf templates
    echo ""
    echo "[adopt] VERIFY 3.1: module conf templates"
    if [ -f "$STACK_DIR/configs/modules/mod_ahbot.conf.dist" ] \
       && [ -f "$STACK_DIR/configs/modules/mod_ahbot.conf" ]; then
        echo "  ✓ mod_ahbot conf + conf.dist present"
    else
        echo "  ✗ mod_ahbot conf or conf.dist missing"
        fail=1
    fi
    if [ -f "$STACK_DIR/configs/modules/playerbots.conf.dist" ] \
       || [ -f "$STACK_DIR/configs/modules/mod_playerbots.conf.dist" ]; then
        echo "  ✓ playerbots conf.dist present"
        (cd "$STACK_DIR" && ensure_playerbots_performance_config)
    else
        echo "  ✗ playerbots(.|_)conf.dist missing"
        fail=1
    fi

    # VERIFY Phase 4 — containers + databases + MySQL vars
    echo ""
    echo "[adopt] VERIFY Phase 4: containers + databases + MySQL vars"
    local c
    for c in ac-database ac-authserver ac-worldserver; do
        if [ "$(docker inspect --format='{{.State.Status}}' "$c" 2>/dev/null)" = "running" ]; then
            echo "  ✓ $c running"
        else
            echo "  ✗ $c not running"
            fail=1
        fi
    done

    if [ "$fail" -eq 0 ]; then
        local dbs
        dbs="$(docker exec ac-database mysql -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" \
               -N -B -e "SHOW DATABASES;" 2>/dev/null || echo "")"
        local db
        for db in acore_auth acore_characters acore_world acore_playerbots; do
            if echo "$dbs" | grep -qFx "$db"; then
                echo "  ✓ Database $db exists"
            else
                echo "  ✗ Database $db missing"
                fail=1
            fi
        done

        if echo "$dbs" | grep -qFx acore_playerbots; then
            verify_playerbots_schema_now || fail=1
        fi

        if worldserver_has_playerbots_fatal_logs; then
            echo "  ✗ Worldserver log contains fatal Playerbots database/updater errors"
            fail=1
        else
            echo "  ✓ No fatal Playerbots database/updater errors found in worldserver log"
        fi

        verify_mysql_tuning_active || fail=1
    fi

    if [ "$fail" -ne 0 ]; then
        echo ""
        echo "════════════════════════════════════════════════════════════════"
        echo "✗ Adoption aborted: one or more VERIFY checks failed."
        echo "  Fix the failures above and re-run --adopt, or use --force-fresh"
        echo "  to start over."
        echo "════════════════════════════════════════════════════════════════"
        clean_exit 1
    fi

    # All checks passed — mark phases complete.
    echo ""
    echo "All adopt VERIFY checks passed. Marking phases 0.0 through 4 complete."
    local p phase_id
    for p in "${PHASES[@]}"; do
        phase_id="${p%|*}"
        case "$phase_id" in
            0.0|0.1|0.2|0.3|0.4|0.5|1|2.1|2.2|2.3|2.4|2.5|2.6|3|3.1|4)
                mark_phase_complete "$phase_id" "${p#*|} (adopted)"
                ;;
        esac
    done
    # The relocate-log check needs ${STACK_DIR}/logs to exist.
    relocate_log_if_possible
    save_config
    echo "Adoption complete. Resuming from Pause 2 (account creation)."
}

if [ "$ADOPT" = true ]; then
    adopt_existing_install
fi

# ============================================================================
# PHASE 0.0 — Pre-flight checks
# ============================================================================
if should_run_phase "0.0"; then
    banner "0.0" "Pre-flight checks"

    echo "OS version:"
    lsb_release -a 2>/dev/null || true

    echo ""
    if docker --version >/dev/null 2>&1; then
        echo "Docker: OK ($(docker --version))"
    else
        echo "Docker: NOT INSTALLED"
    fi

    if docker compose version >/dev/null 2>&1; then
        echo "Docker Compose: OK ($(docker compose version))"
    else
        echo "Docker Compose: NOT INSTALLED"
    fi

    if groups "$USER" | grep -qw docker; then
        echo "Docker group: OK (no sudo needed for docker)"
    else
        echo "Docker group: NOT IN GROUP (will need to log out/in after joining)"
    fi

    if tailscale version >/dev/null 2>&1; then
        echo "Tailscale: OK"
    else
        echo "Tailscale: NOT INSTALLED"
    fi

    if tailscale ip -4 >/dev/null 2>&1; then
        echo "Tailscale: authenticated ($(tailscale ip -4 | head -1))"
    else
        echo "Tailscale: not authenticated"
    fi

    sudo ufw status 2>/dev/null | head -5 || true

    if git --version >/dev/null 2>&1; then
        echo "git: OK"
    else
        echo "git: NOT INSTALLED"
    fi

    if unzip -v >/dev/null 2>&1; then
        echo "unzip: OK"
    else
        echo "unzip: NOT INSTALLED"
    fi

    if [ -d "$STACK_DIR" ]; then
        echo "Stack dir: EXISTS"
    else
        echo "Stack dir: not yet created"
    fi

    if crontab -l 2>/dev/null | grep -qi azerothcore; then
        echo "Cron: backup entry already exists"
    else
        echo "Cron: no existing backup entry"
    fi

    df -h /opt 2>/dev/null || df -h /

    mark_phase_complete "0.0" "Pre-flight checks"
fi

# ============================================================================
# PHASE 0.1 — OS version check
# ============================================================================
if should_run_phase "0.1"; then
    banner "0.1" "OS version check"

    OS_CODENAME="$(lsb_release -cs 2>/dev/null || echo unknown)"
    OS_RELEASE="$(lsb_release -rs 2>/dev/null || echo unknown)"
    echo "Detected: ${OS_CODENAME} (${OS_RELEASE})"

    case "$OS_RELEASE" in
        22.04|22.04.*)
            echo "Ubuntu 22.04 LTS — primary tested target. OK."
            ;;
        24.04|24.04.*)
            echo ""
            echo "WARNING: Ubuntu 24.04 is not this installer's primary tested target."
            echo "The Docker-only path SHOULD work but is not the tested one."
            prompt_yn "Proceed on 24.04 at your own risk?" n
            if [ "$PROMPT_RESULT" != "y" ]; then
                echo "Aborted by user."
                clean_exit 1
            fi
            ;;
        *)
            echo ""
            echo "ERROR: Detected OS is not Ubuntu 22.04 or 24.04."
            echo "This installer is targeted at Ubuntu 22.04 LTS."
            prompt_yn "Accept the risk and proceed anyway?" n
            if [ "$PROMPT_RESULT" != "y" ]; then
                echo "Aborted."
                clean_exit 1
            fi
            ;;
    esac

    mark_phase_complete "0.1" "OS version check"
fi

# ============================================================================
# PHASE 0.2 — System packages
# UFW package omitted from apt-get install if INSTALL_UFW=n (locked decision #4)
# ============================================================================
if should_run_phase "0.2"; then
    banner "0.2" "System packages (apt)"

    if ! sudo apt-get update; then
        echo "ERROR: apt-get update failed. Check network/repos and re-run --resume-from=0.2"
        exit 1
    fi
    sudo apt-get upgrade -y

    APT_PKGS="ca-certificates cron curl git gnupg lsb-release openssl wget unzip"
    if [ "$INSTALL_UFW" = "y" ]; then
        APT_PKGS="$APT_PKGS ufw"
    else
        echo "INSTALL_UFW=n — ufw package omitted from apt install list"
    fi
    # shellcheck disable=SC2086
    sudo apt-get install -y $APT_PKGS

    mark_phase_complete "0.2" "System packages installed"
fi

# ============================================================================
# PHASE 0.3 — Docker Engine
# ============================================================================
if should_run_phase "0.3"; then
    banner "0.3" "Docker Engine install + verify"

    if ! docker --version >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
        echo "Installing Docker Engine..."
        sudo rm -f /usr/share/keyrings/docker-archive-keyring.gpg
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
          | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg

        echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] \
https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
          | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

        sudo apt-get update
        sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
    else
        echo "Docker already installed: $(docker --version)"
    fi

    # Compose plugin sanity check
    if ! docker compose version >/dev/null 2>&1; then
        echo "ERROR: 'docker compose' (v2 plugin) is not available."
        echo "Install docker-compose-plugin via apt; the legacy docker-compose binary is not supported."
        exit 1
    fi

    # Docker version warn (24.x+ recommended)
    DOCKER_MAJOR="$(docker version --format '{{.Server.Version}}' 2>/dev/null | cut -d. -f1)"
    if [ -n "$DOCKER_MAJOR" ] && [ "$DOCKER_MAJOR" -lt 24 ] 2>/dev/null; then
        echo "WARNING: Docker $DOCKER_MAJOR.x detected; 24.x or newer recommended."
        prompt_yn "Continue at your own risk?" n
        [ "$PROMPT_RESULT" = "y" ] || exit 1
    fi

    if ! groups "$USER" | grep -qw docker; then
        echo "Adding $USER to docker group..."
        sudo usermod -aG docker "$USER"
        echo ""
        echo "════════════════════════════════════════════════════════════════"
        echo "ACTION REQUIRED: log out and back in for the group change to take effect."
        echo "Do NOT run 'newgrp docker' — it breaks scripted execution."
        echo ""
        echo "After re-login, re-run this script (state file preserves progress)."
        echo "════════════════════════════════════════════════════════════════"
        clean_exit 10
    fi

    # VERIFY 0.3
    echo ""
    echo "VERIFY 0.3:"
    docker --version
    docker compose version
    if docker run --rm hello-world 2>&1 | grep -q "Hello from Docker"; then
        echo "Docker: working correctly"
    else
        echo "Docker: ERROR — hello-world did not return expected output"
        exit 1
    fi

    mark_phase_complete "0.3" "Docker Engine installed + verified"
fi

# ============================================================================
# PHASE 0.4 — Tailscale (PAUSE 1)
# ============================================================================
if should_run_phase "0.4"; then
    banner "0.4" "Tailscale install + authentication"

    if ! tailscale version >/dev/null 2>&1; then
        echo "Installing Tailscale..."
        curl -fsSL https://tailscale.com/install.sh | sh
    else
        echo "Tailscale already installed: $(tailscale version | head -1)"
    fi

    # Skip Pause 1 entirely if already authenticated (per spec)
    if tailscale ip -4 2>/dev/null | grep -Eq '^100\.'; then
        echo "Tailscale already authenticated."
    else
        echo ""
        echo "════════════════════════════════════════════════════════════════"
        echo "MANUAL STEP 1 of 3: Tailscale authentication"
        echo "════════════════════════════════════════════════════════════════"
        echo ""
        echo "Running 'sudo tailscale up' — it will print a browser URL."
        echo "Open the URL on any device, authenticate to your Tailscale account,"
        echo "then return here. The script will poll for your Tailscale IP and"
        echo "continue automatically (timeout 5 minutes)."
        echo ""
        sudo tailscale up

        # Fail fast if tailscaled isn't running at all. Without this, a dead
        # daemon causes the polling loop below to time out after 5 minutes
        # before showing any useful error.
        if ! sudo tailscale status >/dev/null 2>&1; then
            echo ""
            echo "ERROR: tailscale status failed after 'tailscale up'."
            echo "The tailscaled daemon may not be running. Try:"
            echo "    sudo systemctl status tailscaled"
            echo "    sudo systemctl restart tailscaled"
            echo "Then re-run: $0 --resume-from=0.4"
            exit 1
        fi

        echo ""
        echo "Polling for Tailscale IPv4 (timeout 300s)..."
        POLL_START=$(date +%s)
        while true; do
            if tailscale ip -4 2>/dev/null | grep -Eq '^100\.'; then
                echo "Tailscale authenticated."
                break
            fi
            if (( $(date +%s) - POLL_START > 300 )); then
                echo "ERROR: Tailscale did not authenticate within 5 minutes."
                echo "Re-run Phase 0.4: $0 --resume-from=0.4"
                exit 1
            fi
            sleep 5
        done
    fi

    # Auto-detect Tailscale IP (locked decision #9). First IPv4 of 100.x.x.x form.
    ALL_TS_IPS="$(tailscale ip -4 2>/dev/null | grep -E '^100\.' || true)"
    TS_IP_COUNT=$(echo "$ALL_TS_IPS" | grep -c . || true)
    TAILSCALE_IP="$(echo "$ALL_TS_IPS" | head -1)"

    if [ -z "$TAILSCALE_IP" ]; then
        echo "ERROR: Could not determine Tailscale IPv4."
        exit 1
    fi

    echo "Tailscale IPv4: $TAILSCALE_IP"
    if [ "$TS_IP_COUNT" -gt 1 ]; then
        echo "WARNING: multiple Tailscale IPv4 addresses returned. Using the first."
        echo "Other addresses (not used):"
        echo "$ALL_TS_IPS" | tail -n +2 | sed 's/^/  /'
    fi

    save_config
    mark_phase_complete "0.4" "Tailscale authenticated; IP=$TAILSCALE_IP"
fi

# Load TAILSCALE_IP if we skipped 0.4 in this run.
if [ -z "${TAILSCALE_IP:-}" ] && [ -f "$CONFIG_FILE" ]; then
    load_config
fi

# ============================================================================
# PHASE 0.5 — Directory structure
# Note: Jellyfin/monitoring mkdirs omitted (out of scope, F#2).
# ============================================================================
if should_run_phase "0.5"; then
    banner "0.5" "Directory structure"

    # Prepare only the parent directory. Phase 1 owns creation/population of
    # $STACK_DIR so git clone is never asked to clone into a non-empty path.
    sudo mkdir -p "$(dirname "$STACK_DIR")"
    sudo chown "$USER:$USER" "$(dirname "$STACK_DIR")"

    mark_phase_complete "0.5" "Stack parent directory prepared"
fi

# ============================================================================
# PHASE 1 — Clone repositories
# ============================================================================
if should_run_phase "1"; then
    banner "1" "Clone AzerothCore + modules"

    clone_or_verify_core_repo

    cd "$STACK_DIR"
    mkdir -p "${STACK_DIR}/logs"
    relocate_log_if_possible

    clone_or_verify_repo \
        "https://github.com/mod-playerbots/mod-playerbots.git" \
        "master" \
        "${STACK_DIR}/modules/mod-playerbots" \
        "mod-playerbots" \
        "mod-playerbots/mod-playerbots"

    clone_or_verify_repo \
        "https://github.com/NathanHandley/mod-ah-bot-plus.git" \
        "master" \
        "${STACK_DIR}/modules/mod-ah-bot-plus" \
        "mod-ah-bot-plus" \
        "NathanHandley/mod-ah-bot-plus"

    clone_or_verify_repo \
        "https://github.com/ZhengPeiRu21/mod-individual-progression.git" \
        "master" \
        "${STACK_DIR}/modules/mod-individual-progression" \
        "mod-individual-progression" \
        "ZhengPeiRu21/mod-individual-progression"

    # VERIFY Phase 1
    echo ""
    echo "VERIFY Phase 1:"
    pwd
    git remote get-url origin
    git branch
    ls modules/mod-playerbots/ | head -10
    ls modules/mod-ah-bot-plus/ | head -10
    ls modules/mod-individual-progression/ | head -10
    ls modules/mod-ah-bot-plus/conf/

    mark_phase_complete "1" "Repos cloned"
fi

# ============================================================================
# PHASE 2.1 — Create .env
# Output redacted in log per F#1.
# ============================================================================
if should_run_phase "2.1"; then
    banner "2.1" "Create .env"

    cd "$STACK_DIR"

    # Guard sanity
    if [ -z "${DB_ROOT_PASSWORD:-}" ] || [ -z "${TAILSCALE_IP:-}" ]; then
        echo "ERROR: DB_ROOT_PASSWORD or TAILSCALE_IP not set."
        exit 1
    fi

    if [ -f .env ]; then
        echo "Existing .env found; validating required generated values and leaving it unchanged."
        ENV_FAIL=0
        for key in DOCKER_DB_ROOT_PASSWORD DOCKER_USER_ID DOCKER_GROUP_ID DOCKER_DB_EXTERNAL_PORT DOCKER_AUTH_EXTERNAL_PORT DOCKER_WORLD_EXTERNAL_PORT DOCKER_SOAP_EXTERNAL_PORT DOCKER_IMAGE_TAG; do
            if ! grep -qE "^${key}=" .env; then
                echo "  ✗ Missing ${key}"
                ENV_FAIL=1
            fi
        done
        if ! grep -qE "^COMPOSE_PROJECT_NAME=" .env; then
            echo "  → Adding missing COMPOSE_PROJECT_NAME=azerothcore to existing .env"
            printf '\nCOMPOSE_PROJECT_NAME=azerothcore\n' >> .env
        fi

        for expected in \
            "COMPOSE_PROJECT_NAME=azerothcore" \
            "DOCKER_DB_EXTERNAL_PORT=127.0.0.1:3306" \
            "DOCKER_AUTH_EXTERNAL_PORT=${TAILSCALE_IP}:3724" \
            "DOCKER_WORLD_EXTERNAL_PORT=${TAILSCALE_IP}:8085" \
            "DOCKER_SOAP_EXTERNAL_PORT=127.0.0.1:7878" \
            "DOCKER_IMAGE_TAG=playerbot-local"
        do
            if ! grep -qFx "$expected" .env; then
                echo "  ✗ .env does not contain expected line: $expected"
                ENV_FAIL=1
            fi
        done
        if [ "$ENV_FAIL" -ne 0 ]; then
            echo "ERROR: Existing .env does not match this installer's expected generated values."
            echo "Move it aside or use --force-fresh if you intend to wipe this stack."
            exit 10
        fi
        echo "Existing .env preview (DOCKER_DB_ROOT_PASSWORD redacted):"
        sed -E 's/^(DOCKER_DB_ROOT_PASSWORD=).*/\1<redacted>/' .env
    else
        cat > .env <<EOF
# AzerothCore Docker stack — secrets and configuration
# Do NOT commit this file to git
# Variable names match conf/dist/env.docker in the AzerothCore source repo

# Keep the Compose project stable even if commands are run from scripts/systemd.
COMPOSE_PROJECT_NAME=azerothcore

# MySQL root password — used by the ac-database container AND by all
# mysqldump / mysql exec commands later in this installer
DOCKER_DB_ROOT_PASSWORD=${DB_ROOT_PASSWORD}

# Run containers as the current user — avoids bind-mount permission issues
DOCKER_USER_ID=1000
DOCKER_GROUP_ID=1000

# Bind published ports safely. The base docker-compose.yml publishes these ports by default;
# binding them here prevents MySQL/SOAP from being exposed to the LAN and makes WoW access
# Tailscale-only. If the Tailscale IP ever changes, update these and recreate the containers.
DOCKER_DB_EXTERNAL_PORT=127.0.0.1:3306
DOCKER_AUTH_EXTERNAL_PORT=${TAILSCALE_IP}:3724
DOCKER_WORLD_EXTERNAL_PORT=${TAILSCALE_IP}:8085
DOCKER_SOAP_EXTERNAL_PORT=127.0.0.1:7878

# Tag locally-built images with a non-upstream tag so that an accidental
# \`docker compose pull\` cannot replace the playerbots-built worldserver with
# the upstream image of the same name (acore/ac-wotlk-worldserver:master).
DOCKER_IMAGE_TAG=playerbot-local
EOF

    chmod 600 .env

    # UID/GID substitution — run containers as the current user.
    sed -i "s/DOCKER_USER_ID=1000/DOCKER_USER_ID=$(id -u)/" .env
    sed -i "s/DOCKER_GROUP_ID=1000/DOCKER_GROUP_ID=$(id -g)/" .env

        echo "Created .env — preview (DOCKER_DB_ROOT_PASSWORD redacted):"
        sed -E 's/^(DOCKER_DB_ROOT_PASSWORD=).*/\1<redacted>/' .env
    fi

    chmod 600 .env
    mark_phase_complete "2.1" ".env created/verified"
fi

# ============================================================================
# PHASE 2.2 — Data directories
# ============================================================================
if should_run_phase "2.2"; then
    banner "2.2" "Create data directories"

    cd "$STACK_DIR"
    mkdir -p data/mysql
    mkdir -p data/sql/custom/db_characters
    mkdir -p data/sql/custom/db_world
    mkdir -p data/sql/custom/db_auth
    mkdir -p configs/mysql
    mkdir -p configs/modules
    mkdir -p backups
    mkdir -p logs

    mark_phase_complete "2.2" "Data directories created"
fi

# ============================================================================
# PHASE 2.3 — Clean Playerbots custom SQL duplicates
# ============================================================================
if should_run_phase "2.3"; then
    banner "2.3" "Clean Playerbots custom SQL duplicates"

    cd "$STACK_DIR"

    cleanup_playerbots_custom_sql_files
    assert_no_playerbots_sql_duplicates_in_custom

    # VERIFY 2.3
    echo ""
    echo "VERIFY 2.3 — custom SQL files remaining per database:"
    for d in db_auth db_characters db_world; do
        n=$(find "data/sql/custom/$d" -type f -name '*.sql' 2>/dev/null | wc -l)
        printf "  %-15s %d SQL file(s)\n" "$d:" "$n"
    done
    echo ""
    echo "Full list (sorted):"
    find data/sql/custom -type f -name "*.sql" | sort
    echo ""
    echo "Note: Phase 2.3 does not stage Playerbots SQL."
    echo "Playerbots SQL is left in modules/mod-playerbots so db-import/updater sees only one copy."
    echo "Any non-Playerbots custom SQL left under data/sql/custom is preserved."
    echo "acore_playerbots is created in Phase 4. Its tables are initialized by the"
    echo "mod-playerbots updater on worldserver startup, so AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES"
    echo "must remain set to 1."

    mark_phase_complete "2.3" "Playerbots custom SQL duplicates cleaned"
fi

# ============================================================================
# PHASE 2.4 — MySQL tuning
# INNODB_BUFFER_POOL_SIZE substituted from prompt; all other values verbatim.
# ============================================================================
if should_run_phase "2.4"; then
    banner "2.4" "MySQL tuning config (configs/mysql/custom.cnf)"

    cd "$STACK_DIR"

    ensure_mysql_custom_cnf_file

    echo "custom.cnf preview:"
    cat configs/mysql/custom.cnf

    mark_phase_complete "2.4" "MySQL tuning config written"
fi

# ============================================================================
# PHASE 2.5 — docker-compose.override.yml
# Heredoc is mostly verbatim. PLAYERBOT_COUNT and MAP_UPDATE_THREADS are
# substituted via anchored sed; SERVER_XP_RATE inserts optional worldserver
# Rate.* overrides using the same AC_* environment override pattern.
# ============================================================================
if should_run_phase "2.5"; then
    banner "2.5" "docker-compose.override.yml"

    cd "$STACK_DIR"

    if [ -f docker-compose.override.yml ]; then
        echo "Existing docker-compose.override.yml found; backing it up before regenerating."
        cp -a docker-compose.override.yml "docker-compose.override.yml.bak.${UNIX_TS}"
    fi

    cat > docker-compose.override.yml <<'EOF'
# AzerothCore + mod-playerbots + mod-ah-bot-plus override
# Applies automatically on top of docker-compose.yml — no -f flag needed

services:

  # NOTE: The upstream docker-compose.yml already sets explicit container_name
  # values on every service (ac-database, ac-db-import, ac-worldserver,
  # ac-authserver, ac-client-data-init), so we do NOT need to set them here.
  # We only override what we actually want to change.

  ac-database:
    volumes:
      - ./data/mysql:/var/lib/mysql
      - ./configs/mysql/custom.cnf:/etc/mysql/conf.d/custom.cnf:ro

  ac-worldserver:
    volumes:
      # Debug/source visibility only. Modules are compiled into the image at build time;
      # changing files under ./modules still requires docker compose build.
      - ./modules:/azerothcore/modules:ro
      # Live module configs. Phase 3.1 installs .conf.dist templates and creates .conf files here.
      - ./configs/modules:/azerothcore/env/dist/etc/modules:rw
      - ./logs:/azerothcore/env/dist/logs:rw
    environment:
      # ----- mod-playerbots -----
      AC_PLAYERBOTS_DATABASE_INFO: "ac-database;3306;root;${DOCKER_DB_ROOT_PASSWORD:-password};acore_playerbots"
      AC_AI_PLAYERBOT_ENABLED: "1"
      AC_AI_PLAYERBOT_RANDOM_BOT_AUTOLOGIN: "1"

      # Required for mod-playerbots to initialize and update its own
      # acore_playerbots schema on worldserver startup. Do not disable this:
      # with value 0, acore_playerbots can exist while required tables such as
      # playerbots_custom_strategy and playerbots_item_info_cache are missing.
      AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES: "1"

      # Fixed random bot count for this hardware target.
      AC_AI_PLAYERBOT_MIN_RANDOM_BOTS: "200"
      AC_AI_PLAYERBOT_MAX_RANDOM_BOTS: "200"

      # Conservative worldserver/playerbots performance settings for 6 physical cores / 12 threads.
      AC_MAP_UPDATE_THREADS: "4"
      AC_MAP_UPDATE_INTERVAL: "10"
      AC_MIN_WORLD_UPDATE_TIME: "1"
      AC_PRELOAD_ALL_NON_INSTANCED_MAP_GRIDS: "0"
      AC_DONT_CACHE_RANDOM_MOVEMENT_PATHS: "0"
      AC_QUESTS_IGNORE_AUTO_ACCEPT: "1"
      AC_PLAYER_LIMIT: "0"
      AC_LEAVE_GROUP_ON_LOGOUT_ENABLED: "1"

      # ----- mod-ah-bot-plus -----
      # Enable the seller (populates auctions). Required for the AH to fill.
      AC_AUCTION_HOUSE_BOT_ENABLE_SELLER: "true"
      # Enable the buyer (bots buy from player auctions).
      AC_AUCTION_HOUSE_BOT_BUYER_ENABLED: "true"
      # NOTE: AuctionHouseBot.GUIDs is set in configs/modules/mod_ahbot.conf
      # during Phase 6.1, after the user creates the AH bot character.

      # ----- core worldserver.conf overrides -----
      # Allow cross-faction interaction across the board so Alliance + Horde
      # can play together. Auction flips all houses to neutral and applies
      # the neutral AH cut; the others enable chat, calendar invites, custom
      # channels, parties, guilds, and arena teams across factions.
      AC_ALLOW_TWO_SIDE_INTERACTION_AUCTION: "1"
      AC_ALLOW_TWO_SIDE_INTERACTION_CHAT: "1"
      AC_ALLOW_TWO_SIDE_INTERACTION_CALENDAR: "1"
      AC_ALLOW_TWO_SIDE_INTERACTION_CHANNEL: "1"
      AC_ALLOW_TWO_SIDE_INTERACTION_GROUP: "1"
      AC_ALLOW_TWO_SIDE_INTERACTION_GUILD: "1"
      AC_ALLOW_TWO_SIDE_INTERACTION_ARENA: "1"

      # ----- mod-individual-progression -----
      # Required for the mod-IP world DB updater to pick up its SQL.
      # AzerothCore default is 7; set explicitly so the dependency is
      # visible at the override site.
      AC_UPDATES_ENABLE_DATABASES: "7"
      # Required: stores per-player progression data.
      AC_ENABLE_PLAYER_SETTINGS: "1"

      # ----- progression rate overrides -----

  ac-db-import:
    volumes:
      # Keep custom SQL mount points available, but do not copy Playerbots SQL here.
      # Playerbots SQL is loaded from modules/mod-playerbots directly; duplicate
      # basenames under data/sql/custom make AzerothCore's DB updater abort.
      - ./data/sql/custom/db_characters:/azerothcore/data/sql/custom/db_characters:ro
      - ./data/sql/custom/db_world:/azerothcore/data/sql/custom/db_world:ro
      - ./data/sql/custom/db_auth:/azerothcore/data/sql/custom/db_auth:ro
EOF

    # Substitutions (anchored both sides; confirmed sed lines from prior round)
    sed -i -E "s|^(      AC_AI_PLAYERBOT_MIN_RANDOM_BOTS: \")200(\")$|\1${PLAYERBOT_COUNT}\2|" docker-compose.override.yml
    sed -i -E "s|^(      AC_AI_PLAYERBOT_MAX_RANDOM_BOTS: \")200(\")$|\1${PLAYERBOT_COUNT}\2|" docker-compose.override.yml
    sed -i -E "s|^(      AC_MAP_UPDATE_THREADS: \")4(\")$|\1${MAP_UPDATE_THREADS}\2|" docker-compose.override.yml
    insert_xp_rate_overrides_into_compose docker-compose.override.yml

    # Verification greps (per spec refinement #1 — disambiguated messages)
    if ! grep -qE "^      AC_AI_PLAYERBOT_MIN_RANDOM_BOTS: \"${PLAYERBOT_COUNT}\"$" docker-compose.override.yml; then
        echo "ERROR: MIN_RANDOM_BOTS substitution did not match (expected ${PLAYERBOT_COUNT})"
        exit 1
    fi
    if ! grep -qE "^      AC_AI_PLAYERBOT_MAX_RANDOM_BOTS: \"${PLAYERBOT_COUNT}\"$" docker-compose.override.yml; then
        echo "ERROR: MAX_RANDOM_BOTS substitution did not match (expected ${PLAYERBOT_COUNT})"
        exit 1
    fi
    if ! grep -qE "^      AC_MAP_UPDATE_THREADS: \"${MAP_UPDATE_THREADS}\"$" docker-compose.override.yml; then
        echo "ERROR: MAP_UPDATE_THREADS substitution did not match (expected ${MAP_UPDATE_THREADS})"
        exit 1
    fi
    verify_xp_rate_overrides_in_compose docker-compose.override.yml

    for expected in \
        '      AC_MAP_UPDATE_INTERVAL: "10"' \
        '      AC_MIN_WORLD_UPDATE_TIME: "1"' \
        '      AC_PRELOAD_ALL_NON_INSTANCED_MAP_GRIDS: "0"' \
        '      AC_DONT_CACHE_RANDOM_MOVEMENT_PATHS: "0"' \
        '      AC_QUESTS_IGNORE_AUTO_ACCEPT: "1"' \
        '      AC_PLAYER_LIMIT: "0"' \
        '      AC_LEAVE_GROUP_ON_LOGOUT_ENABLED: "1"' \
        '      AC_UPDATES_ENABLE_DATABASES: "7"' \
        '      AC_ENABLE_PLAYER_SETTINGS: "1"' \
        '      AC_ALLOW_TWO_SIDE_INTERACTION_AUCTION: "1"' \
        '      AC_ALLOW_TWO_SIDE_INTERACTION_CHAT: "1"' \
        '      AC_ALLOW_TWO_SIDE_INTERACTION_CALENDAR: "1"' \
        '      AC_ALLOW_TWO_SIDE_INTERACTION_CHANNEL: "1"' \
        '      AC_ALLOW_TWO_SIDE_INTERACTION_GROUP: "1"' \
        '      AC_ALLOW_TWO_SIDE_INTERACTION_GUILD: "1"' \
        '      AC_ALLOW_TWO_SIDE_INTERACTION_ARENA: "1"'
    do
        if ! grep -qFx "$expected" docker-compose.override.yml; then
            echo "ERROR: Missing expected worldserver performance override: $expected"
            exit 1
        fi
    done
    if ! grep -qE '^      AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES: "1"$' docker-compose.override.yml; then
        echo "ERROR: AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES must be set to 1."
        exit 1
    fi

    echo "docker-compose.override.yml written and verified."
    mark_phase_complete "2.5" "Compose override written"
fi

# ============================================================================
# PHASE 2.6 — Compose validation
# ============================================================================
if should_run_phase "2.6"; then
    banner "2.6" "Compose validation"

    cd "$STACK_DIR"
    # shellcheck disable=SC1091
    source .env

    ensure_mysql_custom_cnf_file
    ensure_playerbots_updater_enabled_in_compose_override

    COMPOSE_EFFECTIVE="$(mktemp /tmp/ac-compose-effective.XXXXXX.yml)"
    chmod 600 "$COMPOSE_EFFECTIVE"
    docker compose config > "$COMPOSE_EFFECTIVE"

    fail=0

    # Required stable container names from the upstream compose file.
    for name in ac-database ac-db-import ac-worldserver ac-authserver ac-client-data-init; do
        if ! grep -qF "container_name: ${name}" "$COMPOSE_EFFECTIVE"; then
            echo "MISSING container_name: ${name}"
            fail=1
        fi
    done

    # Locally built playerbot image tag must be used for built AzerothCore images.
    for image in db-import worldserver authserver client-data; do
        expected="image: acore/ac-wotlk-${image}:${DOCKER_IMAGE_TAG:-playerbot-local}"
        if ! grep -qF "${expected}" "$COMPOSE_EFFECTIVE"; then
            echo "MISSING image: ${expected}"
            fail=1
        fi
    done

    # Published ports must match the intended binding model. Handle both short
    # (HOST_IP:HOST_PORT:CONTAINER_PORT) and long (host_ip:/published:) YAML formats,
    # since docker compose config may emit either depending on the Compose version.
    check_port () {
        local binding="$1"
        local host_ip="${binding%%:*}"
        local host_port="${binding##*:}"
        if grep -qF "${binding}:" "$COMPOSE_EFFECTIVE"; then
            return 0
        fi
        if grep -qE "host_ip:\s*${host_ip//./\\.}" "$COMPOSE_EFFECTIVE" \
            && grep -qE "published:\s*\"?${host_port}\"?" "$COMPOSE_EFFECTIVE"
        then
            return 0
        fi
        return 1
    }

    for binding in "${DOCKER_DB_EXTERNAL_PORT}" \
                   "${DOCKER_AUTH_EXTERNAL_PORT}" \
                   "${DOCKER_WORLD_EXTERNAL_PORT}" \
                   "${DOCKER_SOAP_EXTERNAL_PORT}"
    do
        if ! check_port "${binding}"; then
            echo "MISSING port binding: ${binding}"
            fail=1
        fi
    done

    # Worldserver overrides must be present in the effective config.
    for var in \
        AC_PLAYERBOTS_DATABASE_INFO \
        AC_AI_PLAYERBOT_ENABLED \
        AC_AI_PLAYERBOT_RANDOM_BOT_AUTOLOGIN \
        AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES \
        AC_AI_PLAYERBOT_MIN_RANDOM_BOTS \
        AC_AI_PLAYERBOT_MAX_RANDOM_BOTS \
        AC_AUCTION_HOUSE_BOT_ENABLE_SELLER \
        AC_AUCTION_HOUSE_BOT_BUYER_ENABLED \
        AC_ALLOW_TWO_SIDE_INTERACTION_AUCTION \
        AC_ALLOW_TWO_SIDE_INTERACTION_CHAT \
        AC_ALLOW_TWO_SIDE_INTERACTION_CALENDAR \
        AC_ALLOW_TWO_SIDE_INTERACTION_CHANNEL \
        AC_ALLOW_TWO_SIDE_INTERACTION_GROUP \
        AC_ALLOW_TWO_SIDE_INTERACTION_GUILD \
        AC_ALLOW_TWO_SIDE_INTERACTION_ARENA \
        AC_MAP_UPDATE_THREADS \
        AC_MAP_UPDATE_INTERVAL \
        AC_MIN_WORLD_UPDATE_TIME \
        AC_PRELOAD_ALL_NON_INSTANCED_MAP_GRIDS \
        AC_DONT_CACHE_RANDOM_MOVEMENT_PATHS \
        AC_QUESTS_IGNORE_AUTO_ACCEPT \
        AC_PLAYER_LIMIT \
        AC_LEAVE_GROUP_ON_LOGOUT_ENABLED \
        AC_UPDATES_ENABLE_DATABASES \
        AC_ENABLE_PLAYER_SETTINGS
    do
        if ! grep -qF "${var}" "$COMPOSE_EFFECTIVE"; then
            echo "MISSING env var: ${var}"
            fail=1
        fi
    done

    if ! grep -Eq 'AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES:[[:space:]]*"?1"?|AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES=1' "$COMPOSE_EFFECTIVE"; then
        echo "INVALID env var value: AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES must be 1"
        fail=1
    fi

    if ! verify_xp_rate_overrides_in_effective_compose "$COMPOSE_EFFECTIVE"; then
        fail=1
    fi

    for mount_target in \
        /etc/mysql/conf.d/custom.cnf \
        /azerothcore/data/sql/custom/db_characters \
        /azerothcore/data/sql/custom/db_world \
        /azerothcore/data/sql/custom/db_auth
    do
        if ! grep -qF "$mount_target" "$COMPOSE_EFFECTIVE"; then
            echo "MISSING mount target: $mount_target"
            fail=1
        fi
    done

    if [ "${fail}" -ne 0 ]; then
        echo "ERROR: Effective compose config is missing expected entries."
        echo "Inspect $COMPOSE_EFFECTIVE and re-create .env / docker-compose.override.yml before building."
        exit 1
    fi
    rm -f "$COMPOSE_EFFECTIVE"
    echo "Effective Compose configuration looks correct."

    mark_phase_complete "2.6" "Compose validation passed"
fi

# ============================================================================
# PHASE 3 — Build
# Expected duration 45-75 min on Ryzen 5 7430U.
# ============================================================================
if should_run_phase "3"; then
    banner "3" "Docker compose build (expected 45-75 min)"

    cd "$STACK_DIR"

    echo "Starting build at $(date -u +%Y-%m-%dT%H:%M:%SZ)..."
    if ! docker compose build 2>&1 | tee /tmp/ac-build.log; then
        echo ""
        echo "════════════════════════════════════════════════════════════════"
        echo "Build FAILED. Last 80 lines of /tmp/ac-build.log:"
        echo "════════════════════════════════════════════════════════════════"
        tail -80 /tmp/ac-build.log
        echo ""
        echo "Common causes: wrong repo, low memory, network failure during apt/pkg fetch."
        echo "Do NOT auto-retry — investigate first."
        exit 1
    fi
    echo "Build complete at $(date -u +%Y-%m-%dT%H:%M:%SZ)."

    # VERIFY Phase 3
    echo ""
    echo "VERIFY Phase 3:"
    # shellcheck disable=SC1091
    source .env

    docker images | grep -E "acore/ac-wotlk-(worldserver|authserver|db-import|client-data).*${DOCKER_IMAGE_TAG:-playerbot-local}"

    # Fatal-error grep (cosmetic warnings are expected and ignored)
    grep -iE "(^|[[:space:]])(fatal error|error:|undefined reference)" /tmp/ac-build.log \
        | grep -viE "warning:|deprecated" \
        | tail -20 || true

    # Confirm both modules picked up. Exact wording varies by revision.
    grep -iE "mod-playerbots|playerbots|mod-ah-bot-plus|ahbot" /tmp/ac-build.log | head -50 || true

    mark_phase_complete "3" "Docker images built"
fi

# ============================================================================
# PHASE 3.1 — Install module conf templates
# ============================================================================
if should_run_phase "3.1"; then
    banner "3.1" "Install module conf templates"

    cd "$STACK_DIR"
    # shellcheck disable=SC1091
    source .env

    WORLDSERVER_IMAGE="acore/ac-wotlk-worldserver:${DOCKER_IMAGE_TAG:-playerbot-local}"

    mkdir -p configs/modules

    # Verify the base runtime config path in the image.
    # Do not require /azerothcore/env/dist/etc/modules here: in this stack,
    # that directory is supplied by the host bind mount ./configs/modules at
    # container runtime, so it may legitimately be absent from the raw image.
    docker run --rm --entrypoint sh "${WORLDSERVER_IMAGE}" -c '
set -e
ls -ld /azerothcore/env/dist/etc
ls -ld /azerothcore/env/dist/logs || true
'

    need_extract=false
    required_templates=("mod_ahbot.conf.dist" "individualProgression.conf.dist")

    if [ ! -f configs/modules/playerbots.conf.dist ] && [ ! -f configs/modules/mod_playerbots.conf.dist ]; then
        need_extract=true
    fi

    for f in "${required_templates[@]}"; do
        if [ ! -f "configs/modules/${f}" ]; then
            need_extract=true
        fi
    done

    if [ "${need_extract}" = true ]; then
        echo "Installing module .conf.dist templates into ./configs/modules ..."
        echo "Source priority: checked-out module source tree first; built-image fallback only if available."

        found_templates=0
        while IFS= read -r -d '' src; do
            dest="./configs/modules/$(basename "$src")"
            if [ ! -f "$dest" ]; then
                cp "$src" "$dest"
                echo "Copied template: $src -> $dest"
            else
                echo "Template already present, not overwriting: $dest"
            fi
            found_templates=1
        done < <(find modules/mod-playerbots modules/mod-ah-bot-plus modules/mod-individual-progression \
            -type f -path '*/conf/*.conf.dist' -print0 2>/dev/null || true)

        # Fallback for future image layouts that may install module configs into
        # the image. The fallback is conditional because current builds may not
        # contain /azerothcore/env/dist/etc/modules until the bind mount exists.
        if [ "$found_templates" -eq 0 ]; then
            echo "No module .conf.dist files found in the source tree; checking built image fallback..."

            if docker run --rm --entrypoint sh "${WORLDSERVER_IMAGE}" \
                -c 'test -d /azerothcore/env/dist/etc/modules'
            then
                docker rm -f ac-conf-extract >/dev/null 2>&1 || true
                docker create --name ac-conf-extract "${WORLDSERVER_IMAGE}" >/dev/null
                docker cp ac-conf-extract:/azerothcore/env/dist/etc/modules/. ./configs/modules/
                docker rm ac-conf-extract >/dev/null
            else
                echo "ERROR: Could not find module .conf.dist templates in the source tree or image."
                echo "Checked:"
                echo "  modules/mod-playerbots/**/conf/*.conf.dist"
                echo "  modules/mod-ah-bot-plus/**/conf/*.conf.dist"
                echo "  modules/mod-individual-progression/**/conf/*.conf.dist"
                echo "  ${WORLDSERVER_IMAGE}:/azerothcore/env/dist/etc/modules"
                exit 1
            fi
        fi
    else
        echo "Required module .conf.dist templates already exist — skipping template installation."
    fi

    # Create live .conf files from every .conf.dist if missing. Never overwrite.
    for dist in configs/modules/*.conf.dist; do
        [ -e "$dist" ] || continue
        conf="${dist%.dist}"
        if [ ! -f "$conf" ]; then
            cp "$dist" "$conf"
            echo "Created live config: $conf"
        else
            echo "Live config already exists, not overwriting: $conf"
        fi
    done

    ls -la configs/modules/

    # VERIFY 3.1
    echo ""
    echo "VERIFY 3.1:"
    ls configs/modules/mod_ahbot.conf.dist
    ls configs/modules/mod_ahbot.conf
    ls configs/modules/playerbots.conf.dist 2>/dev/null || ls configs/modules/mod_playerbots.conf.dist
    ls configs/modules/playerbots.conf 2>/dev/null || ls configs/modules/mod_playerbots.conf
    ls configs/modules/individualProgression.conf.dist
    ls configs/modules/individualProgression.conf

    ensure_playerbots_performance_config

    mark_phase_complete "3.1" "Conf templates installed; live configs created; Playerbots performance profile applied"
fi

# ============================================================================
# PHASE 4 — First run + DB init + client data
# ============================================================================
if should_run_phase "4"; then
    banner "4" "First run (DB init + client data download)"

    cd "$STACK_DIR"
    # shellcheck disable=SC1091
    source .env
    VERIFY4_FAIL=0

    ensure_mysql_custom_cnf_file
    ensure_playerbots_updater_enabled_in_compose_override
    ensure_playerbots_performance_config
    cleanup_playerbots_custom_sql_files
    assert_no_playerbots_sql_duplicates_in_custom

    # Stop dependent services first, then recreate ac-database so custom.cnf
    # changes are guaranteed to be loaded even when resuming after a failed run.
    docker compose stop ac-worldserver ac-authserver ac-db-import ac-client-data-init 2>/dev/null || true
    docker compose up -d --force-recreate ac-database

    echo "Waiting for ac-database to become healthy..."
    TIMEOUT_SECS=300
    START=$(date +%s)
    until docker inspect --format='{{json .State.Health.Status}}' ac-database 2>/dev/null | grep -q '"healthy"'; do
        if (( $(date +%s) - START > TIMEOUT_SECS )); then
            echo "ERROR: ac-database did not become healthy within ${TIMEOUT_SECS}s."
            docker compose ps ac-database
            docker logs --tail 100 ac-database
            exit 1
        fi
        sleep 5
    done

    # Create the separate Playerbots database before worldserver starts.
    # The mod-playerbots updater creates/updates the tables inside it.
    docker exec ac-database mysql \
        -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" \
        -e "CREATE DATABASE IF NOT EXISTS acore_playerbots DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"

    # Start the rest of the stack. Scale phpmyadmin and ac-eluna-ts-dev to 0:
    # they're inherited from the upstream compose file but we don't need them,
    # and phpmyadmin's default port can collide with other services.
    # compose_scale_args emits nothing if those services don't exist, so this
    # remains forward-compatible if upstream removes them.
    mapfile -t SCALE_ARGS < <(compose_scale_args)
    if [ "${#SCALE_ARGS[@]}" -gt 0 ]; then
        echo "Scaling unwanted services to 0: ${SCALE_ARGS[*]}"
    fi
    docker compose up -d "${SCALE_ARGS[@]}"

    # Wait for short-lived init containers with timeout. We deliberately avoid
    # `docker wait` here: it blocks indefinitely if the container is wedged
    # (network stall during the ~600 MB client-data download, DB lock during
    # import). wait_for_init_container polls docker inspect and bails out with
    # a useful log dump if the wait exceeds the timeout.
    echo "Waiting for ac-client-data-init (downloads ~600 MB; up to 30 min)..."
    if ! wait_for_init_container ac-client-data-init 1800 "client data init"; then
        exit 1
    fi
    docker logs --tail 80 ac-client-data-init

    echo "Waiting for ac-db-import (imports world DB; up to 30 min)..."
    if ! wait_for_init_container ac-db-import 1800 "db import"; then
        exit 1
    fi
    docker logs --tail 120 ac-db-import

    # Restart worldserver after DB/client-data init so module updaters run
    # against the finalized database state. This also recovers cleanly if an
    # earlier failed run left worldserver exited before db-import completed.
    echo "Restarting ac-worldserver after DB/client-data initialization..."
    WORLD_RESTART_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    docker compose restart ac-worldserver
    wait_for_running_container ac-worldserver 180 "worldserver"
    wait_for_playerbots_schema 300 || VERIFY4_FAIL=1
    # Give the module updater a short grace period to flush final startup log lines
    # before the log-based verification reads only this restart's output.
    sleep 10

    # Status overview
    docker compose ps
    docker logs --since "$WORLD_RESTART_TS" --tail 250 ac-worldserver

    # VERIFY Phase 4
    echo ""
    echo "VERIFY Phase 4:"

    # Containers Up
    for c in ac-database ac-authserver ac-worldserver; do
        if [ "$(docker inspect --format='{{.State.Status}}' "$c" 2>/dev/null)" != "running" ]; then
            echo "  ✗ $c is not running"
            VERIFY4_FAIL=1
        else
            echo "  ✓ $c running"
        fi
    done

    # Required databases
    DBS_OUT="$(docker exec ac-database mysql -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" \
               -N -B -e "SHOW DATABASES;" 2>/dev/null || echo "")"
    for db in acore_auth acore_characters acore_world acore_playerbots; do
        if echo "$DBS_OUT" | grep -qFx "$db"; then
            echo "  ✓ Database $db exists"
        else
            echo "  ✗ Database $db missing"
            VERIFY4_FAIL=1
        fi
    done

    if echo "$DBS_OUT" | grep -qFx acore_playerbots; then
        verify_playerbots_schema_now || VERIFY4_FAIL=1
    fi

    if worldserver_has_playerbots_fatal_logs "$WORLD_RESTART_TS"; then
        echo "  ✗ Worldserver log contains fatal Playerbots database errors after this restart"
        print_worldserver_playerbots_fatal_logs "$WORLD_RESTART_TS"
        VERIFY4_FAIL=1
    else
        echo "  ✓ No fatal Playerbots database errors found in worldserver log after this restart"
    fi

    # MySQL tuning variables
    docker exec ac-database mysql -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" -e "
SHOW VARIABLES WHERE Variable_name IN (
  'innodb_buffer_pool_size',
  'innodb_io_capacity',
  'innodb_io_capacity_max',
  'innodb_use_fdatasync',
  'transaction_isolation',
  'log_bin',
  'sync_binlog',
  'innodb_flush_log_at_trx_commit'
);
"
    verify_mysql_tuning_active || VERIFY4_FAIL=1

    if [ "$VERIFY4_FAIL" -ne 0 ]; then
        echo ""
        echo "════════════════════════════════════════════════════════════════"
        echo "✗ Phase 4 VERIFY failed."
        echo ""
        echo "Common causes: Playerbots updater disabled, acore_playerbots missing/empty,"
        echo "or MySQL custom.cnf missing, mounted as a directory, ignored, or not active."
        echo "Diagnostic commands you can run by hand from this directory:"
        echo ""
        echo "  # 1. Look for playerbots/SQL errors in the worldserver log:"
        echo "  docker logs ac-worldserver 2>&1 \\"
        echo "    | grep -iE 'playerbot|database|sql|error|denied' | tail -60"
        echo ""
        echo "  # 2. Confirm all four expected databases exist:"
        echo "  docker exec ac-database mysql \\"
        echo "    -uroot -p\"\$(grep ^DOCKER_DB_ROOT_PASSWORD .env | cut -d= -f2-)\" \\"
        echo "    -e \"SHOW DATABASES LIKE 'acore_%';\""
        echo ""
        echo "  # 3. If acore_playerbots is missing, create it:"
        echo "  docker exec ac-database mysql \\"
        echo "    -uroot -p\"\$(grep ^DOCKER_DB_ROOT_PASSWORD .env | cut -d= -f2-)\" \\"
        echo "    -e \"CREATE DATABASE IF NOT EXISTS acore_playerbots;\""
        echo ""
        echo "  # 4. Restart worldserver so it re-runs its bot-table init:"
        echo "  docker compose restart ac-worldserver"
        echo ""
        echo "  # 5. Once the failing check would pass, resume from Phase 4:"
        echo "  $0 --resume-from=4"
        echo "════════════════════════════════════════════════════════════════"
        exit 1
    fi

    mark_phase_complete "4" "First run complete; DB + client data initialized"
fi

# ============================================================================
# PAUSE 2 — Account creation via worldserver console (between Phase 4 and 5)
# Combined into one console session.
# ============================================================================
if should_run_phase "pause-2"; then
    banner "pause-2" "Account creation (GM + AHBOT) via worldserver console"

    # shellcheck disable=SC1091
    source "${STACK_DIR}/.env"

    # Make sure worldserver is up before asking the user to attach
    if [ "$(docker inspect --format='{{.State.Status}}' ac-worldserver 2>/dev/null)" != "running" ]; then
        echo "ERROR: ac-worldserver is not running. Start the stack and retry."
        exit 1
    fi

    PAUSE2_ATTEMPT=0
    PAUSE2_MAX=3

    # NOTE on `account create` syntax: current AzerothCore takes
    # `account create <user> <password> [email]` (2 required args, email
    # optional). Some older guides and 3rd-party scripts use a 3-arg
    # `account create USER PASS PASS` form (password duplicated for
    # confirmation), which on current AC is interpreted as `email=PASS`
    # and silently fails email validation, leaving the account uncreated.
    # Stick with the 2-arg form below.

    while true; do
        PAUSE2_ATTEMPT=$((PAUSE2_ATTEMPT + 1))

        echo ""
        echo "════════════════════════════════════════════════════════════════"
        echo "MANUAL STEP 2 of 3: Create accounts via worldserver console"
        echo "  (attempt ${PAUSE2_ATTEMPT}/${PAUSE2_MAX})"
        echo "════════════════════════════════════════════════════════════════"
        echo ""
        echo "Open a NEW terminal on this server. Run:"
        echo ""
        echo "    docker attach ac-worldserver"
        echo ""
        echo "Then type EXACTLY these THREE commands, one per line, pressing"
        echo "Enter after each. Passwords are redacted in the install log."
        echo ""
        echo "    account create ${GM_USERNAME} <GM_PASSWORD>"
        echo "    account set gmlevel ${GM_USERNAME} 3 -1"
        echo "    account create AHBOT <AHBOT_PASSWORD>"
        echo ""
        if [ -w /dev/tty ]; then
            {
                echo ""
                echo "Actual account commands for manual entry (not written to install log):"
                echo "    account create ${GM_USERNAME} ${GM_PASSWORD}"
                echo "    account set gmlevel ${GM_USERNAME} 3 -1"
                echo "    account create AHBOT ${AHBOT_PASSWORD}"
                echo ""
            } > /dev/tty
        else
            echo "WARNING: /dev/tty is not writable, so actual passwords cannot be printed outside the log."
            echo "Use the GM_PASSWORD and AHBOT_PASSWORD values from $CONFIG_FILE to complete this manual step."
        fi
        echo "Detach with Ctrl+P then Ctrl+Q. Do NOT press Ctrl+C — it kills"
        echo "the worldserver."
        echo ""
        read -rp "When done, press Enter to continue..." _ignored

        # SQL verification — both accounts must exist.
        # AzerothCore stores usernames uppercase via SRP6; query with toupper.
        echo ""
        echo "Verifying accounts in acore_auth.account..."
        GM_UPPER="$(echo "$GM_USERNAME" | tr '[:lower:]' '[:upper:]')"
        FOUND="$(docker exec ac-database mysql -N -B \
            -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" \
            -e "SELECT username FROM acore_auth.account WHERE username IN ('${GM_UPPER}', 'AHBOT');" 2>/dev/null || echo "")"

        FOUND_GM=0
        FOUND_AHBOT=0
        echo "$FOUND" | grep -qFx "$GM_UPPER" && FOUND_GM=1
        echo "$FOUND" | grep -qFx "AHBOT" && FOUND_AHBOT=1

        if [ "$FOUND_GM" -eq 1 ] && [ "$FOUND_AHBOT" -eq 1 ]; then
            echo "  ✓ Both accounts present: $GM_UPPER, AHBOT"
            break
        fi

        echo "  ✗ Expected to find: $GM_UPPER, AHBOT"
        echo "    Actually found:   $(echo "$FOUND" | paste -sd, -)"

        if [ "$PAUSE2_ATTEMPT" -ge "$PAUSE2_MAX" ]; then
            echo "ERROR: ${PAUSE2_MAX} attempts exhausted. Aborting."
            exit 1
        fi
        echo "Retrying — please ensure you typed the commands exactly as shown above."
    done

    mark_phase_complete "pause-2" "GM + AHBOT accounts created"
fi

# ============================================================================
# PHASE 5 — Networking (Tailscale realmlist)
# ============================================================================
if should_run_phase "5"; then
    banner "5" "Networking — update realmlist for Tailscale"

    cd "$STACK_DIR"
    # shellcheck disable=SC1091
    source .env

    # Format check: Tailscale CGNAT addresses are always 100.x.x.x.
    if ! printf '%s\n' "$TAILSCALE_IP" | grep -Eq '^100\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$'; then
        echo "ERROR: TAILSCALE_IP '$TAILSCALE_IP' is not a valid 100.x.x.x address"
        exit 1
    fi

    # Host-assignment check: the IP must actually be one of ours right now.
    if ! tailscale ip -4 2>/dev/null | grep -Fxq "$TAILSCALE_IP"; then
        echo "ERROR: $TAILSCALE_IP is not currently assigned to this host by Tailscale."
        tailscale ip -4 2>/dev/null || true
        exit 1
    fi

    docker exec ac-database mysql \
        -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" acore_auth \
        -e "UPDATE realmlist SET address='${TAILSCALE_IP}' WHERE id=1;"

    docker exec ac-database mysql \
        -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" acore_auth \
        -e "SELECT id, name, address, port FROM realmlist;"

    docker compose restart ac-authserver
    sleep 3
    docker logs --tail 80 ac-authserver

    # VERIFY Phase 5 networking exposure
    echo ""
    echo "VERIFY Phase 5 networking exposure:"
    ss -ltnp 2>/dev/null | grep -E ':(3306|3724|8085|7878)\b' || true
    docker port ac-database || true
    docker port ac-authserver || true
    docker port ac-worldserver || true

    mark_phase_complete "5" "Realmlist updated; authserver restarted"
fi

# ============================================================================
# PHASE 5.1 — UFW firewall (conditional)
# ============================================================================
if should_run_phase "5.1"; then
    if [ "$INSTALL_UFW" != "y" ]; then
        banner "5.1" "UFW firewall — SKIPPED (INSTALL_UFW=n)"
        mark_phase_complete "5.1" "UFW skipped (user opted out)"
    else
        banner "5.1" "UFW firewall configuration"

        sudo ufw allow ssh

        if ip link show tailscale0 >/dev/null 2>&1; then
            sudo ufw allow in on tailscale0
            echo "tailscale0 interface allowed in UFW."
        else
            echo "ERROR: tailscale0 interface not found."
            echo "Run Phase 0.4 (Tailscale install + authentication) before continuing."
            echo "Do NOT enable UFW yet, or you will lose Tailscale access."
            exit 1
        fi

        sudo ufw --force enable
        sudo ufw status verbose

        mark_phase_complete "5.1" "UFW configured + enabled"
    fi
fi

# ============================================================================
# PAUSE 3 — AH bot character creation in WoW client
# ============================================================================
if should_run_phase "pause-3"; then
    banner "pause-3" "AH bot character creation (WoW client)"

    # shellcheck disable=SC1091
    source "${STACK_DIR}/.env"

    PAUSE3_ATTEMPT=0
    PAUSE3_MAX=3
    GUIDS=""

    while true; do
        PAUSE3_ATTEMPT=$((PAUSE3_ATTEMPT + 1))

        echo ""
        echo "════════════════════════════════════════════════════════════════"
        echo "MANUAL STEP 3 of 3: Create AH bot character(s) in WoW client"
        echo "  (attempt ${PAUSE3_ATTEMPT}/${PAUSE3_MAX})"
        echo "════════════════════════════════════════════════════════════════"
        echo ""
        echo "From your WoW 3.3.5a client (which must be connected to Tailscale):"
        echo ""
        echo "  1. Set realmlist.wtf to: set realmlist ${TAILSCALE_IP}"
        echo "  2. Log in to the WoW client with the AHBOT account credentials"
        echo "  3. Create ${AHBOT_CHARACTER_COUNT} character(s). Names will appear"
        echo "     publicly in the auction house — pick natural-sounding names."
        echo "  4. After creation, log out completely. Do not play these characters."
        echo ""
        echo "Tailscale IP for client realmlist.wtf: ${TAILSCALE_IP}"
        echo ""
        read -rp "When done, press Enter to continue..." _ignored

        # Capture GUIDs
        GUIDS="$(docker exec ac-database mysql -N -B \
            -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" \
            -e "SELECT c.guid FROM acore_characters.characters c
                INNER JOIN acore_auth.account a ON c.account = a.id
                WHERE a.username = 'AHBOT';" 2>/dev/null | paste -sd, -)"

        # Trim whitespace
        GUIDS="${GUIDS//$'\t'/}"
        GUIDS="${GUIDS//$' '/}"

        if [ -z "$GUIDS" ]; then
            echo "  ✗ No characters found on the AHBOT account."
            if [ "$PAUSE3_ATTEMPT" -ge "$PAUSE3_MAX" ]; then
                echo "ERROR: ${PAUSE3_MAX} attempts exhausted. Aborting."
                exit 1
            fi
            echo "Retrying — ensure the character(s) have been created and you've logged out."
            continue
        fi

        ACTUAL_COUNT=$(echo "$GUIDS" | tr ',' '\n' | grep -c .)
        echo "  ✓ Found GUIDs: $GUIDS  (count: $ACTUAL_COUNT, expected: $AHBOT_CHARACTER_COUNT)"

        if [ "$ACTUAL_COUNT" -ne "$AHBOT_CHARACTER_COUNT" ]; then
            echo ""
            echo "  ⚠ Actual character count ($ACTUAL_COUNT) differs from configured ($AHBOT_CHARACTER_COUNT)."
            prompt_yn "Proceed with actual count of ${ACTUAL_COUNT}?" y
            if [ "$PROMPT_RESULT" != "y" ]; then
                if [ "$PAUSE3_ATTEMPT" -ge "$PAUSE3_MAX" ]; then
                    echo "ERROR: ${PAUSE3_MAX} attempts exhausted. Aborting."
                    exit 1
                fi
                continue
            fi
        fi
        break
    done

    # Persist GUIDS into config so the next phase can pick it up across resume.
    # Also set the in-memory variable: this ensures that any save_config call
    # later in the same process (e.g. a future code path that re-saves config)
    # will preserve the GUIDs via save_config's AHBOT_GUIDS-preservation block.
    AHBOT_GUIDS="${GUIDS}"
    set_config_value "AHBOT_GUIDS" "${GUIDS}" "$CONFIG_FILE"
    mark_phase_complete "pause-3" "AHBOT characters created; GUIDs=${GUIDS}"
fi

# Load AHBOT_GUIDS if Pause 3 ran in a prior invocation
if [ -z "${AHBOT_GUIDS:-}" ] && [ -f "$CONFIG_FILE" ]; then
    load_config
fi

# ============================================================================
# PHASE 6.1.4 — Write GUIDs into mod_ahbot.conf
# ============================================================================
if should_run_phase "6.1.4"; then
    banner "6.1.4" "Write GUID(s) into mod_ahbot.conf"

    cd "$STACK_DIR"

    AHBOT_CONF="configs/modules/mod_ahbot.conf"
    AHBOT_DIST="configs/modules/mod_ahbot.conf.dist"

    if [ ! -f "$AHBOT_DIST" ]; then
        echo "ERROR: Missing $AHBOT_DIST"
        echo "Run Phase 3.1 again to install module config templates from the module source tree."
        exit 1
    fi

    if [ ! -f "$AHBOT_CONF" ]; then
        cp "$AHBOT_DIST" "$AHBOT_CONF"
        echo "Created live config: $AHBOT_CONF"
    else
        echo "Existing live config found. It will not be replaced from .dist."
        echo "Relevant settings before cleanup:"
        grep -nE "^[[:space:]]*AuctionHouseBot\.(GUIDs|EnableSeller|Buyer\.Enabled)[[:space:]]*=" "$AHBOT_CONF" || \
            echo "  (none found)"
    fi

    YOUR_GUIDS="${AHBOT_GUIDS:-}"

    if ! printf '%s\n' "$YOUR_GUIDS" | grep -Eq '^[0-9]+(,[0-9]+)*$'; then
        echo "ERROR: YOUR_GUIDS must be a comma-separated list of numeric GUIDs, e.g. 7 or 7,8"
        echo "Current value: '${YOUR_GUIDS}'"
        exit 1
    fi

    echo "Canonicalizing AH bot settings in $AHBOT_CONF ..."
    echo "This removes duplicate active/commented instances of the managed keys and appends one clean value each."

    set_conf_key "AuctionHouseBot.GUIDs" "$YOUR_GUIDS" "$AHBOT_CONF"
    set_conf_key "AuctionHouseBot.EnableSeller" "true" "$AHBOT_CONF"
    set_conf_key "AuctionHouseBot.Buyer.Enabled" "true" "$AHBOT_CONF"

    require_conf_key_once "AuctionHouseBot.GUIDs" "$YOUR_GUIDS" "$AHBOT_CONF"
    require_conf_key_once "AuctionHouseBot.EnableSeller" "true" "$AHBOT_CONF"
    require_conf_key_once "AuctionHouseBot.Buyer.Enabled" "true" "$AHBOT_CONF"

    echo "Relevant settings after cleanup:"
    grep -nE "^[[:space:]]*AuctionHouseBot\.(GUIDs|EnableSeller|Buyer\.Enabled)[[:space:]]*=" "$AHBOT_CONF"

    # If worldserver is already running, prove that the bind-mounted in-container
    # config view sees the same de-duplicated file. Do not fail here if the
    # container is not running yet; Phase 6.1.5 restarts and verifies it.
    if [ "$(docker inspect --format='{{.State.Status}}' ac-worldserver 2>/dev/null || echo missing)" = "running" ]; then
        echo "Verifying bind-mounted AH bot config visible inside ac-worldserver:"
        docker exec -i ac-worldserver sh -s <<'SH'
set -e
conf=/azerothcore/env/dist/etc/modules/mod_ahbot.conf
test -f "$conf"
grep -nE '^[[:space:]]*AuctionHouseBot\.(GUIDs|EnableSeller|Buyer\.Enabled)[[:space:]]*=' "$conf"
for key in AuctionHouseBot.GUIDs AuctionHouseBot.EnableSeller AuctionHouseBot.Buyer.Enabled; do
    escaped=$(printf '%s' "$key" | sed 's/[.[\*^$()+?{}|]/\\&/g')
    count=$(grep -Ec "^[[:space:]]*${escaped}[[:space:]]*=" "$conf" || true)
    if [ "$count" != "1" ]; then
        echo "ERROR: ${key} appears ${count} time(s) in ${conf}; expected exactly 1."
        exit 1
    fi
done
SH
    fi

    mark_phase_complete "6.1.4" "mod_ahbot.conf canonicalized with GUIDs ${YOUR_GUIDS}"
fi

# ============================================================================
# PHASE 6.1.5 — Worldserver restart + AH verify
# ============================================================================
if should_run_phase "6.1.5"; then
    banner "6.1.5" "Worldserver restart + AH verify"

    cd "$STACK_DIR"
    # shellcheck disable=SC1091
    source .env

    RESTART_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    docker compose restart ac-worldserver

    echo "Waiting 15s for worldserver to come back..."
    sleep 15
    wait_for_running_container "ac-worldserver" 120 "worldserver"

    echo ""
    echo "VERIFY AH bot config inside worldserver container:"
    docker exec -i ac-worldserver sh -s <<'SH'
set -e
conf=/azerothcore/env/dist/etc/modules/mod_ahbot.conf
test -f "$conf"
grep -nE '^[[:space:]]*AuctionHouseBot\.(GUIDs|EnableSeller|Buyer\.Enabled)[[:space:]]*=' "$conf"
for key in AuctionHouseBot.GUIDs AuctionHouseBot.EnableSeller AuctionHouseBot.Buyer.Enabled; do
    escaped=$(printf '%s' "$key" | sed 's/[.[\*^$()+?{}|]/\\&/g')
    count=$(grep -Ec "^[[:space:]]*${escaped}[[:space:]]*=" "$conf" || true)
    if [ "$count" != "1" ]; then
        echo "ERROR: ${key} appears ${count} time(s) in ${conf}; expected exactly 1."
        exit 1
    fi
done
SH

    if docker logs --since "$RESTART_TS" ac-worldserver 2>&1 | grep -qE "Duplicate key name 'AuctionHouseBot\.(GUIDs|EnableSeller|Buyer\.Enabled)'"; then
        echo "ERROR: worldserver still reported duplicate AH bot config keys after restart."
        docker logs --since "$RESTART_TS" ac-worldserver 2>&1 | grep -E "Duplicate key name 'AuctionHouseBot\.(GUIDs|EnableSeller|Buyer\.Enabled)'" || true
        exit 1
    fi

    echo ""
    echo "Recent worldserver AH bot log lines (population may take 1-5 minutes):"
    docker logs --since "$RESTART_TS" ac-worldserver 2>&1 | grep -iE "ahbot|auction|AuctionHouseBot" || \
        echo "  (no ahbot/auction log lines yet — give it a few minutes)"

    echo ""
    echo "Auctions table count (informational; full population takes hours):"
    docker exec ac-database mysql \
        -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" acore_characters \
        -e "SELECT COUNT(*) AS total_auctions FROM auctionhouse;" || true

    docker exec ac-database mysql \
        -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" acore_characters \
        -e "SELECT itemowner, COUNT(*) FROM auctionhouse GROUP BY itemowner LIMIT 10;" || true

    mark_phase_complete "6.1.5" "Worldserver restarted; AH bot config verified"
fi

# ============================================================================
# PHASE 7 — Backup script + cron
# backup.sh heredoc is fixed content; no substitutions.
# ============================================================================
if should_run_phase "7"; then
    banner "7" "Backup script + cron"

    cd "$STACK_DIR"

    if [ -f "${STACK_DIR}/backup.sh" ]; then
        echo "Existing backup.sh found; backing it up before regenerating."
        cp -a "${STACK_DIR}/backup.sh" "${STACK_DIR}/backup.sh.bak.${UNIX_TS}"
    fi

    cat > "${STACK_DIR}/backup.sh" <<'SCRIPT'
#!/bin/bash
set -euo pipefail
umask 077

STACK_DIR=/opt/stacks/azerothcore
BACKUP_DIR="${STACK_DIR}/backups"
DATE=$(date +%F)

# Load secrets — required for MySQL authentication.
source "${STACK_DIR}/.env"

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"

echo "[$(date)] Starting AzerothCore backup..."

if ! docker inspect ac-database >/dev/null 2>&1; then
    echo "[$(date)] ERROR: ac-database container does not exist."
    exit 1
fi

for DB in acore_auth acore_characters acore_world acore_playerbots; do
    if docker exec ac-database mysql \
        -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" \
        -e "USE ${DB};" >/dev/null 2>&1; then

        docker exec ac-database mysqldump \
            -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" \
            --single-transaction --routines --triggers --events "${DB}" \
            > "${BACKUP_DIR}/${DB}-${DATE}.sql"

        chmod 600 "${BACKUP_DIR}/${DB}-${DATE}.sql"
        echo "[$(date)] Backed up ${DB}"
    else
        echo "[$(date)] WARNING: Database ${DB} does not exist or is inaccessible; skipping."
    fi
done

# Back up configuration and revision metadata. These are required for a clean restore.
# --warning=no-file-changed suppresses the legitimate noise of files being touched during
# the tar, but a real failure (disk full, permission, archive corruption) is still raised.
if ! tar -czf "${BACKUP_DIR}/azerothcore-config-${DATE}.tar.gz" \
        -C "${STACK_DIR}" \
        .env docker-compose.override.yml configs \
        --warning=no-file-changed
then
    echo "[$(date)] ERROR: config tar failed."
    exit 1
fi
chmod 600 "${BACKUP_DIR}/azerothcore-config-${DATE}.tar.gz"

{
    echo "core $(cd "${STACK_DIR}" && git rev-parse HEAD 2>/dev/null || echo unknown)"
    echo "mod-playerbots $(cd "${STACK_DIR}/modules/mod-playerbots" && git rev-parse HEAD 2>/dev/null || echo unknown)"
    echo "mod-ah-bot-plus $(cd "${STACK_DIR}/modules/mod-ah-bot-plus" && git rev-parse HEAD 2>/dev/null || echo unknown)"
} > "${BACKUP_DIR}/git-revisions-${DATE}.txt"
chmod 600 "${BACKUP_DIR}/git-revisions-${DATE}.txt"

# Rotate: keep 7 days only. Increase this if the server matters long-term.
find "${BACKUP_DIR}" -name "*.sql" -mtime +7 -delete
find "${BACKUP_DIR}" -name "azerothcore-config-*.tar.gz" -mtime +7 -delete
find "${BACKUP_DIR}" -name "git-revisions-*.txt" -mtime +7 -delete

echo "[$(date)] Backup complete."
ls -lh "${BACKUP_DIR}"
SCRIPT

    chmod +x "${STACK_DIR}/backup.sh"

    # Cron entry — idempotent and canonicalized. Avoid piping directly into
    # `crontab -` under `set -euo pipefail`; using temp files gives clearer
    # errors and lets us remove stale/older backup entries for this stack.
    CRON_ENTRY="0 3 * * * /opt/stacks/azerothcore/backup.sh >> /opt/stacks/azerothcore/logs/backup.log 2>&1"

    if ! command -v crontab >/dev/null 2>&1; then
        echo "crontab command not found; installing cron."
        sudo apt-get update
        sudo apt-get install -y cron
    fi
    sudo systemctl enable --now cron >/dev/null 2>&1 || \
        echo "WARNING: Could not enable/start cron via systemctl; continuing because crontab may still be usable."

    CRON_TMP="$(mktemp)"
    CRON_NEW="$(mktemp)"
    crontab -l > "$CRON_TMP" 2>/dev/null || true

    if grep -qF "${CRON_ENTRY}" "$CRON_TMP"; then
        echo "Cron entry already exists — skipping."
    else
        grep -vF "/opt/stacks/azerothcore/backup.sh" "$CRON_TMP" > "$CRON_NEW" || true
        printf '%s\n' "${CRON_ENTRY}" >> "$CRON_NEW"
        if ! crontab "$CRON_NEW"; then
            echo "ERROR: Failed to install backup cron entry. Candidate crontab follows:"
            sed 's/^/  /' "$CRON_NEW" || true
            rm -f "$CRON_TMP" "$CRON_NEW"
            exit 1
        fi
        echo "Cron entry added or updated."
    fi

    rm -f "$CRON_TMP" "$CRON_NEW"

    echo "Installed AzerothCore backup cron entry:"
    crontab -l | grep -F "/opt/stacks/azerothcore/backup.sh"

    # Run a test backup so verify-azerothcore.sh check #12 has something to find
    echo ""
    echo "Running initial backup as a smoke test..."
    /opt/stacks/azerothcore/backup.sh
    ls -lh /opt/stacks/azerothcore/backups/

    mark_phase_complete "7" "Backup script + cron installed; initial backup done"
fi

# ============================================================================
# PHASE 8 — Systemd auto-start (conditional)
# Heredoc is fixed content; only REPLACE_WITH_YOUR_USERNAME gets sed-substituted below.
# ============================================================================
if should_run_phase "8"; then
    if [ "$ENABLE_SYSTEMD" != "y" ]; then
        banner "8" "Systemd auto-start — SKIPPED (ENABLE_SYSTEMD=n)"
        mark_phase_complete "8" "Systemd skipped (user opted out)"
    else
        banner "8" "Systemd auto-start unit"

        sudo systemctl is-enabled docker || true
        sudo systemctl is-enabled tailscaled || true

        # Detect which "extras" services exist in the effective compose config
        # right now, and bake their --scale=0 args into the unit's ExecStart.
        # We don't redetect at boot because the compose file is part of the
        # cloned repo and effectively immutable on this host.
        SYSTEMD_SCALE_FRAGMENT=""
        EFFECTIVE_SERVICES="$(cd "$STACK_DIR" && docker compose config --services 2>/dev/null || echo "")"
        for svc in phpmyadmin ac-eluna-ts-dev; do
            if echo "$EFFECTIVE_SERVICES" | grep -qx "$svc"; then
                SYSTEMD_SCALE_FRAGMENT="${SYSTEMD_SCALE_FRAGMENT} --scale ${svc}=0"
            fi
        done
        if [ -n "$SYSTEMD_SCALE_FRAGMENT" ]; then
            echo "Systemd unit will scale these services to 0:${SYSTEMD_SCALE_FRAGMENT}"
        fi

        sudo tee /etc/systemd/system/azerothcore.service <<'EOF' >/dev/null
[Unit]
Description=AzerothCore + Playerbots Docker Stack
Requires=docker.service tailscaled.service
Wants=network-online.target
After=docker.service tailscaled.service network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=REPLACE_WITH_YOUR_USERNAME
WorkingDirectory=/opt/stacks/azerothcore

# Wait until the Tailscale IP configured in .env is assigned locally.
# Prefer `tailscale ip -4` because it reflects Tailscale state directly.
# Keep a tailscale0 interface fallback, but match a real CIDR suffix such as /32.
ExecStartPre=/bin/bash -lc 'source /opt/stacks/azerothcore/.env; TS_IP="${DOCKER_AUTH_EXTERNAL_PORT%%:*}"; if [ -z "$TS_IP" ]; then echo "ERROR: DOCKER_AUTH_EXTERNAL_PORT is empty or missing in /opt/stacks/azerothcore/.env"; exit 1; fi; for i in {1..60}; do tailscale ip -4 2>/dev/null | grep -Fxq "$TS_IP" && exit 0; ip -4 -o addr show dev tailscale0 2>/dev/null | grep -Eq "inet ${TS_IP//./\\.}/[0-9]+" && exit 0; echo "Waiting for Tailscale IP $TS_IP..."; sleep 2; done; echo "ERROR: Tailscale IP $TS_IP is not assigned locally"; echo "tailscale ip -4:"; tailscale ip -4 2>/dev/null || true; echo "tailscale0 IPv4 addresses:"; ip -4 -o addr show dev tailscale0 2>/dev/null || true; exit 1'

ExecStart=/usr/bin/docker compose up -d --no-build REPLACE_WITH_SCALE_FRAGMENT
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
EOF

        sudo sed -i "s/REPLACE_WITH_YOUR_USERNAME/$(whoami)/" /etc/systemd/system/azerothcore.service
        # Bake in the scale fragment we detected above. The trailing space on the
        # placeholder ensures clean substitution whether the fragment is empty or not.
        sudo sed -i "s| REPLACE_WITH_SCALE_FRAGMENT|${SYSTEMD_SCALE_FRAGMENT}|" /etc/systemd/system/azerothcore.service

        sudo systemctl daemon-reload
        sudo systemctl enable azerothcore
        sudo systemctl reset-failed azerothcore || true
        sudo systemctl start azerothcore
        sudo systemctl status azerothcore --no-pager
        sudo systemctl is-active --quiet azerothcore

        mark_phase_complete "8" "Systemd unit installed + enabled + verified active"
    fi
fi

# ============================================================================
# Finalisation
# ============================================================================

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "✓ Installer complete."
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "Stack:           ${STACK_DIR}"
echo "Tailscale IP:    ${TAILSCALE_IP:-<unknown>}"
echo "Log:             ${LOG_FILE}"
echo "State file:      ${STATE_FILE}"
echo ""
echo "Post-install tuning:"
echo "  - AH bot:       edit ${STACK_DIR}/configs/modules/mod_ahbot.conf, then run"
echo "                  '.ahbot reload' in the WoW client as your GM character"
echo "                  to apply without restarting the worldserver."
echo "  - Playerbots:   edit ${STACK_DIR}/configs/modules/playerbots.conf (or"
echo "                  mod_playerbots.conf), then 'docker compose restart"
echo "                  ac-worldserver'."
echo "  - MySQL tuning: edit ${STACK_DIR}/configs/mysql/custom.cnf, then"
echo "                  'docker compose restart ac-database'."
echo ""
echo "Verify with:     ./verify-azerothcore.sh"
echo ""
echo "Auction house population takes hours at default ItemsPerCycle=150."
echo "Playerbot spawning takes several minutes after worldserver init."
echo ""

# Shred persisted config now that install is done (per F#4)
shred_config

echo "════════════════════════════════════════════════════════════════"
