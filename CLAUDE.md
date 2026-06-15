# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## What this repo is

Two sibling sub-projects on Docker:

- **`scripts/`** — single-file bash installer for AzerothCore (WoW 3.3.5a) + mod-playerbots + mod-ah-bot-plus + mod-individual-progression. AC stack installs under `/opt/stacks/azerothcore/`.
- **`wow-server-sp-admin/`** — FastAPI + HTMX web admin for monitoring/editing the running AC server. Installs to `/opt/stacks/azerothcore-admin/` (separate stack dir so its lifecycle never disturbs AC's). Every config edit becomes an `AC_*` env var in `docker-compose.admin.yml`, the LAST-precedence Compose layer. Authoritative design spec: `docs/superpowers/specs/2026-05-20-wow-server-sp-admin-design.md` — read before touching admin code.

**Target environment:** Ubuntu 22.04 LTS (recommended; 24.04 allowed only after explicit user confirmation — "possible, maybe", not supported). Ryzen 5 7430U, 16 GB RAM, 512 GB SSD. Tailscale required for all WoW clients (no public IP / port forwarding / direct-LAN path). Use case: ~2 humans + a few hundred playerbots.

## Linting

```bash
shellcheck scripts/*.sh wow-server-sp-admin/scripts/*.sh
```

Admin Python tests run under Docker (no local venv):

```bash
docker run --rm -v "$(pwd)/wow-server-sp-admin:/src" -w /src python:3.12-slim \
    bash -c "pip install -r requirements-dev.txt -q && python -m pytest -q"
```

`# shellcheck disable=SC1091` is used only for dynamic `source` calls. These warnings are intentional — do not "fix" them:

- **SC2016 on `escape_regex_metachars`** (~`install-azerothcore.sh:775`): `sed 's/[.[\*^$()+?{}|]/\\&/g'` MUST use single quotes — `\&` is sed's back-reference; double quotes would let the shell eat the backslash and break escaping.
- **SC2001 multi-line prefixing via `sed 's/^/    - /'`:** bash parameter expansion can't cleanly prefix each line of a multi-line string.
- **SC2012 on the three `ls modules/mod-…/ | head -10` lines** (~`install-azerothcore.sh:2414-2416`): informational stdout only, plain alphanumeric filenames.

## Scripts

```bash
# Run as your normal user (NOT sudo — scripts call sudo internally)
chmod +x scripts/*.sh

./scripts/install-azerothcore.sh                     # fresh install (auto-resumes)
./scripts/install-azerothcore.sh --resume-from=2.5   # resume from a phase
./scripts/install-azerothcore.sh --force-from=2.5    # alias of --resume-from
./scripts/install-azerothcore.sh --force-fresh       # wipe state and restart (requires WIPE confirmation)
./scripts/install-azerothcore.sh --adopt             # adopt an existing install
./scripts/install-azerothcore.sh --help              # list phases

./scripts/verify-azerothcore.sh                      # post-install verification (0=pass, 1=fail)
./scripts/redeploy-azerothcore.sh                    # recompile + redeploy ONLY ac-worldserver after a source/module edit (preserves .env/override.yml/admin.yml/DBs)
./scripts/uninstall-azerothcore.sh --dry-run         # preview cleanup
./scripts/uninstall-azerothcore.sh                   # full teardown
./scripts/uninstall-azerothcore.sh --yes             # skip confirmation

./wow-server-sp-admin/scripts/install-azerothcore-admin.sh    # install/upgrade the admin stack
./wow-server-sp-admin/scripts/redeploy-azerothcore-admin.sh   # rebuild+restart after code changes (preserves admin.yml, .env, snapshots)
./wow-server-sp-admin/scripts/verify-azerothcore-admin.sh     # post-install admin verification
./wow-server-sp-admin/scripts/uninstall-azerothcore-admin.sh  # remove admin stack only (not AC); also --dry-run, --yes
```

## Architecture

**`scripts/install-azerothcore.sh`** — the core of this repo:
- `set -euo pipefail` + `trap on_error ERR` that prints phase/line and the resume command
- Phase checkpointing: state in `~/.azerothcore-install-state`; the `PHASES` array defines order and drives `--resume-from` index comparisons
- Config captured up front via prompts; persisted to `~/.azerothcore-install-config` (mode 600), sourced on resume, shredded on success
- Three manual pauses: Tailscale auth, GM/AHBOT account creation via `docker attach ac-worldserver`, AH bot character creation in the WoW client
- Logging starts at `/tmp/azerothcore-install-<ts>.log`, relocated to `logs/` once it exists; `exec > >(tee ...)` preserves original stdout/stderr for `clean_exit`

**`scripts/verify-azerothcore.sh`** — `set -u` (not `-e`) so every check runs after a failure. Reports `[OK]`/`[FAIL]`/`[INFO]` per check; INFO is advisory, excluded from pass/fail totals.

**`scripts/redeploy-azerothcore.sh`** — isolated graceful recompile + redeploy of only `ac-worldserver` after a source/module edit: `docker compose build ac-worldserver` (ccache-fast) → graceful stop (clean saveall) → recreate. Never touches `.env`/`override.yml`/`admin.yml`/the DBs. Use this instead of `install-azerothcore.sh --resume-from=3`, which would also run Phase 4 DB-init, the account-creation pauses, networking, etc.

**`scripts/uninstall-azerothcore.sh`** — `docker compose -p azerothcore down` + explicit named-container cleanup; never `--remove-orphans` (see Constraints).

**`clean_exit` vs `exit`:** `clean_exit` disarms the `ERR` trap — use it for graceful aborts (e.g. user-must-act-and-rerun) so no error banner prints. Plain `exit` or a failing command prints the banner via `on_error`.

**`skills/wow-server-sp-gamemaster/`** — the Game Master skill. Source lives here; copy to `~/.claude/skills/wow-server-sp-gamemaster/` to install globally. Invoke via the `Skill` tool (auto-triggers on questions about install, GM commands, playerbots, AH bot, individual progression, admin app, troubleshooting). `SKILL.md` routes each topic to one of 15 `references/` files (install phases, client setup, GM commands, worldserver.conf/`AC_*` env vars, playerbot commands + raid strategies, AH bot, progression tiers, admin app, troubleshooting, SQL queries).

## Install phases

From the `PHASES` array in `install-azerothcore.sh`:

`0.0` pre-flight → `0.1` OS check → `0.2` apt → `0.3` Docker → `0.4` Tailscale → `0.5` dirs → `1` git clone (core + 3 mods) → `2.1–2.6` config/compose → `3` Docker build → `3.1` module conf templates → `4` first run + DB init → `pause-2` account creation → `5` realmlist → `5.1` UFW → `pause-3` AH bot chars → `6.1.4` write GUIDs → `6.1.5` worldserver restart → `7` backup cron → `8` systemd

There is no `pause-1`: the first manual pause (Tailscale auth) runs inline in phase `0.4`; only pauses 2 and 3 are standalone phases.

## Key paths (post-install)

Paths are under the two stack roots `/opt/stacks/azerothcore/` (AC) and `/opt/stacks/azerothcore-admin/` (admin).

| Path | Purpose |
|------|---------|
| `azerothcore/` | AC stack root |
| `azerothcore/.env` | DB credentials, image tags (do not publish) |
| `azerothcore/configs/modules/` | Module `.conf` files; only `mod_ahbot.conf` GUIDs are installer-managed (after Pause 3) |
| `azerothcore/configs/mysql/custom.cnf` | MySQL tuning (`innodb_buffer_pool_size` needs db restart) |
| `azerothcore/logs/install-<ts>.log` | Full install transcript (relocated from `/tmp/`) |
| `azerothcore/logs/Errors.log` | AC's error channel — **authoritative for runtime errors**; 0 bytes = clean |
| `azerothcore/logs/Server.log` | Worldserver boot/init log — chatty at startup then mostly silent; goes quiet after `World Initialized` (normal). Runtime traffic → `Playerbots.log` + `docker logs ac-worldserver`. Benign init noise (see below) |
| `azerothcore/logs/Playerbots.log` | mod-playerbots action log (chatty; benign "FAILED" retries + periodic `Random Bots Stats:` block — see below) |
| `docker logs ac-worldserver` | Live worldserver stdout; authoritative for the periodic `Random Bots Stats:`/`Bots status:` block (not in `Server.log`) |
| `azerothcore/backups/` | Consolidated `azerothcore-backup-<label>-<stamp>.tar.gz` (daily cron, manual, pre-restore) |
| `~/.azerothcore-install-state` | Phase checkpoint |
| `~/.azerothcore-install-config` | Persisted prompt answers (shredded on success) |
| `azerothcore/docker-compose.admin.yml` | Admin-authored Compose overlay (LAST precedence, after `override.yml`). Created empty by the admin installer; populated only via admin UI Apply. AC installer never touches it |
| `azerothcore-admin/` | Admin stack root (separate from AC's) |
| `azerothcore-admin/.env` | Admin runtime: `TAILSCALE_IP`, `ADMIN_PORT`, `HOST_UID`, `HOST_GID`, `DOCKER_GID` (mode 600) |
| `azerothcore-admin/snapshots/` | `admin.yml.bak.<ts>` snapshots before every Apply/Rollback (mounted `/admin-snapshots/`). Here, not next to `admin.yml`, because `/ac/`'s parent is ro in the admin container (sibling-file snapshot → EROFS). 7-day GC on admin boot |
| `azerothcore-admin/data/` | Admin runtime state (mounted `/admin-data`): `maintenance.json` (scheduler config), `maintenance-log.jsonl` (last 20 runs). Created on first write |
| `docker logs azerothcore-admin` | Admin app's JSON stdout |

## Non-obvious internal conventions

**Upstream fork.** Phase 1 clones `mod-playerbots/azerothcore-wotlk` on branch `Playerbot` (not canonical `azerothcore/azerothcore-wotlk`); mod-playerbots and mod-ah-bot-plus are cloned as subdirs of it.

**`AC_*` env var derivation.** AzerothCore derives env-var names from config keys: prefix `AC_`, replace dots/spaces/hyphens with `_`, insert `_` at lowercase→uppercase and letter→number boundaries, uppercase. E.g. `AiPlayerbot.Enabled`→`AC_AI_PLAYERBOT_ENABLED`, `Respawn.DynamicRateGameObject`→`AC_RESPAWN_DYNAMIC_RATE_GAME_OBJECT`, `SkillGain.Crafting`→`AC_SKILL_GAIN_CRAFTING`. **Unknown env vars are silently ignored** — when adding an `AC_*` line, verify the target key exists in the relevant `.conf.dist` under `docs/configs/` or it's dead weight.

**`docker-compose.override.yml` is the single source of truth for AC tuning.** Every static `AC_*` in Phase 2.5's heredoc is mirrored by a verification grep array right below it and listed in the Phase 2.6 `for var in …` effective-compose check. Prompt-substituted values (`PLAYERBOT_COUNT`, `MAP_UPDATE_THREADS`, `SERVER_PVP`) and XP-rate values have dedicated checks. When adding/removing an `AC_*`, update the heredoc, the Phase 2.5 grep array, and the Phase 2.6 list — skipping any lets a missing/corrupted override pass install silently.

**Rename-detection / silent-drop check.** Phase 4 (`verify_managed_env_vars_bound_in_worldserver`) and `verify-azerothcore.sh` (Check 12) confirm every managed `AC_*` is present in the running `ac-worldserver` env and reverse-maps (via AC's real Config.cpp rule) to a key in the loaded `worldserver.conf`/module `.conf` files. This catches an upstream rename or typo that invalidates an `AC_*`. Keep in sync when adding/removing an `AC_*`: the Phase 2.5 grep array, the Phase 2.6 list, the install helper's `managed_vars`, Check 12's `managed_vars`, and `OVERRIDE_EXPECTED` in `verify-azerothcore.sh`. `AC_PLAYERBOTS_DATABASE_INFO` is excluded (a connection string, verified indirectly by `acore_playerbots` access); XP-rate vars are included only when `SERVER_XP_RATE != "x1"`. (`Server.log` binding lines are evidence but not authoritative — AC logs a binding only when the env value differs from the loaded `.conf` value.)

**`AuctionHouseBot.GUIDs` is the one `.conf`-side write.** Written to `configs/modules/mod_ahbot.conf` in Phase 6.1.4 because the comma-separated AH bot GUIDs are runtime-discovered after pause-3 and don't fit one env var. `EnableSeller`/`Buyer.Enabled` use env vars (`AC_AUCTION_HOUSE_BOT_ENABLE_SELLER`/`AC_AUCTION_HOUSE_BOT_BUYER_ENABLED`). Don't add other `.conf`-side writes — add an `AC_*` to the Phase 2.5 heredoc instead.

**`set_conf_key` vs `require_conf_key_once`.** `set_conf_key` removes _all_ existing occurrences of a key and appends one canonical line (avoids AC's duplicate-key warning); `require_conf_key_once` only validates exactly one occurrence exists, never modifies. Their only remaining callers are the `AuctionHouseBot.GUIDs` write/assert pair in Phase 6.1.4 (`install-azerothcore.sh:3477`/`:3479`). They (plus `escape_regex_metachars`) survive solely because GUIDs are runtime-discovered after pause-3.

**`PLAYERBOT_COUNT` non-interactive seed.** In non-interactive mode the bot-count default reads from `$AC_AI_PLAYERBOT_MIN_RANDOM_BOTS` (AC's real env-var name, reused so AC docker docs carry over). Don't rename it to a script-local `PLAYERBOT_*` var.

**`--adopt` mode** verifies existing stack state before marking phases complete and aborts if verification fails (desired). Don't add a `--force-adopt` bypass without explicit request.

**`xp_rate_values` field order** is always `quest kill explore money reputation skill_discovery item_normal item_uncommon skill_crafting skill_gathering skill_weapon skill_defense`. The `read -r` destructuring in `insert_xp_rate_overrides_into_compose` and both verify helpers depends on it.

**`save_config` GUID preservation.** It rewrites `~/.azerothcore-install-config` from scratch but appends `AHBOT_GUIDS` if non-empty, preserving GUIDs across config rewrites after Pause 3 (e.g. retrying an earlier phase).

**`compose_scale_args` empty-array guard.** Returns no output (not even a newline) when nothing needs scaling down. Callers `mapfile -t` the args; a blank line would yield a one-element empty array that breaks `docker compose`.

**`INNODB_BUFFER_POOL_INSTANCES` is derived, never persisted.** Computed as `${INNODB_BUFFER_POOL_SIZE%G}` after both prompt branches converge (right after the `SERVER_XP_RATE` backfill). Deliberately not in `save_config`/`load_config` — recomputation is canonical so a stale config can't desync it. 1 GB per instance keeps each above MySQL's honoring threshold.

**`playerbots.conf` is seeded but never edited by the installer.** Phase 3.1 copies any `*.conf.dist`→`.conf` if missing (`install-azerothcore.sh:2975`), then never touches it. Env vars in `override.yml` win at config-read time, so its content is **not** the source of truth — stale `set_conf_key` values in a long-running install's `playerbots.conf` are cosmetic. Verify Check 11 only asserts the file exists, not its content.

## Known-benign log noise

These appear in a healthy install — don't chase them as bugs. The canonical "is anything broken?" signal is `Errors.log` size: 0 bytes = no real runtime errors.

**Install log, Phase 3 build only:** hundreds of clang `-Wsign-compare`, `-Wdeprecated-copy-with-user-provided-copy`, `-Wimplicit-const-int-float-conversion`, and `"N warnings generated"` lines from `modules/mod-playerbots` and core sources (upstream code built with `-DWITH_WARNINGS=ON`). Build still succeeds; don't "fix" them here.

**`Server.log`:**
- `mysql: [Warning] Using a password on the command line interface can be insecure.` — every time a script shells out to `mysql -p`; deliberate for non-interactive use.
- `Can't set process priority class, error: Permission denied` — worldserver can't raise priority without `CAP_SYS_NICE`. Cosmetic; don't add the cap.
- `MoveSplineInitArgs::Validate: expression 'velocity > 0.01f' failed for GUID …` — upstream world-DB quirk (zero-velocity spline data). Cosmetic.
- `>> The file 'YYYY_MM_DD_NN.sql' was applied … but is missing in your update directory now!` — high-volume (~2500+/boot) DBUpdater message for every applied SQL file absent from `data/sql/archive/db_<name>/`. Each DB still ends `>> <Name> database is up-to-date!` — informational. (Upstream fix is shipping populated `archive/db_*` dirs; this build doesn't.)
- A frozen `Server.log` mtime after `WORLD: World Initialized` is normal — the Server appender goes quiet; runtime traffic routes elsewhere. Check `Errors.log` size and `docker logs --tail 20 ac-worldserver` before suspecting a stall.

**`Playerbots.log` / `docker logs ac-worldserver`:**
- `<Bot> A:<action> - FAILED` (e.g. `A:follow`, `A:add gathering loot`, `A:reset botAI`) and `Can cast spell failed. No spellid. - spellid: 0, bot name: <Bot>` — the module logs every inapplicable action-tick (retry/inapplicability traces). High volume is normal; not errors.
- `Random teleporting bot <Name> (level N) to Map: … (i/k locations)` — normal `RandomBot` periodic re-distribution.
- `Random Bots Stats: 0 online` (Active/Moving/In flight/In combat all 0) is the **expected steady state with no real player logged in.** `AC_AI_PLAYERBOT_DISABLED_WITHOUT_REAL_PLAYER=1` (in `override.yml`) gates the random-bot login engine on real-player presence — characters aren't deleted, just kept `online=0`. The pool (250 `RNDBOT*` accounts × 10 chars ≈ 2500 characters, split 200 RNDbot / 50 AddClass per `acore_playerbots.playerbots_account_type`) persists across restarts; graceful shutdown sets all `online=0`. A real player ramps active bots toward `AC_AI_PLAYERBOT_MIN_RANDOM_BOTS`. Verify pool integrity: `docker exec -i ac-database mysql -uroot -p"$DOCKER_DB_ROOT_PASSWORD" -e "SELECT COUNT(*) FROM acore_characters.characters c JOIN acore_auth.account a ON a.id=c.account WHERE a.username LIKE 'RNDBOT%'"` ≈ 2500.

**`Errors.log` — `Table `graveyard_zone` incomplete: Zone <id> Team <0|1> does not have a linked graveyard`** (`sql.sql` channel): an upstream **data gap**, not corruption. AC ships no `graveyard_zone` link for a few zones a real player never dies in — notably **2037 Quel'thalas** (map 0) and **3455 The North Sea** (map 530). Playerbots roam/teleport/drown there, so it surfaces only after hours of uptime (one line per death). Benign — `GetClosestGraveyard` (`GameGraveyard.cpp:168`) falls back to the default GY (Westfall/Crossroads) — but it breaks the "0 bytes = clean" invariant. Fix = add a neutral (`Faction=0`) `graveyard_zone` row per zone pointing at the nearest existing graveyard, then `reload graveyard_zone` in the console (no restart). This server applied it live (1448→2037, 922→3455); it is **not** baked into the installer, so a fresh world import reintroduces the noise. Exact SQL + revert: gamemaster `ref-troubleshooting.md`.

## Non-obvious admin app conventions

Invariants of `wow-server-sp-admin/` to know before touching admin code — or before changing anything in `install-azerothcore.sh` the admin reads (env-var derivation, mounts, config paths). Internal details (snapshots, runner, SSE) live in the design spec.

**Admin's only writes inside `/opt/stacks/azerothcore/` are `docker-compose.admin.yml` and `backups/`.** The whole `/ac/` mount is ro except those two rw sub-mounts. The admin never edits `override.yml`, AC's `.env` (the installer does, once), any `.conf`, MySQL `custom.cnf`, or `data/`. In-app Restore writes DBs via `docker exec ac-database` and restores `docker-compose.admin.yml` only; host-side configs are restored by `scripts/restore-azerothcore.sh` (fresh-machine recovery). Installer-shipped defaults remain owned by the Phase 2.5 heredoc.

**One installer-time touch outside the admin stack dir:** `install-azerothcore-admin.sh` appends `docker-compose.admin.yml` to AC's `.env` `COMPOSE_FILE=` line (idempotent). Regenerating AC's `.env` via the AC installer loses it — re-run the admin installer to restore.

**`docker-compose.admin.yml` is the LAST-precedence Compose layer** (merges last in `COMPOSE_FILE` order, overriding the same key in `override.yml`). Same silent-drop trap applies. The Apply flow runs the same two-part check as `install-azerothcore.sh:verify_managed_env_vars_bound_in_worldserver` (presence in `docker exec ac-worldserver env` + reverse-map to a loaded `.conf` key). `config_key_to_ac_env_var` in `wow-server-sp-admin/app/services/env_var.py` is golden-file-tested against the bash helper over all ~1874 keys in the four `.conf.dist` files — both must agree exactly or the check yields false negatives.

**`AuctionHouseBot.GUIDs` is in admin's `BLOCKED_KEYS`** — installer-managed and runtime-discovered, so the admin refuses to write it server-side regardless of the client. Don't make it admin-editable.

**Progression page: internal key `"vanilla"` displays as `"Classic"`.** `EXPANSION_LABELS`/`EXPANSION_ICONS`/`TARGET_STATES` and all JS in `partials/progression_page.html` use `"vanilla"`; icon file is `classic.png`; UI label is `"Classic"`. Don't rename the internal key — it flows through DB queries, the service, and template JS.

**Progression data is embedded once, not fetched separately.** `api_progression_characters` serialises all character rows to `rows_json` injected into a `<script type="application/json" id="progression-char-data">` tag; client JS builds the dropdown and list from that blob. No second API call, no Refresh button by design; `hx-trigger="load"` fires once.

**Progression apply writes `acore_characters.character_queststatus_rewarded`.** mod-individual-progression gates content via hidden rewarded quests 66001–66013 (QUEST_BASE+1 … QUEST_BASE+target_state). The service INSERTs missing rows in a `SELECT … FOR UPDATE` transaction. Downgrade is blocked server-side (`ValueError`) and in the UI (`state-downgrade`, `pointer-events: none`).

**Admin container needs `group_add: ["${DOCKER_GID}"]`.** It runs as non-root (`HOST_UID:HOST_GID`) so its writes own correctly, but that user can't reach `/var/run/docker.sock` by default — every Docker SDK call would `PermissionError(13)`. The installer resolves the host docker-group GID (`getent group docker`) into `.env`; `docker-compose.yml` mounts it via `group_add`. Don't remove it.

**`admin.yml` writes are in-place (open+truncate), not tmp+rename** — it's a bind-mount source inode, so `rename(2)` over it fails with EBUSY. Crash safety = snapshot-before-write (snapshots in `/admin-snapshots/`, 7-day GC on boot); both snapshot and write run under the action runner's single-flight lock (`pre` hook). By contrast `maintenance.json` is a regular file in `ADMIN_DATA_DIR`, not a bind-mount source, so it uses atomic tmp+rename (`os.replace`).

**`Server.log` wait is truncate-aware.** AC's `Appender.Server=…,Server.log,w` opens fresh each boot, truncating the file. `_wait_for_world_init` baselines `last_size` at entry and resets to `0` on a size drop — else a stale prior-boot "World Initialized" line trips a false positive on Restart. Revisit if you change the Server.log appender mode.

**Admin's Stop does NOT use `server shutdown N`.** AC's SIGTERM handler (`World::StopNow`) instantly collapses any in-progress countdown, and `docker stop` sends SIGTERM first. So the admin holds the grace window itself (`time.sleep`), sends `announce`/`notify`/`saveall` over `docker attach` stdin (detach bytes `\x10\x11`), then `docker stop --time 60 ac-worldserver` (60 s because clean saveall can take 30–45 s under bot load; avoids Docker SIGKILL mid-save), then polls for `Status=exited` on its own 120 s budget.

**Admin console attach must use a raw PTY, not `subprocess.PIPE`.** The worldserver container has `Tty=true`/`OpenStdin=true`; from the non-interactive admin container, `docker attach` with a pipe dies (`cannot attach stdin to a TTY-enabled container …`), breaking Restart and Apply. `wow-server-sp-admin/app/services/console.py` opens a PTY, sets the slave raw, passes the slave FD as Docker's stdin, writes commands + `\x10\x11` via the master, waits for Docker to consume the detach, then closes. On clean raw-mode detach Docker may exit 1 with stderr `read escape sequence` — that's the detach ack, not a failure.

**Backup architecture is one shared script.** `scripts/backup.sh` is canonical. Phase 7 copies it to `/opt/stacks/azerothcore/backup.sh` for host cron; admin install/redeploy bundle the same file as `/app/scripts/backup.sh` with `STACK_DIR=/ac`. It produces one consolidated `azerothcore-backup-<label>-<stamp>.tar.gz` (manifest.json + SQL dumps + staged configs). No `backup_runner.py`, no per-DB/config-tarball multi-file format.

**Backups are daily-cron, manual, and pre-restore only.** Stop/Restart/Apply take no backup. Manual archives use label `manual`; in-app Restore first takes a `prerestore` archive; nightly cron runs `daily` mode. Pruning is 7 days, owned by `backup.sh` daily mode (deletes old archives of every label plus old legacy artifacts).

**`MaintenanceScheduler` is an asyncio background task, not cron.** Started in `lifespan` (`main.py`) on `app.state.maintenance_scheduler`, cancelled on shutdown. Polls every 30 s but fires only when `now.minute == 0` (top of the UTC hour). `ADMIN_DATA_DIR` (default `/admin-data`, from `azerothcore-admin/data/`) holds `maintenance.json` (config) and `maintenance-log.jsonl` (last 20 runs, trimmed on append).

**`mark_attempted` is called before `runner.start`.** A job's `last_runs` stamp is committed before `runner.start`. If start raises (e.g. another action running), the job logs "skipped" but the stamp stands — no retry until the next UTC hour. Prevents double-fire when the scheduler ticks twice in the same minute.

**Midnight-crossing stop/start windows are unsupported.** `MaintenanceStore.validate` requires `window_start_hour_utc > window_stop_hour_utc` (strictly); "stop 23:00, start 02:00" is rejected 400. Both are 0–23; no cross-day arithmetic.

**Expected admin test warnings.** Dockerized pytest may print pip's "running as root" + new-version notices (disposable container) and Starlette `multipart` / `TemplateResponse` deprecation warnings. The latter two are worth cleaning before a FastAPI/Starlette upgrade but are unrelated to the restart/apply console path.

**The top nav bar lives in `base.html`, not `dashboard.html`.** `#last-refresh` and `#nav-status-pill` are `<span>`s in `<nav class="topnav">`. The pill polls `/api/status` every 60 s via `hx-get`/`hx-swap="none"` + an `htmx:afterRequest` handler that updates its text/class — **not** `outerHTML`/`hx-select` (which would destroy the polling element after one swap). The `htmx:afterSwap` listener updates `#last-refresh` only when the dashboard `#status` card swaps (dashboard only; the pill polls independently on every page).

**`switchLog()` is in `dashboard.html`'s inline `<script>`, not `settings.js`** (which only loads on the settings page). `partials/logs.html`'s `onclick="switchLog(...)"` needs it on the dashboard too — don't move it.

**`settings.js` source-file checkbox selector is `.check-group input[type=checkbox][value]`.** New sidebar checkboxes that should trigger a re-render must live inside `.check-group` (or be wired separately). The sidebar class changed from `.settings-filters` to `.settings-sidebar`/`.check-group`.

**Settings page defaults to "Show only modified."** The `only-modified` checkbox is `checked` in `settings.html`, so `_render()` first paints with `modifiedOnly = true` (only `source === 'admin'|'installer'` keys). No "Show all" toggle, no `COMMON_KEYS`; unchecking shows the full ~1874-key index. With the filter on and no modified keys, it renders one `.empty-state` paragraph — not a bug on a fresh install. The pending badge and Apply button update before the empty-state early return, so they always reflect true pending state.

**SSE activity log needs the canonical htmx-ext-sse pattern — `sse-connect` on a parent, `sse-swap` on the child.** `registerSSE()` finds the EventSource via `getClosestMatch(elt, hasEventSource)`, an ancestor walk; same-element use happens to work but is fragile and unsupported. In `dashboard.html`: `.panel-body` has `hx-ext="sse" sse-connect="/api/action/stream"`, and `#action-log` (`<ul>`) has `sse-swap="progress,done" hx-swap="beforeend"`.

**`GZipMiddleware` must not compress `/api/action/stream`.** Browsers send `Accept-Encoding: gzip` on every `EventSource` request; gzipped SSE makes some browsers silently drop all events (permanently empty activity log). `main.py` uses `_GZipExcludeSSE`, an ASGI wrapper that bypasses GZip for that path and delegates the rest. Don't replace it with a plain `GZipMiddleware`.

**`config_index.py` comment parsing has two rules.** `parse_dist_file()`: (1) *per-key blocks* — `_comments_by_key()` keys a structured section by the SHORT identifier in the comment text (e.g. `"BotActiveAlone"`), not the full key, so `active_comment_by_key.get(key)` falls back to `key.rsplit(".",1)[1]` on a miss — keep that fallback (it's what lets `AiPlayerbot.BotActiveAlone` find its description). (2) *flat blocks* — consecutive KV lines after a comment share it, but sharing stops at a blank line (`had_blank` clears `active_comment`), preventing descriptions bleeding across unrelated keys.

**`$STACK_DIR/build/dist/` is not in the repo** — the installer creates it (`install-azerothcore-admin.sh:118`). Any `rsync -a --delete "$REPO_DIR/" "$STACK_DIR/build/"` wipes it; always `mkdir -p "$STACK_DIR/build/dist"` right after the rsync, before copying `.conf.dist` files.

**HTMX vendor files (`htmx.min.js`, `htmx-sse.js`) are 51/59-byte placeholders in the repo** — the install script fetches the real ones from unpkg; redeploy preserves them via `rsync --exclude`. Any new `rsync -a --delete` must exclude both paths or the app serves broken HTMX (silently relying on the browser cache). Restore: `curl -sSfL -o "$STACK_DIR/build/app/static/htmx.min.js" "https://unpkg.com/htmx.org@2.0.3/dist/htmx.min.js"` (and `htmx-ext-sse@2.2.2` for `htmx-sse.js`). Redeploy aborts if either file is ≤ 200 bytes.

**Mobile layout is `@media (max-width: 768px)` only — desktop untouched above it.** `#last-refresh` is hidden on mobile. The settings sidebar becomes a sticky bar with a collapsible `.mobile-collapsible` toggled by `#mobile-filter-toggle` (JS at the bottom of `settings.js`). The detail panel is a full-screen overlay (`position: fixed; translateX(100%)` → `.mobile-visible` removes it); `selectKey()` adds `mobile-visible` when `window.innerWidth <= 768`; `closeMobileDetail()` (global in `settings.js`) clears it and `state.selected` (Back button calls it). `#result-count` must stay OUTSIDE `.mobile-collapsible` so the count shows when filters collapse.

## Reference docs

`docs/` (gitignored — specs/plans can't be committed) holds offline reference material. Consult it to verify config options or module behaviour rather than guessing defaults.

| Path | Contents |
|------|---------|
| `docs/configs/worldserver.conf.dist` | Authoritative default `worldserver.conf` — every key + default |
| `docs/configs/playerbots.conf.dist` | mod-playerbots defaults |
| `docs/configs/mod_ahbot.conf.dist` | mod-ah-bot defaults |
| `docs/configs/individualProgression.conf.dist` | mod-individual-progression defaults |
| `docs/wikis/azerothcore-wiki/docs/` | Full AC wiki (install, DB schema, GM commands, module dev, …) |
| `docs/wikis/mod-playerbots-wiki/` | Playerbots: install, config, commands, raid strategy, troubleshooting |
| `docs/wikis/mod-individual-progression-wiki/` | Progression: install, tiers, changes, extras |
| `docs/superpowers/plans/` | In-progress implementation plans |
| `docs/superpowers/specs/` | In-progress design specs |
| `…/specs/2026-05-20-wow-server-sp-admin-design.md` | **Authoritative admin spec** — read before changing the action runner, apply/rollback, post-apply verification, snapshot/write semantics, or mount layout |
| `…/specs/2026-05-22-admin-ui-overhaul-design.md` | UI/UX overhaul (Classic palette, nav, stat cards, settings layout) — read before touching `app/templates/`, `app/static/app.css`, `app/static/settings.js` |
| `…/specs/2026-05-24-settings-description-and-mobile-design.md` | Settings description parsing + mobile layout — read before touching `config_index.py` parsing or mobile CSS/JS |

Check `plans/` and `specs/` before non-trivial architectural work — in-flight designs may already cover or constrain the task.

## Constraints to preserve

- Scripts must **not** run as root — the `EUID -eq 0` guard in `install-azerothcore.sh` (and `install-azerothcore-admin.sh`) is intentional; scripts call `sudo` internally.
- Password inputs accept only shell-safe chars (`letters, numbers, . _ @ % + = , : -`) so the config file is safe to `source` on resume.
- Neither uninstaller (`uninstall-azerothcore.sh`, `uninstall-azerothcore-admin.sh`) may use `--remove-orphans` (would remove unrelated containers sharing the Compose project name); the admin's `docker rm -f azerothcore-admin` covers its single service.
- `verify-azerothcore.sh` / `verify-azerothcore-admin.sh` stay on `set -u` without `-e` so all checks run regardless of individual failures.
- The admin app must edit nothing under `/opt/stacks/azerothcore/` except `docker-compose.admin.yml` and `backups/*` — the `/ac/` mount is ro to enforce this; add no other rw sub-mount.
