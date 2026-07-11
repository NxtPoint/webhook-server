/* attribution.js — first-touch ad-click / UTM capture for Google Ads offline conversions.
 *
 * Ten-Fifty5. Deliberately NOT part of the shared cookieless beacon (analytics.js) — ad-click
 * attribution is a distinct concern. On EVERY page it records the FIRST gclid/gbraid/wbraid/fbclid +
 * utm_* seen (first-touch wins). Once on a logged-in page (which carries ?email= & ?key=), it flushes
 * ONCE to POST /api/client/acquisition, which persists onto core.acquisition. A later scheduled CSV
 * upload sends the REAL downstream purchase to Google Ads by gclid — so Ads bids for buyers, not
 * clickers. Safe no-op on organic visits + logged-out pages. Never blocks or breaks the page.
 */
(function () {
  "use strict";
  var STORE = "tf_attr", DONE = "tf_attr_flushed";
  var CLICK = ["gclid", "gbraid", "wbraid", "fbclid"];
  var UTM = ["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"];
  var qp = new URLSearchParams(location.search);

  function capture() {
    try {
      if (localStorage.getItem(STORE)) return;              // first-touch wins — never overwrite
      var attr = {}, has = false;
      CLICK.concat(UTM).forEach(function (k) {
        var v = qp.get(k);
        if (v) { attr[k] = String(v).slice(0, 512); has = true; }
      });
      if (!has) return;                                     // organic visit — nothing to attribute
      attr.landing_page = location.pathname.slice(0, 512);
      attr.referrer = (document.referrer || "").slice(0, 512);
      attr.ts = new Date().toISOString();
      localStorage.setItem(STORE, JSON.stringify(attr));
    } catch (e) { /* storage disabled (private mode) — ignore */ }
  }

  function flush() {
    try {
      if (localStorage.getItem(DONE)) return;               // already persisted
      var raw = localStorage.getItem(STORE);
      if (!raw) return;                                     // nothing captured
      var email = qp.get("email"), key = qp.get("key");
      if (!email || !key) return;                           // logged-out page — flush on a later authed load
      var API = (window.__API_BASE || qp.get("api") || "https://api.nextpointtennis.com").replace(/\/+$/, "");
      var attr;
      try { attr = JSON.parse(raw); } catch (e) { return; }
      attr.email = email;
      fetch(API + "/api/client/acquisition", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Client-Key": key },
        body: JSON.stringify(attr)
      }).then(function (r) {
        if (r && r.ok) { try { localStorage.setItem(DONE, "1"); } catch (e) {} }
      }).catch(function () { /* transient — retry on the next authed page load */ });
    } catch (e) { /* never break the page */ }
  }

  capture();
  if (document.readyState === "complete" || document.readyState === "interactive") flush();
  else window.addEventListener("DOMContentLoaded", flush);
})();
