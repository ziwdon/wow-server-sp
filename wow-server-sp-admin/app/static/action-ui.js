// Shared request handling for controls that start or refresh server actions.
(function () {
  async function requestActionJson(url, options) {
    let response;
    try {
      response = await fetch(url, options);
    } catch (_) {
      return {
        ok: false,
        kind: 'network',
        message: 'Server unreachable. Check your connection and try again.',
      };
    }

    let text;
    try {
      text = await response.text();
    } catch (_) {
      return {
        ok: false,
        kind: 'network',
        message: 'Could not read the server response. Check your connection and try again.',
      };
    }

    let data;
    try {
      data = JSON.parse(text);
    } catch (_) {
      return {
        ok: false,
        kind: 'invalid-json',
        status: response.status,
        message: response.ok
          ? 'The server returned an unexpected response. Refresh the page and try again.'
          : `Request failed (HTTP ${response.status}). Refresh the page and try again.`,
      };
    }

    if (!response.ok) {
      const detail = data && typeof data.detail === 'string' ? data.detail : 'request was rejected';
      return {
        ok: false,
        kind: 'http',
        status: response.status,
        message: `Request failed (HTTP ${response.status}): ${detail}. Try again.`,
      };
    }

    return { ok: true, data: data };
  }

  function showActionFailure(label, result) {
    const message = `${label}: ${result.message}`;
    if (window.showActionToast) window.showActionToast(message);
    else window.alert(message);
  }

  window.requestActionJson = requestActionJson;
  window.showActionFailure = showActionFailure;

  document.addEventListener('htmx:sseError', function () {
    showActionFailure('Live activity stream disconnected', {
      message: 'Refresh the page to reconnect; the server action may still be running.',
    });
  });
}());
