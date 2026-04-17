# tennis_coach/coach_api.py — Flask blueprint for LLM Tennis Coach endpoints.
#
# All routes are under /api/client/coach/* following the client_api pattern.
# Auth: X-Client-Key header (same CLIENT_API_KEY as client_api.py).
# Tenant isolation: email query/body param, verified against bronze.submission_context.
#
# Endpoints:
#   POST /api/client/coach/analyze         — main coaching call
#   GET  /api/client/coach/cards/<task_id> — pre-generated insight cards
#   GET  /api/client/coach/status/<task_id>— poll for card generation status
#   GET  /api/client/coach/debug/<task_id> — raw data payload (admin only)

import hmac
import json
import logging
import os
from typing import Optional

from flask import Blueprint, jsonify, request
from sqlalchemy import text

from db_init import engine
from tennis_coach.claude_client import call_claude
from tennis_coach.data_fetcher import fetch_match_data
from tennis_coach.db import cache_get, cache_put, freeform_key

# Technique analysis data fetcher (lazy import to avoid boot-time circular deps)
def _fetch_data_for_task(task_id: str) -> dict:
    """Route to match or technique data fetcher based on sport_type."""
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT sport_type FROM bronze.submission_context WHERE task_id = :t"),
                {"t": task_id},
            ).mappings().first()
        if row and row.get("sport_type") == "technique_analysis":
            from technique.coach_data_fetcher import fetch_technique_data
            return fetch_technique_data(task_id)
    except Exception:
        pass  # Fall through to match fetcher
    return fetch_match_data(task_id)
from tennis_coach.prompt_builder import (
    build_cards_prompt,
    build_freeform_prompt,
    build_serve_analysis_prompt,
    build_tactics_prompt,
    build_weakness_prompt,
)
from tennis_coach.rate_limiter import check_rate_limit

log = logging.getLogger(__name__)

coach_bp = Blueprint("tennis_coach", __name__)

CLIENT_API_KEY = os.environ.get("CLIENT_API_KEY", "").strip()

# Prompt keys that map to named templates
_NAMED_PROMPTS = {
    "serve_analysis": build_serve_analysis_prompt,
    "weakness":       build_weakness_prompt,
    "tactics":        build_tactics_prompt,
}

# Cards are free (not rate-limited, cached indefinitely per match)
_CARDS_PROMPT_KEY = "cards"

# Admin emails (same whitelist as client_api.py)
_ADMIN_EMAILS = {"info@ten-fifty5.com", "tomo.stojakovic@gmail.com"}


# ---------------------------------------------------------------------------
# Auth helpers (mirrors client_api.py)
# ---------------------------------------------------------------------------

def _guard() -> bool:
    hk = request.headers.get("X-Client-Key") or ""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        hk = auth.split(" ", 1)[1].strip()
    return bool(CLIENT_API_KEY) and hmac.compare_digest(hk.strip(), CLIENT_API_KEY)


def _forbid():
    return jsonify({"ok": False, "error": "forbidden"}), 403


def _norm_email(email: Optional[str]) -> str:
    return (email or "").strip().lower()


# ---------------------------------------------------------------------------
# Ownership check
# ---------------------------------------------------------------------------

def _verify_ownership(task_id: str, email: str) -> bool:
    """Return True if email owns this task_id (matches bronze.submission_context)."""
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT 1 FROM bronze.submission_context
                    WHERE task_id = :tid AND email = :email
                    LIMIT 1
                """),
                {"tid": task_id, "email": email},
            ).fetchone()
        return row is not None
    except Exception:
        log.exception("[coach_api] ownership check failed task_id=%s", task_id)
        return False


# ---------------------------------------------------------------------------
# AI Coach paywall — see docs/pricing_strategy.md §7
# ---------------------------------------------------------------------------

def _check_ai_coach_entitled(email: str) -> tuple[bool, Optional[str]]:
    """AI Coach is the premium differentiator. Returns (allowed, reason).

    Allowed: admins, coaches, and paid_active players.
    Blocked: free-trial users (no active subscription) and cancelled/expired.
    """
    if email in _ADMIN_EMAILS:
        return True, None

    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT
                      a.active AS account_active,
                      COALESCE(m.role, 'player_parent') AS role,
                      (s.status = 'ACTIVE') AS paid_active
                    FROM billing.account a
                    LEFT JOIN billing.member m
                      ON m.account_id = a.id AND m.is_primary = true
                    LEFT JOIN LATERAL (
                      SELECT status
                      FROM billing.subscription_state
                      WHERE account_id = a.id
                      ORDER BY updated_at DESC NULLS LAST
                      LIMIT 1
                    ) s ON TRUE
                    WHERE a.email = :email
                    LIMIT 1
                """),
                {"email": email},
            ).mappings().first()
    except Exception:
        log.exception("[coach_api] entitlement check failed email=%s", email)
        return False, "ENTITLEMENT_CHECK_FAILED"

    if not row:
        return False, "ACCOUNT_NOT_FOUND"
    if not row["account_active"]:
        return False, "ACCOUNT_INACTIVE"
    if row["role"] == "coach":
        return True, None
    if row["paid_active"]:
        return True, None
    return False, "UPGRADE_REQUIRED"


def _paywall_response():
    """402 Payment Required — tells the frontend to swap in the upgrade teaser."""
    return jsonify({
        "ok": False,
        "error": "UPGRADE_REQUIRED",
        "message": "AI Coach is included with all paid plans. Upgrade to unlock.",
        "upgrade_url": "/pricing",
    }), 402


# ---------------------------------------------------------------------------
# Options preflight
# ---------------------------------------------------------------------------

@coach_bp.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        return "", 204


# ---------------------------------------------------------------------------
# POST /api/client/coach/analyze
# ---------------------------------------------------------------------------

@coach_bp.route("/api/client/coach/analyze", methods=["POST", "OPTIONS"])
def analyze():
    """
    Main coaching endpoint.

    Body (JSON):
      task_id      — match identifier
      email        — user email (tenant isolation)
      prompt_key   — 'serve_analysis' | 'weakness' | 'tactics' | 'freeform'
      freeform_text — required when prompt_key == 'freeform'
      force        — boolean, skip cache and regenerate

    Response:
      { ok, response, data_snapshot, cached, tokens_used }
    """
    if not _guard():
        return _forbid()

    body = request.get_json(silent=True) or {}
    task_id    = (body.get("task_id") or "").strip()
    email      = _norm_email(body.get("email") or request.args.get("email"))
    prompt_key = (body.get("prompt_key") or "").strip()
    freeform   = (body.get("freeform_text") or "").strip()
    force      = bool(body.get("force", False))

    if not task_id:
        return jsonify({"ok": False, "error": "task_id required"}), 400
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400
    if not prompt_key:
        return jsonify({"ok": False, "error": "prompt_key required"}), 400
    if prompt_key == "freeform" and not freeform:
        return jsonify({"ok": False, "error": "freeform_text required for freeform prompt_key"}), 400
    if prompt_key not in _NAMED_PROMPTS and prompt_key != "freeform":
        return jsonify({"ok": False, "error": f"unknown prompt_key: {prompt_key}"}), 400

    if not _verify_ownership(task_id, email):
        return jsonify({"ok": False, "error": "not found or access denied"}), 404

    # Paywall: AI Coach requires paid plan (or coach/admin). Free-trial users
    # see the teaser UI but this endpoint is hard-gated. See pricing_strategy.md §7.
    entitled, block_reason = _check_ai_coach_entitled(email)
    if not entitled:
        if block_reason == "UPGRADE_REQUIRED":
            return _paywall_response()
        return jsonify({"ok": False, "error": block_reason or "not_entitled"}), 403

    # Derive cache key
    if prompt_key == "freeform":
        cache_key = freeform_key(freeform)
    else:
        cache_key = prompt_key

    # Rate limit (cards excluded; this endpoint does not serve cards)
    allowed, reason, resets_at = check_rate_limit(email, task_id)
    if not allowed:
        return jsonify({
            "ok":       False,
            "error":    reason,
            "resets_at": resets_at,
        }), 429

    # Cache check
    if not force:
        cached = cache_get(task_id, email, cache_key)
        if cached:
            return jsonify({
                "ok":           True,
                "response":     cached["response"],
                "data_snapshot": cached["data_snapshot"],
                "tokens_used":  cached["tokens_used"],
                "cached":       True,
            })

    # Fetch match data
    try:
        match_data = _fetch_data_for_task(task_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except Exception:
        log.exception("[coach_api] data fetch failed task_id=%s", task_id)
        return jsonify({"ok": False, "error": "failed to fetch match data"}), 500

    # Build prompt
    if prompt_key == "freeform":
        messages, system = build_freeform_prompt(match_data, freeform)
    else:
        messages, system = _NAMED_PROMPTS[prompt_key](match_data)

    # Call Claude
    result = call_claude(messages, system)
    if not result.get("ok"):
        return jsonify({
            "ok":    False,
            "error": result.get("error", "claude_call_failed"),
            "detail": result.get("detail"),
        }), 502

    tokens = (result.get("input_tokens") or 0) + (result.get("output_tokens") or 0)
    response_text = result["text"]

    # Store in cache
    cache_put(task_id, email, cache_key, response_text, match_data, tokens)

    return jsonify({
        "ok":            True,
        "response":      response_text,
        "data_snapshot": match_data,
        "tokens_used":   tokens,
        "cached":        False,
    })


# ---------------------------------------------------------------------------
# GET /api/client/coach/cards/<task_id>
# ---------------------------------------------------------------------------

@coach_bp.route("/api/client/coach/cards/<task_id>", methods=["GET", "OPTIONS"])
def get_cards(task_id: str):
    """
    Return pre-generated insight cards for this match.

    If cards are already cached: return them immediately.
    If not: generate synchronously, cache, return.
    Cards are not rate-limited (they are pre-generated, not freeform).

    Response: { ok, cards: [{title, body, category}], cached }
    """
    if not _guard():
        return _forbid()

    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400
    if not task_id:
        return jsonify({"ok": False, "error": "task_id required"}), 400

    if not _verify_ownership(task_id, email):
        return jsonify({"ok": False, "error": "not found or access denied"}), 404

    # Paywall: pre-generated insight cards are AI Coach output — same gate.
    entitled, block_reason = _check_ai_coach_entitled(email)
    if not entitled:
        if block_reason == "UPGRADE_REQUIRED":
            return _paywall_response()
        return jsonify({"ok": False, "error": block_reason or "not_entitled"}), 403

    force = request.args.get("force", "").lower() in ("1", "true", "yes")

    # Cache check
    if not force:
        cached = cache_get(task_id, email, _CARDS_PROMPT_KEY)
        if cached:
            cards = _parse_cards(cached["response"])
            return jsonify({"ok": True, "cards": cards, "cached": True})

    # Generate synchronously
    try:
        match_data = _fetch_data_for_task(task_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except Exception:
        log.exception("[coach_api] data fetch failed for cards task_id=%s", task_id)
        return jsonify({"ok": False, "error": "failed to fetch match data"}), 500

    messages, system = build_cards_prompt(match_data)
    result = call_claude(messages, system, max_tokens=800)
    if not result.get("ok"):
        return jsonify({
            "ok":    False,
            "error": result.get("error", "claude_call_failed"),
            "detail": result.get("detail"),
        }), 502

    response_text = result["text"]
    tokens = (result.get("input_tokens") or 0) + (result.get("output_tokens") or 0)
    cards = _parse_cards(response_text)

    # Cache (always store raw response — parse again on retrieval)
    cache_put(task_id, email, _CARDS_PROMPT_KEY, response_text, match_data, tokens)

    return jsonify({"ok": True, "cards": cards, "cached": False})


# ---------------------------------------------------------------------------
# GET /api/client/coach/status/<task_id>
# ---------------------------------------------------------------------------

@coach_bp.route("/api/client/coach/status/<task_id>", methods=["GET", "OPTIONS"])
def get_status(task_id: str):
    """
    Lightweight poll endpoint. Returns {ready: true} if cards are cached.
    Cards are generated synchronously in /cards so this is rarely needed,
    but it's here for frontend polling compatibility.
    """
    if not _guard():
        return _forbid()

    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    if not _verify_ownership(task_id, email):
        return jsonify({"ok": False, "error": "not found or access denied"}), 404

    cached = cache_get(task_id, email, _CARDS_PROMPT_KEY)
    return jsonify({"ok": True, "ready": cached is not None})


# ---------------------------------------------------------------------------
# GET /api/client/coach/debug/<task_id> — admin-only data payload inspection
# ---------------------------------------------------------------------------

@coach_bp.route("/api/client/coach/debug/<task_id>", methods=["GET", "OPTIONS"])
def debug_payload(task_id: str):
    """
    Return the raw data payload that would be sent to Claude.
    Admin-only: email must be in ADMIN_EMAILS whitelist.
    Useful for validating data quality before trusting coach output.
    """
    if not _guard():
        return _forbid()

    email = _norm_email(request.args.get("email"))
    if email not in _ADMIN_EMAILS:
        return _forbid()

    try:
        match_data = _fetch_data_for_task(task_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except Exception:
        log.exception("[coach_api] debug fetch failed task_id=%s", task_id)
        return jsonify({"ok": False, "error": "failed to fetch match data"}), 500

    return jsonify({"ok": True, "data": match_data})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_cards(response_text: str) -> list:
    """
    Parse Claude's JSON card response.
    Returns a list of card dicts or a single error card if parsing fails.
    """
    text = response_text.strip()
    # Strip markdown fences if Claude added them despite instructions
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(
            ln for ln in lines
            if not ln.strip().startswith("```")
        ).strip()

    try:
        cards = json.loads(text)
        if isinstance(cards, list):
            # Validate shape — keep only well-formed cards
            valid = []
            for c in cards:
                if isinstance(c, dict) and "title" in c and "body" in c:
                    valid.append({
                        "title":    str(c.get("title", "")),
                        "body":     str(c.get("body", "")),
                        "category": str(c.get("category", "general")),
                    })
            return valid
    except (json.JSONDecodeError, TypeError):
        log.warning("[coach_api] failed to parse cards JSON: %r", text[:200])

    # Fallback: return the raw text as a single card so user sees something
    return [{"title": "Coach Insight", "body": response_text.strip()[:300], "category": "general"}]
