# PR #25 Boundary-Focused Corrections Design

**Status:** Approved corrective scope; implementation deferred to a cold session.

**Audit provenance:** pre-PR #25 `8c5c0cb877a4ccf0183cd089dbb71e0ee0b02f6d`; original PR #25 result `0931a5f0ea2ba773ad54b84bc1929ce3c0960d00`; audited current main `eedc7c2ae968f785eb8b900c0fe52042f7be8b29`; audit branch `audit/pr25-behavioral-audit`.

## Purpose

Correct only defects that still exist in behavior introduced or materially changed by PR #25. Preserve the valid corrections in e16f877, b3e6cd9, and PR #26. Keep deep validation at creation/import/restore boundaries, keep read-only pages metadata-only, and avoid new workers, caches, persistent state, dependency upgrades, or unrelated architecture changes.

The complete audit ledger contains 44 behavioral rows. Twelve correction units cover the two Critical, ten Important, and two actionable Minor findings; a thirteenth unit defines preservation evidence. The remaining Minor browser-timeout concern and adjacent pre-PR #25 risks remain documented rather than changed.

## Global constraints

- Never exercise host Docker lifecycle, `/opt/stacks`, systemd, crontab, installers, uninstallers, redeploys, or restores during tests.
- Lifecycle tests run only inside a disposable container with no Docker socket, no host `/opt` mount, and stubbed `docker`, `sudo`, `systemctl`, `rm`, and `crontab` wherever those commands are reachable.
- Every behavior correction follows RED → GREEN: add a focused regression, run it against the defective implementation, inspect the expected failure, then make the smallest implementation change.
- Do not weaken or replace the current PR #26 backup-listing, database-statistics, App Events, Dashboard Logs, or browser behavior.
- Backups GET paths must not open/decompress archives. General verification must not inspect backup contents or freshness.
- No production dependency changes and no authentication/CSRF redesign in this correction set.
- No implementation agents run concurrently against the shared working tree.

## Correction units

### 1. Backup publication contract

`scripts/backup.sh` will validate the staged v2 SQL stream before constructing or publishing an archive. Validation requires exactly the four canonical `-- Current Database: \`name\`` sections in canonical order and a terminal mysqldump completion footer. It scans line-by-line with a 4096-byte section-header bound and reads only an 8192-byte tail for the footer. Validation is O(dump size) once at the creation boundary and uses bounded memory.

An exit-zero but malformed `mysqldump` therefore fails before tar creation, atomic rename, pruning, or “Backup complete.” Existing recovery archives remain untouched. Test stubs that represent successful mysqldump output must emit the canonical stream; a dedicated malformed-success stub proves rejection.

### 2. Shared backup/restore mutation exclusion

The existing `${BACKUP_DIR}/.backup.lock` remains the one cross-process lock understood by host cron and the admin container.

- Host disaster-recovery restore acquires the lock non-blockingly before it stops the worldserver or changes configuration/databases, and holds it through readiness completion.
- In-app restore first validates the archive, stops the server, and creates its pre-restore safety backup. That backup owns and releases the existing writer lock. The restore then acquires the same lock before the first database mutation and holds it through overlay restoration and server start.
- If the post-safety-backup lock cannot be acquired, in-app restore has not changed a database. It restarts the server, reports a busy failure, and does not mutate.

This ordering prevents backup/restore overlap without deadlocking the safety backup.

### 3. Host restore archive containment and cleanup

The host restore preflight will replace name-only `tar -tzf` validation and unrestricted extraction with a Python standard-library extractor invoked before confirmation or live mutation. It will:

- reject absolute paths, `..` components, duplicate member names, links, devices, FIFOs, sockets, and every type except directories and regular files;
- cap members at 10,000, each ordinary member at 8 GiB, total declared expansion at 16 GiB, `manifest.json` at 1 MiB, and `config/docker-compose.admin.yml` at 1 MiB;
- create a fresh stage and write regular files only beneath it, without following links;
- reject truncated members and clean the stage on every exit.

The fresh stack's preserved `custom.cnf` copy will live inside the already-trapped restore stage rather than in a second unmanaged `mktemp` path. Success, validation failure, stop failure, import failure, timeout, and interruption all remove it with the stage.

### 4. Uninstaller confinement and recovery ordering

Production target constants in `scripts/uninstall-azerothcore.sh` will be immutable and will no longer accept `STACK_DIR`, `STATE_FILE`, or `CONFIG_FILE` environment overrides. The stack removal must pass through the same exact-literal guard as other removals; its privileged flag changes how the approved literal is removed, not which path is approved.

Tests will obtain isolation by copying the script to a temporary directory and rewriting all fixed target literals in that copy. They will not add a production override seam. Their dangerous command set will be stub-only.

The systemd unit is stopped/disabled before Docker teardown so it cannot restart the stack, but its file is not removed until Compose and fallback Docker cleanup have succeeded. If teardown fails, the unit file and stack/state/config recovery context remain, the message explains that the unit may need re-enabling, and the script exits nonzero. A stop/disable failure aborts before Docker mutation.

### 5. Root redeploy current-boot readiness

`scripts/redeploy-azerothcore.sh` will not infer current-boot readiness from a host log file that the new process has not yet demonstrably truncated. After Compose starts the service, it obtains the container `StartedAt` timestamp and polls the current container's logs with `docker logs --since "$started_at"`. Only a matching marker from that interval succeeds. Missing `StartedAt`, early exit, and timeout remain explicit failures.

The regression test seeds a stale `Server.log` marker and proves it cannot make a stubbed new boot succeed. A separate test supplies a current-boot Docker log marker.

### 6. Admin redeploy durability and serialization

Admin redeploy acquires `${STACK_DIR}/.redeploy.lock` with non-blocking `flock` before creating a timestamped candidate. This prevents tag collision and rollback interleaving.

The existing staged build, e16f877 `build/dist` recreation, candidate health check, full verifier, and rollback all remain. Only after health and verification succeed does the script promote the candidate image to `azerothcore-admin:local`, the Compose default used by the installed systemd unit and ordinary future recreation. If promotion fails, the still-unmodified old `:local` tag permits automatic rollback. After promotion, removing the temporary candidate tag is best-effort; the running container keeps its image by ID and future Compose operations select the promoted `:local` tag.

No global Docker image prune is introduced.

### 7. Bounded canonical admin archive validation

`wow-server-sp-admin/app/services/actions.py` will define type-specific 1 MiB caps for `manifest.json` and the admin overlay in addition to the existing generic member/count/total limits. Member metadata is rejected before any read or extraction. Manifest reads request at most the cap plus one byte, and every call site—including the post-validation format lookup in `run_restore`—uses the bounded loader.

Manifest semantics become exact: `format_version` must be an `int` but not `bool`; `databases` must be a list exactly equal to the four canonical names; `skipped_databases` must be an empty list; malformed types return a validation error rather than raising through the request path.

### 8. Typed restored-overlay validation

`compose_admin.validate_restored_overlay` will receive the actual env-var-to-`KeyEntry` mapping, not only a set of names. After existing YAML shape, service, environment, and blocklist checks, each value must be a non-empty string accepted by the same `config_index.validate_value` function used by Settings. Empty overrides are rejected because the Settings contract represents them by deleting the key.

The overlay is size-checked before `read_text`/YAML parsing. Invalid restored values fail before worldserver stop or database mutation. The existing topology allowlist and protected-key behavior remain unchanged.

### 9. Cancellation-safe, non-blocking import staging

The import route will move local file copy, flush, and fsync work to a thread. A small async helper creates the `to_thread` task, shields it from caller cancellation, and, if the request is cancelled, waits for the underlying thread to finish before allowing cleanup or releasing state. The same helper wraps archive validation.

The route owns one outer `try/finally` for its hidden `.upload` path. Expected oversize, disk, validation, publication, busy-dispatch, unexpected validator exception, and request cancellation paths all remove the hidden file. Once publication succeeds, busy dispatch also removes the published imported archive as today. A timeout that merely stops waiting is not added because it would leave the local I/O thread running and make cleanup unsafe.

### 10. Cancellation-safe progression reservation

Progression apply uses the same shield-and-wait helper around `apply_progression`. Client cancellation waits for the worker thread to reach a terminal result before `runner.release_mutation()` executes, then re-raises cancellation. A second destructive action therefore cannot start while the original database mutation is still running.

Normal result and `ValueError` response semantics remain unchanged.

### 11. Backup-path symlink rejection

Backup enumeration rejects matching symlink entries using non-following metadata. Download and restore resolve only a regular, non-symlink archive whose resolved parent is the canonical backups directory. The common helper performs filename validation, `lstat`, symlink rejection, regular-file enforcement, and parent containment.

This closes the PR #25 symlink escape without changing the project's Tailscale-only trust model or adding authentication architecture. Metadata-only listing still never opens an archive.

### 12. Bounded backup-log status polling

Dashboard backup status will reuse the existing bounded reverse-tail behavior in `services/logs.py` (or an equivalently bounded binary tail helper) and inspect at most 1 MiB / the required trailing lines. The async route offloads backup-status collection with `asyncio.to_thread`.

The newest “Backup complete.” still supersedes older errors, and the newest error since the last completion remains visible. Cost is bounded independently of historical log size and does not decompress archives.

### 13. Preservation checks and no-change findings

Focused regressions and final suites must explicitly preserve:

- e16f877 candidate `build/dist` recreation after `rsync --delete`;
- b3e6cd9/158a3ad final state: general verification does no backup-content or freshness validation;
- bf4dadf/831588d final state: Backups GET paths are metadata-only and scan/stat errors are visible;
- mysql-connector-python 9.1.0 compatibility, two-second connection timeout, and server-side `MAX_EXECUTION_TIME(2000)`;
- corrected Dashboard/Stats Online-card path;
- App Events' 200-record bound, process-local restart clearing, sanitization, coalescing, incident links, best-effort recording, one-shot deep links, tab preservation, filters, and keyboard tabs;
- development-only browser harness and the sole documented Starlette multipart PendingDeprecationWarning.

No correction is planned for the browser fetch-deadline Minor finding. A generic client timeout could report failure while a server mutation continues, so it needs a separate action-id/reconciliation design. Also unchanged because they predate PR #25: authentication/CSRF architecture, full Stats query structure, maintenance attempt-stamp persistence semantics, and generic runner history/queue bounds.

## Error and recovery semantics

- Creation/import validation failure never publishes or prunes recovery data.
- Restore validation failure occurs before stop/mutation.
- Host restore lock contention exits without mutation.
- In-app restore lock contention after its safety backup restarts the unchanged server and reports failure.
- Any failure after database mutation remains conservative: the server stays stopped unless the existing start step itself succeeded, and the safety archive is named in progress output.
- Admin upload/event recording failures do not replace a deliberately degraded response with HTTP 500, except genuine upload storage/publication failures already defined as 500.
- Admin redeploy only rolls back while the old durable image tag is still authoritative.

## Test and validation strategy

Each correction has a focused failing regression. Shell behavior tests use real temporary filesystem semantics and stub only external lifecycle commands. Admin archive tests use real tarfile/YAML/filesystem behavior, reserving mocks for Docker/database/action boundaries. Cancellation tests coordinate real threads with events so they prove that cleanup/release occurs after worker completion.

Final validation runs, in disposable containers with no Docker socket and no `/opt` mounts:

1. `git diff --check`.
2. `bash -n` for every root and admin shell script.
3. `node --check` for browser harness JavaScript.
4. Complete `scripts/tests/` suite with explicit no-socket/no-`/opt` preflight and a read-only live-stack health check after lifecycle-related tests.
5. Complete admin Python suite with pinned requirements, recording exact warnings.
6. Complete Playwright/Chromium/axe suite.
7. Exact focused tests for all thirteen correction units and all required PR #26 preservation cases.
8. Exact mysql-connector-python 9.1.0 construction/query compatibility check.
9. Independent whole-branch correctness and reasonableness review against the original range, net diff, ledger, findings, commits, and command evidence.

## Acceptance criteria

- Every F01–F13 and F15 ledger finding is fixed and verified; F14 remains an explicit documented no-change finding.
- No Critical or Important finding remains unresolved.
- No read-only page or general verifier opens/decompresses backup contents.
- Writer, importer, and both restore paths agree on the canonical archive contract.
- All temporary archive/config/upload artifacts are removed on success, failure, exception, timeout, and cancellation.
- Backup and restore database work cannot overlap.
- Current-boot and durable-deployment claims are supported by current-container and durable-tag evidence.
- All PR #26 corrections remain behaviorally intact.
- All focused/full/static/browser/compatibility/warning validations pass and their actual output is recorded.
- Independent review explicitly approves both correctness and operational reasonableness before delivery.
