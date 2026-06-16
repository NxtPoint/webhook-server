# core_db/repositories/accounts.py — accounts, users, persons, relationships.

from datetime import date, datetime, timezone

from sqlalchemy import func, select

from core_db.db import norm_email
from core_db.models import Account, AppUser, Acquisition, Person, Relationship


def _now():
    return datetime.now(timezone.utc)


def _is_minor(dob):
    if not dob:
        return None
    today = date.today()
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    return age < 18


# ---- Account -------------------------------------------------------------

def get_account_by_email(session, email):
    email = norm_email(email)
    if not email:
        return None
    return session.execute(
        select(Account).where(func.lower(Account.email) == email, Account.deleted_at.is_(None))
    ).scalar_one_or_none()


def get_account_by_public_id(session, public_id):
    return session.execute(
        select(Account).where(Account.public_id == public_id)
    ).scalar_one_or_none()


def create_account(session, *, email, display_name=None, currency_code="USD",
                   external_wix_id=None):
    """Create an account (idempotent on email — returns existing if present)."""
    existing = get_account_by_email(session, email)
    if existing:
        return existing
    acct = Account(
        email=norm_email(email),
        display_name=display_name,
        currency_code=currency_code,
        external_wix_id=external_wix_id,
    )
    session.add(acct)
    session.flush()
    return acct


# ---- User ----------------------------------------------------------------

def get_user_by_email(session, email):
    email = norm_email(email)
    if not email:
        return None
    return session.execute(
        select(AppUser).where(func.lower(AppUser.email) == email, AppUser.deleted_at.is_(None))
    ).scalar_one_or_none()


def get_user_by_auth_provider_uid(session, auth_provider, auth_provider_uid):
    """Resolve a login identity by its external IdP id (e.g. Clerk `sub`).
    This is the primary lookup for per-user token auth (auth_v2)."""
    if not auth_provider or not auth_provider_uid:
        return None
    return session.execute(
        select(AppUser).where(
            AppUser.auth_provider == auth_provider,
            AppUser.auth_provider_uid == auth_provider_uid,
            AppUser.deleted_at.is_(None),
        )
    ).scalar_one_or_none()


def set_auth_provider(session, user_id, *, auth_provider, auth_provider_uid, email_verified=None):
    """Bind/refresh a user's external IdP identity. Used when an existing
    (e.g. wix-seeded) user logs in via the new provider for the first time —
    we link by email, then stamp the provider uid so future logins resolve directly."""
    user = session.get(AppUser, user_id)
    if user is None:
        return None
    user.auth_provider = auth_provider
    user.auth_provider_uid = auth_provider_uid
    if email_verified is not None:
        user.email_verified = bool(email_verified)
    user.updated_at = _now()
    session.flush()
    return user


def create_user(session, *, account_id, email, auth_provider="wix", auth_provider_uid=None,
                is_account_owner=False, marketing_opt_in=False, email_verified=False):
    existing = get_user_by_email(session, email)
    if existing:
        return existing
    user = AppUser(
        account_id=account_id,
        email=norm_email(email),
        auth_provider=auth_provider,
        auth_provider_uid=auth_provider_uid,
        is_account_owner=is_account_owner,
        marketing_opt_in=marketing_opt_in,
        email_verified=email_verified,
    )
    session.add(user)
    session.flush()
    return user


def record_acquisition(session, *, user_id, **fields):
    """Upsert the 1:1 acquisition row for a user."""
    acq = session.get(Acquisition, user_id)
    if acq is None:
        acq = Acquisition(user_id=user_id, first_seen_at=_now(), signed_up_at=_now())
        session.add(acq)
    for k, v in fields.items():
        if hasattr(acq, k):
            setattr(acq, k, v)
    session.flush()
    return acq


def touch_login(session, user_id):
    user = session.get(AppUser, user_id)
    if user:
        user.last_login_at = _now()
        session.flush()
    return user


# ---- Person --------------------------------------------------------------

def create_person(session, *, account_id, full_name, role="player", user_id=None,
                  is_primary=False, dob=None, **profile):
    person = Person(
        account_id=account_id,
        user_id=user_id,
        full_name=full_name,
        role=role,
        is_primary=is_primary,
        dob=dob,
        is_minor=_is_minor(dob),
    )
    for k, v in profile.items():
        if hasattr(person, k):
            setattr(person, k, v)
    session.add(person)
    session.flush()
    return person


def update_person(session, person_id, **fields):
    person = session.get(Person, person_id)
    if person is None:
        return None
    for k, v in fields.items():
        if hasattr(person, k):
            setattr(person, k, v)
    if "dob" in fields:
        person.is_minor = _is_minor(person.dob)
    person.updated_at = _now()
    session.flush()
    return person


def get_primary_person(session, account_id):
    return session.execute(
        select(Person).where(Person.account_id == account_id, Person.is_primary.is_(True),
                             Person.deleted_at.is_(None)).order_by(Person.id).limit(1)
    ).scalar_one_or_none()


def set_marketing_opt_in(session, user_id, value):
    user = session.get(AppUser, user_id)
    if user:
        user.marketing_opt_in = bool(value)
        user.updated_at = _now()
        session.flush()
    return user


def ensure_identity(session, *, email, full_name=None, role="player"):
    """Idempotently ensure a core account + owner user + primary person exist for an email.
    This is the forward write-path into core.* (e.g. driven by consent capture / signup).
    Returns (account, owner_user, primary_person)."""
    acct = create_account(session, email=email, display_name=full_name)
    user = create_user(session, account_id=acct.id, email=email, is_account_owner=True)
    person = get_primary_person(session, acct.id)
    if person is None:
        person = create_person(session, account_id=acct.id, full_name=(full_name or email),
                               role=role, user_id=user.id, is_primary=True)
    return acct, user, person


def list_persons_for_account(session, account_id, include_deleted=False):
    q = select(Person).where(Person.account_id == account_id)
    if not include_deleted:
        q = q.where(Person.deleted_at.is_(None))
    return list(session.execute(q.order_by(Person.is_primary.desc(), Person.id)).scalars())


# ---- Relationship (coach↔player, parent↔junior) --------------------------

def link_persons(session, *, from_person_id, to_person_id, type_, status="active",
                 invite_token=None, invited_email=None):
    """Idempotent on (from, to, type): re-linking updates status/token."""
    existing = session.execute(
        select(Relationship).where(
            Relationship.from_person_id == from_person_id,
            Relationship.to_person_id == to_person_id,
            Relationship.type == type_,
        )
    ).scalar_one_or_none()
    if existing:
        existing.status = status
        existing.invite_token = invite_token
        existing.invited_email = invited_email
        existing.updated_at = _now()
        if status == "revoked":
            existing.revoked_at = _now()
        session.flush()
        return existing
    rel = Relationship(
        from_person_id=from_person_id, to_person_id=to_person_id, type=type_,
        status=status, invite_token=invite_token, invited_email=invited_email,
    )
    session.add(rel)
    session.flush()
    return rel


def players_for_coach(session, coach_person_id):
    return list(session.execute(
        select(Person).join(Relationship, Relationship.to_person_id == Person.id).where(
            Relationship.from_person_id == coach_person_id,
            Relationship.type == "coach_player",
            Relationship.status == "active",
        )
    ).scalars())
