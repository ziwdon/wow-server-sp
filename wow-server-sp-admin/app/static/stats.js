// Stats page: manual refresh w/ non-blocking spinner + poll until idle,
// plus reset-bots (simple confirm) and clear-bots (type-CLEAR modal).

function refreshData() {
  const el = document.getElementById('stats-data');
  if (el) htmx.ajax('GET', '/api/stats/data', { target: '#stats-data', swap: 'innerHTML' });
}

let pollTimer = null;
function pollWhileRefreshing() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(function () {
    const inner = document.getElementById('stats-data-inner');
    const status = inner ? inner.dataset.status : 'idle';
    const spin = document.getElementById('stats-spinner');
    if (status === 'refreshing') {
      if (spin) spin.style.display = '';
      refreshData();
      pollWhileRefreshing();
    } else if (spin) {
      spin.style.display = 'none';
    }
  }, 2000);
}

// After each data swap, sync the "last refreshed" label + keep polling if busy.
document.addEventListener('htmx:afterSwap', function (e) {
  if (e.detail.target && e.detail.target.id === 'stats-data') {
    const inner = document.getElementById('stats-data-inner');
    const label = document.getElementById('stats-last-refreshed');
    const spin = document.getElementById('stats-spinner');
    if (inner && label) {
      const card = inner.querySelector('.stats-headline .stat-card:last-child .stat-value');
      label.textContent = card ? ('Last refreshed: ' + card.textContent.trim()) : '—';
    }
    if (inner && inner.dataset.status === 'refreshing') {
      if (spin) spin.style.display = '';
      pollWhileRefreshing();
    } else if (spin) {
      spin.style.display = 'none';
    }
  }
});

document.getElementById('refresh-stats-btn').addEventListener('click', async function () {
  const spin = document.getElementById('stats-spinner');
  if (spin) spin.style.display = '';
  await fetch('/api/stats/refresh', { method: 'POST' });
  refreshData();
  pollWhileRefreshing();
});

// Reset bots — simple confirm.
document.getElementById('reset-bots-btn').addEventListener('click', function () {
  if (!window.confirm(
    'Reset bots? This re-rolls the existing rndbot pool (playerbot rndbot init). ' +
    'No data is deleted. Consider a backup first. Continue?')) return;
  const log = document.getElementById('action-log');
  if (log) log.innerHTML = '';
  fetch('/api/action/reset-bots', { method: 'POST' });
});

// Clear bots — type-CLEAR modal.
const clearDlg = document.getElementById('clear-dialog');
const clearInput = document.getElementById('clear-confirm-input');
const clearConfirm = document.getElementById('clear-confirm-btn');
document.getElementById('clear-bots-btn').addEventListener('click', function () {
  clearInput.value = '';
  clearConfirm.disabled = true;
  clearDlg.showModal();
});
clearInput.addEventListener('input', function () {
  clearConfirm.disabled = (clearInput.value.trim() !== 'CLEAR');
});
document.getElementById('clear-cancel-btn').addEventListener('click', function () {
  clearDlg.close();
});
clearConfirm.addEventListener('click', function () {
  clearDlg.close();
  const log = document.getElementById('action-log');
  if (log) log.innerHTML = '';
  fetch('/api/action/clear-bots', { method: 'POST' });
});

// On SSE done, refresh the stats data so counts reflect post-action state.
document.addEventListener('htmx:sseMessage', function (e) {
  if (e.detail && e.detail.type === 'done') refreshData();
});
