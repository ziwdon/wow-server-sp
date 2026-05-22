# Admin Settings Dashboard Log Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved admin installer, settings layout/highlighting, dashboard scroll, and on-demand bounded log-tail improvements.

**Architecture:** Keep the current FastAPI + Jinja2 + HTMX + vanilla JS structure. Fix expensive log work in `app/services/logs.py`, make dashboard logs opt-in at the template layer, and keep settings state styling client-side in `settings.js` with CSS-only visual states.

**Tech Stack:** Bash installer, Python 3.12, FastAPI/Jinja2, HTMX, vanilla JavaScript, CSS, pytest.

---

## File Structure

- Modify `wow-server-sp-admin/scripts/install-azerothcore-admin.sh`: change only the systemd prompt default and fallback value.
- Modify `wow-server-sp-admin/tests/test_installer_script.py`: add a static regression test for default-yes prompt semantics.
- Modify `wow-server-sp-admin/app/services/logs.py`: replace full-file log reading with bounded reverse-tail reading.
- Modify `wow-server-sp-admin/tests/test_logs.py`: preserve existing behavior tests and add a guard that `tail_filtered()` does not call `Path.read_text()` for log content.
- Modify `wow-server-sp-admin/app/templates/dashboard.html`: remove automatic `/api/logs` load and render an on-demand logs panel.
- Modify `wow-server-sp-admin/app/templates/partials/logs.html`: label the log reload button `Load latest logs`.
- Modify `wow-server-sp-admin/app/static/app.css`: constrain dashboard panel heights, widen settings columns, and add applied/pending highlight rules.
- Modify `wow-server-sp-admin/app/static/settings.js`: include pending keys in modified filtering, apply state classes, and update selected-key detail for pending edits.
- Modify `wow-server-sp-admin/tests/test_settings_ui.py`: add source checks for applied/pending JS and CSS hooks.
- Modify `wow-server-sp-admin/tests/test_main.py`: add a dashboard regression test proving logs are not auto-loaded on page load.

## Task 1: Installer Default-Yes Prompt

**Files:**
- Modify: `wow-server-sp-admin/tests/test_installer_script.py`
- Modify: `wow-server-sp-admin/scripts/install-azerothcore-admin.sh`

- [ ] **Step 1: Write the failing installer prompt test**

Add this test to `InstallerScriptTest` in `wow-server-sp-admin/tests/test_installer_script.py`:

```python
    def test_systemd_unit_prompt_defaults_to_yes(self):
        source = INSTALLER.read_text()

        self.assertIn(
            "Install azerothcore-admin.service systemd unit (auto-start at boot)? [Y/n] ",
            source,
        )
        self.assertIn('if [ "${answer:-y}" = "y" ]; then', source)
        self.assertNotIn("[y/N]", source)
        self.assertNotIn("${answer:-n}", source)
```

- [ ] **Step 2: Run the focused test and confirm it fails**

Run:

```bash
cd wow-server-sp-admin
python -m pytest -q tests/test_installer_script.py::InstallerScriptTest::test_systemd_unit_prompt_defaults_to_yes
```

Expected: FAIL because the script still contains `[y/N]` and `${answer:-n}`.

- [ ] **Step 3: Implement the prompt default change**

In `wow-server-sp-admin/scripts/install-azerothcore-admin.sh`, replace:

```bash
read -rp "Install azerothcore-admin.service systemd unit (auto-start at boot)? [y/N] " answer
if [ "${answer:-n}" = "y" ]; then
```

with:

```bash
read -rp "Install azerothcore-admin.service systemd unit (auto-start at boot)? [Y/n] " answer
if [ "${answer:-y}" = "y" ]; then
```

- [ ] **Step 4: Run the focused installer tests**

Run:

```bash
cd wow-server-sp-admin
python -m pytest -q tests/test_installer_script.py
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

Run:

```bash
git add wow-server-sp-admin/tests/test_installer_script.py wow-server-sp-admin/scripts/install-azerothcore-admin.sh
git commit -m "fix(admin): default systemd installer prompt to yes"
```

## Task 2: Bounded Reverse Log Tail

**Files:**
- Modify: `wow-server-sp-admin/tests/test_logs.py`
- Modify: `wow-server-sp-admin/app/services/logs.py`

- [ ] **Step 1: Write the failing bounded-tail test**

Add imports and this test to `wow-server-sp-admin/tests/test_logs.py`:

```python
from pathlib import Path
```

```python
def test_tail_filtered_does_not_read_entire_file(tmp_path, monkeypatch):
    p = tmp_path / "Playerbots.log"
    p.write_text(
        "old important line\n"
        + "\n".join(f"benign filler {i} A:follow - FAILED" for i in range(2000))
        + "\nrecent line 1\nrecent line 2\n"
    )

    def fail_read_text(self, *args, **kwargs):
        if self == p:
            raise AssertionError("tail_filtered must not read the whole log file")
        return Path.read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_read_text)

    assert tail_filtered(p, n=2, max_bytes=4096) == ["recent line 1", "recent line 2"]
```

- [ ] **Step 2: Run the focused log tests and confirm failure**

Run:

```bash
cd wow-server-sp-admin
python -m pytest -q tests/test_logs.py
```

Expected: FAIL because `tail_filtered()` currently calls `Path.read_text()`.

- [ ] **Step 3: Implement bounded reverse-tail reading**

Replace `tail_filtered()` in `wow-server-sp-admin/app/services/logs.py` with this implementation and remove the unused `deque` import:

```python
def tail_filtered(
    path: Path,
    n: int = 20,
    *,
    chunk_size: int = 8192,
    max_bytes: int = 1024 * 1024,
) -> list[str]:
    """Return up to n trailing non-benign lines without scanning huge logs."""
    if not path.exists():
        return []

    chunks: list[bytes] = []
    bytes_read = 0
    with path.open("rb") as f:
        f.seek(0, 2)
        offset = f.tell()

        while offset > 0 and bytes_read < max_bytes:
            read_size = min(chunk_size, offset, max_bytes - bytes_read)
            offset -= read_size
            f.seek(offset)
            chunks.append(f.read(read_size))
            bytes_read += read_size

            lines = _decode_tail(chunks, offset)
            keep = [line for line in lines if not _is_benign(line)]
            if len(keep) >= n:
                return keep[-n:]

    lines = _decode_tail(chunks, 0)
    keep = [line for line in lines if not _is_benign(line)]
    return keep[-n:]
```

Add this helper above `tail_filtered()`:

```python
def _decode_tail(chunks: list[bytes], offset: int) -> list[str]:
    data = b"".join(reversed(chunks))
    lines = data.decode("utf-8", errors="replace").splitlines()
    if offset > 0 and lines:
        return lines[1:]
    return lines
```

- [ ] **Step 4: Run the focused log tests**

Run:

```bash
cd wow-server-sp-admin
python -m pytest -q tests/test_logs.py
```

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

Run:

```bash
git add wow-server-sp-admin/tests/test_logs.py wow-server-sp-admin/app/services/logs.py
git commit -m "perf(admin): bound dashboard log tail reads"
```

## Task 3: Dashboard On-Demand Logs and Scroll Limits

**Files:**
- Modify: `wow-server-sp-admin/tests/test_main.py`
- Modify: `wow-server-sp-admin/app/templates/dashboard.html`
- Modify: `wow-server-sp-admin/app/templates/partials/logs.html`
- Modify: `wow-server-sp-admin/app/static/app.css`

- [ ] **Step 1: Write the failing dashboard log-load test**

Add this test to `wow-server-sp-admin/tests/test_main.py`:

```python
def test_dashboard_logs_are_loaded_on_demand():
    client = TestClient(app)
    resp = client.get("/")

    assert resp.status_code == 200
    assert "Load latest logs" in resp.text
    assert 'id="logs" class="panel" hx-get="/api/logs" hx-trigger="load"' not in resp.text
```

- [ ] **Step 2: Run the focused dashboard test and confirm failure**

Run:

```bash
cd wow-server-sp-admin
python -m pytest -q tests/test_main.py::test_dashboard_logs_are_loaded_on_demand
```

Expected: FAIL because the dashboard still auto-loads `/api/logs` on `load`.

- [ ] **Step 3: Make dashboard logs on-demand**

In `wow-server-sp-admin/app/templates/dashboard.html`, replace the `#logs` panel with:

```html
    <div id="logs" class="panel">
      <div class="panel-header">
        <span class="panel-title">Logs</span>
        <button class="btn btn-sm"
                hx-get="/api/logs"
                hx-target="#logs"
                hx-swap="innerHTML">Load latest logs</button>
      </div>
      <div class="panel-body log-empty">
        Logs are loaded on demand to avoid blocking the dashboard.
      </div>
    </div>
```

Add `activity-panel` to the Server Activity panel:

```html
    <div class="panel activity-panel">
```

In `wow-server-sp-admin/app/templates/partials/logs.html`, change the button text:

```html
  <button class="btn btn-sm"
          hx-get="/api/logs" hx-target="#logs" hx-swap="innerHTML">Load latest logs</button>
```

- [ ] **Step 4: Add dashboard scroll CSS**

Update `wow-server-sp-admin/app/static/app.css` dashboard rules:

```css
.dashboard-content {
  flex: 1;
  height: calc(100vh - 52px);
  padding: 1.25rem 1.5rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
  min-height: 0;
  overflow: hidden;
}

.lower-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 340px;
  gap: 0.75rem;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.lower-grid > .panel { min-height: 0; }
.activity-panel .panel-body { min-height: 0; overflow-y: auto; }

.log-empty {
  color: var(--text-muted);
  font-size: 0.85rem;
  align-items: center;
  justify-content: center;
}

#action-log {
  list-style: none;
  font-family: 'Consolas', 'Menlo', monospace;
  font-size: 0.8rem;
  min-height: 0;
}
```

- [ ] **Step 5: Run focused dashboard test**

Run:

```bash
cd wow-server-sp-admin
python -m pytest -q tests/test_main.py
```

Expected: PASS.

- [ ] **Step 6: Commit Task 3**

Run:

```bash
git add wow-server-sp-admin/tests/test_main.py wow-server-sp-admin/app/templates/dashboard.html wow-server-sp-admin/app/templates/partials/logs.html wow-server-sp-admin/app/static/app.css
git commit -m "fix(admin): load dashboard logs on demand"
```

## Task 4: Settings Widths, Internal Scrolling, and Highlights

**Files:**
- Modify: `wow-server-sp-admin/tests/test_settings_ui.py`
- Modify: `wow-server-sp-admin/app/static/app.css`
- Modify: `wow-server-sp-admin/app/static/settings.js`

- [ ] **Step 1: Write the failing settings UI hook test**

Replace `wow-server-sp-admin/tests/test_settings_ui.py` with:

```python
from pathlib import Path


ADMIN_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ADMIN_ROOT / rel).read_text()


def test_settings_js_renders_read_only_keys_disabled_and_uneditable():
    script = _read("app/static/settings.js")

    assert "k.read_only" in script
    assert "key-badge" in script
    assert "installer-managed" in script
    assert "disabled readonly" in script
    assert "if (k.read_only)" in script


def test_settings_js_marks_applied_and_pending_states():
    script = _read("app/static/settings.js")

    assert "key-row-applied" in script
    assert "key-row-pending" in script
    assert "key-input-applied" in script
    assert "key-input-pending" in script
    assert "Object.prototype.hasOwnProperty.call(state.pending, k.key)" in script
    assert "pending, not applied" in script


def test_settings_css_widens_value_and_detail_columns():
    css = _read("app/static/app.css")

    assert "grid-template-columns: 270px minmax(0, 1fr) 525px" in css
    assert "minmax(240px, 300px)" in css
    assert ".key-input-pending" in css
    assert ".key-input-applied" in css
```

- [ ] **Step 2: Run the focused settings UI tests and confirm failure**

Run:

```bash
cd wow-server-sp-admin
python -m pytest -q tests/test_settings_ui.py
```

Expected: FAIL because applied/pending hooks and widened settings CSS are not present.

- [ ] **Step 3: Update settings CSS layout**

In `wow-server-sp-admin/app/static/app.css`, add these root tokens:

```css
  --yellow:      #e0c45a;
  --yellow-bg:   #2a2410;
```

Update the settings page and list columns:

```css
.settings-page {
  flex: 1;
  height: calc(100vh - 52px);
  display: grid;
  grid-template-columns: 270px minmax(0, 1fr) 525px;
  min-height: 0;
  overflow: hidden;
}

.key-list-header {
  grid-template-columns: minmax(0, 1fr) minmax(240px, 300px);
}
.key-list-header.show-meta {
  grid-template-columns: minmax(0, 1fr) 80px 100px minmax(240px, 300px);
}
.key-row {
  grid-template-columns: minmax(0, 1fr) minmax(240px, 300px);
}
.key-row.show-meta {
  grid-template-columns: minmax(0, 1fr) 80px 100px minmax(240px, 300px);
}
```

Add highlight rules:

```css
.key-row-applied .key-name { color: var(--green); }
.key-row-pending .key-name { color: var(--yellow); }
.key-input-applied {
  border-color: #2a6a1a;
  background: #0e1a0a;
  color: var(--green);
}
.key-input-pending {
  border-color: var(--yellow);
  background: var(--yellow-bg);
  color: var(--yellow);
}
.detail-value-applied { color: var(--green); }
.detail-value-pending { color: var(--yellow); }
```

- [ ] **Step 4: Update settings JS state rendering**

In `wow-server-sp-admin/app/static/settings.js`, add helpers after `matches()`:

```javascript
function hasPending(k) {
  return Object.prototype.hasOwnProperty.call(state.pending, k.key);
}

function isApplied(k) {
  return k.source === 'admin' || k.source === 'installer';
}

function updatePendingControls() {
  const pendingCount = Object.keys(state.pending).length;
  const badge = document.getElementById('pending-count');
  if (badge) {
    badge.textContent = pendingCount;
    badge.style.display = pendingCount > 0 ? '' : 'none';
  }
  document.getElementById('apply-btn').disabled = pendingCount === 0;
}
```

Change the modified-only guard in `matches()` to include pending keys:

```javascript
  if (modifiedOnly && k.source !== 'admin' && k.source !== 'installer' && !hasPending(k)) return false;
```

In `_render()`, replace the inline pending-count block with:

```javascript
  updatePendingControls();
```

In the row render loop, use these class decisions:

```javascript
    const pending = hasPending(k);
    const applied = isApplied(k);
    const rowClasses = ['key-row', 'source-' + k.source];
    if (readOnly) rowClasses.push('read-only');
    if (pending) rowClasses.push('key-row-pending');
    else if (applied) rowClasses.push('key-row-applied');
    row.className = rowClasses.join(' ');
    const value = pending ? state.pending[k.key] : k.effective_value;
    const inputClasses = ['key-input'];
    if (pending) inputClasses.push('key-input-pending');
    else if (applied) inputClasses.push('key-input-applied');
```

Render the input with the computed class:

```javascript
      <input class="${inputClasses.join(' ')}" data-key="${esc(k.key)}" value="${esc(value)}"${readOnlyAttrs}>
```

In `selectKey(k)`, compute pending detail state:

```javascript
  const pending = hasPending(k);
  const applied = isApplied(k);
  const effectiveValue = pending ? state.pending[k.key] : k.effective_value;
  const valueClass = pending ? ' detail-value-pending' : (applied ? ' detail-value-applied' : '');
  const sourceText = pending ? 'pending, not applied' : `from ${k.source}`;
```

Render the Effective value line with:

```javascript
      <div class="detail-section-value${valueClass}">${esc(effectiveValue)} <span class="detail-from">(${esc(sourceText)})</span></div>
```

Replace the document `change` listener with an `input` listener so pending color appears as soon as the value changes:

```javascript
document.addEventListener('input', e => {
  if (e.target.classList.contains('key-input')) {
    const key = e.target.dataset.key;
    const k = state.keys.find(x => x.key === key);
    if (k.read_only) {
      delete state.pending[key];
      e.target.value = k.effective_value;
      return;
    }
    if (e.target.value === k.effective_value) {
      delete state.pending[key];
    } else {
      state.pending[key] = e.target.value;
    }
    const row = e.target.closest('.key-row');
    const pending = hasPending(k);
    const applied = isApplied(k);
    if (row) {
      row.classList.toggle('key-row-pending', pending);
      row.classList.toggle('key-row-applied', !pending && applied);
    }
    e.target.classList.toggle('key-input-pending', pending);
    e.target.classList.toggle('key-input-applied', !pending && applied);
    updatePendingControls();
    if (state.selected && state.selected.key === key) selectKey(k);
  }
});
```

- [ ] **Step 5: Run focused settings UI tests**

Run:

```bash
cd wow-server-sp-admin
python -m pytest -q tests/test_settings_ui.py
```

Expected: PASS.

- [ ] **Step 6: Commit Task 4**

Run:

```bash
git add wow-server-sp-admin/tests/test_settings_ui.py wow-server-sp-admin/app/static/app.css wow-server-sp-admin/app/static/settings.js
git commit -m "feat(admin): highlight settings override states"
```

## Task 5: Full Verification and Review

**Files:**
- Review all changed files under `wow-server-sp-admin/`

- [ ] **Step 1: Run focused regression tests**

Run:

```bash
cd wow-server-sp-admin
python -m pytest -q tests/test_installer_script.py tests/test_logs.py tests/test_settings_ui.py tests/test_main.py
```

Expected: PASS.

- [ ] **Step 2: Run full Dockerized admin test suite**

Run from repo root:

```bash
docker run --rm -v "$(pwd)/wow-server-sp-admin:/src" -w /src python:3.12-slim bash -c "pip install -r requirements-dev.txt -q && python -m pytest -q"
```

Expected: PASS. Pip root/new-version notices and known Starlette deprecation warnings are acceptable if tests pass.

- [ ] **Step 3: Review the diff for forbidden paths**

Run:

```bash
git diff --stat HEAD~4..HEAD
git diff --name-only HEAD~4..HEAD
```

Expected: changed paths are limited to `wow-server-sp-admin/` and Superpowers docs. There must be no changes under `scripts/` and no `/opt/stacks/azerothcore/` edits.

- [ ] **Step 4: Commit any final fixes**

If verification uncovered small fixes, commit them:

```bash
git add wow-server-sp-admin
git commit -m "fix(admin): polish settings dashboard improvements"
```

If no final fixes were needed, do not create an empty commit.

- [ ] **Step 5: Summarize verification**

Record:

```text
Focused tests: PASS
Full Dockerized test suite: PASS
Forbidden path review: PASS
```
