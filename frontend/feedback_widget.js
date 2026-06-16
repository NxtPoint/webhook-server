/* Ten-Fifty5 in-app feedback + NPS widget (Prompt 6).
 * Drop-in, dependency-free. Reads auth (email/key/api) from the host page's URL params — the
 * same params the portal already forwards. Renders a floating "Feedback" button + modal, and
 * auto-shows an NPS survey when the API says the user is eligible (after Nth report viewed).
 * Fails silently if the feedback API is disabled/unauthorized. Include with:
 *     <script src="/feedback_widget.js" defer></script>
 */
(function () {
  "use strict";
  var p = new URLSearchParams(location.search);
  var API = p.get("api") || "https://api.nextpointtennis.com";
  var KEY = p.get("key") || "";
  var EMAIL = (p.get("email") || "").trim();
  if (!KEY || !EMAIL) return; // no auth context → stay invisible

  var B = "/api/client/feedback";
  function post(path, body) {
    return fetch(API + path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Client-Key": KEY },
      body: JSON.stringify(Object.assign({ email: EMAIL }, body)),
    });
  }
  function get(path) {
    var u = new URL(API + path);
    u.searchParams.set("email", EMAIL);
    return fetch(u, { headers: { "X-Client-Key": KEY } }).then(function (r) {
      return r.ok ? r.json() : null;
    }).catch(function () { return null; });
  }

  // ---- styles ----
  var css = ""
    + ".tf-fab{position:fixed;right:18px;bottom:18px;z-index:99998;background:#22c55e;color:#06240f;"
    + "border:none;border-radius:99px;padding:11px 16px;font:600 13px Inter,system-ui,sans-serif;"
    + "box-shadow:0 6px 20px rgba(0,0,0,.25);cursor:pointer}"
    + ".tf-ov{position:fixed;inset:0;background:rgba(8,10,14,.55);z-index:99999;display:flex;"
    + "align-items:center;justify-content:center;padding:16px}"
    + ".tf-card{background:#fff;color:#11151c;border-radius:16px;max-width:420px;width:100%;padding:22px;"
    + "font:14px Inter,system-ui,sans-serif;box-shadow:0 20px 60px rgba(0,0,0,.4)}"
    + ".tf-card h3{margin:0 0 6px;font-size:17px}.tf-card p{margin:0 0 14px;color:#5b6573}"
    + ".tf-row{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}"
    + ".tf-n{flex:1;min-width:30px;padding:9px 0;border:1px solid #d5dae2;border-radius:8px;background:#f7f8fa;"
    + "cursor:pointer;text-align:center;font-weight:600}.tf-n.sel{background:#22c55e;color:#06240f;border-color:#22c55e}"
    + ".tf-card textarea,.tf-card select{width:100%;border:1px solid #d5dae2;border-radius:8px;padding:9px;"
    + "font:14px Inter,sans-serif;margin-bottom:12px;box-sizing:border-box}"
    + ".tf-btns{display:flex;gap:8px;justify-content:flex-end}"
    + ".tf-btn{padding:9px 16px;border-radius:8px;border:none;cursor:pointer;font-weight:600}"
    + ".tf-btn.pri{background:#22c55e;color:#06240f}.tf-btn.sec{background:#eef0f3;color:#3a4250}"
    + ".tf-scale{display:flex;justify-content:space-between;font-size:11px;color:#8a93a1;margin:-6px 0 14px}";
  var st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);

  function modal(html) {
    var ov = document.createElement("div"); ov.className = "tf-ov";
    ov.innerHTML = '<div class="tf-card">' + html + "</div>";
    ov.addEventListener("click", function (e) { if (e.target === ov) close(ov); });
    document.body.appendChild(ov);
    return ov;
  }
  function close(ov) { if (ov && ov.parentNode) ov.parentNode.removeChild(ov); }
  function thanks(ov) {
    ov.querySelector(".tf-card").innerHTML =
      "<h3>Thank you! 🎾</h3><p>Your feedback helps us improve.</p>"
      + '<div class="tf-btns"><button class="tf-btn pri" id="tf-done">Close</button></div>';
    ov.querySelector("#tf-done").onclick = function () { close(ov); };
    setTimeout(function () { close(ov); }, 2500);
  }

  // ---- general feedback modal ----
  function openFeedback() {
    var ov = modal(
      "<h3>Send feedback</h3><p>Spotted a bug or have an idea? Tell us.</p>"
      + '<select id="tf-area"><option value="">Area (optional)</option>'
      + "<option>Dashboard</option><option>Upload</option><option>AI Coach</option>"
      + "<option>Billing</option><option>Other</option></select>"
      + '<textarea id="tf-msg" rows="4" placeholder="Your message…"></textarea>'
      + '<div class="tf-btns"><button class="tf-btn sec" id="tf-x">Cancel</button>'
      + '<button class="tf-btn pri" id="tf-send">Send</button></div>');
    ov.querySelector("#tf-x").onclick = function () { close(ov); };
    ov.querySelector("#tf-send").onclick = function () {
      var msg = ov.querySelector("#tf-msg").value.trim();
      if (!msg) return;
      post(B + "/widget", { message: msg, area: ov.querySelector("#tf-area").value || null,
                            page: location.pathname }).catch(function () {});
      thanks(ov);
    };
  }

  // ---- NPS modal ----
  function openNps() {
    var nums = "";
    for (var i = 0; i <= 10; i++) nums += '<div class="tf-n" data-v="' + i + '">' + i + "</div>";
    var ov = modal(
      "<h3>How likely are you to recommend Ten-Fifty5?</h3>"
      + '<p>0 = not likely, 10 = extremely likely.</p>'
      + '<div class="tf-row" id="tf-scale">' + nums + "</div>"
      + '<div class="tf-scale"><span>Not likely</span><span>Very likely</span></div>'
      + '<textarea id="tf-c" rows="3" placeholder="What\'s the main reason? (optional)"></textarea>'
      + '<div class="tf-btns"><button class="tf-btn sec" id="tf-later">Maybe later</button>'
      + '<button class="tf-btn pri" id="tf-sub">Submit</button></div>');
    var score = null;
    ov.querySelectorAll(".tf-n").forEach(function (n) {
      n.onclick = function () {
        ov.querySelectorAll(".tf-n").forEach(function (x) { x.classList.remove("sel"); });
        n.classList.add("sel"); score = parseInt(n.getAttribute("data-v"), 10);
      };
    });
    ov.querySelector("#tf-later").onclick = function () {
      try { sessionStorage.setItem("tf_nps_dismissed", "1"); } catch (e) {}
      close(ov);
    };
    ov.querySelector("#tf-sub").onclick = function () {
      if (score === null) return;
      post(B + "/nps", { score: score, comment: ov.querySelector("#tf-c").value.trim() || null })
        .catch(function () {});
      try { sessionStorage.setItem("tf_nps_dismissed", "1"); } catch (e) {}
      thanks(ov);
    };
  }

  // ---- mount ----
  function mount() {
    var fab = document.createElement("button");
    fab.className = "tf-fab"; fab.textContent = "💬 Feedback";
    fab.onclick = openFeedback;
    document.body.appendChild(fab);

    var dismissed = false;
    try { dismissed = sessionStorage.getItem("tf_nps_dismissed") === "1"; } catch (e) {}
    if (!dismissed) {
      get(B + "/nps-eligibility").then(function (d) {
        if (d && d.ok && d.show) setTimeout(openNps, 1200);
      });
    }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
  else mount();
})();
