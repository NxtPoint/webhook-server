/* Ten-Fifty5 consent screens (DRAFT copy — pending legal sign-off, easily editable).
 * Maps each block to a core.consent type via /api/client/consent/*. No dark patterns:
 * boxes unchecked by default; marketing + biometric separate from terms; pose data named plainly.
 * Auth (key/api/email) read from the host page's URL params. Exposes window.TF_Consent.
 *
 * The exact policy_version is set after the lawyer signs off — pass it into record() calls then.
 */
(function () {
  "use strict";
  var p = new URLSearchParams(location.search);
  var API = p.get("api") || "https://api.nextpointtennis.com";
  var KEY = p.get("key") || "";
  var EMAIL = (p.get("email") || "").trim();
  var POLICY_VERSION = null; // ← set after legal sign-off (e.g. "2026-07-01")

  function post(path, body) {
    return fetch(API + "/api/client/consent" + path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Client-Key": KEY },
      body: JSON.stringify(body),
    }).then(function (r) { return r.json().catch(function () { return { ok: r.ok }; }); });
  }
  function getState(email) {
    var u = new URL(API + "/api/client/consent/state");
    u.searchParams.set("email", email || EMAIL);
    return fetch(u, { headers: { "X-Client-Key": KEY } })
      .then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; });
  }
  function record(email, type, extra) {
    return post("/record", Object.assign(
      { email: email || EMAIL, consent_type: type, policy_version: POLICY_VERSION }, extra || {}));
  }
  function withdraw(email, type) {
    return post("/withdraw", { email: email || EMAIL, consent_type: type });
  }

  // ---- styles (scoped, light) ----
  var css = ""
    + ".tfc-ov{position:fixed;inset:0;background:rgba(8,10,14,.55);z-index:99999;display:flex;align-items:center;justify-content:center;padding:16px}"
    + ".tfc-card{background:#fff;color:#11151c;border-radius:16px;max-width:480px;width:100%;padding:24px;font:14px Inter,system-ui,sans-serif;box-shadow:0 20px 60px rgba(0,0,0,.4);line-height:1.5}"
    + ".tfc-card h3{margin:0 0 10px;font-size:18px}.tfc-card p{margin:0 0 12px;color:#3a4250}"
    + ".tfc-chk{display:flex;gap:9px;align-items:flex-start;margin:12px 0;font-size:13px}"
    + ".tfc-chk input{margin-top:3px}"
    + ".tfc-btns{display:flex;gap:8px;justify-content:flex-end;margin-top:16px}"
    + ".tfc-btn{padding:9px 16px;border-radius:8px;border:none;cursor:pointer;font-weight:600}"
    + ".tfc-btn.pri{background:#22c55e;color:#06240f}.tfc-btn.sec{background:#eef0f3;color:#3a4250}"
    + ".tfc-small{font-size:12px;color:#6b7480}.tfc-row{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid #eef0f3}"
    + ".tfc-toggle{cursor:pointer;font-weight:600}.tfc-on{color:#16a34a}.tfc-off{color:#9aa4b2}";
  var st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);

  function modal(html) {
    var ov = document.createElement("div"); ov.className = "tfc-ov";
    ov.innerHTML = '<div class="tfc-card">' + html + "</div>";
    document.body.appendChild(ov); return ov;
  }
  function close(ov) { if (ov && ov.parentNode) ov.parentNode.removeChild(ov); }

  // ── §1+§2 Signup consent block (terms+privacy required, marketing optional) ──
  // Renders into a container; call .record(email, fullName) after the account is created.
  function signupBlock(container) {
    container.innerHTML =
      '<label class="tfc-chk"><input type="checkbox" id="tfc-terms">'
      + '<span>I agree to the <a href="/privacy" target="_blank">Terms of Service</a> and '
      + '<a href="/privacy" target="_blank">Privacy Policy</a>.</span></label>'
      + '<div class="tfc-small" style="margin:-4px 0 8px 27px">We\'ll only ever use your data to run '
      + "your analysis and improve Ten-Fifty5. You're in control — change your mind anytime in Settings.</div>"
      + '<label class="tfc-chk"><input type="checkbox" id="tfc-mkt">'
      + "<span>Send me tips, product updates and the occasional offer by email. "
      + "<span class='tfc-small'>(Optional — unsubscribe anytime.)</span></span></label>";
    return {
      isValid: function () { return container.querySelector("#tfc-terms").checked; },
      record: function (email, fullName) {
        if (!container.querySelector("#tfc-terms").checked) return Promise.resolve({ ok: false });
        var jobs = [record(email, "terms_of_service", { full_name: fullName, source: "signup" }),
                    record(email, "privacy_policy", { full_name: fullName, source: "signup" })];
        if (container.querySelector("#tfc-mkt").checked)
          jobs.push(record(email, "marketing_email", { full_name: fullName, source: "signup" }));
        return Promise.all(jobs);
      },
    };
  }

  // ── §3 Biometric consent (before first technique/pose analysis) ──
  function biometricModal(opts) {
    opts = opts || {};
    var parental = !!opts.parental;
    var ov = modal(
      "<h3>One quick thing before we analyse " + (parental ? "their" : "your") + " technique</h3>"
      + "<p>To break down " + (parental ? "their" : "your") + " strokes, our technology maps the position of "
      + (parental ? "their" : "your") + " body's joints across each frame of the video — skeletal "
      + '"pose" data. This is considered <b>biometric data</b>, so we ask for explicit permission before we create it.</p>'
      + "<p>We use it only to produce the technique analysis. We never share it, never use it for marketing, "
      + "and you can withdraw this permission anytime — we'll stop pose processing and delete the pose data we hold.</p>"
      + '<label class="tfc-chk"><input type="checkbox" id="tfc-bio">'
      + "<span>I explicitly consent to Ten-Fifty5 processing " + (parental ? "this player's" : "my")
      + " biometric (pose) data to analyse technique.</span></label>"
      + '<div class="tfc-btns"><button class="tfc-btn sec" id="tfc-bio-no">Not now</button>'
      + '<button class="tfc-btn pri" id="tfc-bio-yes">Agree &amp; analyse</button></div>');
    ov.querySelector("#tfc-bio-no").onclick = function () { close(ov); if (opts.onCancel) opts.onCancel(); };
    ov.querySelector("#tfc-bio-yes").onclick = function () {
      if (!ov.querySelector("#tfc-bio").checked) return;
      var extra = { source: "technique" };
      if (opts.subject_person_public_id) extra.subject_person_public_id = opts.subject_person_public_id;
      record(opts.email, "biometric_processing", extra).finally(function () {
        close(ov); if (opts.onAgree) opts.onAgree();
      });
    };
  }

  // ── §4 Parental/guardian consent (adding a junior) ──
  function parentalModal(opts) {
    opts = opts || {};
    var name = opts.juniorName || "this player";
    var ov = modal(
      "<h3>You're setting up an account for a young player</h3>"
      + "<p>Because <b>" + name + "</b> is under 16, we need you, as their parent or guardian, to give "
      + "permission for Ten-Fifty5 to process their data — including profile details and, if you use "
      + "technique analysis, their biometric (pose) data from match video.</p>"
      + "<p>You stay in control: you can review, export or delete their data at any time.</p>"
      + '<label class="tfc-chk"><input type="checkbox" id="tfc-par">'
      + "<span>I am the parent or legal guardian of this player and I consent to Ten-Fifty5 processing "
      + "their data, including biometric (pose) data for technique analysis, as described in the "
      + '<a href="/privacy" target="_blank">Privacy Policy</a>.</span></label>'
      + '<div class="tfc-btns"><button class="tfc-btn pri" id="tfc-par-go">Confirm &amp; continue</button></div>');
    ov.querySelector("#tfc-par-go").onclick = function () {
      if (!ov.querySelector("#tfc-par").checked) return;
      record(opts.email, "minor_processing_parental", {
        subject_name: opts.juniorName, subject_dob: opts.juniorDob || null, source: "add_junior",
      }).finally(function () { close(ov); if (opts.onConfirm) opts.onConfirm(); });
    };
  }

  // ── §5 Settings: privacy & consent management ──
  function renderSettings(container, email) {
    email = email || EMAIL;
    container.innerHTML = '<div class="tfc-small">Loading…</div>';
    getState(email).then(function (s) {
      if (!s) { container.innerHTML = '<div class="tfc-small">Sign in to manage your privacy choices.</div>'; return; }
      var mkt = !!s.marketing_opt_in;
      var bio = (s.consents || {}).biometric_processing === "granted";
      function row(label, on, sub, id) {
        return '<div class="tfc-row"><div><div>' + label + '</div><div class="tfc-small">' + sub + "</div></div>"
          + '<div class="tfc-toggle ' + (on ? "tfc-on" : "tfc-off") + '" id="' + id + '">' + (on ? "On" : "Off") + "</div></div>";
      }
      container.innerHTML =
        "<h3>Your privacy choices</h3>"
        + row("Marketing emails", mkt, "Turn off anytime; you'll still get essential service emails.", "tfc-t-mkt")
        + row("Biometric (pose) processing", bio, "Turning off stops future technique analysis and deletes pose data we hold.", "tfc-t-bio")
        + '<div class="tfc-row"><div>Your data</div><div><a href="#" id="tfc-dl">Download</a> · <a href="#" id="tfc-del">Delete account</a></div></div>'
        + '<div class="tfc-small" style="margin-top:10px">Questions or a specific request? Email info@ten-fifty5.com.</div>';
      container.querySelector("#tfc-t-mkt").onclick = function () {
        (mkt ? withdraw(email, "marketing_email") : record(email, "marketing_email")).then(function () { renderSettings(container, email); });
      };
      container.querySelector("#tfc-t-bio").onclick = function () {
        (bio ? withdraw(email, "biometric_processing") : record(email, "biometric_processing")).then(function () { renderSettings(container, email); });
      };
      container.querySelector("#tfc-dl").onclick = function (e) { e.preventDefault(); post("/dsar", { email: email, request_type: "access" }).then(function () { alert("Data download request received — we'll email it to you."); }); };
      container.querySelector("#tfc-del").onclick = function (e) { e.preventDefault(); if (confirm("Request account deletion? We'll process this and confirm by email.")) post("/dsar", { email: email, request_type: "erasure" }).then(function () { alert("Deletion request received."); }); };
    });
  }

  window.TF_Consent = {
    signupBlock: signupBlock,
    biometricModal: biometricModal,
    parentalModal: parentalModal,
    renderSettings: renderSettings,
    record: record, withdraw: withdraw, getState: getState,
    setPolicyVersion: function (v) { POLICY_VERSION = v; },
  };
})();
