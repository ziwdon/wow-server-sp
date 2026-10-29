/* Armory character page (spec 5, 7): the Talents/Glyphs/Stats tabs, the item and talent
 * tooltips and the 3D model panel. Loaded only by armory.html, and only with a character
 * on the page.
 *
 * The armory templates hold no inline script and no inline event handler (so the page's CSP
 * can forbid them): every handler is attached here. Page data comes from the JSON blob
 * <script type="application/json" id="armory-data">; the model panel asks the model API
 * (GET /api/armory/<guid>/model) and draws with Wowhead's viewer script, which reaches the
 * model data only through the admin's allow-listed proxy under /armory/assets/.
 *
 * Text from the blob or the API is always set with textContent, never parsed as HTML.
 */
(function () {
  'use strict';

  const VIEWER_SCRIPT = '/armory/assets/live/viewer/viewer.min.js';
  const JQUERY_SCRIPT = '/static/jquery.min.js'; // armory.html loads it before this script
  const CONTENT_PATH = '/armory/assets/wrath/';
  // A <link> to another origin (absolute or protocol-relative href) in HTML that jQuery parses.
  const FOREIGN_LINK = /<link\b[^>]*\bhref\s*=\s*["']?(?:[a-z][a-z0-9+.-]*:)?\/\/[^>]*>/gi;
  const WATCHDOG_MS = 30000; // spec 7.5: nothing drawn within 30 s -> "unavailable"
  const DRAWN_POLL_MS = 500;
  const MARGIN = 8; // tooltip gap to its trigger and to the viewport edge (px)
  const PROBE_W = 24; // canvas sample size for the "drawn" check
  const PROBE_H = 32;
  const TABARD = 19; // INVTYPE_TABARD: what "Hide tabard" leaves out of the viewer

  const root = document.querySelector('.armory-character');
  if (!root) return;

  const blob = readBlob();

  function readBlob() {
    const el = document.getElementById('armory-data');
    try {
      const data = JSON.parse(el ? el.textContent : 'null');
      if (data && typeof data === 'object') return data;
    } catch (e) {
      /* no tooltips then; everything else still works */
    }
    return { tooltips: {}, itemsBySlot: {} };
  }

  function describe(value) {
    if (value instanceof Error) return value.message;
    if (value && typeof value === 'object') {
      try {
        return JSON.stringify(value).slice(0, 200);
      } catch (e) {
        return String(value);
      }
    }
    return String(value);
  }

  function list(value) {
    return Array.isArray(value) ? value : [];
  }

  // --- icons: one that fails to load becomes the empty-slot silhouette ---

  function iconFailed(img) {
    const empty = document.createElement('span');
    empty.className = 'armory-icon armory-icon--empty';
    empty.setAttribute('aria-hidden', 'true');
    img.replaceWith(empty);
  }

  // Capture phase: a resource "error" event does not bubble. Icons that failed before this
  // deferred script ran are found by the scan right after.
  document.addEventListener('error', (event) => {
    const target = event.target;
    if (target instanceof HTMLImageElement && target.hasAttribute('data-armory-icon')) iconFailed(target);
  }, true);
  root.querySelectorAll('img[data-armory-icon]').forEach((img) => {
    if (img.complete && img.naturalWidth === 0) iconFailed(img);
  });

  // --- tooltips: one div[role=tooltip]; hover on desktop, tap-to-open / tap-outside on touch ---

  const tooltip = (function () {
    const el = document.getElementById('armory-tooltip');
    if (!el) return { hide() {} };
    let owner = null; // the trigger the tooltip belongs to
    let mode = null; // 'hover' | 'focus' | 'tap'
    let lastPointer = { type: '', at: 0 };

    const recentPointer = () => Date.now() - lastPointer.at < 800;
    const hovering = (event) => event.pointerType === 'mouse' || event.pointerType === 'pen';

    function triggerOf(node) {
      const trigger = node && node.closest ? node.closest('[data-tip-slot], [data-tip-title]') : null;
      return trigger && root.contains(trigger) ? trigger : null;
    }

    function line(text, cls, color) {
      const div = document.createElement('div');
      div.className = 'tt-line' + (cls ? ' ' + cls : '');
      div.textContent = String(text);
      if (color) div.style.color = color;
      return div;
    }

    function pair(left, right) {
      const div = document.createElement('div');
      div.className = 'tt-line tt-pair';
      const a = document.createElement('span');
      a.textContent = left || '';
      const b = document.createElement('span');
      b.textContent = right || '';
      div.append(a, b);
      return div;
    }

    // An item's WoW-style tooltip from the blob (app.services.armory.Tooltip as JSON).
    function itemContent(tip) {
      if (!tip || typeof tip !== 'object') return null;
      const f = document.createDocumentFragment();
      f.append(line(tip.name, 'tt-name', tip.quality_color));
      if (tip.ilvl) f.append(line('Item Level ' + tip.ilvl, 'tt-yellow'));
      if (tip.binding) f.append(line(tip.binding));
      if (tip.slot_name || tip.type_name) f.append(pair(tip.slot_name, tip.type_name));
      if (tip.damage) f.append(pair(tip.damage, tip.speed));
      if (tip.dps) f.append(line('(' + tip.dps + ' damage per second)'));
      if (tip.armor) f.append(line(tip.armor + ' Armor'));
      for (const stat of list(tip.stats)) f.append(line(stat));
      for (const stat of list(tip.suffix_stats)) f.append(line(stat));
      for (const res of list(tip.resistances)) f.append(line(res));
      if (tip.enchant) f.append(line(tip.enchant, 'tt-green'));
      for (const socket of list(tip.sockets)) {
        if (socket && socket.gem_text) f.append(line(socket.gem_text));
        else f.append(line(((socket && socket.color) || 'Prismatic') + ' Socket', 'tt-dim'));
      }
      if (tip.socket_bonus) {
        f.append(line('Socket Bonus: ' + tip.socket_bonus, tip.socket_bonus_active ? 'tt-green' : 'tt-dim'));
      }
      if (tip.required_level > 1) f.append(line('Requires Level ' + tip.required_level));
      for (const equip of list(tip.equips)) f.append(line(equip, 'tt-green'));
      const set = tip.set_block;
      if (set && typeof set === 'object') {
        f.append(line(set.name + ' (' + set.have + '/' + (set.total || '?') + ')', 'tt-yellow tt-gap'));
        for (const bonus of list(set.bonuses)) {
          const need = Number(list(bonus)[0]);
          f.append(line('(' + need + ') Set: ' + list(bonus)[1], set.have >= need ? 'tt-green' : 'tt-dim'));
        }
      }
      if (tip.description) f.append(line('"' + tip.description + '"', 'tt-yellow tt-gap'));
      return f;
    }

    // A talent's tooltip: name, rank and the spell's description verbatim (spec 5).
    function talentContent(trigger) {
      const f = document.createDocumentFragment();
      f.append(line(trigger.dataset.tipTitle || '', 'tt-name'));
      if (trigger.dataset.tipRank) f.append(line(trigger.dataset.tipRank, 'tt-dim'));
      if (trigger.dataset.tipDesc) f.append(line(trigger.dataset.tipDesc, 'tt-yellow tt-gap'));
      return f;
    }

    function clamp(value, low, high) {
      return Math.max(low, Math.min(value, Math.max(low, high)));
    }

    // Beside the trigger (right, else left), else below/above it; always inside the viewport.
    function place(trigger) {
      const r = trigger.getBoundingClientRect();
      const vw = document.documentElement.clientWidth;
      const vh = document.documentElement.clientHeight;
      el.style.left = '0px';
      el.style.top = '0px';
      const w = el.offsetWidth;
      const h = el.offsetHeight;
      let left;
      let top = r.top;
      if (r.right + MARGIN + w <= vw - MARGIN) {
        left = r.right + MARGIN;
      } else if (r.left - MARGIN - w >= MARGIN) {
        left = r.left - MARGIN - w;
      } else {
        left = r.left;
        top = r.bottom + MARGIN;
        if (top + h > vh - MARGIN) top = r.top - MARGIN - h;
      }
      el.style.left = clamp(left, MARGIN, vw - MARGIN - w) + 'px';
      el.style.top = clamp(top, MARGIN, vh - MARGIN - h) + 'px';
    }

    function show(trigger, how) {
      const content = trigger.hasAttribute('data-tip-slot')
        ? itemContent((blob.tooltips || {})[trigger.dataset.tipSlot])
        : talentContent(trigger);
      if (!content) {
        hide();
        return;
      }
      if (owner && owner !== trigger) owner.removeAttribute('aria-describedby');
      el.replaceChildren(content);
      el.hidden = false;
      el.style.visibility = 'hidden';
      place(trigger);
      el.style.visibility = '';
      owner = trigger;
      mode = how;
      trigger.setAttribute('aria-describedby', el.id);
    }

    function hide() {
      if (owner) owner.removeAttribute('aria-describedby');
      owner = null;
      mode = null;
      el.hidden = true;
      el.replaceChildren();
    }

    document.addEventListener('pointerdown', (event) => {
      lastPointer = { type: event.pointerType, at: Date.now() };
    }, true);
    root.addEventListener('pointerover', (event) => {
      if (!hovering(event)) return;
      const trigger = triggerOf(event.target);
      if (trigger && trigger !== owner) show(trigger, 'hover');
    });
    root.addEventListener('pointerout', (event) => {
      if (!hovering(event) || mode !== 'hover' || !owner) return;
      if (triggerOf(event.target) !== owner) return;
      if (event.relatedTarget && owner.contains(event.relatedTarget)) return;
      hide();
    });
    root.addEventListener('focusin', (event) => {
      if (recentPointer()) return; // a press focuses too; its click decides
      const trigger = triggerOf(event.target);
      if (trigger) show(trigger, 'focus');
    });
    root.addEventListener('focusout', (event) => {
      if (mode === 'focus' && triggerOf(event.target) === owner) hide();
    });
    document.addEventListener('click', (event) => {
      const trigger = triggerOf(event.target);
      if (trigger) {
        if (lastPointer.type === 'mouse' && recentPointer()) return; // hovering shows it already
        if (trigger === owner && mode === 'tap') hide();
        else show(trigger, 'tap');
        return;
      }
      if (owner && !el.contains(event.target)) hide(); // tap (or click) outside closes
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') hide();
    });
    // The tooltip is position: fixed; any scroll (capture: scroll does not bubble) or resize
    // would leave it beside nothing.
    document.addEventListener('scroll', (event) => {
      if (owner && !(event.target instanceof Node && el.contains(event.target))) hide();
    }, true);
    window.addEventListener('resize', () => {
      if (owner) hide();
    });

    return { hide };
  })();

  // --- tabs (WAI-ARIA tabs: click, or arrows/Home/End move and activate) ---

  function tabsOf(tablist) {
    return Array.from(tablist.querySelectorAll('[role="tab"]'))
      .filter((tab) => tab.closest('[role="tablist"]') === tablist);
  }

  function selectTab(tab) {
    const tablist = tab.closest('[role="tablist"]');
    if (!tablist) return;
    for (const t of tabsOf(tablist)) {
      const on = t === tab;
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      t.tabIndex = on ? 0 : -1;
      const panel = document.getElementById(t.getAttribute('aria-controls'));
      if (panel) panel.hidden = !on;
    }
    tooltip.hide();
  }

  root.addEventListener('click', (event) => {
    const tab = event.target.closest('[role="tab"]');
    if (tab && root.contains(tab)) selectTab(tab);
  });
  root.addEventListener('keydown', (event) => {
    const tab = event.target.closest('[role="tab"]');
    if (!tab || !root.contains(tab)) return;
    const tabs = tabsOf(tab.closest('[role="tablist"]'));
    const i = tabs.indexOf(tab);
    let next;
    if (event.key === 'ArrowRight') next = tabs[(i + 1) % tabs.length];
    else if (event.key === 'ArrowLeft') next = tabs[(i - 1 + tabs.length) % tabs.length];
    else if (event.key === 'Home') next = tabs[0];
    else if (event.key === 'End') next = tabs[tabs.length - 1];
    else return;
    event.preventDefault();
    selectTab(next);
    next.focus();
  });

  // --- viewer error capture (while a model loads) ---
  //
  // The 30 s watchdog reports what went wrong: the viewer's own error text and, where the
  // browser tells (Resource Timing responseStatus, Chromium), the asset path that failed.
  // The viewer logs "Error loading metadata: error, Not Found" through console.log (spike
  // run0), so console.log is watched too, for error-looking lines only.

  function errorCapture() {
    let active = false;
    let saved = {};
    let lastError = '';
    let metadataError = '';
    let failedAsset = '';
    let observer = null;

    function note(text) {
      const msg = String(text).replace(/\s+/g, ' ').trim().slice(0, 300);
      if (!msg) return;
      if (/error loading metadata/i.test(msg)) {
        if (!metadataError) metadataError = msg;
      } else {
        lastError = msg;
      }
    }

    function wrap(name, filter) {
      const original = console[name];
      saved[name] = original;
      console[name] = function (...args) {
        try {
          const msg = args.map(describe).join(' ');
          if (!filter || filter.test(msg)) note(msg);
        } catch (e) {
          /* never break the viewer's logging */
        }
        return original.apply(this, args);
      };
    }

    function watchResources() {
      if (!('PerformanceObserver' in window)) return;
      try {
        observer = new PerformanceObserver((entries) => {
          for (const entry of entries.getEntries()) {
            const status = entry.responseStatus; // undefined where the browser does not report it
            if (typeof status === 'number' && status >= 400 && entry.name.indexOf('/armory/assets/') !== -1) {
              try {
                failedAsset = new URL(entry.name).pathname;
              } catch (e) {
                failedAsset = entry.name;
              }
            }
          }
        });
        observer.observe({ type: 'resource' });
      } catch (e) {
        observer = null;
      }
    }

    return {
      start() {
        lastError = '';
        metadataError = '';
        failedAsset = '';
        if (active) return;
        active = true;
        saved = {};
        wrap('error', null);
        wrap('log', /error|fail/i);
        watchResources();
      },
      stop() {
        if (!active) return;
        active = false;
        console.error = saved.error;
        console.log = saved.log;
        if (observer) observer.disconnect();
        observer = null;
      },
      // window.WH.debug: the viewer's own debug channel
      debug(args) {
        if (!active) return;
        const msg = list(args).map(describe).join(' ');
        if (/rror|fail|not found/i.test(msg)) note(msg);
      },
      runtime(msg) {
        if (active && msg) note(msg);
      },
      message() {
        const parts = [];
        if (failedAsset) parts.push('failed: ' + failedAsset);
        if (metadataError) parts.push(metadataError);
        if (lastError) parts.push(lastError);
        return parts.join('; ');
      },
    };
  }

  function hasWebGL() {
    try {
      const canvas = document.createElement('canvas');
      const gl = canvas.getContext('webgl2') || canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
      if (!gl) return false;
      const lose = gl.getExtension('WEBGL_lose_context');
      if (lose) lose.loseContext();
      return true;
    } catch (e) {
      return false;
    }
  }

  // --- the 3D model panel ---
  //
  // #armory-model data-state: loading | rendered | unavailable | off | nowebgl (spec 5;
  // the browser check in Task 13 waits for "rendered").

  const model = (function () {
    const panel = document.getElementById('armory-model');
    if (!panel) return null;
    const box = panel.querySelector('.armory-model-canvas');
    const text = panel.querySelector('.armory-model-text');
    const note = panel.querySelector('.armory-model-note');
    const sheath = document.getElementById('armory-sheath');
    const hideTabard = document.getElementById('armory-hide-tabard');
    const capture = errorCapture();
    const probe = document.createElement('canvas');
    let gen = 0; // bumped by every load or re-create; stale callbacks compare it
    let fetching = false; // a load() is waiting for the model API (the toggles wait for it)
    let data = null; // the last model API answer
    let enabled = null; // its viewer.enabled
    let viewer = null;
    let timers = [];
    let scriptPromise = null;
    let sheathSet = false;
    let watchdog = null; // the running start()'s { my, deadline, hidden }

    window.addEventListener('error', (event) => {
      if (event instanceof ErrorEvent && event.message) capture.runtime(event.message);
    });
    window.addEventListener('unhandledrejection', (event) => capture.runtime(describe(event.reason)));

    // The watchdog counts only time the tab is visible: requestAnimationFrame does not run in a
    // hidden tab (a character opened in a background tab from the Players page), and neither
    // does the viewer's drawing. A hide is noted here, while the page still runs; the first
    // frame after it gives the model the full 30 s again, whether that frame or the
    // visibilitychange back to visible comes first.
    document.addEventListener('visibilitychange', () => {
      if (document.hidden && watchdog && watchdog.my === gen) watchdog.hidden = true;
    });

    function setState(state, message) {
      panel.dataset.state = state;
      text.textContent = message || '';
    }

    function setNote(message) {
      note.textContent = message || '';
      note.hidden = !message;
    }

    // "no 3D" corner marks on the paper-doll slots whose model Wowhead does not have
    // (definitive misses only; pending slots are explained by the panel note instead).
    function markSlots(unresolved) {
      root.querySelectorAll('.no-3d').forEach((badge) => badge.remove());
      root.querySelectorAll('[data-base-label]').forEach((slot) => {
        slot.setAttribute('aria-label', slot.getAttribute('data-base-label'));
        slot.removeAttribute('data-base-label');
      });
      for (const slot of list(unresolved)) {
        const host = document.getElementById('slot-' + Number(slot));
        const frame = host && host.querySelector('.slot-icon');
        if (!frame) continue;
        const badge = document.createElement('span');
        badge.className = 'no-3d';
        badge.textContent = 'no 3D';
        badge.title = 'No 3D model found for this item; the model is shown without it.';
        frame.append(badge);
        const label = host.getAttribute('aria-label');
        if (label) {
          host.setAttribute('data-base-label', label);
          host.setAttribute('aria-label', label + ' (no 3D model)');
        }
      }
    }

    // The viewer appends a protocol-relative <link rel="stylesheet"> to Wowhead's viewer.css
    // through jQuery for every renderer it builds. The page needs nothing from it (the spike
    // saw no visible change) and the CSP refuses it, logging a violation each time; dropping
    // any foreign <link> from the HTML jQuery parses means the browser never even tries.
    function dropForeignLinks() {
      const jq = window.jQuery;
      if (jq.htmlPrefilter.armoryWrapped) return;
      const original = jq.htmlPrefilter;
      const filtered = (html) => original.call(jq, String(html).replace(FOREIGN_LINK, ''));
      filtered.armoryWrapped = true;
      jq.htmlPrefilter = filtered;
    }

    function installShim() {
      // The globals the viewer expects (spike page.html; constants from the npm package's
      // setup.js). contentPath = the Wrath data through our proxy, as the spike proved.
      window.CONTENT_PATH = window.location.origin + CONTENT_PATH;
      dropForeignLinks();
      if (window.WH) return;
      window.WH = {
        debug: (...args) => capture.debug(args),
        defaultAnimation: 'Stand',
        WebP: { getImageExtension: () => '.webp' },
        Wow: {
          Item: {
            INVENTORY_TYPE_HEAD: 1,
            INVENTORY_TYPE_NECK: 2,
            INVENTORY_TYPE_SHOULDERS: 3,
            INVENTORY_TYPE_SHIRT: 4,
            INVENTORY_TYPE_CHEST: 5,
            INVENTORY_TYPE_WAIST: 6,
            INVENTORY_TYPE_LEGS: 7,
            INVENTORY_TYPE_FEET: 8,
            INVENTORY_TYPE_WRISTS: 9,
            INVENTORY_TYPE_HANDS: 10,
            INVENTORY_TYPE_FINGER: 11,
            INVENTORY_TYPE_TRINKET: 12,
            INVENTORY_TYPE_ONE_HAND: 13,
            INVENTORY_TYPE_SHIELD: 14,
            INVENTORY_TYPE_RANGED: 15,
            INVENTORY_TYPE_BACK: 16,
            INVENTORY_TYPE_TWO_HAND: 17,
            INVENTORY_TYPE_BAG: 18,
            INVENTORY_TYPE_TABARD: 19,
            INVENTORY_TYPE_ROBE: 20,
            INVENTORY_TYPE_MAIN_HAND: 21,
            INVENTORY_TYPE_OFF_HAND: 22,
            INVENTORY_TYPE_HELD_IN_OFF_HAND: 23,
            INVENTORY_TYPE_PROJECTILE: 24,
            INVENTORY_TYPE_THROWN: 25,
            INVENTORY_TYPE_RANGED_RIGHT: 26,
            INVENTORY_TYPE_QUIVER: 27,
            INVENTORY_TYPE_RELIC: 28,
            INVENTORY_TYPE_PROFESSION_TOOL: 29,
            INVENTORY_TYPE_PROFESSION_ACCESSORY: 30,
          },
        },
      };
    }

    // The pinned viewer script, through the proxy; a failed load can be retried later. The
    // viewer needs jQuery: without it the script throws while it loads ("jQuery is not
    // defined") and its constructor calls container.width() (Task 13's browser run).
    function viewerScript() {
      if (!window.jQuery) return Promise.reject(new Error(JQUERY_SCRIPT + ' did not load'));
      if (window.ZamModelViewer) return Promise.resolve();
      if (!scriptPromise) {
        installShim();
        const tag = document.createElement('script');
        tag.src = VIEWER_SCRIPT;
        tag.async = true;
        scriptPromise = new Promise((resolve, reject) => {
          tag.addEventListener('load', () => (window.ZamModelViewer ? resolve() : reject(new Error('no ZamModelViewer'))));
          tag.addEventListener('error', () => reject(new Error(VIEWER_SCRIPT + ' did not load')));
          document.head.append(tag);
        }).catch((err) => {
          scriptPromise = null;
          tag.remove();
          throw err;
        });
      }
      return scriptPromise;
    }

    // The spike's proven configuration (spec 2, 7): type 2 = character, models.type 16,
    // hd: true, items [[inventoryType, displayId], ...], options already mapped by the server
    // (R12). Sheathed weapons = sheathMain/sheathOff 1; unsheathed leaves them out, exactly
    // as the spike page did. "Hide tabard" leaves the tabard out of this list only (the
    // answer and the paper-doll keep it). The container is a jQuery object (the viewer calls
    // its width(), css() and append()).
    function viewerOptions() {
      const charCustomization = { options: data.options };
      if (sheath && sheath.checked) {
        charCustomization.sheathMain = 1;
        charCustomization.sheathOff = 1;
      }
      const hide = Boolean(hideTabard && hideTabard.checked);
      const items = list(data.items).filter((item) => !(hide && isTabard(item)));
      const w = box.clientWidth;
      const h = box.clientHeight;
      return {
        type: 2,
        contentPath: window.location.origin + CONTENT_PATH,
        container: window.jQuery(box),
        aspect: w > 0 && h > 0 ? w / h : 0.75,
        hd: true,
        models: { id: data.modelId, type: 16 },
        items,
        charCustomization,
      };
    }

    // One viewer item, [inventoryType, displayId].
    function isTabard(item) {
      return Array.isArray(item) && Number(item[0]) === TABARD;
    }

    // "Drawn" = the viewer's canvas holds a non-blank picture. A PROBE_W x PROBE_H copy is
    // sampled: some pixels transparent and some not (the model on a transparent canvas), or,
    // when every pixel is opaque, at least two colours (a cleared opaque canvas is one flat
    // colour, like the spike's blank run0 render). The copy is taken in a
    // requestAnimationFrame callback registered from a timer, i.e. after the viewer's own
    // frame callback, so it reads the frame the viewer just drew even if its WebGL context
    // does not preserve the drawing buffer. A canvas the browser refuses to read
    // (SecurityError: tainted by drawn content) counts as drawn. Task 13 confirms this in a
    // real browser.
    function canvasDrawn(canvas) {
      if (!canvas.width || !canvas.height) return false;
      try {
        probe.width = PROBE_W;
        probe.height = PROBE_H;
        const ctx = probe.getContext('2d', { willReadFrequently: true });
        ctx.drawImage(canvas, 0, 0, PROBE_W, PROBE_H);
        const px = ctx.getImageData(0, 0, PROBE_W, PROBE_H).data;
        let opaque = 0;
        let first = -1;
        let varied = false;
        for (let i = 0; i < px.length; i += 4) {
          if (px[i + 3] === 0) continue;
          opaque += 1;
          const rgb = (px[i] << 16) | (px[i + 1] << 8) | px[i + 2];
          if (first === -1) first = rgb;
          else if (rgb !== first) varied = true;
        }
        if (opaque === 0) return false;
        return opaque < PROBE_W * PROBE_H || varied;
      } catch (e) {
        return Boolean(e && e.name === 'SecurityError');
      }
    }

    function drawn() {
      return Array.from(box.querySelectorAll('canvas')).some(canvasDrawn);
    }

    function destroyViewer() {
      if (viewer && typeof viewer.destroy === 'function') {
        try {
          viewer.destroy();
        } catch (e) {
          /* the container is cleared below anyway */
        }
      }
      viewer = null;
      box.replaceChildren();
    }

    function stop() {
      timers.forEach(clearTimeout);
      timers = [];
      watchdog = null;
      capture.stop();
      destroyViewer();
    }

    function unavailable(detail) {
      setState('unavailable', '3D model unavailable — ' + detail);
    }

    // Create the viewer for `data` (the container is cleared first) and watch for its first
    // drawn frame; after WATCHDOG_MS of a visible tab without one, the panel says what failed.
    async function start(my) {
      setState('loading', 'Fetching model…');
      capture.start();
      try {
        await viewerScript();
      } catch (e) {
        if (my !== gen) return;
        capture.stop();
        unavailable("couldn't load the viewer (" + describe(e) + ').');
        return;
      }
      if (my !== gen) return;
      destroyViewer();
      try {
        viewer = new window.ZamModelViewer(viewerOptions());
      } catch (e) {
        capture.stop();
        destroyViewer();
        unavailable('the viewer failed to start (' + describe(e) + ').');
        return;
      }
      // The 30 s start in the first frame callback, not now (in a hidden tab none runs yet),
      // and start again in the first frame after the tab was hidden.
      const watch = { my, deadline: null, hidden: false };
      watchdog = watch;
      const poll = () => {
        if (my !== gen) return;
        window.requestAnimationFrame(() => {
          if (my !== gen) return;
          if (watch.deadline === null || watch.hidden) {
            watch.deadline = Date.now() + WATCHDOG_MS;
            watch.hidden = false;
          }
          if (drawn()) {
            capture.stop();
            setState('rendered', '');
            return;
          }
          if (Date.now() >= watch.deadline) {
            const why = capture.message();
            capture.stop();
            destroyViewer();
            unavailable('nothing was drawn within 30 seconds' + (why ? ' (' + why + ').' : '.'));
            return;
          }
          timers.push(window.setTimeout(poll, DRAWN_POLL_MS));
        });
      };
      timers.push(window.setTimeout(poll, DRAWN_POLL_MS));
    }

    async function load() {
      const my = ++gen;
      fetching = true;
      stop();
      setState('loading', 'Fetching model…');
      setNote('');
      let answer = null;
      let resp;
      try {
        resp = await fetch(panel.dataset.modelApi, { headers: { Accept: 'application/json' }, cache: 'no-store' });
        answer = await resp.json().catch(() => null);
      } catch (e) {
        if (my !== gen) return;
        fetching = false;
        unavailable("couldn't load the model data from the admin.");
        return;
      }
      if (my !== gen) return; // a newer load() owns `fetching` now
      fetching = false;
      if (!resp.ok || !answer || typeof answer !== 'object') {
        const detail = answer && answer.detail ? String(answer.detail) : 'HTTP ' + resp.status;
        unavailable(detail + '.');
        return;
      }
      data = answer;
      enabled = Boolean(answer.viewer && answer.viewer.enabled);
      // Nothing to hide without a tabard in the answer (none worn, its model not found, or
      // the viewer switched off); the box keeps its tick for the next answer.
      if (hideTabard) hideTabard.disabled = !list(answer.items).some(isTabard);
      markSlots(enabled ? answer.unresolved : []);
      if (!enabled) {
        setState('off', '3D viewer is switched off');
        return;
      }
      if (!hasWebGL()) {
        setState('nowebgl', 'No WebGL: this browser cannot draw the 3D model.');
        return;
      }
      if (!Array.isArray(answer.options)) {
        unavailable("couldn't reach Wowhead's model data" + (answer.reason ? ' (' + answer.reason + ').' : '.'));
        return;
      }
      // reason (R23) here explains the pending slots: items not shown yet, no "no 3D" mark.
      setNote(list(answer.pending).length && answer.reason ? String(answer.reason) : '');
      if (sheath && !sheathSet) {
        sheath.checked = Boolean(answer.sheath);
        sheathSet = true;
      }
      start(my);
    }

    // "Sheathe weapons" and "Hide tabard" re-create the viewer; viewerOptions() reads both
    // boxes, so they combine. While load() waits for the model API a change only sets the box:
    // the start() after the answer reads it (re-creating now would orphan that answer).
    function recreate() {
      if (fetching || !data || !enabled || !Array.isArray(data.options) || panel.dataset.state === 'nowebgl') return;
      const my = ++gen;
      stop();
      start(my);
    }
    if (sheath) sheath.addEventListener('change', recreate);
    if (hideTabard) hideTabard.addEventListener('change', recreate);

    // The footer's On/Off switch re-renders the strip; follow it without a page reload.
    document.addEventListener('htmx:afterSwap', (event) => {
      const target = event.detail && event.detail.target;
      if (!target || target.id !== 'armory-footer' || enabled === null) return;
      const row = target.querySelector('[data-viewer-enabled]');
      if (row && (row.dataset.viewerEnabled === '1') !== enabled) load();
    });

    return { load };
  })();

  if (model) model.load();
})();
