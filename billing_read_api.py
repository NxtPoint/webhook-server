#==================================
# billing_read_api.py
#==================================


from flask import Blueprint, jsonify, request
from sqlalchemy import text
from db_init import engine

billing_read_bp = Blueprint("billing_read", __name__)

@billing_read_bp.get("/api/billing/summary")
def billing_summary():
    email = request.args.get("email")
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    with engine.begin() as conn:
                row = conn.execute(
            text(
                """
                SELECT
                  a.email,
                  COALESCE(v.matches_granted, 0)    AS matches_granted,
                  COALESCE(v.matches_consumed, 0)   AS matches_consumed,
                  COALESCE(v.matches_remaining, 0)  AS matches_remaining,
                  v.last_processed_at
                FROM billing.account a
                LEFT JOIN billing.vw_customer_usage v
                  ON v.account_id = a.id
                WHERE a.email = :email
                """
            ),
            {"email": email.strip().lower()},
        ).mappings().first()

    return jsonify({"ok": True, "data": dict(row) if row else None})
