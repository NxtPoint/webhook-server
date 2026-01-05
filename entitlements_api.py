#==================================
# entitlements_api.py
# CANONICAL UPLOAD GATE
#==================================

from flask import Blueprint, jsonify, request
from sqlalchemy import text
from db_init import engine

entitlements_bp = Blueprint("entitlements", __name__)

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
  can_upload, block_reason, updated_at
)
SELECT
  account_id, email, role, account_active,
  subscription_status, current_period_end, paid_active,
  matches_granted, matches_consumed, matches_remaining,

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
  email               = EXCLUDED.email,
  role                = EXCLUDED.role,
  account_active      = EXCLUDED.account_active,
  subscription_status = EXCLUDED.subscription_status,
  current_period_end  = EXCLUDED.current_period_end,
  paid_active         = EXCLUDED.paid_active,
  matches_granted     = EXCLUDED.matches_granted,
  matches_consumed    = EXCLUDED.matches_consumed,
  matches_remaining   = EXCLUDED.matches_remaining,
  can_upload          = EXCLUDED.can_upload,
  block_reason        = EXCLUDED.block_reason,
  updated_at          = now();
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
  updated_at
FROM billing.entitlements
WHERE email = :email
LIMIT 1
""")

@entitlements_bp.get("/api/entitlements/summary")
def entitlements_summary():
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    with engine.begin() as conn:
        conn.execute(UPSERT_SQL, {"email": email})
        row = conn.execute(READ_SQL, {"email": email}).mappings().first()

    if not row:
        return jsonify({"ok": False, "error": "account_not_found"}), 404

    return jsonify({"ok": True, "data": dict(row)})
