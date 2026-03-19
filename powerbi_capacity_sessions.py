# ==================================================================================================
# powerbi_capacity_sessions.py
# ==================================================================================================
# PURPOSE
# -------
# Server-owned lease/session control for Azure Power BI Embedded capacity.
#
# WHY
# ---
# Front-end/browser shutdown is not reliable enough for cost control.
# This module stores active report-viewing leases in Postgres so the backend
# can decide whether Azure capacity should remain running.
#
# DESIGN
# ------
# - One row per browser/viewing session
# - Session remains active while heartbeats continue
# - Session expires automatically after lease timeout
# - Capacity may suspend ONLY when active session count = 0
# - Advisory lock used to prevent concurrent suspend/resume races across workers
#
# DEPENDENCIES
# ------------
# - db_init.py must expose `engine`
#
# ==================================================================================================

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Dict, Optional

from sqlalchemy import text as sql_text

from db_init import engine


# --------------------------------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------------------------------
OPS_SCHEMA = "ops"
OPS_TABLE = "powerbi_capacity_session"

# advisory lock key namespace (stable integer)
ADVISORY_LOCK_NAME = "powerbi_capacity_control_v1"


# --------------------------------------------------------------------------------------------------
# INIT
# --------------------------------------------------------------------------------------------------
def powerbi_capacity_sessions_init() -> None:
    """
    Idempotent schema/table/index creation.
    Safe to call on every boot.
    """
    with engine.begin() as conn:
        conn.execute(sql_text(f"CREATE SCHEMA IF NOT EXISTS {OPS_SCHEMA};"))

        conn.execute(sql_text(f"""
            CREATE TABLE IF NOT EXISTS {OPS_SCHEMA}.{OPS_TABLE} (
                session_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                report_id TEXT NULL,
                workspace_id TEXT NULL,
                dataset_id TEXT NULL,
                status TEXT NOT NULL DEFAULT 'active',   -- active / ended / expired
                started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                expires_at TIMESTAMPTZ NOT NULL,
                ended_at TIMESTAMPTZ NULL,
                end_reason TEXT NULL,
                created_by TEXT NULL DEFAULT 'embed',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """))

        conn.execute(sql_text(f"""
            CREATE INDEX IF NOT EXISTS ix_{OPS_TABLE}_status
            ON {OPS_SCHEMA}.{OPS_TABLE}(status);
        """))

        conn.execute(sql_text(f"""
            CREATE INDEX IF NOT EXISTS ix_{OPS_TABLE}_expires_at
            ON {OPS_SCHEMA}.{OPS_TABLE}(expires_at);
        """))

        conn.execute(sql_text(f"""
            CREATE INDEX IF NOT EXISTS ix_{OPS_TABLE}_username
            ON {OPS_SCHEMA}.{OPS_TABLE}(username);
        """))

        conn.execute(sql_text(f"""
            CREATE INDEX IF NOT EXISTS ix_{OPS_TABLE}_active_lookup
            ON {OPS_SCHEMA}.{OPS_TABLE}(status, expires_at);
        """))


# --------------------------------------------------------------------------------------------------
# HELPERS
# --------------------------------------------------------------------------------------------------
def _clean_username(username: str) -> str:
    u = str(username or "").strip().lower()
    if not u or "@" not in u:
        raise RuntimeError("missing_or_invalid_username")
    return u


def _clean_optional(v: Optional[str]) -> Optional[str]:
    x = str(v or "").strip()
    return x or None


def _new_session_id() -> str:
    return str(uuid.uuid4())


def _lock_key() -> int:
    """
    Stable 64-bit advisory lock key derived from name.
    """
    h = hashlib.sha256(ADVISORY_LOCK_NAME.encode("utf-8")).digest()
    return int.from_bytes(h[:8], byteorder="big", signed=False) & 0x7FFFFFFFFFFFFFFF


# --------------------------------------------------------------------------------------------------
# LOCKING
# --------------------------------------------------------------------------------------------------
def acquire_capacity_lock(conn) -> None:
    conn.execute(sql_text("SELECT pg_advisory_lock(:k)"), {"k": _lock_key()})


def release_capacity_lock(conn) -> None:
    conn.execute(sql_text("SELECT pg_advisory_unlock(:k)"), {"k": _lock_key()})


# --------------------------------------------------------------------------------------------------
# SESSION WRITE OPS
# --------------------------------------------------------------------------------------------------
def start_session(
    username: str,
    lease_seconds: int = 180,
    report_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    dataset_id: Optional[str] = None,
    created_by: str = "embed",
) -> Dict[str, Any]:
    """
    Create a new active lease.
    """
    username = _clean_username(username)
    report_id = _clean_optional(report_id)
    workspace_id = _clean_optional(workspace_id)
    dataset_id = _clean_optional(dataset_id)
    created_by = _clean_optional(created_by) or "embed"
    session_id = _new_session_id()

    with engine.begin() as conn:
        conn.execute(sql_text(f"""
            INSERT INTO {OPS_SCHEMA}.{OPS_TABLE} (
                session_id,
                username,
                report_id,
                workspace_id,
                dataset_id,
                status,
                started_at,
                last_seen_at,
                expires_at,
                created_by,
                updated_at
            )
            VALUES (
                :session_id,
                :username,
                :report_id,
                :workspace_id,
                :dataset_id,
                'active',
                now(),
                now(),
                now() + (:lease_seconds || ' seconds')::interval,
                :created_by,
                now()
            )
        """), {
            "session_id": session_id,
            "username": username,
            "report_id": report_id,
            "workspace_id": workspace_id,
            "dataset_id": dataset_id,
            "lease_seconds": int(lease_seconds),
            "created_by": created_by,
        })

        row = conn.execute(sql_text(f"""
            SELECT
                session_id,
                username,
                report_id,
                workspace_id,
                dataset_id,
                status,
                started_at,
                last_seen_at,
                expires_at
            FROM {OPS_SCHEMA}.{OPS_TABLE}
            WHERE session_id = :session_id
        """), {"session_id": session_id}).mappings().first()

    return dict(row) if row else {"session_id": session_id, "username": username, "status": "active"}


def heartbeat_session(
    session_id: str,
    username: str,
    lease_seconds: int = 180,
) -> Dict[str, Any]:
    """
    Refresh an active lease.
    No-op if session already ended/expired/missing.
    """
    session_id = str(session_id or "").strip()
    username = _clean_username(username)

    if not session_id:
        raise RuntimeError("missing_session_id")

    with engine.begin() as conn:
        conn.execute(sql_text(f"""
            UPDATE {OPS_SCHEMA}.{OPS_TABLE}
            SET
                last_seen_at = now(),
                expires_at = now() + (:lease_seconds || ' seconds')::interval,
                updated_at = now()
            WHERE session_id = :session_id
              AND username = :username
              AND status = 'active'
        """), {
            "session_id": session_id,
            "username": username,
            "lease_seconds": int(lease_seconds),
        })

        row = conn.execute(sql_text(f"""
            SELECT
                session_id,
                username,
                status,
                started_at,
                last_seen_at,
                expires_at,
                ended_at,
                end_reason
            FROM {OPS_SCHEMA}.{OPS_TABLE}
            WHERE session_id = :session_id
        """), {"session_id": session_id}).mappings().first()

    if not row:
        return {
            "ok": False,
            "found": False,
            "session_id": session_id,
            "status": "missing",
        }

    return {"ok": True, "found": True, **dict(row)}


def end_session(
    session_id: str,
    username: str,
    reason: str = "client_end",
) -> Dict[str, Any]:
    """
    Mark a session ended.
    """
    session_id = str(session_id or "").strip()
    username = _clean_username(username)
    reason = str(reason or "").strip() or "client_end"

    if not session_id:
        raise RuntimeError("missing_session_id")

    with engine.begin() as conn:
        conn.execute(sql_text(f"""
            UPDATE {OPS_SCHEMA}.{OPS_TABLE}
            SET
                status = 'ended',
                ended_at = COALESCE(ended_at, now()),
                end_reason = COALESCE(end_reason, :reason),
                updated_at = now()
            WHERE session_id = :session_id
              AND username = :username
              AND status = 'active'
        """), {
            "session_id": session_id,
            "username": username,
            "reason": reason,
        })

        row = conn.execute(sql_text(f"""
            SELECT
                session_id,
                username,
                status,
                started_at,
                last_seen_at,
                expires_at,
                ended_at,
                end_reason
            FROM {OPS_SCHEMA}.{OPS_TABLE}
            WHERE session_id = :session_id
        """), {"session_id": session_id}).mappings().first()

    if not row:
        return {
            "ok": False,
            "found": False,
            "session_id": session_id,
            "status": "missing",
        }

    return {"ok": True, "found": True, **dict(row)}


def expire_stale_sessions() -> int:
    """
    Mark any stale active sessions as expired.
    Returns count updated.
    """
    with engine.begin() as conn:
        res = conn.execute(sql_text(f"""
            UPDATE {OPS_SCHEMA}.{OPS_TABLE}
            SET
                status = 'expired',
                ended_at = COALESCE(ended_at, now()),
                end_reason = COALESCE(end_reason, 'lease_expired'),
                updated_at = now()
            WHERE status = 'active'
              AND expires_at < now()
        """))
        return int(res.rowcount or 0)


# --------------------------------------------------------------------------------------------------
# READ OPS
# --------------------------------------------------------------------------------------------------
def active_session_count() -> int:
    with engine.begin() as conn:
        n = conn.execute(sql_text(f"""
            SELECT COUNT(*)::INT
            FROM {OPS_SCHEMA}.{OPS_TABLE}
            WHERE status = 'active'
              AND expires_at >= now()
        """)).scalar_one()
    return int(n or 0)


def has_active_sessions() -> bool:
    return active_session_count() > 0


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    session_id = str(session_id or "").strip()
    if not session_id:
        return None

    with engine.begin() as conn:
        row = conn.execute(sql_text(f"""
            SELECT
                session_id,
                username,
                report_id,
                workspace_id,
                dataset_id,
                status,
                started_at,
                last_seen_at,
                expires_at,
                ended_at,
                end_reason,
                created_by,
                updated_at
            FROM {OPS_SCHEMA}.{OPS_TABLE}
            WHERE session_id = :session_id
        """), {"session_id": session_id}).mappings().first()

    return dict(row) if row else None


def sweep_sessions() -> Dict[str, Any]:
    """
    Sweep stale sessions and return current counts.
    Note: actual suspend decision should be made by caller while holding advisory lock.
    """
    expired = expire_stale_sessions()

    with engine.begin() as conn:
        active = conn.execute(sql_text(f"""
            SELECT COUNT(*)::INT
            FROM {OPS_SCHEMA}.{OPS_TABLE}
            WHERE status = 'active'
              AND expires_at >= now()
        """)).scalar_one()

        total_active_users = conn.execute(sql_text(f"""
            SELECT COUNT(DISTINCT username)::INT
            FROM {OPS_SCHEMA}.{OPS_TABLE}
            WHERE status = 'active'
              AND expires_at >= now()
        """)).scalar_one()

    return {
        "expired_count": int(expired or 0),
        "active_session_count": int(active or 0),
        "active_user_count": int(total_active_users or 0),
    }