// assets/tab_presence.js
//
// Logs the whole shared SSO session out once the last open tab, across
// EVERY connected app (not just this one), closes — see the SSO portal's
// own NEW-APP-INTEGRATION.md Section 10. Dash auto-loads every file in
// assets/ on page load (same mechanism shortcut_toggle.js already uses),
// so this needs no build step and no index_string override.
//
// Unlike Transfer App's equivalent (TabPresenceWatcher.jsx), this app has
// no login/logout redirects of its own to suppress — closing a tab is
// always a genuine close here, since there's no intentional hard
// navigation that would otherwise be mistaken for one. tabController.py's
// /api/tabs/close still accepts a `suppressed` field for consistency with
// every other connected app, but it's always false from here.
(function () {
  // Comfortably under the SSO portal's own PRESENCE_TTL_SECONDS (45s) —
  // same margin every other connected app's watcher keeps.
  var HEARTBEAT_MS = 15 * 1000;
  var TAB_ID_KEY = 'bescom_tab_id';

  function getTabId() {
    try {
      var id = sessionStorage.getItem(TAB_ID_KEY);
      if (!id) {
        id = (window.crypto && window.crypto.randomUUID)
          ? window.crypto.randomUUID()
          : (Date.now() + '-' + Math.random());
        sessionStorage.setItem(TAB_ID_KEY, id);
      }
      return id;
    } catch (e) {
      // Private-mode/storage-blocked: fall back to an in-memory id for
      // this page load — this tab just won't survive a refresh with the
      // same id, degraded but not broken.
      return Date.now() + '-' + Math.random();
    }
  }

  var tabId = getTabId();

  function heartbeat() {
    fetch('/api/tabs/heartbeat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tabId: tabId }),
    }).catch(function () {});
  }

  function sendCloseBeacon() {
    var payload = new Blob(
      [JSON.stringify({ tabId: tabId, suppressed: false })],
      { type: 'application/json' }
    );
    // sendBeacon, not fetch: pagehide gives no guarantee a fetch started
    // there survives long enough to actually reach the network.
    var queued = navigator.sendBeacon('/api/tabs/close', payload);
    // TEMPORARY DIAGNOSTIC (2026-10-14) — localStorage, not sessionStorage:
    // this tab is closing, so sessionStorage disappears with it, but
    // localStorage survives and is readable from a DIFFERENT, freshly
    // opened tab afterward. `queued` is sendBeacon's own return value —
    // false means the browser refused to even accept the beacon (a real
    // client-side failure, e.g. a quota/queue limit); true just means it
    // was handed off, not that it actually reached the server. Remove
    // once the "close doesn't reach the server after the dashboard-to-SSO
    // redirect chain" case is understood.
    try {
      localStorage.setItem('bescom_tab_close_debug', JSON.stringify({
        tabId: tabId,
        queued: queued,
        time: new Date().toISOString(),
        url: window.location.href,
      }));
    } catch (e) {}
  }

  heartbeat();
  setInterval(heartbeat, HEARTBEAT_MS);
  // CONFIRMED GAP (2026-10-14), live: pagehide alone didn't fire when the
  // ENTIRE browser application was closed at once (the only tab/window
  // open, closed ~30s after loading — far too soon for Chrome's Memory
  // Saver tab-discarding to be the cause, which was the first theory and
  // got ruled out). pagehide is reliably specified for closing one tab
  // while the browser keeps running, but browsers have historically been
  // less consistent about firing it when the whole application quits at
  // once. Also listening on the older sibling event `unload` as a
  // backup — standard cross-browser belt-and-suspenders pattern for
  // exactly this gap. Safe even if both fire for the same close: sending
  // the close signal twice is harmless, removing an already-removed
  // presence entry is a no-op either way.
  window.addEventListener('pagehide', sendCloseBeacon);
  window.addEventListener('unload', sendCloseBeacon);
})();
