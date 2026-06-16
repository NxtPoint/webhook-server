# core_db/db.py — session + serialization helpers for the core data-access layer.
#
# Repository functions take an explicit `session` (they never commit) so callers
# compose transactions via session_scope(). This keeps multi-step writes atomic
# (e.g. create account + owner user + primary person in one commit).

from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import inspect
from sqlalchemy.orm import Session


def get_engine():
    """The shared app engine (db_init.engine). Imported lazily so importing this
    module never forces a DB connection at import time."""
    from db_init import engine
    return engine


@contextmanager
def session_scope():
    """Transactional scope: commits on success, rolls back on error."""
    s = Session(get_engine())
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def as_dict(obj):
    """Serialize an ORM row to a JSON-friendly dict (uuid/datetime/date → str)."""
    if obj is None:
        return None
    out = {}
    for col in inspect(obj).mapper.column_attrs:
        key = col.key
        val = getattr(obj, key)
        if isinstance(val, (UUID,)):
            val = str(val)
        elif isinstance(val, (datetime, date)):
            val = val.isoformat()
        elif isinstance(val, Decimal):
            val = float(val)
        out[key] = val
    return out


def norm_email(email):
    return (email or "").strip().lower() or None
