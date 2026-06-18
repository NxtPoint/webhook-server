# marketing_crm/klaviyo/flow_builder.py — build (and optionally create) the two Klaviyo flows
# from marketing_crm/klaviyo/flow_build_spec.md via Klaviyo's BETA Create Flow API.
#
# Why this is code (Claude Code's lane): the Klaviyo MCP connector can't create flows; the REST
# API can (POST /api/flows/, beta — revision header "2024-10-15.pre"). Flows are created in DRAFT.
#
# Two modes:
#   dry_run=True  (default) → build + RETURN the exact JSON payloads. No key needed, nothing sent.
#                             Use this to eyeball the structure (esp. the flows-profile-metric
#                             conditions) before sending anything live.
#   dry_run=False           → POST each payload to Klaviyo (needs KLAVIYO_API_KEY; runs in Render).
#
# ⚠️ TWO THINGS TO CONFIRM before a live (dry_run=False) build:
#   1. SUBJECTS below are PLACEHOLDERS — Cowork must supply the real subject line per template.
#   2. The flows-profile-metric condition shape is best-effort (the beta schema is under-documented).
#      The dry_run payload surfaces it so Cowork can confirm field names; expect to iterate once.

from __future__ import annotations

import os

_BASE = "https://a.klaviyo.com/api/flows/"
_REVISION = "2024-10-15.pre"   # beta Create Flow API

FROM_EMAIL = "info@ten-fifty5.com"
FROM_LABEL = "Ten-Fifty5"

# ── Reference IDs (from flow_build_spec.md — live in the Klaviyo account) ─────
M_ACCOUNT_CREATED      = "UvEhHt"
M_REPORT_VIEWED        = "RRcmqL"
M_MATCH_UPLOADED       = "SxXTwc"
M_SUBSCRIPTION_STARTED = "THnJq4"
M_CREDIT_PURCHASED     = "S6dmAm"
M_COACH_ACCEPTED       = "Wi6bdW"

# Email templates (Trial→Paid)
T_WELCOME       = "U4uKSv"  # 1.1 Welcome — first match free
T_FRICTION      = "W9qEC3"  # 1.2 Friction-buster — any camera
T_PROOF         = "S9qBS7"  # 1.3 Proof — what one match tells you
T_GAP           = "TwGdDC"  # 2.1 The gap — one match vs a trend
T_AICOACH       = "SDMpYP"  # 2.2 AI Coach tease
T_LONGGAME      = "VEenA3"  # 2.3 The long game — progression
T_LASTCALL      = "QWA2S6"  # 2.4 Last call — PAYG $25
# Email templates (Coach Engagement)
T_COACH_ORIENT  = "WuiVMV"  # C0 How it works (orientation)
T_COACH_CONNECT = "SW5qXQ"  # C1 A player connected
T_COACH_VIEWS   = "RTU6Cf"  # C2 Three views
T_COACH_AI      = "SEaDM9"  # C3 AI coach
T_COACH_UPSELL  = "TfaGff"  # Coach Pro upsell (2nd player)

# Final copy from Cowork (flow_build_spec.md §Subject lines). Brand voice: NO emoji.
SUBJECTS = {
    T_WELCOME:       "Your first match analysis is on us",
    T_FRICTION:      "Still sitting on your first match?",
    T_PROOF:         "What one match actually tells you",
    T_GAP:           "You've seen one match. Here's what you're not seeing yet.",
    T_AICOACH:       "Ask your data why you lose the second set",
    T_LONGGAME:      "Every match teaches you something. Don't let it fade.",
    T_LASTCALL:      "One more match? It's $25 — and your credits never expire.",
    T_COACH_ORIENT:  "How Ten-Fifty5 works for coaches",
    T_COACH_CONNECT: "A player just shared their game with you",
    T_COACH_VIEWS:   "The 3 views that change how you coach",
    T_COACH_AI:      "Ask the data about any player",
    T_COACH_UPSELL:  "A second player connected — time to go unlimited",
}
PREVIEW = {
    T_WELCOME:       "One upload. 450+ data points. No card needed.",
    T_FRICTION:      "Any camera works. One MP4 is all we need.",
    T_PROOF:         "The stat that ended a two-year losing streak.",
    T_GAP:           "One match is a snapshot. Your game is a trend.",
    T_AICOACH:       "A tour coach, trained on your matches.",
    T_LONGGAME:      "Your progression chart compounds. Your memory doesn't.",
    T_LASTCALL:      "Not ready to subscribe? Pay as you go.",
    T_COACH_ORIENT:  "When a player shares a match, it lands here.",
    T_COACH_CONNECT: "Their dashboard is live on your roster.",
    T_COACH_VIEWS:   "Serve zones, rally drop-off, technique scores.",
    T_COACH_AI:      "A tour coach's read, grounded in their matches.",
    T_COACH_UPSELL:  "Coach Pro: every player who shares with you, one price.",
}


# ── action + condition builders ──────────────────────────────────────────────

def _email(tid, nxt):
    return {
        "temporary_id": f"email_{tid}",
        "type": "send-email",
        "links": {"next": nxt},
        "data": {
            "message": {
                "from_email": FROM_EMAIL,
                "from_label": FROM_LABEL,
                "subject_line": SUBJECTS.get(tid, ""),
                "preview_text": PREVIEW.get(tid, ""),
                "template_id": tid,
                "smart_sending_enabled": True,
                "transactional": False,
            },
            "status": "draft",
        },
    }


def _delay(tid, days, nxt):
    return {
        "temporary_id": tid,
        "type": "time-delay",
        "links": {"next": nxt},
        "data": {"unit": "days", "value": days, "timezone": "profile"},
    }


def _metric_condition(metric_id, *, op="equals", value=0, timeframe="flow-start"):
    """`profile-metric` condition — VERBATIM shape read back from a real Klaviyo flow (2026-06-18,
    flow_build_spec.md §CONFIRMED LITERAL): profile has done METRIC `op` `value` within `timeframe`.
    `timeframe='flow-start'` (since starting this flow) is confirmed. NOTE: the all-time variant
    (for Coach-Pro ≥2) was NOT read back — confirm that literal before relying on it."""
    return {
        "type": "profile-metric",
        "metric_id": metric_id,
        "measurement": "count",
        "measurement_filter": {"type": "numeric", "operator": op, "value": value},
        "timeframe_filter": {"type": "date", "operator": timeframe},
        "metric_filters": None,
    }


def _filter(*condition_groups):
    """Wrap condition groups. Groups are OR'd; conditions within a group are AND'd."""
    return {"condition_groups": list(condition_groups)}


def _group(*conditions):
    return {"conditions": list(conditions)}


def _consent_condition():
    """Email marketing opt-in condition — VERBATIM from Klaviyo docs (Cowork-confirmed)."""
    return {
        "type": "profile-marketing-consent",
        "consent": {
            "channel": "email",
            "can_receive_marketing": True,
            "consent_status": {"subscription": "subscribed", "filters": None},
        },
    }


def _consent_gate():
    """Flow-level filter: email opt-in only (Coach flows). Trial flows AND a metric exit condition."""
    return _filter(_group(_consent_condition()))


def _split(tid, profile_filter, *, if_true, if_false):
    return {
        "temporary_id": tid,
        "type": "conditional-split",
        "links": {"next_if_true": if_true, "next_if_false": if_false},
        "data": {"profile_filter": profile_filter},
    }


# ── Flow 1: Trial · Welcome & Activation ─────────────────────────────────────

def build_flow_welcome():
    """account_created → 3-email Welcome/Activation sequence.
    V1: linear + opt-in gate. V2 (after the canonical flows-profile-metric literal is read back):
    add the exit-on-`match_uploaded`-since-flow-start filter + conditional splits."""
    actions = [
        _email(T_WELCOME, "delay1"),
        _delay("delay1", 1, f"email_{T_FRICTION}"),
        _email(T_FRICTION, "delay2"),
        _delay("delay2", 2, f"email_{T_PROOF}"),
        _email(T_PROOF, None),
    ]
    return {
        "data": {
            "type": "flow",
            "attributes": {
                "name": "Trial · Welcome & Activation",
                "definition": {
                    "triggers": [{"type": "metric", "id": M_ACCOUNT_CREATED}],
                    # opt-in AND exit-on-upload: Klaviyo re-checks the flow filter before each step,
                    # so the profile auto-exits the moment match_uploaded (since flow start) goes > 0.
                    "profile_filter": _filter(_group(
                        _consent_condition(),
                        _metric_condition(M_MATCH_UPLOADED, op="equals", value=0))),
                    "entry_action_id": f"email_{T_WELCOME}",
                    "actions": actions,
                },
            },
        }
    }


# ── Flow 2: Trial → Paid Conversion ──────────────────────────────────────────

def build_flow_conversion():
    """report_viewed → 4-email conversion sequence.
    V1: linear + opt-in gate. V2: add the exit-on-(`subscription_started` OR `credit_purchased`)
    filter + per-email conditional splits once the canonical metric literal is confirmed."""
    actions = [
        _delay("delay1", 1, f"email_{T_GAP}"),
        _email(T_GAP, "delay2"),
        _delay("delay2", 2, f"email_{T_AICOACH}"),
        _email(T_AICOACH, "delay3"),
        _delay("delay3", 3, f"email_{T_LONGGAME}"),
        _email(T_LONGGAME, "delay4"),
        _delay("delay4", 4, f"email_{T_LASTCALL}"),
        _email(T_LASTCALL, None),
    ]
    return {
        "data": {
            "type": "flow",
            "attributes": {
                "name": "Trial → Paid Conversion",
                "definition": {
                    "triggers": [{"type": "metric", "id": M_REPORT_VIEWED}],
                    # opt-in AND exit-on-convert: auto-exits when subscription_started OR
                    # credit_purchased (since flow start) goes > 0 (both must stay 0 to remain).
                    "profile_filter": _filter(_group(
                        _consent_condition(),
                        _metric_condition(M_SUBSCRIPTION_STARTED, op="equals", value=0),
                        _metric_condition(M_CREDIT_PURCHASED, op="equals", value=0))),
                    "entry_action_id": "delay1",
                    "actions": actions,
                },
            },
        }
    }


# ── Flow 3: Coach · Engagement ───────────────────────────────────────────────

def build_flow_coach_engagement():
    """coach_accepted (a player granted access) → orient the coach over the first week.
    (Trigger A / C0 orientation on account_created+role=coach is deferred — account_created
    doesn't carry a role property yet; spec says rely on Trigger B until it does.)"""
    actions = [
        _email(T_COACH_CONNECT, "delay1"),       # C1 immediately
        _delay("delay1", 2, f"email_{T_COACH_VIEWS}"),
        _email(T_COACH_VIEWS, "delay2"),          # C2 after 2 days
        _delay("delay2", 4, f"email_{T_COACH_AI}"),
        _email(T_COACH_AI, None),                 # C3 after a further 4 days
    ]
    return {
        "data": {
            "type": "flow",
            "attributes": {
                "name": "Coach · Engagement",
                "definition": {
                    "triggers": [{"type": "metric", "id": M_COACH_ACCEPTED}],
                    "profile_filter": _consent_gate(),
                    "entry_action_id": f"email_{T_COACH_CONNECT}",
                    "actions": actions,
                },
            },
        }
    }


# ── Flow 4: Coach Pro upsell ─────────────────────────────────────────────────

def build_flow_coach_pro_upsell():
    """Coach Pro upsell on a 2nd player connecting.
    V1: opt-in gate only (drops the unconfirmed `coach_accepted` >= 2 all-time entry filter).
    ⚠️ V2 MUST add that >= 2 filter before go-live — without it this would target a coach on their
    FIRST connected player too. Safe in draft (sends nothing)."""
    actions = [
        _delay("delay1", 1, f"email_{T_COACH_UPSELL}"),
        _email(T_COACH_UPSELL, None),
    ]
    return {
        "data": {
            "type": "flow",
            "attributes": {
                "name": "Coach Pro upsell",
                "definition": {
                    "triggers": [{"type": "metric", "id": M_COACH_ACCEPTED}],
                    "profile_filter": _consent_gate(),
                    "entry_action_id": "delay1",
                    "actions": actions,
                },
            },
        }
    }


def read_flow(flow_id: str) -> dict:
    """GET a flow's full definition (beta) so saved condition literals can be read back VERBATIM —
    e.g. the Coach-Pro >=2/all-time condition Cowork built in the UI. Needs KLAVIYO_API_KEY (Render).
    Returns the raw Klaviyo response so we mirror the exact operator/timeframe keys, not a guess."""
    key = (os.getenv("KLAVIYO_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("KLAVIYO_API_KEY not set in this environment")
    import requests
    headers = {"Authorization": f"Klaviyo-API-Key {key}", "revision": _REVISION,
               "accept": "application/vnd.api+json"}
    r = requests.get(f"{_BASE}{flow_id}/", headers=headers,
                     params={"additional-fields[flow]": "definition"}, timeout=20)
    return {"status": r.status_code, "body": r.json() if r.content else {}}


def build_all():
    return [build_flow_welcome(), build_flow_conversion(),
            build_flow_coach_engagement(), build_flow_coach_pro_upsell()]


def create_flows(dry_run: bool = True, delete_ids=None) -> dict:
    """dry_run=True → return the payloads (no key, nothing sent). dry_run=False → (optionally DELETE
    delete_ids first, to replace prior drafts cleanly) then POST each to Klaviyo's beta Create Flow
    API in draft and return ids/errors."""
    payloads = build_all()
    if dry_run:
        return {"payloads": payloads,
                "note": "DRY RUN — nothing sent. v2: final subjects + confirmed profile-metric exit "
                        "filters (Flow1 exit-on-upload, Flow2 exit-on-convert). Coach Pro >=2 pending."}
    key = (os.getenv("KLAVIYO_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("KLAVIYO_API_KEY not set in this environment")
    import time

    import requests
    headers = {
        "Authorization": f"Klaviyo-API-Key {key}",
        "revision": _REVISION,
        "accept": "application/vnd.api+json",
        "content-type": "application/vnd.api+json",
    }
    deleted = []
    for fid in (delete_ids or []):
        try:
            dr = requests.delete(f"{_BASE}{fid}/", headers=headers, timeout=20)
            deleted.append({"id": fid, "status": dr.status_code,
                            "ok": dr.status_code in (200, 204, 404)})
        except Exception as ex:
            deleted.append({"id": fid, "ok": False, "errors": str(ex)})
        time.sleep(2)
    results = []
    for i, p in enumerate(payloads):
        name = p["data"]["attributes"]["name"]
        if i:
            time.sleep(4)  # flow-create is heavily rate-limited — space the POSTs out
        try:
            r = None
            for attempt in range(3):  # retry on 429 (Klaviyo: "available in 1 second")
                r = requests.post(_BASE, headers=headers, json=p, timeout=20)
                if r.status_code != 429:
                    break
                time.sleep(2 + attempt * 2)
            ok = r.status_code < 300
            body = r.json() if r.content else {}
            results.append({
                "name": name, "status": r.status_code, "ok": ok,
                "flow_id": (body.get("data") or {}).get("id") if ok else None,
                "errors": None if ok else body.get("errors", r.text[:500]),
            })
        except Exception as ex:
            results.append({"name": name, "ok": False, "errors": str(ex)})
    return {"deleted": deleted, "results": results}
