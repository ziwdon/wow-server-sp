#!/usr/bin/env bash
# uninstall-azerothcore.sh
# Remove the local AzerothCore Docker stack created by install-azerothcore.sh.
#
# Scope:
#   - Stops/disables/removes the optional azerothcore.service systemd unit
#   - Runs project-scoped docker compose down -v for /opt/stacks/azerothcore when possible
#   - Removes known AzerothCore containers, networks, and named volumes
#   - Removes locally-built AzerothCore Docker images (acore/ac-wotlk-*:playerbot-local)
#   - Removes the matching backup cron entry from the current user's crontab
#   - Removes installer state/config files from the current user's home directory
#   - Removes the stack directory and known temporary installer files
#
# It intentionally does NOT uninstall Docker, Tailscale, UFW, cron, git, curl,
# packages, system-wide user/group changes, the Docker apt repo, or upstream
# Docker images pulled (but not built) by the installer.

set -euo pipefail

readonly STACK_DIR="/opt/stacks/azerothcore"
readonly STATE_FILE="${HOME}/.azerothcore-install-state"
readonly CONFIG_FILE="${HOME}/.azerothcore-install-config"
readonly SYSTEMD_UNIT="/etc/systemd/system/azerothcore.service"
COMPOSE_PROJECT="azerothcore"
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
# Locally-built images. Install script tags them with DOCKER_IMAGE_TAG=playerbot-local
# (set in /opt/stacks/azerothcore/.env). If the user overrode the tag we honor it
# by sourcing .env before this list is materialized at runtime.
LOCAL_IMAGE_REPOS=(
  acore/ac-wotlk-worldserver
  acore/ac-wotlk-authserver
  acore/ac-wotlk-db-import
  acore/ac-wotlk-client-data
)

YES=false
DRY_RUN=false
CLEANUP_FAILED=false

record_cleanup_failure() {
  CLEANUP_FAILED=true
  echo "ERROR: $1" >&2
}

has_recovery_context() {
  [ -e "$STACK_DIR" ] || [ -e "$STATE_FILE" ] || [ -e "$CONFIG_FILE" ]
}

preserve_recovery_context_and_exit() {
  echo "Uninstall incomplete. Recovery context was preserved at $STACK_DIR and $STATE_FILE." >&2
  echo "The systemd unit file was preserved; re-enable it after Docker recovery if service management is needed." >&2
  echo "Fix Docker availability/resource errors, then re-run ./scripts/uninstall-azerothcore.sh --yes." >&2
  exit 1
}

require_docker_daemon_for_recovery_context() {
  if [ "$DRY_RUN" = true ] || ! has_recovery_context; then
    return 0
  fi

  if ! command -v docker >/dev/null 2>&1; then
    record_cleanup_failure "Docker command is unavailable; preserving stack and installer state."
  elif ! docker info >/dev/null 2>&1; then
    record_cleanup_failure "Docker daemon is unavailable; preserving stack and installer state. Start or repair Docker, then re-run the uninstaller."
  fi

  if [ "$CLEANUP_FAILED" = true ]; then
    preserve_recovery_context_and_exit
  fi
}

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
  local use_sudo="${2:-no}"
  case "$path" in
    "$STACK_DIR"|"$STATE_FILE"|"$CONFIG_FILE"|/tmp/ac-build.log) ;;
    *) echo "Refusing to remove unexpected path: $path" >&2; exit 1 ;;
  esac
  if [ "$use_sudo" = sudo ]; then
    run sudo rm -rf -- "$path"
  else
    run rm -rf -- "$path"
  fi
}

safe_remove_glob() {
  local pattern="$1"
  local use_sudo="${2:-no}"
  case "$pattern" in
    /tmp/azerothcore-install-\*.log \
    |/tmp/ac-compose-effective.\*.yml \
    |/tmp/ac-xp-rate-overrides.\* \
    |/tmp/ac-playerbots-schema-check.out \
    |/opt/stacks/.azerothcore-clone-\*)
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
  if [ "$use_sudo" = "sudo" ]; then
    run sudo rm -rf -- "${matches[@]}"
  else
    run rm -rf -- "${matches[@]}"
  fi
}

echo "════════════════════════════════════════════════════════════════"
echo "AzerothCore stack uninstaller"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "This will remove only this AzerothCore stack's local artifacts:"
echo "  - $STACK_DIR (includes data/mysql, configs, logs, backups, modules)"
echo "  - $STATE_FILE"
echo "  - $CONFIG_FILE"
echo "  - backup cron lines containing: $CRON_BACKUP_PATH"
echo "  - optional systemd unit: $SYSTEMD_UNIT"
echo "  - known containers: ${KNOWN_CONTAINERS[*]}"
echo "  - docker volumes/networks labelled com.docker.compose.project=${COMPOSE_PROJECT}"
echo "  - locally-built images: ${LOCAL_IMAGE_REPOS[*]/%/:<tag>}"
echo "  - stale installer temp files in /tmp and /opt/stacks/.azerothcore-clone-*"
echo ""
echo "It will NOT uninstall Docker, Tailscale, UFW, cron, git, curl, packages,"
echo "the Docker apt repo/keyring, docker group membership, or upstream Docker"
echo "images pulled (but not built) by the installer."
echo ""

if [ "$YES" != true ] && [ "$DRY_RUN" != true ]; then
  read -r -p "Type 'REMOVE' to continue: " confirm
  if [ "$confirm" != "REMOVE" ]; then
    echo "Aborted."
    exit 1
  fi
fi

# A recovery context must never be altered until Docker can distinguish an
# unavailable daemon from an empty project. This also protects the systemd unit.
require_docker_daemon_for_recovery_context

# Prime sudo only if needed. Do this after confirmation.
if [ "$DRY_RUN" != true ]; then
  echo ""
  echo "Priming sudo for systemd/stack-directory cleanup..."
  sudo -v
fi

# 1) Stop/disable optional systemd unit first, so it cannot restart the stack
#    mid-teardown. The unit FILE itself is intentionally left in place until
#    Docker/Compose cleanup below has succeeded, so a failed teardown can
#    still be retried via systemctl without losing the recovery context.
echo ""
echo "[1/8] Stopping/disabling optional systemd unit if present..."
if [ -f "$SYSTEMD_UNIT" ]; then
  if ! run sudo systemctl disable --now azerothcore.service; then
    record_cleanup_failure "Could not stop/disable the azerothcore.service systemd unit; preserving stack and installer state."
    preserve_recovery_context_and_exit
  fi
else
  echo "No azerothcore.service unit found."
fi

# 2) Bring down compose stack when possible, including named volumes.
#    Use -v to drop project-scoped named volumes; bind mounts under STACK_DIR
#    are untouched by -v and are removed with the stack directory in step 7.
echo ""
echo "[2/8] Bringing down Docker compose stack if possible..."
if [ -d "$STACK_DIR" ] && { [ -f "$STACK_DIR/docker-compose.yml" ] || [ -f "$STACK_DIR/compose.yml" ]; }; then
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    if [ "$DRY_RUN" = true ]; then
      echo "[dry-run] cd '$STACK_DIR' && docker compose -p ${COMPOSE_PROJECT} down -v"
    else
      if ! (cd "$STACK_DIR" && docker compose -p "${COMPOSE_PROJECT}" down -v); then
        record_cleanup_failure "Docker compose down failed; preserving stack and installer state."
      fi
    fi
  else
    record_cleanup_failure "Docker compose is unavailable; preserving stack and installer state."
  fi
else
  echo "No compose file found under $STACK_DIR."
fi

# 3) Fallback cleanup: known named containers, project-labelled networks/volumes.
#    The label-based filter catches anything the upstream compose declared that
#    isn't in our hard-coded KNOWN_NETWORKS list, without using --remove-orphans
#    (which could touch unrelated containers sharing the project name).
echo ""
echo "[3/8] Removing leftover containers, networks, and named volumes..."
if [ "$CLEANUP_FAILED" = true ] && [ "$DRY_RUN" != true ]; then
  preserve_recovery_context_and_exit
fi

if command -v docker >/dev/null 2>&1; then
  for c in "${KNOWN_CONTAINERS[@]}"; do
    if docker inspect "$c" >/dev/null 2>&1; then
      run docker rm -f "$c" || record_cleanup_failure "Could not remove container $c."
    fi
  done

  # Project-labelled networks (covers azerothcore_default and any others).
  while IFS= read -r n; do
    [ -z "$n" ] && continue
    run docker network rm "$n" >/dev/null || record_cleanup_failure "Could not remove network $n."
  done < <(docker network ls --quiet \
            --filter "label=com.docker.compose.project=${COMPOSE_PROJECT}" 2>/dev/null)
  # Also try the hard-coded names in case the label was stripped.
  for n in "${KNOWN_NETWORKS[@]}"; do
    if docker network inspect "$n" >/dev/null 2>&1; then
      run docker network rm "$n" >/dev/null || record_cleanup_failure "Could not remove network $n."
    fi
  done

  # Project-labelled named volumes (host bind mounts are not affected here).
  while IFS= read -r v; do
    [ -z "$v" ] && continue
    run docker volume rm -f "$v" >/dev/null || record_cleanup_failure "Could not remove volume $v."
  done < <(docker volume ls --quiet \
            --filter "label=com.docker.compose.project=${COMPOSE_PROJECT}" 2>/dev/null)
else
  if [ "$DRY_RUN" != true ] && has_recovery_context; then
    record_cleanup_failure "Docker command is unavailable; preserving stack and installer state."
  else
    echo "Docker command not found; skipping Docker fallback cleanup."
  fi
fi

if [ "$CLEANUP_FAILED" = true ] && [ "$DRY_RUN" != true ]; then
  preserve_recovery_context_and_exit
fi

# 4) Compose/fallback Docker cleanup has now succeeded (or was skipped in
#    dry-run), so it is safe to remove the systemd unit file itself.
echo ""
echo "[4/8] Removing systemd unit file now that Docker cleanup has succeeded..."
if [ -f "$SYSTEMD_UNIT" ]; then
  run sudo rm -f "$SYSTEMD_UNIT"
  run sudo systemctl daemon-reload
  run sudo systemctl reset-failed azerothcore.service || true
else
  echo "No azerothcore.service unit file to remove."
fi

# 5) Remove locally-built AzerothCore images. Honors a custom DOCKER_IMAGE_TAG
#    from the stack's .env when present; otherwise falls back to the install
#    script's default tag (playerbot-local).
echo ""
echo "[5/8] Removing locally-built AzerothCore Docker images..."
if command -v docker >/dev/null 2>&1; then
  IMAGE_TAG="playerbot-local"
  if [ -r "${STACK_DIR}/.env" ]; then
    env_tag="$(grep -E '^DOCKER_IMAGE_TAG=' "${STACK_DIR}/.env" 2>/dev/null \
                 | tail -n1 | cut -d= -f2- | tr -d '"'"'")"
    if [ -n "${env_tag:-}" ]; then
      IMAGE_TAG="$env_tag"
    fi
  fi
  for repo in "${LOCAL_IMAGE_REPOS[@]}"; do
    img="${repo}:${IMAGE_TAG}"
    if docker image inspect "$img" >/dev/null 2>&1; then
      run docker image rm "$img" >/dev/null || true
    fi
  done
else
  echo "Docker command not found; skipping image cleanup."
fi

# 6) Remove backup cron lines for current user.
echo ""
echo "[6/8] Removing matching backup cron entries from current user's crontab..."
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

# 7) Remove stack directory and installer state files.
echo ""
echo "[7/8] Removing stack directory and installer state files..."
if [ ! -d "$STACK_DIR" ]; then
  echo "Stack directory already absent: $STACK_DIR"
fi
safe_remove_literal "$STACK_DIR" sudo
safe_remove_literal "$STATE_FILE"
safe_remove_literal "$CONFIG_FILE"

# 8) Remove known temporary files created by installer validation/build logging,
#    plus any stale temp clone left behind by an interrupted Phase 1.
#    The clone dir sits under /opt/stacks/ and is owned by root, so use sudo.
echo ""
echo "[8/8] Removing known temporary installer files..."
safe_remove_glob "/tmp/azerothcore-install-*.log"
safe_remove_glob "/tmp/ac-compose-effective.*.yml"
safe_remove_glob "/tmp/ac-xp-rate-overrides.*"
safe_remove_glob "/tmp/ac-playerbots-schema-check.out"
safe_remove_glob "/opt/stacks/.azerothcore-clone-*" sudo
safe_remove_literal /tmp/ac-build.log

cat <<DONE

Done.

Remaining things intentionally left installed/configured:
  - Docker engine, the Docker apt repo, and GPG keyring under /usr/share/keyrings/
  - Upstream Docker images that were pulled (not built) by the installer
  - Tailscale and Tailscale login/auth state
  - UFW and any firewall rules added by the installer (allow ssh, allow in on tailscale0)
  - apt packages installed by the installer (cron, git, curl, gnupg, openssl, unzip, ufw)
  - docker group membership for your user
  - cron service (still enabled; only the AzerothCore backup line was removed)

To reinstall from scratch, run the installer again as your normal user.
DONE
