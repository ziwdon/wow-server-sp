# Admin Web App Reference (wow-server-sp-admin)

## Overview

`wow-server-sp-admin` is a FastAPI + HTMX web admin for monitoring and managing the running AzerothCore server. It runs as a separate Docker stack at `/opt/stacks/azerothcore-admin/`.

- **Access:** `http://<tailscale-ip>:<admin-port>/` (port set during install); optionally also `http://<lan-ip>:<admin-port>/` when `LAN_IP` is set
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
Four tabs: Server.log, Playerbots.log, Errors.log, App Events.
- **Server Log** (`Server.log`) — boot/init log; quiet after world init (frozen mtime is normal)
- **Playerbots Log** (`Playerbots.log`) — bot activity; chatty and mostly benign
- **Errors Log** (`Errors.log`) — runtime errors. The bar above the tabs has three states:
  - green **No runtime errors** — the file is empty;
  - neutral **Only known harmless warnings (N)** — every line is a documented harmless one (graveyard gap, empty pool 1072, the Shattered Hand Berserker emote). The tab opens with a **Summary: known harmless warnings** section (each one's reason and count), then a **Raw log: last 40 lines** section where the matching lines are dimmed with a KNOWN tag. Nothing to do;
  - red **N runtime errors detected** — at least one line is not known. Known lines in the same file stay dimmed, so the real error stands out. Also red when the file is over 1 MB (not classified).
  The same list drives `verify-azerothcore.sh` Check 16 (INFO instead of FAIL).
- **App Events** — the admin app's own warnings/errors since its last restart

### Actions (Dashboard)
- **Restart** — graceful restart (announce → saveall → stop → wait → start)
- **Stop** — graceful stop with configurable countdown
- **Backup** — triggers a manual backup through the same bundled `backup.sh` used by nightly cron

### Backup Status
Shows the latest backup status from `/opt/stacks/azerothcore/backups/`.

### Players Panel
Shows online real players with basic info.

**Who counts as "online" (Players tab + dashboard "Online" card):** the admin's presence tracker (see Player Announcements below) remembers the first character that came online on each real account — that is the human; alt-bots summoned afterwards are ignored. The Players tab's **Online now** card lists it as `Account (Character)`, exactly like the in-game announcement, and shows it immediately after login. If the tracker has no memory for an account (admin restarted while you were playing, or you were already logged in when the admin started), the page falls back to the old rule — a character with `online=1` **and** `latency > 0` — which can lag up to 15 minutes after login because AC only writes `latency` on its periodic character save. So a human never disappears from the list; they may just appear late in that fallback case. The "Last online" column in All characters shows `online` under the same rule, otherwise today/yesterday/N days ago.

## Settings Page

The Settings page allows browsing and modifying AzerothCore configuration via `AC_*` env vars.

### How it works
1. Browse ~1996 config keys from all `.conf.dist` files (built from the AC source tree the server was built from, so the list matches the deployed version; falls back to `docs/configs/` if a file is missing)
2. Filter by "Show only modified" (default) or show all
3. Click a key to see its description, default value, current value
4. Edit the value and add to pending changes
5. Click **Apply** to write changes to `docker-compose.admin.yml` and restart the worldserver

### Filters
- **Show only modified** (default) — shows keys with `source: admin` or `source: installer`
- When unchecked — shows all ~1996 keys
- Pending-count badge and Apply button always reflect true pending state

### Apply Flow
1. Admin validates each AC_* against the loaded config keys (prevents silent-drop)
2. Snapshots current `admin.yml` to `/opt/stacks/azerothcore-admin/snapshots/admin.yml.bak.<ts>`
3. Writes new `docker-compose.admin.yml`
4. Restarts worldserver (same graceful path as Dashboard Restart)

### Rollback
If something breaks after Apply, use the Rollback button to restore the most recent snapshot.

## Progression Page

The Progression page (`/progression`) lets you advance individual characters through expansion tiers (Classic → TBC → WotLK) without GM commands or direct database access.

### How to use it
1. Select an account from the dropdown
2. Pick a character from the scrollable list (sorted by level high → low, then name)
3. Click the target expansion icon — grayed-out tiles are either the current expansion or a downgrade and cannot be selected
4. A confirmation dialog summarises the action and its caveats; click **Yes, proceed**
5. The admin inserts the missing `character_queststatus_rewarded` rows and returns the result inline

### Icon states
| State | Appearance | Meaning |
|-------|-----------|---------|
| Current | Dimmed, no click | The character's existing expansion boundary |
| Available | Semi-opaque, clickable | A valid upward target |
| Downgrade | Fully grayed, no click | Below the character's current progression state |

### Constraints
- The character must be **offline** — the page warns and blocks Apply for online characters
- Progression is **forward-only** — the service rejects downgrade requests server-side as well as in the UI
- The internal key `"vanilla"` is displayed as **"Classic"** throughout the UI; the database and service always use `"vanilla"`

## Maintenance Page

- **Bot Control** — `Reset bots` (sends `playerbot rndbot init`: every random bot **currently logged in** keeps its name/race/class but is re-rolled to a fresh level within `RandomBotMinLevel`/`MaxLevel`, new talents and gear, and teleported to a level-appropriate zone; offline bots are untouched, no accounts or characters are deleted) and `Clear bots` (destructive: stops the server, safety backup, deletes every RNDBOT account and its characters, restarts; pool regenerates on next boot).
- **Player Announcements** — one checkbox, saves immediately. The admin always polls `acore_characters.characters.online` every 15 s for real accounts (not `RNDBOT%`/`ahbot`) to track who is in (this feeds the Players tab and the dashboard "Online" card); when the checkbox is on it also sends `announce Player <Account> (<Character>) is online.` / `… has gone offline.` over the worldserver console (account name title-cased; the character is omitted only when two characters appeared in the same poll). Presence is tracked **per account**: the first character to come online is the human, so alt-bots summoned from your own account are never announced; "offline" needs two consecutive empty polls, so a quick character swap is silent. Nothing is announced around admin Stop/Restart or worldserver boots (state reseeds silently). Config: `azerothcore-admin/data/presence.json`; API: `GET/POST /api/presence`.
- **Scheduled Restart** / **Stop / Start Window** — daily UTC-hour jobs run by the in-app scheduler (`maintenance.json`); the log below shows the last 20 runs.

## Backups & Restore

The Backups page lists available `azerothcore-backup-<label>-<stamp>.tar.gz` archives, creates manual backups, and restores a selected archive. Restore is a same-machine rollback path: it imports backed-up databases with `docker exec`, restores `docker-compose.admin.yml`, and takes a `prerestore` safety backup first. It refuses an archive taken on different AzerothCore/module source revisions than the running server (or one whose revisions are unknown); cross-version restores are host-only (`scripts/restore-azerothcore.sh`).

Backups are kept for 7 days by the nightly cron run of `backup.sh`; daily pruning covers every label (daily, manual, prerestore, preclear) except `preupgrade` and `prerollback` (an upgrade rollback's copy of the upgraded state), which are never pruned (delete those archives by hand once no longer needed). To keep a backup longer or off the server, copy the archive from `/opt/stacks/azerothcore/backups/` by hand or turn on Cloud Backup (below).

For fresh-machine disaster recovery, use `docs/runbooks/disaster-recovery.md`: reinstall AzerothCore first, copy the chosen archive to the new host, then run `./scripts/restore-azerothcore.sh /path/to/archive.tar.gz`. The script refuses an archive whose manifest `git_revisions` differ from the stack's checkouts: check out those revisions and rebuild first, or pass `--allow-version-mismatch` knowingly.

### Blocked Keys
`AuctionHouseBot.GUIDs` is blocked — it's installer-managed and cannot be set via the admin UI.

## Cloud Backup (Proton Drive)

Optional panel on the **Backups** tab (**Cloud backup · Proton Drive**) that copies each backup archive to a folder in the owner's own Proton Drive, so a dead disk or lost machine does not take every backup with it.

**What it does**
- Every 10 minutes it checks whether a selected backup is missing from the Drive folder and uploads only what is new. It never replaces a Drive file: a different file with the same name is reported in the panel (amber) and left alone — move that Drive copy to the Drive trash by hand if you want this server's version uploaded.
- Retention is applied on the Drive itself (default 7 days, 1–90, same cut-off as the local nightly prune): older copies go to the **Proton Drive trash** (recoverable — the owner empties it), never erased. `preupgrade`/`prerollback` archives are never removed, the newest archive is always kept, and nothing is trashed until the newest backup is confirmed in the Drive.
- Archives found in the Drive get an **In Drive** badge in the backups list. After a failed run the next automatic attempt waits 60 minutes (**Upload now** ignores that); failing for over 26 hours with no success shows **Stalled**.
- The panel's "Recent cloud runs" lists the last runs (`ok`, `warning` = conflicts only, `partial` = some files failed, `error` = the run stopped).

**Connect** (needs a TPM 2.0 chip; with none the panel says **Not available** and why, and there is no software-only fallback)
1. Click **Connect to Proton Drive**, open the link on your own device (copyable; it expires in 10 minutes), sign in on Proton's page and approve. The Proton password is never typed on the server.
2. Set the **Drive folder** (e.g. `/my-files/WoW Backups`; must start with `/my-files/` or `/shared-with-me/`). If it does not exist, **Create folder** makes the last part of the path (the folders above must exist).
3. Tick *Copy new backups to Proton Drive automatically*, optionally pick which backup kinds to copy and the keep-for days, **Save cloud settings**. **Upload now** works as soon as the connection and folder are good, even with automatic copying off.

**Disconnect** deletes the stored session from this server and stops the copies (settings are kept). It does **not** end the session at Proton: revoke it in the Proton account's session list. The same applies after uninstalling the admin app (the uninstaller prints the reminder).

**Restore from the Drive:** download the archive in Proton Drive, then **Import & Restore** on the Backups tab; on a new machine reinstall AzerothCore and run `scripts/restore-azerothcore.sh ARCHIVE`.

**Security in plain words**
- The password is typed only on Proton's page, on the owner's device.
- The stored session can read, change and trash the whole Drive and probably the rest of the account's encrypted data until revoked; it cannot change the password, 2FA or recovery settings.
- At rest it is GPG-encrypted with a passphrase that exists only sealed to this server's TPM: a stolen disk, a copied data folder or a backup archive is useless elsewhere. Whoever controls the running server (the owner's user, root, or the admin container, which has the Docker socket) can use it.
- The admin has no login, so anyone who can reach it (Tailscale/LAN) can use the panel like any other page, but no page reveals the session.
- Safer later: a separate Proton account with one folder shared to it as editor (folder `/shared-with-me/…`), so the worst case is a damaged backups folder.
- Only Proton receives data; its CLI sends crash reports/telemetry per the account's preference.

**Troubleshooting:** "Can't unlock the stored session — reconnect" = the TPM seal no longer opens (TPM cleared, or the data folder was copied to another machine): press **Reconnect**. A stale `TPM_DEVICE=/dev/tpmrm0` in `azerothcore-admin/.env` on a host without that device stops the admin container from starting — set `TPM_DEVICE=/dev/null` (or delete the line and redeploy). `verify-azerothcore-admin.sh` prints a single advisory `[INFO] cloud backup: …` line; it never fails the verify.

## Key Architecture Notes

**Admin container runs as non-root** with `HOST_UID:HOST_GID` and `group_add: [DOCKER_GID]` to access `/var/run/docker.sock`. Without the docker group membership, all Docker SDK calls fail with `PermissionError(13)`.

**Server restart path uses PTY attachment**, not `subprocess.PIPE`. The worldserver container has `Tty=true`; attaching via a pipe fails immediately. The admin opens a pseudo-terminal for command injection.

**Stop mechanism uses `docker stop --time 60`** (not `server shutdown N`). AC's SIGTERM handler collapses any in-progress `server shutdown N` countdown. The 60-second grace period accounts for bot-heavy saveall (30-45s) before Docker sends SIGKILL.

**SSE activity log at `/api/action/stream`** is exempt from GZip compression (some browsers silently drop gzip-encoded SSE events).

## Admin .env Variables

| Variable | Description |
|----------|-------------|
| `TAILSCALE_IP` | Admin app bind address (always on) |
| `LAN_IP` | Optional second bind on a LAN interface (empty = Tailscale only; unset falls back to loopback, never `0.0.0.0`). Set via `LAN_IP=<ip> ./install-azerothcore-admin.sh`, or add to `.env` and redeploy. The admin has no login — anyone on the LAN gets full control. Needs host sysctl `net.ipv4.ip_nonlocal_bind=1`, or a missing LAN IP stops the whole container (Tailscale too) |
| `ADMIN_PORT` | Port the admin listens on |
| `HOST_UID` / `HOST_GID` | User/group IDs for file ownership |
| `DOCKER_GID` | Docker group GID for socket access |
| `TPM_DEVICE` / `TSS_GID` | Cloud Backup: host TPM device mapped into the container (`/dev/tpmrm0`, or `/dev/null` when the host has none) and the `tss` group GID that can open it. The installer writes both; the redeploy appends whichever is missing and never edits existing lines |

## File Paths

| Path | Description |
|------|-------------|
| `/opt/stacks/azerothcore-admin/` | Admin stack root |
| `/opt/stacks/azerothcore-admin/.env` | Admin runtime vars (mode 600) |
| `/opt/stacks/azerothcore-admin/snapshots/` | `admin.yml.bak.<ts>` snapshots (7-day retention) |
| `/opt/stacks/azerothcore-admin/data/cloud/` | Cloud Backup: `config.json`, `state.json`, `log.jsonl` and `secrets/` (the encrypted Proton session; mode 700). Never included in backup archives |
| `/opt/stacks/azerothcore/docker-compose.admin.yml` | Admin-written Compose overlay |
| `/opt/stacks/azerothcore/backups/` | Consolidated backup archives (daily, manual, prerestore, preclear, preupgrade, prerollback) |

## Backup Format

Admin-created backups and nightly cron backups use the same single-archive format:
- `azerothcore-backup-<label>-<stamp>.tar.gz` — consolidated archive
- `manifest.json` — format version, label, database list, git revisions, image tag, image IDs, `client_data_version`, `backup_script_sha256`
- `sql/azerothcore.sql` — one mysqldump transaction covering all four databases
- `config/` — staged config files included for restore/DR workflows: `.env`, the compose overlays, `configs/`, the generated `etc/*.conf` from `env/dist/etc/`, and `admin-data/{maintenance,presence}.json`

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
