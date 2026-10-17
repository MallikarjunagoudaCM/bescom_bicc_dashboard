// integration-kit/frontend/idle-timeout-watcher.js
//
// Vanilla-JS port of IdleTimeoutWatcher.jsx, for connected apps that
// aren't React (Dash, Flask+Jinja, a PWA, plain HTML — see
// NEW-APP-INTEGRATION.md Section 9). Same logic, no framework dependency.
// Unlike tab_presence.js's own auto-running IIFE, this exposes an
// explicit init() — idle-timeout fundamentally needs an app-specific
// "what does my real logout actually do" callback (see the warning below),
// so it can't be fully fire-and-forget the way tab-presence is.
//
// Usage, once per page, after your own auth check confirms someone's
// logged in:
//
//   BescomIdleWatcher.init({
//     apiBase: '/api',              // -> ${apiBase}/auth/activity/ping, .../status
//     idleMs: 15 * 60 * 1000,       // optional, defaults to 15 minutes
//     onTimeout: function () {
//       // MUST perform your app's REAL logout — see the warning below.
//     },
//   });
//
// *** READ THIS BEFORE WIRING onTimeout ***
// NEW-APP-INTEGRATION.md Section 9 flags this exact mistake: a function
// your own app happens to call "logout" is not guaranteed to be the REAL
// logout (end every Authentik session, clear everything, navigate away)
// — it might just clear local state and rely on something else to
// redirect. Go check what your app's logout button ACTUALLY does before
// wiring it here. If in doubt, call your own /end-session route (from
// ssoIntegration.js) directly inside onTimeout yourself, the same way
// your real logout button does.
//
// FIXED (2026-10-17, same history as the React version of this file):
// uses a frequent (1s) timestamp-comparison tick, NOT a single
// long-delay setTimeout/setInterval(idleMs) — CONFIRMED LIVE, twice
// independently (the SSO portal's own component, then Transfer App's
// copy, still broken there as of this date) that a long-delay timer
// silently misses its deadline on a backgrounded/minimized tab (browsers
// throttle or fully suspend timers there), then fires all at once — or
// shows a stale/wrong countdown — the moment the tab regains focus.
var BescomIdleWatcher = (function () {
  var WARNING_SECONDS = 10;
  var WARNING_MS = WARNING_SECONDS * 1000;
  var PING_THROTTLE_MS = 60 * 1000; // at most one activity ping per minute
  var SHARED_CHECK_MS = 30 * 1000; // match your own session-status watchdog's cadence if you have one
  var TICK_MS = 1000;
  var ACTIVITY_EVENTS = ['mousemove', 'keydown', 'click', 'scroll', 'touchstart'];

  var state = null; // set by init(); null means "not running"

  function buildOverlay() {
    var overlay = document.createElement('div');
    overlay.id = 'bescom-idle-overlay';
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(20,24,33,0.55);display:none;align-items:center;justify-content:center;padding:20px;z-index:2000;';

    var card = document.createElement('div');
    card.style.cssText = 'background:#fff;border-radius:14px;padding:28px;width:100%;max-width:380px;box-shadow:0 20px 60px rgba(0,0,0,0.35);text-align:center;font-family:inherit;';

    var title = document.createElement('h3');
    title.textContent = 'Still there?';
    title.style.cssText = 'font-size:18px;font-weight:700;color:#1a1a1a;margin:0 0 10px;';

    var body = document.createElement('p');
    body.style.cssText = 'font-size:14px;color:#5F5E5A;margin:0 0 20px;line-height:1.5;';
    var bodyPrefix = document.createTextNode("You've been inactive for a while. For your security, you'll be signed out in ");
    var secondsSpan = document.createElement('strong');
    secondsSpan.id = 'bescom-idle-seconds';
    var bodySuffix = document.createTextNode(' seconds.');
    body.appendChild(bodyPrefix);
    body.appendChild(secondsSpan);
    body.appendChild(bodySuffix);

    var button = document.createElement('button');
    button.type = 'button';
    button.textContent = 'Continue session';
    button.style.cssText = 'padding:10px 28px;background:linear-gradient(135deg,#009FE3 0%,#0072B1 100%);color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;';
    button.addEventListener('click', function () {
      if (state) state.handleActivity();
    });

    card.appendChild(title);
    card.appendChild(body);
    card.appendChild(button);
    overlay.appendChild(card);
    document.body.appendChild(overlay);
    return { overlay: overlay, secondsSpan: secondsSpan };
  }

  function init(options) {
    if (state) return; // already running — init() is idempotent, call it once
    options = options || {};
    var apiBase = options.apiBase || '/api';
    var idleMs = options.idleMs || 15 * 60 * 1000;
    var onTimeout = options.onTimeout || function () {};

    var dom = buildOverlay();
    var lastKnownActivity = Date.now();
    var lastPingSent = 0;
    var warningShownAt = null;
    var showingWarning = false;
    var loggedOut = false;

    function pingActivity() {
      var now = Date.now();
      if (now - lastPingSent < PING_THROTTLE_MS) return;
      lastPingSent = now;
      fetch(apiBase + '/auth/activity/ping', { method: 'POST', credentials: 'include' }).catch(function () {});
    }

    function showWarning(secondsLeft) {
      showingWarning = true;
      dom.secondsSpan.textContent = String(secondsLeft);
      dom.overlay.style.display = 'flex';
    }
    function hideWarning() {
      showingWarning = false;
      warningShownAt = null;
      dom.overlay.style.display = 'none';
    }

    function handleActivity() {
      lastKnownActivity = Date.now();
      pingActivity();
      if (showingWarning) hideWarning();
    }

    // Frequent timestamp comparison instead of a single long-delay timer —
    // see this file's own header comment for the live bug this fixes.
    function checkState() {
      if (loggedOut) return;
      var now = Date.now();
      if (!showingWarning) {
        var idleFor = now - lastKnownActivity;
        if (idleFor < idleMs) return;
        // How far PAST the idle threshold we already are — if a
        // backgrounded tab's timer got throttled and this only ran once
        // the tab regained focus, don't restart a fresh WARNING_MS
        // countdown (that would silently extend real idle time); log out
        // immediately if the warning window's already fully elapsed too.
        var intoWarning = idleFor - idleMs;
        if (intoWarning >= WARNING_MS) {
          loggedOut = true;
          onTimeout();
          return;
        }
        warningShownAt = now - intoWarning;
        showWarning(Math.max(1, Math.ceil((WARNING_MS - intoWarning) / 1000)));
        return;
      }
      var remainingMs = WARNING_MS - (now - warningShownAt);
      if (remainingMs <= 0) {
        warningShownAt = null;
        loggedOut = true;
        onTimeout();
        return;
      }
      dom.secondsSpan.textContent = String(Math.ceil(remainingMs / 1000));
    }

    function handleVisibilityOrFocus() {
      if (document.visibilityState === 'visible') checkState();
    }

    ACTIVITY_EVENTS.forEach(function (evt) {
      window.addEventListener(evt, handleActivity, { passive: true });
    });
    document.addEventListener('visibilitychange', handleVisibilityOrFocus);
    window.addEventListener('focus', handleVisibilityOrFocus);
    setInterval(checkState, TICK_MS);

    // Cross-app check: has ANY connected app seen this person recently,
    // even if this particular tab hasn't? Without this, a genuinely
    // active OTHER tab/app wouldn't protect this one from timing out —
    // see NEW-APP-INTEGRATION.md Section 9's own "why per-tab local idle
    // detection alone is wrong".
    setInterval(function () {
      fetch(apiBase + '/auth/activity/status', { credentials: 'include' })
        .then(function (res) { return res.json(); })
        .then(function (data) {
          if (data.lastActivity && data.lastActivity > lastKnownActivity) {
            lastKnownActivity = data.lastActivity;
            if (showingWarning) hideWarning();
          }
        })
        .catch(function () {});
    }, SHARED_CHECK_MS);

    state = { handleActivity: handleActivity };
  }

  return { init: init };
})();
