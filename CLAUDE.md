# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A single-file bash installer for AzerothCore (WoW 3.3.5a private server) + mod-playerbots + mod-ah-bot-plus + mod-individual-progression, running on Docker. The stack installs under `/opt/stacks/azerothcore/`.

**Target environment:**
- Ubuntu 22.04 LTS (CLI) is the recommended target. Ubuntu 24.04 is detected and allowed only after explicit user confirmation — treat it as "possible, maybe" rather than supported. Hardware target: Ryzen 5 7430U, 16 GB RAM, 512 GB SSD
- Networking: Tailscale required for all WoW clients — no public IP, no router port forwarding, no direct-LAN client path in this revision
- Use case: private play with ~2 human players and a few hundred playerbots

## Linting

```bash
shellcheck scripts/*.sh
```

Suppress directives (`# shellcheck disable=SC1091`) are used only for dynamic `source` calls where the sourced file is not statically discoverable.

A handful of warnings reported by shellcheck are intentional and must not be "fixed":

- **SC2016 on `escape_regex_metachars`** (around `install-azerothcore.sh:775`): the `sed 's/[.[\*^$()+?{}|]/\\&/g'` MUST use single quotes — `\&` is sed's back-reference for the matched character. Switching to double quotes would let the shell eat the backslash and break escaping for any input containing those metacharacters.
- **SC2001 multi-line prefixing via `sed 's/^/    - /'`:** bash parameter expansion cannot cleanly add a per-line prefix to a multi-line string; sed is the right tool here.
- **SC2012 on the three `ls modules/mod-…/ | head -10` lines** (around `install-azerothcore.sh:2320-2322`): informational stdout only, against directories with plain alphanumeric filenames.

## Scripts

```bash
# Run as your normal user (NOT with sudo — scripts call sudo internally)
chmod +x scripts/*.sh

./scripts/install-azerothcore.sh                     # fresh install (auto-resumes)
./scripts/install-azerothcore.sh --resume-from=2.5   # resume from a phase
./scripts/install-azerothcore.sh --force-from=2.5    # alias of --resume-from
./scripts/install-azerothcore.sh --force-fresh        # wipe state and restart (requires WIPE confirmation)
./scripts/install-azerothcore.sh --adopt              # adopt an existing install
./scripts/install-azerothcore.sh --help               # list phases

./scripts/verify-azerothcore.sh                      # post-install verification (exits 0=pass, 1=fail)
./scripts/uninstall-azerothcore.sh --dry-run         # preview cleanup
./scripts/uninstall-azerothcore.sh                   # full teardown
./scripts/uninstall-azerothcore.sh --yes             # skip confirmation
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

**`.claude/skills/`** — currently unused; only `references/` exists as a placeholder for reference material (phases, config options, error patterns) that future skill files could link to. No active skills live here yet.

## Install phases

Defined in the `PHASES` array in `install-azerothcore.sh`:

`0.0` pre-flight → `0.1` OS check → `0.2` apt packages → `0.3` Docker → `0.4` Tailscale → `0.5` dirs → `1` git clone (core + mod-playerbots + mod-ah-bot-plus + mod-individual-progression) → `2.1–2.6` config/compose → `3` Docker build → `3.1` module conf templates → `4` first run + DB init → `pause-2` account creation → `5` realmlist → `5.1` UFW → `pause-3` AH bot chars → `6.1.4` write GUIDs → `6.1.5` worldserver restart → `7` backup cron → `8` systemd

Note: there is no `pause-1` phase. The first manual pause (Tailscale auth) runs inline inside phase `0.4`; only the second and third pauses are standalone phases.

## Key paths (post-install)

| Path | Purpose |
|------|---------|
| `/opt/stacks/azerothcore/` | Stack root |
| `/opt/stacks/azerothcore/.env` | DB credentials, image tags (do not publish) |
| `/opt/stacks/azerothcore/configs/modules/` | Module `.conf` files; only `mod_ahbot.conf` GUIDs are installer-managed after Pause 3 |
| `/opt/stacks/azerothcore/configs/mysql/custom.cnf` | MySQL tuning (`innodb_buffer_pool_size` needs db restart) |
| `/opt/stacks/azerothcore/logs/install-<unix-ts>.log` | Full install transcript (relocated from `/tmp/` once `logs/` exists) |
| `/opt/stacks/azerothcore/logs/Errors.log` | AzerothCore's dedicated error channel — **authoritative for runtime errors**; 0 bytes = clean |
| `/opt/stacks/azerothcore/logs/Server.log` | Worldserver stdout/general log (chatty; contains benign warnings — see below) |
| `/opt/stacks/azerothcore/logs/Playerbots.log` | mod-playerbots verbose action log (chatty; contains benign action-retry "FAILED" lines — see below) |
| `/opt/stacks/azerothcore/backups/` | Nightly `mysqldump`s + config tarball + git-revisions snapshot |
| `~/.azerothcore-install-state` | Phase checkpoint file |
| `~/.azerothcore-install-config` | Persisted prompt answers (shredded on success) |

## Non-obvious internal conventions

**Upstream fork:** Phase 1 clones `mod-playerbots/azerothcore-wotlk` on branch `Playerbot` (not the canonical `azerothcore/azerothcore-wotlk`). The mod-playerbots and mod-ah-bot-plus modules are cloned as subdirectories of that repo.

**`AC_*` env vars in docker-compose:** the AzerothCore docker entrypoint translates these by stripping `AC_`, lowercasing, stripping non-alphanumerics, and matching against any conf key in any `.conf` file under the stack's configs dir. So `AC_AI_PLAYERBOT_ENABLED` writes to `AiPlayerbot.Enabled` in `playerbots.conf`, not `worldserver.conf`. **Unknown env vars are silently ignored** — when adding a new `AC_*` line, verify the target key exists in the relevant `.conf.dist` under `docs/configs/` or it will be dead weight. The Phase 4 rename-detection check (`verify_managed_env_vars_bound_in_worldserver`, also mirrored as Check 12 in `verify-azerothcore.sh`) catches this silent-drop case at install time by greping `Server.log` for the worldserver's `> Config: Found config value '…' from environment variable 'AC_…'` binding lines.

**`PLAYERBOT_COUNT` non-interactive seed:** in non-interactive mode the bot count default is read from `$AC_AI_PLAYERBOT_MIN_RANDOM_BOTS` (the real AzerothCore env-var name, deliberately reused so anyone familiar with AC's docker docs can override it without learning a script-local name). Don't rename this to a `PLAYERBOT_*` script-local variable.

**`--adopt` mode:** verifies existing stack state before marking phases complete; it does *not* blindly mark state as done. If verification fails it aborts, which is the desired behavior — don't add a `--force-adopt` bypass without explicit user request.

**`set_conf_key` vs `require_conf_key_once`:** `set_conf_key` removes _all_ existing (commented or not) occurrences of a key and appends one canonical line — used when writing values to avoid the AzerothCore duplicate-key warning. `require_conf_key_once` only validates that exactly one occurrence exists with the expected value; it never modifies the file. After the env-var consolidation, the *only* remaining caller of either helper is the `AuctionHouseBot.GUIDs` write/assert pair in Phase 6.1.4 (`install-azerothcore.sh:3477` and `:3479`). Do not reintroduce `.conf`-side writes for any other key — add a new `AC_*` env var to the Phase 2.5 heredoc instead. Both helpers (plus `escape_regex_metachars`) are kept solely because GUIDs are runtime-discovered after pause-3 and cannot live in the heredoc.

**`clean_exit` vs `exit`:** `clean_exit` disarms the `ERR` trap before exiting. Use it for graceful aborts (e.g., "user must take action and re-run") so no error banner is printed. Plain `exit` or a failing command prints the error banner via `on_error`.

**`xp_rate_values` field order:** The space-delimited string emitted is always `quest kill explore money reputation skill_discovery item_normal item_uncommon skill_crafting skill_gathering skill_weapon skill_defense`. The `read -r` destructuring in `insert_xp_rate_overrides_into_compose` and both verify helpers depends on this order.

**`save_config` GUID preservation:** `save_config` always rewrites `~/.azerothcore-install-config` from scratch, but it appends `AHBOT_GUIDS` at the end if the variable is non-empty. This preserves GUIDs across config rewrites that occur after Pause 3 (e.g., retrying an earlier phase).

**`compose_scale_args` empty-array guard:** This helper returns no output (not even a newline) when there are no services to scale down. Callers use `mapfile -t` to capture the args; if the helper printed a blank line, `mapfile` would produce a one-element empty array that breaks `docker compose`.

**`INNODB_BUFFER_POOL_INSTANCES` is derived, never persisted.** Computed unconditionally as `${INNODB_BUFFER_POOL_SIZE%G}` after both prompt branches converge (right after the `SERVER_XP_RATE` backfill). It is deliberately *not* written to `save_config`/`load_config` — recomputation is the canonical source so a stale config file can never produce a mismatched value. The 1-GB-per-instance rule means each pool instance stays above MySQL's threshold for actually honoring the setting.

**`docker-compose.override.yml` is the single source of truth for AC tuning.** Every static, non-substituted `AC_*` env var in Phase 2.5's heredoc is mirrored verbatim by a verification grep array immediately below the heredoc, and every managed `AC_*` env var is listed in the Phase 2.6 `for var in …` effective-compose check. Prompt-substituted values (`PLAYERBOT_COUNT`, `MAP_UPDATE_THREADS`, `SERVER_PVP`) and XP-rate values have dedicated verification checks. When adding or removing an `AC_*` line, update the heredoc, the Phase 2.5 verification immediately below it, and the Phase 2.6 list as appropriate. Skipping any relevant check means a missing or corrupted override silently passes install instead of failing the phase loudly.

The one exception is `AuctionHouseBot.GUIDs`, written into `configs/modules/mod_ahbot.conf` in Phase 6.1.4 because its value (the comma-separated AH bot character GUIDs) is runtime-discovered after pause-3 and does not belong in a single env var. `EnableSeller` and `Buyer.Enabled` are set via env vars (`AC_AUCTION_HOUSE_BOT_ENABLE_SELLER` / `AC_AUCTION_HOUSE_BOT_BUYER_ENABLED`), not via `set_conf_key` writes.

Rename-detection: the install script (Phase 4, via `verify_managed_env_vars_bound_in_worldserver` at `install-azerothcore.sh:840`) and `verify-azerothcore.sh` (Check 12) both run a check that greps `Server.log` for `> Config: Found config value '…' from environment variable 'AC_…'` lines, asserting that every managed `AC_*` actually bound to a real config key. This catches the silent-no-op failure mode where an upstream rename invalidates an `AC_*` derivation. The two `managed_vars` arrays — one in the install helper, one in verify Check 12 — are independent copies and must be kept in sync by hand when adding/removing an `AC_*` line; same applies to the Phase 2.5 verification grep array, the Phase 2.6 `for var in …` list, and `OVERRIDE_EXPECTED` in verify-azerothcore.sh. `AC_PLAYERBOTS_DATABASE_INFO` is intentionally excluded from the log check because some builds consume it before emitting the standard binding line; the XP-rate vars are conditionally included only when `SERVER_XP_RATE != "x1"`.

**`playerbots.conf` is seeded but never edited by the installer.** Phase 3.1's `for dist in configs/modules/*.conf.dist; do cp …` loop (`install-azerothcore.sh:2975`) copies any module `.conf.dist` to `.conf` if missing, then never touches it again. The file's content is **not** the source of truth for any playerbot setting — env vars in `docker-compose.override.yml` win at config-read time. If you find stale `set_conf_key`-written values in a pre-existing `playerbots.conf` on a long-running install, they are cosmetic only. `verify-azerothcore.sh`'s Check 11 only asserts the file exists; it deliberately does not assert content.

## Known-benign log noise

These patterns appear in a healthy install and should not be chased as bugs. When auditing logs after an install/verify, filter them out before drawing conclusions. The canonical signal for "is anything actually broken?" is `Errors.log` size — if it's 0 bytes, no real runtime errors.

**Install log (`install-<ts>.log`), Phase 3 build only:**
- Hundreds of clang `-Wsign-compare`, `-Wdeprecated-copy-with-user-provided-copy`, `-Wimplicit-const-int-float-conversion`, and `"N warnings generated"` lines from `modules/mod-playerbots/**/*.cpp` and core AzerothCore sources. These come from upstream code compiled with `-DWITH_WARNINGS=ON`. The build still succeeds; do not "fix" them in this repo.

**`Server.log` (worldserver):**
- `mysql: [Warning] Using a password on the command line interface can be insecure.` — emitted every time the install/backup/verify scripts shell out to `mysql` with `-p`. Expected; the scripts deliberately pass passwords this way for non-interactive use.
- `Can't set process priority class, error: Permission denied` — worldserver tries to raise its scheduling priority inside the container without `CAP_SYS_NICE`. Cosmetic; do not add the capability just to silence it.
- `MoveSplineInitArgs::Validate: expression 'velocity > 0.01f' failed for GUID … Type: Creature Entry: …` — upstream world-DB data quirk where a handful of creatures have zero-velocity spline data. Cosmetic.

**`Playerbots.log` (mod-playerbots):**
- `<BotName> A:<action> - FAILED` (e.g., `A:follow - FAILED`, `A:add gathering loot - FAILED`, `A:reset botAI - FAILED`) and `Can cast spell failed. No spellid. - spellid: 0, bot name: <BotName>` — this module logs every action-tick that wasn't applicable in the bot's current state. High volume is normal; these are retry/inapplicability traces, not errors.
- `Random teleporting bot <Name> (level N) to Map: … (i/k locations)` — normal `RandomBot` relocation, not an error despite the verbose tone.

## Reference docs

The `docs/` directory contains offline reference material. Consult it whenever you need to understand configuration options, verify a setting, or figure out how to do something with any of the modules.

| Path | Contents |
|------|---------|
| `docs/configs/worldserver.conf.dist` | Upstream default `worldserver.conf` — authoritative reference for every config key and its default value |
| `docs/configs/playerbots.conf.dist` | Upstream default mod-playerbots config — reference for every playerbot config key and its default value |
| `docs/configs/mod_ahbot.conf.dist` | Upstream default mod-ah-bot config — reference for every AH bot config key and its default value |
| `docs/configs/individualProgression.conf.dist` | Upstream default mod-individual-progression config — reference for progression config keys and defaults |
| `docs/wikis/azerothcore-wiki/docs/` | Full AzerothCore wiki (hundreds of `.md` files) covering installation, DB schema, GM commands, module development, and more |
| `docs/wikis/mod-playerbots-wiki/` | mod-playerbots wiki: Installation Guide, Configuration, Commands, Raid Strategy Guide, Troubleshooting, and more |
| `docs/wikis/mod-individual-progression-wiki/` | mod-individual-progression wiki: installation, progression tiers, list of changes, useful extras |
| `docs/superpowers/plans/` | Implementation plans for in-progress work in this repo |
| `docs/superpowers/specs/` | Design specs for in-progress work in this repo |

When making changes to config keys, reviewing module behaviour, or writing install logic, read the relevant wiki pages and the `.conf.dist` file rather than guessing defaults.

Check `docs/superpowers/plans/` and `docs/superpowers/specs/` before starting non-trivial architectural work — in-flight designs and partially-executed plans live there and may already cover the task, or constrain how it should be approached.

## Constraints to preserve

- Scripts must **not** be run as root. The root guard (`EUID -eq 0`) at the top of `install-azerothcore.sh` is intentional.
- Password inputs are restricted to shell-safe characters (`letters, numbers, . _ @ % + = , : -`) so the config file can be safely sourced on resume.
- The uninstall script must **not** use `--remove-orphans` — it would risk removing unrelated containers sharing the Compose project name.
- `verify-azerothcore.sh` must stay on `set -u` without `-e` so all checks run regardless of individual failures.
