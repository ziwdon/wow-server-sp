function showBanner(msg) {
  const b = document.createElement('div');
  b.className = 'banner error';
  b.textContent = msg;
  document.body.prepend(b);
  setTimeout(() => b.remove(), 8000);
}

const state = { keys: [], pending: {}, common: new Set(COMMON_KEYS), selected: null };

async function load() {
  const r = await fetch('/api/keys');
  state.keys = await r.json();
  render();
}

function matches(k, q, files, modifiedOnly, showAll) {
  if (!files.has(k.source_file)) return false;
  if (!showAll && !state.common.has(k.key)) return false;
  if (modifiedOnly && k.source !== 'admin' && k.source !== 'installer') return false;
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
    [...document.querySelectorAll('.settings-filters input[type=checkbox][value]')]
      .filter(c => c.checked).map(c => c.value)
  );
  const modifiedOnly = document.getElementById('only-modified').checked;
  const showAll = document.getElementById('show-all').checked;
  const filtered = state.keys.filter(k => matches(k, q, files, modifiedOnly, showAll));
  document.getElementById('result-count').textContent = `${filtered.length} keys`;

  const list = document.getElementById('key-list');
  list.innerHTML = '';
  filtered.slice(0, 200).forEach(k => {
    const row = document.createElement('div');
    row.className = 'key-row source-' + k.source;
    const pending = state.pending[k.key];
    const value = pending !== undefined ? pending : k.effective_value;
    row.innerHTML = `
      <span class="key-name">${k.key}</span>
      <span class="key-source">${k.source}</span>
      <input class="key-input" data-key="${k.key}" value="${value}">
    `;
    row.addEventListener('click', () => selectKey(k));
    list.appendChild(row);
  });
  if (filtered.length > 200) {
    const more = document.createElement('p');
    more.textContent = `+${filtered.length - 200} more — narrow your search`;
    list.appendChild(more);
  }
  document.getElementById('pending-count').textContent = Object.keys(state.pending).length;
  document.getElementById('apply-btn').disabled = Object.keys(state.pending).length === 0;
}

function selectKey(k) {
  state.selected = k;
  const detail = document.getElementById('key-detail');
  detail.innerHTML = `
    <h2>${k.key}</h2>
    <p><strong>${k.env_var}</strong></p>
    <p>Effective: <code>${k.effective_value}</code> (from ${k.source})</p>
    <p>Default: <code>${k.default}</code> (type: ${k.inferred_type})</p>
    <pre>${k.comment || '(no comment)'}</pre>
  `;
}

document.addEventListener('change', e => {
  if (e.target.classList.contains('key-input')) {
    const key = e.target.dataset.key;
    const k = state.keys.find(x => x.key === key);
    if (e.target.value === k.effective_value) {
      delete state.pending[key];
    } else {
      state.pending[key] = e.target.value;
    }
    render();
  }
});

['search', 'only-modified', 'show-all'].forEach(id => {
  document.getElementById(id).addEventListener('input', render);
});
document.querySelectorAll('.settings-filters input[type=checkbox][value]')
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

function watchActionUntilDone(id, label) {
  return new Promise((resolve) => {
    const es = new EventSource(`/api/action/stream?id=${encodeURIComponent(id)}`);
    es.addEventListener('done', (e) => {
      es.close();
      if (/action-error/.test(e.data)) {
        showBanner(`${label} finished with error — see action log.`);
      } else if (/verify-failed/.test(e.data)) {
        showBanner(`${label} applied, but some env vars did not bind. See action log.`);
      }
      resolve();
    });
    es.addEventListener('idle', () => { es.close(); resolve(); });
  });
}

document.getElementById('apply-confirm').addEventListener('click', async () => {
  document.getElementById('apply-dialog').close();
  const r = await fetch('/api/settings/apply', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pending: state.pending }),
  });
  if (!r.ok) {
    showBanner('Apply failed: ' + await r.text());
    return;
  }
  const { id } = await r.json();
  state.pending = {};
  await load();
  await watchActionUntilDone(id, 'Apply');
  await load();
});

document.getElementById('rollback-btn').addEventListener('click', async () => {
  if (!confirm('Roll back to the most recent admin.yml snapshot and restart?')) return;
  const r = await fetch('/api/settings/rollback', { method: 'POST' });
  if (!r.ok) {
    showBanner('Rollback failed: ' + await r.text());
    return;
  }
  const { id } = await r.json();
  await load();
  await watchActionUntilDone(id, 'Rollback');
  await load();
});

load();
