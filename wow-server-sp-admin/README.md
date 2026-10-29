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

The verifier also prints one advisory `[INFO] cloud backup: …` line (off,
connected with how long ago the last success was, e.g. `last success 5h ago
(<time>)`, or the connection state and last result). It never counts as a
failure: a problem at Proton must not fail a verify.

It prints a second advisory line for the Armory, e.g. `[INFO] armory: dbc ok,
cache 12 MB, viewer fetched 2026-10-29T12:34:56+00:00, 3d on` (which reference
files are readable, the model cache size, when the viewer script was fetched,
and whether the 3D viewer is on). It never counts as a failure either.

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

## Cloud backup (Proton Drive)

Optional. It copies each backup archive to a folder in your own Proton Drive,
so a dead disk or a lost machine does not take every backup with it. Everything
is set up in the **Cloud backup · Proton Drive** panel on the **Backups** tab.

**What it does**

- Every 10 minutes it checks for a backup that is not in the Drive folder yet and
  uploads only that. It never replaces a file that is already there: if a
  different file with the same name exists, the panel names it and leaves it
  alone (move that Drive copy to the trash yourself if you want this server's
  version uploaded).
- Old copies leave the Drive on the same schedule as locally (7 days by
  default, 1 to 90 in the settings). They are moved to the **Proton Drive
  trash**, not erased; you empty that trash yourself. Pre-upgrade and
  pre-rollback archives are never removed automatically, and nothing is removed
  until the newest backup is confirmed to be in the Drive.
- Archives that are in the Drive get an **In Drive** badge in the backups list.

**Connecting**

1. The server needs a TPM 2.0 chip. Without one the panel says **Not
   available** and why; there is no software-only fallback. If the chip only
   failed for a moment, the app checks it again about every hour. **Disconnect**
   still shows there while a session is stored on this server.
2. Click **Connect to Proton Drive**, open the link on your own device (you can
   copy it), sign in on Proton's page and approve. Your Proton password is typed
   only there, never on the server.
3. Enter the Drive folder, for example `/my-files/WoW Backups` (it must start
   with `/my-files/` or `/shared-with-me/`). If it does not exist yet, use
   **Create folder**. Tick *Copy new backups to Proton Drive automatically* and
   save. **Upload now** works even before you enable the automatic copy.

**Settings:** automatic copy on or off, the Drive folder, which kinds of backup
to copy (all of them by default), and how many days copies stay in the Drive.
The folder cannot be changed while an upload, a folder check or **Create
folder** is running (the panel says it is busy); the other settings can.

**Disconnect** removes the stored session from this server and stops the
copies; your settings are kept. It does **not** end the session at Proton: to do
that, revoke it in your Proton account's session list. Do the same after
uninstalling the admin app.

**Restoring from the Drive:** download the archive in Proton Drive, then use
**Import & Restore** on the Backups tab. On a new machine, reinstall AzerothCore
and run `scripts/restore-azerothcore.sh` with the archive.

**How safe is it?**

- The password is typed only on Proton's own page, on your device.
- The stored session can read, change and trash your whole Drive, and probably
  the rest of the account's encrypted data, until you revoke it. It cannot
  change your password, 2FA or recovery settings.
- On disk it is encrypted with a passphrase that exists only sealed to this
  server's TPM: a stolen disk, a copied data folder or a backup archive is
  useless elsewhere. Anyone who controls the running server (your user, root, or
  the admin container) can use it.
- The admin app has no login: anyone who can reach it over Tailscale or the LAN
  can use this panel like every other page, but no page ever shows the session.
- Safer, if you want it later: a separate Proton account with one folder shared
  to it as editor, and the folder set to `/shared-with-me/…`. Then the worst
  case is that the backups folder gets damaged.
- Only Proton receives data. The Proton Drive tool sends crash reports and
  telemetry to Proton according to your account's preference.

## Armory

The **Armory** tab shows one character at a time: search by name (or click a
name on the Players page), then see the saved gear on a paper-doll with
tooltips, the talents of both specs, the glyphs, the saved stats, and a 3D model
of the character wearing that gear.

**Where the data comes from.** Everything but the 3D model comes from this
server: the characters database (read only) and the game's reference files
(talents, spells, item sets …) from AzerothCore's client-data volume, which the
admin mounts read-only. The 3D model is drawn in your browser by Wowhead's model
viewer, but your browser never talks to Wowhead: the admin fetches each model
file once, keeps it on disk (`data/armory/cache/`, at most 300 MB), and serves
it from there. The first view of a race and gender costs about 3–4 MB from
Wowhead, each new item about 15 KB, and a repeat view nothing. Wowhead sees this
server's internet address and which models and items were asked for — never a
character or account name. Nothing is fetched that no one asked to see.

**Stats.** AzerothCore saves a character's stats only when the setting
`PlayerSave.Stats.MinLevel` is 1 or more (it is 0 by default, which never saves
them). Set it on the **Settings** page; a character's stats then appear after
its next logout. Leave `PlayerSave.Stats.SaveOnlyOnLogout` at 1 (the default).
Until then the Stats tab says so and shows the current value.

**The footer of the Armory page:**

- **3D viewer: On/Off** — Off stops every request to Wowhead; models already in
  the cache still show.
- **Clear cached model data** — deletes the cached model files (they are fetched
  again when needed); it keeps the viewer script.
- **Update** — the viewer script is fetched once and then kept as it is, so a
  change at Wowhead cannot break the page unannounced. Update fetches the
  current one; if the model then stops drawing, **Revert** goes back to the
  previous copy. If the kept viewer script is ever broken and neither Update
  nor Revert helps, delete `data/armory/cache/live/viewer/` (under
  `/opt/stacks/azerothcore-admin/`) and reload the Armory page: the first view
  fetches and keeps a fresh copy.

If a piece of gear shows a small "no 3D" mark, Wowhead has no model for it; the
rest of the character still renders. The Armory's data is all re-fetchable, so
it is not part of the backup archives.

**Deploying it on an older install:** the Armory needs jQuery next to HTMX.
The first `redeploy-azerothcore-admin.sh` fetches it once by itself (from the
same pinned address the installer uses) and keeps it for later redeploys. If
that download fails, the redeploy stops before building anything and prints
the `curl` command to run by hand; run it, then redeploy again.

## Uninstalling

```bash
./scripts/uninstall-azerothcore-admin.sh
```

If Cloud backup was connected, the uninstaller reminds you to revoke the Proton
Drive session in your Proton account's session list (removing the app deletes the
copy stored on this server, not the session at Proton).

When removing everything, uninstall the admin app **before** AzerothCore: the
admin uses AzerothCore's client-data volume, and the AzerothCore uninstaller
cannot remove that volume while the admin is still running.

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
7. **Settings edit + apply**: change `Playerbots.MinRandomBots` to a
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
11. **Customization**: open **Customization**, enter a name, choose a small
   square PNG and Save. The header, the browser tab title
   (`<name> · <page>`) and the tab icon change on every page; a second
   Save with no new image keeps the logo; **Remove logo** + Save brings
   back ⚔. An SVG or a file over 512 KB is refused with a red banner and
   nothing changes.
