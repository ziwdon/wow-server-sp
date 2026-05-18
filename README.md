# AzerothCore + Playerbots + AH Bot Plus + Individual Progression — Docker Installer

Bash installer for a private, home-server AzerothCore (WoW 3.3.5a) stack on Docker, bundling:

- `mod-playerbots` — fill the world with AI players
- `mod-ah-bot-plus` — keep the auction house populated
- `mod-individual-progression` — per-character expansion progression (Vanilla → TBC → WotLK)

Target setup:

- Ubuntu 22.04 LTS (CLI). Ubuntu 24.04 is detected and only allowed after confirmation
- Installed under `/opt/stacks/azerothcore/`
- Private play, small number of human players, a few hundred playerbots
- WoW clients connect over Tailscale only — no public IP, no port forwarding

## Prerequisites

- Ubuntu 22.04 LTS, ~50 GB free under `/opt`, `sudo` rights
- Internet access for apt, GitHub, Docker Hub, and Tailscale
- A Tailscale account you can authenticate in a browser

Anything missing (apt packages, Docker, Tailscale) is installed by the script.

## Quick start

```bash
git clone https://github.com/ziwdon/wow-server-sp ~/azerothcore-install
cd ~/azerothcore-install
chmod +x scripts/*.sh
./scripts/install-azerothcore.sh
```

Run as your normal user. **Do not use `sudo`** — the script calls `sudo` itself where needed. Running as root produces wrong ownership, wrong `$HOME`, wrong crontab, and broken Docker UID/GID.

## What the installer prompts for

Answers are persisted to `~/.azerothcore-install-config` (mode `600`) so the install can resume, and shredded on success.

- DB root password (or Enter to auto-generate)
- GM account username and password
- AHBOT account password
- Random playerbot count (applied to both MIN and MAX)
- Server XP/progression rate (`x1`, `x3`, `x5`, `x7`)
- InnoDB buffer pool size (`1G`–`32G`)
- Map update threads (1–16)
- AH bot character count (1 or 2)
- Whether to install/enable UFW
- Whether to enable systemd auto-start

Manual passwords are restricted to shell-safe characters: `letters, numbers, . _ @ % + = , : -`. This is so the saved config can be safely sourced on resume.

## Manual pauses

The installer stops three times for steps that can't be automated.

**1. Tailscale auth (Phase 0.4)** — `sudo tailscale up` runs and prints a URL. Open it in a browser, authenticate, return to the terminal. The script polls for a Tailscale IPv4 and continues.

**2. Account creation** — after first server start and DB init, attach to the worldserver from a second terminal:

```bash
docker attach ac-worldserver
```

Run the account commands the installer prints, then detach with `Ctrl+P` then `Ctrl+Q`. **Do not use `Ctrl+C`** — that stops the container. Real passwords are kept out of the install log; the terminal still shows them during this step.

**3. AH bot character creation** — log in with the `AHBOT` account in the WoW 3.3.5a client, create the configured number of characters, log out. The installer detects their GUIDs and writes them into `mod_ahbot.conf`.

## What it changes outside the stack directory

Most files live under `/opt/stacks/azerothcore/`. The installer also touches:

- apt packages (installs what's missing)
- Docker (installs if missing, adds your user to the `docker` group)
- Tailscale (installs and authenticates if missing)
- `~/.azerothcore-install-state` (phase checkpoint)
- `~/.azerothcore-install-config` (prompt answers, shredded on success)
- `/tmp/ac-build.log` and `/tmp/azerothcore-install-*.log` (relocated/cleaned later)
- A backup cron entry
- UFW rules (only if opted in)
- `/etc/systemd/system/azerothcore.service` (only if opted in)

## Resume after failure or interruption

```bash
./scripts/install-azerothcore.sh                     # auto-resume from last checkpoint
./scripts/install-azerothcore.sh --resume-from=2.5   # force re-run from a phase
./scripts/install-azerothcore.sh --force-from=2.5    # alias of --resume-from
./scripts/install-azerothcore.sh --help              # list phases
```

Use this after a failure, reboot, SSH disconnect, or after logging out/in for Docker group membership to take effect.

## Adopt an existing install

If the stack directory exists but the state file is missing:

```bash
./scripts/install-azerothcore.sh --adopt
```

Adopt mode verifies the install before marking phases complete, and aborts if checks fail.

## Wipe and start over

```bash
./scripts/install-azerothcore.sh --force-fresh
```

Removes the installer state, the stack directory, and the saved config. Asks for explicit `WIPE` confirmation. Use this when you just want to restart the install flow; use `uninstall-azerothcore.sh` for a full teardown.

## Verify

```bash
./scripts/verify-azerothcore.sh
```

Exits `0` on pass, `1` on failure. Checks containers, databases, MySQL tuning, realmlist vs Tailscale IP, image tags, AH bot GUIDs, playerbots config, backup script + cron, and (if opted in) the systemd unit.

## Uninstall

```bash
./scripts/uninstall-azerothcore.sh --dry-run   # preview
./scripts/uninstall-azerothcore.sh             # run
./scripts/uninstall-azerothcore.sh --yes       # skip confirmation
```

Removes the stack directory, installer state and config, matching backup cron lines, the optional systemd unit, known AzerothCore containers (`ac-database`, `ac-authserver`, `ac-worldserver`, `ac-db-import`, `ac-client-data-init`), and known temp installer files under `/tmp`.

Does **not** remove Docker, Tailscale, UFW, apt packages it installed, unrelated containers/images, unrelated cron/firewall rules, or your Docker group membership. Compose cleanup is project-scoped (`docker compose -p azerothcore down`); `--remove-orphans` is intentionally avoided so unrelated containers can't be removed by mistake.

## Key paths

| Path | Purpose |
|------|---------|
| `/opt/stacks/azerothcore/` | Stack root |
| `/opt/stacks/azerothcore/.env` | DB credentials and image tags — do not publish |
| `/opt/stacks/azerothcore/configs/modules/` | `mod_ahbot.conf`, `playerbots.conf`, `individualProgression.conf` |
| `/opt/stacks/azerothcore/configs/mysql/custom.cnf` | MySQL tuning |
| `/opt/stacks/azerothcore/logs/install-<ts>.log` | Main install log |
| `/opt/stacks/azerothcore/logs/backup.log` | Backup history |
| `/tmp/ac-build.log` | Docker build log (safe to delete after install) |

## Post-install tuning

All edits happen on the host; the containers see them via bind mounts.

**AH bot** — edit `configs/modules/mod_ahbot.conf`, then in-game as GM: `.ahbot reload`. No worldserver restart needed for simple tweaks.

**Playerbots** — edit `configs/modules/playerbots.conf`, then:

```bash
cd /opt/stacks/azerothcore && docker compose restart ac-worldserver
```

**Individual Progression** — edit `configs/modules/individualProgression.conf`, then restart the worldserver as above. See `docs/wikis/mod-individual-progression-wiki/` for tier descriptions and changes.

**MySQL** — edit `configs/mysql/custom.cnf`, then:

```bash
cd /opt/stacks/azerothcore && docker compose restart ac-database
```

`innodb_buffer_pool_size` only takes effect after a database restart.

## Don't publish these files

Generated runtime files can contain credentials or private network info:

```text
/opt/stacks/azerothcore/.env
/opt/stacks/azerothcore/backups/
/opt/stacks/azerothcore/logs/
~/.azerothcore-install-config
/tmp/azerothcore-install-*.log
/tmp/ac-build.log
/tmp/ac-compose-effective.*.yml
```

The installer redacts the most obvious password output, but review before sharing.
