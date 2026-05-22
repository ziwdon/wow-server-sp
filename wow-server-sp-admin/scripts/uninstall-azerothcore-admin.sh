#!/bin/bash
set -euo pipefail

STACK_DIR=/opt/stacks/azerothcore-admin
AC_STACK_DIR=/opt/stacks/azerothcore
ADMIN_YML_PATH="$AC_STACK_DIR/docker-compose.admin.yml"
SYSTEMD_SERVICE=azerothcore-admin.service
SYSTEMD_UNIT=/etc/systemd/system/$SYSTEMD_SERVICE
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

    local env_dir env_base tmp
    env_dir="$(dirname "$env_file")"
    env_base="$(basename "$env_file")"
    tmp="$(sudo mktemp "${env_dir}/${env_base}.tmp.XXXXXX")"

    cleanup_env_tmp() {
        if [ -n "${tmp:-}" ]; then
            sudo rm -f "$tmp"
        fi
    }
    trap cleanup_env_tmp RETURN

    sudo awk -v target="docker-compose.admin.yml" '
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
    ' "$env_file" | sudo tee "$tmp" >/dev/null
    sudo chown --reference="$env_file" "$tmp"
    sudo chmod --reference="$env_file" "$tmp"
    sudo mv -f "$tmp" "$env_file"
    tmp=""
    trap - RETURN
}

remove_admin_compose_file() {
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "WOULD: remove $ADMIN_YML_PATH if it is a regular file"
        return
    fi

    if [ -f "$ADMIN_YML_PATH" ] || [ -L "$ADMIN_YML_PATH" ]; then
        sudo rm -f -- "$ADMIN_YML_PATH"
    elif [ -e "$ADMIN_YML_PATH" ]; then
        echo "Leaving non-regular $ADMIN_YML_PATH in place; inspect manually." >&2
    fi
}

remove_systemd_unit() {
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "WOULD: disable and remove $SYSTEMD_UNIT if present"
        return
    fi

    if [ -f "$SYSTEMD_UNIT" ]; then
        sudo systemctl disable --now "$SYSTEMD_SERVICE" >/dev/null 2>&1 || true
        sudo rm -f "$SYSTEMD_UNIT"
        sudo systemctl daemon-reload >/dev/null 2>&1 || true
    fi
}

if [ "${AUTO_YES:-0}" -ne 1 ] && [ "$DRY_RUN" -eq 0 ]; then
    read -rp "Remove azerothcore-admin stack (NOT AC itself)? [y/N]: " confirm
    [ "$confirm" = "y" ] || { echo "Aborted."; exit 0; }
fi

# NOTE: --remove-orphans is intentionally omitted. Per CLAUDE.md and the AC
# uninstaller convention, it can remove unrelated containers that share the
# Compose project name. The named `docker rm -f` below covers our one service.
remove_systemd_unit
run docker compose -f "$STACK_DIR/docker-compose.yml" --env-file "$STACK_DIR/.env" down || true
run docker rm -f azerothcore-admin 2>/dev/null || true
run docker rmi -f azerothcore-admin:local 2>/dev/null || true
run sudo rm -rf "$STACK_DIR"

# Strip only the admin compose layer from AC's COMPOSE_FILE. Preserve AC's
# base/override files so uninstalling admin cannot disturb the AC stack.
if grep -qE '^COMPOSE_FILE=' "$AC_STACK_DIR/.env" 2>/dev/null; then
    remove_admin_compose_file_from_ac_env "$AC_STACK_DIR/.env"
fi

# Remove only the admin-managed compose overlay. Never recurse here: if this
# path is a directory or another non-regular file, leave it for manual review.
remove_admin_compose_file

echo "Uninstall complete."
