#!/bin/bash
set -euo pipefail

if [ "$EUID" -eq 0 ]; then
    echo "ERROR: do not run as root; sudo is invoked internally where needed." >&2
    exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STACK_DIR=/opt/stacks/azerothcore-admin
AC_STACK_DIR=/opt/stacks/azerothcore

if [ ! -d "$AC_STACK_DIR" ]; then
    echo "ERROR: AzerothCore stack not found at $AC_STACK_DIR." >&2
    echo "Install AzerothCore first via scripts/install-azerothcore.sh." >&2
    exit 1
fi

# --- Step 1: Tailscale IP detection ---
if ! command -v tailscale >/dev/null 2>&1; then
    echo "ERROR: tailscale CLI not found; admin requires Tailscale." >&2
    exit 1
fi
TAILSCALE_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
if [ -z "$TAILSCALE_IP" ]; then
    echo "ERROR: could not detect Tailscale IPv4 address." >&2
    exit 1
fi
echo "Tailscale IP: $TAILSCALE_IP"

# --- Step 2: port selection / collision check ---
ADMIN_PORT="${ADMIN_PORT:-8765}"
while ss -ltn "sport = :$ADMIN_PORT" 2>/dev/null | grep -q ":$ADMIN_PORT"; do
    echo "Port $ADMIN_PORT is in use:"
    sudo ss -ltnp "sport = :$ADMIN_PORT" || true
    read -rp "Enter a different port [default 8765]: " ADMIN_PORT
    ADMIN_PORT="${ADMIN_PORT:-8765}"
done
echo "Admin port: $ADMIN_PORT"

# --- Step 3: COMPOSE_FILE entry in AC's .env (preserves existing entries) ---
# We never overwrite an existing COMPOSE_FILE wholesale -- splitting on `:`
# lets us append docker-compose.admin.yml without dropping any custom file
# a future installer or operator may have added. This edit only touches
# .env; it does NOT modify docker-compose.override.yml or any other
# compose file content.
ADMIN_YML_NAME='docker-compose.admin.yml'
ADMIN_YML_PATH="$AC_STACK_DIR/$ADMIN_YML_NAME"
existing_line="$(grep -E '^COMPOSE_FILE=' "$AC_STACK_DIR/.env" 2>/dev/null | head -n1 || true)"
if [ -z "$existing_line" ]; then
    new_line="COMPOSE_FILE=docker-compose.yml:docker-compose.override.yml:${ADMIN_YML_NAME}"
    echo "Adding COMPOSE_FILE to $AC_STACK_DIR/.env."
    echo "$new_line" | sudo tee -a "$AC_STACK_DIR/.env" >/dev/null
else
    existing_value="${existing_line#COMPOSE_FILE=}"
    case ":${existing_value}:" in
        *":${ADMIN_YML_NAME}:"*)
            echo "COMPOSE_FILE already includes ${ADMIN_YML_NAME}; leaving as-is."
            ;;
        *)
            new_line="COMPOSE_FILE=${existing_value}:${ADMIN_YML_NAME}"
            echo "Appending ${ADMIN_YML_NAME} to existing COMPOSE_FILE in $AC_STACK_DIR/.env."
            # Use a same-directory temporary file so the final rename is atomic.
            # Reapply the original metadata because the replacement inode would
            # otherwise inherit the permissions and ownership of the temp file.
            env_file="$AC_STACK_DIR/.env"
            env_owner="$(stat -c '%u:%g' "$env_file")"
            env_mode="$(stat -c '%a' "$env_file")"
            env_tmp="$(sudo mktemp "$AC_STACK_DIR/.env.tmp.XXXXXX")"
            cleanup_env_tmp() {
                if [ -n "${env_tmp:-}" ] && [ -e "$env_tmp" ]; then
                    sudo rm -f -- "$env_tmp" || true
                fi
            }
            trap cleanup_env_tmp EXIT

            sudo awk -v old="$existing_line" -v new="$new_line" \
                '$0 == old { print new; next } { print }' \
                "$env_file" | sudo tee "$env_tmp" >/dev/null
            sudo chown "$env_owner" "$env_tmp"
            sudo chmod "$env_mode" "$env_tmp"
            sudo mv -f -- "$env_tmp" "$env_file"
            env_tmp=""
            trap - EXIT
            ;;
    esac
fi

# --- Step 4: empty admin.yml so AC compose calls don't fail ---
if [ -d "$ADMIN_YML_PATH" ]; then
    echo "ERROR: $ADMIN_YML_PATH exists as a directory; expected a regular file." >&2
    echo "The installer will not remove it. Inspect that directory, move or remove it yourself if appropriate, then re-run this installer." >&2
    exit 1
fi
if [ -e "$ADMIN_YML_PATH" ] && [ ! -f "$ADMIN_YML_PATH" ]; then
    echo "ERROR: $ADMIN_YML_PATH exists but is not a regular file." >&2
    exit 1
fi
if [ ! -f "$ADMIN_YML_PATH" ]; then
    echo "Creating empty $ADMIN_YML_PATH."
    sudo tee "$ADMIN_YML_PATH" >/dev/null <<'YAML'
# Managed by wow-server-sp-admin. AC_* env vars added/removed via the admin UI.
services:
  ac-worldserver:
    environment: {}
YAML
    sudo chown "$(id -u):$(id -g)" "$ADMIN_YML_PATH"
    sudo chmod 644 "$ADMIN_YML_PATH"
fi

# --- Step 4b: backups dir (rw target for explicit and restore-safety backups) ---
# The host's backup.sh cron normally creates this on first run, but the admin
# needs to write here on day one for Create backup and pre-restore safety archives.
if [ ! -d "$AC_STACK_DIR/backups" ]; then
    echo "Creating $AC_STACK_DIR/backups/."
    sudo mkdir -p "$AC_STACK_DIR/backups"
    sudo chown "$(id -u):$(id -g)" "$AC_STACK_DIR/backups"
    sudo chmod 700 "$AC_STACK_DIR/backups"
fi

# --- Step 5: stack dir + subdirs ---
# snapshots/ is the rw target for admin.yml.bak.<ts> files.
# data/ is the rw target for maintenance scheduler state (maintenance.json, maintenance-log.jsonl).
# Both MUST be writable as the same UID/GID that runs the admin container
# (HOST_UID/GID); if Docker creates them it does so as root:root, which
# prevents the non-root container user from writing.
sudo mkdir -p "$STACK_DIR" "$STACK_DIR/snapshots" "$STACK_DIR/data"
sudo chown "$(id -u):$(id -g)" "$STACK_DIR" "$STACK_DIR/snapshots" "$STACK_DIR/data"
sudo chmod 700 "$STACK_DIR/snapshots" "$STACK_DIR/data"

# --- Step 6: copy compose + dist into stack ---
cp "$REPO_DIR/docker-compose.yml" "$STACK_DIR/"
rsync -a --delete "$REPO_DIR/" "$STACK_DIR/build/"

# Stage .conf.dist files under build/ so the Dockerfile can `COPY dist/`.
mkdir -p "$STACK_DIR/build/dist"
cp "$REPO_DIR/../docs/configs/"*.conf.dist "$STACK_DIR/build/dist/"
# Stage the canonical backup script into the build context (Dockerfile COPYs it).
cp "$REPO_DIR/../scripts/backup.sh" "$STACK_DIR/build/backup.sh"

# Vendor HTMX core + the SSE extension (one-time fetch). Both are referenced
# in base.html from Task 4 -- vendoring both up-front means base.html never
# 404s during Phase A/B/C even though the SSE-consuming UI lands in Phase D.
HTMX_VERSION=2.0.3
HTMX_SSE_VERSION=2.2.2
curl -sSfL -o "$STACK_DIR/build/app/static/htmx.min.js" \
    "https://unpkg.com/htmx.org@${HTMX_VERSION}/dist/htmx.min.js"
# Upstream `htmx-ext-sse` ships `sse.js` (unminified) at the version-pinned
# path; save it as `htmx-sse.js` (no `.min`) so the filename is honest.
curl -sSfL -o "$STACK_DIR/build/app/static/htmx-sse.js" \
    "https://unpkg.com/htmx-ext-sse@${HTMX_SSE_VERSION}/sse.js"

# --- Step 7: write .env ---
# DOCKER_GID is the host's docker-group GID. The admin container runs as
# the non-root admin user (UID=HOST_UID, primary group GID=HOST_GID) and
# would otherwise be unable to read /var/run/docker.sock (typically owned
# root:docker, mode 660). docker-compose.yml's `group_add: ["${DOCKER_GID}"]`
# gives the admin user supplementary access to the socket. Fall back to 999
# (the Ubuntu/Debian default for the docker group) only if the lookup fails.
DOCKER_GID="$(getent group docker | awk -F: '{print $3}')"
if [ -z "$DOCKER_GID" ]; then
    echo "WARNING: docker group not found via getent; defaulting DOCKER_GID=999." >&2
    DOCKER_GID=999
fi
cat > "$STACK_DIR/.env" <<EOF
TAILSCALE_IP=$TAILSCALE_IP
ADMIN_PORT=$ADMIN_PORT
HOST_UID=$(id -u)
HOST_GID=$(id -g)
DOCKER_GID=$DOCKER_GID
EOF
chmod 600 "$STACK_DIR/.env"

# --- Step 8: build image and bring up ---
cd "$STACK_DIR"
docker compose --project-directory "$STACK_DIR/build" --env-file "$STACK_DIR/.env" build
docker compose --env-file "$STACK_DIR/.env" up -d

echo ""
echo "Admin app starting at http://${TAILSCALE_IP}:${ADMIN_PORT}/"
echo "Verify with: $REPO_DIR/scripts/verify-azerothcore-admin.sh"

# --- Step 9: optional systemd unit ---
echo ""
read -rp "Install azerothcore-admin.service systemd unit (auto-start at boot)? [Y/n] " answer
case "${answer:-y}" in
    [yY])
        sudo tee /etc/systemd/system/azerothcore-admin.service <<'UNIT' >/dev/null
[Unit]
Description=AzerothCore Admin (Docker Compose)
Requires=docker.service tailscaled.service
Wants=network-online.target azerothcore.service
After=docker.service tailscaled.service network-online.target azerothcore.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/stacks/azerothcore-admin
ExecStartPre=/bin/bash -lc 'source /opt/stacks/azerothcore-admin/.env; for i in {1..60}; do tailscale ip -4 2>/dev/null | grep -Fxq "$TAILSCALE_IP" && exit 0; echo "Waiting for Tailscale IP $TAILSCALE_IP..."; sleep 2; done; echo "ERROR: Tailscale IP $TAILSCALE_IP not assigned"; exit 1'
ExecStart=/usr/bin/docker compose --env-file /opt/stacks/azerothcore-admin/.env up -d
ExecStop=/usr/bin/docker compose --env-file /opt/stacks/azerothcore-admin/.env down
TimeoutStartSec=300
User=REPLACE_WITH_YOUR_USERNAME

[Install]
WantedBy=multi-user.target
UNIT
        sudo sed -i "s/REPLACE_WITH_YOUR_USERNAME/$(whoami)/" /etc/systemd/system/azerothcore-admin.service
        sudo systemctl daemon-reload
        sudo systemctl enable --now azerothcore-admin.service
        echo "azerothcore-admin.service installed and enabled."
        ;;
esac
