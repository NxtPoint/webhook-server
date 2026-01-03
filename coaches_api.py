# ============================================================
# coaches_api.py
#
# PURPOSE
# -------
# Manage coach permissions (invite / accept / revoke)
# View-only access for dashboards.
#
# DESIGN PRINCIPLES
# -----------------
# - Separate from billing (no changes to billing code)
# - Python-only write logic, SQL read-only elsewhere
# - Idempotent operations
# - Minimal surface area (no over-engineering)
#
# TABLE
# -----
# schema: coaching
# table : coaches_permission
#
# ============================================================

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from flask import Blueprint, jsonify, request
from sqlalchemy import (
    Column,
    BigInteger,
    String,
    Boolean,
    DateTime,
    ForeignKey,
    text,
    select,
)
from sqlalchemy.orm import Session, declarative_base

from db_init import engine
from models_billing import Account

# ============================================================
# Constants
# ============================================================

SCHEMA = "coaching"
STATUS_INVITED = "INVITED"
STATUS_ACCEPTED = "ACCEPTED"
STATUS_REVOKED = "REVOKED"

# ============================================================
# SQLAlchemy Base (local to this file)
# ============================================================

Base = declarative_base()

# ============================================================
# Model
# ============================================================

class CoachPermission(Base):
    __tablename__ = "coaches_permission"
    __table_args__ = {"schema": SCHEMA}

    id = Column(BigInteger, primary_key=True)

    owner_account_id = Column(
        BigInteger,
        ForeignKey("billing.account.id", ondelete="CASCADE"),
        nullable=False,
    )

    coach_account_id = Column(
        BigInteger,
        ForeignKey("billing.account.id", ondelete="CASCADE"),
        nullable=True,
    )

    coach_email = Column(String, nullable=False)
    status = Column(String, nullable=False, server_default=text(f"'{STATUS_INVITED}'"))
    active = Column(Boolean, nullable=False, server_default=text("true"))

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

# ============================================================
# Service Logic
# ============================================================

def _now():
    return datetime.now(tz=timezone.utc)


def invite_coach(
    session: Session,
    owner_account_id: int,
    coach_email: str,
) -> CoachPermission:
    coach_email = coach_email.strip().lower()

    stmt = select(CoachPermission).where(
        CoachPermission.owner_account_id == owner_account_id,
        CoachPermission.coach_email == coach_email,
    )

    existing = session.execute(stmt).scalar_one_or_none()
    now = _now()

    if existing:
        existing.status = STATUS_INVITED
        existing.active = True
        existing.updated_at = now
        return existing

    perm = CoachPermission(
        owner_account_id=owner_account_id,
        coach_email=coach_email,
        status=STATUS_INVITED,
        active=True,
        created_at=now,
        updated_at=now,
    )

    session.add(perm)
    return perm


def accept_coach(
    session: Session,
    coach_account_id: int,
    coach_email: str,
) -> CoachPermission:
    coach_email = coach_email.strip().lower()

    stmt = select(CoachPermission).where(
        CoachPermission.coach_email == coach_email,
        CoachPermission.status == STATUS_INVITED,
        CoachPermission.active.is_(True),
    )

    perm = session.execute(stmt).scalar_one_or_none()
    if not perm:
        raise ValueError("invite_not_found")

    perm.status = STATUS_ACCEPTED
    perm.coach_account_id = coach_account_id
    perm.updated_at = _now()

    return perm


def revoke_coach(
    session: Session,
    permission_id: int,
) -> CoachPermission:
    perm = session.get(CoachPermission, permission_id)
    if not perm:
        raise ValueError("permission_not_found")

    perm.status = STATUS_REVOKED
    perm.active = False
    perm.updated_at = _now()

    return perm

# ============================================================
# HTTP API
# ============================================================

bp = Blueprint("coaches", __name__, url_prefix="/api/coaches")


@bp.post("/invite")
def api_invite():
    body = request.json or {}

    owner_email = (body.get("owner_email") or "").strip().lower()
    coach_email = (body.get("coach_email") or "").strip().lower()

    if not owner_email or not coach_email:
        return jsonify(ok=False, error="owner_email_and_coach_email_required"), 400

    with Session(engine) as session:
        owner = session.query(Account).filter_by(email=owner_email).one_or_none()
        if not owner:
            return jsonify(ok=False, error="owner_not_found"), 404

        perm = invite_coach(session, owner.id, coach_email)
        session.commit()

        return jsonify(
            ok=True,
            permission_id=perm.id,
            status=perm.status,
        )


@bp.post("/accept")
def api_accept():
    body = request.json or {}
    coach_email = (body.get("coach_email") or "").strip().lower()

    if not coach_email:
        return jsonify(ok=False, error="coach_email_required"), 400

    with Session(engine) as session:
        coach = session.query(Account).filter_by(email=coach_email).one_or_none()
        if not coach:
            return jsonify(ok=False, error="coach_account_not_found"), 404

        try:
            accept_coach(session, coach.id, coach_email)
            session.commit()
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400

        return jsonify(ok=True)


@bp.post("/revoke")
def api_revoke():
    body = request.json or {}
    permission_id = body.get("permission_id")

    if not permission_id:
        return jsonify(ok=False, error="permission_id_required"), 400

    with Session(engine) as session:
        try:
            revoke_coach(session, int(permission_id))
            session.commit()
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400

        return jsonify(ok=True)
