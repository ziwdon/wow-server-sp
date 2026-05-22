# wow-server-sp-admin Implementation Verification Report

Repo root: `/home/ziwdon/wow-server-sp/`

Implementation: `wow-server-sp-admin/`

Verification date: 2026-05-22

## Verification Results

`shellcheck wow-server-sp-admin/scripts/*.sh` passed with zero output.

Docker pytest did not pass. The requested command collected 48 tests, not 45:
`46 passed, 2 failed`. Both failures are in `tests/test_installer_script.py`.

### [ISSUE-1] Config type inference misclassifies real keys

**File:** `wow-server-sp-admin/app/services/config_index.py:25`

**Severity:** Medium

**What the spec/plan requires:** `inferred_type` should distinguish `int`, `float`, `bool`, and `string`; the checklist explicitly names `AiPlayerbot.Enabled`, `Rate.XP.Kill`, `PlayerLimit`, and `Server.LoginInfo`.

**What the code does:** `_infer_type()` only parses numeric text, so `0`/`1` become `int`. I confirmed: `AiPlayerbot.Enabled -> int`, `Rate.XP.Kill -> int`, `Server.LoginInfo -> int`; only `PlayerLimit -> int` matches.

**Confirmation:** Ran `build_key_index(Path("docs/configs"))` and printed those four entries.

**Fix:** Add comment/key-aware inference for booleans and rate/string-style keys, then update tests to pin these real examples.

### [ISSUE-2] Comment blocks are attached to the wrong keys

**File:** `wow-server-sp-admin/app/services/config_index.py:73`

**Severity:** Medium

**What the spec/plan requires:** Each key should expose its relevant multi-line comment block.

**What the code does:** For grouped dist-file documentation, the whole block is attached to the first assignment, then later documented keys get an empty comment.

**Confirmation:** `AuctionHouseBot.DEBUG` receives a huge AH-bot command/config block, while `AuctionHouseBot.GUIDs` and `AuctionHouseBot.ItemsPerCycle` have empty comments despite documented comments in `mod_ahbot.conf.dist`. `Rate.XP.Kill` has the XP group comment; `Rate.XP.Quest` has none.

**Fix:** Parse documented key names inside comment blocks and associate the block with each listed key instead of only the next assignment.

### [ISSUE-3] Missing dist files only produce a partial index

**File:** `wow-server-sp-admin/app/services/config_index.py:110`

**Severity:** Medium

**What the spec/plan requires:** The app builds the settings index from all four baked `.conf.dist` files at startup.

**What the code does:** Missing files log a warning and continue, so the app can serve a partial `/api/keys` index.

**Confirmation:** Built an index with only `worldserver.conf.dist`; it returned one key and logged warnings for the three missing files.

**Fix:** Fail startup when any required dist file is missing.

### [ISSUE-4] `/api/keys` response shape does not include `default_value`

**File:** `wow-server-sp-admin/app/state.py:120`

**Severity:** Low

**What the spec/plan requires:** Each key entry includes `key`, `default_value`, `env_var`, `source_file`, `comment`, `effective_value`, `source`, and `inferred_type`.

**What the code does:** The response uses `"default"` instead of `"default_value"`.

**Confirmation:** Generated a resolved row for `AuctionHouseBot.GUIDs`; keys were `comment, default, effective_value, env_var, inferred_type, key, source, source_file`.

**Fix:** Add `default_value` while preserving `default` temporarily if the frontend still expects it.

### [ISSUE-5] Blocked key is editable in the UI

**File:** `wow-server-sp-admin/app/static/settings.js:54`

**Severity:** Low

**What the spec/plan requires:** `AuctionHouseBot.GUIDs` should be visible but read-only, and writes rejected server-side.

**What the code does:** The backend rejects it, but the frontend renders every key as an editable `<input>`.

**Confirmation:** Searched for blocked/read-only handling; only backend `BLOCKED_KEYS` exists in `wow-server-sp-admin/app/main.py`.

**Fix:** Include blocked metadata in `/api/keys` or mirror the blocklist in JS, then render the input disabled/read-only with an installer-managed badge.

### [ISSUE-6] Admin uninstall strips AC's whole `COMPOSE_FILE`

**File:** `wow-server-sp-admin/scripts/uninstall-azerothcore-admin.sh:36`

**Severity:** High

**What the spec/plan requires:** Admin uninstall must not disturb the AC stack, and if it removes admin compose integration it must be safe for reinstall.

**What the code does:** It deletes the entire `COMPOSE_FILE=` line from `/opt/stacks/azerothcore/.env`. On this host that line is `docker-compose.yml:docker-compose.override.yml:docker-compose.admin.yml`, so uninstall would also drop `docker-compose.override.yml` from future AC Compose runs.

**Confirmation:** Read the script and confirmed the live AC `.env` contains all three compose files on one line.

**Fix:** Remove only `docker-compose.admin.yml` from the colon-delimited list; preserve the rest.

### [ISSUE-7] Verify script does not check container health

**File:** `wow-server-sp-admin/scripts/verify-azerothcore-admin.sh:28`

**Severity:** Low

**What the spec/plan requires:** Verify admin container running + healthy.

**What the code does:** It checks only `.State.Status == running`; there is no Docker health-state check, and the compose file has no healthcheck.

**Confirmation:** Read verify script and compose file; health is not inspected.

**Fix:** Add a container healthcheck in compose or explicitly document/use `/healthz` as the health substitute, then verify it.

### [ISSUE-8] Docker pytest command fails on installer-script tests

**File:** `wow-server-sp-admin/tests/test_installer_script.py:10`

**Severity:** Medium

**What the spec/plan requires:** Full suite should pass under the provided Docker command.

**What the code does:** New tests compute `REPO_ROOT = Path(__file__).resolve().parents[2]`; inside the prescribed `/src` mount that resolves to `/`, so tests look for `/wow-server-sp-admin/...` and fail.

**Confirmation:** Ran the exact Docker pytest command: `48 collected`, `46 passed`, `2 failed`, both `FileNotFoundError` for `/wow-server-sp-admin/...`.

**Fix:** Use `parents[1]` for the mounted admin project root or derive paths relative to the test file.

### [ISSUE-9] Rollback and post-apply verifier lack tests

**File:** `wow-server-sp-admin/tests/test_apply.py:42`

**Severity:** Low

**What the spec/plan requires:** Coverage assessment asks for rollback tests and post-apply verify-path tests.

**What the code does:** `rg` found no tests calling `rollback`, `verify_env_vars_bound`, `_read_live_env`, or `_read_loaded_config`.

**Confirmation:** Searched the full test tree. Existing apply tests cover apply/delete/block/concurrent, but not rollback or env-binding verification.

**Fix:** Add focused unit tests for rollback snapshot/restore/restart flow and verifier presence + reverse-mapping failures.

## Checked OK

- Env-var derivation matches the shell helper and CLAUDE examples; golden file covers 1,874 keys and matches fresh shell regeneration.
- Resolver precedence is `admin > installer override > conf > dist`.
- AdminCompose writes in place, snapshots to `/admin-snapshots`, sorts snapshots newest first, and preserves unrelated YAML structure.
- Runner singleton, SSE unsubscribe, history replay, done replay, `asyncio.to_thread`, and `call_soon_threadsafe` are present.
- Stop/start/restart/force-stop mostly match the required lifecycle commands, including detach bytes and `docker stop --time 60`.
- Post-apply verification uses post-write `read_env()`, live `docker exec env`, and the loaded config path verified in the running container.
- Logging config is called before other imports and root level is INFO.
- Dockerfile/compose include Docker CLI, compose plugin, non-root UID, Tailscale bind, external network, docker group, and required mounts in the correct order.

## Summary

Issues found: ISSUE-1 through ISSUE-9.

