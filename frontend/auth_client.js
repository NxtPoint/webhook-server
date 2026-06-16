// auth_client.js — shared dual-mode auth for Locker Room SPAs (de-Wix Phase 2).
//
// Exposes window.TFAuth. Two modes, chosen automatically:
//   legacy : a Wix-style ?key= (+ ?email=) is present in the URL  ->  X-Client-Key
//            header + ?email param. IDENTICAL to the pre-Clerk behaviour.
//   clerk  : no ?key= and a Clerk session exists on THIS origin    ->  a fresh
//            Authorization: Bearer <token> per request; email derived server-side.
//
// Safety: if a legacy ?key= is present we STAY legacy and never load Clerk, so the
// Wix-embedded path is untouched. Clerk JS is loaded lazily (only on first use in
// clerk mode), so injecting this script everywhere is cheap.
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

  var mode = null;     // 'legacy' | 'clerk' (resolved by ready())
  var clerk = null;
  var readyP = null;

  function pkOk(k) { return !!k && k.indexOf("__") !== 0 && /^pk_(test|live)_/.test(k); }
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

  // Resolve the mode once. Loads + initialises Clerk only when there's no legacy
  // key and auth_v2 is enabled. Never throws (falls back to legacy on any error).
  function ready() {
    if (readyP) return readyP;
    readyP = (async function () {
      if (legacyKey) { mode = "legacy"; return; }            // Wix embed — never touch Clerk
      if (!CFG.enabled || !pkOk(CFG.pk)) { mode = "legacy"; return; }
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
      var opts = (CFG.tmpl && CFG.tmpl.indexOf("__") !== 0) ? { template: CFG.tmpl } : undefined;
      var t = await clerk.session.getToken(opts);
      return { "Authorization": "Bearer " + t };
    }
    return { "X-Client-Key": legacyKey };
  }

  // legacy keeps ?email=…; clerk derives the email server-side (no email param).
  function emailQS() { return (mode === "clerk") ? "" : ("email=" + encodeURIComponent(legacyEmail)); }

  function isAuthed() {
    return (mode === "clerk") ? !!(clerk && clerk.user) : !!(legacyEmail && legacyKey);
  }

  function email() {
    if (mode === "clerk" && clerk && clerk.user && clerk.user.primaryEmailAddress)
      return clerk.user.primaryEmailAddress.emailAddress;
    return legacyEmail;
  }

  // The one call sites use: fetch the API with the right auth, dual-mode.
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
  // mode we forward only the api base — the next page resolves its own Clerk session.
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
