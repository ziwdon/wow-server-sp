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
- Realm type — PvP (default) or PvE/Normal
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

## Installer-applied configuration

Every setting the installer writes is listed below. **Prompted** entries show the prompt default; the user can change them during an interactive install. **Fixed** entries are always written with the value shown. **Derived** means computed from a prompted value, not settable independently.

Post-install changes: worldserver settings live in `docker-compose.override.yml`; module configs live in `configs/modules/`; MySQL tuning lives in `configs/mysql/custom.cnf`. See [Post-install tuning](#post-install-tuning) below.

### Worldserver (`docker-compose.override.yml`)

| `AC_*` variable | Value | Source |
|---|---|---|
| `AC_GAME_TYPE` | `1` (PvP) / `0` (PvE) | Prompted — default PvP |
| `AC_AI_PLAYERBOT_ENABLED` | `1` | Fixed |
| `AC_AI_PLAYERBOT_RANDOM_BOT_AUTOLOGIN` | `1` | Fixed |
| `AC_AI_PLAYERBOT_MIN_RANDOM_BOTS` | `1000` | Prompted |
| `AC_AI_PLAYERBOT_MAX_RANDOM_BOTS` | `1000` | Prompted |
| `AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES` | `1` | Fixed |
| `AC_MAP_UPDATE_THREADS` | `4` | Prompted |
| `AC_MAP_UPDATE_INTERVAL` | `10` | Fixed |
| `AC_MIN_WORLD_UPDATE_TIME` | `1` | Fixed |
| `AC_PRELOAD_ALL_NON_INSTANCED_MAP_GRIDS` | `0` | Fixed |
| `AC_DONT_CACHE_RANDOM_MOVEMENT_PATHS` | `0` | Fixed |
| `AC_QUESTS_IGNORE_AUTO_ACCEPT` | `1` | Fixed |
| `AC_PLAYER_LIMIT` | `0` (unlimited) | Fixed |
| `AC_LEAVE_GROUP_ON_LOGOUT_ENABLED` | `1` | Fixed |
| `AC_ALLOW_TWO_SIDE_INTERACTION_AUCTION` | `1` | Fixed |
| `AC_ALLOW_TWO_SIDE_INTERACTION_CHAT` | `1` | Fixed |
| `AC_ALLOW_TWO_SIDE_INTERACTION_CALENDAR` | `0` | Fixed |
| `AC_ALLOW_TWO_SIDE_INTERACTION_CHANNEL` | `0` | Fixed |
| `AC_ALLOW_TWO_SIDE_INTERACTION_GROUP` | `0` | Fixed |
| `AC_ALLOW_TWO_SIDE_INTERACTION_GUILD` | `0` | Fixed |
| `AC_ALLOW_TWO_SIDE_INTERACTION_ARENA` | `0` | Fixed |
| `AC_MAIL_DELIVERY_DELAY` | `10` (seconds) | Fixed |
| `AC_CHAR_DELETE_METHOD` | `1` (soft-delete) | Fixed |
| `AC_RESPAWN_DYNAMIC_RATE_CREATURE` | `10` | Fixed |
| `AC_RESPAWN_DYNAMIC_RATE_GAMEOBJECT` | `20` | Fixed |
| `AC_UPDATES_ENABLE_DATABASES` | `7` | Fixed |
| `AC_ENABLE_PLAYER_SETTINGS` | `1` | Fixed |
| `AC_AUCTION_HOUSE_BOT_ENABLE_SELLER` | `true` | Fixed |
| `AC_AUCTION_HOUSE_BOT_BUYER_ENABLED` | `true` | Fixed |

XP/progression rate overrides are written by the rate prompt — see the table below.

### XP rates

Prompted during install (default: `x5`). Choosing `x1` writes no rate overrides; all others set the following keys in `docker-compose.override.yml`:

| `AC_*` variable | x1 | x3 | x5 | x7 |
|---|---|---|---|---|
| `AC_RATE_XP_QUEST` | 1 | 3 | 5 | 7 |
| `AC_RATE_XP_KILL` | 1 | 3 | 3 | 5 |
| `AC_RATE_XP_EXPLORE` | 1 | 3 | 3 | 5 |
| `AC_RATE_DROP_MONEY` | 1 | 2 | 3 | 3 |
| `AC_RATE_REPUTATION_GAIN` | 1 | 3 | 5 | 7 |
| `AC_RATE_SKILL_DISCOVERY` | 1 | 2 | 3 | 3 |
| `AC_RATE_DROP_ITEM_NORMAL` | 1 | 1 | 1 | 1.5 |
| `AC_RATE_DROP_ITEM_UNCOMMON` | 1 | 1 | 1 | 1.5 |
| `AC_SKILLGAIN_CRAFTING` | 1 | 2 | 3 | 5 |
| `AC_SKILLGAIN_GATHERING` | 1 | 2 | 3 | 5 |
| `AC_SKILLGAIN_WEAPON` | 1 | 3 | 5 | 7 |
| `AC_SKILLGAIN_DEFENSE` | 1 | 3 | 5 | 7 |

### mod-playerbots (`configs/modules/playerbots.conf`)

| Key | Value | Source |
|---|---|---|
| `AiPlayerbot.MinRandomBots` | `1000` | Prompted (mirrors worldserver) |
| `AiPlayerbot.MaxRandomBots` | `1000` | Prompted (mirrors worldserver) |
| `AiPlayerbot.BotActiveAlone` | `0` | Fixed |
| `AiPlayerbot.botActiveAloneSmartScale` | `1` | Fixed |
| `AiPlayerbot.botActiveAloneSmartScaleWhenMinLevel` | `1` | Fixed |
| `AiPlayerbot.botActiveAloneSmartScaleWhenMaxLevel` | `80` | Fixed |
| `AiPlayerbot.DisabledWithoutRealPlayer` | `1` | Fixed |
| `AiPlayerbot.EnablePeriodicOnlineOffline` | `1` | Fixed |
| `AiPlayerbot.PeriodicOnlineOfflineRatio` | `2.0` | Fixed |
| `AiPlayerbot.BotActiveAloneForceWhenInRadius` | `150` | Fixed |
| `AiPlayerbot.BotActiveAloneForceWhenInZone` | `1` | Fixed |
| `AiPlayerbot.BotActiveAloneForceWhenInMap` | `0` | Fixed |
| `AiPlayerbot.BotActiveAloneForceWhenIsFriend` | `1` | Fixed |
| `AiPlayerbot.BotActiveAloneForceWhenInGuild` | `0` | Fixed |
| `PlayerbotsDatabase.WorkerThreads` | `1` | Fixed |
| `PlayerbotsDatabase.SynchThreads` | `2` | Fixed |

### mod-ah-bot-plus (`configs/modules/mod_ahbot.conf`)

| Key | Value | Source |
|---|---|---|
| `AuctionHouseBot.GUIDs` | characters created at Pause 3 | Manual (in-game step) |
| `AuctionHouseBot.EnableSeller` | `true` | Fixed |
| `AuctionHouseBot.Buyer.Enabled` | `true` | Fixed |

The installer prompts for how many AH bot characters to create (1 or 2, default 1); the actual GUID values come from the in-game character creation step at Pause 3.

### mod-individual-progression (`configs/modules/individualProgression.conf`)

The installer copies `individualProgression.conf.dist` to `individualProgression.conf` but writes no key overrides — all settings remain at upstream defaults. The module is activated by `AC_UPDATES_ENABLE_DATABASES = 7` and `AC_ENABLE_PLAYER_SETTINGS = 1` in `docker-compose.override.yml` (both fixed — see the Worldserver table above).

See `docs/configs/individualProgression.conf.dist` and `docs/wikis/mod-individual-progression-wiki/` for available settings.

### MySQL (`configs/mysql/custom.cnf`)

| Key | Value | Source |
|---|---|---|
| `innodb_buffer_pool_size` | `6G` | Prompted |
| `innodb_buffer_pool_instances` | `= size in GB` | Derived |
| `innodb_io_capacity` | `500` | Fixed |
| `innodb_io_capacity_max` | `2500` | Fixed |
| `innodb_use_fdatasync` | `ON` | Fixed |
| `innodb_log_buffer_size` | `32M` | Fixed |
| `innodb_flush_log_at_trx_commit` | `2` | Fixed |

`innodb_buffer_pool_instances` is computed as `buffer_pool_size / 1G` (e.g., `6G` → `6`). Changing `innodb_buffer_pool_size` requires a database restart to take effect.

## Post-install tuning

All edits happen on the host; the containers see them via bind mounts.

**Worldserver** — `worldserver.conf` is baked into the image and is **not** bind-mounted, so you don't edit it on the host. Override its values via environment variables in `docker-compose.override.yml` under `ac-worldserver.environment:` — this is exactly the mechanism the installer already uses for every worldserver setting it touches.

Env vars beat `.conf` values at startup (see `docs/wikis/azerothcore-wiki/docs/config-overrides-with-env-var.md`). The entrypoint matches `AC_*` vars to conf keys by stripping `AC_`, lowercasing, and dropping non-alphanumerics — so `AC_GAME_TYPE` writes to `GameType`, `AC_RATE_XP_QUEST` writes to `Rate.XP.Quest`, etc. **Unknown vars are silently ignored**, so verify the target key exists in `docs/configs/worldserver.conf.dist` (the upstream defaults reference) before adding a new one.

```bash
cd /opt/stacks/azerothcore
# edit docker-compose.override.yml — add or change an AC_* line under ac-worldserver
docker compose restart ac-worldserver
```

For ad-hoc in-game testing without a restart, log in as GM and run `.reload config`. Not all settings honor reload (some are read once at startup, some apply only to new objects/maps), and an `AC_*` override in compose will always win on next restart — use the override file for anything you want to keep.

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

## Restart and shutdown safely

The worldserver should always be told to save state before it goes down. The canonical pattern (see `docs/wikis/azerothcore-wiki/docs/exitcodes.md` and `docs/wikis/azerothcore-wiki/docs/gm-commands.md`) is to drive shutdown from the worldserver console, not from `docker compose restart`:

```bash
docker attach ac-worldserver
```

Then at the worldserver prompt:

```text
saveall              # write all character data to the DB
server restart 30    # warn players, kick gracefully after 30s, exit code 2
```

Detach with `Ctrl+P` then `Ctrl+Q` — **never `Ctrl+C`**, which kills the container.

The upstream `docker-compose.yml` sets `restart: unless-stopped` on `ac-worldserver`, so when worldserver exits cleanly Docker brings it back automatically. No `docker` command needed for a restart.

Variants of the in-game restart command:

- `server idlerestart 30` — same kick, but only fires if no players are connected. Useful for scheduled maintenance.
- `server restart cancel` — abort an in-flight countdown.

### Shut down and keep it down

Don't use `server shutdown` for this — it exits cleanly, but `unless-stopped` will bring the container right back up. Instead, save first and let Docker drive the stop:

```bash
docker attach ac-worldserver
```

At the worldserver prompt:
```text
notify Server going down for maintenance — please log out.
saveall
```

Detach with `Ctrl+P` then `Ctrl+Q`, then:
```bash
cd /opt/stacks/azerothcore && docker compose stop ac-worldserver
```

`docker compose stop` marks the container as user-stopped (which `unless-stopped` honors), sends `SIGTERM`, and the worldserver's signal handler flushes a save before exiting. Connected players are simply disconnected — there's no in-game countdown — so this is the right tool for "no humans online" maintenance.

To bring it back later:
```bash
cd /opt/stacks/azerothcore && docker compose start ac-worldserver
```

If you really need to bypass the in-game flow entirely (worldserver unresponsive, can't attach, etc.), `docker compose restart ac-worldserver` sends `SIGTERM` and the same signal handler flushes a save — but in-game players get no warning. Prefer the `saveall` + `server restart` path whenever the console is reachable.
