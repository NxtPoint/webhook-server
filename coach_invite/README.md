# coach_invite

> Token-based coach invitation flow. The owner invites a coach by email; the coach accepts via a single-use token URL. The token IS the auth — no API keys involved on the accept path.

## What this owns

- The `billing.coaches_permission` schema column for `invite_token` (idempotent boot-time alter)
- Token lifecycle: generate → email → validate → accept → clear
- The Flask blueprint serving `GET /coach-accept` (HTML) + `POST /api/coaches/accept-token` (public accept endpoint)
- Two SES email senders: coach invite, and "your match analysis is ready"

## What this is NOT

- **Not the invite trigger.** Owners click "Invite Coach" in `frontend/locker_room.html` → that hits `client_api.coach_invite` (not in this module). `client_api` creates the permission row + token, then calls `send_invite_email()` from this module.
- **Not the revoke endpoint.** That's also in `client_api` (`POST /api/client/coach-revoke`).
- **Not the ops accept endpoint.** Server-to-server accept is `coaches_api.api_accept` with OPS_KEY auth. This module's accept endpoint is for the human-clicks-email path.
- **Not the coach cap gate.** That's `billing_service.coach_accept_gate()`. This module *calls* it before accepting; it doesn't define the rule.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | On import, runs `ensure_invite_token_column()` (idempotent schema setup). Exports `accept_bp` blueprint and `send_invite_email`. |
| `db.py` | Token-column setup, token generate / set / clear, `get_permission_by_token()` lookup with owner details. |
| `accept_page.py` | `accept_bp` blueprint — serves `coach_accept.html` and handles `POST /api/coaches/accept-token`. |
| `email_sender.py` | `send_invite_email()` — branded HTML + plain-text email via AWS SES with the accept URL. |
| `video_complete_email.py` | `send_completion_email()` — "your match analysis is ready" SES email, fired from ingest worker step 7 + task-status auto-fire. (Not strictly coach-invite, but lives here because both share the same SES boilerplate.) |

## Entry points

| Function / endpoint | Purpose | Caller |
|---|---|---|
| `accept_bp` | Blueprint with the public accept routes | Registered in `upload_app.py` |
| `GET /coach-accept` | Serves `frontend/coach_accept.html` | Coach clicks email link |
| `POST /api/coaches/accept-token` | Public — validates token, runs cap gate, marks ACCEPTED, clears token | `coach_accept.html` JS |
| `send_invite_email(coach_email, coach_name, owner_name, accept_url)` | Send branded invite via SES | `client_api.coach_invite` |
| `send_completion_email(task_id, customer_email, customer_name, ...)` | Send "match ready" email | `ingest_worker_app.py` step 7, `upload_app.py` task-status auto-fire |
| `db.generate_token()` / `set_token()` / `clear_token()` / `get_permission_by_token()` | Token lifecycle helpers | `client_api`, `accept_page` |

## Data model

`billing.coaches_permission` (table not owned by this module — declared in `coaches_api.py` schema setup; this module only adds the `invite_token` column).

| Column | Notes |
|---|---|
| `id` | PK |
| `owner_account_id` | FK to `billing.account.id` (the customer) |
| `coach_account_id` | FK to `billing.account.id` (NULLABLE — coach may not have an account yet at invite time) |
| `coach_email` | Email of invited coach (always lowercase, normalised) |
| `status` | `'INVITED'` or `'ACCEPTED'` |
| `active` | Boolean — false when revoked |
| `invite_token` | TEXT NULLABLE. Single-use. NULL after accept or revoke. |
| `created_at`, `updated_at` | Timestamps |

Plus a unique partial index `WHERE invite_token IS NOT NULL` to prevent token collisions (`db.py:46-49`).

## Flow

### Invite (sent from `client_api`, this module sends the email)

```
Owner clicks "Invite Coach" in Locker Room
        │
        ▼
POST /api/client/coach-invite
        │
        ├─ UPDATE/INSERT billing.coaches_permission row (status=INVITED, active=true)
        ├─ token = coach_invite.db.generate_token()  →  secrets.token_urlsafe(32)
        ├─ coach_invite.db.set_token(permission_id, token)
        └─ coach_invite.email_sender.send_invite_email(...)
                                  │
                                  ▼
                           AWS SES → coach inbox
                           Email contains:
                              {COACH_ACCEPT_BASE_URL}/coach-accept?token=<token>
```

### Accept (handled here)

```
Coach clicks accept link
        │
        ▼
GET /coach-accept                       (serves coach_accept.html)
        │
        ▼
JS reads token from URL, calls
POST /api/coaches/accept-token  body={"token": "..."}
        │
        ├─ get_permission_by_token(token) — must be status=INVITED, active=true
        ├─ billing_service.coach_accept_gate(coach_email)  — cap check (Phase 2)
        │     │
        │     ├─ allowed=False → 402 with COACH_UPGRADE_REQUIRED + upgrade_url
        │     └─ allowed=True  → continue
        │
        ├─ Look up coach's billing.account.id (NULLABLE — might be invited before signup)
        ├─ UPDATE billing.coaches_permission
        │      SET status='ACCEPTED', coach_account_id=..., invite_token=NULL, active=true
        │
        └─ 200 { ok, status:'ACCEPTED', owner_name, coach_email, coach_linked: bool }
```

## Gotchas

- **The token IS the auth.** The accept endpoint takes no API key. Possession of a valid `invite_token` is sufficient proof of identity. Token entropy is 32 bytes from `secrets.token_urlsafe`.
- **Single-use.** Token is NULL'd immediately on accept (or on revoke). Replaying the same link fails with 400 `invalid_or_expired_token`.
- **Re-invite reuses the row.** If a coach was revoked, re-inviting them UPDATEs the same `billing.coaches_permission` row (new token, status reset to `INVITED`). It does not INSERT a duplicate. Permission rows are never DELETEd.
- **Coach cap fires at accept time, not invite time.** Existing accepted links are grandfathered. The owner can invite freely; the gate stops the *coach* from accepting #2 unless they have Coach Pro. See `billing_service.py::coach_accept_gate`.

> **Business rules (invite protocol, cap, idempotency) are canonical in [`../docs/business/coach-model.md`](../docs/business/coach-model.md)** — don't duplicate them here; this README is the code map only.
- **402 keeps the invite pending.** When the cap blocks an accept, the permission row stays `INVITED` with the token still set. Coach can pay for Coach Pro and click the same email link again.
- **`coach_account_id` is set lazily.** If the coach doesn't have an account yet at accept time (rare but possible), it stays NULL. They get linked when their account is later created with the matching email.
- **Email region is `eu-north-1`.** Set via `AWS_REGION` env var. SES domain `ten-fifty5.com` is verified there.
- **Sandbox warning.** SES sandbox mode only sends to verified recipients. Production needs a sandbox-removal request to AWS before invites can land in arbitrary inboxes.

## Required environment variables

| Var | Default | Purpose |
|---|---|---|
| `SES_FROM_EMAIL` | `noreply@ten-fifty5.com` | From address — domain must be SES-verified |
| `AWS_REGION` | `us-east-1` | Used both for SES and (separately) for `eu-north-1` SES instance |
| `COACH_ACCEPT_BASE_URL` | `https://api.nextpointtennis.com` | Base URL for the accept link in the invite email |
| `LOCKER_ROOM_BASE_URL` | `https://www.ten-fifty5.com/portal` | Base URL in the "match ready" email's CTA |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | (boto3 default chain) | IAM user `nextpoint-uploader` needs `ses:SendEmail` |

## See also

- [`../docs/business/coach-model.md`](../docs/business/coach-model.md) — full coach invite + cap rules and idempotency contract (canonical)
- [`../docs/business/pricing-and-packages.md`](../docs/business/pricing-and-packages.md) §6 — coach pricing tiers
- `coaches_api.py` — server-to-server coach permission management (OPS_KEY auth)
- `client_api.py` `coach_invite` / `coach_revoke` — owner-facing endpoints that orchestrate this module
- `frontend/coach_accept.html` — the public accept page
