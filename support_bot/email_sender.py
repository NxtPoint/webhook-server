# support_bot/email_sender.py — SES sender for support escalations.
#
# When the user clicks "email us with this conversation" (or the bot itself
# decided needs_human=true), we send a transcript to info@ten-fifty5.com
# with a Reply-To set to the customer's email so the team can respond
# directly.
#
# Mirrors coach_invite/email_sender.py for visual consistency and SES setup.

from __future__ import annotations

import logging
import os
from typing import Optional

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
SES_FROM_EMAIL = os.environ.get("SES_FROM_EMAIL", "noreply@ten-fifty5.com").strip()
SUPPORT_INBOX = "info@ten-fifty5.com"


def _build_html(
    customer_email: str,
    customer_name: str,
    plan: Optional[str],
    role: Optional[str],
    transcript: list[dict],
    user_note: Optional[str],
) -> str:
    """Branded HTML transcript matching the coach-invite email styling."""
    rows_html = []
    for turn in transcript:
        q = (turn.get("question") or "").replace("<", "&lt;").replace(">", "&gt;")
        a = (turn.get("answer") or "").replace("<", "&lt;").replace(">", "&gt;")
        nh = "yes" if turn.get("needs_human") else "no"
        conf = turn.get("confidence") or "?"
        rows_html.append(
            f'<div style="margin:0 0 16px;padding:12px;background:#f9fafb;border-radius:4px;">'
            f'<div style="font-size:13px;color:#6b7280;margin-bottom:6px;">'
            f'Turn {turn.get("turn_idx", "?")} &middot; conf: {conf} &middot; needs_human: {nh}'
            f'</div>'
            f'<div style="font-size:14px;color:#1a1a1a;margin-bottom:6px;">'
            f'<strong>Q:</strong> {q}</div>'
            f'<div style="font-size:14px;color:#374151;">'
            f'<strong>A:</strong> {a}</div>'
            f'</div>'
        )
    transcript_html = "\n".join(rows_html) if rows_html else "<em>(empty transcript)</em>"
    note_html = ""
    if user_note:
        clean = user_note.replace("<", "&lt;").replace(">", "&gt;")
        note_html = (
            f'<p style="font-size:14px;color:#1a1a1a;background:#fef3c7;'
            f'padding:12px;border-radius:4px;margin:0 0 24px;"><strong>'
            f'Customer note:</strong><br>{clean}</p>'
        )
    return f"""\
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:'Inter',Helvetica,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;padding:40px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:4px;overflow:hidden;">
        <tr>
          <td style="background:#1a5c2e;padding:20px 32px;">
            <span style="color:#ffffff;font-size:18px;font-weight:700;letter-spacing:1px;">TEN-FIFTY5 &middot; Support escalation</span>
          </td>
        </tr>
        <tr>
          <td style="padding:24px 32px;">
            <p style="font-size:15px;color:#1a1a1a;margin:0 0 4px;">
              <strong>From:</strong> {customer_name or '(no name)'} &lt;{customer_email}&gt;
            </p>
            <p style="font-size:13px;color:#6b7280;margin:0 0 24px;">
              Plan: {plan or 'unknown'} &middot; Role: {role or 'unknown'}
            </p>
            {note_html}
            <h3 style="font-size:14px;color:#374151;margin:0 0 12px;text-transform:uppercase;letter-spacing:0.5px;">
              Conversation transcript
            </h3>
            {transcript_html}
            <p style="font-size:13px;color:#9ca3af;margin:24px 0 0;">
              Reply directly to this email to respond to the customer.
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _build_text(
    customer_email: str,
    customer_name: str,
    plan: Optional[str],
    role: Optional[str],
    transcript: list[dict],
    user_note: Optional[str],
) -> str:
    lines = [
        f"From: {customer_name or '(no name)'} <{customer_email}>",
        f"Plan: {plan or 'unknown'}  Role: {role or 'unknown'}",
        "",
    ]
    if user_note:
        lines.extend(["Customer note:", user_note, ""])
    lines.append("--- Conversation transcript ---")
    for turn in transcript:
        lines.append(
            f"\n[Turn {turn.get('turn_idx', '?')}] "
            f"(conf={turn.get('confidence', '?')}, needs_human={turn.get('needs_human')})"
        )
        lines.append(f"Q: {turn.get('question', '')}")
        lines.append(f"A: {turn.get('answer', '')}")
    lines.append("\n---\nReply to this email to respond to the customer.")
    return "\n".join(lines)


def send_escalation(
    customer_email: str,
    customer_name: str,
    plan: Optional[str],
    role: Optional[str],
    transcript: list[dict],
    user_note: Optional[str],
) -> dict:
    """Send an escalation transcript to info@ten-fifty5.com. Reply-To = customer."""
    subject_seed = ""
    if transcript:
        subject_seed = (transcript[0].get("question") or "")[:60]
    subject = f"[Support] {subject_seed}" if subject_seed else "[Support] Customer needs help"

    try:
        ses = boto3.client("ses", region_name=AWS_REGION)
        resp = ses.send_email(
            Source=SES_FROM_EMAIL,
            Destination={"ToAddresses": [SUPPORT_INBOX]},
            ReplyToAddresses=[customer_email] if customer_email else [],
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Html": {
                        "Data": _build_html(customer_email, customer_name, plan, role,
                                             transcript, user_note),
                        "Charset": "UTF-8",
                    },
                    "Text": {
                        "Data": _build_text(customer_email, customer_name, plan, role,
                                             transcript, user_note),
                        "Charset": "UTF-8",
                    },
                },
            },
        )
        message_id = resp.get("MessageId", "")
        log.info("[support_bot] escalation sent for %s (MessageId=%s)", customer_email, message_id)
        return {"ok": True, "message_id": message_id}
    except ClientError as e:
        err = e.response["Error"]["Message"]
        log.error("[support_bot] SES escalation failed for %s: %s", customer_email, err)
        return {"ok": False, "error": err}
    except Exception as e:
        log.exception("[support_bot] escalation error for %s", customer_email)
        return {"ok": False, "error": str(e)}
