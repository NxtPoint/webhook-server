# coach_invite — Coach invite email + accept flow (AWS SES, token-based)

from coach_invite.db import ensure_invite_token_column

ensure_invite_token_column()

from coach_invite.accept_page import accept_bp  # noqa: E402
from coach_invite.email_sender import send_invite_email  # noqa: E402

__all__ = ["accept_bp", "send_invite_email"]
