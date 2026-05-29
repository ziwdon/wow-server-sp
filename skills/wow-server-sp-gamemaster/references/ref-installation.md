# Installation Reference

## Scripts Overview

All scripts live in `scripts/` and `wow-server-sp-admin/scripts/`.
**Never run as root** — scripts call `sudo` internally.

```bash
chmod +x scripts/*.sh

# Fresh install (auto-resumes from last checkpoint if interrupted):
./scripts/install-azerothcore.sh

# Resume from a specific phase:
./scripts/install-azerothcore.sh --resume-from=2.5
./scripts/install-azerothcore.sh --force-from=2.5   # same as above

# Full wipe and restart (requires typing WIPE at the confirmation prompt):
./scripts/install-azerothcore.sh --force-fresh

# Adopt an existing install (verifies state before marking phases complete):
./scripts/install-azerothcore.sh --adopt

# List available phases:
./scripts/install-azerothcore.sh --help

# Post-install verification (exits 0=pass, 1=fail):
./scripts/verify-azerothcore.sh

# Uninstall:
./scripts/uninstall-azerothcore.sh --dry-run   # preview
./scripts/uninstall-azerothcore.sh             # full teardown (with confirmation)
./scripts/uninstall-azerothcore.sh --yes       # skip confirmation

# Admin app:
./wow-server-sp-admin/scripts/install-azerothcore-admin.sh
./wow-server-sp-admin/scripts/redeploy-azerothcore-admin.sh    # rebuild+restart (preserves admin.yml, .env, snapshots)
./wow-server-sp-admin/scripts/verify-azerothcore-admin.sh
./wow-server-sp-admin/scripts/uninstall-azerothcore-admin.sh [--dry-run] [--yes]
```

## Install Phases

State is saved in `~/.azerothcore-install-state`. Each phase can be resumed individually.

| Phase | Description |
|-------|-------------|
| `0.0` | Pre-flight checks |
| `0.1` | OS version check (Ubuntu 22.04 recommended; 24.04 requires user confirmation) |
| `0.2` | System packages (apt) |
| `0.3` | Docker Engine install + verify |
| `0.4` | Tailscale install + authentication (**Manual pause 1**: Tailscale auth inline here) |
| `0.5` | Directory structure |
| `1` | Clone AzerothCore (mod-playerbots fork) + mod-playerbots + mod-ah-bot-plus + mod-individual-progression |
| `2.1` | Create `.env` |
| `2.2` | Create data directories |
| `2.3` | Clean Playerbots custom SQL duplicates |
| `2.4` | MySQL tuning config |
| `2.5` | Create `docker-compose.override.yml` (all AC_* env vars) |
| `2.6` | Compose validation |
| `3` | Docker compose build (compiles AzerothCore from source — takes a long time) |
| `3.1` | Install module conf templates |
| `4` | First run + DB init + client data download |
| `pause-2` | **Manual pause 2**: Account creation (GM + AHBOT) via `docker attach ac-worldserver` |
| `5` | Networking — Tailscale realmlist |
| `5.1` | UFW firewall (conditional) |
| `pause-3` | **Manual pause 3**: AH bot character creation in WoW client |
| `6.1.4` | Write GUID(s) into `configs/modules/mod_ahbot.conf` |
| `6.1.5` | Worldserver restart + AH verify |
| `7` | Backup script + cron |
| `8` | Systemd auto-start (conditional) |

> There is **no** `pause-1` phase. The first manual pause (Tailscale auth) runs inline inside phase `0.4`.

## Manual Pauses

### Pause 1 — Tailscale Auth (inside phase 0.4)
The script prints your Tailscale auth URL and waits. Open it in a browser, authenticate, and press Enter when done.

### Pause 2 — Account Creation (phase `pause-2`)
Attach to the worldserver console:
```bash
docker attach ac-worldserver
# Inside the console (no leading dot needed here):
account create <gmname> <password>
account set gmlevel <gmname> 3 -1

account create ahbot <password>
# Detach with Ctrl-P, Ctrl-Q (never Ctrl-C — kills the server)
```
Then re-run the installer to continue past this pause.

### Pause 3 — AH Bot Characters (phase `pause-3`)
1. Log into the WoW client using the `ahbot` account.
2. Create characters on both factions (or whichever the installer prompts for).
3. Log out of the WoW client.
4. Re-run the installer. It will discover the GUIDs automatically and write them to `mod_ahbot.conf`.

## Key Files Created by the Installer

| File | Description |
|------|-------------|
| `/opt/stacks/azerothcore/.env` | DB passwords, Docker image tags (mode 600) |
| `/opt/stacks/azerothcore/docker-compose.override.yml` | All AC_* env vars (source of truth for tuning) |
| `/opt/stacks/azerothcore/docker-compose.admin.yml` | Empty placeholder, populated by admin UI |
| `/opt/stacks/azerothcore/configs/modules/mod_ahbot.conf` | Only file edited post-install (GUIDs) |
| `/opt/stacks/azerothcore/configs/mysql/custom.cnf` | MySQL tuning (`innodb_buffer_pool_size` etc.) |
| `~/.azerothcore-install-config` | Persisted prompt answers (shredded on success) |
| `~/.azerothcore-install-state` | Current phase checkpoint |

## Configuration Captured at Install Time

The installer asks these questions up front and persists answers:
- Server XP rate (x1, x2, x3, x5, x10, x15, x20, or custom per-category rates)
- Playerbot count (default: 250)
- PvP enabled/disabled
- Map update threads
- GM account name and password
- AH bot account name and password
- Whether to install systemd auto-start
- Whether to configure UFW firewall

## Adopting an Existing Install

`--adopt` verifies existing stack state before marking phases complete. It does NOT blindly mark state as done. If verification fails it aborts. This is intentional — do not add a `--force-adopt` bypass.

## Troubleshooting Installation

**Build fails (Phase 3):** Docker build can take 30-60+ minutes. Check `/tmp/azerothcore-install-<ts>.log` for the actual error. Clang warnings during compilation are normal (benign upstream warnings from mod-playerbots).

**Phase checkpoint out of sync:** Delete `~/.azerothcore-install-state` and use `--resume-from=<last_good_phase>` to reset the checkpoint to a known-good phase.

**Config needs re-entering:** The config file `~/.azerothcore-install-config` can be edited manually if a value needs changing before resuming.

**Log file location:** Starts at `/tmp/azerothcore-install-<ts>.log`, relocated to `/opt/stacks/azerothcore/logs/` once that directory exists.
