# coach_invite/video_complete_email.py
# ============================================================
# AWS SES email sender for "Your match analysis is ready" notifications.
# Sends branded HTML with Portal CTA button to the player/account owner
# when their video analysis has completed.
#
# Main function: send_video_complete_email(to_email, first_name, task_id)
#
# Called from: ingest_worker_app.py (step 7) and upload_app.py task-status
# auto-fire. Both callers are guarded by the wix_notified_at idempotency
# check so the email fires at most once per task.
#
# Env vars: SES_FROM_EMAIL, AWS_REGION,
#   LOCKER_ROOM_BASE_URL (default: https://www.tenfifty5.com/locker-room)
#
# Transition note: runs alongside _notify_wix in upload_app.py.
# Remove _notify_wix and WIX_NOTIFY_* env vars once Wix is retired.
# ============================================================

from __future__ import annotations

import logging
import os

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
SES_FROM_EMAIL = os.environ.get("SES_FROM_EMAIL", "noreply@ten-fifty5.com").strip()
LOCKER_ROOM_BASE_URL = os.environ.get("LOCKER_ROOM_BASE_URL", "https://www.ten-fifty5.com/portal").strip()


def _build_html(customer_name: str, player_a: str, player_b: str,
                match_date: str, location: str, locker_room_url: str) -> str:
    """Build branded HTML email for video analysis completion."""
    display_name = customer_name or "there"
    match_desc_parts = []
    if player_a and player_b:
        match_desc_parts.append(f"<strong>{player_a} vs {player_b}</strong>")
    if match_date:
        match_desc_parts.append(match_date)
    if location:
        match_desc_parts.append(location)
    match_desc = " &middot; ".join(match_desc_parts) if match_desc_parts else "your recent match"

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
            <p style="font-size:15px;color:#6b7280;line-height:1.6;margin:0 0 8px;">
              Your match analysis is ready!
            </p>
            <p style="font-size:14px;color:#1a1a1a;line-height:1.6;margin:0 0 24px;">
              {match_desc}
            </p>
            <!-- CTA Button -->
            <table cellpadding="0" cellspacing="0" style="margin:0 auto 24px;">
              <tr>
                <td style="background:#1a5c2e;border-radius:4px;padding:12px 32px;">
                  <a href="{locker_room_url}"
                     style="color:#ffffff;text-decoration:none;font-size:15px;font-weight:600;display:inline-block;">
                    View in Locker Room
                  </a>
                </td>
              </tr>
            </table>
            <p style="font-size:13px;color:#9ca3af;margin:0;line-height:1.5;">
              Your match stats, point-by-point analysis, and trimmed footage are waiting for you.
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


def _build_text(customer_name: str, player_a: str, player_b: str,
                match_date: str, location: str, locker_room_url: str) -> str:
    """Plain-text fallback."""
    display_name = customer_name or "there"
    parts = []
    if player_a and player_b:
        parts.append(f"{player_a} vs {player_b}")
    if match_date:
        parts.append(match_date)
    if location:
        parts.append(location)
    match_desc = " - ".join(parts) if parts else "your recent match"

    return (
        f"Hi {display_name},\n\n"
        f"Your match analysis is ready!\n\n"
        f"{match_desc}\n\n"
        f"View in Locker Room: {locker_room_url}\n\n"
        f"Your match stats, point-by-point analysis, and trimmed footage are waiting for you.\n"
    )


def send_completion_email(task_id: str, customer_email: str, customer_name: str,
                          player_a: str = "", player_b: str = "",
                          match_date: str = "", location: str = "") -> dict:
    """Send video analysis complete email via SES. Returns {ok, message_id} or {ok, error}."""
    if not customer_email:
        return {"ok": False, "error": "no_customer_email"}

    locker_room_url = LOCKER_ROOM_BASE_URL

    try:
        ses = boto3.client("ses", region_name=AWS_REGION)
        resp = ses.send_email(
            Source=SES_FROM_EMAIL,
            Destination={"ToAddresses": [customer_email]},
            Message={
                "Subject": {
                    "Data": "Your match analysis is ready - TEN-FIFTY5",
                    "Charset": "UTF-8",
                },
                "Body": {
                    "Html": {
                        "Data": _build_html(customer_name, player_a, player_b,
                                            match_date, location, locker_room_url),
                        "Charset": "UTF-8",
                    },
                    "Text": {
                        "Data": _build_text(customer_name, player_a, player_b,
                                            match_date, location, locker_room_url),
                        "Charset": "UTF-8",
                    },
                },
            },
        )
        message_id = resp.get("MessageId", "")
        log.info("Video complete email sent to %s for task_id=%s (MessageId=%s)",
                 customer_email, task_id, message_id)
        return {"ok": True, "message_id": message_id}

    except ClientError as e:
        err = e.response["Error"]["Message"]
        log.error("SES send failed for %s task_id=%s: %s", customer_email, task_id, err)
        return {"ok": False, "error": err}
    except Exception as e:
        log.error("Email send error for %s task_id=%s: %s", customer_email, task_id, e)
        return {"ok": False, "error": str(e)}
