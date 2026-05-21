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

# Strip COMPOSE_FILE from AC's .env so it falls back to default behavior.
if grep -qE '^COMPOSE_FILE=' "$AC_STACK_DIR/.env" 2>/dev/null; then
    run sudo sed -i '/^COMPOSE_FILE=/d' "$AC_STACK_DIR/.env"
fi

# Leave docker-compose.admin.yml in place; harmless empty file. Operator can rm if desired.

echo "Uninstall complete."
