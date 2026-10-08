/* Gateway console auth shim.
   Loaded into every static/*.html page by the server, before the page's own
   inline scripts run (a classic script tag in <head> blocks parsing, so this
   always wins the race).

   Why this file exists: /v1/* requires the `X-Gateway-Token` header, but these
   pages call /v1/* from hand-written inline fetch() calls with no header, so
   every panel came back 401 and the UI looked empty. Rather than edit seven
   pages and risk missing a call, wrap window.fetch once. */
(function () {
  'use strict';
  var meta = document.querySelector('meta[name="gateway-token"]');
  var token = meta ? (meta.getAttribute('content') || '') : '';
  if (!token) {
    console.error('[gateway] no token in page - /v1 calls will 401');
    return;
  }
  var orig = window.fetch;
  window.fetch = function (input, init) {
    var opts = init || {};
    var headers = {};
    try {
      if (opts.headers && typeof opts.headers.forEach === 'function' &&
          typeof opts.headers.append === 'function') {
        opts.headers.forEach(function (v, k) { headers[k] = v; });
      } else if (opts.headers) {
        Object.keys(opts.headers).forEach(function (k) { headers[k] = opts.headers[k]; });
      }
    } catch (e) { /* fall through to a fresh object */ }
    if (!headers['X-Gateway-Token'] && !headers['x-gateway-token']) {
      headers['X-Gateway-Token'] = token;
    }
    opts.headers = headers;
    return orig.call(window, input, opts);
  };
})();