#==================================
# entitlements_api.py  (NEW CANONICAL GATE)
#==================================

from flask import Blueprint, jsonify, request
from sqlalchemy import text
from db_init import engine

entitlements_bp = Blueprint("entitlements", __name__)

UPSERT_SQL = text("""  -- paste the SQL upsert from section 3A here
-- (keep it exactly as-is, with :email parameter)
""")

READ_SQL = text("""
  SELECT
    account_id, email, role,
    account_is_active, subscription_status, paid_through_ts, is_paid_active,
    matches_granted, matches_consumed, matches_remaining, last_processed_at,
    can_upload, block_reason, updated_at
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
        # recompute (fail-closed if missing account)
        conn.execute(UPSERT_SQL, {"email": email})
        row = conn.execute(READ_SQL, {"email": email}).mappings().first()

    if not row:
        return jsonify({"ok": False, "error": "account_not_found"}), 404

    return jsonify({"ok": True, "data": dict(row)})
