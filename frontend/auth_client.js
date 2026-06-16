// auth_client.js — shared dual-mode auth for Locker Room SPAs (de-Wix Phase 2).
//
// Exposes window.TFAuth. Two auth modes, chosen automatically:
//   legacy : a Wix-style ?key= (+ ?email=) in the URL -> X-Client-Key + ?email.
//            IDENTICAL to the pre-Clerk behaviour; Clerk is never loaded.
//   clerk  : no ?key= and a Clerk session -> a fresh Authorization: Bearer <token>
//            per request; email derived server-side.
//
// "Members area, auth once" design — Clerk loads in exactly ONE place:
//   * Top frame (the portal, or a page opened standalone) LOADS Clerk directly and
//     acts as a token PROVIDER for its child iframes.
//   * Child iframes do NOT load Clerk. They RELAY to the parent via postMessage to
//     learn the auth status and to mint a fresh token per request. This removes the
//     per-page Clerk download + handshake (the page-load lag).
//
// Legacy (Wix) is untouched: if a ?key= is present we stay legacy and never relay
// or load Clerk.
//
// Config is server-substituted by the /auth_client.js route:
//   __AUTH_V2_ENABLED__  __CLERK_PUBLISHABLE_KEY__  __CLERK_JWT_TEMPLATE__
(function () {
  var CFG = {
    enabled: "__AUTH_V2_ENABLED__" === "1",
    pk: "__CLERK_PUBLISHABLE_KEY__",
    tmpl: "__CLERK_JWT_TEMPLATE__",
  };
  var P = new URLSearchParams(location.search);
  var legacyKey = (P.get("key") || "").trim();
  var legacyEmail = (P.get("email") || "").trim().toLowerCase();
  var apiBase = (P.get("api") || "https://api.nextpointtennis.com").trim();
  var inIframe = (window.self !== window.top);

  var mode = null;        // 'legacy' | 'clerk'
  var clerk = null;       // top-frame Clerk instance (provider only)
  var relayEmail = null;  // email learned from the parent in relay mode
  var readyP = null;

  function pkOk(k) { return !!k && k.indexOf("__") !== 0 && /^pk_(test|live)_/.test(k); }
  function tmplOpts() { return (CFG.tmpl && CFG.tmpl.indexOf("__") !== 0) ? { template: CFG.tmpl } : undefined; }
  function frontendApi(k) {
    try { return atob(k.split("_").slice(2).join("_")).replace(/\$+$/, ""); }
    catch (e) { return null; }
  }
  function loadClerk() {
    return new Promise(function (resolve, reject) {
      var host = frontendApi(CFG.pk);
      if (!host) return reject(new Error("bad pk"));
      var s = document.createElement("script");
      s.async = true; s.crossOrigin = "anonymous";
      s.setAttribute("data-clerk-publishable-key", CFG.pk);
      s.src = "https://" + host + "/npm/@clerk/clerk-js@5/dist/clerk.browser.js";
      s.onload = resolve; s.onerror = reject;
      document.head.appendChild(s);
    });
  }

  // ---- relay client (child iframe -> parent portal) --------------------------
  var _reqId = 0, _pending = {};
  function callParent(kind) {
    return new Promise(function (resolve) {
      var id = ++_reqId;
      _pending[id] = resolve;
      try { window.parent.postMessage({ __tfauth: 1, dir: "req", id: id, kind: kind }, "*"); }
      catch (e) { delete _pending[id]; resolve(null); return; }
      setTimeout(function () { if (_pending[id]) { delete _pending[id]; resolve(null); } }, 4000);
    });
  }

  // ---- single message listener: client responses + provider requests ---------
  window.addEventListener("message", function (e) {
    var d = e.data;
    if (!d || d.__tfauth !== 1) return;
    // client side: a response to one of our requests
    if (d.dir === "res" && _pending[d.id]) {
      var r = _pending[d.id]; delete _pending[d.id]; r(d.payload); return;
    }
    // provider side: a child asking for status/token. Same-origin children only.
    if (d.dir === "req" && !inIframe) {
      if (e.origin !== location.origin) return;   // never serve a token cross-origin
      serveChild(d, e.source);
    }
  });

  async function serveChild(d, src) {
    await ready();              // ensure our (top-frame) Clerk is resolved
    var payload = null;
    if (d.kind === "status") {
      payload = { mode: mode, email: email() };
    } else if (d.kind === "token") {
      if (mode === "clerk" && clerk && clerk.session) {
        try { payload = await clerk.session.getToken(tmplOpts()); } catch (e2) { payload = null; }
      }
    }
    try { src.postMessage({ __tfauth: 1, dir: "res", id: d.id, payload: payload }, "*"); } catch (e3) {}
  }

  // ---- resolve mode once -----------------------------------------------------
  function ready() {
    if (readyP) return readyP;
    readyP = (async function () {
      if (legacyKey) { mode = "legacy"; return; }                 // Wix embed — never touch Clerk
      if (!CFG.enabled || !pkOk(CFG.pk)) { mode = "legacy"; return; }
      if (inIframe) {
        // Child: ask the parent portal (don't load Clerk here).
        var status = await callParent("status");
        if (status && status.mode === "clerk") { mode = "clerk"; relayEmail = status.email || ""; }
        else { mode = "legacy"; }
        return;
      }
      // Top frame: load Clerk directly (this is the one place it loads).
      try {
        await loadClerk();
        clerk = window.Clerk;
        await clerk.load();
        mode = (clerk && clerk.user) ? "clerk" : "legacy";
      } catch (e) { mode = "legacy"; }
    })();
    return readyP;
  }

  async function authHeaders() {
    await ready();
    if (mode === "clerk") {
      var token = inIframe ? await callParent("token")
                           : (clerk && clerk.session ? await clerk.session.getToken(tmplOpts()) : null);
      if (token) return { "Authorization": "Bearer " + token };
    }
    return { "X-Client-Key": legacyKey };
  }

  // legacy keeps ?email=…; clerk derives the email server-side (no email param).
  function emailQS() { return (mode === "clerk") ? "" : ("email=" + encodeURIComponent(legacyEmail)); }

  function isAuthed() {
    if (mode === "clerk") return inIframe ? !!relayEmail : !!(clerk && clerk.user);
    return !!(legacyEmail && legacyKey);
  }

  function email() {
    if (mode === "clerk") {
      if (inIframe) return relayEmail || legacyEmail;
      if (clerk && clerk.user && clerk.user.primaryEmailAddress) return clerk.user.primaryEmailAddress.emailAddress;
    }
    return legacyEmail;
  }

  async function apiFetch(path, opts) {
    opts = opts || {};
    await ready();
    var sep = path.indexOf("?") >= 0 ? "&" : "?";
    var headers = Object.assign({}, await authHeaders(), opts.headers || {});
    var qs = emailQS();
    var url = apiBase + path + (qs ? (sep + qs) : "");
    return fetch(url, Object.assign({}, opts, { headers: headers }));
  }

  // Query string for cross-page navigation (preserves the auth context). In clerk
  // mode we forward only the api base — the next page resolves its own session.
  function navParams(extra) {
    var p = new URLSearchParams();
    if (apiBase) p.set("api", apiBase);
    if (mode !== "clerk") {
      if (legacyEmail) p.set("email", legacyEmail);
      if (legacyKey) p.set("key", legacyKey);
    }
    if (extra) Object.keys(extra).forEach(function (k) { if (extra[k] != null && extra[k] !== "") p.set(k, extra[k]); });
    return p.toString();
  }

  async function signOut() { try { if (clerk && clerk.signOut) await clerk.signOut(); } catch (e) {} }

  window.TFAuth = {
    ready: ready, authHeaders: authHeaders, emailQS: emailQS, apiFetch: apiFetch,
    isAuthed: isAuthed, email: email, navParams: navParams, signOut: signOut,
    apiBase: function () { return apiBase; },
    mode: function () { return mode; },
  };
})();
