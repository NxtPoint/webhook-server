# support_bot/db.py — Schema init + read/write helpers for support_bot.* tables.
#
# Two tables:
#   support_bot.conversations  — every Q+A logged for review and rate-limit counting.
#   support_bot.faq_cache      — SHA-keyed dedup of (question + page_context).
#                                Invalidated when faq.md content hash changes.
#
# All idempotent: CREATE SCHEMA / TABLE / INDEX IF NOT EXISTS, safe to run
# on every boot.

from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

from sqlalchemy import text

from db_init import engine

log = logging.getLogger(__name__)

_SCHEMA_SQL = "CREATE SCHEMA IF NOT EXISTS support_bot"

_CONVERSATIONS_SQL = """
CREATE TABLE IF NOT EXISTS support_bot.conversations (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid        NOT NULL,
    turn_idx        integer     NOT NULL,
    email           text        NOT NULL,
    page_context    text,
    question        text        NOT NULL,
    answer          text        NOT NULL,
    confidence      text,
    needs_human     boolean     DEFAULT false,
    cited_sections  text[],
    feedback        jsonb,
    escalated_at    timestamptz,
    tokens_input    integer,
    tokens_output   integer,
    tokens_cached   integer,
    cost_cents      numeric(8,4),
    created_at      timestamptz DEFAULT now()
)
"""

_CONVERSATIONS_IDX_CONV_TURN = """
CREATE INDEX IF NOT EXISTS support_conversations_conv_turn_idx
    ON support_bot.conversations (conversation_id, turn_idx)
"""

_CONVERSATIONS_IDX_CREATED = """
CREATE INDEX IF NOT EXISTS support_conversations_created_idx
    ON support_bot.conversations (created_at DESC)
"""

_CONVERSATIONS_IDX_EMAIL = """
CREATE INDEX IF NOT EXISTS support_conversations_email_idx
    ON support_bot.conversations (email)
"""

_FAQ_CACHE_SQL = """
CREATE TABLE IF NOT EXISTS support_bot.faq_cache (
    question_hash   text        PRIMARY KEY,
    page_context    text,
    answer_payload  jsonb       NOT NULL,
    hit_count       integer     DEFAULT 1,
    last_hit_at     timestamptz DEFAULT now(),
    faq_hash        text        NOT NULL,
    created_at      timestamptz DEFAULT now()
)
"""


def init_support_schema():
    """Idempotent schema creation. Safe to call on every boot."""
    try:
        with engine.begin() as conn:
            conn.execute(text(_SCHEMA_SQL))
            conn.execute(text(_CONVERSATIONS_SQL))
            conn.execute(text(_CONVERSATIONS_IDX_CONV_TURN))
            conn.execute(text(_CONVERSATIONS_IDX_CREATED))
            conn.execute(text(_CONVERSATIONS_IDX_EMAIL))
            conn.execute(text(_FAQ_CACHE_SQL))
        log.info("[support_bot.db] schema ready")
    except Exception:
        log.exception("[support_bot.db] init_support_schema failed — bot may be unavailable")


def question_hash(question: str, page_context: Optional[str]) -> str:
    """Stable hash for cache lookup."""
    raw = (question.strip().lower() + "|" + (page_context or "")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def cache_get(qhash: str, faq_hash: str) -> Optional[dict]:
    """Return cached structured answer if present AND faq_hash matches; else None."""
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text("""
                    SELECT answer_payload, faq_hash
                    FROM support_bot.faq_cache
                    WHERE question_hash = :qh
                    LIMIT 1
                """),
                {"qh": qhash},
            ).mappings().fetchone()
            if row is None:
                return None
            if row["faq_hash"] != faq_hash:
                return None  # stale — FAQ has changed since this was cached
            # Bump hit counter (best-effort, separate txn so failure doesn't break read)
            conn.execute(
                text("""
                    UPDATE support_bot.faq_cache
                    SET hit_count = hit_count + 1, last_hit_at = now()
                    WHERE question_hash = :qh
                """),
                {"qh": qhash},
            )
            return dict(row["answer_payload"])
    except Exception:
        log.exception("[support_bot.db] cache_get failed qhash=%s", qhash[:12])
        return None


def cache_put(qhash: str, page_context: Optional[str], payload: dict, faq_hash: str) -> bool:
    """Upsert a cached answer. Returns True on success."""
    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO support_bot.faq_cache
                        (question_hash, page_context, answer_payload, faq_hash)
                    VALUES (:qh, :pg, :payload::jsonb, :fh)
                    ON CONFLICT (question_hash) DO UPDATE SET
                        answer_payload = EXCLUDED.answer_payload,
                        faq_hash       = EXCLUDED.faq_hash,
                        last_hit_at    = now()
                """),
                {
                    "qh":      qhash,
                    "pg":      page_context,
                    "payload": json.dumps(payload),
                    "fh":      faq_hash,
                },
            )
        return True
    except Exception:
        log.exception("[support_bot.db] cache_put failed qhash=%s", qhash[:12])
        return False


def log_turn(
    conversation_id: str,
    turn_idx: int,
    email: str,
    page_context: Optional[str],
    question: str,
    answer: str,
    confidence: Optional[str],
    needs_human: bool,
    cited_sections: list[str],
    tokens_input: int,
    tokens_output: int,
    tokens_cached: int,
    cost_cents: float,
) -> Optional[str]:
    """Insert a conversation turn. Returns the row id (uuid) or None on failure."""
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text("""
                    INSERT INTO support_bot.conversations
                        (conversation_id, turn_idx, email, page_context,
                         question, answer, confidence, needs_human, cited_sections,
                         tokens_input, tokens_output, tokens_cached, cost_cents)
                    VALUES (:cid, :tidx, :email, :pg,
                            :q, :a, :conf, :nh, :cited,
                            :ti, :to_, :tc, :cost)
                    RETURNING id
                """),
                {
                    "cid":   conversation_id,
                    "tidx":  turn_idx,
                    "email": email,
                    "pg":    page_context,
                    "q":     question,
                    "a":     answer,
                    "conf":  confidence,
                    "nh":    needs_human,
                    "cited": cited_sections,
                    "ti":    tokens_input,
                    "to_":   tokens_output,
                    "tc":    tokens_cached,
                    "cost":  cost_cents,
                },
            ).fetchone()
        return str(row[0]) if row else None
    except Exception:
        log.exception("[support_bot.db] log_turn failed cid=%s tidx=%s", conversation_id, turn_idx)
        return None


def record_feedback(turn_id: str, rating: str, comment: Optional[str]) -> bool:
    """Attach feedback jsonb to a turn. rating ∈ {'up','down'}."""
    try:
        payload = {"rating": rating, "comment": comment}
        with engine.begin() as conn:
            conn.execute(
                text("""
                    UPDATE support_bot.conversations
                    SET feedback = :fb::jsonb
                    WHERE id = :tid
                """),
                {"fb": json.dumps(payload), "tid": turn_id},
            )
        return True
    except Exception:
        log.exception("[support_bot.db] record_feedback failed tid=%s", turn_id)
        return False


def mark_escalated(conversation_id: str) -> bool:
    """Stamp escalated_at on every turn in the conversation."""
    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                    UPDATE support_bot.conversations
                    SET escalated_at = now()
                    WHERE conversation_id = :cid AND escalated_at IS NULL
                """),
                {"cid": conversation_id},
            )
        return True
    except Exception:
        log.exception("[support_bot.db] mark_escalated failed cid=%s", conversation_id)
        return False


def fetch_transcript(conversation_id: str, email: str) -> list[dict]:
    """Read all turns of a conversation, owned by `email`. Newest last."""
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                text("""
                    SELECT turn_idx, question, answer, confidence,
                           needs_human, cited_sections, created_at
                    FROM support_bot.conversations
                    WHERE conversation_id = :cid AND email = :email
                    ORDER BY turn_idx ASC
                """),
                {"cid": conversation_id, "email": email},
            ).mappings().fetchall()
        return [dict(r) for r in rows]
    except Exception:
        log.exception("[support_bot.db] fetch_transcript failed cid=%s", conversation_id)
        return []


def count_daily(email: str) -> int:
    """How many questions has this email asked since UTC midnight?"""
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text("""
                    SELECT COUNT(*) AS cnt
                    FROM support_bot.conversations
                    WHERE email = :email
                      AND created_at >= CURRENT_DATE
                """),
                {"email": email},
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        log.exception("[support_bot.db] count_daily failed email=%s", email)
        return 0


def health_metrics() -> dict:
    """Aggregates for the /api/support/health endpoint."""
    try:
        with engine.begin() as conn:
            row = conn.execute(text("""
                SELECT
                    COUNT(*) FILTER (WHERE created_at >= now() - interval '24 hours') AS q24h,
                    COUNT(*) FILTER (WHERE created_at >= now() - interval '7 days')   AS q7d,
                    COUNT(*) FILTER (WHERE feedback->>'rating' = 'up'
                                     AND created_at >= now() - interval '7 days')      AS up7d,
                    COUNT(*) FILTER (WHERE feedback->>'rating' = 'down'
                                     AND created_at >= now() - interval '7 days')      AS down7d,
                    COUNT(*) FILTER (WHERE escalated_at IS NOT NULL
                                     AND created_at >= now() - interval '7 days')      AS esc7d,
                    COALESCE(SUM(cost_cents) FILTER (
                        WHERE created_at >= now() - interval '24 hours'), 0)            AS cost24h_cents,
                    COALESCE(SUM(cost_cents) FILTER (
                        WHERE created_at >= now() - interval '7 days'),  0)             AS cost7d_cents
                FROM support_bot.conversations
            """)).mappings().fetchone()
        return dict(row) if row else {}
    except Exception:
        log.exception("[support_bot.db] health_metrics failed")
        return {}
