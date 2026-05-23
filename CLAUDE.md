# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Two sibling sub-projects:

- **`scripts/`** — a single-file bash installer for AzerothCore (WoW 3.3.5a private server) + mod-playerbots + mod-ah-bot-plus + mod-individual-progression, running on Docker. The AC stack installs under `/opt/stacks/azerothcore/`.
- **`wow-server-sp-admin/`** — a FastAPI + HTMX web admin for monitoring and editing the running AC server. Installs to `/opt/stacks/azerothcore-admin/` (separate stack dir so its lifecycle never disturbs AC's). Every config edit becomes an `AC_*` env var in `docker-compose.admin.yml`, the LAST-precedence Compose layer. Design spec: `docs/superpowers/specs/2026-05-20-wow-server-sp-admin-design.md` (authoritative — read it before touching admin code).

**Target environment:**
- Ubuntu 22.04 LTS (CLI) is the recommended target. Ubuntu 24.04 is detected and allowed only after explicit user confirmation — treat it as "possible, maybe" rather than supported. Hardware target: Ryzen 5 7430U, 16 GB RAM, 512 GB SSD
- Networking: Tailscale required for all WoW clients — no public IP, no router port forwarding, no direct-LAN client path in this revision
- Use case: private play with ~2 human players and a few hundred playerbots

## Linting

```bash
shellcheck scripts/*.sh wow-server-sp-admin/scripts/*.sh
```

The admin app's Python tests run under Docker (no local venv needed):

```bash
docker run --rm -v "$(pwd)/wow-server-sp-admin:/src" -w /src python:3.12-slim \
    bash -c "pip install -r requirements-dev.txt -q && python -m pytest -q"
```

Suppress directives (`# shellcheck disable=SC1091`) are used only for dynamic `source` calls where the sourced file is not statically discoverable.

A handful of warnings reported by shellcheck are intentional and must not be "fixed":

- **SC2016 on `escape_regex_metachars`** (around `install-azerothcore.sh:775`): the `sed 's/[.[\*^$()+?{}|]/\\&/g'` MUST use single quotes — `\&` is sed's back-reference for the matched character. Switching to double quotes would let the shell eat the backslash and break escaping for any input containing those metacharacters.
- **SC2001 multi-line prefixing via `sed 's/^/    - /'`:** bash parameter expansion cannot cleanly add a per-line prefix to a multi-line string; sed is the right tool here.
- **SC2012 on the three `ls modules/mod-…/ | head -10` lines** (around `install-azerothcore.sh:2414-2416`): informational stdout only, against directories with plain alphanumeric filenames.

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

./wow-server-sp-admin/scripts/install-azerothcore-admin.sh         # install/upgrade the admin stack
./wow-server-sp-admin/scripts/verify-azerothcore-admin.sh          # post-install admin verification
./wow-server-sp-admin/scripts/uninstall-azerothcore-admin.sh       # remove admin stack only (not AC)
./wow-server-sp-admin/scripts/uninstall-azerothcore-admin.sh --dry-run
./wow-server-sp-admin/scripts/uninstall-azerothcore-admin.sh --yes
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
| `/opt/stacks/azerothcore/logs/Server.log` | Worldserver boot/init log — chatty during startup, then mostly silent. Runtime traffic goes to `Playerbots.log` and `docker logs ac-worldserver` instead. Contains benign init-time noise — see below |
| `/opt/stacks/azerothcore/logs/Playerbots.log` | mod-playerbots verbose action log (chatty; contains benign action-retry "FAILED" lines and the periodic `Random Bots Stats:` block — see below) |
| `docker logs ac-worldserver` | Live worldserver stdout. Authoritative for the periodic `Random Bots Stats:` / `Bots status:` block; this is *not* in `Server.log` |
| `/opt/stacks/azerothcore/backups/` | Nightly `mysqldump`s + config tarball + git-revisions snapshot |
| `~/.azerothcore-install-state` | Phase checkpoint file |
| `~/.azerothcore-install-config` | Persisted prompt answers (shredded on success) |
| `/opt/stacks/azerothcore/docker-compose.admin.yml` | Admin-authored Compose overlay (LAST precedence, after `docker-compose.override.yml`). Created empty by the admin installer; populated only via the admin UI's Apply flow. The AC installer never reads or writes it. |
| `/opt/stacks/azerothcore-admin/` | Admin stack root (separate from AC's so admin can manage AC without being affected by AC restarts) |
| `/opt/stacks/azerothcore-admin/.env` | Admin runtime: `TAILSCALE_IP`, `ADMIN_PORT`, `HOST_UID`, `HOST_GID`, `DOCKER_GID` (mode 600) |
| `/opt/stacks/azerothcore-admin/snapshots/` | `admin.yml.bak.<unix-ts>` snapshots written before every Apply/Rollback (mounted into the admin container as `/admin-snapshots/`). Lives here, NOT next to `admin.yml`, because `/ac/`'s parent is ro inside the admin container — a sibling-file snapshot would hit EROFS. GC'd to 7-day retention on admin app boot. |
| `docker logs azerothcore-admin` | Admin app's JSON-formatted stdout |

## Non-obvious internal conventions

**Upstream fork:** Phase 1 clones `mod-playerbots/azerothcore-wotlk` on branch `Playerbot` (not the canonical `azerothcore/azerothcore-wotlk`). The mod-playerbots and mod-ah-bot-plus modules are cloned as subdirectories of that repo.

**`AC_*` env vars in docker-compose:** AzerothCore derives env-var names from config keys by prefixing `AC_`, replacing dots/spaces/hyphens with underscores, inserting underscores at lowercase-to-uppercase and letter-to-number boundaries, then uppercasing. So `AiPlayerbot.Enabled` becomes `AC_AI_PLAYERBOT_ENABLED`, `Respawn.DynamicRateGameObject` becomes `AC_RESPAWN_DYNAMIC_RATE_GAME_OBJECT`, and `SkillGain.Crafting` becomes `AC_SKILL_GAIN_CRAFTING`. **Unknown env vars are silently ignored** — when adding a new `AC_*` line, verify the target key exists in the relevant `.conf.dist` under `docs/configs/` or it will be dead weight. The Phase 4 rename-detection check (`verify_managed_env_vars_bound_in_worldserver`) catches this silent-drop case at install time by confirming each managed `AC_*` is present in `ac-worldserver` and maps to a loaded config key. `Server.log` binding lines are useful evidence, but are not authoritative because AzerothCore only logs env bindings when the env value differs from the loaded `.conf` value.

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

Rename-detection: the install script (Phase 4, via `verify_managed_env_vars_bound_in_worldserver`) and `verify-azerothcore.sh` (Check 12) confirm every managed `AC_*` is present in the running `ac-worldserver` environment and maps, using AzerothCore's real Config.cpp conversion rule, to a key in the loaded `worldserver.conf` or module `.conf` files. This catches the silent-no-op failure mode where an upstream rename or env-var typo invalidates an `AC_*` derivation. The Phase 2.5 verification grep array, the Phase 2.6 `for var in …` list, the install helper's `managed_vars` array, verify Check 12's `managed_vars` array, and `OVERRIDE_EXPECTED` in `verify-azerothcore.sh` must stay in sync when adding/removing an `AC_*` line. `AC_PLAYERBOTS_DATABASE_INFO` is intentionally excluded from the mapping check because it is a connection string verified indirectly by successful `acore_playerbots` access; the XP-rate vars are conditionally included only when `SERVER_XP_RATE != "x1"`.

**`playerbots.conf` is seeded but never edited by the installer.** Phase 3.1's `for dist in configs/modules/*.conf.dist; do cp …` loop (`install-azerothcore.sh:2975`) copies any module `.conf.dist` to `.conf` if missing, then never touches it again. The file's content is **not** the source of truth for any playerbot setting — env vars in `docker-compose.override.yml` win at config-read time. If you find stale `set_conf_key`-written values in a pre-existing `playerbots.conf` on a long-running install, they are cosmetic only. `verify-azerothcore.sh`'s Check 11 only asserts the file exists; it deliberately does not assert content.

## Known-benign log noise

These patterns appear in a healthy install and should not be chased as bugs. When auditing logs after an install/verify, filter them out before drawing conclusions. The canonical signal for "is anything actually broken?" is `Errors.log` size — if it's 0 bytes, no real runtime errors.

**Install log (`install-<ts>.log`), Phase 3 build only:**
- Hundreds of clang `-Wsign-compare`, `-Wdeprecated-copy-with-user-provided-copy`, `-Wimplicit-const-int-float-conversion`, and `"N warnings generated"` lines from `modules/mod-playerbots/**/*.cpp` and core AzerothCore sources. These come from upstream code compiled with `-DWITH_WARNINGS=ON`. The build still succeeds; do not "fix" them in this repo.

**`Server.log` (worldserver):**
- `mysql: [Warning] Using a password on the command line interface can be insecure.` — emitted every time the install/backup/verify scripts shell out to `mysql` with `-p`. Expected; the scripts deliberately pass passwords this way for non-interactive use.
- `Can't set process priority class, error: Permission denied` — worldserver tries to raise its scheduling priority inside the container without `CAP_SYS_NICE`. Cosmetic; do not add the capability just to silence it.
- `MoveSplineInitArgs::Validate: expression 'velocity > 0.01f' failed for GUID … Type: Creature Entry: …` — upstream world-DB data quirk where a handful of creatures have zero-velocity spline data. Cosmetic.
- `>> The file 'YYYY_MM_DD_NN.sql' was applied to the database, but is missing in your update directory now!` — high-volume (~2500+ lines per boot, mostly from the World DB) DBUpdater message emitted at startup for every previously-applied SQL update file not present under `data/sql/archive/db_<name>/`. Each DB still concludes `>> <Name> database is up-to-date!` — these are informational, not errors. The upstream-intended fix is shipping populated `archive/db_*` directories, which this build does not.
- **A frozen `Server.log` mtime after the `WORLD: World Initialized` line is normal**, not a sign of a hung worldserver. AC's `Server` log appender writes boot/init output and goes quiet — runtime traffic is routed to `Playerbots.log` and the worldserver's stdout. Check `Errors.log` size and `docker logs --tail 20 ac-worldserver` instead before suspecting a stall.

**`Playerbots.log` (mod-playerbots) and `docker logs ac-worldserver`:**
- `<BotName> A:<action> - FAILED` (e.g., `A:follow - FAILED`, `A:add gathering loot - FAILED`, `A:reset botAI - FAILED`) and `Can cast spell failed. No spellid. - spellid: 0, bot name: <BotName>` — this module logs every action-tick that wasn't applicable in the bot's current state. High volume is normal; these are retry/inapplicability traces, not errors.
- `Random teleporting bot <Name> (level N) to Map: … (i/k locations)` — normal `RandomBot` relocation driven by mod-playerbots' periodic re-distribution, not an error despite the verbose tone.
- `Random Bots Stats: 0 online` with all of `Active/Moving/In flight/In combat/...: 0` is the **expected steady state when no real player is logged in.** `AC_AI_PLAYERBOT_DISABLED_WITHOUT_REAL_PLAYER=1` (set in `docker-compose.override.yml`) gates the random-bot login engine on real-player presence — characters are not deleted, just kept `online=0` in `acore_characters.characters`. The pool (250 `RNDBOT*` accounts × 10 chars = ~2500 characters, split 200 RNDbot / 50 AddClass per `acore_playerbots.playerbots_account_type`) persists across restarts; graceful worldserver shutdown sets every character `online=0`. As soon as a real player connects, the engine ramps the active bot count toward `AC_AI_PLAYERBOT_MIN_RANDOM_BOTS`. To confirm pool integrity from the host: `docker exec -i ac-database mysql -uroot -p"$DOCKER_DB_ROOT_PASSWORD" -e "SELECT COUNT(*) FROM acore_characters.characters c JOIN acore_auth.account a ON a.id=c.account WHERE a.username LIKE 'RNDBOT%'"` should return ~2500.

## Non-obvious admin app conventions

These are the cross-cutting invariants of `wow-server-sp-admin/` you need to know before touching admin code OR before changing anything in `scripts/install-azerothcore.sh` that the admin reads (env-var derivation, mount layout, config paths). Internal-only details (snapshot semantics, runner internals, SSE plumbing) live in the admin's design spec.

**Admin's only writes inside `/opt/stacks/azerothcore/` are `docker-compose.admin.yml` and `backups/`.** The whole `/ac/` mount is ro except those two rw sub-mounts. The admin **never** edits `docker-compose.override.yml`, AC's `.env` (at runtime — the *installer* does once), any `.conf` file, MySQL `custom.cnf`, or anything under `data/`. The single source of truth for installer-shipped defaults remains the AC installer's Phase 2.5 heredoc.

**One installer-time touch outside the admin stack dir:** `wow-server-sp-admin/scripts/install-azerothcore-admin.sh` appends `docker-compose.admin.yml` to AC's `.env` `COMPOSE_FILE=` line (idempotent, preserves existing entries). If you ever regenerate AC's `.env` from scratch via the AC installer, that entry is lost — re-run the admin installer to put it back.

**`docker-compose.admin.yml` is the LAST-precedence Compose layer.** Compose merges in `COMPOSE_FILE` order; an `AC_*` in admin.yml overrides the same key in override.yml. The same silent-drop trap CLAUDE.md documents above applies: an `AC_*` whose name doesn't reverse-map to a loaded config key is ignored without warning. The admin's Apply flow runs the same two-part check `install-azerothcore.sh:verify_managed_env_vars_bound_in_worldserver` does (presence in `docker exec ac-worldserver env` + reverse-map to a key in the loaded `.conf` files). The Python port of `config_key_to_ac_env_var` in `wow-server-sp-admin/app/services/env_var.py` is golden-file-tested against the bash helper over all ~1874 keys in the four `.conf.dist` files; both must agree exactly or the silent-drop check produces false negatives.

**`AuctionHouseBot.GUIDs` is in admin's `BLOCKED_KEYS`.** Same reason CLAUDE.md notes it's the only key the AC installer still writes to a `.conf` file: it's installer-managed and runtime-discovered after Pause 3. The admin refuses to write it server-side regardless of what the client sends. Don't add it as an admin-editable key.

**Admin container needs `group_add: ["${DOCKER_GID}"]`.** It runs as a non-root user (`HOST_UID:HOST_GID`) so writes to `docker-compose.admin.yml` have the installer user's ownership. But that user has no access to `/var/run/docker.sock` by default — every Docker SDK call dies with `PermissionError(13)`. The admin installer resolves the host's docker-group GID via `getent group docker` and writes `DOCKER_GID` to the admin's `.env`; `docker-compose.yml` mounts it via `group_add`. Do not remove this — the container is broken without it.

**`admin.yml` writes are in place (open+truncate+write), NOT tmp+rename.** The file is a bind-mount source inode; `rename(2)` over it fails with EBUSY. Crash safety is via the snapshot-before-write invariant (snapshots in `/admin-snapshots/`, GC'd at 7-day retention on app boot). Both the snapshot and the write are covered by the action runner's single-flight lock via the `pre` hook so no concurrent apply can interleave a half-written file and no orphaned write can occur without a paired restart.

**`Server.log` wait is truncate-aware.** AC's `Appender.Server=…,Server.log,w` (mode `w`) opens the log fresh at each boot, so the new worldserver truncates the file. The admin's `_wait_for_world_init` baselines `last_size` to the file's current size at function entry and resets to `0` on a detected size drop — otherwise a stale prior-boot "World Initialized" line trips a false positive on Restart before the new worldserver has even opened the log. If you ever change the Server.log appender mode in the worldserver config, revisit this routine.

**Admin's Stop does NOT use `server shutdown N`.** AC's SIGTERM handler is `World::StopNow`, which immediately collapses any in-progress `server shutdown N` countdown — and `docker stop` sends SIGTERM as its first action. The admin holds the grace window itself with `time.sleep`, sends `announce`/`notify`/`saveall` over `docker attach` stdin (detach bytes `\x10\x11` = Ctrl-P, Ctrl-Q), then issues `docker stop --time 60 ac-worldserver` (the 60 s is not arbitrary — AC's clean-shutdown saveall can stretch to 30-45 s under bot-heavy load, and 60 s avoids a Docker-initiated SIGKILL mid-save). The admin then polls for `Status=exited` with its own 120 s budget separate from Docker's `--time`.

**Admin console attach must use a raw PTY, not `subprocess.PIPE`.** The AC worldserver container is created with `Tty=true` and `OpenStdin=true`. From the non-interactive admin container, `docker attach` with a pipe fails immediately with `cannot attach stdin to a TTY-enabled container because stdin is not a terminal`, which breaks both Dashboard Restart and Settings Apply (Apply writes `admin.yml`, then runs the same Restart path). `wow-server-sp-admin/app/services/console.py` therefore opens a pseudo-terminal, puts the slave side in raw mode, passes the slave FD as Docker's stdin, writes commands and `\x10\x11` detach bytes through the master FD, waits briefly for Docker to consume the detach sequence, then closes the PTY. In raw-mode clean detach, Docker CLI may exit with status 1 and stderr `read escape sequence`; that is Docker acknowledging the detach sequence, not a console failure.

**Admin's in-process backup mirrors `backup.sh` but does NOT invoke it.** `backup.sh` hardcodes `STACK_DIR=/opt/stacks/azerothcore` and writes via the host filesystem — neither works from inside the admin container's mount layout. `wow-server-sp-admin/app/services/backup_runner.py` re-implements the four `docker exec ac-database mysqldump` calls + the `tar -czf` of `.env`/`docker-compose.override.yml`/`configs/` + `git-revisions-<date>.txt` in Python. Filenames match `backup.sh`'s on-disk format exactly (`<db>-<date>.sql`, `azerothcore-config-<date>.tar.gz`, `git-revisions-<date>.txt`) so the host's nightly cron rotation (`find … -mtime +7 -delete`) handles admin-emitted backups identically.

**Expected admin test warnings.** The Dockerized pytest command may print pip's "running as root" warning and a new-pip-version notice because tests run inside a disposable `python:3.12-slim` container; those are harmless. Current tests also emit a Starlette `multipart` pending-deprecation warning and a `TemplateResponse` call-style deprecation warning. The latter is worth cleaning up before a future FastAPI/Starlette upgrade, but neither warning is related to the admin restart/apply console path.

**Both uninstallers must NOT use `--remove-orphans`.** Same rule as the AC uninstaller — `--remove-orphans` would remove unrelated containers sharing the Compose project name. The admin's `docker rm -f azerothcore-admin` already covers the one service.

**The top nav bar lives in `base.html`, not `dashboard.html`.** `#last-refresh` and `#nav-status-pill` are `<span>` elements inside `<nav class="topnav">` in `base.html`. The status pill polls `/api/status` every 60 s via `hx-get`/`hx-swap="none"` and a `htmx:afterRequest` JS handler that parses the response and updates the pill's text and CSS class — **not** `hx-swap="outerHTML"` or `hx-select`. Using `outerHTML` would destroy the polling element after the first swap; `hx-swap="none"` keeps it alive. The `htmx:afterSwap` listener in the same script block updates `#last-refresh` only when the dashboard's `#status` stat card swaps (i.e. only on the dashboard page — the nav pill handles its own polling independently on every page).

**`switchLog()` is defined in `dashboard.html`'s inline `<script>`, not `settings.js`.** `settings.js` is only loaded on the settings page. Log tab switching (`onclick="switchLog(this, 'server-log')"`) in `partials/logs.html` calls `switchLog` from that inline script. Do not move the function to `settings.js` — it would be undefined when the logs partial renders on the dashboard.

**`settings.js` source-file checkbox selector is `.check-group input[type=checkbox][value]`.** The sidebar class changed from `.settings-filters` to `.settings-sidebar` / `.check-group` in the UI overhaul. If you add new sidebar checkboxes that should trigger a re-render, put them inside `.check-group` or wire them up separately — the current selector only catches `.check-group` children.

**Settings page defaults to "Show only modified" on load.** The `only-modified` checkbox carries `checked` in `settings.html`, so `_render()` runs with `modifiedOnly = true` on the first paint — only keys with `source === 'admin'` or `source === 'installer'` are shown. There is no "Show all keys" toggle and no `COMMON_KEYS` curated list; unchecking "Show only modified" shows the full ~1874-key index. When the filter is active and no modified keys exist, the list renders a single `.empty-state` paragraph instead of going blank — do not interpret an empty list on a fresh install as a rendering bug. The pending-count badge and Apply button are updated before the empty-state early return so they always reflect the true pending state regardless of filter output.

**SSE activity log requires the canonical htmx-ext-sse pattern — `sse-connect` on a parent, `sse-swap` on the child.** `htmx-ext-sse`'s `registerSSE()` locates the EventSource via `getClosestMatch(elt, hasEventSource)`, an ancestor walk. When `sse-connect` and `sse-swap` are on the same element the walk still finds the EventSource (stored on the element itself), but this creates a fragile ordering dependency and is NOT the supported pattern. In `dashboard.html` the canonical structure is: `.panel-body` carries `hx-ext="sse" sse-connect="/api/action/stream"`, and `#action-log` (`<ul>`) carries `sse-swap="progress,done" hx-swap="beforeend"`.

**`GZipMiddleware` must not compress `/api/action/stream`.** Browsers send `Accept-Encoding: gzip` with every `EventSource` request. Starlette's `GZipMiddleware` sees no `Content-Length` on the streaming SSE response and compresses it; some browsers silently drop all events from a gzip-encoded SSE stream, producing a permanently empty activity log. `main.py` uses `_GZipExcludeSSE`, a minimal ASGI wrapper that bypasses GZip for `/api/action/stream` and delegates everything else to the real `GZipMiddleware`. Do not replace this with a plain `GZipMiddleware` call.

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
| `docs/superpowers/specs/2026-05-20-wow-server-sp-admin-design.md` | Authoritative design spec for `wow-server-sp-admin/` — read this before touching admin code, especially before changing the action runner, the apply/rollback flow, the post-apply verification, the snapshot/write semantics, or the mount layout |
| `docs/superpowers/specs/2026-05-22-admin-ui-overhaul-design.md` | UI/UX overhaul spec (WoW Classic palette, nav bar, stat cards, settings layout) — read this before touching `app/templates/`, `app/static/app.css`, or `app/static/settings.js` |

When making changes to config keys, reviewing module behaviour, or writing install logic, read the relevant wiki pages and the `.conf.dist` file rather than guessing defaults.

Check `docs/superpowers/plans/` and `docs/superpowers/specs/` before starting non-trivial architectural work — in-flight designs and partially-executed plans live there and may already cover the task, or constrain how it should be approached.

## Constraints to preserve

- Scripts must **not** be run as root. The root guard (`EUID -eq 0`) at the top of `install-azerothcore.sh` is intentional. Same rule applies to `install-azerothcore-admin.sh`.
- Password inputs are restricted to shell-safe characters (`letters, numbers, . _ @ % + = , : -`) so the config file can be safely sourced on resume.
- Neither uninstall script (`uninstall-azerothcore.sh`, `uninstall-azerothcore-admin.sh`) may use `--remove-orphans` — it would risk removing unrelated containers sharing the Compose project name.
- `verify-azerothcore.sh` and `verify-azerothcore-admin.sh` must stay on `set -u` without `-e` so all checks run regardless of individual failures.
- The admin app must **not** edit any file under `/opt/stacks/azerothcore/` other than `docker-compose.admin.yml` and `backups/*`. The whole-stack `/ac/` mount is ro in the admin container precisely to enforce this; do not add rw sub-mounts for any other path.
