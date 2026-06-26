/* Ten-Fifty5 first-party, cookieless page-view analytics (powers the cockpit's Website Traffic).
 *
 * SHARED ENGINE (replicable across sites — kept in lock-step with the nextpoint repo; only the API
 * default + anon_id storage key differ per site). Auto-injected into every page served by the Locker
 * Room + main API services. Sends a page_view on load and on SPA/route changes, and a `leave` event
 * on unload for time-on-site, via navigator.sendBeacon (text/plain → no CORS preflight, fire-and-
 * forget). NO cookies, NO third parties: a first-party `anon_id` UUID in localStorage counts UNIQUE
 * visitors; referrer + UTM give acquisition source; the server adds country (CDN edge header) +
 * device/browser/OS (User-Agent). NO consent gate — it stores no personal data, so it is exempt from
 * prior consent (see the privacy policy). No email/PII ever leaves the browser.
 */
(function () {
  "use strict";
  var qp = new URLSearchParams(location.search);
  var API = (window.__API_BASE || qp.get("api") || "https://api.nextpointtennis.com").replace(/\/+$/, "");
  var URL_ = API + "/api/track/page";
  var ANON_KEY = "tf_anon";

  function anonId() {
    try {
      var v = localStorage.getItem(ANON_KEY);
      if (v) return v;
    } catch (e) { /* localStorage blocked (private mode) — ephemeral id below */ }
    var id;
    try { id = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : null; } catch (e) { id = null; }
    if (!id) {
      id = "a-" + Date.now().toString(36) + "-" +
           Math.random().toString(36).slice(2, 10) + Math.random().toString(36).slice(2, 10);
    }
    try { localStorage.setItem(ANON_KEY, id); } catch (e) { /* ignore */ }
    return id;
  }
  var ANON = anonId();

  function utm() {
    var keys = ["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"];
    var out = null;
    keys.forEach(function (k) {
      var val = qp.get(k);
      if (val) { (out = out || {})[k.slice(4)] = val; }   // utm_source -> source
    });
    return out;
  }
  var UTM = utm();

  function newPvid() {
    try { if (window.crypto && crypto.randomUUID) return crypto.randomUUID(); } catch (e) {}
    return "p-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
  }

  function post(body) {
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

  var last = null, pvid = null, startedAt = 0, leaveSent = false;

  function sendLeave() {
    if (leaveSent || !pvid || !startedAt) return;
    leaveSent = true;
    var ms = Date.now() - startedAt;
    if (ms < 1000 || ms > 6 * 60 * 60 * 1000) return;   // drop sub-1s + runaway tabs
    post({ event: "leave", path: location.pathname.slice(0, 300),
           pvid: pvid, anon_id: ANON, duration_ms: ms });
  }

  function send(path) {
    if (path === last) return;     // de-dupe rapid duplicate fires
    sendLeave();                   // close the previous pageview (SPA nav)
    last = path;
    pvid = newPvid(); startedAt = Date.now(); leaveSent = false;
    var body = {
      path: location.pathname.slice(0, 300),
      referrer: document.referrer || "",
      anon_id: ANON,
      pvid: pvid,
      sw: (window.screen && screen.width) || window.innerWidth || null,
      lang: navigator.language || null,
      props: { title: (document.title || "").slice(0, 120) },
    };
    try { body.tz = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) {}
    if (UTM) body.utm = UTM;
    post(body);
  }

  function current() { return location.pathname + (location.hash || ""); }
  function fire() { send(current()); }

  if (document.readyState === "complete" || document.readyState === "interactive") fire();
  else document.addEventListener("DOMContentLoaded", fire);

  ["pushState", "replaceState"].forEach(function (m) {
    var orig = history[m];
    if (typeof orig === "function") {
      history[m] = function () { var r = orig.apply(this, arguments); setTimeout(fire, 0); return r; };
    }
  });
  window.addEventListener("popstate", fire);
  window.addEventListener("hashchange", fire);

  window.addEventListener("pagehide", sendLeave);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") sendLeave();
  });
})();
