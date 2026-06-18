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

# ---------------------------------------------------------------------------
# Phase-3 feedback loop — consolidated signal table + human-in-the-loop FAQ workflow.
#
#   support_bot.feedback_signal — one row per customer feedback signal, from
#                                 every source (bot low-conf / thumbs-down /
#                                 escalation, NPS detractors, cancellation +
#                                 widget surveys). The single queryable table.
#   support_bot.faq_candidate   — drafted FAQ entries awaiting human approval.
#                                 NO auto-publish: a human edits faq.md + commits.
#
# Mining views aggregate open signals for the admin cockpit.
# ---------------------------------------------------------------------------

_FEEDBACK_SIGNAL_SQL = """
CREATE TABLE IF NOT EXISTS support_bot.feedback_signal (
    id            bigserial   PRIMARY KEY,
    signal_type   text        NOT NULL,
    source_id     text,
    account_id    bigint,
    email         text,
    question      text,
    context       text,
    raw_feedback  jsonb,
    priority      text        NOT NULL DEFAULT 'medium',
    processed_at  timestamptz,
    routed_to     text,
    created_at    timestamptz NOT NULL DEFAULT now()
)
"""

_FEEDBACK_SIGNAL_IDX_SRC = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_feedback_signal_src
    ON support_bot.feedback_signal (signal_type, source_id)
    WHERE source_id IS NOT NULL
"""

_FEEDBACK_SIGNAL_IDX_OPEN = """
CREATE INDEX IF NOT EXISTS ix_feedback_signal_open
    ON support_bot.feedback_signal (created_at)
    WHERE processed_at IS NULL
"""

_FAQ_CANDIDATE_SQL = """
CREATE TABLE IF NOT EXISTS support_bot.faq_candidate (
    id              bigserial   PRIMARY KEY,
    section_id      text,
    title           text,
    answer_draft    text,
    source_signals  bigint[],
    confidence      text        DEFAULT 'medium',
    status          text        NOT NULL DEFAULT 'draft',
    approved_by     text,
    approved_at     timestamptz,
    rejected_reason text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz
)
"""

_FAQ_CANDIDATE_IDX_STATUS = """
CREATE INDEX IF NOT EXISTS ix_faq_candidate_status
    ON support_bot.faq_candidate (status, created_at)
"""

# Mining views ---------------------------------------------------------------

_VW_TOP_UNANSWERED_SQL = """
CREATE OR REPLACE VIEW support_bot.vw_top_unanswered AS
SELECT
    signal_type,
    lower(btrim(question))        AS question,
    context,
    count(*)                      AS freq,
    max(created_at)               AS most_recent,
    array_agg(id)                 AS signal_ids
FROM support_bot.feedback_signal
WHERE processed_at IS NULL
  AND signal_type IN ('bot_low_conf', 'bot_escalation', 'bot_thumbsdown')
  AND btrim(coalesce(question, '')) <> ''
GROUP BY signal_type, lower(btrim(question)), context
ORDER BY freq DESC
"""

_VW_CANCELLATION_THEMES_SQL = """
CREATE OR REPLACE VIEW support_bot.vw_cancellation_themes AS
SELECT
    COALESCE(raw_feedback->>'reason', context, '(unspecified)') AS reason,
    count(*)        AS freq,
    max(created_at) AS most_recent
FROM support_bot.feedback_signal
WHERE signal_type = 'survey_cancellation'
GROUP BY 1
ORDER BY freq DESC
"""

_VW_DETRACTOR_COMMENTS_SQL = """
CREATE OR REPLACE VIEW support_bot.vw_detractor_comments AS
SELECT
    question        AS comment,
    context,
    count(*)        AS freq,
    max(created_at) AS most_recent
FROM support_bot.feedback_signal
WHERE signal_type = 'nps_detractor'
  AND btrim(coalesce(question, '')) <> ''
GROUP BY question, context
ORDER BY freq DESC
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
            # Phase-3 feedback loop
            conn.execute(text(_FEEDBACK_SIGNAL_SQL))
            conn.execute(text(_FEEDBACK_SIGNAL_IDX_SRC))
            conn.execute(text(_FEEDBACK_SIGNAL_IDX_OPEN))
            conn.execute(text(_FAQ_CANDIDATE_SQL))
            conn.execute(text(_FAQ_CANDIDATE_IDX_STATUS))
            conn.execute(text(_VW_TOP_UNANSWERED_SQL))
            conn.execute(text(_VW_CANCELLATION_THEMES_SQL))
            conn.execute(text(_VW_DETRACTOR_COMMENTS_SQL))
        log.info("[support_bot.db] schema ready")
    except Exception:
        log.exception("[support_bot.db] init_support_schema failed — bot may be unavailable")


def log_feedback_signal(
    signal_type: str,
    *,
    source_id: Optional[str] = None,
    account_id: Optional[int] = None,
    email: Optional[str] = None,
    question: Optional[str] = None,
    context: Optional[str] = None,
    raw_feedback: Optional[dict] = None,
    priority: str = "medium",
) -> bool:
    """Insert one consolidated feedback signal. Idempotent on (signal_type, source_id)
    when source_id is given. NEVER raises — best-effort, log+swallow."""
    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO support_bot.feedback_signal
                        (signal_type, source_id, account_id, email,
                         question, context, raw_feedback, priority)
                    VALUES
                        (:st, :sid, :aid, :email,
                         :q, :ctx, CAST(:raw AS jsonb), :prio)
                    ON CONFLICT (signal_type, source_id)
                        WHERE source_id IS NOT NULL
                        DO NOTHING
                """),
                {
                    "st":    signal_type,
                    "sid":   source_id,
                    "aid":   account_id,
                    "email": email,
                    "q":     question,
                    "ctx":   context,
                    "raw":   json.dumps(raw_feedback) if raw_feedback is not None else None,
                    "prio":  priority or "medium",
                },
            )
        return True
    except Exception:
        log.exception("[support_bot.db] log_feedback_signal failed type=%s sid=%s",
                      signal_type, source_id)
        return False


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
        turn_id = str(row[0]) if row else None
        # Phase-3: a low-confidence answer is an unanswered-question signal.
        if turn_id and (confidence or "").lower() == "low":
            log_feedback_signal(
                "bot_low_conf",
                source_id=turn_id,
                email=email,
                question=question,
                context=page_context,
                priority="medium",
            )
        return turn_id
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
        # Phase-3: a thumbs-down is a negative signal worth mining.
        if rating == "down":
            try:
                with engine.begin() as conn:
                    trow = conn.execute(
                        text("""
                            SELECT question, email, page_context
                            FROM support_bot.conversations
                            WHERE id = :tid
                            LIMIT 1
                        """),
                        {"tid": turn_id},
                    ).mappings().fetchone()
            except Exception:
                trow = None
                log.exception("[support_bot.db] record_feedback turn lookup failed tid=%s", turn_id)
            if trow:
                log_feedback_signal(
                    "bot_thumbsdown",
                    source_id=turn_id,
                    email=trow.get("email"),
                    question=trow.get("question"),
                    context=trow.get("page_context"),
                    raw_feedback={"comment": comment},
                    priority="medium",
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
        # Phase-3: a human escalation is a high-priority signal.
        try:
            with engine.begin() as conn:
                trow = conn.execute(
                    text("""
                        SELECT question, email, page_context
                        FROM support_bot.conversations
                        WHERE conversation_id = :cid
                        ORDER BY turn_idx DESC
                        LIMIT 1
                    """),
                    {"cid": conversation_id},
                ).mappings().fetchone()
        except Exception:
            trow = None
            log.exception("[support_bot.db] mark_escalated turn lookup failed cid=%s", conversation_id)
        if trow:
            log_feedback_signal(
                "bot_escalation",
                source_id=conversation_id,
                email=trow.get("email"),
                question=trow.get("question"),
                context=trow.get("page_context"),
                priority="high",
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


# ---------------------------------------------------------------------------
# Phase-3: NPS / survey sync (core.* → feedback_signal)
# ---------------------------------------------------------------------------

def _table_exists(conn, schema: str, table: str) -> bool:
    """Guard for opt-in core.* tables — a missing table inside a SELECT aborts the
    whole transaction (see memory feedback_postgres_missing_table)."""
    return bool(conn.execute(
        text("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = :s AND table_name = :t
            )
        """),
        {"s": schema, "t": table},
    ).scalar())


def sync_feedback_signals() -> dict:
    """Idempotently copy core.nps_response / core.survey_response into
    feedback_signal. Set-based INSERT...SELECT...ON CONFLICT DO NOTHING.
    Returns counts inserted per signal_type. NEVER raises."""
    counts = {"nps_detractor": 0, "survey_cancellation": 0, "survey_widget": 0}
    try:
        with engine.begin() as conn:
            has_nps = _table_exists(conn, "core", "nps_response")
            has_survey = _table_exists(conn, "core", "survey_response")

            if has_nps:
                res = conn.execute(text("""
                    INSERT INTO support_bot.feedback_signal
                        (signal_type, source_id, account_id, email,
                         question, context, raw_feedback, priority)
                    SELECT
                        'nps_detractor',
                        'nps:' || n.id,
                        n.account_id,
                        NULL,
                        n.comment,
                        'nps',
                        jsonb_build_object('score', n.score, 'bucket', n.bucket),
                        'high'
                    FROM core.nps_response n
                    WHERE n.bucket = 'detractor'
                    ON CONFLICT (signal_type, source_id) WHERE source_id IS NOT NULL
                    DO NOTHING
                """))
                counts["nps_detractor"] = res.rowcount or 0

            if has_survey:
                res = conn.execute(text("""
                    INSERT INTO support_bot.feedback_signal
                        (signal_type, source_id, account_id, email,
                         question, context, raw_feedback, priority)
                    SELECT
                        'survey_cancellation',
                        'survey:' || s.id,
                        s.account_id,
                        NULL,
                        s.responses->>'comment',
                        s.survey_key,
                        s.responses,
                        'high'
                    FROM core.survey_response s
                    WHERE s.survey_key ILIKE '%cancel%'
                    ON CONFLICT (signal_type, source_id) WHERE source_id IS NOT NULL
                    DO NOTHING
                """))
                counts["survey_cancellation"] = res.rowcount or 0

                res = conn.execute(text("""
                    INSERT INTO support_bot.feedback_signal
                        (signal_type, source_id, account_id, email,
                         question, context, raw_feedback, priority)
                    SELECT
                        'survey_widget',
                        'survey:' || s.id,
                        s.account_id,
                        NULL,
                        s.responses->>'message',
                        s.survey_key,
                        s.responses,
                        'medium'
                    FROM core.survey_response s
                    WHERE s.survey_key NOT ILIKE '%cancel%'
                    ON CONFLICT (signal_type, source_id) WHERE source_id IS NOT NULL
                    DO NOTHING
                """))
                counts["survey_widget"] = res.rowcount or 0
        return counts
    except Exception:
        log.exception("[support_bot.db] sync_feedback_signals failed")
        return counts


# ---------------------------------------------------------------------------
# Phase-3: mining-view readers + FAQ-candidate workflow
# ---------------------------------------------------------------------------

def _rows_dict(sql: str, params: Optional[dict] = None) -> list[dict]:
    try:
        with engine.begin() as conn:
            rows = conn.execute(text(sql), params or {}).mappings().fetchall()
        return [dict(r) for r in rows]
    except Exception:
        log.exception("[support_bot.db] query failed: %s", sql[:80])
        return []


def mining_signals() -> dict:
    """The three mining views + open-signal counts by type, for the admin endpoint."""
    open_counts = _rows_dict("""
        SELECT signal_type, count(*) AS open_count
        FROM support_bot.feedback_signal
        WHERE processed_at IS NULL
        GROUP BY signal_type
        ORDER BY open_count DESC
    """)
    return {
        "top_unanswered":      _rows_dict("SELECT * FROM support_bot.vw_top_unanswered LIMIT 100"),
        "cancellation_themes": _rows_dict("SELECT * FROM support_bot.vw_cancellation_themes LIMIT 100"),
        "detractor_comments":  _rows_dict("SELECT * FROM support_bot.vw_detractor_comments LIMIT 100"),
        "open_counts":         {r["signal_type"]: r["open_count"] for r in open_counts},
    }


def _draft_faq_answer(question: str) -> dict:
    """Use Haiku to draft a concise FAQ answer (<=100 words) grounded in faq.md.
    Returns {answer_draft, confidence}. On any error → empty draft + low confidence
    so a human can write it. NEVER raises."""
    try:
        from support_bot.haiku_client import call_haiku
        from support_bot.faq_loader import FAQ_TEXT
        if not FAQ_TEXT:
            return {"answer_draft": "", "confidence": "low"}
        system_prompt = (
            "You are drafting a candidate FAQ answer for human review. Answer ONLY "
            "from the reference FAQ below; if it is not covered, say so plainly. Keep "
            "the answer concise (100 words max), factual, and customer-friendly.\n\n"
            "=== REFERENCE FAQ ===\n" + FAQ_TEXT
        )
        tool = {
            "name": "draft_faq_answer",
            "description": "Return a concise candidate FAQ answer for the question.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "answer_draft": {"type": "string",
                                     "description": "Concise answer, <=100 words, grounded in the FAQ."},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"],
                                   "description": "How well the FAQ covers this question."},
                },
                "required": ["answer_draft", "confidence"],
            },
        }
        result = call_haiku(system_prompt, f"Customer question: {question}", tool)
        if not result.get("ok"):
            return {"answer_draft": "", "confidence": "low"}
        ti = result.get("tool_input") or {}
        draft = (ti.get("answer_draft") or "").strip()
        conf = ti.get("confidence") or "medium"
        if not draft:
            return {"answer_draft": "", "confidence": "low"}
        return {"answer_draft": draft, "confidence": conf}
    except Exception:
        log.exception("[support_bot.db] _draft_faq_answer failed")
        return {"answer_draft": "", "confidence": "low"}


def propose_faq_entry(question: str, signal_ids: Optional[list[int]] = None) -> Optional[dict]:
    """Draft a FAQ answer (Haiku, grounded in faq.md) and insert a faq_candidate
    row status='draft'. If Haiku is unavailable the candidate is still inserted
    with an empty draft + confidence 'low' so a human can write it. Returns the
    inserted candidate row dict, or None on insert failure. NEVER raises."""
    try:
        q = (question or "").strip()
        if not q:
            return None
        drafted = _draft_faq_answer(q)
        sig = list(signal_ids) if signal_ids else None
        with engine.begin() as conn:
            row = conn.execute(
                text("""
                    INSERT INTO support_bot.faq_candidate
                        (title, answer_draft, source_signals, confidence, status, updated_at)
                    VALUES
                        (:title, :draft, :sigs, :conf, 'draft', now())
                    RETURNING id, section_id, title, answer_draft, source_signals,
                              confidence, status, approved_by, approved_at,
                              rejected_reason, created_at, updated_at
                """),
                {
                    "title": q,
                    "draft": drafted["answer_draft"],
                    "sigs":  sig,
                    "conf":  drafted["confidence"],
                },
            ).mappings().fetchone()
        return dict(row) if row else None
    except Exception:
        log.exception("[support_bot.db] propose_faq_entry failed")
        return None


def list_faq_candidates(status: Optional[str] = None) -> list[dict]:
    if status:
        return _rows_dict(
            "SELECT * FROM support_bot.faq_candidate WHERE status = :s "
            "ORDER BY created_at DESC LIMIT 200",
            {"s": status},
        )
    return _rows_dict(
        "SELECT * FROM support_bot.faq_candidate ORDER BY created_at DESC LIMIT 200")


def approve_faq_candidate(candidate_id: int, approved_by: str) -> Optional[dict]:
    """Mark a candidate approved. Publishing remains a human task (edit faq.md +
    commit) — this NEVER writes faq.md. NEVER raises."""
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text("""
                    UPDATE support_bot.faq_candidate
                    SET status = 'approved', approved_by = :by,
                        approved_at = now(), updated_at = now()
                    WHERE id = :id
                    RETURNING id, section_id, title, answer_draft, source_signals,
                              confidence, status, approved_by, approved_at,
                              rejected_reason, created_at, updated_at
                """),
                {"id": candidate_id, "by": approved_by},
            ).mappings().fetchone()
        return dict(row) if row else None
    except Exception:
        log.exception("[support_bot.db] approve_faq_candidate failed id=%s", candidate_id)
        return None


def reject_faq_candidate(candidate_id: int, reason: Optional[str]) -> Optional[dict]:
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text("""
                    UPDATE support_bot.faq_candidate
                    SET status = 'rejected', rejected_reason = :reason, updated_at = now()
                    WHERE id = :id
                    RETURNING id, section_id, title, answer_draft, source_signals,
                              confidence, status, approved_by, approved_at,
                              rejected_reason, created_at, updated_at
                """),
                {"id": candidate_id, "reason": reason},
            ).mappings().fetchone()
        return dict(row) if row else None
    except Exception:
        log.exception("[support_bot.db] reject_faq_candidate failed id=%s", candidate_id)
        return None


def get_feedback_signal(signal_id: int) -> Optional[dict]:
    rows = _rows_dict(
        "SELECT * FROM support_bot.feedback_signal WHERE id = :id LIMIT 1",
        {"id": signal_id})
    return rows[0] if rows else None


def mark_signal_processed(signal_id: int, routed_to: str) -> bool:
    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                    UPDATE support_bot.feedback_signal
                    SET processed_at = now(), routed_to = :rt
                    WHERE id = :id
                """),
                {"id": signal_id, "rt": routed_to},
            )
        return True
    except Exception:
        log.exception("[support_bot.db] mark_signal_processed failed id=%s", signal_id)
        return False
