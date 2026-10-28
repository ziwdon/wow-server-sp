// Selection state for the backup list + restore POST.
window.selectedBackup = null;

function clearActionLog() {
  var log = document.getElementById('action-log');
  if (log) log.innerHTML = '';
}

async function requestBackupAction(button, label, url, options) {
  button.disabled = true;
  try {
    var result = await window.requestActionJson(url, options);
    if (!result.ok) {
      window.showActionFailure(label, result);
      return null;
    }
    if (!result.data || !result.data.id) {
      window.showActionFailure(label, {
        message: 'The server accepted no action id. Refresh the page and try again.',
      });
      return null;
    }
    clearActionLog();
    return result.data.id;
  } finally {
    button.disabled = false;
  }
}

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
  btn.addEventListener('click', async function (e) {
    e.preventDefault();
    if (!window.selectedBackup) return;
    if (!window.confirm(confirmMsg)) return;
    await requestBackupAction(btn, 'Restore', '/api/action/restore', {
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

// Import & Restore: file picker → confirm → multipart upload → restore action.
document.addEventListener('DOMContentLoaded', function () {
  var importBtn = document.getElementById('import-restore-btn');
  var fileInput = document.getElementById('import-file-input');
  if (!importBtn || !fileInput) return;

  var confirmMsg = (
    'IMPORT AND RESTORE this backup file?\n\n' +
    'The whole server is rolled back to the point the backup was taken — ' +
    'characters, items, gold, auctions, and accounts created after it will be ' +
    'permanently LOST. The server will be stopped and unavailable for several ' +
    'minutes while databases reimport (including the large world DB).\n\n' +
    'A pre-restore safety backup is taken first so this can be undone. Continue?'
  );

  importBtn.addEventListener('click', function () {
    fileInput.value = '';
    fileInput.click();
  });

  fileInput.addEventListener('change', async function () {
    var file = fileInput.files[0];
    if (!file) return;
    if (!window.confirm(confirmMsg)) return;
    var formData = new FormData();
    formData.append('file', file);
    await requestBackupAction(importBtn, 'Import and restore', '/api/action/import-restore', {
      method: 'POST', body: formData,
    });
  });
});

// A backup action follows the same acceptance rule as restore/import: don't
// discard the active history until the server returns its new action id.
document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('[data-action-endpoint]').forEach(function (button) {
    button.addEventListener('click', async function () {
      var confirmMsg = button.dataset.actionConfirm;
      if (confirmMsg && !window.confirm(confirmMsg)) return;
      await requestBackupAction(
        button,
        button.dataset.actionLabel,
        button.dataset.actionEndpoint,
        { method: 'POST' },
      );
    });
  });
});
document.addEventListener('htmx:sseMessage', function (e) {
  if (e.detail && e.detail.type === 'done') {
    ['backups-summary', 'backups-list'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) htmx.trigger(el, 'refresh');
    });
    window.selectedBackup = null;
    var btn = document.getElementById('restore-btn');
    if (btn) btn.disabled = true;
  }
});

// --- Cloud backup panel (the panel itself is server-rendered and swapped by htmx) ---

// Copy the Proton sign-in link. The admin is usually served over plain http, where
// navigator.clipboard does not exist, so fall back to a hidden textarea + execCommand.
function cloudCopyLink(button) {
  var text = button.getAttribute('data-copy') || '';
  function report(ok) {
    button.textContent = ok ? 'Copied' : 'Copy failed: select the link instead';
  }
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(function () { report(true); }, function () { report(false); });
    return;
  }
  var area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.position = 'fixed';
  area.style.opacity = '0';
  document.body.appendChild(area);
  area.select();
  var ok = false;
  try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
  document.body.removeChild(area);
  report(ok);
}
window.cloudCopyLink = cloudCopyLink;

// The summary card and the "In Drive" badges follow the cloud panel: refresh them after an
// action and when a polled sign-in or run has finished.
var cloudWasPolling = false;
document.addEventListener('htmx:afterSwap', function (e) {
  var target = e.detail && e.detail.target;
  if (!target) return;
  if (target.id === 'cloud-panel') {
    var panel = document.getElementById('cloud-panel');
    var polling = !!(panel && panel.hasAttribute('hx-trigger'));
    var verb = e.detail.requestConfig && e.detail.requestConfig.verb;
    if (verb === 'post' || (cloudWasPolling && !polling)) {
      ['backups-summary', 'backups-list'].forEach(function (id) {
        var el = document.getElementById(id);
        if (el) htmx.trigger(el, 'refresh');
      });
    }
    cloudWasPolling = polling;
  } else if (target.id === 'backups-list' && window.selectedBackup) {
    // A re-rendered list must not leave an invisible selection behind.
    var kept = null;
    document.querySelectorAll('.backup-row').forEach(function (row) {
      if (row.dataset.archive === window.selectedBackup) kept = row;
    });
    if (kept) {
      kept.classList.add('selected');
    } else {
      window.selectedBackup = null;
      var restore = document.getElementById('restore-btn');
      if (restore) restore.disabled = true;
    }
  }
});
