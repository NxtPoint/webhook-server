# coach_invite/db.py
# ============================================================
# Database helpers for the coach invite token lifecycle.
#
# Schema: creates billing.coaches_permission table (columns: id,
# owner_account_id, coach_account_id, coach_email, status, active,
# invite_token, created_at, updated_at) and ensures the invite_token
# column exists (idempotent ALTER TABLE ... ADD COLUMN IF NOT EXISTS).
#
# Token operations:
#   generate_invite_token()  — secrets.token_urlsafe(32), single-use
#   set_invite_token(...)    — write token + reset status to INVITED
#   clear_invite_token(...)  — NULL out token on accept or revoke
#   lookup_by_token(...)     — find an active INVITED row by token
#
# Business rules:
#   - Tokens are single-use: cleared immediately on accept or revoke.
#   - Unique partial index on invite_token WHERE invite_token IS NOT NULL
#     prevents token collisions.
#   - Re-inviting a revoked coach reuses the existing row (new token,
#     status reset to INVITED).
# ============================================================

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from db_init import engine

SCHEMA = "billing"
TABLE = "coaches_permission"


def ensure_invite_token_column():
    """Idempotent: add invite_token column + unique partial index."""
    with engine.begin() as conn:
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.{TABLE} "
            "ADD COLUMN IF NOT EXISTS invite_token TEXT"
        ))
        conn.execute(text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS ix_coaches_permission_token "
            f"ON {SCHEMA}.{TABLE}(invite_token) "
            "WHERE invite_token IS NOT NULL"
        ))


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def set_token(permission_id: int, token: str):
    """Store a fresh invite token on the permission row."""
    with Session(engine) as session:
        session.execute(
            text(f"""
                UPDATE {SCHEMA}.{TABLE}
                SET invite_token = :token, updated_at = :now
                WHERE id = :id
            """),
            {"id": permission_id, "token": token, "now": datetime.now(tz=timezone.utc)},
        )
        session.commit()


def clear_token(permission_id: int):
    """Clear the invite token (on accept or revoke)."""
    with Session(engine) as session:
        session.execute(
            text(f"""
                UPDATE {SCHEMA}.{TABLE}
                SET invite_token = NULL, updated_at = :now
                WHERE id = :id
            """),
            {"id": permission_id, "now": datetime.now(tz=timezone.utc)},
        )
        session.commit()


def get_permission_by_token(token: str) -> dict | None:
    """Look up an active INVITED permission by token, with owner details."""
    with engine.connect() as conn:
        row = conn.execute(
            text(f"""
                SELECT cp.id, cp.owner_account_id, cp.coach_email, cp.status, cp.active,
                       a.email AS owner_email,
                       m.full_name AS owner_first_name, m.surname AS owner_surname
                FROM {SCHEMA}.{TABLE} cp
                JOIN {SCHEMA}.account a ON a.id = cp.owner_account_id
                LEFT JOIN {SCHEMA}.member m
                    ON m.account_id = a.id AND m.is_primary = true AND m.active = true
                WHERE cp.invite_token = :token
                  AND cp.status = 'INVITED'
                  AND cp.active = true
                LIMIT 1
            """),
            {"token": token},
        ).mappings().first()

    if not row:
        return None

    owner_name = " ".join(
        filter(None, [row["owner_first_name"], row["owner_surname"]])
    ) or row["owner_email"]

    return {
        "id": int(row["id"]),
        "owner_account_id": int(row["owner_account_id"]),
        "coach_email": row["coach_email"],
        "owner_email": row["owner_email"],
        "owner_name": owner_name,
    }
