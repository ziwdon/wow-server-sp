# Admin Settings, Dashboard, and Log Improvements — Design Spec

**Date:** 2026-05-22
**Status:** Approved in chat; awaiting written-spec review
**Author:** Carlos (with Codex)

## Scope

Improve `wow-server-sp-admin` usability and performance in the admin installer,
settings page, and dashboard. All runtime changes stay inside
`wow-server-sp-admin/`; this work does not modify `wow-server-sp/scripts/` or
`/opt/stacks/azerothcore/`.

The app still writes only `docker-compose.admin.yml` and `backups/` at runtime.
Settings changes remain `AC_*` environment-variable overrides in the admin
compose overlay.

## Goals

- Default the admin installer systemd prompt to yes.
- Make the settings value column at least twice as wide without reducing the
  left sidebar width.
- Widen the settings detail panel by about one quarter.
- Keep the settings page itself fixed under the top nav so the selected key
  description remains visible while the key list scrolls internally.
- Visually distinguish already-applied modified settings from pending edits.
- Prevent dashboard Server Activity and Logs panels from growing the whole page
  downward forever.
- Stop loading logs automatically on dashboard page load.
- Reduce log-tail CPU and page blocking by avoiding full-file reads for large
  logs.

## Non-Goals

- No changes to the main AzerothCore installer under `wow-server-sp/scripts/`.
- No writes to `/opt/stacks/azerothcore/` during development.
- No new authentication, log search, pagination, or live log streaming.
- No new frontend framework or build step.
- No change to the Apply/Rollback data model or restart behavior.

## Installer Change

`wow-server-sp-admin/scripts/install-azerothcore-admin.sh` changes only the
optional systemd prompt:

```text
Install azerothcore-admin.service systemd unit (auto-start at boot)? [Y/n]
```

An empty answer is treated as `y`, and explicit `y` still installs/enables the
unit. Explicit `n` or any other answer skips the unit.

## Settings Layout

The settings page keeps the current three-column structure:

```text
[ Sidebar 270px ][ Key list flexible ][ Detail panel about 525px ]
```

The sidebar remains `270px`, preserving source filters and action controls. The
detail panel grows from `420px` to roughly `525px`, about a 25% increase. The
center list absorbs the remaining width.

The key list becomes an internal scroll container with a fixed page-height
budget under the nav. The top-level page uses `min-height: 0`, `height`, and
`overflow: hidden` so scrolling a long configuration list does not move the
whole page or hide the selected key description.

The value column doubles from `140px` to roughly `300px`:

```css
.key-list-header,
.key-row {
  grid-template-columns: minmax(0, 1fr) minmax(240px, 300px);
}

.key-list-header.show-meta,
.key-row.show-meta {
  grid-template-columns: minmax(0, 1fr) 80px 100px minmax(240px, 300px);
}
```

The key column uses `minmax(0, 1fr)` so long key names truncate instead of
forcing horizontal overflow.

## Settings Highlighting

The settings UI distinguishes three states:

- Default/unmodified: current row/input styling.
- Applied override: green foreground/border/background accent when
  `source === "admin"` or `source === "installer"`.
- Pending edit: yellow foreground/border/background accent when the key exists
  in `state.pending`.

Pending styling wins over applied styling. For example, an already-admin-set key
that is edited again shows yellow until Apply finishes and `/api/keys` reloads;
after the reload it shows green as an applied value.

`settings.js` adds row and input classes while rendering:

- `key-row-applied`
- `key-row-pending`
- `key-input-applied`
- `key-input-pending`

The selected key detail panel also reflects pending state. If the selected key
has a pending value, the Effective value section shows the pending value with a
pending label; otherwise it shows the persisted effective value and source.

## Dashboard Layout

The dashboard page remains a flex column under the nav. The lower dashboard
grid receives an explicit internal height budget and `min-height: 0`, allowing
both child panels to scroll internally.

Server Activity keeps the existing SSE wiring and `#action-log` list. The panel
body and list are constrained with `overflow-y: auto` so a long action history
does not increase page height indefinitely.

Logs use the same panel shell but no longer fetch on page load. Initial render
shows a small placeholder and a button labeled `Load latest logs`. Clicking the
button fetches `/api/logs` into `#logs`. Once logs are loaded, the same action
can be used to load the latest log tail again. This avoids page blocking and CPU
spikes for users who open the dashboard only to check status or actions.

## Log Tail Performance

The current log route calls `tail_filtered()`, which uses
`Path.read_text().splitlines()` and scans the entire file. That is expensive for
`Playerbots.log`, which can grow quickly and contain many benign lines.

`app/services/logs.py` changes `tail_filtered()` to read from the end of the
file in bounded chunks. It decodes only the trailing window needed to collect
the requested number of non-benign lines, filters known benign noise, and
returns the last `n` relevant lines in chronological order.

The implementation includes a maximum byte budget so one request cannot scan an
unbounded log file. If the trailing window contains mostly benign noise, the UI
may show fewer than `n` lines rather than blocking the page. That is acceptable
for a dashboard signal panel whose purpose is quick status, not full log
forensics.

## Files Changed

| File | Change |
|---|---|
| `wow-server-sp-admin/scripts/install-azerothcore-admin.sh` | Default systemd unit prompt to yes |
| `wow-server-sp-admin/app/templates/dashboard.html` | Remove log auto-load, add initial on-demand logs placeholder, constrain activity panel |
| `wow-server-sp-admin/app/templates/partials/logs.html` | Rename refresh button to `Load latest logs` and keep tabbed log display |
| `wow-server-sp-admin/app/templates/settings.html` | Keep structure, rely on updated CSS/JS for widths and state styling |
| `wow-server-sp-admin/app/static/app.css` | Settings widths/scrolling/highlights; dashboard panel scroll limits |
| `wow-server-sp-admin/app/static/settings.js` | Add applied/pending classes and pending detail rendering |
| `wow-server-sp-admin/app/services/logs.py` | Bounded reverse-tail implementation |
| `wow-server-sp-admin/tests/test_installer_script.py` | Assert default-yes prompt semantics |
| `wow-server-sp-admin/tests/test_logs.py` | Assert reverse-tail behavior and no full-file read requirement |
| `wow-server-sp-admin/tests/test_settings_ui.py` | Assert applied/pending styling hooks exist |
| `wow-server-sp-admin/tests/test_main.py` | Assert dashboard no longer auto-loads logs |

## Testing

Run focused tests first:

```bash
python -m pytest -q tests/test_installer_script.py tests/test_logs.py tests/test_settings_ui.py tests/test_main.py
```

Then run the full admin test suite in the existing Dockerized test command:

```bash
docker run --rm -v "$(pwd)/wow-server-sp-admin:/src" -w /src python:3.12-slim \
  bash -c "pip install -r requirements-dev.txt -q && python -m pytest -q"
```

Manual checks:

- Open the dashboard and confirm Logs does not load until `Load latest logs` is
  clicked.
- Confirm a long Server Activity list scrolls inside its panel.
- Open Settings, uncheck `Show only modified`, select a key, and scroll the key
  list; the detail panel remains visible.
- Edit a key and confirm it turns yellow while pending.
- Apply the key and confirm it reloads as green.
- Run the admin installer and confirm pressing Enter at the systemd prompt
  installs/enables the unit.

## Risks

- A bounded log tail may show fewer than 20 non-benign lines when the end of a
  large log is dominated by benign filtered noise. This is preferable to
  blocking the dashboard; users can inspect full logs from the host when needed.
- Wider settings columns reduce the center list's key-name space on narrow
  screens. The app is desktop-oriented, and long key names already truncate.
