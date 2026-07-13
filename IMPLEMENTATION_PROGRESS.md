# Implementation pause — 2026-07-12

Work was paused at the user's request. Preserve the dirty working tree; do not
reset, checkout, or discard changes.

## Approved policy decisions

- Only complete backups are restorable. Partial/corrupt archives remain
  downloadable but do not count as healthy.
- Installer default: 1,500 bots. Keep the current installer XP choices.
- Systemd stop grace: 60 seconds.
- Verifier: warn before backup phase/no backup; fail actionable errors.
- Admin redeploy: automatic rollback on unhealthy replacement.
- Backup v2: pursue a real cross-database consistency improvement only with v1
  restore compatibility and regression proof.
- Maintenance: fail disabled on corrupt state. Progression audit snapshots:
  retain 30 days / 100 records, including failures.
- Settings: strict typed validation; strict restored-overlay allowlist;
  reject conflicting destructive actions; liveness remains cheap and Docker
  integration belongs in readiness/verifier.
- Browser harness approved. Do not add a Python lint/coverage gate.

## Completed and validated in this paused tree

### F-01 — complete backup validation

- `wow-server-sp-admin/app/services/actions.py`: shared validator accepts only
  a complete v1 archive with all four canonical databases, no skipped DBs, and
  a timestamped mysqldump footer. Footer reading is bounded to 8 KiB.
- The in-app restore and upload route reject invalid archives before dispatch or
  worldserver stop.
- Tests passed: focused restore/import suite (18); full admin suite (238).

### R-01 / R-02 — host restore shutdown and recreation

- `scripts/restore-azerothcore.sh` now proves worldserver stopped before
  copying configs or replacing databases, then force-recreates it through
  Compose and waits for `WORLD: World Initialized`.
- Tests passed: `python -m pytest scripts/tests/ -q` in the documented Python
  container (21 at the time of the run).

### R-03 — systemd stop grace

- Generated unit now uses `docker compose down --timeout 60` and
  `TimeoutStopSec=75`.

### R-04 — root redeploy readiness

- `scripts/redeploy-azerothcore.sh` now exits nonzero if worldserver exits
  during startup or does not reach `World Initialized` within its deadline.
- Behavioral tests cover healthy initialization, timeout, and a startup crash.

### R-05 — root verifier

- `scripts/verify-azerothcore.sh` safely reports missing required `.env` keys,
  requires current-boot initialization, accepts only fresh readable complete
  v1 archives, and fails actionable `Errors.log` content.
- Fixture coverage includes malformed environment, readiness, backup
  completeness/freshness, errors, and exact report totals.

### R-06 — admin redeploy rollback

- Admin redeploy stages and builds a uniquely tagged candidate while the old
  app keeps running; failed replacement health or verification automatically
  restores the previous compose file and app.

### R-07 — atomic, cross-process backup publication

- Host/admin backups use a shared non-blocking `flock`, write a temporary
  archive on the backup filesystem, validate it, and atomically rename it.
- Failed tar creation preserves the prior daily archive.

### R-08 — online cross-database-consistent backup v2

- `scripts/backup.sh` now creates one `mysqldump --single-transaction
  --databases` snapshot for all four schemas and writes `format_version: 2`
  with `sql/azerothcore.sql`.
- Root/admin restore and root verification accept both new v2 and legacy v1
  archives. V2 restores import the one multi-database SQL stream after
  dropping the canonical databases.
- Regression coverage proves the writer uses exactly one transactional dump
  containing every canonical schema.

### R-09 — incomplete teardown preserves recovery context

- Uninstall records Compose/Docker resource-cleanup failures and exits before
  deleting stack/state/config recovery context. `--force-fresh` also refuses
  to remove the stack when Compose shutdown fails.
- Focused failure fixture plus full script suite passed.

## Completed and validated after the pause

### R-10 — bounded admin backup subprocesses

- Backup children now have overall/no-progress deadlines, bounded process-group
  TERM/KILL cleanup, reaping, a bounded recent-output tail, and timeout status
  propagation to the action runner.
- Real quiet and output-then-hang fixtures prove cleanup, useful progress, and
  that a later action can start. Independent review approved the final design.

### R-11 — maintenance corruption is fail-disabled and repairable

- Corrupt, unreadable, or invalid `maintenance.json` is quarantined without
  clobbering an existing copy, reports a durable repair diagnostic, and blocks
  scheduled actions. Saving valid settings repairs the state.
- Coverage includes marker-write failure, existing quarantine, scheduler
  survival, page/API diagnostics, and repair.

### R-12 — admin installer preserves AC `.env` metadata

- The installer rewrites an existing Compose-file entry through a same-dir
  atomic temporary file while preserving UID, GID, and mode; failure retains
  the original file and cleans the temp.

### R-13 — busy import dispatch cleans its uploaded archive

- A real occupied `ActionRunner` produces 409 through the actual import route;
  the just-uploaded archive is removed while successful imports remain.

### R-14 — progression audit outcomes and bounded retention

- Progression audit records live under their own `progression-audit/` folder,
  distinguish applied, verification-rolled-back, and exception outcomes, and
  retain 30 days / 100 records including failure outcomes.
- Audit I/O and startup pruning are advisory and cannot change committed DB
  truth or prevent the admin app from starting.

### R-15 — stats retry state clears stale errors

- A retry clears the prior terminal error while refreshing, preserves the last
  good snapshot/single-flight behavior, and displays the latest error only if
  the retry fails.

### R-16 — single-instance installer lock

- The installer uses an isolated `setsid flock --close` holder, with an
  internal FD handoff rather than an ambient env bypass. Outer HUP/INT/TERM
  terminates the full holder group before the lock is released.
- Fixtures cover contender rejection, surviving descendants, outer-wrapper
  termination, argument forwarding, and no mutable-state changes by a loser.

### F-02 — verify GM privileges at Pause 2

- Pause 2 requires `acore_auth.account_access.gmlevel = 3` and `RealmID = -1`
  before checkpointing; missing/wrong/realm-scoped access reports the exact
  `account set gmlevel <GM_USERNAME> 3 -1` retry command.
- Fresh verification: full script suite and the documented ShellCheck gate.

### F-03 — Settings terminal-status handling

- Settings redirects only for an explicit action status of `ok`. Timeout,
  error, unknown/idle, and EventSource-disconnect outcomes keep the operator
  on Settings with recovery guidance.

### I-01 — serialize Progression with destructive actions

- `ActionRunner` now owns an external-mutation reservation used by Progression
  applies. Either an active action or a reserved Progression mutation rejects
  the other side before it can begin.

### I-03 — make the documented ShellCheck gate green

- The three intentionally safe ShellCheck cases use narrow local suppressions
  with rationale. The exact documented `shellcheck scripts/*.sh
  wow-server-sp-admin/scripts/*.sh` command now exits zero.

### F-04 / F-05 — Settings/Stats UI correctness

- The Stats player card uses an `innerHTML` swap, preserving its 10-second
  polling wrapper. Settings row keyboard activation now ignores bubbled input
  key events, allowing normal Space/Enter editing in text fields.

### F-06 / F-07 — installer guidance and profile alignment

- Manual Compose-edit guidance recreates only `ac-worldserver` and asks the
  operator to verify `WORLD: World Initialized`; it no longer recommends
  `docker compose restart` for environment changes.
- Installer interactive and repair defaults are 1,500 bots. The Game Master
  installation reference matches that default and the supported `x1/x3/x5/x7`
  XP choices.

### D-01 — strict Settings value types

- Apply validates inferred typed keys before snapshot/write/restart. Accepted
  forms are `true`/`false`/`0`/`1`, signed decimal integers, and finite
  decimal/scientific floats; strings remain exact and an empty value still
  deletes an override.

### D-03 — unique, atomic import staging

- Imported restore archives write to an exclusive hidden staging path, are
  fsynced and validated there, then atomically published under a UUID-suffixed
  archive name. Same-second uploads cannot overwrite each other.

## Pre-existing changes that must be preserved

Before this implementation began, the working tree already contained changes
to the generated systemd unit (`PartOf=docker.service`, restart behavior) and
admin `run_stop` handling for containers that stop during console attachment,
plus their tests. They are now interleaved with the new work; inspect the diff
carefully rather than assuming all dirty lines belong to this pause.

## Plan completion status

Every pending implementation-plan task, task review, whole-branch review, and
required validation gate is complete. The browser harness remains
development-only and is not included in the production image.

## Deferred maintenance follow-up

- Upgrade FastAPI and its resolved Starlette dependency together, then rerun
  the admin and browser suites. Current test output has one non-blocking
  Starlette multipart-import deprecation warning: the application correctly
  uses `python-multipart==0.0.31`, while the Starlette version resolved by
  `fastapi==0.115.0` still imports its legacy compatibility module. Do not
  suppress the warning or downgrade `python-multipart` merely to hide it.

## Last validation commands

```bash
docker run --rm -v "$(pwd)/wow-server-sp-admin:/src" -w /src python:3.12-slim \
  bash -c "pip install -r requirements-dev.txt -q && python -m pytest -q"

docker run --rm -v "$(pwd):/repo" -w /repo python:3.12-slim \
  bash -c "pip install pytest -q && python -m pytest scripts/tests/ -q"

git diff --check
```

Latest verified counts: admin suite **368 passed** (one Starlette dependency
deprecation warning, 6 subtests); script suite **124 passed** (3 subtests);
browser suite **6 passed**. The documented ShellCheck gate and `git diff
--check` both exit zero.
