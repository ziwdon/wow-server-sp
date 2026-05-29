# Admin Web App Reference (wow-server-sp-admin)

## Overview

`wow-server-sp-admin` is a FastAPI + HTMX web admin for monitoring and managing the running AzerothCore server. It runs as a separate Docker stack at `/opt/stacks/azerothcore-admin/`.

- **Access:** `http://<tailscale-ip>:<admin-port>/` (port set during install)
- **Stack:** Separate from AC's Docker stack — admin can manage AC without being affected by AC restarts
- **Filesystem writes:** Inside `/opt/stacks/azerothcore/`, the admin only writes `docker-compose.admin.yml` and files in `backups/`. In-app Restore also mutates AzerothCore databases via `docker exec`, as documented below.

## Installation / Management Scripts

```bash
# Install or upgrade the admin stack:
./wow-server-sp-admin/scripts/install-azerothcore-admin.sh

# Rebuild + restart after code changes (preserves admin.yml, .env, snapshots):
./wow-server-sp-admin/scripts/redeploy-azerothcore-admin.sh

# Post-install verification:
./wow-server-sp-admin/scripts/verify-azerothcore-admin.sh

# Remove admin stack only (does NOT touch AC):
./wow-server-sp-admin/scripts/uninstall-azerothcore-admin.sh [--dry-run] [--yes]
```

## Dashboard Features

### Status Panel
- Server status (online/offline/starting/stopping)
- Player count (real players online)
- Database size
- Server uptime

### Log Tabs (Dashboard)
Three log views accessible via tab switcher:
- **Server Log** (`Server.log`) — boot/init log; quiet after world init (frozen mtime is normal)
- **Playerbots Log** (`Playerbots.log`) — bot activity; chatty and mostly benign
- **Errors Log** (`Errors.log`) — runtime errors; **0 bytes = clean server**

### Actions (Dashboard)
- **Restart** — graceful restart (announce → saveall → stop → wait → start)
- **Stop** — graceful stop with configurable countdown
- **Backup** — triggers a manual backup through the same bundled `backup.sh` used by nightly cron

### Backup Status
Shows the latest backup status from `/opt/stacks/azerothcore/backups/`.

### Players Panel
Shows online real players with basic info.

## Settings Page

The Settings page allows browsing and modifying AzerothCore configuration via `AC_*` env vars.

### How it works
1. Browse ~1874 config keys from all `.conf.dist` files
2. Filter by "Show only modified" (default) or show all
3. Click a key to see its description, default value, current value
4. Edit the value and add to pending changes
5. Click **Apply** to write changes to `docker-compose.admin.yml` and restart the worldserver

### Filters
- **Show only modified** (default) — shows keys with `source: admin` or `source: installer`
- When unchecked — shows all ~1874 keys
- Pending-count badge and Apply button always reflect true pending state

### Apply Flow
1. Admin validates each AC_* against the loaded config keys (prevents silent-drop)
2. Snapshots current `admin.yml` to `/opt/stacks/azerothcore-admin/snapshots/admin.yml.bak.<ts>`
3. Writes new `docker-compose.admin.yml`
4. Restarts worldserver (same graceful path as Dashboard Restart)

### Rollback
If something breaks after Apply, use the Rollback button to restore the most recent snapshot.

## Backups & Restore

The Backups page lists available `azerothcore-backup-<label>-<stamp>.tar.gz` archives, creates manual backups, and restores a selected archive. Restore is a same-machine rollback path: it imports backed-up databases with `docker exec`, restores `docker-compose.admin.yml`, and takes a `prerestore` safety backup first.

Backups are kept for 7 days by the nightly cron run of `backup.sh`; daily pruning covers daily, manual, and prerestore archives. To keep a backup longer, copy the archive off the server from `/opt/stacks/azerothcore/backups/`.

For fresh-machine disaster recovery, use `docs/runbooks/disaster-recovery.md`: reinstall AzerothCore first, copy the chosen archive to the new host, then run `./scripts/restore-azerothcore.sh /path/to/archive.tar.gz`.

### Blocked Keys
`AuctionHouseBot.GUIDs` is blocked — it's installer-managed and cannot be set via the admin UI.

## Key Architecture Notes

**Admin container runs as non-root** with `HOST_UID:HOST_GID` and `group_add: [DOCKER_GID]` to access `/var/run/docker.sock`. Without the docker group membership, all Docker SDK calls fail with `PermissionError(13)`.

**Server restart path uses PTY attachment**, not `subprocess.PIPE`. The worldserver container has `Tty=true`; attaching via a pipe fails immediately. The admin opens a pseudo-terminal for command injection.

**Stop mechanism uses `docker stop --time 60`** (not `server shutdown N`). AC's SIGTERM handler collapses any in-progress `server shutdown N` countdown. The 60-second grace period accounts for bot-heavy saveall (30-45s) before Docker sends SIGKILL.

**SSE activity log at `/api/action/stream`** is exempt from GZip compression (some browsers silently drop gzip-encoded SSE events).

## Admin .env Variables

| Variable | Description |
|----------|-------------|
| `TAILSCALE_IP` | Admin app bind address |
| `ADMIN_PORT` | Port the admin listens on |
| `HOST_UID` / `HOST_GID` | User/group IDs for file ownership |
| `DOCKER_GID` | Docker group GID for socket access |

## File Paths

| Path | Description |
|------|-------------|
| `/opt/stacks/azerothcore-admin/` | Admin stack root |
| `/opt/stacks/azerothcore-admin/.env` | Admin runtime vars (mode 600) |
| `/opt/stacks/azerothcore-admin/snapshots/` | `admin.yml.bak.<ts>` snapshots (7-day retention) |
| `/opt/stacks/azerothcore/docker-compose.admin.yml` | Admin-written Compose overlay |
| `/opt/stacks/azerothcore/backups/` | Consolidated daily, manual, and prerestore backup archives |

## Backup Format

Admin-created backups and nightly cron backups use the same single-archive format:
- `azerothcore-backup-<label>-<stamp>.tar.gz` — consolidated archive
- `manifest.json` — format version, label, database list, git revisions, image tag
- `sql/*.sql` — mysqldump output for each backed-up database
- `config/` — staged config files included for restore/DR workflows

## Viewing Admin Logs

```bash
docker logs azerothcore-admin           # JSON-formatted stdout
docker logs --tail 50 azerothcore-admin # Recent entries
```

## Running Tests

Admin app tests run in Docker (no local venv needed):
```bash
docker run --rm -v "$(pwd)/wow-server-sp-admin:/src" -w /src python:3.12-slim \
    bash -c "pip install -r requirements-dev.txt -q && python -m pytest -q"
```

Expected test warnings (harmless):
- pip "running as root" warning inside the container
- Starlette `multipart` pending-deprecation warning
- `TemplateResponse` call-style deprecation warning

## Redeploying After Code Changes

Use the redeploy script (never `docker compose up` directly — it would wipe config):
```bash
./wow-server-sp-admin/scripts/redeploy-azerothcore-admin.sh
```
This: rebuilds the image → stops old container → starts new container → preserves `admin.yml`, `.env`, snapshots.
