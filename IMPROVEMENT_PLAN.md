# Reliability & Correctness Improvement Plan

**Project:** `wow-server-sp`
**Source audit:** `AUDIT_FINDINGS.md` (2026-07-11)
**Execution model:** This is a backlog of implementation briefs for later Codex sessions. No fixes are included here.

## Prioritization principles

1. Restore must be trustworthy before adding recovery polish.
2. A destructive action must prove preconditions before mutation and must not report success without readiness.
3. Backup creation, publication, and verification must agree on what “healthy” means.
4. Add regression tests with each functional fix; broader harness tasks follow once the highest-risk paths are protected.
5. Tasks marked **Behavior change: yes** require the executing model to confirm intended behavior with the user before editing.

## Functionality

### F-01 — Make in-app restore accept real, complete canonical backups

- **Priority / findings:** P0; A-01, A-02, A-07.
- **Files/locations:** `wow-server-sp-admin/app/services/actions.py:252-357`; `tests/test_restore_action.py:10-132`; `tests/test_import_restore.py:9-31`.
- **Problem/root cause:** Footer validation requires an impossible exact suffix for real mysqldump output, reads whole dumps, and accepts arbitrary DB subsets/partial manifests.
- **Fix approach:** Add a bounded tail helper that recognizes the canonical timestamped completion line; require v1 canonical backups to contain exactly all `KNOWN_DBS` and no `skipped_databases`; keep all validation before `run_stop`. Use one shared validator for upload and restore where practical.
- **Acceptance criteria:** A fixture ending `-- Dump completed on 2026-07-11  3:00:01` passes; shortened/incomplete/empty dumps fail; partial/duplicate/unknown/missing DB inventories fail before stop; footer check reads a bounded tail rather than the whole file. Run `python -m pytest tests/test_restore_action.py tests/test_import_restore.py -q` in the documented admin container, then the full admin suite.
- **Executor / dependencies:** **GPT-5.6 Sol**; do before D-02, U-03, and T-07.
- **Behavior change: yes.** Before implementation, confirm that partial archives must be non-restorable rather than supported through an explicit expert-only subset flow.

### F-02 — Verify GM privileges at installer Pause 2

- **Priority / findings:** P0; RS-04.
- **Files/locations:** `scripts/install-azerothcore.sh:3308-3370`; new/extended installer fixture tests.
- **Problem/root cause:** Pause completes when usernames exist; it never proves the GM account has security level 3 for realm `-1`.
- **Fix approach:** Extend the SQL verification to join/query `account_access`, require the intended security/realm scope, and print a precise retry message. Do not expose or revalidate plaintext passwords.
- **Acceptance criteria:** Accounts-without-access and wrong security/realm fail without advancing the checkpoint; correct level 3/global access passes. Run the focused installer tests, the 17-script Docker suite, ShellCheck, and `git diff --check`.
- **Executor / dependencies:** **GPT-5.6 Terra**; coordinate with T-01.
- **Behavior change: no.** This enforces the already-documented manual step.

### F-03 — Treat every non-success Settings action status as failure

- **Priority / findings:** P0; A-03.
- **Files/locations:** `wow-server-sp-admin/app/static/settings.js:260-315`; `app/main.py:446-467`; UI tests.
- **Problem/root cause:** JS recognizes only `error`, although actions also terminate as `timeout` and potentially `already`.
- **Fix approach:** Parse the terminal `data-status`; define success narrowly as `ok` (and only include `already` if the backend can legitimately return it for Apply/Rollback); preserve failure context, reload effective values, and do not redirect on uncertain outcomes. Add EventSource error/unknown-id handling with a bounded recovery message.
- **Acceptance criteria:** `ok` redirects; `error`, `timeout`, unknown/idle, and stream failure remain on Settings with visible recovery guidance. Run focused JS/browser tests plus the admin suite.
- **Executor / dependencies:** **GPT-5.6 Terra**; browser harness T-10 is helpful but not blocking.
- **Behavior change: no.** Corrects misleading status handling.

### F-04 — Preserve Stats player-card polling after swaps

- **Priority / findings:** P1; A-10.
- **Files/locations:** `app/templates/partials/stats_page.html:40-54`; `partials/players.html:1-9`; stats page tests.
- **Problem/root cause:** `outerHTML` removes the element that owns its HTMX polling attributes.
- **Fix approach:** Use `innerHTML` with a stable polling wrapper or return a replacement wrapper carrying identical attributes.
- **Acceptance criteria:** Two or more simulated/browser polling cycles issue requests and update the same card; dashboard use of the shared partial remains correct. Run stats/main page tests and the admin suite.
- **Executor / dependencies:** **GPT-5.6 Terra**.
- **Behavior change: no.** Restores intended live behavior.

### F-05 — Allow spaces and normal editing in Settings inputs

- **Priority / findings:** P1; A-15 and part of A-24.
- **Files/locations:** `app/static/settings.js:103-139`; Settings templates/tests.
- **Problem/root cause:** A role-button row intercepts Space/Enter bubbling from its nested text input.
- **Fix approach:** Do not model a row containing controls as a button. Scope keyboard activation to the row itself (`event.target === row`) or add a separate semantic detail button while leaving input events untouched.
- **Acceptance criteria:** Keyboard entry of `arms pve` works; Enter/Space can still open row details when the row/detail button has focus; read-only behavior remains. Run browser/UI tests and admin suite.
- **Executor / dependencies:** **GPT-5.6 Terra**; combine with U-05 if desired.
- **Behavior change: no.**

### F-06 — Correct manual Compose-change restart instructions

- **Priority / findings:** P1; RS-11.
- **Files/locations:** `scripts/install-azerothcore.sh:3825-3840`; `README.md:82-86`; relevant skill/admin docs.
- **Problem/root cause:** Guidance says `docker compose restart`, which cannot apply changed service environment.
- **Fix approach:** Document/reuse the safe service recreation command or the admin Apply flow, including expected downtime and readiness check. Add a static documentation consistency guard if command duplication remains.
- **Acceptance criteria:** No instruction claims `restart` applies Compose environment; documented command recreates only the intended service and verifies the env/readiness. Run docs/static tests and `git diff --check`.
- **Executor / dependencies:** **GPT-5.6 Terra**.
- **Behavior change: no.** Documentation only.

### F-07 — Resolve installer default/profile drift

- **Priority / findings:** P2; RS-12, RS-16.
- **Files/locations:** `scripts/install-azerothcore.sh:1400-1450,1760-1820`; `CLAUDE.md`; installation reference; consistency tests.
- **Problem/root cause:** Bot and XP defaults differ materially from documentation and target-host assumptions.
- **Fix approach:** Establish one authoritative profile table/default and generate or test all prompts/docs against it. Keep repair/noninteractive defaults synchronized.
- **Acceptance criteria:** Code, help text, README/skill reference, and tests name the same bot default and XP choices; noninteractive behavior matches. Run script suite and ShellCheck.
- **Executor / dependencies:** **GPT-5.6 Terra**.
- **Behavior change: yes.** Confirm the intended bot default and XP preset/custom set with the user before implementation.

## Reliability and resilience

### R-01 — Gate host restore on confirmed worldserver shutdown

- **Priority / findings:** P0; RS-01.
- **Files/locations:** `scripts/restore-azerothcore.sh:174-224`; `scripts/tests/test_restore_sh.py`.
- **Problem/root cause:** `docker stop ... || true` conflates already absent/stopped with a failed stop, then destructive DB work proceeds.
- **Fix approach:** Inspect initial state, stop with an explicit generous timeout, and poll for `exited`/missing. Abort before config copy/drop if a running container cannot be proven stopped; preserve diagnostic stderr.
- **Acceptance criteria:** Stubbed stop failure with running status performs no copy/drop/import; already absent/exited remains safe; timeout exits nonzero with recovery instructions. Run focused restore tests and script suite.
- **Executor / dependencies:** **GPT-5.6 Terra**; first host-restore task, before R-02 and T-03.
- **Behavior change: no.** Enforces existing safety intent.

### R-02 — Recreate and verify worldserver after host restore

- **Priority / findings:** P0; RS-02.
- **Files/locations:** `scripts/restore-azerothcore.sh:195-238`; disaster-recovery runbook/tests.
- **Problem/root cause:** `docker start` reuses the old creation-time environment after restored Compose files are copied.
- **Fix approach:** Recreate the intended Compose services from the effective files, preserving machine-specific `.env` and MySQL tuning. Wait for current-boot initialization, verify container env reflects a known restored value, and fail accurately if readiness is not reached.
- **Acceptance criteria:** A fresh/archived override fixture with distinct values yields the archived value inside the recreated worldserver; failure to recreate/init exits nonzero. Run focused restore tests and script suite.
- **Executor / dependencies:** **GPT-5.6 Sol**; depends on R-01; coordinate with R-05 policy.
- **Behavior change: no.** Implements documented restoration.

### R-03 — Give systemd shutdown an explicit safe grace period

- **Priority / findings:** P0; RS-03.
- **Files/locations:** systemd heredoc at `scripts/install-azerothcore.sh:3764-3798`; installer consistency tests.
- **Problem/root cause:** `docker compose down` uses default stop timing even though this repo documents 30–45 s saves.
- **Fix approach:** Set a service-specific/Compose stop grace that protects worldserver without extending unrelated teardown unnecessarily; verify ordering with `PartOf=docker.service` and systemd timeout settings.
- **Acceptance criteria:** Slow-stop fixture is not killed before the chosen grace; normal stop/restart and Docker-daemon restart preserve ordering; generated unit passes `systemd-analyze verify` where available. Run script tests and ShellCheck.
- **Executor / dependencies:** **GPT-5.6 Sol** because systemd/Docker shutdown ordering is cross-cutting.
- **Behavior change: yes.** Confirm the desired maximum shutdown wait with the user before implementation.

### R-04 — Make root redeploy readiness failure nonzero

- **Priority / findings:** P0; RS-05.
- **Files/locations:** `scripts/redeploy-azerothcore.sh:107-147`; new behavioral tests.
- **Problem/root cause:** Initialization timeout and actionable errors are advisory, followed by successful completion.
- **Fix approach:** Recheck container status while waiting, fail on timeout/current-boot crash, and define a clear Errors.log advisory/failure policy without treating documented benign noise as fatal.
- **Acceptance criteria:** Running-without-init and crash-during-wait exit nonzero; initialized healthy case passes; messages include next diagnostics. Run focused shell tests, script suite, ShellCheck.
- **Executor / dependencies:** **GPT-5.6 Terra**; readiness error policy should align with R-05.
- **Behavior change: no** for readiness. If Errors.log is made fatal, confirm that policy first.

### R-05 — Strengthen root verifier readiness, backup, and env-schema checks

- **Priority / findings:** P0; RS-06, RS-09, RS-13, VAL-01 partially.
- **Files/locations:** `scripts/verify-azerothcore.sh:20-50,180-205,600-740,833-840`; verifier tests/docs.
- **Problem/root cause:** Unbound env values can abort, `running` substitutes for readiness, and any recent file/no backup can satisfy or avoid the backup gate.
- **Fix approach:** Validate required env schema before expansion while preserving all-check accumulation; add current-boot readiness evidence; classify only readable full v1 archives with all DBs as successful backups; separately report actionable Errors.log state.
- **Acceptance criteria:** Missing env keys become `[FAIL]` without abort; running/uninitialized fails; empty/arbitrary/partial/corrupt/stale backups fail or warn per confirmed policy; recent complete archive passes; exact totals remain correct. Run T-02 tests, root verifier fixture suite, ShellCheck.
- **Executor / dependencies:** **GPT-5.6 Sol** for the error/readiness policy, Terra for implementation after policy.
- **Behavior change: yes.** Confirm whether pre-Phase-7/no-backup and actionable Errors.log should fail or warn.

### R-06 — Build and validate admin replacement before downtime

- **Priority / findings:** P0; A-17.
- **Files/locations:** `wow-server-sp-admin/scripts/redeploy-azerothcore-admin.sh:21-99`; shell tests.
- **Problem/root cause:** Current container and image are removed before fallible staging, vendor checks, and build.
- **Fix approach:** Stage/validate/build a candidate image first, retain a rollback tag/image, then switch with minimal downtime. If start/health verification fails, restore or clearly preserve the last known image and recovery command.
- **Acceptance criteria:** Injected rsync/vendor/copy/build failure leaves the current app running; failed replacement start leaves a usable rollback path; healthy deploy passes verifier. Run shell tests, admin verifier fixture, ShellCheck.
- **Executor / dependencies:** **GPT-5.6 Sol**.
- **Behavior change: yes.** Confirm the desired automatic rollback versus preserve-and-instruct policy before implementation.

### R-07 — Add cross-process backup locking and atomic publication

- **Priority / findings:** P0; RS-07.
- **Files/locations:** `scripts/backup.sh:30-151`; `scripts/tests/test_backup_sh.py`; admin backup service assumptions.
- **Problem/root cause:** Deterministic names are written in place and host/admin callers have no shared lock.
- **Fix approach:** Acquire a lock on the shared backup mount, write to a uniquely named temporary artifact on the same filesystem, validate tar/manifest, chmod, then atomically rename. Preserve an existing healthy daily archive on failure.
- **Acceptance criteria:** Concurrent invocations serialize or return a clear busy result; injected tar failure preserves prior archive; readers never see incomplete final names; successful daily replacement/pruning works. Run backup tests, script suite, admin backup tests, ShellCheck.
- **Executor / dependencies:** **GPT-5.6 Sol** for host/container lock semantics; before R-08.
- **Behavior change: no.** Same intended artifacts, safer publication.

### R-08 — Create a cross-database-consistent backup snapshot

- **Priority / findings:** P1; RS-08 and Larger consideration 2.
- **Files/locations:** `scripts/backup.sh:57-70,117-135`; both restore implementations; manifest/format docs/tests.
- **Problem/root cause:** Four independent transactions can capture different moments.
- **Fix approach:** Choose either one multi-database consistent dump/format revision or a short quiesce/snapshot mechanism. Version the manifest/archive if layout changes and keep backward restore compatibility explicit.
- **Acceptance criteria:** A test mutation between schema writes cannot produce a restored broken cross-schema invariant; new/old supported formats validate deterministically; failure never publishes healthy status.
- **Executor / dependencies:** **GPT-5.6 Sol**; depends on R-07 and user decision; update F-01/D-04 accordingly.
- **Behavior change: yes.** Confirm whether a brief backup quiesce is acceptable and whether archive format v2/backward compatibility is desired.

### R-09 — Preserve recovery context when uninstall/force-fresh teardown fails

- **Priority / findings:** P1; RS-10, RS-14.
- **Files/locations:** `scripts/uninstall-azerothcore.sh:179-318`; `scripts/install-azerothcore.sh:1680-1710`; tests.
- **Problem/root cause:** Docker failures are ignored before stack/state deletion and success output.
- **Fix approach:** Accumulate cleanup failures, distinguish absent resources from failed operations, prove known containers are gone before destructive directory removal, and retain enough Compose/state context plus exact recovery commands on incomplete teardown.
- **Acceptance criteria:** Daemon unavailable/resource failure exits nonzero/incomplete and preserves recovery files; absent resources still produce idempotent success; dry-run never mutates. Run focused lifecycle tests, script suite, ShellCheck.
- **Executor / dependencies:** **GPT-5.6 Sol** for recovery-state policy; implementation can be Terra afterward.
- **Behavior change: no.** Enforces safe teardown expectations.

### R-10 — Bound and terminate admin backup subprocesses

- **Priority / findings:** P1; A-11.
- **Files/locations:** `wow-server-sp-admin/app/services/backup.py:30-60`; action/backup/runner tests.
- **Problem/root cause:** Streaming Popen loop and wait have no deadline or cancellation cleanup.
- **Fix approach:** Add a configured overall/no-progress deadline, terminate then kill with bounded waits, drain/report tail output, and always release runner state.
- **Acceptance criteria:** Never-exiting/no-output and output-then-hang fixtures return timeout/error, child is reaped, later action starts, and UI gets useful progress. Run backup/runner/action tests and full suite.
- **Executor / dependencies:** **GPT-5.6 Terra**.
- **Behavior change: no.** Adds failure bounds.

### R-11 — Surface maintenance state corruption safely

- **Priority / findings:** P1; A-12.
- **Files/locations:** `app/services/maintenance.py:59-115`; `app/main.py:318-369`; maintenance template/tests.
- **Problem/root cause:** Invalid persisted config is indistinguishable from intentionally disabled defaults.
- **Fix approach:** Return/store a degraded-state diagnostic, do not run unsafe jobs, preserve/rename corrupt content for repair, and show a persistent UI/log warning. Define whether last known-good config should be retained.
- **Acceptance criteria:** Malformed/unreadable config disables execution but visibly reports the cause; operator save repairs state; scheduler loop stays alive. Run maintenance tests/full suite.
- **Executor / dependencies:** **GPT-5.6 Sol** for fallback policy.
- **Behavior change: yes.** Confirm fail-disabled versus last-known-good behavior before implementation.

### R-12 — Preserve AC `.env` metadata during admin install

- **Priority / findings:** P1; A-19.
- **Files/locations:** `wow-server-sp-admin/scripts/install-azerothcore-admin.sh:49-68`; installer tests.
- **Problem/root cause:** sudo temp/move rewrite does not preserve owner/mode.
- **Fix approach:** Perform an atomic metadata-preserving rewrite using explicit reference owner/mode and same-directory temp cleanup; verify mode 600 invariant.
- **Acceptance criteria:** Content changes idempotently while UID/GID/mode remain unchanged; failed rewrite leaves original intact. Run installer shell tests and ShellCheck.
- **Executor / dependencies:** **GPT-5.6 Terra**.
- **Behavior change: no.**

### R-13 — Clean up or reserve imported archives before runner dispatch

- **Priority / findings:** P2; A-21.
- **Files/locations:** `app/main.py:559-594`; import tests.
- **Problem/root cause:** Archive persistence precedes the single-flight capacity check, and 409 has no cleanup.
- **Fix approach:** Reserve action capacity before expensive persistence or guarantee cleanup on dispatch failure; use a deliberate retained-import policy only if surfaced in UI.
- **Acceptance criteria:** Busy runner leaves no orphan; successful import remains available through restore completion/failure diagnostics. Run import/action tests.
- **Executor / dependencies:** **GPT-5.6 Terra**; coordinate with D-03.
- **Behavior change: no.**

### R-14 — Record progression snapshot outcome and retention

- **Priority / findings:** P2; A-28.
- **Files/locations:** `app/services/progression.py:225-339`; snapshot GC/lifespan/tests.
- **Problem/root cause:** Pre-commit JSON has no outcome marker and no retention.
- **Fix approach:** Preserve pre-change evidence but append/finalize committed/rolled-back outcome safely, and apply a documented retention/count policy without mixing with admin-yml rollback snapshots.
- **Acceptance criteria:** Applied, validation-failed, and exception paths have unambiguous records; retention removes only eligible old progression records. Run progression/lifespan tests.
- **Executor / dependencies:** **GPT-5.6 Terra**.
- **Behavior change: yes.** Confirm retention duration/count and whether failed-attempt audit records should remain.

### R-15 — Clear or qualify stale Stats refresh errors during retry

- **Priority / findings:** P2; A-30.
- **Files/locations:** `app/services/stats_cache.py:71-93`; stats partial/tests.
- **Problem/root cause:** New refresh sets status but keeps the prior terminal error.
- **Fix approach:** Clear error at retry start or expose separate last-error/retrying state; render consistent status.
- **Acceptance criteria:** Failed refresh followed by retry shows refreshing/retrying, not a stale terminal failure; new failure/success updates correctly. Run stats cache/page tests.
- **Executor / dependencies:** **GPT-5.6 Terra**.
- **Behavior change: no.**

### R-16 — Add a single-instance installer lock

- **Priority / findings:** P2; RS-19.
- **Files/locations:** installer startup/state helpers at `scripts/install-azerothcore.sh:1-140,1240-1265`; tests.
- **Problem/root cause:** Multiple invocations share checkpoints/temp/config paths with no exclusion.
- **Fix approach:** Acquire an advisory lock before mutable preflight/state access; report owner/PID/context where available; ensure clean release and sensible stale-process behavior.
- **Acceptance criteria:** Second concurrent fixture fails fast without mutation; normal resume after first exit works; signals/errors release kernel-managed lock. Run installer tests and ShellCheck.
- **Executor / dependencies:** **GPT-5.6 Terra**.
- **Behavior change: no.**

## Data handling correctness

### D-01 — Enforce Settings value types at the API boundary

- **Priority / findings:** P1; A-05.
- **Files/locations:** `app/main.py:746-851`; `app/services/config_index.py:19-67`; config policy/apply tests; JS.
- **Problem/root cause:** Known keys are validated, but arbitrary strings are written regardless of inferred bool/int/float type.
- **Fix approach:** Validate strict, locale-independent bool/int/float syntax server-side using key metadata; preserve strings exactly; optionally add a small explicit policy for high-risk constrained keys. Return per-key 400 details and mirror controls client-side without trusting them.
- **Acceptance criteria:** Malformed typed values never snapshot/write/restart; valid negative/decimal/boolean representations follow agreed rules; strings with spaces remain valid. Run apply/config-index/UI tests and full suite.
- **Executor / dependencies:** **GPT-5.6 Sol** for validation policy, Terra for implementation.
- **Behavior change: yes.** Confirm accepted boolean forms, numeric normalization, and scope of enum/range enforcement before implementation.

### D-02 — Bound imported archive expansion and validate admin overlay pre-stop

- **Priority / findings:** P1; A-08.
- **Files/locations:** `app/main.py:559-594`; `app/services/actions.py:125-147,289-336`; compose/config policy/tests.
- **Problem/root cause:** Compressed-size limit does not bound extraction, and admin yml is installed after DB replacement without preflight shape/policy validation.
- **Fix approach:** Enforce total expanded bytes/member count/per-member limits while extracting; reject links/special files not required by format; parse overlay before stop and allow only the expected service/environment shape and nonblocked keys.
- **Acceptance criteria:** Oversized/member-heavy/special-file/malformed/extra-service/blocked-key fixtures fail before stop; canonical backup passes. Run import/restore/compose tests and full suite.
- **Executor / dependencies:** **GPT-5.6 Sol**; depends on F-01's shared archive validator.
- **Behavior change: yes.** Confirm practical archive size/member limits and strict overlay compatibility policy.

### D-03 — Use unique exclusive import staging and prevent double-submit races

- **Priority / findings:** P1; A-09.
- **Files/locations:** `app/main.py:565-594`; `app/static/backups.js:36-72`; import tests.
- **Problem/root cause:** Second-resolution final names plus `wb` allow same-second request collisions.
- **Fix approach:** Stream to an exclusive UUID/nanosecond temporary name, fsync/validate, then atomically publish a unique final name; disable import while in progress and make retries explicit.
- **Acceptance criteria:** Concurrent frozen-time uploads produce distinct correct files or one clear busy response; no partial final names; button state recovers after all outcomes. Run concurrent import tests/browser test.
- **Executor / dependencies:** **GPT-5.6 Terra**; coordinate with R-13 and U-01.
- **Behavior change: no.**

### D-04 — Validate host-restore manifest and compatibility before extraction/mutation

- **Priority / findings:** P1; RS-15.
- **Files/locations:** `scripts/restore-azerothcore.sh:98-164`; `scripts/tests/test_restore_sh.py`; format docs.
- **Problem/root cause:** Manifest is only existence-checked/printed.
- **Fix approach:** Parse JSON with an available documented dependency or a narrowly controlled parser, require supported `format_version`, exact DB inventory, no skipped DBs, and consistency with staged files before stop.
- **Acceptance criteria:** Malformed/unsupported/partial/mismatched manifests fail before stop; valid v1 passes; messages identify incompatibility. Run restore tests/script suite/ShellCheck.
- **Executor / dependencies:** **GPT-5.6 Terra**; update alongside F-01 and future R-08 format work.
- **Behavior change: no.** Enforces the existing canonical format.

### D-05 — Add capacity-aware install warnings

- **Priority / findings:** P2; RS-17, RS-18.
- **Files/locations:** installer preflight/prompts around `scripts/install-azerothcore.sh:1815,2143-2145`; docs/tests.
- **Problem/root cause:** Documented disk prerequisite is only printed; buffer pool accepts values well above target RAM without context.
- **Fix approach:** Measure `/opt` free space and physical RAM; implement an explicit fail/warn/confirmation policy with noninteractive behavior and override documentation.
- **Acceptance criteria:** Below/above threshold fixtures follow agreed policy; selected buffer pool beyond safe ratio warns/requires confirmation; CI fixtures do not depend on host hardware. Run installer tests/ShellCheck.
- **Executor / dependencies:** **GPT-5.6 Terra**.
- **Behavior change: yes.** Confirm hard-fail versus warning thresholds and noninteractive override semantics.

## Performance affecting UX

### P-01 — Offload dashboard DB polling and bound query time

- **Priority / findings:** P1; A-06.
- **Files/locations:** `app/main.py:257-268`; `app/services/db_stats.py:26-42`; route tests.
- **Problem/root cause:** Blocking connector/query work runs inline on asyncio loop.
- **Fix approach:** Use `asyncio.to_thread` consistently and configure supported read/query timeout or cancellation boundary; keep graceful unavailable rendering.
- **Acceptance criteria:** A blocked fake query does not delay `/healthz` or SSE heartbeat; timeout yields unavailable card and closes connection. Run DB stats/players/main async tests and full suite.
- **Executor / dependencies:** **GPT-5.6 Terra**.
- **Behavior change: no.**

### P-02 — Remove divergent test dependencies from production runtime image

- **Priority / findings:** P2; A-27.
- **Files/locations:** `wow-server-sp-admin/Dockerfile:20-40`; requirements/build tests/docs.
- **Problem/root cause:** Production layer installs old pytest tooling separately from `requirements-dev.txt`.
- **Fix approach:** Keep runtime image to runtime requirements; use documented disposable test container or a separate test stage if image-contained tests are required.
- **Acceptance criteria:** Production image starts and healthchecks without pytest/httpx test packages; documented suite remains reproducible from `requirements-dev.txt`; build size comparison recorded.
- **Executor / dependencies:** **GPT-5.6 Terra**.
- **Behavior change: no.**

## UI/UX and accessibility

### U-01 — Make Restore/import/action failures visible and preserve current activity

- **Priority / findings:** P1; A-13, A-14, A-22.
- **Files/locations:** `app/static/backups.js:12-92`; `templates/dashboard.html:87-113`; `app/static/settings.js:25-315`; `app/static/stats.js:44-49`.
- **Problem/root cause:** Manual fetch paths discard failures; activity clears after failed requests; network/SSE errors have no recovery state.
- **Fix approach:** Centralize fetch JSON/error parsing, disable/re-enable initiating controls, clear activity only when a new action ID is accepted, preserve busy action history on 409, and show retry/recovery messages for network/SSE errors.
- **Acceptance criteria:** 409/400/500/network/invalid-JSON cases are visible and do not clear current action history; success starts one action and updates controls. Run browser tests plus full suite.
- **Executor / dependencies:** **GPT-5.6 Terra**; D-03 should define import busy state; T-10 recommended.
- **Behavior change: no.**

### U-02 — Make Progression selection and confirmation keyboard-accessible

- **Priority / findings:** P1; A-16.
- **Files/locations:** `templates/partials/progression_page.html:35-64,122-147,205-236`; CSS/tests.
- **Problem/root cause:** Expansion choices are clickable divs; modal lacks complete labelled/focus behavior.
- **Fix approach:** Use semantic buttons or radio group with disabled/selected states, accessible labels, keyboard navigation, Escape/cancel, focus trap, and focus return.
- **Acceptance criteria:** Keyboard-only user can select, confirm, cancel, and return focus; screen-reader roles/states are correct; disabled downgrade/current targets are unavailable. Run axe/browser tests and progression suite.
- **Executor / dependencies:** **GPT-5.6 Terra**; T-10 helpful.
- **Behavior change: no.**

### U-03 — Display backup health/partial state accurately

- **Priority / findings:** P1; A-02, A-20, RS-09.
- **Files/locations:** `app/services/backups.py:12-95`; backup partial templates/routes; JS/tests.
- **Problem/root cause:** Filename parser hides partial, restore remains enabled, and historical errors are attached to newer successes.
- **Fix approach:** Parse/inspect manifest health, expose full/partial/corrupt status, disable unsafe restore with explanation, and correlate latest run outcome instead of scanning for any old error.
- **Acceptance criteria:** Partial/corrupt archives are visibly distinct and nonselectable per F-01 policy; failure followed by success shows healthy current status while retaining optional history; summary counts can distinguish usable archives. Run backup service/page/browser tests.
- **Executor / dependencies:** **GPT-5.6 Terra**; depends on F-01 policy and R-07 artifact semantics.
- **Behavior change: yes.** Confirm whether partial artifacts remain listed/downloadable and how they count in summaries.

### U-04 — Repair undefined Progression CSS tokens

- **Priority / findings:** P2; A-23.
- **Files/locations:** `app/static/app.css:1-20,978-1177`; visual/static tests.
- **Problem/root cause:** Progression references nonexistent `--line`, `--muted`, `--bg-panel`.
- **Fix approach:** Map to established palette tokens or define intentional aliases; add a static undefined-custom-property check with an allowlist for fallbacks.
- **Acceptance criteria:** No undefined referenced token without fallback; toast/borders/muted text meet existing contrast style. Run UI tests/visual inspection.
- **Executor / dependencies:** **GPT-5.6 Terra**.
- **Behavior change: no.**

### U-05 — Correct HTML landmarks, list structure, and mobile filter ARIA

- **Priority / findings:** P2; A-24, A-25 and remaining A-15 semantics.
- **Files/locations:** `templates/base.html:32`; `settings.html:11-56`; `templates/backups.html:23-25`; `app/main.py:434-443`; settings JS.
- **Problem/root cause:** Nested main, interactive controls inside role-button, nested list items for SSE, and missing expanded/controls state.
- **Fix approach:** Use one main landmark, semantic row/detail controls, target an empty `<ul>` for `<li>` SSE payloads, and synchronize `aria-expanded`/`aria-controls`.
- **Acceptance criteria:** HTML validator and axe report no cited issues; keyboard flows remain; SSE progress list is valid. Run browser/UI/SSE tests.
- **Executor / dependencies:** **GPT-5.6 Terra**; can combine with F-05 and T-08/T-10.
- **Behavior change: no.**

### U-06 — Align Stop smoke checklist with actual behavior

- **Priority / findings:** P2; A-26.
- **Files/locations:** `wow-server-sp-admin/README.md:26-63`; stale comment at admin installer `:95-97`.
- **Problem/root cause:** Docs still describe a legacy per-Stop backup.
- **Fix approach:** Document actual notify/save/stop sequence and point users to explicit Create backup / restore safety behavior.
- **Acceptance criteria:** Checklist matches emitted progress and consolidated archive format; no promise of `acore_*-YYYY-MM-DD.sql`. Run docs/static checks.
- **Executor / dependencies:** **GPT-5.6 Terra**.
- **Behavior change: no.**

### U-07 — Remove deprecated TemplateResponse call style

- **Priority / findings:** P2; COMPAT-01.
- **Files/locations:** `app/main.py:149-160,271-306` and any other warning sites; route tests.
- **Problem/root cause:** Old template-first signature emits six Starlette warnings.
- **Fix approach:** Mechanically use the request-first signature consistently.
- **Acceptance criteria:** Admin suite passes with those six warnings gone; the separate multipart pending deprecation may remain documented. Run full admin suite.
- **Executor / dependencies:** **GPT-5.6 Terra**.
- **Behavior change: no.**

### U-08 — Defer log rotation/tail-cap changes pending reproduction

- **Priority / findings:** No change recommended; A-29.
- **Files/locations:** `app/services/logs.py:44-89`; tests only if pursued.
- **Decision:** The cap is a deliberate resource bound and the rotation race is only suspected. Add a targeted reproduction test when operational evidence appears; do not expand scan size or complexity now.
- **Executor / dependencies:** **GPT-5.6 Terra** only if evidence is obtained.
- **Behavior change: no.**

## Integration reliability

### I-01 — Serialize Progression with destructive admin actions

- **Priority / findings:** P0; A-04.
- **Files/locations:** `app/main.py:670-732,486-535`; progression service/runner/UI/tests.
- **Problem/root cause:** Progression is a mutating direct request outside the single-flight domain used by restore/clear/apply.
- **Fix approach:** Either run progression through the action runner with progress/result semantics or acquire a shared mutation gate that returns 409 while destructive actions run. Ensure no DB write can begin during restore/clear and vice versa.
- **Acceptance criteria:** Deterministic blocked-restore concurrency test rejects/queues progression per agreed policy; no “applied” response can be overwritten by an already-running restore without explicit ordering. Run progression/action/concurrency tests and full suite.
- **Executor / dependencies:** **GPT-5.6 Sol**.
- **Behavior change: yes.** Confirm reject-immediately versus queue/progress behavior and corresponding UI with the user.

### I-02 — Separate liveness from Docker-backed readiness verification

- **Priority / findings:** P1; A-18.
- **Files/locations:** `app/main.py:130-132`; `docker-compose.yml:49-54`; `scripts/verify-azerothcore-admin.sh:28-89`; tests.
- **Problem/root cause:** Constant `/healthz` proves only HTTP loop health while verifier never tests Docker socket access from the non-root container.
- **Fix approach:** Keep a cheap liveness endpoint for restart policy and add a readiness/integration check or verifier commands that perform harmless Docker inspect plus DB/DNS reachability with bounded timeouts.
- **Acceptance criteria:** Wrong Docker GID/socket permission fails readiness/verifier but not necessarily liveness; healthy stack passes; DB-down policy is explicitly classified. Run smoke/docker/admin verifier tests.
- **Executor / dependencies:** **GPT-5.6 Sol** for readiness policy, Terra for implementation.
- **Behavior change: yes.** Confirm what belongs in container health versus post-install verifier to avoid restart loops during AC maintenance.

### I-03 — Make the documented ShellCheck gate green and selective

- **Priority / findings:** P0; VAL-01.
- **Files/locations:** `CLAUDE.md:14-31`; cited script warning sites; optional `.shellcheckrc`/validation script.
- **Problem/root cause:** Standard command exits 1 only on intentionally accepted warnings.
- **Fix approach:** Prefer narrow inline disables with rationale where warning is local; otherwise provide a repository validation wrapper/exclusions that suppress exactly the documented codes/sites without hiding new diagnostics.
- **Acceptance criteria:** Documented command exits 0 on current tree; introducing an unsuppressed known ShellCheck error exits nonzero; guidance matches actual invocation. Run exact command and script suite.
- **Executor / dependencies:** **GPT-5.6 Terra**.
- **Behavior change: no.**

## Tests and validation infrastructure

These tasks add coverage. Where a functional task already adds its regression test, avoid duplicating it; extend the same fixture/harness.

### T-01 — Build an executable installer phase/resume/adoption harness

- **Findings / files/locations:** TST-01; new helpers/tests under `scripts/tests/`, exercising `scripts/install-azerothcore.sh:1-137,1858-2075`; supports F-02, F-07, R-03, R-16, D-05.
- **Problem/root cause:** Current tests parse source text but do not execute phase, trap, checkpoint, resume, or adoption state transitions.
- **Fix approach:** Build command/filesystem/Docker stubs and minimally parameterize paths only where necessary without changing production defaults.
- **Acceptance criteria:** Cover stale/malformed checkpoints, phase selection, failed phase not marked complete, save/load recovery, adoption failure, clean_exit vs ERR trap, and init-container timeout/nonzero. Tests are deterministic, use no sudo/live Docker, and pass via `python -m pytest scripts/tests/ -q` in the disposable container.
- **Executor / dependencies:** **GPT-5.6 Sol** for harness design, Terra for enumerated cases.
- **Behavior change: no.**

### T-02 — Add executable verifier failure-path tests

- **Findings / files/locations:** TST-02; new root/admin verifier tests around `scripts/verify-azerothcore.sh:1-840` and `wow-server-sp-admin/scripts/verify-azerothcore-admin.sh:14-115`.
- **Problem/root cause:** Text assertions cannot prove the all-checks accumulator or delegated failure behavior.
- **Fix approach:** Run each script against a fake stack and stubbed commands with a mutation/call log.
- **Acceptance criteria:** Cover multiple failures continuing, INFO exit semantics, exact totals, delegated root failure, malformed env/bind/tools, readiness, backup classification, and Docker permission. Run both suites and ShellCheck.
- **Executor / dependencies:** **GPT-5.6 Terra**; R-05/I-02 policy first.
- **Behavior change: no.**

### T-03 — Cover destructive host-restore failure states

- **Findings / files/locations:** TST-03, RS-20; `scripts/tests/test_restore_sh.py` and `scripts/restore-azerothcore.sh:98-238`.
- **Problem/root cause:** Destructive failure ordering and recovery state are largely unexecuted.
- **Fix approach:** Extend Docker/file stubs to inject failures before and after each mutation and record exact ordering.
- **Acceptance criteria:** Cover incomplete dumps, stop/config-copy/drop/create/import failure at each ordinal, recreate/readiness failure, and restored env; assert exact mutations and recovery message. Run focused/full script suite.
- **Executor / dependencies:** **GPT-5.6 Sol** for recovery expectations, Terra for cases; implement alongside R-01/R-02/D-04.
- **Behavior change: no.**

### T-04 — Cover backup outages, concurrency, and artifact failures

- **Findings / files/locations:** TST-04, RS-20; `scripts/tests/test_backup_sh.py`, `scripts/backup.sh:46-150`, and admin backup service tests.
- **Problem/root cause:** Current tests cover only sequential success and one partial case.
- **Fix approach:** Extend command/file stubs and add controlled concurrent/hanging processes.
- **Acceptance criteria:** Cover DB absent/total outage/mysqldump/copy/tar/disk/prune failure, cleanup, lock contention, atomic replacement, and subprocess hang; no failure publishes healthy output or prunes recovery data. Run focused/full suites.
- **Executor / dependencies:** **GPT-5.6 Terra** after R-07 semantics; coordinate with R-08/R-10.
- **Behavior change: no.**

### T-05 — Add Settings rollback endpoint/persistence coverage

- **Findings / files/locations:** TST-05; new tests near `tests/test_apply.py`/`test_compose_admin.py` for `app/main.py:750-792`.
- **Problem/root cause:** No test invokes the rollback endpoint or proves snapshot/write/action ordering.
- **Fix approach:** Exercise the route with real temporary AdminCompose files and a controlled runner.
- **Acceptance criteria:** Cover no snapshot 404, latest selection, forward snapshot, exact contents, runner race 409/no write, write failure, restart timeout, and post-verify failure. Run focused/full suite.
- **Executor / dependencies:** **GPT-5.6 Sol**; align terminal expectations with F-03.
- **Behavior change: no.**

### T-06 — Test Start's Compose path translation fully

- **Findings / files/locations:** TST-06; `tests/test_actions.py` around `app/services/actions.py:444-576`.
- **Problem/root cause:** Existing Start test checks only that a command contains `compose`.
- **Fix approach:** Unit-test `_ac_compose_base_args` and `run_start` with representative mount/env combinations.
- **Acceptance criteria:** Cover mount discovery, multiple Compose files, custom project, malformed/missing env, inspection fallback, exact env-file override, and failure result; no live Docker. Run focused/full suite.
- **Executor / dependencies:** **GPT-5.6 Terra**; no functional dependency.
- **Behavior change: no.**

### T-07 — Complete in-app restore failure/timeout coverage

- **Findings / files/locations:** TST-07, A-01, A-02, A-07, A-08; `tests/test_restore_action.py`, `test_import_restore.py`, and `app/services/actions.py:81-357`.
- **Problem/root cause:** Real archive format, command failures, timeouts, and resource-policy paths are missing.
- **Fix approach:** Add realistic tar fixtures and controlled subprocess/stop/start failures.
- **Acceptance criteria:** Cover real footer, full/partial manifest, bounded tail, drop/create/import nonzero/timeout, admin-yml preflight, post-import Start failure, and expanded limits; preflight failures precede stop and destructive failures preserve documented recovery state. Run focused/full suite.
- **Executor / dependencies:** **GPT-5.6 Terra** after F-01/D-02 policies.
- **Behavior change: no.**

### T-08 — Add SSE/middleware ordering and multi-subscriber tests

- **Findings / files/locations:** TST-08; new async tests for `app/main.py:80-98,597-630` and `app/services/runner.py:31-159`.
- **Problem/root cause:** Browser-critical SSE/gzip/order behavior is manual-only.
- **Fix approach:** Drive ASGI responses and runner events with deterministic synchronization rather than wall-clock sleeps.
- **Acceptance criteria:** Cover no gzip, ordered replay once, concurrent subscribers, unsubscribe, idle, heartbeat-to-new-action, and exception-before-done. Full suite passes.
- **Executor / dependencies:** **GPT-5.6 Sol** for ordering design; supports U-05.
- **Behavior change: no.**

### T-09 — Add root/admin lifecycle shell behavior tests

- **Findings / files/locations:** TST-09, RS-20; new tests for root/admin redeploy, root/admin uninstall, force-fresh, and systemd heredoc.
- **Problem/root cause:** Lifecycle scripts have almost no executable failure-order coverage.
- **Fix approach:** Reuse a shared shell-command mutation log and fake filesystem/systemctl/Docker tools.
- **Acceptance criteria:** Cover preflight/build preservation, stop timeout, recreate failure, current-boot marker, dry-run/abort, absent daemon/systemd/cron, and metadata preservation; no live mutation; script suite/ShellCheck pass.
- **Executor / dependencies:** **GPT-5.6 Sol** for harness/recovery expectations, Terra for cases; supports R-03/R-04/R-06/R-09/R-12.
- **Behavior change: no.**

### T-10 — Introduce a minimal browser accessibility/HTMX harness

- **Findings / files/locations:** TST-10; new browser-test configuration/tests covering templates/static assets; do not replace fast pytest tests.
- **Problem/root cause:** Source-string assertions cannot execute keyboard, focus, HTMX, fetch, or accessibility behavior.
- **Fix approach:** Select a minimal pinned browser/axe harness in a disposable environment and add only critical flows first.
- **Acceptance criteria:** Cover keyboard/focus/dialog/mobile/filter, real polling swaps, fetch failures, status matrix, Apply/Rollback progress, and axe/HTML checks with one documented command and resource budget.
- **Executor / dependencies:** **GPT-5.6 Sol** for tool choice, Terra for scenarios; supports F-03/F-04/F-05/U-01/U-02/U-05.
- **Behavior change: yes.** Confirm browser dependency/runtime budget before adding.

### T-11 — Cover cache and dashboard DB failure behavior

- **Findings / files/locations:** TST-11; `tests/test_stats_cache.py`, `test_players.py`, DB service tests around `stats_cache.py:71-93` and `main.py:257-268`.
- **Problem/root cause:** Collector/dashboard failure and retry states are incomplete.
- **Fix approach:** Use deterministic thread/events and connector fakes for blocked/failing paths.
- **Acceptance criteria:** Collector failure preserves prior snapshot and returns idle; retry state is clear; blocked query does not block event loop; connections/cursors close. Run focused/full suite.
- **Executor / dependencies:** **GPT-5.6 Terra**; supports R-15/P-01.
- **Behavior change: no.**

### T-12 — Optional Python static-analysis/coverage gate

- **Findings / files/locations:** TST-12; optional changes to `pyproject.toml`, `requirements-dev.txt`, and validation docs.
- **Problem/root cause:** No independent Python lint/type/coverage signal exists, but an unowned gate can create more noise than value.
- **Fix approach:** **No change recommended by default.** If requested, select tools and ratchet from an agreed clean baseline.
- **Acceptance criteria:** Only if pursued: one documented reproducible command, current baseline green, narrowly scoped exclusions, and an agreed coverage/warning budget.
- **Executor / dependencies:** **GPT-5.6 Terra** if explicitly requested; VAL-01 should be resolved first as a cautionary example.
- **Behavior change: yes.** Confirm the development-workflow/tooling commitment before implementation.

## Cross-task dependencies

| Predecessor | Dependent tasks | Reason |
|---|---|---|
| F-01 | D-02, U-03, T-07 | Establish canonical archive validation and partial-backup policy first. |
| R-01 | R-02, T-03 | Restore recreation/readiness tests assume destructive work cannot begin live. |
| R-07 | R-08, U-03, T-04 | Define atomic artifact/lock semantics before format consistency and display. |
| R-05 | T-02, R-04 alignment | One readiness/error/backup policy should drive all verification paths. |
| D-03/R-13 | U-01 | Backend import busy/unique behavior determines correct UI states. |
| I-01 | its concurrency tests | Mutation serialization policy must be agreed before test expectations. |
| T-10 | UI task hardening | Browser harness materially improves regression quality, but urgent UI fixes need not wait if focused tests are possible. |

## Traceability

| Finding(s) | Plan task(s) |
|---|---|
| A-01, A-02, A-07 | F-01, T-07; A-02 also U-03 |
| A-03 | F-03, T-05, T-10 |
| A-04 | I-01 |
| A-05 | D-01 |
| A-06 | P-01, T-11 |
| A-08 | D-02, T-07 |
| A-09 | D-03 |
| A-10 | F-04, T-10 |
| A-11 | R-10, T-04 |
| A-12 | R-11 |
| A-13, A-14, A-22 | U-01, T-10 |
| A-15 | F-05, U-05, T-10 |
| A-16 | U-02, T-10 |
| A-17 | R-06, T-09 |
| A-18 | I-02, T-02 |
| A-19 | R-12, T-09 |
| A-20 | U-03 |
| A-21 | R-13, D-03 |
| A-23 | U-04 |
| A-24, A-25 | U-05, T-10 |
| A-26 | U-06 |
| A-27 | P-02 |
| A-28 | R-14 |
| A-29 | U-08 — no change recommended pending reproduction |
| A-30 | R-15, T-11 |
| RS-01, RS-02 | R-01, R-02, T-03 |
| RS-03 | R-03, T-09 |
| RS-04 | F-02, T-01 |
| RS-05 | R-04, T-09 |
| RS-06, RS-09, RS-13 | R-05, T-02 |
| RS-07 | R-07, T-04 |
| RS-08 | R-08, T-04 |
| RS-10, RS-14 | R-09, T-09 |
| RS-11 | F-06 |
| RS-12, RS-16 | F-07 |
| RS-15 | D-04, T-03 |
| RS-17, RS-18 | D-05, T-01 |
| RS-19 | R-16, T-01 |
| RS-20 | T-01 through T-09 (umbrella; no separate task) |
| VAL-01 | I-03, T-02 |
| COMPAT-01 | U-07 |
| TST-01 through TST-12 | T-01 through T-12 respectively |

Every finding in `AUDIT_FINDINGS.md` is mapped above or explicitly marked no-change/optional.

## How to proceed

Execute tasks in dependency order within the active Codex execution. Do not end the root execution turn between tasks while a dependency-ready task remains and no genuine external blocker or user policy decision exists. Start with P0 tasks in this order: **F-01 → R-01 → R-02 → I-01 → R-03/R-04/R-05 → R-06/R-07 → F-02/F-03/I-03**. Add each task's focused regression tests during that task; use the broader T-series tasks for harness gaps that remain.

For each task:

1. Paste or reference the complete task brief and its finding IDs.
2. If **Behavior change: yes**, require the executing model to confirm the specified intended behavior with the user before editing.
3. Configure **GPT-5.6 Terra** for routine, bounded, well-specified edits and **GPT-5.6 Sol** for concurrency, restore state machines, systemd/Docker ordering, policy decisions, or cross-cutting archive changes. Where the Codex surface exposes model/reasoning controls, the user configures them before starting the execution session; the task brief itself cannot change runtime settings.
4. Preserve the user's existing working-tree changes and inspect current diffs before editing.
5. Run the focused acceptance commands, then the applicable full Docker test suite, exact ShellCheck gate once I-03 is fixed, and `git diff --check`.
