# marketing_crm/consent/blueprint.py — consent capture/withdrawal API.
#
# Auth: X-Client-Key == CLIENT_API_KEY + email (same as the rest of /api/client/*).
# Maps to core.consent types (privacy spec Part B). Recording consent ensures the core identity
# (account/user/person) exists — the forward write-path into core.*. DARK unless CONSENT_ENABLED=1.

import os

from flask import Blueprint, jsonify, request

from core_db.db import session_scope, norm_email
from core_db.repositories import accounts, consent as cons

consent_bp = Blueprint("mc_consent", __name__)
_P = "/api/client/consent"

CONSENT_TYPES = cons.CONSENT_TYPES  # terms_of_service, privacy_policy, marketing_email,
#                                     biometric_processing, minor_processing_parental


def _key_ok():
    # Legacy shared key (X-Client-Key or Bearer == CLIENT_API_KEY) — unchanged.
    expected = os.getenv("CLIENT_API_KEY") or os.getenv("CORE_API_KEY")
    bearer = ""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        bearer = auth[7:].strip()
    supplied = request.headers.get("X-Client-Key") or bearer
    if expected and supplied and supplied == expected:
        return True
    # Per-user IdP token (auth_v2) — accept a verified JWT too. Dark unless
    # AUTH_V2_ENABLED=1 (verify_jwt returns None when disabled), so when off this
    # leaves the legacy result above completely unchanged.
    if bearer:
        try:
            from auth_v2.verifier import is_enabled, verify_jwt
            if is_enabled() and verify_jwt(bearer):
                return True
        except Exception:
            pass
    return False


def _evidence():
    return {"ip": request.headers.get("X-Forwarded-For", request.remote_addr),
            "user_agent": request.headers.get("User-Agent", "")[:300]}


@consent_bp.route(f"{_P}/record", methods=["POST", "OPTIONS"])
def record():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _key_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    email = norm_email(body.get("email"))
    ctype = (body.get("consent_type") or "").strip()
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400
    if ctype not in CONSENT_TYPES:
        return jsonify({"ok": False, "error": f"unknown consent_type (allowed: {sorted(CONSENT_TYPES)})"}), 400

    policy_version = body.get("policy_version")  # set once the lawyer signs off
    full_name = body.get("full_name")
    with session_scope() as s:
        acct, owner, primary = accounts.ensure_identity(s, email=email, full_name=full_name)

        # subject: self by default; for a minor's parental consent, the subject is the junior
        subject_person_id = primary.id
        if ctype == "minor_processing_parental":
            jr_name = (body.get("subject_name") or "").strip()
            jr_dob = body.get("subject_dob")  # ISO date string or None
            if not jr_name:
                return jsonify({"ok": False, "error": "subject_name required for parental consent"}), 400
            junior = accounts.create_person(s, account_id=acct.id, full_name=jr_name,
                                            role="player", dob=_parse_date(jr_dob))
            subject_person_id = junior.id
        elif body.get("subject_person_public_id"):
            # consent for an existing person (e.g. biometric for a specific junior)
            from core_db.models import Person
            from sqlalchemy import select
            p = s.execute(select(Person).where(Person.public_id == body["subject_person_public_id"])).scalar_one_or_none()
            if p:
                subject_person_id = p.id

        cons.record_consent(
            s, subject_person_id=subject_person_id, consent_type=ctype,
            granted_by_user_id=owner.id, status="granted",
            policy_version=policy_version, source=body.get("source") or "portal",
            evidence=_evidence(),
        )
        # marketing consent flips the opt-in flag (the Klaviyo gate). EU double opt-in is handled by
        # sending a confirmation first (Cowork) — pass {"confirmed": true} after the click.
        if ctype == "marketing_email":
            accounts.set_marketing_opt_in(s, owner.id, True)

    _emit_consent_event(ctype, email)
    return jsonify({"ok": True, "consent_type": ctype})


@consent_bp.route(f"{_P}/withdraw", methods=["POST", "OPTIONS"])
def withdraw():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _key_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    email = norm_email(body.get("email"))
    ctype = (body.get("consent_type") or "").strip()
    if not email or ctype not in CONSENT_TYPES:
        return jsonify({"ok": False, "error": "email + valid consent_type required"}), 400
    with session_scope() as s:
        acct = accounts.get_account_by_email(s, email)
        if acct is None:
            return jsonify({"ok": False, "error": "account not found"}), 404
        owner = accounts.get_user_by_email(s, email)
        primary = accounts.get_primary_person(s, acct.id)
        subject_id = primary.id if primary else None
        if subject_id:
            cons.withdraw_consent(s, subject_person_id=subject_id, consent_type=ctype,
                                  granted_by_user_id=(owner.id if owner else None))
        if ctype == "marketing_email" and owner:
            accounts.set_marketing_opt_in(s, owner.id, False)
        # withdrawing biometric → raise an erasure request for the pose data
        if ctype == "biometric_processing" and subject_id:
            cons.open_dsar(s, request_type="erasure", subject_person_id=subject_id,
                           requested_by_user_id=(owner.id if owner else None),
                           notes="biometric_processing consent withdrawn → delete pose data")
    return jsonify({"ok": True, "consent_type": ctype, "status": "withdrawn"})


@consent_bp.route(f"{_P}/state", methods=["GET", "OPTIONS"])
def state():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _key_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    email = norm_email(request.args.get("email"))
    out = {"ok": True, "marketing_opt_in": False, "consents": {}}
    if not email:
        return jsonify(out)
    with session_scope() as s:
        acct = accounts.get_account_by_email(s, email)
        if acct is None:
            return jsonify(out)
        owner = accounts.get_user_by_email(s, email)
        out["marketing_opt_in"] = bool(owner.marketing_opt_in) if owner else False
        primary = accounts.get_primary_person(s, acct.id)
        if primary:
            for ct in CONSENT_TYPES:
                row = cons.latest_consent(s, primary.id, ct)
                out["consents"][ct] = (row.status if row else None)
    return jsonify(out)


@consent_bp.route(f"{_P}/dsar", methods=["POST", "OPTIONS"])
def dsar():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _key_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    email = norm_email(body.get("email"))
    req_type = (body.get("request_type") or "").strip()
    if not email or req_type not in ("access", "erasure", "rectification", "portability"):
        return jsonify({"ok": False, "error": "email + valid request_type required"}), 400
    with session_scope() as s:
        acct = accounts.get_account_by_email(s, email)
        if acct is None:
            return jsonify({"ok": False, "error": "account not found"}), 404
        owner = accounts.get_user_by_email(s, email)
        primary = accounts.get_primary_person(s, acct.id)
        cons.open_dsar(s, request_type=req_type,
                       subject_person_id=(primary.id if primary else None),
                       requested_by_user_id=(owner.id if owner else None),
                       notes=body.get("notes"))
    return jsonify({"ok": True, "request_type": req_type, "status": "received"})


def _parse_date(v):
    if not v:
        return None
    try:
        from datetime import date
        return date.fromisoformat(str(v)[:10])
    except Exception:
        return None


def _emit_consent_event(ctype, email):
    try:
        from marketing_crm.tracking import track
        track("consent_recorded", email=email, properties={"consent_type": ctype})
    except Exception:
        pass


def register(app):
    """Register consent endpoints IFF CONSENT_ENABLED=1. No-op otherwise (dark by default)."""
    if os.getenv("CONSENT_ENABLED", "0") != "1":
        return False
    app.register_blueprint(consent_bp)
    return True
