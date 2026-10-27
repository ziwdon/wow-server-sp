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

# Explicitly permit capacity warnings in noninteractive automation:
./scripts/install-azerothcore.sh --allow-capacity-warnings

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
| `1` | Clone AzerothCore (mod-playerbots fork) + mod-playerbots + mod-ah-bot-plus + mod-individual-progression, **pin each checkout** to `scripts/upstream-versions.env`, then apply `scripts/patches/` (`apply-local-patches.sh`) |
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
| `5` | Networking — Tailscale realmlist (+ `localAddress`/`localSubnetMask` for the optional LAN IP) |
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
- Server XP rate (x1, x3, x5, or x7)
- Playerbot count (default: 1500)
- PvP enabled/disabled
- Map update threads
- GM account name and password
- AH bot account name and password
- Whether to install systemd auto-start
- Whether to configure UFW firewall

## Capacity Preflight

Phase `0.0` measures free space on `/opt` and physical RAM. It fails below 25 GiB free on `/opt`, warns from 25 GiB through less than 50 GiB, and has no disk warning at 50 GiB or more. It also warns when the selected InnoDB buffer pool is greater than 50% of physical RAM.

Interactive installs must acknowledge each warning. Noninteractive installs fail for either warning unless `--allow-capacity-warnings` is supplied explicitly. This flag is intentionally limited to capacity warnings; use it only after reviewing the target host's storage and RAM.

## Adopting an Existing Install

`--adopt` verifies existing stack state before marking phases complete. It does NOT blindly mark state as done. If verification fails it aborts. This is intentional — do not add a `--force-adopt` bypass.

## Troubleshooting Installation

**Build fails (Phase 3):** Docker build can take 30-60+ minutes. Check `/tmp/azerothcore-install-<ts>.log` for the actual error. Clang warnings during compilation are normal (benign upstream warnings from mod-playerbots).

**Phase checkpoint out of sync:** Delete `~/.azerothcore-install-state` and use `--resume-from=<last_good_phase>` to reset the checkpoint to a known-good phase. On a server that is already running, never pick phase `1` or earlier (see "Pinned Upstream Revisions and Upgrades" below).

**Config needs re-entering:** The config file `~/.azerothcore-install-config` can be edited manually if a value needs changing before resuming.

**Log file location:** Starts at `/tmp/azerothcore-install-<ts>.log`, relocated to `/opt/stacks/azerothcore/logs/` once that directory exists.

## Pinned Upstream Revisions and Upgrades

**Pins.** `scripts/upstream-versions.env` is the only place upstream revisions are recorded: URL, branch and a full 40-character commit SHA for the core (`mod-playerbots/azerothcore-wotlk`, branch `Playerbot`) and for mod-playerbots, mod-ah-bot-plus and mod-individual-progression, plus the client data version (`CLIENT_DATA_VERSION`, currently `v20.0`; ~1.2 GB download in Phase 4). Phase 1 checks every repo out at its pin, the verifier (Checks 24 and 26) compares the live checkouts and client data to it, and `upgrade-azerothcore.sh` is the only thing that changes it.

**Local patches.** `scripts/patches/<module>/*.patch` are applied to `modules/<module>` by `scripts/apply-local-patches.sh SOURCE_ROOT [--check|--reverse]` (idempotent). Current patch: `mod-playerbots/0001-mark-only-logging-out-character-offline.patch` — without it, any logout marks *every* character on the account offline, so dismissing one alt-bot flags the human player offline for up to 15 minutes, which breaks the admin's presence tracker, Players page and online counts. If a new upstream revision breaks the patch the script says "refresh the patch for this upstream revision"; fix the patch, do not skip it.

**Never re-run the installer from Phase 1 or earlier (`--resume-from`/`--force-from` ≤ 1) on a live server that is not already at the pins.** It moves the checkouts under the running containers, and the worldserver applies mod-playerbots' DB updates from the host-mounted `./modules` checkout — new SQL against the old binary can abort the boot. Resuming from Phase 2 or later is fine.

**Upgrades.** Only `scripts/upgrade-azerothcore.sh` changes upstream versions on a live stack: `preflight` (read-only checks) → `stage` (build a staging checkout, candidate images and client data while the server keeps running) → `rehearse` (boot the candidate against a restored backup on an isolated network) → `cutover` (the one disruptive step: cold `preupgrade` backup, switch, boot, verify) → `finalize` (delete rollback artifacts) or `rollback` (return to the exact pre-cutover state). Usage: `cutover --previous-repo-rev REV [--pin AC_K=V]... [--yes] [--state DIR] [--skip-rehearsal] [--allow-online-players]`; rehearse with the same `--pin` set (the cutover accepts only a rehearsal of the staged build with identical pins). A failed cutover stops the realm and prints how to resume (`cutover --state DIR`) or roll back; a resume refuses changed pins/patches, a realm restarted since the stop (e.g. a reboot) and the 02:30–03:45 UTC window. After go-live, `rollback` first saves the upgraded databases (`prerollback` backup, never pruned) and configs to `~/azerothcore-upgrade/cutover-<ts>/pre-rollback/`; if that backup fails it stops with nothing overwritten (`--no-safety-backup` skips it knowingly). If only the previous verifier fails after the stack is restored, re-run `rollback --state DIR` to verify again (nothing is restored twice) or close it with `--accept-verify-failure`. Work files live in `~/azerothcore-upgrade/`. `redeploy-azerothcore.sh` is **not** an upgrade tool (it rebuilds only the worldserver). `cutover` and `rollback` stop and restart the server — run them only when the operator asks.

**Env-var rename.** `scripts/migrate-ac-env.sh` rewrites `AC_AI_PLAYERBOT_*` to `AC_PLAYERBOTS_*` in `docker-compose.override.yml` and `docker-compose.admin.yml` (originals are copied to `--backup-dir` first; `--pin AC_K=V` adds pinned values to the admin overlay; `--dry-run` shows the diff). A fresh install already uses the new names.

**Verifier expectations.** Until the cutover, `verify-azerothcore.sh` is *expected* to FAIL Check 8 (the override lacks the new `AC_PLAYERBOTS_*` names), Check 11's legacy-key test (`AiPlayerbot.*` in `playerbots.conf`), Check 12 (managed names not in the worldserver env), Check 24 (checkouts not at the pins / patch not applied) and Check 26 (client data not `v20.0`) on a server installed before the pins existed, and Check 15 until the new `backup.sh` is deployed. Check 25 only looks at the current boot and passes on a self-consistent old boot. `redeploy-azerothcore-admin.sh` verifies its candidate (via the AC verifier) and rolls back on failure, so admin redeploy is blocked until the cutover; the admin installer is not gated (it just prints a "Verify with:" hint). Check 25 inspects only the current boot's `docker logs` for `Missing property` (stale `.conf` files or unmapped keys) and `built with generator` (mmaps from the wrong client-data generation — these go to `Server.log`/console, **not** `Errors.log`).
