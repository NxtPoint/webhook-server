# support_bot/support_api.py — Flask blueprint for the customer-service bot.
#
# Endpoints (all X-Client-Key authenticated):
#   POST /api/support/ask         — main entry: question in, structured answer out
#   POST /api/support/feedback    — thumbs up/down on a turn
#   POST /api/support/escalate    — email the transcript to info@ten-fifty5.com
#   GET  /api/support/health      — admin-only: usage + cost metrics
#
# Auth: same X-Client-Key pattern as client_api.py.
# All responses include CORS headers via the existing global afterRequest hook.

from __future__ import annotations

import hmac
import logging
import os
import uuid
from typing import Optional

from flask import Blueprint, jsonify, request
from sqlalchemy import text

from db_init import engine

from support_bot import db as sb_db
from support_bot.email_sender import send_escalation
from support_bot.faq_loader import FAQ_HASH, FAQ_LOADED_AT, FAQ_TEXT
from support_bot.haiku_client import call_haiku
from support_bot.prompt_builder import (
    ANSWER_TOOL,
    build_system_prompt,
    build_user_message,
)
from support_bot.rate_limiter import HARD_LIMIT, check_rate_limit

log = logging.getLogger(__name__)

support_bp = Blueprint("support_bot", __name__)

CLIENT_API_KEY = os.environ.get("CLIENT_API_KEY", "").strip()
ADMIN_EMAILS = {"info@ten-fifty5.com", "tomo.stojakovic@gmail.com"}

MAX_MESSAGE_LEN = 1000


# ---------- auth ----------

def _check_client_key() -> bool:
    hk = (request.headers.get("X-Client-Key") or "").strip()
    return bool(CLIENT_API_KEY) and hmac.compare_digest(hk, CLIENT_API_KEY)


def _err(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


# ---------- user context ----------

def _first_name_from_full(full_name: str) -> str:
    """Extract a first-name guess from full_name. Empty string if unparseable."""
    s = (full_name or "").strip()
    if not s:
        return ""
    return s.split()[0]


def _fetch_user_context(email: str) -> dict:
    """
    Pull what the bot needs to know about the user from billing tables.
    All fields optional — bot handles missing values gracefully.

    `billing.subscription_state` is an opt-in table; we check existence in
    information_schema.tables BEFORE selecting from it (otherwise a missing
    table aborts the whole transaction). Pattern matches client_api.py.
    """
    if not email:
        return {}
    e = email.strip().lower()
    try:
        with engine.begin() as conn:
            has_sub_table = conn.execute(text("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'billing' AND table_name = 'subscription_state'
                )
            """)).scalar()

            if has_sub_table:
                row = conn.execute(text("""
                    SELECT
                        a.id                                  AS account_id,
                        COALESCE(m.full_name, '')             AS full_name,
                        COALESCE(m.role, 'player_parent')     AS role,
                        COALESCE(s.plan_code, '')             AS plan,
                        COALESCE(v.matches_remaining, 0)      AS credits_remaining
                    FROM billing.account a
                    LEFT JOIN billing.member m
                      ON m.account_id = a.id AND m.is_primary = true
                    LEFT JOIN billing.subscription_state s
                      ON s.account_id = a.id
                    LEFT JOIN billing.vw_customer_usage v
                      ON v.account_id = a.id
                    WHERE a.email = :email
                    LIMIT 1
                """), {"email": e}).mappings().first()
            else:
                row = conn.execute(text("""
                    SELECT
                        a.id                                  AS account_id,
                        COALESCE(m.full_name, '')             AS full_name,
                        COALESCE(m.role, 'player_parent')     AS role,
                        ''                                    AS plan,
                        COALESCE(v.matches_remaining, 0)      AS credits_remaining
                    FROM billing.account a
                    LEFT JOIN billing.member m
                      ON m.account_id = a.id AND m.is_primary = true
                    LEFT JOIN billing.vw_customer_usage v
                      ON v.account_id = a.id
                    WHERE a.email = :email
                    LIMIT 1
                """), {"email": e}).mappings().first()

        if not row:
            return {}
        d = dict(row)
        d["first_name"] = _first_name_from_full(d.get("full_name") or "")
        return d
    except Exception:
        log.exception("[support_bot] _fetch_user_context failed for %s", e)
        return {}


# ---------- POST /api/support/ask ----------

@support_bp.post("/api/support/ask")
def ask():
    if not _check_client_key():
        return _err("unauthorized", 401)

    body = request.get_json(silent=True) or {}
    message = (body.get("message") or "").strip()
    email = (body.get("email") or "").strip().lower()
    page_context = (body.get("page_context") or "").strip() or None
    conversation_id = (body.get("conversation_id") or "").strip() or None

    if not message:
        return _err("message_required")
    if len(message) > MAX_MESSAGE_LEN:
        return _err(f"message_too_long_max_{MAX_MESSAGE_LEN}_chars")
    if not email:
        return _err("email_required")

    # Hard kill switch — env var to disable bot in case of cost spike or outage.
    if os.environ.get("SUPPORT_BOT_ENABLED", "true").lower() in ("0", "false", "no"):
        return jsonify({
            "ok":          True,
            "answer":      "Our support bot is temporarily unavailable. Please email "
                           "info@ten-fifty5.com and we'll get back to you.",
            "confidence":  "low",
            "needs_human": True,
            "cited_sections": [],
            "actions":     [],
            "tokens_used": {"input": 0, "output": 0, "cached": 0},
        }), 200

    # Rate limit
    allowed, reason, resets_at, used = check_rate_limit(email)
    if not allowed:
        return jsonify({
            "ok":         False,
            "error":      reason,
            "resets_at":  resets_at,
            "used_today": used,
            "hard_limit": HARD_LIMIT,
        }), 429

    # Per-conversation turn index — auto-assign on first turn.
    if not conversation_id:
        conversation_id = str(uuid.uuid4())
        turn_idx = 0
    else:
        try:
            with engine.begin() as conn:
                row = conn.execute(text("""
                    SELECT COALESCE(MAX(turn_idx), -1) + 1 AS next_idx
                    FROM support_bot.conversations
                    WHERE conversation_id = :cid AND email = :email
                """), {"cid": conversation_id, "email": email}).fetchone()
            turn_idx = int(row[0]) if row else 0
        except Exception:
            log.exception("[support_bot] turn_idx lookup failed — using 0")
            turn_idx = 0

    # User context for prompt
    ctx = _fetch_user_context(email)
    first_name = ctx.get("first_name") or None
    plan = ctx.get("plan") or None
    role = ctx.get("role") or None
    credits_remaining = ctx.get("credits_remaining")

    # Cache lookup
    qhash = sb_db.question_hash(message, page_context)
    cached = sb_db.cache_get(qhash, FAQ_HASH) if FAQ_HASH else None
    if cached:
        # Log the turn anyway (for rate limiting + analytics) but no LLM cost.
        sb_db.log_turn(
            conversation_id=conversation_id,
            turn_idx=turn_idx,
            email=email,
            page_context=page_context,
            question=message,
            answer=cached.get("answer", ""),
            confidence=cached.get("confidence"),
            needs_human=bool(cached.get("needs_human")),
            cited_sections=list(cached.get("cited_sections") or []),
            tokens_input=0,
            tokens_output=0,
            tokens_cached=0,
            cost_cents=0.0,
        )
        return jsonify({
            "ok":              True,
            "conversation_id": conversation_id,
            "turn_idx":        turn_idx,
            "answer":          cached.get("answer", ""),
            "confidence":      cached.get("confidence", "medium"),
            "needs_human":     bool(cached.get("needs_human")),
            "cited_sections":  list(cached.get("cited_sections") or []),
            "actions":         list(cached.get("actions") or []),
            "tokens_used":     {"input": 0, "output": 0, "cached": 0},
            "from_cache":      True,
        }), 200

    # FAQ missing? Hard-escalate without spending tokens.
    if not FAQ_TEXT:
        log.warning("[support_bot] FAQ empty — escalating question")
        return jsonify({
            "ok":              True,
            "conversation_id": conversation_id,
            "turn_idx":        turn_idx,
            "answer":          "I don't have my reference notes loaded right now. "
                               "Please email info@ten-fifty5.com and we'll get back to you.",
            "confidence":      "low",
            "needs_human":     True,
            "cited_sections":  [],
            "actions":         [],
            "tokens_used":     {"input": 0, "output": 0, "cached": 0},
        }), 200

    # Call Haiku
    system_prompt = build_system_prompt()
    user_msg = build_user_message(
        question=message,
        first_name=first_name,
        plan=plan,
        role=role,
        credits_remaining=credits_remaining,
        page_context=page_context,
    )
    result = call_haiku(system_prompt, user_msg, ANSWER_TOOL)

    if not result.get("ok"):
        # Fail-safe: still log the failed turn so we can audit, but escalate the user.
        sb_db.log_turn(
            conversation_id=conversation_id,
            turn_idx=turn_idx,
            email=email,
            page_context=page_context,
            question=message,
            answer="(LLM call failed — escalated)",
            confidence="low",
            needs_human=True,
            cited_sections=[],
            tokens_input=0,
            tokens_output=0,
            tokens_cached=0,
            cost_cents=0.0,
        )
        return jsonify({
            "ok":              True,  # ok=true at the API layer; the bot answers with escalation
            "conversation_id": conversation_id,
            "turn_idx":        turn_idx,
            "answer":          "I'm having trouble answering right now — please email "
                               "info@ten-fifty5.com and we'll respond.",
            "confidence":      "low",
            "needs_human":     True,
            "cited_sections":  [],
            "actions":         [],
            "tokens_used":     {"input": 0, "output": 0, "cached": 0},
            "llm_error":       result.get("error"),
        }), 200

    tool_input = result["tool_input"]
    answer = (tool_input.get("answer") or "").strip()
    confidence = tool_input.get("confidence") or "medium"
    needs_human = bool(tool_input.get("needs_human"))
    cited = list(tool_input.get("cited_sections") or [])
    actions = list(tool_input.get("actions") or [])

    payload = {
        "answer":         answer,
        "confidence":     confidence,
        "needs_human":    needs_human,
        "cited_sections": cited,
        "actions":        actions,
    }

    # Persist to cache (only if confidence is high — don't cache speculative answers)
    if confidence == "high" and not needs_human:
        sb_db.cache_put(qhash, page_context, payload, FAQ_HASH)

    # Log the turn
    sb_db.log_turn(
        conversation_id=conversation_id,
        turn_idx=turn_idx,
        email=email,
        page_context=page_context,
        question=message,
        answer=answer,
        confidence=confidence,
        needs_human=needs_human,
        cited_sections=cited,
        tokens_input=int(result.get("tokens_input") or 0),
        tokens_output=int(result.get("tokens_output") or 0),
        tokens_cached=int(result.get("tokens_cached") or 0),
        cost_cents=float(result.get("cost_cents") or 0.0),
    )

    return jsonify({
        "ok":              True,
        "conversation_id": conversation_id,
        "turn_idx":        turn_idx,
        "answer":          answer,
        "confidence":      confidence,
        "needs_human":     needs_human,
        "cited_sections":  cited,
        "actions":         actions,
        "tokens_used": {
            "input":       result.get("tokens_input"),
            "output":      result.get("tokens_output"),
            "cached":      result.get("tokens_cached"),
            "cache_write": result.get("tokens_cache_write"),
        },
        "from_cache": False,
    }), 200


# ---------- POST /api/support/feedback ----------

@support_bp.post("/api/support/feedback")
def feedback():
    if not _check_client_key():
        return _err("unauthorized", 401)

    body = request.get_json(silent=True) or {}
    turn_id = (body.get("turn_id") or "").strip()
    rating = (body.get("rating") or "").strip().lower()
    comment = (body.get("comment") or "").strip() or None

    if not turn_id:
        return _err("turn_id_required")
    if rating not in ("up", "down"):
        return _err("rating_must_be_up_or_down")

    ok = sb_db.record_feedback(turn_id, rating, comment)
    return jsonify({"ok": ok}), (200 if ok else 500)


# ---------- POST /api/support/escalate ----------

@support_bp.post("/api/support/escalate")
def escalate():
    if not _check_client_key():
        return _err("unauthorized", 401)

    body = request.get_json(silent=True) or {}
    conversation_id = (body.get("conversation_id") or "").strip()
    email = (body.get("email") or "").strip().lower()
    user_note = (body.get("user_note") or "").strip() or None

    if not conversation_id:
        return _err("conversation_id_required")
    if not email:
        return _err("email_required")

    transcript = sb_db.fetch_transcript(conversation_id, email)
    if not transcript:
        return _err("conversation_not_found", 404)

    ctx = _fetch_user_context(email)
    customer_name = ctx.get("first_name") or ""
    plan = ctx.get("plan") or None
    role = ctx.get("role") or None

    result = send_escalation(
        customer_email=email,
        customer_name=customer_name,
        plan=plan,
        role=role,
        transcript=transcript,
        user_note=user_note,
    )
    if result.get("ok"):
        sb_db.mark_escalated(conversation_id)
        return jsonify({"ok": True, "email_sent": True}), 200
    return jsonify({"ok": False, "error": result.get("error", "ses_send_failed")}), 500


# ---------- GET /api/support/health (admin) ----------

@support_bp.get("/api/support/health")
def health():
    if not _check_client_key():
        return _err("unauthorized", 401)
    email = (request.args.get("email") or "").strip().lower()
    if email not in ADMIN_EMAILS:
        return _err("admin_only", 403)

    metrics = sb_db.health_metrics()
    # numeric cents → dollars for readability
    for k in ("cost24h_cents", "cost7d_cents"):
        if k in metrics and metrics[k] is not None:
            metrics[k.replace("_cents", "_usd")] = round(float(metrics[k]) / 100.0, 4)

    return jsonify({
        "ok":           True,
        "faq_hash":     FAQ_HASH[:12] if FAQ_HASH else "",
        "faq_loaded":   FAQ_LOADED_AT,
        "faq_chars":    len(FAQ_TEXT),
        "metrics":      metrics,
    }), 200
