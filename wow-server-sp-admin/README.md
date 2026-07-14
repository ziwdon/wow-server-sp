# wow-server-sp-admin

A self-hosted web admin for the AzerothCore stack installed by
`wow-server-sp`. See
[`docs/superpowers/specs/2026-05-20-wow-server-sp-admin-design.md`](../docs/superpowers/specs/2026-05-20-wow-server-sp-admin-design.md)
for the design.

## Installing

```bash
./scripts/install-azerothcore-admin.sh
```

## Verifying

```bash
./scripts/verify-azerothcore-admin.sh
```

## Development tests

The production image installs only `requirements.txt`. Run the test suite in a
disposable Python container, using the separately pinned development
dependencies:

```bash
docker run --rm -v "$PWD:/src" -w /src python:3.12-slim \
  bash -c 'pip install -r requirements-dev.txt -q && python -m pytest -q'
```

## Browser accessibility and HTMX tests

The browser harness is development-only: it uses pinned Playwright, axe-core,
and HTMX packages from `package-lock.json`, and is never copied into the admin
image. Run its complete critical-flow suite in the disposable Playwright
container:

```bash
docker run --rm --init --ipc=host -v "$PWD:/work" -w /work \
  mcr.microsoft.com/playwright:v1.61.1-noble \
  bash -lc 'npm ci --ignore-scripts && npm run test:browser'
```

Resource budget: Chromium only, one worker, no retries, and a 15-second limit
per test. The fixture server uses checked-in templates/static assets with fake
HTTP/SSE responses, so it never needs the AzerothCore Docker stack, database,
or server credentials. It covers keyboard/focus/dialog/mobile/filter controls, HTMX
status swaps, Apply/Rollback SSE completion, fetch failures, and focused axe/
HTML semantics.

## Uninstalling

```bash
./scripts/uninstall-azerothcore-admin.sh
```

## Manual smoke checklist

After install, verify these flows work end-to-end:

1. **Dashboard live**: open `http://${TAILSCALE_IP}:8765/`; all five
   panels populate within ~10 s. Confirm the **Players & bots** panel
   shows real numbers (not "DB unreachable") — proves the admin is on
   `azerothcore_ac-network` and DNS-resolves `ac-database`.
2. **Stop**: click Stop, confirm. SSE log shows
   `attach → notify → wait_grace → notify_final → save → docker_stop →
   wait_exit → done`. Players in-game see two warning broadcasts and
   `saveall` runs before Docker sends the final clean-shutdown signal. The
   POST returns immediately (the browser does not hang for the grace period);
   the container exits cleanly (exit code 0). Stop and Restart do not create
   backups.
3. **Backups and restore safety**: open **Backups** and click **Create backup**
   when an on-demand backup is wanted. It writes one consolidated
   `azerothcore-backup-manual-<timestamp>.tar.gz` archive under
   `/opt/stacks/azerothcore/backups/`, containing a consistent four-database
   snapshot, configuration files, and a manifest. Before a restore replaces
   anything, the app takes a pre-restore safety archive. Imported archives are
   rejected before the server is stopped if expansion would exceed 16 GiB in
   total, 10,000 members, or 8 GiB for one member; links and special files are
   not accepted. A restored `docker-compose.admin.yml` must contain only the
   managed `ac-worldserver.environment` shape and settings keys approved by
   the installed config index (never installer-managed keys).
4. **Start**: click Start; SSE shows `compose_up → wait_init` then
   "Running" within ~5 min. Confirm `Server.log` contains a
   `WORLD: World Initialized In N Minutes M Seconds` line (this is what
   the start action keys off; if upstream changes the casing without
   the case-insensitive matcher catching it, Start will time out).
5. **Restart**: click Restart; full stop + start sequence runs.
6. **Settings search**: open `/settings`; type "innodb buffer" — the
   `Database.WorkerThreads` row (or similar comment match) appears.
7. **Settings edit + apply**: change `AiPlayerbot.MinRandomBots` to a
   small value (e.g. 50), Apply. Confirm the apply POST returns an
   action id immediately, the SSE action log streams restart progress,
   and post-apply verification reports the env var bound (no red
   verify-failed banner).
8. **Apply delete**: clear an admin-set value entirely (empty input
   field), Apply. Confirm the env var disappears from
   `docker-compose.admin.yml`, the restart runs, and the verify panel
   does NOT flag the removed key as failed (regression guard for
   review finding #10).
9. **Two-tab live progress**: kick off a Restart in one browser tab,
   then open the dashboard in a second tab. Both should show the same
   live SSE progress thanks to broadcast subscribe + history replay.
10. **Rollback**: click "Rollback last apply"; previous value is restored
   and another restart runs.
