# entitlements_api.py — Server-side upload gate: computes and caches entitlement state.
#
# Provides a single OPS_KEY-authenticated endpoint used by upload_app.py to decide
# whether a given email is allowed to submit a new video for analysis.
#
# Endpoint:
#   GET /api/entitlements/summary?email=<email>
#     — Upserts billing.entitlements (computed from account + subscription + grants + consumption)
#     — Returns can_upload, block_reason, can_view_dashboards, dashboard_block_reason,
#       matches_granted, matches_consumed, matches_remaining, role, subscription_status
#
# Auth: OPS_KEY via X-Ops-Key header or Authorization: Bearer <key>
#
# Business rules:
#   - can_upload requires: account active + role != 'coach' + subscription ACTIVE + credits > 0
#   - block_reason values: ACCOUNT_INACTIVE | COACH_VIEW_ONLY | SUBSCRIPTION_INACTIVE | NO_CREDITS
#   - can_view_dashboards requires: account active + subscription ACTIVE (coaches can view)
#   - Entitlement state is written to billing.entitlements on every call (upsert, not cached)
#   - Unknown email returns 404 (account must be registered before upload is possible)

import os

from flask import Blueprint, jsonify, request
from sqlalchemy import text
from db_init import engine

entitlements_bp = Blueprint("entitlements", __name__)

OPS_KEY = os.environ.get("OPS_KEY", "").strip()


def _guard() -> bool:
    hk = request.headers.get("X-OPS-Key") or request.headers.get("X-Ops-Key")
    auth = request.headers.get("Authorization", "")
    if auth and auth.lower().startswith("bearer "):
        hk = auth.split(" ", 1)[1].strip()
    import hmac
    return bool(OPS_KEY) and hmac.compare_digest((hk or "").strip(), OPS_KEY)

UPSERT_SQL = text("""
WITH a AS (
  SELECT id AS account_id, email, active AS account_active
  FROM billing.account
  WHERE email = :email
),
m AS (
  SELECT account_id, role
  FROM billing.member
  WHERE account_id IN (SELECT account_id FROM a)
    AND is_primary = true
  LIMIT 1
),
s AS (
  SELECT account_id, status AS subscription_status, current_period_end
  FROM billing.subscription_state
  WHERE account_id IN (SELECT account_id FROM a)
  ORDER BY updated_at DESC NULLS LAST
  LIMIT 1
),
g AS (
  SELECT
    account_id,
    COALESCE(SUM(matches_granted), 0)::int AS matches_granted
  FROM billing.entitlement_grant
  WHERE account_id IN (SELECT account_id FROM a)
    AND is_active = true
    AND (valid_from IS NULL OR valid_from <= now())
    AND (valid_to   IS NULL OR now() < valid_to)
  GROUP BY account_id
),
c AS (
  SELECT
    account_id,
    COALESCE(SUM(consumed_matches), 0)::int AS matches_consumed
  FROM billing.entitlement_consumption
  WHERE account_id IN (SELECT account_id FROM a)
  GROUP BY account_id
),
calc AS (
  SELECT
    a.account_id,
    a.email,
    COALESCE(m.role, 'player_parent') AS role,
    a.account_active,

    s.subscription_status,
    s.current_period_end,

    (s.subscription_status = 'ACTIVE') AS paid_active,

    COALESCE(g.matches_granted, 0) AS matches_granted,
    COALESCE(c.matches_consumed, 0) AS matches_consumed,
    GREATEST(
      COALESCE(g.matches_granted, 0) - COALESCE(c.matches_consumed, 0),
      0
    ) AS matches_remaining
  FROM a
  LEFT JOIN m ON m.account_id = a.account_id
  LEFT JOIN s ON s.account_id = a.account_id
  LEFT JOIN g ON g.account_id = a.account_id
  LEFT JOIN c ON c.account_id = a.account_id
)
INSERT INTO billing.entitlements (
  account_id, email, role, account_active,
  subscription_status, current_period_end, paid_active,
  matches_granted, matches_consumed, matches_remaining,
  can_view_dashboards, dashboard_block_reason,
  can_upload, block_reason, updated_at
)
SELECT
  account_id, email, role, account_active,
  subscription_status, current_period_end, paid_active,
  matches_granted, matches_consumed, matches_remaining,

  (account_active AND paid_active) AS can_view_dashboards,

  CASE
    WHEN NOT account_active THEN 'ACCOUNT_INACTIVE'
    WHEN NOT paid_active THEN 'SUBSCRIPTION_INACTIVE'
    ELSE NULL
  END AS dashboard_block_reason,

  (account_active AND role <> 'coach' AND paid_active AND matches_remaining > 0) AS can_upload,

  CASE
    WHEN NOT account_active THEN 'ACCOUNT_INACTIVE'
    WHEN role = 'coach' THEN 'COACH_VIEW_ONLY'
    WHEN NOT paid_active THEN 'SUBSCRIPTION_INACTIVE'
    WHEN matches_remaining <= 0 THEN 'NO_CREDITS'
    ELSE NULL
  END AS block_reason,

  now()
FROM calc
ON CONFLICT (account_id) DO UPDATE SET
  email                  = EXCLUDED.email,
  role                   = EXCLUDED.role,
  account_active         = EXCLUDED.account_active,
  subscription_status    = EXCLUDED.subscription_status,
  current_period_end     = EXCLUDED.current_period_end,
  paid_active            = EXCLUDED.paid_active,
  matches_granted        = EXCLUDED.matches_granted,
  matches_consumed       = EXCLUDED.matches_consumed,
  matches_remaining      = EXCLUDED.matches_remaining,
  can_view_dashboards    = EXCLUDED.can_view_dashboards,
  dashboard_block_reason = EXCLUDED.dashboard_block_reason,
  can_upload             = EXCLUDED.can_upload,
  block_reason           = EXCLUDED.block_reason,
  updated_at             = now();
""")

READ_SQL = text("""
SELECT
  account_id,
  email,
  role,
  account_active,
  subscription_status,
  current_period_end,
  paid_active,
  matches_granted,
  matches_consumed,
  matches_remaining,
  can_upload,
  block_reason,
  can_view_dashboards,
  dashboard_block_reason,
  updated_at
FROM billing.entitlements
WHERE email = :email
ORDER BY updated_at DESC
LIMIT 1
""")

@entitlements_bp.get("/api/entitlements/summary")
def entitlements_summary():
    if not _guard():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    with engine.begin() as conn:
        conn.execute(UPSERT_SQL, {"email": email})
        row = conn.execute(READ_SQL, {"email": email}).mappings().first()

    if not row:
        return jsonify({"ok": False, "error": "account_not_found"}), 404

    return jsonify({"ok": True, "data": dict(row)})
