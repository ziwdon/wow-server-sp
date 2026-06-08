# AzerothCore Single Player Server + Web Admin

A Docker-based AzerothCore installer for solo or small-group play, with AI bots to fill the world, an active auction house, and per-character expansion progression. Includes a web admin UI for managing and tuning the server.

Clients connect over [Tailscale](https://tailscale.com) — a personal VPN that lets you reach the server without exposing any port to the internet.

## What's in the box

**Installer** (`scripts/`)
- Single bash script — handles everything from Docker setup to first server boot
- Phase-based checkpointing: auto-resumes after failures, reboots, or SSH disconnects

**Mods**
- [`mod-playerbots`](https://github.com/liyunfan1223/mod-playerbots) — fill the world with AI players that level, quest, trade, and group
- [`mod-ah-bot-plus`](https://github.com/ZhengPeiRu21/mod-ah-bot-plus) — keep the auction house stocked and buying
- [`mod-individual-progression`](https://github.com/ZhengPeiRu21/mod-individual-progression) — per-character expansion gating (Vanilla → TBC → WotLK)

**Web admin** (`wow-server-sp-admin/`)
- Dashboard — live server status, player/bot counts, uptime, log viewer, and Start / Stop / Restart with a live SSE progress log
- Players — who is online, with per-character stats
- Stats — top characters by level, kills, and playtime
- Settings — browse, edit, and apply any of the ~1 800 server config keys; live apply + rollback
- Backups — trigger manual backups, browse archives, and restore from any snapshot
- Progression — per-character expansion control (Classic → TBC → WotLK) with icon picker and confirmation flow

**AI assistant skill** (`skills/wow-server-sp-gamemaster/`)
- Installable skill that gives an AI assistant deep knowledge of this stack
- Covers GM commands, playerbot control, raid strategies, AH bot, individual progression, admin app, and troubleshooting

## Prerequisites

- Ubuntu 22.04 LTS, ~50 GB free under `/opt`, `sudo` rights
- Internet access (apt, GitHub, Docker Hub, Tailscale)
- A [Tailscale](https://tailscale.com) account

Missing packages, Docker, and Tailscale are installed automatically by the script.

## Install

```bash
git clone https://github.com/ziwdon/wow-server-sp ~/wow-server-sp
cd ~/wow-server-sp
chmod +x scripts/*.sh
./scripts/install-azerothcore.sh
```

Run as your normal user — **do not use `sudo`**. The script calls `sudo` internally where needed.

> **Heads up:** the install includes 3 manual pauses — Tailscale authentication, account creation via the worldserver console, and AH bot character setup. The script guides you through each one.

## Admin

Install the web admin after the server is running:

```bash
./wow-server-sp-admin/scripts/install-azerothcore-admin.sh
```

Then open `http://<tailscale-ip>:8765` in a browser.

Verify it's working:

```bash
./wow-server-sp-admin/scripts/verify-azerothcore-admin.sh
```

## Operations

| Command | What it does |
|---|---|
| `./scripts/install-azerothcore.sh` | Auto-resume from last checkpoint |
| `./scripts/install-azerothcore.sh --resume-from=<phase>` | Re-run from a specific phase |
| `./scripts/install-azerothcore.sh --adopt` | Adopt an existing install with no state file |
| `./scripts/install-azerothcore.sh --force-fresh` | Wipe state and start over (asks for confirmation) |
| `./scripts/install-azerothcore.sh --help` | List all phases |
| `./scripts/verify-azerothcore.sh` | Post-install health check (exits 0 = pass) |
| `./scripts/uninstall-azerothcore.sh --dry-run` | Preview what uninstall would remove |
| `./scripts/uninstall-azerothcore.sh` | Full teardown |

## Configuration

AzerothCore reads settings from its `.conf` files, but the installer uses `AC_*` environment variables in `docker-compose.override.yml` to override them — no editing config files directly. This covers everything from XP rates and bot counts to game type, respawn rates, and mail delay.

The easiest way to change settings after install is the admin's **Settings** page: search for any config key, edit its value, and Apply — the admin writes the override and restarts the server automatically.

For manual edits, add or change `AC_*` lines under `ac-worldserver.environment:` in `/opt/stacks/azerothcore/docker-compose.override.yml`, then restart the worldserver. See `CLAUDE.md` for the full reference, the key-to-env-var conversion rule, and the MySQL tuning options.
