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

  function send(path) {
    if (path === last) return;     // de-dupe rapid duplicate fires
    last = path;
    var payload = JSON.stringify({
      path: path,
      email: EMAIL,
      referrer: document.referrer || "",
      props: { title: (document.title || "").slice(0, 120) },
    });
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
