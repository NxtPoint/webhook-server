# coach_invite/email_sender.py — Send coach invite email via AWS SES

from __future__ import annotations

import logging
import os

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
SES_FROM_EMAIL = os.environ.get("SES_FROM_EMAIL", "noreply@ten-fifty5.com").strip()


def _build_html(coach_name: str, owner_name: str, accept_url: str) -> str:
    """Build branded HTML email with inline CSS."""
    display_name = coach_name or "Coach"
    return f"""\
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:'Inter',Helvetica,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;padding:40px 0;">
    <tr><td align="center">
      <table width="520" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:4px;overflow:hidden;">
        <!-- Header -->
        <tr>
          <td style="background:#1a5c2e;padding:24px 32px;">
            <span style="color:#ffffff;font-size:18px;font-weight:700;letter-spacing:1px;">TEN-FIFTY5</span>
          </td>
        </tr>
        <!-- Body -->
        <tr>
          <td style="padding:32px;">
            <p style="font-size:16px;color:#1a1a1a;margin:0 0 16px;">
              Hi {display_name},
            </p>
            <p style="font-size:15px;color:#6b7280;line-height:1.6;margin:0 0 24px;">
              <strong style="color:#1a1a1a;">{owner_name}</strong> has invited you
              to be their coach on TEN-FIFTY5. You'll be able to view their match
              analysis and footage in read-only mode.
            </p>
            <!-- CTA Button -->
            <table cellpadding="0" cellspacing="0" style="margin:0 auto 24px;">
              <tr>
                <td style="background:#1a5c2e;border-radius:4px;padding:12px 32px;">
                  <a href="{accept_url}"
                     style="color:#ffffff;text-decoration:none;font-size:15px;font-weight:600;display:inline-block;">
                    Accept Invitation
                  </a>
                </td>
              </tr>
            </table>
            <p style="font-size:13px;color:#9ca3af;margin:0;line-height:1.5;">
              If you didn't expect this invitation, you can safely ignore this email.
            </p>
          </td>
        </tr>
        <!-- Footer -->
        <tr>
          <td style="padding:16px 32px;border-top:1px solid #e5e5e5;">
            <p style="font-size:12px;color:#9ca3af;margin:0;">
              &copy; TEN-FIFTY5 &middot; nextpointtennis.com
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _build_text(coach_name: str, owner_name: str, accept_url: str) -> str:
    """Plain-text fallback."""
    display_name = coach_name or "Coach"
    return (
        f"Hi {display_name},\n\n"
        f"{owner_name} has invited you to be their coach on TEN-FIFTY5.\n"
        f"You'll be able to view their match analysis and footage in read-only mode.\n\n"
        f"Accept the invitation: {accept_url}\n\n"
        f"If you didn't expect this invitation, you can safely ignore this email.\n"
    )


def send_invite_email(
    coach_email: str,
    coach_name: str,
    owner_name: str,
    accept_url: str,
) -> dict:
    """Send invite email via SES. Returns {ok, message_id} or {ok, error}."""
    try:
        ses = boto3.client("ses", region_name=AWS_REGION)
        resp = ses.send_email(
            Source=SES_FROM_EMAIL,
            Destination={"ToAddresses": [coach_email]},
            Message={
                "Subject": {
                    "Data": f"{owner_name} has invited you to coach on TEN-FIFTY5",
                    "Charset": "UTF-8",
                },
                "Body": {
                    "Html": {
                        "Data": _build_html(coach_name, owner_name, accept_url),
                        "Charset": "UTF-8",
                    },
                    "Text": {
                        "Data": _build_text(coach_name, owner_name, accept_url),
                        "Charset": "UTF-8",
                    },
                },
            },
        )
        message_id = resp.get("MessageId", "")
        log.info("Coach invite email sent to %s (MessageId=%s)", coach_email, message_id)
        return {"ok": True, "message_id": message_id}

    except ClientError as e:
        err = e.response["Error"]["Message"]
        log.error("SES send failed for %s: %s", coach_email, err)
        return {"ok": False, "error": err}
    except Exception as e:
        log.error("Email send error for %s: %s", coach_email, e)
        return {"ok": False, "error": str(e)}
