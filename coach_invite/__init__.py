# coach_invite
# ============================================================
# Package init for the coach invite and accept flow.
#
# On import, calls ensure_invite_token_column() from db.py to perform
# idempotent schema setup (creates billing.coaches_permission table and
# ensures the invite_token column exists). This runs at service startup
# so no separate migration step is required.
#
# Exports:
#   accept_bp        — Flask blueprint (GET /coach-accept,
#                      POST /api/coaches/accept-token)
#   send_invite_email — sends branded coach invite email via AWS SES
# ============================================================

from coach_invite.db import ensure_invite_token_column

ensure_invite_token_column()

from coach_invite.accept_page import accept_bp  # noqa: E402
from coach_invite.email_sender import send_invite_email  # noqa: E402

__all__ = ["accept_bp", "send_invite_email"]
