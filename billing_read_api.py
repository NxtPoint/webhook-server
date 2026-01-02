#==================================
# billing_read_api.py  (FINAL BASELINE)
#==================================

from __future__ import annotations

from flask import Blueprint, jsonify, request
from sqlalchemy import text

from db_init import engine

billing_read_bp = Blueprint("billing_read", __name__)


@billing_read_bp.get("/api/billing/summary")
def billing_summary():
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT
                  a.email,
                  m.role,
                  COALESCE(v.matches_granted, 0)   AS matches_granted,
                  COALESCE(v.matches_consumed, 0)  AS matches_consumed,
                  COALESCE(v.matches_remaining, 0) AS matches_remaining,
                  v.last_processed_at
                FROM billing.account a
                LEFT JOIN billing.member m
                  ON m.account_id = a.id AND m.is_primary = true
                LEFT JOIN billing.vw_customer_usage v
                  ON v.account_id = a.id
                WHERE a.email = :email
                """
            ),
            {"email": email},
        ).mappings().first()

    # IMPORTANT: keep response shape stable for Wix proxy.
    # - If account doesn't exist: ok:true + data:null (not an error)
    return jsonify({"ok": True, "data": dict(row) if row else None})
