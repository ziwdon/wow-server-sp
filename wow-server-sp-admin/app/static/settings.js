function showBanner(msg) {
  const b = document.createElement('div');
  b.className = 'banner error';
  b.textContent = msg;
  document.body.prepend(b);
  setTimeout(() => b.remove(), 8000);
}

// Escape user/operator-supplied strings before interpolating into innerHTML
// or quoted attributes. Keys come from upstream .conf.dist files (trusted);
// effective values come from admin.yml (operator-set, so technically
// arbitrary) — escape both so a stray quote/angle bracket can't break out.
const ESC_MAP = {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'};
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ESC_MAP[c]);
}

const state = { keys: [], pending: {}, selected: null };

async function load() {
  const r = await fetch('/api/keys');
  state.keys = await r.json();
  render();
}

function hasPending(k) {
  return Object.prototype.hasOwnProperty.call(state.pending, k.key);
}

function isApplied(k) {
  return k.source === 'admin' || k.source === 'installer';
}

function refreshSelectedKey() {
  if (!state.selected) return;
  const fresh = state.keys.find(x => x.key === state.selected.key);
  if (fresh) selectKey(fresh);
  else state.selected = null;
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

function matches(k, q, files, modifiedOnly, pendingOnly) {
  if (!files.has(k.source_file)) return false;
  if (pendingOnly && !hasPending(k)) return false;
  if (modifiedOnly && k.source !== 'admin' && k.source !== 'installer' && !hasPending(k)) return false;
  if (!q) return true;
  const hay = (k.key + ' ' + k.default + ' ' + k.comment + ' ' + k.env_var).toLowerCase();
  return hay.includes(q.toLowerCase());
}

let renderTimer = null;
function render() {
  clearTimeout(renderTimer);
  renderTimer = setTimeout(_render, 150);
}

function _render() {
  const q = document.getElementById('search').value;
  const files = new Set(
    [...document.querySelectorAll('.check-group input[type=checkbox][value]')]
      .filter(c => c.checked).map(c => c.value)
  );
  const modifiedOnly = document.getElementById('only-modified').checked;
  const pendingOnly = document.getElementById('only-pending').checked;
  const filtered = state.keys.filter(k => matches(k, q, files, modifiedOnly, pendingOnly));
  document.getElementById('result-count').textContent = `${filtered.length} keys`;

  const list = document.getElementById('key-list');
  list.innerHTML = '';

  updatePendingControls();

  if (filtered.length === 0) {
    if (pendingOnly) {
      const msg = document.createElement('p');
      msg.className = 'empty-state';
      msg.textContent = 'No pending changes — edit a value to stage it for apply.';
      list.appendChild(msg);
      return;
    }
    if (modifiedOnly) {
      const msg = document.createElement('p');
      msg.className = 'empty-state';
      msg.textContent = 'No modified configurations — uncheck "Show only modified" to browse all keys.';
      list.appendChild(msg);
      return;
    }
  }
  filtered.slice(0, 200).forEach(k => {
    const row = document.createElement('div');
    const readOnly = Boolean(k.read_only);
    const readOnlyReason = k.read_only_reason || 'installer-managed';
    const readOnlyAttrs = readOnly ? ' disabled readonly aria-readonly="true"' : '';
    const readOnlyBadge = readOnly
      ? `<span class="key-badge" title="${esc(readOnlyReason)}">${esc(readOnlyReason)}</span>`
      : '';
    const pending = hasPending(k);
    const applied = isApplied(k);
    const rowClasses = ['key-row', 'source-' + k.source];
    if (readOnly) rowClasses.push('read-only');
    if (pending) rowClasses.push('key-row-pending');
    else if (applied) rowClasses.push('key-row-applied');
    if (state.selected && k.key === state.selected.key) rowClasses.push('selected');
    row.className = rowClasses.join(' ');
    const value = pending ? state.pending[k.key] : k.effective_value;
    const inputClasses = ['key-input'];
    if (pending) inputClasses.push('key-input-pending');
    else if (applied) inputClasses.push('key-input-applied');
    row.innerHTML = `
      <button type="button" class="key-row-select" aria-controls="key-detail">
        <span class="key-name">${esc(k.key)}</span>
        <span class="key-source">${esc(k.source)}</span>
        <span class="key-flags">${readOnlyBadge}</span>
      </button>
      <input class="${inputClasses.join(' ')}" data-key="${esc(k.key)}" aria-label="Value for ${esc(k.key)}" value="${esc(value)}"${readOnlyAttrs}>
    `;
    if (document.getElementById('show-meta') && document.getElementById('show-meta').checked) {
      row.classList.add('show-meta');
    }
    row.querySelector('.key-row-select').addEventListener('click', () => selectKey(k));
    list.appendChild(row);
  });
  if (filtered.length > 200) {
    const more = document.createElement('p');
    more.textContent = `+${filtered.length - 200} more — narrow your search`;
    list.appendChild(more);
  }
}

let mobileDetailTrigger = null;
function closeMobileDetail() {
  const detail = document.getElementById('key-detail');
  detail.classList.remove('mobile-visible');
  detail.removeAttribute('role');
  detail.removeAttribute('aria-modal');
  if (mobileDetailTrigger) mobileDetailTrigger.focus();
  mobileDetailTrigger = null;
  state.selected = null;
  const prev = document.querySelector('.key-row.selected');
  if (prev) prev.classList.remove('selected');
}

function selectKey(k) {
  const prev = document.querySelector('.key-row.selected');
  if (prev) prev.classList.remove('selected');

  state.selected = k;

  const newRow = document.querySelector(`.key-input[data-key="${k.key}"]`)?.closest('.key-row');
  if (newRow) newRow.classList.add('selected');
  const detailButton = newRow?.querySelector('.key-row-select');

  const detail = document.getElementById('key-detail');
  const readOnlyBadge = k.read_only
    ? `<span class="key-badge">${esc(k.read_only_reason || 'installer-managed')}</span>`
    : '';
  const pending = hasPending(k);
  const applied = isApplied(k);
  const effectiveValue = pending ? state.pending[k.key] : k.effective_value;
  const valueClass = pending ? ' detail-value-pending' : (applied ? ' detail-value-applied' : '');
  const sourceText = pending ? 'pending, not applied' : `from ${k.source}`;
  detail.innerHTML = `
    <button class="mobile-back-btn" onclick="closeMobileDetail()">← Back</button>
    <div class="detail-key-name">${esc(k.key)}</div>
    <div class="detail-env-var">${esc(k.env_var)}</div>
    ${readOnlyBadge ? `<div>${readOnlyBadge}</div>` : ''}
    <div class="detail-section">
      <div class="detail-section-label">Effective value</div>
      <div class="detail-section-value${valueClass}">${esc(effectiveValue)} <span class="detail-from">(${esc(sourceText)})</span></div>
    </div>
    <div class="detail-section">
      <div class="detail-section-label">Default</div>
      <div class="detail-section-value">${esc(k.default)} <span class="detail-from">(type: ${esc(k.inferred_type)})</span></div>
    </div>
    <div class="detail-section">
      <div class="detail-section-label">Description</div>
      <div class="detail-comment">${esc(k.comment || '(no comment)')}</div>
    </div>
  `;

  if (window.innerWidth <= 768) {
    mobileDetailTrigger = detailButton;
    detail.setAttribute('role', 'dialog');
    detail.setAttribute('aria-modal', 'true');
    detail.classList.add('mobile-visible');
    detail.querySelector('.mobile-back-btn')?.focus();
  }
}

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && document.getElementById('key-detail').classList.contains('mobile-visible')) closeMobileDetail();
});

document.addEventListener('input', e => {
  if (e.target.classList.contains('key-input')) {
    const key = e.target.dataset.key;
    const k = state.keys.find(x => x.key === key);
    if (!k) return;
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

['search', 'only-modified', 'only-pending'].forEach(id => {
  document.getElementById(id).addEventListener('input', render);
});
document.querySelectorAll('.check-group input[type=checkbox][value]')
  .forEach(c => c.addEventListener('change', render));

document.getElementById('apply-btn').addEventListener('click', async () => {
  const dlg = document.getElementById('apply-dialog');
  const diff = Object.entries(state.pending).map(
    ([k, v]) => {
      const cur = state.keys.find(x => x.key === k);
      return `${k}: ${cur.effective_value} → ${v}`;
    }
  ).join('\n');
  document.getElementById('apply-diff').textContent = diff;
  dlg.showModal();
});

document.getElementById('apply-cancel').addEventListener('click',
  () => document.getElementById('apply-dialog').close());

function actionStatusFromDone(data) {
  const match = /data-status="([^"]+)"/.exec(data);
  return match?.[1] || 'unknown';
}

function watchActionUntilDone(id, label) {
  return new Promise((resolve) => {
    const es = new EventSource(`/api/action/stream?id=${encodeURIComponent(id)}`);
    let settled = false;
    const finish = (status, message) => {
      if (settled) return;
      settled = true;
      es.close();
      if (status !== 'ok') showBanner(message);
      resolve(status !== 'ok');
    };
    es.addEventListener('done', (e) => {
      const status = actionStatusFromDone(e.data);
      finish(status, `${label} finished with ${status} — see action log.`);
    });
    es.addEventListener('idle', () => {
      finish('idle', `${label} action was not found. Refresh Settings before trying again.`);
    });
    es.addEventListener('error', () => {
      finish('stream-error', `${label} action stream disconnected. Refresh Settings to check its result.`);
    });
  });
}

document.getElementById('apply-confirm').addEventListener('click', async () => {
  const button = document.getElementById('apply-confirm');
  document.getElementById('apply-dialog').close();
  button.disabled = true;
  try {
    const result = await window.requestActionJson('/api/settings/apply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pending: state.pending }),
    });
    if (!result.ok) {
      window.showActionFailure('Apply', result);
      return;
    }
    if (!result.data || !result.data.id) {
      window.showActionFailure('Apply', {
        message: 'The server accepted no action id. Refresh Settings and try again.',
      });
      return;
    }
    const { id } = result.data;
    state.pending = {};
    await load();
    const hardError = await watchActionUntilDone(id, 'Apply');
    if (hardError) {
      await load();
      refreshSelectedKey();
    } else {
      window.location.href = '/';
    }
  } finally {
    button.disabled = false;
  }
});

document.getElementById('rollback-btn').addEventListener('click', async () => {
  if (!confirm('Roll back to the most recent admin.yml snapshot and restart?')) return;
  const button = document.getElementById('rollback-btn');
  button.disabled = true;
  try {
    const result = await window.requestActionJson('/api/settings/rollback', { method: 'POST' });
    if (!result.ok) {
      window.showActionFailure('Rollback', result);
      return;
    }
    if (!result.data || !result.data.id) {
      window.showActionFailure('Rollback', {
        message: 'The server accepted no action id. Refresh Settings and try again.',
      });
      return;
    }
    const { id } = result.data;
    await load();
    const hardError = await watchActionUntilDone(id, 'Rollback');
    if (hardError) {
      await load();
      refreshSelectedKey();
    } else {
      window.location.href = '/';
    }
  } finally {
    button.disabled = false;
  }
});

const showMetaEl = document.getElementById('show-meta');
if (showMetaEl) {
  showMetaEl.addEventListener('change', function() {
    const on = this.checked;
    const header = document.getElementById('key-list-header');
    if (header) {
      if (on) {
        header.classList.add('show-meta');
        header.innerHTML = '<span>Key</span><span>Source</span><span>Flags</span><span>Value</span>';
      } else {
        header.classList.remove('show-meta');
        header.innerHTML = '<span>Key</span><span>Value</span>';
      }
    }
    render();
  });
}

// Mobile filter toggle
const mobileFilterToggle = document.getElementById('mobile-filter-toggle');
const sidebarExtra = document.getElementById('sidebar-extra');
if (mobileFilterToggle && sidebarExtra) {
  mobileFilterToggle.addEventListener('click', () => {
    const isOpen = sidebarExtra.classList.toggle('open');
    mobileFilterToggle.setAttribute('aria-expanded', String(isOpen));
    mobileFilterToggle.textContent = isOpen ? '⚙ Filters ▲' : '⚙ Filters ▼';
  });
}

load();
