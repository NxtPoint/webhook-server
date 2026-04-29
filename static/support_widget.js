/* ===================================================================
 * support_widget.js — Ten-Fifty5 customer-service chat bubble.
 *
 * Self-contained, zero deps. Drop a single <script src="/static/support_widget.js" defer>
 * tag into any portal page; the widget self-mounts a floating bubble
 * + panel at the bottom-right.
 *
 * Reads auth context (email, firstName, key, api base) from URL params,
 * mirroring the pattern every portal page already uses.
 *
 * Iframe idempotency: when both the portal shell AND the inner page
 * include this script, the bubble appears exactly once — anchored to
 * the outermost frame (parent), inner-frame mount short-circuits.
 *
 * Endpoints (all X-Client-Key authenticated):
 *   POST /api/support/ask
 *   POST /api/support/feedback
 *   POST /api/support/escalate
 *
 * Reuses look-and-feel from the AI Coach (.coach-pill / .coach-quick-btn
 * patterns, same green palette, same "thinking…" spinner copy).
 * =================================================================== */
(function () {
  'use strict';

  // ---------- 1. Idempotency: bail if already mounted ----------
  // We mount on the OUTERMOST frame we can see. If we're inside an iframe
  // and the parent already has the widget, short-circuit. If the parent
  // is cross-origin (try/catch fires) we treat it as not-mounted and
  // mount anyway — better one extra bubble than zero.
  if (window.__nf5_support_mounted__) return;
  try {
    if (window.top !== window && window.top.__nf5_support_mounted__) return;
  } catch (e) {
    /* cross-origin parent — proceed and mount here */
  }
  window.__nf5_support_mounted__ = true;

  // ---------- 2. Read auth context from URL params ----------
  // Mirrors the pattern already used by locker_room.html / match_analysis.html /
  // pricing.html etc. — `?email=&firstName=&key=&api=&...`. If any of those
  // pages already exposes window.__nf5_auth__, prefer that.
  const params = new URLSearchParams(location.search);
  const auth = window.__nf5_auth__ || {};
  const EMAIL = (auth.email || params.get('email') || '').trim().toLowerCase();
  const FIRST_NAME = (auth.firstName || params.get('firstName') || '').trim();
  const API_KEY = (auth.key || params.get('key') || '').trim();
  const API_BASE = (auth.api || params.get('api') || 'https://api.nextpointtennis.com')
    .trim().replace(/\/+$/, '');
  const PAGE_CONTEXT = location.pathname || '/';

  // No auth → no widget. Public marketing pages, register/coach-accept
  // flows etc. will silently skip the bubble.
  if (!EMAIL || !API_KEY) {
    console.info('[support_widget] no auth — widget not mounted');
    return;
  }

  // ---------- 3. Session-scoped state ----------
  // Conversation id + turn history persist within this browser tab only
  // (cleared on tab close). Survives in-portal navigation between
  // sub-pages because all portal pages share the same window.
  let conversationId = sessionStorage.getItem('nf5_support_cid') || null;
  let turns = (function () {
    try { return JSON.parse(sessionStorage.getItem('nf5_support_turns') || '[]'); }
    catch (_) { return []; }
  })();
  let busy = false;
  let panelOpen = false;
  let escalationSent = false;

  function persistState() {
    if (conversationId) sessionStorage.setItem('nf5_support_cid', conversationId);
    sessionStorage.setItem('nf5_support_turns', JSON.stringify(turns));
  }

  // ---------- 4. DOM construction ----------
  injectStyles();

  const root = document.createElement('div');
  root.id = 'nf5-support-widget';
  root.innerHTML = `
    <button class="nf5-bubble" type="button" aria-label="Open help and support" aria-expanded="false">
      <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
        <path fill="currentColor" d="M12 2C6.48 2 2 6.04 2 11c0 2.74 1.4 5.18 3.6 6.83V22l3.7-2.04A11 11 0 0 0 12 20c5.52 0 10-4.04 10-9S17.52 2 12 2Zm-1 13H9V8h2v7Zm4 0h-2v-4h2v4Zm0-6h-2V7h2v2Z"/>
      </svg>
    </button>

    <section class="nf5-panel" role="dialog" aria-label="Help and support" aria-modal="false" hidden>
      <header class="nf5-header">
        <div class="nf5-title">
          <span class="nf5-spark" aria-hidden="true">✦</span>
          <span>Help &amp; Support</span>
        </div>
        <button class="nf5-close" type="button" aria-label="Close support">
          <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
            <path fill="currentColor" d="M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12 19 6.41Z"/>
          </svg>
        </button>
      </header>

      <div class="nf5-body" role="log" aria-live="polite"></div>

      <footer class="nf5-footer">
        <div class="nf5-input-row">
          <input type="text" class="nf5-input" maxlength="1000"
                 placeholder="Ask a question…" aria-label="Type your question" />
          <button type="button" class="nf5-send" aria-label="Send message">
            <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
              <path fill="currentColor" d="M3.4 20.4 22 12 3.4 3.6 3.39 10.13 17 12l-13.61 1.87L3.4 20.4Z"/>
            </svg>
          </button>
        </div>
      </footer>
    </section>
  `;
  document.body.appendChild(root);

  const bubbleEl = root.querySelector('.nf5-bubble');
  const panelEl = root.querySelector('.nf5-panel');
  const bodyEl = root.querySelector('.nf5-body');
  const inputEl = root.querySelector('.nf5-input');
  const sendEl = root.querySelector('.nf5-send');
  const closeEl = root.querySelector('.nf5-close');

  // ---------- 5. Wire events ----------
  bubbleEl.addEventListener('click', togglePanel);
  closeEl.addEventListener('click', () => setPanelOpen(false));
  sendEl.addEventListener('click', () => onSend());
  inputEl.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && !ev.shiftKey) {
      ev.preventDefault();
      onSend();
    }
  });

  // Cmd/Ctrl + / toggles the panel; Esc closes it.
  document.addEventListener('keydown', (ev) => {
    if ((ev.metaKey || ev.ctrlKey) && ev.key === '/') {
      ev.preventDefault();
      togglePanel();
    } else if (ev.key === 'Escape' && panelOpen) {
      setPanelOpen(false);
    }
  });

  // ---------- 6. Initial render ----------
  renderBody();

  // ---------- 7. Sidebar-launched mode ----------
  // On portal.html the bubble would duplicate the new "Help & Support"
  // sidebar entry — hide it. Direct page access (e.g. /match-analysis
  // accessed without the portal shell) keeps the bubble as a fallback
  // so the user always has a way in.
  const onPortalShell = location.pathname.replace(/\/+$/, '') === '/portal';
  if (onPortalShell) bubbleEl.style.display = 'none';

  // Expose simple open/close so the portal sidebar (or any host page)
  // can trigger the panel without knowing the widget's internals.
  window.nf5SupportOpen  = () => setPanelOpen(true);
  window.nf5SupportClose = () => setPanelOpen(false);
  window.nf5SupportToggle = togglePanel;

  // =================================================================
  //   Functions
  // =================================================================

  function togglePanel() { setPanelOpen(!panelOpen); }

  function setPanelOpen(open) {
    panelOpen = open;
    panelEl.hidden = !open;
    bubbleEl.setAttribute('aria-expanded', String(open));
    root.classList.toggle('nf5-open', open);
    if (open) {
      setTimeout(() => inputEl.focus(), 50);
      // Scroll body to bottom so the latest turn is visible
      requestAnimationFrame(() => { bodyEl.scrollTop = bodyEl.scrollHeight; });
    }
  }

  function renderBody() {
    if (!turns.length) {
      bodyEl.innerHTML = `
        <div class="nf5-greeting">
          <p class="nf5-hi">Hi ${escapeHtml(FIRST_NAME || 'there')} 👋</p>
          <p class="nf5-tag">Ask me anything about Ten-Fifty5.</p>
        </div>
        <div class="nf5-quick-row">
          ${quickChip('Cancel my plan')}
          ${quickChip('Add a new match')}
          ${quickChip('Invite my coach')}
          ${quickChip('Billing question')}
        </div>
      `;
      bodyEl.querySelectorAll('[data-quick]').forEach((btn) => {
        btn.addEventListener('click', () => {
          inputEl.value = btn.getAttribute('data-quick');
          onSend();
        });
      });
      return;
    }

    bodyEl.innerHTML = turns.map((t, i) => renderTurnHtml(t, i)).join('') + (escalationSent
      ? `<div class="nf5-toast">Email sent to our team — we'll be in touch.</div>`
      : '');
    // wire feedback + escalate buttons for each turn
    bodyEl.querySelectorAll('[data-thumb]').forEach((b) => {
      b.addEventListener('click', () => onFeedback(b.getAttribute('data-turn-id'), b.getAttribute('data-thumb'), b));
    });
    bodyEl.querySelectorAll('[data-escalate]').forEach((b) => {
      b.addEventListener('click', () => onEscalate());
    });

    requestAnimationFrame(() => { bodyEl.scrollTop = bodyEl.scrollHeight; });
  }

  function quickChip(label) {
    return `<button class="nf5-quick-btn" type="button" data-quick="${escapeHtml(label)}">${escapeHtml(label)}</button>`;
  }

  function renderTurnHtml(turn, idx) {
    const userBlock = `
      <div class="nf5-msg nf5-msg-user">
        <div class="nf5-bubble-text">${escapeHtml(turn.question || '')}</div>
      </div>
    `;
    if (turn.pending) {
      return userBlock + `
        <div class="nf5-msg nf5-msg-bot">
          <div class="nf5-loading"><span class="nf5-spinner"></span>Support thinking…</div>
        </div>
      `;
    }
    if (turn.error) {
      return userBlock + `
        <div class="nf5-msg nf5-msg-bot">
          <div class="nf5-empty">Network error — please try again or email info@ten-fifty5.com</div>
        </div>
      `;
    }
    const answerHtml = highlightStats(turn.answer || '');
    const actionsHtml = (turn.actions || []).map((a) =>
      `<a class="nf5-action" href="${escapeAttr(a.href)}" target="_top" rel="noopener">${escapeHtml(a.label)}<span aria-hidden="true">↗</span></a>`
    ).join('');
    const showEscalate = turn.needs_human || turn.confidence === 'low';
    const escalateRow = showEscalate && !escalationSent
      ? `<button class="nf5-escalate" type="button" data-escalate>This didn't help — email us</button>`
      : '';
    const thumbState = turn.feedback ? `nf5-thumbs-given nf5-thumbs-${turn.feedback}` : '';
    return userBlock + `
      <div class="nf5-msg nf5-msg-bot">
        <div class="nf5-answer">${answerHtml}</div>
        ${actionsHtml ? `<div class="nf5-actions">${actionsHtml}</div>` : ''}
        ${escalateRow}
        <div class="nf5-thumbs ${thumbState}">
          <button type="button" class="nf5-thumb" data-thumb="up" data-turn-id="${escapeAttr(turn.id || '')}" aria-label="Helpful" ${!turn.id ? 'disabled' : ''}>👍</button>
          <button type="button" class="nf5-thumb" data-thumb="down" data-turn-id="${escapeAttr(turn.id || '')}" aria-label="Not helpful" ${!turn.id ? 'disabled' : ''}>👎</button>
        </div>
      </div>
    `;
  }

  // ---------- API calls ----------

  async function onSend() {
    const q = (inputEl.value || '').trim();
    if (!q || busy) return;
    inputEl.value = '';

    const pendingTurn = { question: q, pending: true };
    turns.push(pendingTurn);
    busy = true;
    renderBody();

    try {
      const res = await fetch(API_BASE + '/api/support/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Client-Key': API_KEY },
        body: JSON.stringify({
          message: q,
          email: EMAIL,
          page_context: PAGE_CONTEXT,
          conversation_id: conversationId || undefined,
        }),
      });
      const data = await res.json().catch(() => ({}));

      if (!res.ok || !data.ok) {
        if (res.status === 429) {
          replaceLastTurn({ question: q, error: false, answer:
            "You've hit today's limit. Please email info@ten-fifty5.com if it's urgent.",
            confidence: 'low', needs_human: true, cited_sections: [], actions: [] });
        } else {
          replaceLastTurn({ question: q, error: true });
        }
        return;
      }

      // Capture conversation_id from first answer.
      if (!conversationId && data.conversation_id) conversationId = data.conversation_id;

      // Look up the turn id from server (we don't always get one — only on
      // logged turns; cached + fail-safe paths return without a turn_id).
      // The /ask endpoint logs every turn, so we can ask the server for its
      // id if we need feedback. For now we use turn_idx + cid as a fallback.
      replaceLastTurn({
        question:       q,
        answer:         data.answer || '',
        confidence:     data.confidence || 'medium',
        needs_human:    !!data.needs_human,
        cited_sections: data.cited_sections || [],
        actions:        data.actions || [],
        // We don't have the row uuid in the response, but we can identify
        // the turn by (cid, turn_idx) for feedback. Stored as a synthetic id:
        id:             `${data.conversation_id}:${data.turn_idx}`,
      });
    } catch (e) {
      console.error('[support_widget] /ask failed', e);
      replaceLastTurn({ question: q, error: true });
    } finally {
      busy = false;
    }
  }

  function replaceLastTurn(turn) {
    turns[turns.length - 1] = turn;
    persistState();
    renderBody();
  }

  async function onFeedback(turnIdRaw, rating, btnEl) {
    const turn = turns.find((t) => t.id === turnIdRaw);
    if (!turn || turn.feedback) return;
    turn.feedback = rating;
    persistState();
    btnEl.parentElement.classList.add('nf5-thumbs-given', 'nf5-thumbs-' + rating);

    // Server-side: we don't have the row uuid, only synthetic (cid:turn_idx).
    // Skip the API call when the id isn't a uuid. The thumbs state still
    // persists locally for the user's session — fine for v1, can be
    // upgraded server-side later by returning the row uuid in /ask.
    const idIsUuid = /^[0-9a-f-]{36}$/i.test(turnIdRaw);
    if (!idIsUuid) return;
    try {
      await fetch(API_BASE + '/api/support/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Client-Key': API_KEY },
        body: JSON.stringify({ turn_id: turnIdRaw, rating }),
      });
    } catch (_) { /* best-effort */ }
  }

  async function onEscalate() {
    if (escalationSent || !conversationId) return;
    try {
      const res = await fetch(API_BASE + '/api/support/escalate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Client-Key': API_KEY },
        body: JSON.stringify({
          conversation_id: conversationId,
          email: EMAIL,
          user_note: 'Escalated from support widget',
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (data && data.ok) {
        escalationSent = true;
        renderBody();
      } else {
        alert('Could not send the email. Please write to info@ten-fifty5.com directly.');
      }
    } catch (e) {
      alert('Could not send the email. Please write to info@ten-fifty5.com directly.');
    }
  }

  // ---------- Helpers ----------

  function highlightStats(s) {
    const safe = escapeHtml(s);
    return safe.replace(/\[([^\]]+)\]/g, '<span class="nf5-pill">$1</span>');
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function escapeAttr(s) { return escapeHtml(s); }

  // ---------- Styles ----------

  function injectStyles() {
    const css = `
      :where(#nf5-support-widget) {
        --nf5-green:      var(--green, #1a5c2e);
        --nf5-green-bg:   var(--green-bg, rgba(26,92,46,0.08));
        --nf5-white:      var(--white, #ffffff);
        --nf5-bg:         var(--bg, #f5f5f5);
        --nf5-text:       var(--text, #1a1a1a);
        --nf5-text-sec:   var(--text-sec, #6b7280);
        --nf5-text-dim:   var(--text-dim, #9ca3af);
        --nf5-border:     var(--border, #e5e5e5);
        --nf5-radius:     var(--radius, 4px);
        --nf5-amber:      var(--amber, #d97706);
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        color: var(--nf5-text);
      }
      :where(#nf5-support-widget) *, :where(#nf5-support-widget) *::before, :where(#nf5-support-widget) *::after {
        box-sizing: border-box;
      }

      /* Bubble */
      :where(#nf5-support-widget) .nf5-bubble {
        position: fixed; right: 20px; bottom: 20px; width: 56px; height: 56px;
        border-radius: 50%; border: none; cursor: pointer;
        background: var(--nf5-green); color: var(--nf5-white);
        box-shadow: 0 4px 16px rgba(0,0,0,0.18);
        display: flex; align-items: center; justify-content: center;
        z-index: 999998;
        transition: transform 0.18s ease, box-shadow 0.18s ease;
      }
      :where(#nf5-support-widget) .nf5-bubble:hover {
        transform: translateY(-1px); box-shadow: 0 6px 18px rgba(0,0,0,0.22);
      }
      :where(#nf5-support-widget).nf5-open .nf5-bubble { display: none; }

      /* Panel */
      :where(#nf5-support-widget) .nf5-panel {
        position: fixed; right: 20px; bottom: 20px;
        width: 380px; max-width: calc(100vw - 24px);
        height: 560px; max-height: calc(100vh - 32px);
        background: var(--nf5-white); border: 1px solid var(--nf5-border);
        border-radius: 8px; box-shadow: 0 10px 32px rgba(0,0,0,0.18);
        display: flex; flex-direction: column; overflow: hidden;
        z-index: 999999;
      }

      /* Header */
      :where(#nf5-support-widget) .nf5-header {
        background: var(--nf5-green); color: var(--nf5-white);
        padding: 12px 16px; display: flex; align-items: center; justify-content: space-between;
        flex-shrink: 0;
      }
      :where(#nf5-support-widget) .nf5-title {
        display: flex; align-items: center; gap: 8px;
        font-weight: 700; font-size: 0.92rem; letter-spacing: 0.3px;
      }
      :where(#nf5-support-widget) .nf5-spark { font-size: 1rem; opacity: 0.9; }
      :where(#nf5-support-widget) .nf5-close {
        background: transparent; border: none; color: var(--nf5-white); cursor: pointer;
        padding: 4px; border-radius: var(--nf5-radius); display: flex; opacity: 0.85;
      }
      :where(#nf5-support-widget) .nf5-close:hover { opacity: 1; background: rgba(255,255,255,0.12); }

      /* Body (scroll area) */
      :where(#nf5-support-widget) .nf5-body {
        flex: 1; overflow-y: auto; padding: 14px 14px 6px;
        display: flex; flex-direction: column; gap: 10px;
        background: var(--nf5-white);
      }

      /* Greeting + quick chips */
      :where(#nf5-support-widget) .nf5-greeting {
        padding: 8px 4px 4px;
      }
      :where(#nf5-support-widget) .nf5-hi {
        font-size: 1rem; font-weight: 700; color: var(--nf5-text); margin: 0 0 4px;
      }
      :where(#nf5-support-widget) .nf5-tag {
        font-size: 0.85rem; color: var(--nf5-text-sec); margin: 0;
      }
      :where(#nf5-support-widget) .nf5-quick-row {
        display: flex; flex-direction: column; gap: 6px; padding: 6px 0 4px;
      }
      :where(#nf5-support-widget) .nf5-quick-btn {
        text-align: left; padding: 8px 12px; border-radius: var(--nf5-radius);
        border: 1px solid var(--nf5-border); background: var(--nf5-white);
        font-size: 0.83rem; font-weight: 500; color: var(--nf5-text-sec);
        cursor: pointer; transition: all 0.18s ease; font-family: inherit;
      }
      :where(#nf5-support-widget) .nf5-quick-btn:hover {
        border-color: var(--nf5-green); color: var(--nf5-green); background: var(--nf5-green-bg);
      }

      /* Messages */
      :where(#nf5-support-widget) .nf5-msg { display: flex; flex-direction: column; gap: 6px; }
      :where(#nf5-support-widget) .nf5-msg-user { align-items: flex-end; }
      :where(#nf5-support-widget) .nf5-msg-bot  { align-items: stretch; }
      :where(#nf5-support-widget) .nf5-bubble-text {
        background: var(--nf5-green); color: var(--nf5-white);
        padding: 8px 12px; border-radius: 14px 14px 2px 14px;
        font-size: 0.85rem; line-height: 1.4;
        max-width: 85%; word-wrap: break-word;
      }
      :where(#nf5-support-widget) .nf5-answer {
        background: var(--nf5-green-bg); border-left: 3px solid var(--nf5-green);
        padding: 10px 14px; border-radius: 0 var(--nf5-radius) var(--nf5-radius) 0;
        font-size: 0.86rem; line-height: 1.55; color: var(--nf5-text);
        white-space: pre-wrap;
      }
      :where(#nf5-support-widget) .nf5-pill {
        display: inline-block; padding: 1px 8px; border-radius: 10px;
        background: var(--nf5-green); color: var(--nf5-white);
        font-weight: 700; font-size: 0.72rem; margin: 0 2px;
        opacity: 0.92;
      }

      /* Inline action buttons */
      :where(#nf5-support-widget) .nf5-actions {
        display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px;
      }
      :where(#nf5-support-widget) .nf5-action {
        display: inline-flex; align-items: center; gap: 4px;
        padding: 6px 12px; border-radius: var(--nf5-radius);
        border: 1px solid var(--nf5-border); background: var(--nf5-white);
        font-size: 0.78rem; font-weight: 500; color: var(--nf5-text-sec);
        text-decoration: none; cursor: pointer; transition: all 0.18s ease;
      }
      :where(#nf5-support-widget) .nf5-action:hover {
        border-color: var(--nf5-green); color: var(--nf5-green); background: var(--nf5-green-bg);
      }
      :where(#nf5-support-widget) .nf5-action span { font-size: 0.7rem; opacity: 0.8; }

      /* Escalation CTA */
      :where(#nf5-support-widget) .nf5-escalate {
        align-self: flex-start; margin-top: 6px;
        padding: 8px 14px; border-radius: var(--nf5-radius);
        border: 1px solid var(--nf5-amber); background: rgba(217, 119, 6, 0.08);
        color: var(--nf5-amber); font-size: 0.8rem; font-weight: 600;
        cursor: pointer; transition: all 0.18s ease; font-family: inherit;
      }
      :where(#nf5-support-widget) .nf5-escalate:hover {
        background: rgba(217, 119, 6, 0.14);
      }

      /* Thumbs */
      :where(#nf5-support-widget) .nf5-thumbs {
        display: flex; gap: 6px; margin-top: 4px;
      }
      :where(#nf5-support-widget) .nf5-thumb {
        background: transparent; border: 1px solid var(--nf5-border);
        border-radius: var(--nf5-radius); width: 30px; height: 26px;
        font-size: 0.85rem; cursor: pointer; opacity: 0.65; transition: all 0.18s ease;
        padding: 0; line-height: 1;
      }
      :where(#nf5-support-widget) .nf5-thumb:hover { opacity: 1; border-color: var(--nf5-green); }
      :where(#nf5-support-widget) .nf5-thumb:disabled { cursor: default; opacity: 0.35; }
      :where(#nf5-support-widget) .nf5-thumbs-given .nf5-thumb { pointer-events: none; opacity: 0.4; }
      :where(#nf5-support-widget) .nf5-thumbs-up .nf5-thumb[data-thumb="up"]    { opacity: 1; border-color: var(--nf5-green); background: var(--nf5-green-bg); }
      :where(#nf5-support-widget) .nf5-thumbs-down .nf5-thumb[data-thumb="down"]{ opacity: 1; border-color: var(--nf5-amber); background: rgba(217,119,6,0.10); }

      /* Loading spinner mirroring AI Coach .coach-loading */
      :where(#nf5-support-widget) .nf5-loading {
        display: flex; align-items: center; gap: 10px;
        padding: 10px 14px; font-size: 0.82rem; color: var(--nf5-text-sec);
        background: var(--nf5-bg); border-radius: var(--nf5-radius);
      }
      :where(#nf5-support-widget) .nf5-spinner {
        display: inline-block; width: 16px; height: 16px; border: 2px solid var(--nf5-border);
        border-top-color: var(--nf5-green); border-radius: 50%;
        animation: nf5-spin 0.8s linear infinite;
      }
      @keyframes nf5-spin { to { transform: rotate(360deg); } }

      :where(#nf5-support-widget) .nf5-empty {
        padding: 12px; text-align: center; font-size: 0.82rem;
        color: var(--nf5-text-dim); background: var(--nf5-bg); border-radius: var(--nf5-radius);
      }
      :where(#nf5-support-widget) .nf5-toast {
        margin: 8px 0 4px; padding: 10px 14px; border-radius: var(--nf5-radius);
        background: var(--nf5-green-bg); color: var(--nf5-green);
        font-size: 0.82rem; font-weight: 600; text-align: center;
      }

      /* Footer / input */
      :where(#nf5-support-widget) .nf5-footer {
        border-top: 1px solid var(--nf5-border); padding: 10px 12px;
        background: var(--nf5-white); flex-shrink: 0;
      }
      :where(#nf5-support-widget) .nf5-input-row { display: flex; gap: 8px; align-items: center; }
      :where(#nf5-support-widget) .nf5-input {
        flex: 1; padding: 8px 12px; border: 1px solid var(--nf5-border);
        border-radius: var(--nf5-radius); font-size: 0.85rem; font-family: inherit;
        background: var(--nf5-white); color: var(--nf5-text);
      }
      :where(#nf5-support-widget) .nf5-input:focus { outline: none; border-color: var(--nf5-green); }
      :where(#nf5-support-widget) .nf5-send {
        background: var(--nf5-green); color: var(--nf5-white); border: none;
        width: 36px; height: 36px; border-radius: var(--nf5-radius);
        cursor: pointer; display: flex; align-items: center; justify-content: center;
        transition: opacity 0.18s ease;
      }
      :where(#nf5-support-widget) .nf5-send:hover { opacity: 0.9; }

      /* Mobile: full-screen panel */
      @media (max-width: 640px) {
        :where(#nf5-support-widget) .nf5-panel {
          right: 0; bottom: 0; left: 0; top: 0;
          width: 100vw; height: 100vh; max-height: 100vh;
          border-radius: 0; border: none;
        }
        :where(#nf5-support-widget) .nf5-bubble { right: 16px; bottom: 16px; width: 52px; height: 52px; }
      }

      @media (prefers-reduced-motion: reduce) {
        :where(#nf5-support-widget) * { transition: none !important; animation: none !important; }
      }
    `;
    const style = document.createElement('style');
    style.id = 'nf5-support-widget-styles';
    style.textContent = css;
    document.head.appendChild(style);
  }
})();
