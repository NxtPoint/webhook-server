#==================================
# entitlements_api.py
#==================================

from flask import Blueprint, jsonify, request
from sqlalchemy import text
from db_init import engine

entitlements_bp = Blueprint("entitlements", __name__)

UPSERT_SQL = text("""
-- paste the UPSERT SQL here, but replace the literal email with:
-- WHERE email = :email
""")

READ_SQL = text("""
  SELECT
    account_id, email, role, account_active,
    subscription_status, current_period_end, paid_active,
    matches_granted, matches_consumed, matches_remaining,
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
        conn.execute(UPSERT_SQL, {"email": email})
        row = conn.execute(READ_SQL, {"email": email}).mappings().first()

    if not row:
        return jsonify({"ok": False, "error": "account_not_found"}), 404

    return jsonify({"ok": True, "data": dict(row)})
