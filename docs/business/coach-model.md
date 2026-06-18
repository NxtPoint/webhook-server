# Coach Model

> **Part of the Ten-Fifty5 business documentation set** ([master index](README.md)).
> The canonical statement of how coaches work: invite protocol, the free-vs-paid cap, what coaches can and cannot do, and where the rules are enforced. Behaviour rules live here; tier numerics live in [`pricing-and-packages.md`](pricing-and-packages.md); the implementation file map is in `coach_invite/README.md`.

---

## 1. Positioning — free the channel, monetise the player

Coaches are an **acquisition channel**, not a revenue line. One coach invites 5–20 players; the players pay. So the first linked player is **free forever** and only the 2nd+ player requires a paid Coach Pro plan. Coaches never upload and never consume match credits — their value is read access + AI Coach over their linked players' data.

Marketing anchor (use verbatim on the For-Coaches page):

> *Coaches can't travel to every match. Their kids lose tournaments and the coach doesn't know why. Our data tells them where the kids are falling short — detailed stats, performance history trending over time, the AI Coach giving them game knowledge, technique analysis putting them above their peers. Data-driven coaching, in real time.*

---

## 2. The pricing shape

| State | Price | Linked players | Can upload | AI Coach / technique on linked players |
|---|---|---|---|---|
| Coach (launch) | Free | 1 | ❌ | ✅ |
| Coach Pro | **$50/mo** (direct PayPal, `plan_code 'coach pro'`) | unlimited (2+) | ❌ | ✅ |

- **Coach Pro is sold via direct PayPal** (`paypal_billing/plans.py` + `catalog.json`, LIVE 2026-06-17). `COACH_PRO_UPGRADE_URL` points at `/plans` → coach view → PayPal subscribe button.
- The legacy Wix coach plan IDs are **rollback only** (`PAYPAL_ENABLED=0`).
- The **Free Coach Access** plan (Wix ID `cd2b6772-1880-42ec-9049-4d9e4decc42b`) does **not** count as paid — free-access subscribers still hit the 1-player cap.

Full tier numerics + the coach access matrix: [`pricing-and-packages.md`](pricing-and-packages.md) §6–7.

---

## 3. The invite protocol

`billing.coaches_permission`: `(id, owner_account_id, coach_account_id, coach_email, status, active, invite_token, created_at, updated_at)`. Schema in `coach_invite/db.py`.

- `status ∈ {INVITED, ACCEPTED}` — never anything else; `active` is a boolean kill-switch set false on revoke.
- **One row per `(owner_account_id, coach_email)` pair, ever** — re-invites UPDATE the same row, never INSERT a new one.

### Token lifecycle

- `secrets.token_urlsafe(32)` — single-use; unique partial index `WHERE invite_token IS NOT NULL` prevents collisions.
- **The token IS the auth** — the accept endpoint is public, no API key.
- On accept or revoke the token is NULL'd immediately.
- Re-inviting a revoked coach: same row, new token, status reset to `INVITED`.

Accept flow: owner clicks "Invite Coach" → `POST /api/client/coach-invite` (creates row + token + SES email) → coach clicks `GET /coach-accept?token=…` → `POST /api/coaches/accept-token` (validates, sets ACCEPTED, clears token, redirects to portal).

---

## 4. The coach cap (Phase 2 — LIVE 2026-04-19)

`billing_service.coach_accept_gate(email) -> (allowed, reason)`:

- First accepted+active link: **free**.
- 2nd+ link: requires an ACTIVE non-free coach subscription (any provider — `coach_has_pro_subscription()` accepts PayPal or legacy Wix, just not the free Coach Access plan).
- The gate fires at **accept time**, not invite time. Existing `ACCEPTED + active` links are **grandfathered** — coaches already at 2+ players keep their stable.
- **Fails open on DB error** — never blocks an invite due to infrastructure noise. Channel acquisition matters more than strict cap enforcement during outages.

Live code paths:
- Public accept (token): `coach_invite/accept_page.py::accept_by_token` → HTTP 402 `{error, message, upgrade_url, current_links, free_limit}` when capped.
- Ops accept: `coaches_api.py::api_accept` — same gate, same 402.
- Accept UI: `coach_accept.html` intercepts 402 and renders a `stateUpgrade` card with the Coach Pro CTA. Invite stays pending — coach can pay then retry the same link.
- Entitlements surface: `billing.entitlements` carries `coach_linked_players` (int) and `can_link_additional_player` (bool); coaches compute as `coach_linked_players < 1 OR is_coach_pro`.

---

## 5. What coaches cannot do

- **Upload matches** — `role <> 'coach'` is part of `can_upload`.
- **Consume credits** — no upload path means no consumption.
- **Invite further coaches** — no UI, no endpoint.

---

## 6. What Coach Pro will add (when built)

Thin additions on top of existing views — enough to justify the price: multi-player comparison dashboard, priority AI Coach queue, PDF "session brief" export for lesson prep, cross-session trend view across the coach's whole stable.

---

## Cross-references

- Tier numerics + access matrix → [`pricing-and-packages.md`](pricing-and-packages.md)
- Entitlement gates + block reasons → [`README.md`](README.md) §5 + [`billing-implementation.md`](billing-implementation.md)
- Implementation file map → `coach_invite/README.md`
- Coach lifecycle email flows → [`marketing-and-seo.md`](marketing-and-seo.md) (Klaviyo coach flows)
