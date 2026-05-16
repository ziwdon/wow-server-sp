# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A single-file bash installer for AzerothCore (WoW 3.3.5a private server) + mod-playerbots + mod-ah-bot-plus, running on Docker. The stack installs under `/opt/stacks/azerothcore/`.

**Target environment:**
- Ubuntu 22.04 LTS (CLI), Ryzen 5 7430U, 16 GB RAM, 512 GB SSD
- Networking: Tailscale required for all WoW clients — no public IP, no router port forwarding, no direct-LAN client path in this revision
- Use case: private play with ~2 human players and a few hundred playerbots

## Linting

```bash
shellcheck scripts/*.sh
```

Suppress directives (`# shellcheck disable=SC1091`) are used only for dynamic `source` calls where the sourced file is not statically discoverable.

## Scripts

```bash
# Run as your normal user (NOT with sudo — scripts call sudo internally)
chmod +x scripts/*.sh

./scripts/install-azerothcore.sh                     # fresh install
./scripts/install-azerothcore.sh --resume-from=2.5   # resume from a phase
./scripts/install-azerothcore.sh --force-fresh        # wipe state and restart
./scripts/install-azerothcore.sh --adopt              # adopt an existing install
./scripts/install-azerothcore.sh --help               # list phases

./scripts/verify-azerothcore.sh                      # post-install verification (exits 0=pass, 1=fail)
./scripts/uninstall-azerothcore.sh --dry-run         # preview cleanup
./scripts/uninstall-azerothcore.sh                   # full teardown
```

## Architecture

**`scripts/install-azerothcore.sh`** — the core of this repo. Key design points:

- `set -euo pipefail` with a `trap on_error ERR` that prints phase/line and the resume command
- Phase-based checkpointing: state written to `~/.azerothcore-install-state`; the `PHASES` array at the top defines execution order and is used for `--resume-from` index comparisons
- Configuration captured up front via interactive prompts; persisted to `~/.azerothcore-install-config` (mode 600) and sourced on resume; shredded on success
- Three manual pauses built in: Tailscale auth, GM/AHBOT account creation via `docker attach ac-worldserver`, and AH bot character creation in the WoW client
- Logging: starts at `/tmp/azerothcore-install-<ts>.log`, relocated to `/opt/stacks/azerothcore/logs/` once that directory exists; uses `exec > >(tee ...)` to preserve original stdout/stderr for `clean_exit`

**`scripts/verify-azerothcore.sh`** — uses `set -u` (not `-e`) intentionally so every check runs even after a failure. Reports `[OK]`, `[FAIL]`, or `[INFO]` per check; `INFO` lines are advisory and excluded from pass/fail totals.

**`scripts/uninstall-azerothcore.sh`** — uses `docker compose -p azerothcore down` plus explicit named-container cleanup to avoid touching unrelated Docker containers (no `--remove-orphans`).

**`.claude/skills/`** — reserved for future Claude Code skill files. `references/` subdirectory is for reference material (phases, config options, error patterns) that skills will link to.

## Install phases

Defined in the `PHASES` array in `install-azerothcore.sh`:

`0.0` pre-flight → `0.1` OS check → `0.2` apt packages → `0.3` Docker → `0.4` Tailscale → `0.5` dirs → `1` git clone → `2.1–2.6` config/compose → `3` Docker build → `3.1` module conf templates → `4` first run + DB init → `pause-2` account creation → `5` realmlist → `5.1` UFW → `pause-3` AH bot chars → `6.1.4` write GUIDs → `6.1.5` worldserver restart → `7` backup cron → `8` systemd

## Key paths (post-install)

| Path | Purpose |
|------|---------|
| `/opt/stacks/azerothcore/` | Stack root |
| `/opt/stacks/azerothcore/.env` | DB credentials, image tags (do not publish) |
| `/opt/stacks/azerothcore/configs/modules/` | `mod_ahbot.conf`, `playerbots.conf` |
| `/opt/stacks/azerothcore/configs/mysql/custom.cnf` | MySQL tuning (`innodb_buffer_pool_size` needs db restart) |
| `~/.azerothcore-install-state` | Phase checkpoint file |
| `~/.azerothcore-install-config` | Persisted prompt answers (shredded on success) |

## Non-obvious internal conventions

**Upstream fork:** Phase 1 clones `mod-playerbots/azerothcore-wotlk` on branch `Playerbot` (not the canonical `azerothcore/azerothcore-wotlk`). The mod-playerbots and mod-ah-bot-plus modules are cloned as subdirectories of that repo.

**`set_conf_key` vs `require_conf_key_once`:** `set_conf_key` removes _all_ existing (commented or not) occurrences of a key and appends one canonical line — used when writing values to avoid the AzerothCore duplicate-key warning. `require_conf_key_once` only validates that exactly one occurrence exists with the expected value; it never modifies the file.

**`clean_exit` vs `exit`:** `clean_exit` disarms the `ERR` trap before exiting. Use it for graceful aborts (e.g., "user must take action and re-run") so no error banner is printed. Plain `exit` or a failing command prints the error banner via `on_error`.

**`xp_rate_values` field order:** The space-delimited string emitted is always `quest kill explore money reputation skill_discovery item_normal item_uncommon`. The `read -r` destructuring in `insert_xp_rate_overrides_into_compose` depends on this order.

**`save_config` GUID preservation:** `save_config` always rewrites `~/.azerothcore-install-config` from scratch, but it appends `AHBOT_GUIDS` at the end if the variable is non-empty. This preserves GUIDs across config rewrites that occur after Pause 3 (e.g., retrying an earlier phase).

**`compose_scale_args` empty-array guard:** This helper returns no output (not even a newline) when there are no services to scale down. Callers use `mapfile -t` to capture the args; if the helper printed a blank line, `mapfile` would produce a one-element empty array that breaks `docker compose`.

## Constraints to preserve

- Scripts must **not** be run as root. The root guard (`EUID -eq 0`) at the top of `install-azerothcore.sh` is intentional.
- Password inputs are restricted to shell-safe characters (`letters, numbers, . _ @ % + = , : -`) so the config file can be safely sourced on resume.
- The uninstall script must **not** use `--remove-orphans` — it would risk removing unrelated containers sharing the Compose project name.
- `verify-azerothcore.sh` must stay on `set -u` without `-e` so all checks run regardless of individual failures.
