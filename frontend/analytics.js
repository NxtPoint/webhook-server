/* Ten-Fifty5 page-view analytics (navigation + drop-off tracking).
 * Auto-injected into every page served by the Locker Room service. Sends a page_view on load and on
 * SPA/route changes, via navigator.sendBeacon (no CORS preflight, fire-and-forget). Attributes to the
 * account when an email is present in the URL (member pages); anonymous on public marketing pages.
 * No-op server-side unless TRACKING_ENABLED=1. No PII beyond the (already-present) email param.
 */
(function () {
  "use strict";
  var p = new URLSearchParams(location.search);
  var API = p.get("api") || "https://api.nextpointtennis.com";
  var EMAIL = (p.get("email") || "").trim() || null;
  var URL_ = API + "/api/track/page";
  var last = null;

  // First-party anonymous id: read-or-create + persist in localStorage. Survives across
  // page-views/sessions so the server can stitch anonymous traffic to one visitor.
  function anonId() {
    var k = "tf_anon";
    try {
      var v = localStorage.getItem(k);
      if (v) return v;
    } catch (e) { /* localStorage blocked (private mode) — fall through to ephemeral id */ }
    var id;
    try {
      id = (crypto && crypto.randomUUID) ? crypto.randomUUID() : null;
    } catch (e) { id = null; }
    if (!id) {
      id = "a-" + Date.now().toString(36) + "-" +
           Math.random().toString(36).slice(2, 10) + Math.random().toString(36).slice(2, 10);
    }
    try { localStorage.setItem(k, id); } catch (e) { /* ignore */ }
    return id;
  }
  var ANON = anonId();

  // UTM attribution from the current URL querystring (omit entirely if none present).
  function utm() {
    var keys = ["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"];
    var out = null;
    keys.forEach(function (k) {
      var val = p.get(k);
      if (val) { (out = out || {})[k.slice(4)] = val; }   // utm_source -> source
    });
    return out;
  }
  var UTM = utm();

  function send(path) {
    if (path === last) return;     // de-dupe rapid duplicate fires
    last = path;
    var body = {
      path: path,
      email: EMAIL,
      referrer: document.referrer || "",
      anon_id: ANON,
      props: { title: (document.title || "").slice(0, 120) },
    };
    if (UTM) body.utm = UTM;
    var payload = JSON.stringify(body);
    try {
      if (navigator.sendBeacon) {
        navigator.sendBeacon(URL_, new Blob([payload], { type: "text/plain" }));
        return;
      }
    } catch (e) { /* fall through to fetch */ }
    try {
      fetch(URL_, { method: "POST", body: payload, keepalive: true,
                    headers: { "Content-Type": "text/plain" } }).catch(function () {});
    } catch (e) { /* ignore */ }
  }

  function current() { return location.pathname + (location.hash || ""); }
  function fire() { send(current()); }

  // initial view
  if (document.readyState === "complete" || document.readyState === "interactive") fire();
  else document.addEventListener("DOMContentLoaded", fire);

  // SPA navigation: history API + hash + back/forward
  ["pushState", "replaceState"].forEach(function (m) {
    var orig = history[m];
    if (typeof orig === "function") {
      history[m] = function () { var r = orig.apply(this, arguments); setTimeout(fire, 0); return r; };
    }
  });
  window.addEventListener("popstate", fire);
  window.addEventListener("hashchange", fire);
})();
