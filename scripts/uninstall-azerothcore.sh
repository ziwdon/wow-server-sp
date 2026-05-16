#!/usr/bin/env bash
# uninstall-azerothcore.sh
# Remove the local AzerothCore Docker stack created by install-azerothcore.sh.
#
# Scope:
#   - Stops/disables/removes the optional azerothcore.service systemd unit
#   - Runs project-scoped docker compose down for /opt/stacks/azerothcore when possible
#   - Removes known AzerothCore containers if compose metadata/files are missing
#   - Removes the matching backup cron entry from the current user's crontab
#   - Removes installer state/config files from the current user's home directory
#   - Removes the stack directory and known temporary installer files
#
# It intentionally does NOT uninstall Docker, Tailscale, UFW, cron, git, curl,
# packages, or system-wide user/group changes.

set -euo pipefail

STACK_DIR="/opt/stacks/azerothcore"
STATE_FILE="${HOME}/.azerothcore-install-state"
CONFIG_FILE="${HOME}/.azerothcore-install-config"
SYSTEMD_UNIT="/etc/systemd/system/azerothcore.service"
CRON_BACKUP_PATH="/opt/stacks/azerothcore/backup.sh"
KNOWN_CONTAINERS=(
  ac-database
  ac-authserver
  ac-worldserver
  ac-db-import
  ac-client-data-init
  ac-conf-extract
)
KNOWN_NETWORKS=(
  azerothcore_default
)

YES=false
DRY_RUN=false

usage() {
  cat <<USAGE
Usage: ./uninstall-azerothcore.sh [OPTIONS]

Options:
  --yes       Do not prompt for confirmation
  --dry-run   Show what would be removed, but do not remove anything
  -h, --help  Show this help

Run as your normal user, not with sudo. The script calls sudo only where needed.
USAGE
}

for arg in "$@"; do
  case "$arg" in
    --yes) YES=true ;;
    --dry-run) DRY_RUN=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

if [ "${EUID}" -eq 0 ]; then
  echo "ERROR: Do not run this script with sudo/root."
  echo "Run it as the same normal user that ran the installer, for example:"
  echo "  ./uninstall-azerothcore.sh"
  echo ""
  echo "Reason: root changes HOME/crontab/state cleanup targets to /root."
  exit 2
fi

run() {
  if [ "$DRY_RUN" = true ]; then
    printf '[dry-run] '
    printf '%q ' "$@"
    printf '\n'
  else
    "$@"
  fi
}

run_bash() {
  local script="$1"
  if [ "$DRY_RUN" = true ]; then
    echo "[dry-run] bash -c $script"
  else
    bash -c "$script"
  fi
}

safe_remove_literal() {
  local path="$1"
  case "$path" in
    /opt/stacks/azerothcore|"${HOME}/.azerothcore-install-state"|"${HOME}/.azerothcore-install-config"|/tmp/ac-build.log)
      run rm -rf -- "$path"
      ;;
    *)
      echo "Refusing to remove unexpected path: $path" >&2
      exit 1
      ;;
  esac
}

safe_remove_glob() {
  local pattern="$1"
  case "$pattern" in
    /tmp/azerothcore-install-\*.log|/tmp/ac-compose-effective.\*.yml)
      ;;
    *)
      echo "Refusing to remove unexpected glob: $pattern" >&2
      exit 1
      ;;
  esac

  local matches=()
  # Intentionally allow expansion of the validated glob pattern.
  # shellcheck disable=SC2206
  matches=( $pattern )
  if [ "${#matches[@]}" -eq 0 ] || [ ! -e "${matches[0]}" ]; then
    echo "No files matched: $pattern"
    return 0
  fi
  run rm -f -- "${matches[@]}"
}

echo "════════════════════════════════════════════════════════════════"
echo "AzerothCore stack uninstaller"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "This will remove only this AzerothCore stack's local artifacts:"
echo "  - $STACK_DIR"
echo "  - $STATE_FILE"
echo "  - $CONFIG_FILE"
echo "  - backup cron lines containing: $CRON_BACKUP_PATH"
echo "  - optional systemd unit: $SYSTEMD_UNIT"
echo "  - known containers: ${KNOWN_CONTAINERS[*]}"
echo ""
echo "It will NOT uninstall Docker, Tailscale, UFW, cron, git, curl, or packages."
echo ""

if [ "$YES" != true ] && [ "$DRY_RUN" != true ]; then
  read -r -p "Type 'REMOVE' to continue: " confirm
  if [ "$confirm" != "REMOVE" ]; then
    echo "Aborted."
    exit 1
  fi
fi

# Prime sudo only if needed. Do this after confirmation.
if [ "$DRY_RUN" != true ]; then
  echo ""
  echo "Priming sudo for systemd/stack-directory cleanup..."
  sudo -v
fi

# 1) Stop/disable/remove optional systemd unit first, so it cannot restart the stack.
echo ""
echo "[1/6] Removing optional systemd unit if present..."
if [ -f "$SYSTEMD_UNIT" ]; then
  run sudo systemctl disable --now azerothcore.service || true
  run sudo rm -f "$SYSTEMD_UNIT"
  run sudo systemctl daemon-reload
  run sudo systemctl reset-failed azerothcore.service || true
else
  echo "No azerothcore.service unit found."
fi

# 2) Bring down compose stack when possible.
echo ""
echo "[2/6] Bringing down Docker compose stack if possible..."
if [ -d "$STACK_DIR" ] && { [ -f "$STACK_DIR/docker-compose.yml" ] || [ -f "$STACK_DIR/compose.yml" ]; }; then
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    if [ "$DRY_RUN" = true ]; then
      echo "[dry-run] cd '$STACK_DIR' && docker compose -p azerothcore down"
    else
      (cd "$STACK_DIR" && docker compose -p azerothcore down) || true
    fi
  else
    echo "Docker compose not available; skipping compose down."
  fi
else
  echo "No compose file found under $STACK_DIR."
fi

# 3) Fallback cleanup for known named containers and default project network.
echo ""
echo "[3/6] Removing known containers/networks if they still exist..."
if command -v docker >/dev/null 2>&1; then
  for c in "${KNOWN_CONTAINERS[@]}"; do
    if docker inspect "$c" >/dev/null 2>&1; then
      run docker rm -f "$c" || true
    fi
  done
  for n in "${KNOWN_NETWORKS[@]}"; do
    if docker network inspect "$n" >/dev/null 2>&1; then
      run docker network rm "$n" || true
    fi
  done
else
  echo "Docker command not found; skipping Docker fallback cleanup."
fi

# 4) Remove backup cron lines for current user.
echo ""
echo "[4/6] Removing matching backup cron entries from current user's crontab..."
if crontab -l >/tmp/azerothcore-cron-before.$$ 2>/dev/null; then
  if grep -Fq "$CRON_BACKUP_PATH" /tmp/azerothcore-cron-before.$$; then
    grep -Fv "$CRON_BACKUP_PATH" /tmp/azerothcore-cron-before.$$ > /tmp/azerothcore-cron-after.$$ || true
    if [ "$DRY_RUN" = true ]; then
      echo "[dry-run] would remove these crontab line(s):"
      grep -F "$CRON_BACKUP_PATH" /tmp/azerothcore-cron-before.$$ || true
    else
      if [ -s /tmp/azerothcore-cron-after.$$ ]; then
        crontab /tmp/azerothcore-cron-after.$$
      else
        crontab -r
      fi
      echo "Removed matching cron entry/entries."
    fi
  else
    echo "No matching backup cron entry found."
  fi
  rm -f /tmp/azerothcore-cron-before.$$ /tmp/azerothcore-cron-after.$$
else
  echo "No crontab found for current user."
fi

# 5) Remove stack directory and installer state files.
echo ""
echo "[5/6] Removing stack directory and installer state files..."
if [ -d "$STACK_DIR" ]; then
  run sudo rm -rf "$STACK_DIR"
else
  echo "Stack directory already absent: $STACK_DIR"
fi
safe_remove_literal "$STATE_FILE"
safe_remove_literal "$CONFIG_FILE"

# 6) Remove known temporary files created by installer validation/build logging.
echo ""
echo "[6/6] Removing known temporary installer files..."
safe_remove_glob "/tmp/azerothcore-install-*.log"
safe_remove_glob "/tmp/ac-compose-effective.*.yml"
safe_remove_literal /tmp/ac-build.log

cat <<DONE

Done.

Remaining things intentionally left installed/configured:
  - Docker and Docker images/cache
  - Tailscale and Tailscale login/auth state
  - UFW and any non-AzerothCore firewall configuration
  - apt packages installed by the installer
  - docker group membership for your user

To reinstall from scratch, run the installer again as your normal user.
DONE
