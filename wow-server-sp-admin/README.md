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
   `announce/notify (30s) → wait_grace → announce/notify (final 10s) →
   saveall → docker_stop → wait_exit → backup → done`. Players in-game
   see two warning broadcasts. The POST returns immediately (browser
   does not hang for ~50 s). Container exits cleanly (exit code 0); a
   fresh `acore_*-YYYY-MM-DD.sql` appears under
   `/opt/stacks/azerothcore/backups/`. Total wall-clock ~40-60 s.
3. **Start**: click Start; SSE shows `compose_up → wait_init` then
   "Running" within ~5 min. Confirm `Server.log` contains a
   `WORLD: World Initialized In N Minutes M Seconds` line (this is what
   the start action keys off; if upstream changes the casing without
   the case-insensitive matcher catching it, Start will time out).
4. **Restart**: click Restart; full stop + start sequence runs.
5. **Settings search**: open `/settings`; type "innodb buffer" — the
   `Database.WorkerThreads` row (or similar comment match) appears.
6. **Settings edit + apply**: change `AiPlayerbot.MinRandomBots` to a
   small value (e.g. 50), Apply. Confirm the apply POST returns an
   action id immediately, the SSE action log streams restart progress,
   and post-apply verification reports the env var bound (no red
   verify-failed banner).
7. **Apply delete**: clear an admin-set value entirely (empty input
   field), Apply. Confirm the env var disappears from
   `docker-compose.admin.yml`, the restart runs, and the verify panel
   does NOT flag the removed key as failed (regression guard for
   review finding #10).
8. **Two-tab live progress**: kick off a Restart in one browser tab,
   then open the dashboard in a second tab. Both should show the same
   live SSE progress thanks to broadcast subscribe + history replay.
9. **Rollback**: click "Rollback last apply"; previous value is restored
   and another restart runs.
