# Codebase Reliability & Correctness Audit

**Project:** `wow-server-sp`
**Audit date:** 2026-07-11
**Audited tree:** current working tree, including pre-existing uncommitted changes in `scripts/install-azerothcore.sh`, `scripts/tests/test_installer_consistency.py`, `wow-server-sp-admin/app/services/actions.py`, and `wow-server-sp-admin/tests/test_actions.py`
**Scope:** AzerothCore installer/lifecycle/backup/restore scripts and the FastAPI + HTMX admin application. No fixes were implemented.

**Severity scale:** Critical = data loss/corruption or crash on a common path; High = realistically encountered incorrect behavior or broken safety net; Medium = incorrect edge behavior or meaningful UX/performance degradation; Low = minor usability inconsistency or hardening. This audit found **15 High, 29 Medium, and 20 Low** items; no issue met the Critical threshold on the verified paths.

## Executive summary

The highest-impact problems are concentrated in recovery and lifecycle safety nets:

1. The admin's in-app Restore rejects every canonical backup because it does not accept mysqldump's timestamped completion footer (A-01).
2. Partial backups are presented as healthy and may be restored as an inconsistent subset of the four related databases (A-02).
3. Host disaster restore ignores a failed worldserver stop before dropping schemas, and it starts the old container after restoring Compose configuration, so restored environment values are not applied (RS-01, RS-02).
4. Routine lifecycle tools can report success without a usable server, or destroy the last working admin deployment before a replacement is validated (RS-05, A-17).
5. Backup publication is non-atomic and unlocked, while the four database dumps do not share a confirmed cross-database snapshot (RS-07, RS-08).

The automated Python and script suites are currently green, but they do not exercise many of these destructive state transitions. The documented ShellCheck command is intrinsically red on warnings the repository declares intentional, so it cannot currently act as a useful validation gate (VAL-01).

## Project and architecture inferred

- `scripts/` installs and operates AzerothCore WoW 3.3.5a with mod-playerbots, mod-ah-bot-plus, and mod-individual-progression under `/opt/stacks/azerothcore`; phase checkpoints and persisted prompt answers support resume/adoption (`CLAUDE.md:7-10`, `scripts/install-azerothcore.sh:39-137`).
- `wow-server-sp-admin/` is a single-worker FastAPI application with server-rendered Jinja/HTMX pages, an in-process single-flight action runner, SSE progress, Docker/PTY lifecycle operations, and direct MySQL services (`wow-server-sp-admin/app/main.py:49-77`, `wow-server-sp-admin/app/services/runner.py:76-159`).
- The admin's intended write boundaries are the Compose admin overlay, backup directory, admin snapshots, maintenance state, and explicit DB mutations through Docker (`wow-server-sp-admin/docker-compose.yml:23-48`).
- The target host is Ubuntu 22.04, 16 GB RAM, Tailscale-only connectivity, about two human players, and a few hundred bots (`CLAUDE.md:12-14`).

## Baseline validation

| Check | Result | Evidence / limitation |
|---|---|---|
| `shellcheck scripts/*.sh wow-server-sp-admin/scripts/*.sh` | **FAIL**, exit 1, 1.74 s | SC2001 at `scripts/install-azerothcore.sh:388`, SC2016 at `:776`, and SC2012 at `:2415-2417`; these are the exact warnings declared intentional at `CLAUDE.md:27-31`. |
| `docker run --rm -v "$(pwd)/wow-server-sp-admin:/src" -w /src python:3.12-slim bash -c "pip install -r requirements-dev.txt -q && python -m pytest -q"` | **PASS**, exit 0, 15.10 s wall | `230 passed, 7 warnings, 6 subtests passed`; pytest time 3.59 s. One multipart pending deprecation and six TemplateResponse deprecations. |
| `python3 -m pytest -q scripts/tests` | **BLOCKED**, exit 1 | Host Python has no pytest: `/usr/bin/python3: No module named pytest`. |
| `docker run --rm -v "$(pwd):/repo" -w /repo python:3.12-slim bash -c "pip install pytest -q && python -m pytest scripts/tests/ -q"` | **PASS**, exit 0, 4.50 s wall | `17 passed`; pytest time 0.55 s. |
| `git diff --check` | **PASS**, exit 0 | No whitespace errors in the pre-existing working-tree diff. |

Not run: the root/admin verifier scripts require installed stacks and fixed `/opt/stacks/...` paths (`scripts/verify-azerothcore.sh:10`, `wow-server-sp-admin/scripts/verify-azerothcore-admin.sh:5-6`). The manual smoke checklist was not run because it performs live Stop, Apply, Restore, and Rollback operations (`wow-server-sp-admin/README.md:26-63`). No Python lint, typecheck, or coverage command is configured (`wow-server-sp-admin/pyproject.toml:1-4`, `requirements-dev.txt:1-4`).

## Functional correctness

### A-01 — High — confirmed: in-app Restore rejects canonical backups

- **Impact:** The advertised same-machine rollback path never reaches stop/import for a real `backup.sh` archive.
- **Evidence/root cause:** `wow-server-sp-admin/app/services/actions.py:307-313` requires the dump to end exactly with `b"-- Dump completed"`. The canonical live archive `/opt/stacks/azerothcore/backups/azerothcore-backup-daily-2026-07-11.tar.gz` ends `-- Dump completed on 2026-07-11  3:00:01`. The test fixture masks this with the shortened footer at `wow-server-sp-admin/tests/test_restore_action.py:15-16`. Host restore correctly searches for the marker as a substring at `scripts/restore-azerothcore.sh:140-144`.
- **Affected files:** `wow-server-sp-admin/app/services/actions.py`, `wow-server-sp-admin/tests/test_restore_action.py`.
- **Missing evidence:** None material; the production artifact and exact caller were verified read-only.

### A-03 — High — confirmed: Settings Apply/Rollback treats timeout as success

- **Impact:** If worldserver fails to initialize within the action deadline, Settings redirects to the dashboard as though Apply/Rollback succeeded, obscuring an uncertain or failed boot.
- **Evidence/root cause:** `ActionResult.TIMEOUT` is returned at `wow-server-sp-admin/app/services/actions.py:571-573` and propagated by `app/main.py:854-857`; `app/static/settings.js:260-270` only treats literal `data-status="error"` as failure, and redirects for every other status at `:290-296` and `:308-314`.
- **Affected files:** `wow-server-sp-admin/app/static/settings.js`, `wow-server-sp-admin/tests/test_settings_ui.py`.
- **Missing evidence:** No browser-level timeout test; the status mismatch is direct.

### RS-04 — High — confirmed: installer verifies GM account existence, not privileges

- **Impact:** Missing or mistyping the `gmlevel` command still completes installation, leaving the advertised GM account unable to administer the realm.
- **Evidence/root cause:** The operator is instructed to set level 3 at `scripts/install-azerothcore.sh:3323-3325`, but verification only selects usernames at `:3345-3352`; account existence alone completes the pause at `:3359-3361`. Credential mismatch for a pre-existing account is additionally suspected because no authentication check occurs.
- **Affected files:** `scripts/install-azerothcore.sh`, installer behavior tests.
- **Missing evidence:** GM privilege failure was not exercised against the live realm; the incomplete SQL predicate is confirmed.

### RS-11 — Medium — confirmed: documented manual config restart does not apply Compose environment changes

- **Impact:** Operators edit the documented source of truth but the running container retains its creation-time `AC_*` environment.
- **Evidence/root cause:** Installer completion prints `docker compose restart ac-worldserver` at `scripts/install-azerothcore.sh:3830`; README tells users to restart after editing the override at `README.md:82-86`. Compose `restart` does not recreate the service; the repo itself establishes Compose environment as the source of truth at `CLAUDE.md:116-123`.
- **Affected files:** `scripts/install-azerothcore.sh`, `README.md`, operational docs.
- **Missing evidence:** No live env-change demonstration was needed; Compose lifecycle semantics are deterministic.

### RS-12 — Medium — suspected impact / confirmed mismatch: bot default is 1000, documentation says 250

- **Impact:** Accepting defaults may provision four times the documented bot target on the stated 16 GB host, with possible latency or memory pressure.
- **Evidence/root cause:** Interactive and repair defaults are 1000 at `scripts/install-azerothcore.sh:1768` and `:1806`; project/reference guidance says a few hundred/250 at `CLAUDE.md:12` and `skills/wow-server-sp-gamemaster/references/ref-installation.md:115`.
- **Affected files:** `scripts/install-azerothcore.sh`, `CLAUDE.md`, installation reference and tests.
- **Missing evidence:** Intended new default is ambiguous and runtime resource impact was not reproduced.

### RS-16 — Low — confirmed: documented XP profiles do not match the installer

- **Impact:** Users plan around presets/custom rates that the prompt does not offer.
- **Evidence/root cause:** Reference advertises `x1, x2, x3, x5, x10, x15, x20, or custom` at `skills/wow-server-sp-gamemaster/references/ref-installation.md:114`; installer accepts `x1, x3, x5, x7` at `scripts/install-azerothcore.sh:1425-1438`.
- **Affected files:** installer and installation reference.
- **Missing evidence:** Product intent is unresolved: either code or documentation may be authoritative.

### A-10 — Medium — confirmed: Stats player card stops polling after its first swap

- **Impact:** Player counts on the Stats page silently become stale.
- **Evidence/root cause:** Polling wrapper uses `hx-swap="outerHTML"` at `wow-server-sp-admin/app/templates/partials/stats_page.html:47-49`; the replacement root returned by `partials/players.html:1-9` has no polling attributes, so the behavior is destroyed on first swap.
- **Affected files:** stats/player partials and UI tests.
- **Missing evidence:** No browser test, but HTMX outerHTML replacement semantics are direct.

### A-15 — Medium — confirmed: Settings inputs cannot enter spaces reliably

- **Impact:** Legitimate string values such as `arms pve` cannot be typed normally.
- **Evidence/root cause:** Each text input is nested inside a row with `role="button"` (`wow-server-sp-admin/app/static/settings.js:119-130`); the row keydown handler prevents Space/Enter without checking the event target (`:134-137`). A real space-containing setting exists at `docs/configs/playerbots.conf.dist:1432`.
- **Affected files:** `wow-server-sp-admin/app/static/settings.js`, browser/UI tests.
- **Missing evidence:** No browser automation; bubbling keyboard semantics are deterministic.

## Reliability and resilience

### RS-01 — High — confirmed: host restore can drop schemas while worldserver remains live

- **Impact:** A stop failure can leave worldserver writing while restore drops/recreates databases, causing runtime failures or inconsistent restored state.
- **Evidence/root cause:** `scripts/restore-azerothcore.sh:186-187` discards `docker stop` failure with `|| true`; destructive drop/create follows at `:217-220` without inspecting terminal state.
- **Affected files:** `scripts/restore-azerothcore.sh`, `scripts/tests/test_restore_sh.py`.
- **Missing evidence:** No destructive live reproduction was attempted; the control-flow failure is direct.

### RS-02 — High — confirmed: host restore does not apply restored Compose environment

- **Impact:** Restored bot counts, progression/rates, and other `AC_*` settings can differ between files and the running process.
- **Evidence/root cause:** Archived overlays are copied at `scripts/restore-azerothcore.sh:203-207`, then the old container is normally reused with `docker start` at `:232-236`; Compose recreation is only a fallback after start failure.
- **Affected files:** restore script, restore tests, disaster-recovery runbook.
- **Missing evidence:** No live restore was executed; Docker container environment immutability confirms the failure path.

### RS-03 — High — suspected: systemd shutdown grace may SIGKILL bot-heavy saves

- **Impact:** Reboot/service stop may terminate worldserver during its final save and lose recent game state.
- **Evidence/root cause:** Generated unit uses unqualified `docker compose down` at `scripts/install-azerothcore.sh:3786-3787`. The root redeploy documents Docker's 10 s default versus 30–45 s bot-heavy save time at `scripts/redeploy-azerothcore.sh:91-98`; admin stop deliberately allows 60 s (`CLAUDE.md:184`).
- **Affected files:** systemd heredoc in installer and its tests.
- **Missing evidence:** No deliberately slow live shutdown was observed; actual data loss remains unverified.

### RS-05 — High — confirmed: root redeploy reports success without readiness

- **Impact:** Automation and operators receive exit 0 even if the replacement worldserver never initializes.
- **Evidence/root cause:** Missing `World Initialized` only warns at `scripts/redeploy-azerothcore.sh:121-136`; script then prints `Redeploy complete` at `:145-147`. Non-empty authoritative `Errors.log` is only displayed at `:138-143`.
- **Affected files:** `scripts/redeploy-azerothcore.sh`, new behavioral tests.
- **Missing evidence:** No intentionally stuck live container was created; success control flow is confirmed.

### RS-06 — High — suspected: full verifier can pass a running but unusable worldserver

- **Impact:** A stuck boot or actionable runtime errors can be reported as a healthy install.
- **Evidence/root cause:** Check 1 tests only Docker `running` at `scripts/verify-azerothcore.sh:180-189`; final PASS depends only on accumulated failures at `:833-840`. The script has no readiness-marker check and no actionable `Errors.log` policy, despite both being authoritative in `CLAUDE.md:96-97`.
- **Affected files:** root verifier and verifier tests.
- **Missing evidence:** A concrete stuck-but-running production state was not reproduced; known-benign Errors.log entries require a product decision on fail versus warn.

### A-17 — High — confirmed: admin redeploy removes the working deployment before validating its replacement

- **Impact:** A sync, vendor-file, copy, or build failure leaves the admin stopped and the previous local image removed.
- **Evidence/root cause:** `wow-server-sp-admin/scripts/redeploy-azerothcore-admin.sh:21-25` runs `down` and removes the image before fallible sync/validation/build steps at `:27-68`, under `set -euo pipefail` (`:1-2`).
- **Affected files:** admin redeploy script and shell behavior tests.
- **Missing evidence:** None material; shell failure ordering is direct.

### RS-07 — Medium — confirmed: backup publication is non-atomic and unlocked

- **Impact:** A failed same-day rerun can truncate the previous healthy archive; overlapping host/admin runs can publish or overwrite incomplete output.
- **Evidence/root cause:** Daily names collide for a whole date and other labels within a second (`scripts/backup.sh:30-35`); `tar -czf "${ARCHIVE}"` writes directly to the final name at `:135`; no cross-process lock exists although cron and admin share the script (`CLAUDE.md:188`).
- **Affected files:** `scripts/backup.sh`, backup services and tests.
- **Missing evidence:** No concurrent or injected disk-failure reproduction yet.

### RS-08 — Medium — suspected: four dumps are not one cross-database snapshot

- **Impact:** Auth, character, world, and playerbot dumps may represent different moments, breaking cross-schema relationships on restore.
- **Evidence/root cause:** `scripts/backup.sh:57-70` invokes a separate `mysqldump --single-transaction` connection for each database while worldserver remains active.
- **Affected files:** backup format/script, both restore paths, tests/docs.
- **Missing evidence:** No coordinated mutation was injected between dump calls.

### RS-09 — Medium — confirmed: verifier accepts no backup or any recent file as recovery evidence

- **Impact:** Post-install verification can pass with no recoverable archive or with a recent failed partial/corrupt artifact.
- **Evidence/root cause:** Empty backups are INFO at `scripts/verify-azerothcore.sh:724-730`; freshness counts every file at `:732-736` without archive/manifest/completeness validation. Partial archives are deliberately written by `scripts/backup.sh:73-83,139-141`.
- **Affected files:** root verifier and tests.
- **Missing evidence:** None material.

### RS-10 — Medium — confirmed: uninstaller masks teardown failures and removes recovery context

- **Impact:** Docker artifacts can remain while Compose/state paths are deleted, producing reinstall conflicts despite a `Done` result.
- **Evidence/root cause:** Compose/resource removal failures are ignored at `scripts/uninstall-azerothcore.sh:201-263`; stack/state deletion follows at `:295`, and success is unconditional at `:318`.
- **Affected files:** root uninstaller and tests.
- **Missing evidence:** No daemon-outage fixture has been run.

### RS-14 — Medium — confirmed: `--force-fresh` deletes stack files after failed shutdown

- **Impact:** Still-running containers may retain deleted bind mounts and collide with the replacement install.
- **Evidence/root cause:** Compose down failure is ignored at `scripts/install-azerothcore.sh:1701`; `sudo rm -rf "$STACK_DIR"` follows at `:1705` without proving containers are gone.
- **Affected files:** installer and installer state-machine tests.
- **Missing evidence:** No live failed-down reproduction was attempted.

### A-02 — High — confirmed: partial backups are displayed and restored as healthy

- **Impact:** Restore can replace only some related databases and report completion, creating a cross-database mixture.
- **Evidence/root cause:** Partial filenames/manifests are emitted at `scripts/backup.sh:73-83,117-123`; list parsing hides `partial` in the timestamp portion (`wow-server-sp-admin/app/services/backups.py:12-14,71-81`); the UI enables selection (`templates/partials/backups_list.html:5-10`); restore accepts any nonempty known subset (`app/services/actions.py:278-287,325-357`).
- **Affected files:** backup list/service/templates, restore action, import/restore tests.
- **Missing evidence:** No partial archive is currently present, but the end-to-end code path is confirmed.

### A-11 — Medium — suspected: hung backup can wedge all admin actions indefinitely

- **Impact:** A stalled Docker/mysqldump child holds the single-flight slot, blocking Stop, Start, Restore, and Apply until admin restart.
- **Evidence/root cause:** `wow-server-sp-admin/app/services/backup.py:38-57` iterates stdout then waits with no deadline; runner refuses any later action while `_current` is set (`app/services/runner.py:114-120`).
- **Affected files:** backup service, runner-facing action tests.
- **Missing evidence:** A never-exiting child has not been injected.

### A-12 — Medium — confirmed: corrupt maintenance config silently disables schedules

- **Impact:** Scheduled lifecycle jobs stop firing and the UI shows disabled defaults without alerting the operator.
- **Evidence/root cause:** `wow-server-sp-admin/app/services/maintenance.py:66-73` converts any I/O/JSON/type/value failure into a default disabled config; scheduler and page consume it directly (`:168-192`, `app/main.py:322-343`).
- **Affected files:** maintenance service/routes/template/tests.
- **Missing evidence:** Incidence of real disk corruption is unknown; malformed-file behavior is confirmed.

### A-19 — Medium — suspected: admin installer may replace AC `.env` with unsafe metadata

- **Impact:** DB-secret file may become root-owned/more broadly readable and later non-root workflows may lose write access.
- **Evidence/root cause:** rewrite uses `sudo tee .../.env.tmp` then `sudo mv` without preserving mode/owner at `wow-server-sp-admin/scripts/install-azerothcore-admin.sh:49-68`; the uninstaller demonstrates metadata-aware handling at `scripts/uninstall-azerothcore-admin.sh:62-65`.
- **Affected files:** admin installer and installer tests.
- **Missing evidence:** Actual before/after metadata under the target host's root umask was not reproduced.

### A-21 — Low — confirmed: busy runner leaves uploaded archive orphaned

- **Impact:** Repeated 409 responses consume backup storage and clutter the list.
- **Evidence/root cause:** upload persists and validates the file at `wow-server-sp-admin/app/main.py:565-591`; `_kick` can then raise 409 at `:593`/`:486-491` with no cleanup.
- **Affected files:** import route and import tests.
- **Missing evidence:** None material.

### A-28 — Low — confirmed: progression audit snapshots have no outcome or retention management

- **Impact:** Tiny files accumulate indefinitely and a rolled-back DB mutation can leave a snapshot that looks like an applied target.
- **Evidence/root cause:** snapshot is written before DB verification/commit at `wow-server-sp-admin/app/services/progression.py:225-252,311-332`; startup GC only manages admin-yml snapshots (`compose_admin.py:81-96`, `app/main.py:62-66`).
- **Affected files:** progression service and snapshot maintenance/tests.
- **Missing evidence:** Long-term file volume is unknown.

### A-29 — Low — suspected: log reads race rotation and cap backward scanning at 1 MiB

- **Impact:** Rare replacement races may 500, or useful lines may be hidden behind more than 1 MiB of later benign noise.
- **Evidence/root cause:** existence checks precede open/stat at `wow-server-sp-admin/app/services/logs.py:58-64,86-89`; tail scan caps at 1 MiB at `:44-50,68-83`.
- **Affected files:** log service/tests.
- **Missing evidence:** No rotation race or >1 MiB benign suffix was reproduced. **No change recommended until reproduced**; retain a focused test candidate.

### A-30 — Low — confirmed: Stats refresh retains a stale failure during retry

- **Impact:** UI can show a terminal failure banner while a new refresh is actively running.
- **Evidence/root cause:** refresh start sets only `status` at `wow-server-sp-admin/app/services/stats_cache.py:71-79`; `error` clears only after success at `:81-93`; template renders error independently at `templates/partials/stats_page.html:40-43`.
- **Affected files:** stats cache/template/tests.
- **Missing evidence:** None material.

### RS-19 — Low — suspected: installer has no single-instance lock

- **Impact:** Accidental concurrent resume runs can race checkpoint writes, Compose generation, builds, and DB initialization.
- **Evidence/root cause:** shared state/config paths are fixed (`scripts/install-azerothcore.sh:25-28`), checkpoint rewrite uses one shared `.tmp` path (`:1256`), and no lock exists.
- **Affected files:** installer and concurrency tests.
- **Missing evidence:** Concurrent installer execution was not attempted.

## Data handling correctness

### A-05 — Medium — suspected: server accepts arbitrary unvalidated Settings values

- **Impact:** Nonnumeric/invalid values can reach the last-precedence Compose layer and prevent or alter worldserver startup.
- **Evidence/root cause:** payload is only `dict[str, str]` at `wow-server-sp-admin/app/main.py:746-747`; server checks key/blocklist but writes any nonempty string at `:808-842`. `inferred_type` exists (`app/services/config_index.py:19-67`) but is used only for display; inputs are free text (`app/static/settings.js:121-130`).
- **Affected files:** settings API, config index/policy, JS and tests.
- **Missing evidence:** A representative invalid value causing a real AC boot failure was not applied.

### A-07 — Medium — confirmed: Restore footer validation reads entire dumps into memory

- **Impact:** Restore preflight creates avoidable latency and large memory spikes as databases grow; current live world SQL is about 307 MB before Python copies/stripping.
- **Evidence/root cause:** `wow-server-sp-admin/app/services/actions.py:307-310` uses `sql_path.read_bytes().rstrip()` for a footer check. Host restore uses a bounded tail at `scripts/restore-azerothcore.sh:140-144`.
- **Affected files:** restore action/tests.
- **Missing evidence:** OOM threshold was not reached on the current host.

### A-08 — Medium — suspected: import has no expanded-size bound or admin-yml preflight

- **Impact:** A compressed archive can exhaust temp storage; malformed/overbroad `docker-compose.admin.yml` can be installed after databases have already been replaced and prevent restart.
- **Evidence/root cause:** compressed upload cap is 8 GiB (`wow-server-sp-admin/app/main.py:35-37,571-582`), but `extractall` is unrestricted (`app/services/actions.py:289-299`). Admin yml is copied verbatim at `:136-147,332-336` without YAML shape/policy validation before stop.
- **Affected files:** import route, restore action, compose policy/tests.
- **Missing evidence:** No expansion-bomb or malformed-overlay fixture was run.

### A-09 — Medium — suspected: concurrent imports can clobber one destination

- **Impact:** Two requests in one second can truncate/interleave the same file and dispatch restore against corrupted or wrong content.
- **Evidence/root cause:** destination name has second-only resolution at `wow-server-sp-admin/app/main.py:565-569`; the async upload loop writes with `wb` at `:575-583`, and the UI does not disable import (`app/static/backups.js:51-71`).
- **Affected files:** import route/JS/tests.
- **Missing evidence:** Concurrent frozen-clock request reproduction is pending.

### RS-13 — Medium — confirmed: malformed `.env` aborts verifier before its summary

- **Impact:** One missing required key prevents unrelated checks and violates the verifier's all-checks reporting contract.
- **Evidence/root cause:** verifier uses `set -u` (`scripts/verify-azerothcore.sh:8`) and expands `DOCKER_AUTH_EXTERNAL_PORT` at `:42` after checking only that `.env` exists (`:29-37`).
- **Affected files:** root verifier/tests.
- **Missing evidence:** Missing-key fixture has not been executed; shell behavior is direct.

### RS-15 — Low — confirmed: host restore ignores manifest compatibility

- **Impact:** A malformed/future-format archive can enter destructive restore when four dump files happen to look complete.
- **Evidence/root cause:** restore checks only manifest presence (`scripts/restore-azerothcore.sh:131-134`) and prints it (`:156-164`); it never parses `format_version`, database inventory, or skipped DBs, despite the versioned writer at `scripts/backup.sh:117-132`.
- **Affected files:** host restore, backup format docs/tests.
- **Missing evidence:** None material.

### RS-17 — Low — suspected: disk prerequisite is displayed but not enforced

- **Impact:** An undersized host can spend substantial time cloning/building before predictable disk exhaustion.
- **Evidence/root cause:** README requires about 50 GB at `README.md:30`; preflight only prints `df` then checkpoints success at `scripts/install-azerothcore.sh:2143-2145`.
- **Affected files:** installer, docs/tests.
- **Missing evidence:** No low-disk install was reproduced; hard-fail versus confirmation is a product decision.

### RS-18 — Low — suspected: memory tuning permits 32 GB on the documented 16 GB target without warning

- **Impact:** An accepted value may induce swapping/OOM alongside worldserver/playerbots.
- **Evidence/root cause:** target RAM is 16 GB (`CLAUDE.md:12`); prompt accepts 1–32 GB without comparing host memory (`scripts/install-azerothcore.sh:1815`).
- **Affected files:** installer prompt/validation/tests.
- **Missing evidence:** Requires an operator-selected high value and load; warning policy is unresolved.

## Performance affecting UX

### A-06 — Medium — confirmed blocking behavior / suspected outage: dashboard DB poll runs on the event loop

- **Impact:** A slow accepted MySQL connection can freeze all HTTP/SSE handling, not just the Online card, every 10-second poll.
- **Evidence/root cause:** async endpoint directly calls blocking `count_online` at `wow-server-sp-admin/app/main.py:257-263`; the Players page correctly uses `asyncio.to_thread` at `:648-655`. Dashboard polls every 10 s (`templates/dashboard.html:12-13`); connector has only a connect timeout (`app/services/db_stats.py:26-38`).
- **Affected files:** route/db service/tests.
- **Missing evidence:** An accepted-but-stalled query was not induced.

### A-27 — Low — confirmed: production image installs a divergent test toolchain

- **Impact:** Unnecessary image size/build work and version drift between image-baked tests and the documented dev suite.
- **Evidence/root cause:** `wow-server-sp-admin/Dockerfile:22-24` installs pytest 8.3.3/pytest-asyncio 0.24.0/httpx after runtime requirements; `requirements-dev.txt:1-4` specifies pytest 9.0.3/pytest-asyncio 1.3.0.
- **Affected files:** Dockerfile and build/test docs.
- **Missing evidence:** Exact image-size savings not measured.

## UI/UX and accessibility

### A-13 — Medium — confirmed: Restore/import request failures are silent or incomplete

- **Impact:** A busy 409, invalid archive, server error, or network failure can leave users believing restore started; repeat clicks worsen races.
- **Evidence/root cause:** restore discards the fetch response at `wow-server-sp-admin/app/static/backups.js:17-27`; import only partially handles non-2xx and has no rejection/JSON catch or disabled state at `:56-71`. Backend legitimately returns 409 at `app/main.py:486-491`.
- **Affected files:** backup JS/templates/browser tests.
- **Missing evidence:** No browser failure-state test.

### A-14 — Medium — suspected race / confirmed 409 loss: action logs clear after failed requests

- **Impact:** Clicking while another action is busy erases current action history; fast actions may emit initial progress before the post-response clear.
- **Evidence/root cause:** unconditional `htmx:afterRequest` clear at `templates/dashboard.html:87-92` and `app/static/backups.js:75-80`; runner launches work before POST response (`app/services/runner.py:136-159`).
- **Affected files:** dashboard/backups JS and browser tests.
- **Missing evidence:** The fast-event timing race has not been reproduced; clearing on 409 is direct.

### A-16 — Medium — confirmed: Progression target selection is mouse-only

- **Impact:** Keyboard users cannot complete the core progression flow.
- **Evidence/root cause:** choices are nonfocusable divs at `wow-server-sp-admin/app/templates/partials/progression_page.html:35-43`; JS assigns only `onclick` at `:122-147`. Modal lacks labelled relationship/focus trap/return at `:51-64,205-236`.
- **Affected files:** progression template/CSS/browser tests.
- **Missing evidence:** No formal screen-reader audit.

### A-20 — Low — confirmed: backup error remains stale after later success

- **Impact:** Dashboard keeps showing an old red error under a newer successful backup, reducing trust in recovery status.
- **Evidence/root cause:** `wow-server-sp-admin/app/services/backups.py:23-41` scans backward for any historical ERROR without correlating it to a later `Backup complete`; template renders it with latest backup at `templates/partials/backups.html:3-9`.
- **Affected files:** backup status service/template/tests.
- **Missing evidence:** None material.

### A-22 — Low — confirmed: manual fetch/SSE failures lack UI recovery

- **Impact:** Settings can stay on Loading, Apply/Rollback can close and fail silently, and action watching can hang after SSE disconnect.
- **Evidence/root cause:** no catch/status handling in `wow-server-sp-admin/app/static/settings.js:25-29,260-315`; stats refresh ignores response status at `app/static/stats.js:44-49`.
- **Affected files:** settings/stats JS and browser tests.
- **Missing evidence:** No browser network-failure tests.

### A-23 — Low — confirmed: Progression CSS uses undefined design tokens

- **Impact:** Some borders/muted text are dropped and toast feedback can render with an invalid/transparent background.
- **Evidence/root cause:** defined tokens are at `wow-server-sp-admin/app/static/app.css:1-20`; progression references undefined `--line`, `--muted`, and `--bg-panel` at `:978-999,1019,1039-1075,1116-1177`.
- **Affected files:** app CSS and visual/static tests.
- **Missing evidence:** No visual contrast snapshot was taken.

### A-24 — Low — confirmed: invalid/ambiguous HTML and control semantics

- **Impact:** Assistive technologies encounter nested landmarks/interactive roles and malformed list content.
- **Evidence/root cause:** base already provides `<main>` (`templates/base.html:32`) while settings nests another (`templates/settings.html:50-56`); Settings rows put inputs inside `role=button` (`app/static/settings.js:119-130`); backup SSE target list item receives rendered `<li>` progress (`templates/backups.html:23-25`, `app/main.py:434-443`).
- **Affected files:** base/settings/backups templates and settings JS.
- **Missing evidence:** No HTML-validator/axe run.

### A-25 — Low — confirmed: mobile filter toggle omits expanded state

- **Impact:** Screen-reader users cannot tell whether filters are open.
- **Evidence/root cause:** toggle lacks `aria-expanded`/`aria-controls` at `templates/settings.html:11-13`; JS changes only text/class at `app/static/settings.js:335-342`.
- **Affected files:** settings template/JS/tests.
- **Missing evidence:** No axe run.

### A-26 — Low — confirmed: admin smoke checklist promises a Stop backup that intentionally does not occur

- **Impact:** Operators may believe recovery data exists or diagnose a correct Stop as failed.
- **Evidence/root cause:** `wow-server-sp-admin/README.md:34-40` says Stop emits backup and a new SQL artifact; `app/services/actions.py:426-441` ends after stop, and `tests/test_actions.py:207-217` asserts no backup.
- **Affected files:** admin README and stale installer comment (`wow-server-sp-admin/scripts/install-azerothcore-admin.sh:95-97`).
- **Missing evidence:** None material.

### COMPAT-01 — Low — confirmed: six old TemplateResponse calls emit deprecation warnings

- **Impact:** A future FastAPI/Starlette upgrade can turn tolerated patterns into failures.
- **Evidence/root cause:** old template-first calls exist at `wow-server-sp-admin/app/main.py:149-160,271-306`; baseline emitted six warnings. Debt is acknowledged at `CLAUDE.md:200`.
- **Affected files:** admin route rendering/tests.
- **Missing evidence:** Upgrade failure is prospective.

## Integration reliability

### A-04 — High — suspected: progression mutation can race destructive actions

- **Impact:** Progression may report applied immediately before restore overwrites it, or execute while the character schema is being dropped/recreated.
- **Evidence/root cause:** endpoint mutates DB directly via thread at `wow-server-sp-admin/app/main.py:713-723`, without the action runner used by restore/clear (`:486-535`); progression commits independently at `app/services/progression.py:266-339` while restore replaces DBs at `app/services/actions.py:315-329`.
- **Affected files:** progression route/service, runner coordination/tests.
- **Missing evidence:** A deterministic blocked-import concurrency test is required.

### A-18 — Medium — confirmed gap / suspected outage: health and verifier do not prove Docker access

- **Impact:** Wrong Docker GID/socket permissions can pass container health/admin verification while all Docker-backed status/actions are broken.
- **Evidence/root cause:** `/healthz` is constant at `wow-server-sp-admin/app/main.py:130-132`; Compose and verifier use only that endpoint/container health (`docker-compose.yml:49-54`, `scripts/verify-azerothcore-admin.sh:28-40,80-89`) despite Docker group access being load-bearing (`docker-compose.yml:14-20`).
- **Affected files:** health/readiness route, admin verifier, tests.
- **Missing evidence:** No deployed wrong-GID reproduction.

### VAL-01 — High — confirmed: documented ShellCheck gate is permanently red

- **Impact:** Standard lint cannot distinguish expected diagnostics from new defects and cannot be used as a green safety gate.
- **Evidence/root cause:** documented command at `CLAUDE.md:14-18` exits 1 on exactly the warnings declared intentional at `CLAUDE.md:27-31`; baseline reproduced SC2001/SC2016/SC2012 only.
- **Affected files:** lint configuration/guidance and the cited script sites.
- **Missing evidence:** None material.

## Test coverage gaps

All items below are **confirmed gaps**. They do not independently assert a runtime defect beyond the findings they reference.

### TST-01 — High — confirmed gap: installer phase/resume/adoption behavior is mostly unexecuted

The 3,849-line installer's checkpointing, traps, resume, config recovery, adoption failure, and init-container failure paths (`scripts/install-azerothcore.sh:5-137,1858-2075`) are covered mainly by source-text/array assertions (`scripts/tests/test_installer_consistency.py:1-85`) and two Phase 7 strings (`test_install_phase7.py:7-20`). **Missing evidence:** executable failure-state harness. **Affected:** installer/tests.

### TST-02 — High — confirmed gap: verification scripts lack executable failure-path coverage

Root verifier's 22-check accumulator (`scripts/verify-azerothcore.sh:1-13,833-840`) and admin verifier's stack/health/delegation checks (`wow-server-sp-admin/scripts/verify-azerothcore-admin.sh:14-115`) have only textual consistency tests. Missing: simultaneous failures continue, INFO exit semantics, delegated failure, malformed env/bind, missing tools, exact totals.

### TST-03 — High — confirmed gap: destructive host restore failures are untested

Tests cover success/config preservation/traversal/realmlist/root refusal (`scripts/tests/test_restore_sh.py:97-242`) but not incomplete dumps, stop failure, drop/create/import failure after earlier DB replacement, config-copy failure, stale container environment, or restart/readiness failure (`scripts/restore-azerothcore.sh:136-236`).

### TST-04 — Medium — confirmed gap: backup outage/artifact failures are weakly covered

Existing tests cover healthy labels/pruning and one missing-DB partial (`scripts/tests/test_backup_sh.py:64-150`). Missing: container absent, total outage, mysqldump nonzero, config copy/tar/disk-full/prune failure, cleanup, concurrency, and atomic publication (`scripts/backup.sh:46-150`).

### TST-05 — Medium — confirmed gap: Settings rollback has no endpoint/persistence test

Rollback selection, forward snapshot, in-place write under runner lock, restart, and verify (`wow-server-sp-admin/app/main.py:750-792`) are not invoked by tests; apply coverage ends at write/delete/block/busy behavior (`tests/test_apply.py:90-170`).

### TST-06 — Medium — confirmed gap: Start Compose path translation is superficially tested

`_ac_compose_base_args` maps host mounts, COMPOSE_FILE/project name, explicit `-f`, and container env-file path (`app/services/actions.py:444-510`); Start test only asserts a subprocess contains `compose` (`tests/test_actions.py:109-128`).

### TST-07 — Medium — confirmed gap: in-app restore command failures/timeouts are untested

Drop/create and import failure/timeout branches (`app/services/actions.py:94-122,315-340`) lack tests for nonzero/timeout and post-import Start failure. Fixture footer is also unrealistic, causing A-01 (`tests/test_restore_action.py:15-16`).

### TST-08 — Medium — confirmed gap: SSE ordering, replay, multi-tab, and gzip exclusion are manual-only

Runner tests cover limited replay/single-flight (`tests/test_runner.py:11-59`), but not middleware compression, concurrent subscribers, disconnect cleanup, idle/heartbeat transition, or exception-before-done ordering (`app/main.py:80-98,597-630`; `app/services/runner.py:136-156`).

### TST-09 — Medium — confirmed gap: lifecycle scripts have little behavioral coverage

Root/admin redeploy and uninstall failure ordering is largely untested; root uninstaller has only the textual `--remove-orphans` guard (`scripts/tests/test_installer_consistency.py:47-69`). This gap also covers RS-03, RS-05, RS-10, RS-14, A-17, and A-19.

### TST-10 — Low — confirmed gap: UI tests inspect source strings, not browser behavior/accessibility

`wow-server-sp-admin/tests/test_settings_ui.py:7-77` asserts strings rather than keyboard/focus/dialog/mobile/filter/fetch/HTMX behavior. It cannot catch A-03, A-10, A-13 through A-16, or A-22 through A-25.

### TST-11 — Low — confirmed gap: cache and dashboard DB-failure paths are incomplete

Stats cache tests cover successful single-flight/write failure but not collector failure preserving the prior snapshot (`tests/test_stats_cache.py:92-119`); dashboard online-card endpoint covers success but not DB outage (`tests/test_players.py:249-255`).

### TST-12 — Low — confirmed gap: no Python static-analysis or coverage regression gate

`wow-server-sp-admin/pyproject.toml:1-4` configures pytest only and `requirements-dev.txt:1-4` contains no linter/typechecker/coverage tool. **No change recommended by default:** add such gates only if maintainers want to own their ongoing signal/noise.

### RS-20 — Medium — confirmed umbrella gap: destructive lifecycle state transitions lack regression coverage

This umbrella finding consolidates the repository-script investigator's coverage concern and is fully represented by TST-01 through TST-09. It traces especially to RS-01 through RS-10. No separate implementation task is needed beyond those test tasks.

## Larger considerations

1. **Atomic four-schema restore.** Both restore paths replace schemas sequentially. A mid-import failure leaves some schemas restored and others old; current behavior intentionally leaves the server stopped and points to the safety archive (`scripts/restore-azerothcore.sh:195-223`, `wow-server-sp-admin/app/services/actions.py:325-347`). Stronger staging/swap/automatic rollback is redesign-sized and should follow, not block, the immediate stop/preflight/recreation fixes.
2. **Consistent backup format.** Achieving a single cross-database snapshot may require a multi-database dump/format revision or a deliberate quiesce window. That decision changes operational behavior and archive compatibility; it should be confirmed before implementation (RS-08).
3. **Generic configuration validation.** The admin indexes about 1,800 heterogeneous keys. Type validation is feasible, but comprehensive enum/range validation needs a maintained schema rather than comment heuristics (A-05).
4. **Browser-level testing.** Several deterministic accessibility and HTMX/SSE defects cannot be guarded robustly by the current source-string tests. A small real-browser harness is preferable to expanding brittle string assertions (TST-08, TST-10).

## Areas reviewed with no findings

- Root/non-root guards and shell-safe password restrictions.
- Archive path-traversal rejection, known-DB allowlist, SQL-presence preflight, and the intentional remain-stopped-on-destructive-failure behavior, apart from A-01/A-02/A-07/A-08.
- Tailscale IP validation, realmlist update path, Compose port-binding scope, and managed `AC_*` reverse mapping.
- Playerbots duplicate-SQL cleanup, module config seeding, and AH GUID canonicalization.
- Admin env-var derivation and server-side blocked-key enforcement.
- Action runner registration/history/subscriber cleanup/lock release for routes that use it.
- PTY console detach workflow, bounded Docker waits, and truncate-aware boot-log scan.
- SSE HTML escaping and gzip exclusion.
- Progression downgrade/online transaction guards and latency-based real-player filtering.
- Responsive layouts exist for dashboard/settings/backups/players/progression; findings concern semantics and feedback rather than absent mobile support.
- Daily retention intentionally prunes all labels older than seven days.
