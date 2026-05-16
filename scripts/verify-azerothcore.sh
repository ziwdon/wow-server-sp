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

# ----- Check 1: long-running containers running -----
for c in ac-database ac-authserver ac-worldserver; do
    status="$(docker inspect --format='{{.State.Status}}' "$c" 2>/dev/null || echo missing)"
    if [ "$status" = "running" ]; then
        ok "Container $c is running"
    else
        fail "Container $c status: $status"
    fi
done

# ----- Check 2: short-lived init containers exited 0 -----
for c in ac-client-data-init ac-db-import; do
    exit_code="$(docker inspect --format='{{.State.ExitCode}}' "$c" 2>/dev/null || echo missing)"
    state="$(docker inspect --format='{{.State.Status}}' "$c" 2>/dev/null || echo missing)"
    if [ "$exit_code" = "0" ] && [ "$state" = "exited" ]; then
        ok "Init container $c last exit code = 0"
    else
        fail "Init container $c: state=$state exit=$exit_code"
    fi
done

# ----- Check 3: databases exist -----
DBS_OUT="$(docker exec ac-database mysql -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" \
           -N -B -e "SHOW DATABASES;" 2>/dev/null || echo "")"
for db in acore_auth acore_characters acore_world acore_playerbots; do
    if echo "$DBS_OUT" | grep -qFx "$db"; then
        ok "Database $db exists"
    else
        fail "Database $db missing"
    fi
done

# ----- Check 4: innodb_buffer_pool_size >= configured -----
configured="$(grep -E '^innodb_buffer_pool_size[[:space:]]+=' "${STACK_DIR}/configs/mysql/custom.cnf" 2>/dev/null \
              | sed -E 's/.*=[[:space:]]+//;s/[[:space:]]+$//')"
if [[ "$configured" =~ ^([0-9]+)G$ ]]; then
    configured_bytes=$(( ${BASH_REMATCH[1]} * 1024 * 1024 * 1024 ))
    actual="$(docker exec ac-database mysql -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" \
              -N -B -e "SELECT @@global.innodb_buffer_pool_size;" 2>/dev/null || echo 0)"
    if [[ "$actual" =~ ^[0-9]+$ ]] && [ "$actual" -ge "$configured_bytes" ]; then
        # MySQL 8.4 may round up to chunk_size × instances boundary; >= is correct.
        ok "innodb_buffer_pool_size = $actual bytes (>= configured $configured_bytes / $configured)"
    else
        fail "innodb_buffer_pool_size = $actual bytes (< configured $configured_bytes / $configured)"
    fi
else
    fail "Cannot parse configured innodb_buffer_pool_size from custom.cnf: '$configured'"
fi

# ----- Check 5: transaction_isolation = READ-COMMITTED -----
iso="$(docker exec ac-database mysql -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" \
       -N -B -e "SELECT @@global.transaction_isolation;" 2>/dev/null || echo "")"
if [ "$iso" = "READ-COMMITTED" ]; then
    ok "MySQL transaction_isolation = READ-COMMITTED"
else
    fail "MySQL transaction_isolation = '$iso' (expected READ-COMMITTED)"
fi

# ----- Check 6: log_bin = OFF -----
log_bin="$(docker exec ac-database mysql -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" \
           -N -B -e "SHOW VARIABLES LIKE 'log_bin';" 2>/dev/null \
           | awk '{print $2}')"
if [ "$log_bin" = "OFF" ]; then
    ok "MySQL log_bin = OFF"
else
    fail "MySQL log_bin = '$log_bin' (expected OFF)"
fi

# ----- Check 7: realmlist address matches Tailscale IPv4 -----
ts_ip="$(tailscale ip -4 2>/dev/null | grep -E '^100\.' | head -1 || true)"
realm_addr="$(docker exec ac-database mysql -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" \
              -N -B -e "SELECT address FROM acore_auth.realmlist WHERE id=1;" 2>/dev/null || echo "")"
if [ -n "$ts_ip" ] && [ "$realm_addr" = "$ts_ip" ]; then
    ok "Realmlist address ($realm_addr) matches first Tailscale IPv4 ($ts_ip)"
else
    fail "Realmlist address '$realm_addr' != Tailscale IPv4 '$ts_ip'"
fi

# ----- Check 8: all four built images have the configured tag -----
expected_tag="${DOCKER_IMAGE_TAG:-playerbot-local}"
for img in worldserver authserver db-import client-data; do
    full="acore/ac-wotlk-${img}:${expected_tag}"
    if docker images --format '{{.Repository}}:{{.Tag}}' | grep -qFx "$full"; then
        ok "Image $full present"
    else
        fail "Image $full missing"
    fi
done

# ----- Check 9: mod_ahbot.conf has correct edits -----
AHBOT_CONF="${STACK_DIR}/configs/modules/mod_ahbot.conf"
if [ -f "$AHBOT_CONF" ]; then
    guids_val="$(grep -E '^AuctionHouseBot\.GUIDs[[:space:]]*=' "$AHBOT_CONF" \
                 | head -1 | sed -E 's/^[^=]+=[[:space:]]*//;s/[[:space:]]+$//')"
    if [ -n "$guids_val" ] && [ "$guids_val" != "0" ]; then
        ok "mod_ahbot.conf AuctionHouseBot.GUIDs = $guids_val"
    else
        fail "mod_ahbot.conf AuctionHouseBot.GUIDs is empty or zero ('$guids_val')"
    fi

    seller="$(grep -E '^AuctionHouseBot\.EnableSeller[[:space:]]*=' "$AHBOT_CONF" \
              | sed -E 's/^[^=]+=[[:space:]]*//;s/[[:space:]]+$//')"
    if [ "$seller" = "true" ]; then
        ok "mod_ahbot.conf AuctionHouseBot.EnableSeller = true"
    else
        fail "mod_ahbot.conf AuctionHouseBot.EnableSeller = '$seller' (expected true)"
    fi

    buyer="$(grep -E '^AuctionHouseBot\.Buyer\.Enabled[[:space:]]*=' "$AHBOT_CONF" \
             | sed -E 's/^[^=]+=[[:space:]]*//;s/[[:space:]]+$//')"
    if [ "$buyer" = "true" ]; then
        ok "mod_ahbot.conf AuctionHouseBot.Buyer.Enabled = true"
    else
        fail "mod_ahbot.conf AuctionHouseBot.Buyer.Enabled = '$buyer' (expected true)"
    fi
else
    fail "mod_ahbot.conf missing at $AHBOT_CONF"
fi

# ----- Check 10: playerbots conf exists AND AiPlayerbot.Enabled = 1 -----
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
    # A bare file existence check is too weak — verify the module is actually
    # enabled. AiPlayerbot.Enabled is the master switch; if it's 0 or commented
    # out, no bots will spawn even if the rest of the stack is healthy.
    pb_enabled="$(grep -E '^AiPlayerbot\.Enabled[[:space:]]*=' "$PB_CONF" \
                  | head -1 | sed -E 's/^[^=]+=[[:space:]]*//;s/[[:space:]]+$//')"
    if [ "$pb_enabled" = "1" ]; then
        ok "Playerbots conf present; AiPlayerbot.Enabled = 1 ($PB_CONF)"
    else
        fail "Playerbots conf at $PB_CONF has AiPlayerbot.Enabled = '$pb_enabled' (expected 1)"
    fi
fi

# ----- Check 11: backup.sh exists and is executable -----
BACKUP_SCRIPT="${STACK_DIR}/backup.sh"
if [ -x "$BACKUP_SCRIPT" ]; then
    ok "backup.sh exists and is executable"
else
    fail "backup.sh missing or not executable at $BACKUP_SCRIPT"
fi

# ----- Check 12: a backup exists newer than backup.sh (INFO if backups/ empty) -----
if [ -d "${STACK_DIR}/backups" ] && [ -x "$BACKUP_SCRIPT" ]; then
    if [ -z "$(ls -A "${STACK_DIR}/backups" 2>/dev/null)" ]; then
        info "Backups directory is empty (no backup has run yet)"
    else
        newer_count=$(find "${STACK_DIR}/backups" -type f -newer "$BACKUP_SCRIPT" 2>/dev/null | wc -l)
        if [ "$newer_count" -gt 0 ]; then
            ok "Backups directory has $newer_count file(s) newer than backup.sh"
        else
            fail "Backups directory has files but none newer than backup.sh"
        fi
    fi
elif [ ! -d "${STACK_DIR}/backups" ]; then
    fail "Backups directory missing at ${STACK_DIR}/backups"
fi

# ----- Check 13: crontab has the backup entry (uncommented) -----
# `grep -qF` alone would match a commented-out cron line, so filter those first.
if crontab -l 2>/dev/null | grep -vE '^\s*#' | grep -qF "${STACK_DIR}/backup.sh"; then
    ok "Crontab contains active backup entry"
else
    fail "Crontab is missing an active backup entry (commented entries don't count)"
fi

# ----- Check 14: systemd unit, only if file exists -----
if [ -f /etc/systemd/system/azerothcore.service ]; then
    if systemctl is-enabled azerothcore.service >/dev/null 2>&1; then
        ok "Systemd unit azerothcore.service is enabled"
    else
        fail "Systemd unit exists but is not enabled"
    fi
fi
# (No else branch: if the unit file doesn't exist, the user opted out and the check is skipped entirely.)

# ----- Check 15: auctions count (informational, with stale-empty warning) -----
# Pure-INFO checks can hide a fully-broken AH bot: a bad GUID or a bot character
# that never connected will silently leave auctions at 0 forever. We don't promote
# this to FAIL (a fresh server legitimately has 0 for a while), but if the
# worldserver has been up for more than an hour with zero auctions, warn loudly.
auc_count="$(docker exec ac-database mysql -uroot -p"${DOCKER_DB_ROOT_PASSWORD}" \
             -N -B -e "SELECT COUNT(*) FROM acore_characters.auctionhouse;" 2>/dev/null || echo "(unknown)")"

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
