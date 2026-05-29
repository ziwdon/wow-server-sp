// Selection state for the backup list + restore POST.
window.selectedBackup = null;

function selectBackup(li) {
  document.querySelectorAll('.backup-row').forEach(function (r) { r.classList.remove('selected'); });
  li.classList.add('selected');
  window.selectedBackup = li.dataset.archive;
  var btn = document.getElementById('restore-btn');
  if (btn) btn.disabled = false;
}

// Restore via fetch (JSON body) avoids needing an htmx JSON extension.
document.addEventListener('DOMContentLoaded', function () {
  var btn = document.getElementById('restore-btn');
  if (!btn) return;
  var confirmMsg = btn.getAttribute('hx-confirm') || 'Restore this backup?';
  btn.addEventListener('click', function (e) {
    e.preventDefault();
    if (!window.selectedBackup) return;
    if (!window.confirm(confirmMsg)) return;
    var log = document.getElementById('action-log');
    if (log) log.innerHTML = '';
    fetch('/api/action/restore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ archive: window.selectedBackup }),
    });
  });
  // Remove htmx attrs so htmx doesn't also fire a form-encoded POST.
  btn.removeAttribute('hx-post');
  btn.removeAttribute('hx-vals');
  btn.removeAttribute('hx-ext');
  btn.removeAttribute('hx-confirm');
});

// Clear the action log when Create backup fires; refresh the list when an action finishes.
document.addEventListener('htmx:afterRequest', function (e) {
  if (e.detail.elt && e.detail.elt.closest && e.detail.elt.closest('.action-bar')) {
    var log = document.getElementById('action-log');
    if (log) log.innerHTML = '';
  }
});
document.addEventListener('htmx:sseMessage', function (e) {
  if (e.detail && e.detail.type === 'done') {
    var el = document.getElementById('backups-list');
    if (el) htmx.trigger(el, 'refresh');
    window.selectedBackup = null;
    var btn = document.getElementById('restore-btn');
    if (btn) btn.disabled = true;
  }
});
