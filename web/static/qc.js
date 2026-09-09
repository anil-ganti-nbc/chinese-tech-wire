/* Authenticated QC transport.
 *
 * Phase 0 containment requires Authorization: Bearer $CTW_DASHBOARD_AUTH_TOKEN
 * on every mutation, and a plain browser form submit cannot attach that
 * header — an ordinary form POST is rejected by the middleware with a raw
 * JSON 403. The pages therefore render the same per-process token the Health
 * page's "Run collector now" button uses, and this interceptor sends QC form
 * submissions through fetch() with that header instead. The server answers
 * JSON requests with {ok, outcome, next}; navigating to `next` preserves the
 * exact newsroom filter context the click came from and lands after the QC
 * archive, so the decided lead is already gone from the queue.
 *
 * The token rotates on GUI restart; a 403 here means this page's copy is
 * stale (or the dashboard was restarted) — say so in place instead of
 * exposing a raw JSON error page.
 */
(function () {
  'use strict';

  function showBanner(message) {
    var banner = document.getElementById('qc-auth-banner');
    if (!banner) {
      banner = document.createElement('div');
      banner.id = 'qc-auth-banner';
      banner.style.cssText =
        'position:fixed;top:0;left:0;right:0;z-index:9999;padding:10px 16px;' +
        'background:#7a1f1f;color:#fff;font:13px/1.4 sans-serif;text-align:center;';
      document.body.appendChild(banner);
    }
    banner.textContent = message;
    banner.style.display = 'block';
  }

  function init() {
    var token = window.CTW_DASHBOARD_AUTH_TOKEN || '';

    document.addEventListener('submit', function (ev) {
      var form = ev.target;
      if (!(form instanceof HTMLFormElement)) return;
      if (!form.classList.contains('qc-inline') && !form.classList.contains('fb-post')) return;
      ev.preventDefault();

      var btn = form.querySelector('button[type="submit"], button:not([type])');
      if (btn) btn.disabled = true;

      var headers = { 'Accept': 'application/json' };
      if (token) headers['Authorization'] = 'Bearer ' + token;

      fetch(form.getAttribute('action'), {
        method: 'POST',
        headers: headers,
        body: new FormData(form),
        credentials: 'same-origin'
      })
        .then(function (r) {
          if (r.status === 403) {
            showBanner(
              'QC blocked: the dashboard rejected this session\u2019s authentication ' +
              '(the token rotates on GUI restart). Reload this page; if it persists, ' +
              'restart the Chinese Tech Wire newsroom GUI.'
            );
            if (btn) btn.disabled = false;
            return null;
          }
          return r.json().then(function (data) { return { status: r.status, data: data }; });
        })
        .then(function (res) {
          if (!res) return; // 403 banner already shown; stay on the page
          if (res.data && res.data.next) {
            window.location.assign(res.data.next);
            return;
          }
          if (btn) btn.disabled = false;
          showBanner('QC failed (HTTP ' + res.status + '). Reload and try again.');
        })
        .catch(function () {
          if (btn) btn.disabled = false;
          showBanner('QC failed: network error reaching the dashboard.');
        });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
