# tennis_coach/db.py — Cache table schema, read/write helpers, rate-limit counts.
#
# Schema: tennis_coach.coach_cache
#   - Unique index on (task_id, email, prompt_key) — one cached response per question.
#   - Cache is indefinite: match data doesn't change after ingest.
#   - prompt_key for freeform: 'freeform:<sha256_first_12_chars>'
#   - prompt_key for cards:    'cards'
#   - prompt_key for named:    'serve_analysis' | 'weakness' | 'tactics'

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from db_init import engine

log = logging.getLogger(__name__)

_SCHEMA_SQL = "CREATE SCHEMA IF NOT EXISTS tennis_coach"

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tennis_coach.coach_cache (
    id            serial PRIMARY KEY,
    task_id       uuid    NOT NULL,
    email         text    NOT NULL,
    prompt_key    text    NOT NULL,
    response      text    NOT NULL,
    data_snapshot jsonb,
    tokens_used   integer,
    created_at    timestamptz DEFAULT now()
)
"""

_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS coach_cache_uq
    ON tennis_coach.coach_cache (task_id, email, prompt_key)
"""


def init_coach_cache():
    """
    Idempotent creation of tennis_coach schema + coach_cache table + unique index.
    Safe to call on every boot.
    """
    try:
        with engine.begin() as conn:
            conn.execute(text(_SCHEMA_SQL))
            conn.execute(text(_TABLE_SQL))
            conn.execute(text(_INDEX_SQL))
        log.info("[coach_db] tennis_coach.coach_cache ready")
    except Exception:
        log.exception("[coach_db] failed to init coach_cache — coach feature may be unavailable")


def freeform_key(question: str) -> str:
    """Derive a stable cache key for a freeform question."""
    digest = hashlib.sha256(question.strip().lower().encode()).hexdigest()
    return f"freeform:{digest[:12]}"


def cache_get(task_id: str, email: str, prompt_key: str) -> Optional[dict]:
    """
    Return cached entry as dict or None if not found.
    Dict shape: { response, data_snapshot, tokens_used, created_at }
    """
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT response, data_snapshot, tokens_used, created_at
                    FROM tennis_coach.coach_cache
                    WHERE task_id = :tid AND email = :email AND prompt_key = :pk
                    LIMIT 1
                """),
                {"tid": task_id, "email": email, "pk": prompt_key},
            ).mappings().fetchone()
        if row is None:
            return None
        return {
            "response":      row["response"],
            "data_snapshot": row["data_snapshot"],
            "tokens_used":   row["tokens_used"],
            "created_at":    row["created_at"].isoformat() if row["created_at"] else None,
        }
    except Exception:
        log.exception("[coach_db] cache_get failed task_id=%s pk=%s", task_id, prompt_key)
        return None


def cache_put(
    task_id: str,
    email: str,
    prompt_key: str,
    response: str,
    data_snapshot: Optional[dict],
    tokens_used: Optional[int],
) -> bool:
    """
    Upsert a cache entry. Returns True on success.
    Uses INSERT … ON CONFLICT DO UPDATE so re-generation (force=true) works.
    """
    try:
        snapshot_json = json.dumps(data_snapshot) if data_snapshot is not None else None
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO tennis_coach.coach_cache
                        (task_id, email, prompt_key, response, data_snapshot, tokens_used)
                    VALUES (:tid, :email, :pk, :response, :snapshot::jsonb, :tokens)
                    ON CONFLICT (task_id, email, prompt_key)
                    DO UPDATE SET
                        response      = EXCLUDED.response,
                        data_snapshot = EXCLUDED.data_snapshot,
                        tokens_used   = EXCLUDED.tokens_used,
                        created_at    = now()
                """),
                {
                    "tid":      task_id,
                    "email":    email,
                    "pk":       prompt_key,
                    "response": response,
                    "snapshot": snapshot_json,
                    "tokens":   tokens_used,
                },
            )
        return True
    except Exception:
        log.exception("[coach_db] cache_put failed task_id=%s pk=%s", task_id, prompt_key)
        return False


def count_daily_calls(email: str, task_id: Optional[str] = None) -> int:
    """
    Count non-card LLM calls made today by this email (optionally filtered to one task_id).
    Cards ('cards') are excluded from the count — they are free and pre-generated.
    """
    try:
        params: dict = {"email": email}
        task_filter = ""
        if task_id:
            task_filter = "AND task_id = :tid"
            params["tid"] = task_id

        with engine.connect() as conn:
            row = conn.execute(
                text(f"""
                    SELECT COUNT(*) AS cnt
                    FROM tennis_coach.coach_cache
                    WHERE email = :email
                      AND prompt_key <> 'cards'
                      AND created_at >= CURRENT_DATE
                      {task_filter}
                """),
                params,
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        log.exception("[coach_db] count_daily_calls failed email=%s", email)
        return 0
