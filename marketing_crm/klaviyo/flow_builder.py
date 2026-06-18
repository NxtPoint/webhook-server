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

# Email templates (Trial→Paid)
T_WELCOME       = "U4uKSv"  # 1.1 Welcome — first match free
T_FRICTION      = "W9qEC3"  # 1.2 Friction-buster — any camera
T_PROOF         = "S9qBS7"  # 1.3 Proof — what one match tells you
T_GAP           = "TwGdDC"  # 2.1 The gap — one match vs a trend
T_AICOACH       = "SDMpYP"  # 2.2 AI Coach tease
T_LONGGAME      = "VEenA3"  # 2.3 The long game — progression
T_LASTCALL      = "QWA2S6"  # 2.4 Last call — PAYG $25

# ⚠️ PLACEHOLDER subject lines — Cowork to replace with the real copy (the spec maps template IDs
# but not subjects, and Klaviyo's send-email action needs a subject_line per message).
SUBJECTS = {
    T_WELCOME:  "Your first match is on us 🎾",
    T_FRICTION: "Any phone camera works — here's how",
    T_PROOF:    "What one match actually tells you",
    T_GAP:      "One match is a snapshot. A trend is the story.",
    T_AICOACH:  "Meet your AI coach",
    T_LONGGAME: "Where this takes your game",
    T_LASTCALL: "Last call — analyse your next match for $25",
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


def _done_metric_condition(metric_id, *, times, op):
    """A `flows-profile-metric` condition: profile has done METRIC_ID `op` `times` since the flow
    started. op ∈ {'equals' (e.g. 0 → has NOT done), 'greater-than-or-equal' (e.g. 1 → has done)}.
    NOTE: beta schema — confirm field names with Cowork from the dry_run output."""
    return {
        "type": "flows-profile-metric",
        "metric_id": metric_id,
        "measurement": "count",
        "measurement_filter": {"type": "numeric", "operator": op, "value": times},
        "timeframe": {"type": "since-starting-flow"},
    }


def _filter(*condition_groups):
    """Wrap condition groups. Groups are OR'd; conditions within a group are AND'd."""
    return {"condition_groups": list(condition_groups)}


def _group(*conditions):
    return {"conditions": list(conditions)}


def _split(tid, profile_filter, *, if_true, if_false):
    return {
        "temporary_id": tid,
        "type": "conditional-split",
        "links": {"next_if_true": if_true, "next_if_false": if_false},
        "data": {"profile_filter": profile_filter},
    }


# ── Flow 1: Trial · Welcome & Activation ─────────────────────────────────────

def build_flow_welcome():
    """account_created → Welcome, then nudge to upload; exit once they upload a match."""
    uploaded_once = _filter(_group(
        _done_metric_condition(M_MATCH_UPLOADED, times=1, op="greater-than-or-equal")))
    actions = [
        _email(T_WELCOME, "delay1"),
        _delay("delay1", 1, "split1"),
        _split("split1", uploaded_once, if_true=None, if_false=f"email_{T_FRICTION}"),
        _email(T_FRICTION, "delay2"),
        _delay("delay2", 2, "split2"),
        _split("split2", uploaded_once, if_true=None, if_false=f"email_{T_PROOF}"),
        _email(T_PROOF, None),
    ]
    return {
        "data": {
            "type": "flow",
            "attributes": {
                "name": "Trial · Welcome & Activation",
                "definition": {
                    "triggers": [{"type": "metric", "id": M_ACCOUNT_CREATED}],
                    # flow filter: only keep profiles who have NOT uploaded a match (exit on upload)
                    "profile_filter": _filter(_group(
                        _done_metric_condition(M_MATCH_UPLOADED, times=0, op="equals"))),
                    "entry_action_id": f"email_{T_WELCOME}",
                    "actions": actions,
                },
            },
        }
    }


# ── Flow 2: Trial → Paid Conversion ──────────────────────────────────────────

def build_flow_conversion():
    """report_viewed → 4-email conversion sequence; exit the moment they subscribe or buy PAYG."""
    # converted = has done subscription_started OR credit_purchased since flow start (two OR'd groups)
    converted = _filter(
        _group(_done_metric_condition(M_SUBSCRIPTION_STARTED, times=1, op="greater-than-or-equal")),
        _group(_done_metric_condition(M_CREDIT_PURCHASED, times=1, op="greater-than-or-equal")),
    )
    # not-converted flow filter: sub_started zero AND credit_purchased zero (one group, AND'd)
    not_converted = _filter(_group(
        _done_metric_condition(M_SUBSCRIPTION_STARTED, times=0, op="equals"),
        _done_metric_condition(M_CREDIT_PURCHASED, times=0, op="equals"),
    ))
    actions = [
        _delay("delay1", 1, "split1"),
        _split("split1", converted, if_true=None, if_false=f"email_{T_GAP}"),
        _email(T_GAP, "delay2"),
        _delay("delay2", 2, "split2"),
        _split("split2", converted, if_true=None, if_false=f"email_{T_AICOACH}"),
        _email(T_AICOACH, "delay3"),
        _delay("delay3", 3, "split3"),
        _split("split3", converted, if_true=None, if_false=f"email_{T_LONGGAME}"),
        _email(T_LONGGAME, "delay4"),
        _delay("delay4", 4, "split4"),
        _split("split4", converted, if_true=None, if_false=f"email_{T_LASTCALL}"),
        _email(T_LASTCALL, None),
    ]
    return {
        "data": {
            "type": "flow",
            "attributes": {
                "name": "Trial → Paid Conversion",
                "definition": {
                    "triggers": [{"type": "metric", "id": M_REPORT_VIEWED}],
                    "profile_filter": not_converted,
                    "entry_action_id": "delay1",
                    "actions": actions,
                },
            },
        }
    }


def build_all():
    return [build_flow_welcome(), build_flow_conversion()]


def create_flows(dry_run: bool = True) -> dict:
    """dry_run=True → return the payloads (no key, nothing sent). dry_run=False → POST each to
    Klaviyo's beta Create Flow API (draft) and return ids/errors."""
    payloads = build_all()
    if dry_run:
        return {"payloads": payloads,
                "note": "DRY RUN — nothing sent. Subjects are PLACEHOLDERS; "
                        "flows-profile-metric shape is best-effort (confirm with Cowork)."}
    key = (os.getenv("KLAVIYO_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("KLAVIYO_API_KEY not set in this environment")
    import requests
    headers = {
        "Authorization": f"Klaviyo-API-Key {key}",
        "revision": _REVISION,
        "accept": "application/vnd.api+json",
        "content-type": "application/vnd.api+json",
    }
    results = []
    for p in payloads:
        name = p["data"]["attributes"]["name"]
        try:
            r = requests.post(_BASE, headers=headers, json=p, timeout=20)
            ok = r.status_code < 300
            body = r.json() if r.content else {}
            results.append({
                "name": name, "status": r.status_code, "ok": ok,
                "flow_id": (body.get("data") or {}).get("id") if ok else None,
                "errors": None if ok else body.get("errors", r.text[:500]),
            })
        except Exception as ex:
            results.append({"name": name, "ok": False, "errors": str(ex)})
    return {"results": results}
