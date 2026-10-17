// assets/zz_idle_timeout_init.js
//
// Wires up idle-timeout-watcher.js (an unmodified copy of the integration
// kit's own template — keep it that way, so future kit fixes can just be
// re-copied over it) for this app specifically. Named with a `zz_` prefix
// so Dash's alphabetical asset-loading order guarantees it runs AFTER
// idle-timeout-watcher.js defines the global BescomIdleWatcher, without
// needing to touch that file itself (same numeric/alpha-prefix trick the
// 0N_*.css files already use for load order).
//
// onTimeout calls this app's own new /api/auth/end-session route (see
// app.py's own comment — this app has no local logout of its own; that
// route exists purely to let THIS watcher end the shared session), then
// redirects to the SSO portal gateway per the hard rule every connected
// app follows (NEW-APP-INTEGRATION.md): login always via
// sso.bescomapps.com first, never straight to Authentik.
(function () {
  BescomIdleWatcher.init({
    apiBase: '/api',
    idleMs: 15 * 60 * 1000,
    onTimeout: function () {
      fetch('/api/auth/end-session', { method: 'POST' })
        .catch(function () {})
        .then(function () {
          window.location.href = 'https://sso.bescomapps.com/';
        });
    },
  });
})();
