# core_db.repositories — clean data-access functions over the core.* schema.
#
# Every function takes an explicit SQLAlchemy `session` (from core_db.db.session_scope)
# and does NOT commit — callers own the transaction boundary.

from core_db.repositories import accounts, subscriptions, matches, feedback, consent  # noqa: F401

__all__ = ["accounts", "subscriptions", "matches", "feedback", "consent"]
