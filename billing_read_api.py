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
        row = conn.execute(text("""
            SELECT
              a.email,
              COUNT(DISTINCT u.id) AS matches_used,
              COALESCE(SUM(u.billable_minutes),0) AS minutes_used
            FROM billing.account a
            LEFT JOIN billing.usage_video u ON u.account_id = a.id
            WHERE a.email = :email
            GROUP BY a.email
        """), {"email": email}).mappings().first()

    return jsonify({"ok": True, "data": dict(row) if row else None})
