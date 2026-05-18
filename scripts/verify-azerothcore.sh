#!/bin/bash
# verify-azerothcore.sh — post-install verification.
#
# Exits 0 if every check passes, 1 if any check fails.
# Prints one line per check: [OK], [FAIL], or [INFO]. INFO lines are
# advisory and do not count toward pass/fail totals.

set -u   # NOT -e: we want to run every check, even if one fails.

STACK_DIR="/opt/stacks/azerothcore"
PASS=0
FAIL=0
TOTAL=0   # OK + FAIL only; INFO is excluded

ok() {
    echo "[OK] $1"
    PASS=$((PASS + 1))
    TOTAL=$((TOTAL + 1))
}
fail() {
    echo "[FAIL] $1"
    FAIL=$((FAIL + 1))
    TOTAL=$((TOTAL + 1))
}
info() {
    echo "[INFO] $1"
}

if [ ! -f "${STACK_DIR}/.env" ]; then
    fail "Cannot find ${STACK_DIR}/.env — install not complete"
    echo ""
    echo "TOTAL: $TOTAL"
    echo "RESULT: FAIL ($FAIL failed)"
    exit 1
fi
# shellcheck disable=SC1091
source "${STACK_DIR}/.env"

# Expected Tailscale IP comes from .env, not from `tailscale ip -4 | head -1`.
# A host can hold more than one Tailscale IPv4 (multi-tailnet, re-auth, exit
# node), so the install-time value is the only authoritative source.
EXPECTED_TS_IP="${DOCKER_AUTH_EXTERNAL_PORT%%:*}"

# ============================================================================
# Helpers
# ============================================================================

# Run a SQL statement against ac-database as root. Returns -N -B output
# (no headers, tab-separated). Silently empty if the connection fails;
# Checks 1 and 3 should run first so any connection failure is already flagged.
mysql_exec() {
    docker exec ac-database mysql \
        -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" -N -B -e "$1" 2>/dev/null
}

# Read a single MySQL global variable via SHOW VARIABLES LIKE. Returns the
# value as MySQL reports it (e.g. "OFF"/"ON", "READ-COMMITTED", numeric
# strings for sizes).
mysql_variable() {
    mysql_exec "SHOW VARIABLES LIKE '$1';" | awk '{print $2}' | tail -1
}

# Assert that a MySQL global variable equals an expected literal value.
verify_mysql_static() {
    local var="$1" expected="$2"
    local actual
    actual="$(mysql_variable "$var")"
    if [ "$actual" = "$expected" ]; then
        ok "MySQL $var = $expected"
    else
        fail "MySQL $var = '${actual:-<unset>}' (expected $expected)"
    fi
}

# Print every local address that is currently listening on $1/tcp, one per
# line. Handles IPv4 (127.0.0.1:3306) and IPv6 ([::]:3306) ss output.
listening_addrs_for_port() {
    local port="$1"
    ss -ltn 2>/dev/null | tail -n +2 | awk '{print $4}' | awk -F: -v p="$port" '
        $NF == p {
            out = ""
            for (i = 1; i < NF; i++) out = (i == 1 ? $i : out ":" $i)
            print out
        }
    '
}

# Verify that every listener on $port is in the expected-address set.
# Fails if the port has no listener, or any listener falls outside the set.
verify_port_scope() {
    local label="$1" port="$2"; shift 2
    local expected=("$@")
    local actual=() bad=()

    mapfile -t actual < <(listening_addrs_for_port "$port")
    if [ "${#actual[@]}" -eq 0 ]; then
        fail "Port $port ($label) is not listening"
        return
    fi

    local a e found
    for a in "${actual[@]}"; do
        found=0
        for e in "${expected[@]}"; do
            [ "$a" = "$e" ] && { found=1; break; }
        done
        [ "$found" -eq 0 ] && bad+=("$a")
    done

    if [ "${#bad[@]}" -eq 0 ]; then
        ok "Port $port ($label) listening only on expected scope: ${actual[*]}"
    else
        fail "Port $port ($label) listening on unexpected scope: ${bad[*]} (expected one of: ${expected[*]})"
    fi
}

# Convenience: split an addr:port string from .env, then check exposure scope.
verify_port_scope_from_env() {
    local name="$1" var_value="$2"
    if [[ "$var_value" != *:* ]]; then
        fail "$name .env value is malformed (no addr:port): '$var_value'"
        return
    fi
    verify_port_scope "$name" "${var_value##*:}" "${var_value%:*}"
}

# Escape regex metachars in a literal conf key. Mirrors the helper in the
# installer (escape_regex_metachars) — single quotes are required so the
# `\&` back-reference reaches sed unmodified.
# shellcheck disable=SC2016
conf_escape_key() {
    printf '%s' "$1" | sed 's/[.[\*^$()+?{}|]/\\&/g'
}

# ============================================================================
# Check 1 — long-running containers are running
# ============================================================================
for c in ac-database ac-authserver ac-worldserver; do
    status="$(docker inspect --format='{{.State.Status}}' "$c" 2>/dev/null || echo missing)"
    if [ "$status" = "running" ]; then
        ok "Container $c is running"
    else
        fail "Container $c status: $status"
    fi
done

# ============================================================================
# Check 2 — short-lived init containers exited 0
# ============================================================================
for c in ac-client-data-init ac-db-import; do
    exit_code="$(docker inspect --format='{{.State.ExitCode}}' "$c" 2>/dev/null || echo missing)"
    state="$(docker inspect --format='{{.State.Status}}' "$c" 2>/dev/null || echo missing)"
    if [ "$exit_code" = "0" ] && [ "$state" = "exited" ]; then
        ok "Init container $c last exit code = 0"
    else
        fail "Init container $c: state=$state exit=$exit_code"
    fi
done

# ============================================================================
# Check 3 — required databases exist
# ============================================================================
DBS_OUT="$(mysql_exec "SHOW DATABASES;" || echo "")"
for db in acore_auth acore_characters acore_world acore_playerbots; do
    if echo "$DBS_OUT" | grep -qFx "$db"; then
        ok "Database $db exists"
    else
        fail "Database $db missing"
    fi
done

# ============================================================================
# Check 4 — acore_playerbots schema has every required table
# AiPlayerbot.Enabled=1 alone is not enough: if the mod-playerbots updater
# never ran (because AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES was 0), the
# database exists but core tables are missing and bots silently fail to
# persist. Mirror the install's eight-table required list.
# ============================================================================
if echo "$DBS_OUT" | grep -qFx acore_playerbots; then
    PB_REQUIRED=(
        playerbots_custom_strategy
        playerbots_db_store
        playerbots_random_bots
        playerbots_equip_cache
        playerbots_travelnode
        playerbots_travelnode_link
        playerbots_travelnode_path
        playerbots_item_info_cache
    )
    pb_union=""
    for t in "${PB_REQUIRED[@]}"; do
        [ -n "$pb_union" ] && pb_union+=" UNION ALL "
        pb_union+="SELECT '$t' AS t"
    done
    missing_tables="$(mysql_exec "
        SELECT required.t FROM ($pb_union) AS required
        LEFT JOIN information_schema.tables ti
          ON ti.table_schema='acore_playerbots' AND ti.table_name=required.t
        WHERE ti.table_name IS NULL;
    " || echo "")"
    if [ -z "$missing_tables" ]; then
        ok "acore_playerbots has all ${#PB_REQUIRED[@]} required Playerbots tables"
    else
        joined="$(echo "$missing_tables" | paste -sd, -)"
        fail "acore_playerbots is missing required table(s): $joined"
    fi
fi

# ============================================================================
# Check 5 — MySQL tuning matches install's authoritative custom.cnf
# Mirrors install/verify_mysql_tuning_active (lines 553-663) for full parity.
# ============================================================================
CUSTOM_CNF="${STACK_DIR}/configs/mysql/custom.cnf"

# 5a: the bind-mounted file must be a readable regular file inside the
# container. Docker creates a directory on the host if the source path is
# missing at first start; MySQL then ignores it (defaults silently apply).
if docker exec ac-database sh -c 'test -f /etc/mysql/conf.d/custom.cnf && test -r /etc/mysql/conf.d/custom.cnf' 2>/dev/null; then
    ok "ac-database sees /etc/mysql/conf.d/custom.cnf as a readable regular file"
else
    fail "ac-database cannot read /etc/mysql/conf.d/custom.cnf as a regular file (bind-mount may have created a directory)"
fi

# 5b: innodb_buffer_pool_size — must equal configured exactly. The installer
# always writes <N>G with <N> instances and default 128M chunks, so MySQL's
# chunk-rounding (multiple of chunk_size * instances = 128M * N) divides
# N * 1024M evenly and never bumps the actual size above the configured one.
# Strict equality matches install/verify_mysql_tuning_active and catches drift
# (e.g., custom.cnf says 1G but mysqld was started with --innodb-buffer-pool-size=32G).
configured_size="$(grep -E '^innodb_buffer_pool_size[[:space:]]+=' "$CUSTOM_CNF" 2>/dev/null \
                   | sed -E 's/.*=[[:space:]]+//;s/[[:space:]]+$//')"
configured_g=""
if [[ "$configured_size" =~ ^([0-9]+)G$ ]]; then
    configured_g="${BASH_REMATCH[1]}"
    configured_bytes=$(( configured_g * 1024 * 1024 * 1024 ))
    actual_size="$(mysql_variable innodb_buffer_pool_size)"
    if [ "$actual_size" = "$configured_bytes" ]; then
        ok "innodb_buffer_pool_size = $actual_size bytes ($configured_size)"
    else
        fail "innodb_buffer_pool_size = ${actual_size:-<unset>} (expected $configured_bytes / $configured_size)"
    fi
else
    fail "Cannot parse configured innodb_buffer_pool_size from custom.cnf: '$configured_size'"
fi

# 5c: innodb_buffer_pool_instances — install derives this as floor(pool_size_g)
# to keep each instance >= 1 GB (MySQL silently collapses to 1 instance below
# that threshold). Verify both the conf value and the live MySQL value.
configured_instances="$(grep -E '^innodb_buffer_pool_instances[[:space:]]+=' "$CUSTOM_CNF" 2>/dev/null \
                         | sed -E 's/.*=[[:space:]]+//;s/[[:space:]]+$//')"
actual_instances="$(mysql_variable innodb_buffer_pool_instances)"
if [ -n "$configured_g" ] && [ "$configured_instances" != "$configured_g" ]; then
    fail "custom.cnf innodb_buffer_pool_instances = '$configured_instances' (expected $configured_g, derived from pool size in GB)"
elif [ -n "$configured_instances" ] && [ "$actual_instances" = "$configured_instances" ]; then
    ok "innodb_buffer_pool_instances = $actual_instances"
else
    fail "innodb_buffer_pool_instances = ${actual_instances:-<unset>} (expected $configured_instances per custom.cnf)"
fi

# 5d-5k: the rest of the tuning surface — all install-time literals.
verify_mysql_static innodb_io_capacity            500
verify_mysql_static innodb_io_capacity_max        2500
verify_mysql_static innodb_use_fdatasync          ON
verify_mysql_static innodb_log_buffer_size        33554432
verify_mysql_static transaction_isolation         READ-COMMITTED
verify_mysql_static log_bin                       OFF
verify_mysql_static sync_binlog                   0
verify_mysql_static innodb_flush_log_at_trx_commit 2

# ============================================================================
# Check 6 — realmlist matches the IP captured in .env, AND Tailscale assigns
# that same IP locally right now. Splitting these gives a clearer signal than
# a single combined check: a stale realmlist row and a Tailscale outage look
# the same to the user otherwise.
# ============================================================================
if ! printf '%s' "$EXPECTED_TS_IP" | grep -Eq '^100\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$'; then
    fail "DOCKER_AUTH_EXTERNAL_PORT in .env does not start with a valid Tailscale CGNAT IPv4: '${DOCKER_AUTH_EXTERNAL_PORT}'"
else
    realm_addr="$(mysql_exec "SELECT address FROM acore_auth.realmlist WHERE id=1;" || echo "")"
    if [ "$realm_addr" = "$EXPECTED_TS_IP" ]; then
        ok "Realmlist address ($realm_addr) matches .env-configured Tailscale IP"
    else
        fail "Realmlist address '$realm_addr' != .env-configured Tailscale IP '$EXPECTED_TS_IP'"
    fi

    if ! command -v tailscale >/dev/null 2>&1; then
        fail "tailscale binary not installed; cannot confirm $EXPECTED_TS_IP is currently assigned"
    elif tailscale ip -4 2>/dev/null | grep -Fxq "$EXPECTED_TS_IP"; then
        ok "Tailscale is currently assigning $EXPECTED_TS_IP locally"
    else
        fail "Tailscale is not assigning $EXPECTED_TS_IP locally (run 'tailscale ip -4' to inspect)"
    fi
fi

# ============================================================================
# Check 7 — all four built images carry the configured tag
# ============================================================================
expected_tag="${DOCKER_IMAGE_TAG:-playerbot-local}"
for img in worldserver authserver db-import client-data; do
    full="acore/ac-wotlk-${img}:${expected_tag}"
    if docker images --format '{{.Repository}}:{{.Tag}}' | grep -qFx "$full"; then
        ok "Image $full present"
    else
        fail "Image $full missing"
    fi
done

# ============================================================================
# Check 8 — docker-compose.override.yml has every expected static AC_* line
# Mirrors the install's Phase 2.5 assertion block verbatim. The dual sourcing
# with install is intentional and documented in CLAUDE.md — a silent loss of
# any of these (manual edit, partial restore, override regenerated from an
# older template) changes server behavior without any other warning.
# ============================================================================
OVERRIDE="${STACK_DIR}/docker-compose.override.yml"
if [ ! -f "$OVERRIDE" ]; then
    fail "docker-compose.override.yml missing at $OVERRIDE"
else
    OVERRIDE_EXPECTED=(
        '      AC_AI_PLAYERBOT_ENABLED: "1"'
        '      AC_AI_PLAYERBOT_RANDOM_BOT_AUTOLOGIN: "1"'
        '      AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES: "1"'
        '      AC_MAP_UPDATE_INTERVAL: "10"'
        '      AC_MIN_WORLD_UPDATE_TIME: "1"'
        '      AC_PRELOAD_ALL_NON_INSTANCED_MAP_GRIDS: "0"'
        '      AC_DONT_CACHE_RANDOM_MOVEMENT_PATHS: "0"'
        '      AC_QUESTS_IGNORE_AUTO_ACCEPT: "1"'
        '      AC_PLAYER_LIMIT: "0"'
        '      AC_LEAVE_GROUP_ON_LOGOUT_ENABLED: "1"'
        '      AC_AUCTION_HOUSE_BOT_ENABLE_SELLER: "true"'
        '      AC_AUCTION_HOUSE_BOT_BUYER_ENABLED: "true"'
        '      AC_ALLOW_TWO_SIDE_INTERACTION_AUCTION: "1"'
        '      AC_ALLOW_TWO_SIDE_INTERACTION_CHAT: "1"'
        '      AC_ALLOW_TWO_SIDE_INTERACTION_CALENDAR: "1"'
        '      AC_ALLOW_TWO_SIDE_INTERACTION_CHANNEL: "1"'
        '      AC_ALLOW_TWO_SIDE_INTERACTION_GROUP: "1"'
        '      AC_ALLOW_TWO_SIDE_INTERACTION_GUILD: "1"'
        '      AC_ALLOW_TWO_SIDE_INTERACTION_ARENA: "1"'
        '      AC_UPDATES_ENABLE_DATABASES: "7"'
        '      AC_ENABLE_PLAYER_SETTINGS: "1"'
        '      AC_MAIL_DELIVERY_DELAY: "10"'
        '      AC_CHAR_DELETE_METHOD: "1"'
    )
    missing_override=0
    for expected in "${OVERRIDE_EXPECTED[@]}"; do
        if ! grep -qFx "$expected" "$OVERRIDE"; then
            fail "docker-compose.override.yml missing line: ${expected## }"
            missing_override=1
        fi
    done
    if [ "$missing_override" -eq 0 ]; then
        ok "docker-compose.override.yml has all ${#OVERRIDE_EXPECTED[@]} expected static AC_* lines"
    fi

    # Substituted lines — values come from install-time prompts, so check
    # only the shape and that MIN/MAX agree (install always writes them equal).
    if grep -qE '^      AC_AI_PLAYERBOT_MIN_RANDOM_BOTS: "[0-9]+"$' "$OVERRIDE"; then
        ok "docker-compose.override.yml AC_AI_PLAYERBOT_MIN_RANDOM_BOTS is numeric"
    else
        fail "docker-compose.override.yml AC_AI_PLAYERBOT_MIN_RANDOM_BOTS missing or non-numeric"
    fi
    if grep -qE '^      AC_AI_PLAYERBOT_MAX_RANDOM_BOTS: "[0-9]+"$' "$OVERRIDE"; then
        ok "docker-compose.override.yml AC_AI_PLAYERBOT_MAX_RANDOM_BOTS is numeric"
    else
        fail "docker-compose.override.yml AC_AI_PLAYERBOT_MAX_RANDOM_BOTS missing or non-numeric"
    fi
    pb_min="$(grep -E '^      AC_AI_PLAYERBOT_MIN_RANDOM_BOTS: "[0-9]+"$' "$OVERRIDE" | grep -oE '[0-9]+' | head -1)"
    pb_max="$(grep -E '^      AC_AI_PLAYERBOT_MAX_RANDOM_BOTS: "[0-9]+"$' "$OVERRIDE" | grep -oE '[0-9]+' | head -1)"
    if [ -n "$pb_min" ] && [ -n "$pb_max" ] && [ "$pb_min" = "$pb_max" ]; then
        ok "Playerbot MIN/MAX_RANDOM_BOTS agree ($pb_min)"
    elif [ -n "$pb_min" ] || [ -n "$pb_max" ]; then
        fail "Playerbot MIN_RANDOM_BOTS='$pb_min' != MAX_RANDOM_BOTS='$pb_max'"
    fi
    if grep -qE '^      AC_MAP_UPDATE_THREADS: "[0-9]+"$' "$OVERRIDE"; then
        ok "docker-compose.override.yml AC_MAP_UPDATE_THREADS is numeric"
    else
        fail "docker-compose.override.yml AC_MAP_UPDATE_THREADS missing or non-numeric"
    fi

    # AC_PLAYERBOTS_DATABASE_INFO carries the DB password (substituted at install),
    # so we check shape only: "ac-database;3306;root;<password>;acore_playerbots".
    # Per CLAUDE.md, password chars are restricted to a set that excludes ';', so
    # [^;]+ matches the password segment without spillover.
    if grep -qE '^      AC_PLAYERBOTS_DATABASE_INFO: "ac-database;3306;root;[^;]+;acore_playerbots"$' "$OVERRIDE"; then
        ok "docker-compose.override.yml AC_PLAYERBOTS_DATABASE_INFO has expected shape"
    else
        fail "docker-compose.override.yml AC_PLAYERBOTS_DATABASE_INFO missing or malformed (bots cannot connect without it)"
    fi
fi

# ============================================================================
# Check 9 — XP/skill-gain rate overrides are in a consistent state
# Install writes all 12 keys (non-x1) or none (x1); SERVER_XP_RATE is not
# persisted to .env (it lives only in the install-time config, which is
# shredded on success), so verify can't know which rate was chosen. We instead
# check the consistency invariant: all 12 present exactly once, OR all 12
# absent. A partial state (e.g., a manual sed removed half the block) is the
# regression worth catching.
# ============================================================================
if [ -f "$OVERRIDE" ]; then
    XP_KEYS=(
        AC_RATE_XP_QUEST
        AC_RATE_XP_KILL
        AC_RATE_XP_EXPLORE
        AC_RATE_DROP_MONEY
        AC_RATE_REPUTATION_GAIN
        AC_RATE_SKILL_DISCOVERY
        AC_RATE_DROP_ITEM_NORMAL
        AC_RATE_DROP_ITEM_UNCOMMON
        AC_SKILLGAIN_CRAFTING
        AC_SKILLGAIN_GATHERING
        AC_SKILLGAIN_WEAPON
        AC_SKILLGAIN_DEFENSE
    )
    xp_present=0
    xp_absent=0
    xp_dup=()
    for k in "${XP_KEYS[@]}"; do
        c="$(grep -cE "^[[:space:]]*${k}[[:space:]]*:" "$OVERRIDE" 2>/dev/null || true)"
        case "$c" in
            0) xp_absent=$((xp_absent + 1)) ;;
            1) xp_present=$((xp_present + 1)) ;;
            *) xp_dup+=("${k}:${c}") ;;
        esac
    done
    if [ "${#xp_dup[@]}" -gt 0 ]; then
        fail "XP rate overrides have duplicate keys in docker-compose.override.yml: ${xp_dup[*]}"
    elif [ "$xp_present" -eq 12 ]; then
        ok "All 12 XP/skill-gain rate overrides present exactly once (non-x1 mode)"
    elif [ "$xp_absent" -eq 12 ]; then
        ok "No XP/skill-gain rate overrides present (x1 mode)"
    else
        fail "XP rate overrides in partial state: ${xp_present} present, ${xp_absent} absent (install writes all-or-nothing)"
    fi
fi

# ============================================================================
# Check 10 — mod_ahbot.conf has canonical AH bot settings AND the GUIDs
# refer to real characters on the AHBOT account.
# ============================================================================
AHBOT_CONF="${STACK_DIR}/configs/modules/mod_ahbot.conf"
if [ ! -f "$AHBOT_CONF" ]; then
    fail "mod_ahbot.conf missing at $AHBOT_CONF"
else
    # Each managed key must appear exactly once (install canonicalizes via
    # set_conf_key + require_conf_key_once). A duplicate after install means
    # somebody hand-edited it back.
    for key in AuctionHouseBot.GUIDs AuctionHouseBot.EnableSeller AuctionHouseBot.Buyer.Enabled; do
        esc="$(conf_escape_key "$key")"
        count="$(grep -cE "^[[:space:]]*${esc}[[:space:]]*=" "$AHBOT_CONF" 2>/dev/null || true)"
        if [ "$count" != "1" ]; then
            fail "mod_ahbot.conf has ${key} appearing ${count} time(s) (expected exactly 1)"
        fi
    done

    guids_val="$(grep -E '^[[:space:]]*AuctionHouseBot\.GUIDs[[:space:]]*=' "$AHBOT_CONF" \
                 | sed -E 's/^[[:space:]]*[^=]+=[[:space:]]*//;s/[[:space:]]+$//')"
    if ! printf '%s' "$guids_val" | grep -Eq '^[0-9]+(,[0-9]+)*$'; then
        fail "mod_ahbot.conf AuctionHouseBot.GUIDs = '$guids_val' (expected comma-separated positive integers)"
    elif [ "$guids_val" = "0" ]; then
        fail "mod_ahbot.conf AuctionHouseBot.GUIDs = 0 (no AH bot character GUID assigned)"
    else
        ok "mod_ahbot.conf AuctionHouseBot.GUIDs = $guids_val"

        # Cross-check: every listed GUID must exist AND belong to AHBOT.
        # SQL IN accepts the bare comma-separated digits.
        guid_count="$(echo "$guids_val" | tr ',' '\n' | grep -c .)"
        matched="$(mysql_exec "
            SELECT COUNT(*) FROM acore_characters.characters c
            INNER JOIN acore_auth.account a ON c.account = a.id
            WHERE a.username='AHBOT' AND c.guid IN ($guids_val);
        " || echo "")"
        if [ "$matched" = "$guid_count" ]; then
            ok "All $guid_count AH bot GUID(s) map to characters on the AHBOT account"
        else
            fail "Only '${matched:-0}' of $guid_count AH bot GUID(s) belong to AHBOT account characters"
        fi
    fi

    seller="$(grep -E '^[[:space:]]*AuctionHouseBot\.EnableSeller[[:space:]]*=' "$AHBOT_CONF" \
              | sed -E 's/^[[:space:]]*[^=]+=[[:space:]]*//;s/[[:space:]]+$//')"
    if [ "$seller" = "true" ]; then
        ok "mod_ahbot.conf AuctionHouseBot.EnableSeller = true"
    else
        fail "mod_ahbot.conf AuctionHouseBot.EnableSeller = '$seller' (expected true)"
    fi

    buyer="$(grep -E '^[[:space:]]*AuctionHouseBot\.Buyer\.Enabled[[:space:]]*=' "$AHBOT_CONF" \
             | sed -E 's/^[[:space:]]*[^=]+=[[:space:]]*//;s/[[:space:]]+$//')"
    if [ "$buyer" = "true" ]; then
        ok "mod_ahbot.conf AuctionHouseBot.Buyer.Enabled = true"
    else
        fail "mod_ahbot.conf AuctionHouseBot.Buyer.Enabled = '$buyer' (expected true)"
    fi
fi

# ============================================================================
# Check 11 — playerbots conf present and master switch on
# (Schema health is covered in Check 4.)
# ============================================================================
PB_CONF=""
for candidate in playerbots.conf mod_playerbots.conf; do
    if [ -f "${STACK_DIR}/configs/modules/$candidate" ]; then
        PB_CONF="${STACK_DIR}/configs/modules/$candidate"
        break
    fi
done

if [ -z "$PB_CONF" ]; then
    fail "Neither playerbots.conf nor mod_playerbots.conf found"
else
    pb_enabled="$(grep -E '^[[:space:]]*AiPlayerbot\.Enabled[[:space:]]*=' "$PB_CONF" \
                  | head -1 | sed -E 's/^[[:space:]]*[^=]+=[[:space:]]*//;s/[[:space:]]+$//')"
    if [ "$pb_enabled" = "1" ]; then
        ok "Playerbots conf present; AiPlayerbot.Enabled = 1 ($PB_CONF)"
    else
        fail "Playerbots conf at $PB_CONF has AiPlayerbot.Enabled = '$pb_enabled' (expected 1)"
    fi
fi

# ============================================================================
# Check 12 — playerbots performance profile keys (16 mandatory)
# Install's ensure_playerbots_performance_config writes these via set_conf_key.
# Per CLAUDE.md the dual-source with compose env-vars is intentional: the .conf
# is the visible/editable face of the runtime tuning, so drift here is silent
# at runtime but breaks anyone reading the live config to understand the box.
# Each key gets its own OK/FAIL so the drifted key is pinpointable.
#
# MIN/MAX bot counts are user-driven, so we re-use pb_min/pb_max captured by
# Check 8 from the compose override. If those are unset (Check 8 failed),
# we still validate the .conf line is numeric.
# ============================================================================
if [ -n "$PB_CONF" ]; then
    pb_count="${pb_min:-${pb_max:-}}"
    PB_PROFILE_KEYS=(
        "AiPlayerbot.BotActiveAlone|0"
        "AiPlayerbot.botActiveAloneSmartScale|1"
        "AiPlayerbot.botActiveAloneSmartScaleWhenMinLevel|1"
        "AiPlayerbot.botActiveAloneSmartScaleWhenMaxLevel|80"
        "AiPlayerbot.DisabledWithoutRealPlayer|1"
        "AiPlayerbot.MinRandomBots|__PB_COUNT__"
        "AiPlayerbot.MaxRandomBots|__PB_COUNT__"
        "AiPlayerbot.EnablePeriodicOnlineOffline|1"
        "AiPlayerbot.PeriodicOnlineOfflineRatio|2.0"
        "AiPlayerbot.BotActiveAloneForceWhenInRadius|150"
        "AiPlayerbot.BotActiveAloneForceWhenInZone|1"
        "AiPlayerbot.BotActiveAloneForceWhenInMap|0"
        "AiPlayerbot.BotActiveAloneForceWhenIsFriend|1"
        "AiPlayerbot.BotActiveAloneForceWhenInGuild|0"
        "PlayerbotsDatabase.WorkerThreads|1"
        "PlayerbotsDatabase.SynchThreads|2"
    )
    for entry in "${PB_PROFILE_KEYS[@]}"; do
        pb_key="${entry%%|*}"
        pb_expected="${entry##*|}"
        pb_esc="$(conf_escape_key "$pb_key")"
        pb_kcount="$(grep -cE "^[[:space:]]*${pb_esc}[[:space:]]*=" "$PB_CONF" 2>/dev/null || true)"
        pb_actual="$(grep -E "^[[:space:]]*${pb_esc}[[:space:]]*=" "$PB_CONF" \
                     | head -1 | sed -E 's/^[[:space:]]*[^=]+=[[:space:]]*//;s/[[:space:]]+$//')"
        if [ "$pb_kcount" != "1" ]; then
            fail "playerbots conf: ${pb_key} appears ${pb_kcount} time(s) (expected exactly 1)"
            continue
        fi
        if [ "$pb_expected" = "__PB_COUNT__" ]; then
            if ! [[ "$pb_actual" =~ ^[0-9]+$ ]]; then
                fail "playerbots conf: ${pb_key} = '${pb_actual}' (expected positive integer)"
            elif [ -n "$pb_count" ] && [ "$pb_actual" != "$pb_count" ]; then
                fail "playerbots conf: ${pb_key} = ${pb_actual} (does not match compose override MIN/MAX value ${pb_count})"
            else
                ok "playerbots conf: ${pb_key} = ${pb_actual}"
            fi
        elif [ "$pb_actual" = "$pb_expected" ]; then
            ok "playerbots conf: ${pb_key} = ${pb_expected}"
        else
            fail "playerbots conf: ${pb_key} = '${pb_actual}' (expected '${pb_expected}')"
        fi
    done
fi

# ============================================================================
# Check 13 — mod-individual-progression wired up (files + live master switch)
# Compose env-var presence (AC_UPDATES_ENABLE_DATABASES=7,
# AC_ENABLE_PLAYER_SETTINGS=1) is asserted in Check 8.
# ============================================================================
IP_DIR="${STACK_DIR}/modules/mod-individual-progression"
IP_CONF="${STACK_DIR}/configs/modules/individualProgression.conf"
if [ -d "$IP_DIR" ]; then
    ok "mod-individual-progression module directory present"
else
    fail "mod-individual-progression module directory missing at $IP_DIR"
fi
if [ ! -f "$IP_CONF" ]; then
    fail "individualProgression.conf missing at $IP_CONF"
else
    ok "individualProgression.conf present at $IP_CONF"
    ip_enabled="$(grep -E '^[[:space:]]*IndividualProgression\.Enable[[:space:]]*=' "$IP_CONF" \
                  | head -1 | sed -E 's/^[[:space:]]*[^=]+=[[:space:]]*//;s/[[:space:]]+$//')"
    if [ "$ip_enabled" = "1" ]; then
        ok "individualProgression.conf IndividualProgression.Enable = 1"
    else
        fail "individualProgression.conf IndividualProgression.Enable = '$ip_enabled' (expected 1)"
    fi
fi

# ============================================================================
# Check 14 — AHBOT account exists in acore_auth
# Required for AH bot characters (and Check 9's GUID cross-check) to mean
# anything. AzerothCore stores usernames uppercased via SRP6.
# ============================================================================
ahbot_account="$(mysql_exec "SELECT username FROM acore_auth.account WHERE username='AHBOT';" || echo "")"
if [ "$ahbot_account" = "AHBOT" ]; then
    ok "acore_auth.account contains AHBOT"
else
    fail "acore_auth.account is missing the AHBOT user (AH bot will not function)"
fi

# ============================================================================
# Check 15 — backup.sh exists and is executable
# ============================================================================
BACKUP_SCRIPT="${STACK_DIR}/backup.sh"
if [ -x "$BACKUP_SCRIPT" ]; then
    ok "backup.sh exists and is executable"
else
    fail "backup.sh missing or not executable at $BACKUP_SCRIPT"
fi

# ============================================================================
# Check 16 — backups directory present, with at least one fresh file
# Three deterministic paths; no silent skip if backup.sh is missing.
# ============================================================================
BACKUPS_DIR="${STACK_DIR}/backups"
if [ ! -d "$BACKUPS_DIR" ]; then
    fail "Backups directory missing at $BACKUPS_DIR"
elif [ -z "$(ls -A "$BACKUPS_DIR" 2>/dev/null)" ]; then
    info "Backups directory is empty (no backup has run yet)"
elif [ ! -x "$BACKUP_SCRIPT" ]; then
    info "Cannot check backup freshness: backup.sh missing/not executable (see Check 15)"
else
    newer_count="$(find "$BACKUPS_DIR" -type f -newer "$BACKUP_SCRIPT" 2>/dev/null | wc -l)"
    if [ "$newer_count" -gt 0 ]; then
        ok "Backups directory has $newer_count file(s) newer than backup.sh"
    else
        fail "Backups directory has files but none newer than backup.sh (backup hasn't run since script was last written)"
    fi
fi

# ============================================================================
# Check 17 — crontab has the backup entry (uncommented)
# ============================================================================
if crontab -l 2>/dev/null | grep -vE '^\s*#' | grep -qF "${STACK_DIR}/backup.sh"; then
    ok "Crontab contains active backup entry"
else
    fail "Crontab is missing an active backup entry (commented entries don't count)"
fi

# ============================================================================
# Check 18 — systemd unit (only if user opted in and the file exists)
# ============================================================================
if [ -f /etc/systemd/system/azerothcore.service ]; then
    if systemctl is-enabled azerothcore.service >/dev/null 2>&1; then
        ok "Systemd unit azerothcore.service is enabled"
    else
        fail "Systemd unit exists but is not enabled"
    fi
fi

# ============================================================================
# Check 19 — docker compose config parses cleanly
# Catches drift in docker-compose.override.yml (manual edit, partial sed)
# that the running stack won't notice until the next `compose up`.
# ============================================================================
if (cd "$STACK_DIR" && docker compose config -q >/dev/null 2>&1); then
    ok "docker compose config parses cleanly"
else
    fail "docker compose config has errors — the running stack still works, but next 'compose up' will fail"
fi

# ============================================================================
# Check 20 — phpmyadmin / ac-eluna-ts-dev are not running
# Install Phase 4 brings the stack up with `--scale ${svc}=0` for each of
# these that exists in the effective compose config, and Phase 8 bakes the
# same flags into the systemd unit's ExecStart. A user who runs `docker
# compose up -d` by hand without the flags would silently get phpmyadmin
# bound to its default port. We check current state (no running container)
# rather than scale=0 specifically, since either stopped or scaled-to-0
# is acceptable.
# ============================================================================
EFFECTIVE_SERVICES="$(cd "$STACK_DIR" && docker compose config --services 2>/dev/null || echo "")"
for svc in phpmyadmin ac-eluna-ts-dev; do
    if echo "$EFFECTIVE_SERVICES" | grep -qx "$svc"; then
        svc_running="$(cd "$STACK_DIR" && docker compose ps -q "$svc" 2>/dev/null | wc -l)"
        if [ "$svc_running" -eq 0 ]; then
            ok "$svc is not running (scaled to 0 or stopped)"
        else
            fail "$svc has ${svc_running} running container(s); install scales it to 0"
        fi
    fi
done

# ============================================================================
# Check 21 — port-binding scope matches .env intent
# Catches the failure mode where Docker silently exposes 3306/SOAP on
# 0.0.0.0 because .env values were not honored on the last recreate.
# ============================================================================
verify_port_scope_from_env "MySQL"       "$DOCKER_DB_EXTERNAL_PORT"
verify_port_scope_from_env "SOAP"        "$DOCKER_SOAP_EXTERNAL_PORT"
verify_port_scope_from_env "authserver"  "$DOCKER_AUTH_EXTERNAL_PORT"
verify_port_scope_from_env "worldserver" "$DOCKER_WORLD_EXTERNAL_PORT"

# ============================================================================
# Check 22 — auctions count (informational, with stale-empty warning)
# Pure-INFO checks can hide a fully-broken AH bot: a bad GUID or a bot
# character that never connected will silently leave auctions at 0 forever.
# Don't promote to FAIL (a fresh server has 0 for a while), but if the
# worldserver has been up > 1h with zero auctions, warn loudly.
# ============================================================================
auc_count="$(mysql_exec "SELECT COUNT(*) FROM acore_characters.auctionhouse;" || echo "(unknown)")"

ws_started="$(docker inspect --format='{{.State.StartedAt}}' ac-worldserver 2>/dev/null || echo "")"
ws_age_secs=""
if [ -n "$ws_started" ]; then
    ws_started_epoch="$(date -d "$ws_started" +%s 2>/dev/null || echo "")"
    if [ -n "$ws_started_epoch" ]; then
        ws_age_secs=$(( $(date +%s) - ws_started_epoch ))
    fi
fi

if [[ "$auc_count" =~ ^[0-9]+$ ]] && [ "$auc_count" = "0" ] \
   && [ -n "$ws_age_secs" ] && [ "$ws_age_secs" -gt 3600 ]; then
    info "auctions_count = 0 (worldserver up ${ws_age_secs}s, > 1h)."
    info "  This is unusual — AH bot may not be working. Check:"
    info "    docker logs ac-worldserver 2>&1 | grep -i ahbot | tail -20"
    info "  Verify GUIDs in mod_ahbot.conf match real characters on the AHBOT account."
elif [ -n "$ws_age_secs" ]; then
    info "auctions_count = $auc_count (worldserver uptime ${ws_age_secs}s)"
else
    info "auctions_count = $auc_count"
fi

echo ""
echo "TOTAL: $TOTAL checks"
if [ "$FAIL" -eq 0 ]; then
    echo "RESULT: PASS ($PASS passed)"
    exit 0
else
    echo "RESULT: FAIL ($PASS passed, $FAIL failed)"
    exit 1
fi
