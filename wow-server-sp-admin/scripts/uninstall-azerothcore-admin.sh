#!/bin/bash
set -euo pipefail

STACK_DIR=/opt/stacks/azerothcore-admin
AC_STACK_DIR=/opt/stacks/azerothcore
DRY_RUN=0

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --yes) AUTO_YES=1 ;;
    esac
done

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "WOULD: $*"
    else
        "$@"
    fi
}

remove_admin_compose_file_from_ac_env() {
    local env_file="$1"
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "WOULD: remove docker-compose.admin.yml from COMPOSE_FILE in $env_file"
        return
    fi

    local tmp
    tmp="$(mktemp)"
    awk -v target="docker-compose.admin.yml" '
        /^COMPOSE_FILE=/ {
            value = substr($0, index($0, "=") + 1)
            count = split(value, parts, ":")
            kept = ""
            for (i = 1; i <= count; i++) {
                if (parts[i] == "" || parts[i] == target) {
                    continue
                }
                kept = kept (kept == "" ? "" : ":") parts[i]
            }
            if (kept != "") {
                print "COMPOSE_FILE=" kept
            }
            next
        }
        { print }
    ' "$env_file" > "$tmp"
    sudo cp "$tmp" "$env_file"
    rm -f "$tmp"
}

if [ "${AUTO_YES:-0}" -ne 1 ] && [ "$DRY_RUN" -eq 0 ]; then
    read -rp "Remove azerothcore-admin stack (NOT AC itself)? [y/N]: " confirm
    [ "$confirm" = "y" ] || { echo "Aborted."; exit 0; }
fi

# NOTE: --remove-orphans is intentionally omitted. Per CLAUDE.md and the AC
# uninstaller convention, it can remove unrelated containers that share the
# Compose project name. The named `docker rm -f` below covers our one service.
run docker compose -f "$STACK_DIR/docker-compose.yml" --env-file "$STACK_DIR/.env" down || true
run docker rm -f azerothcore-admin 2>/dev/null || true
run docker rmi -f azerothcore-admin:local 2>/dev/null || true
run sudo rm -rf "$STACK_DIR"

# Strip only the admin compose layer from AC's COMPOSE_FILE. Preserve AC's
# base/override files so uninstalling admin cannot disturb the AC stack.
if grep -qE '^COMPOSE_FILE=' "$AC_STACK_DIR/.env" 2>/dev/null; then
    remove_admin_compose_file_from_ac_env "$AC_STACK_DIR/.env"
fi

# Leave docker-compose.admin.yml in place; harmless empty file. Operator can rm if desired.

echo "Uninstall complete."
