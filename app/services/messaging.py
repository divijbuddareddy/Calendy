"""Messaging facade (email + SMS) - BASELINE; the integrations module may extend providers.

CONTRACT:
    send_email(db, to, subject, html, attachments=None, job_id=None) -> SentMessage
        attachments: [{"filename": "invite.ics", "content": "<text>", "mime": "text/calendar"}]
    send_sms(db, to, body, job_id=None) -> SentMessage

Every message is logged in `sent_messages` (also browsable at /dev/inbox in dev). If SMTP is
configured, email is also delivered via SMTP. Raises MessagingError on provider failure.
Caller owns the transaction (adds + flushes only).
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Optional

from sqlalchemy.orm import Session

from app.config import IS_TEST, settings
from app.models import SentMessage


class MessagingError(Exception):
    pass


def send_email(
    db: Session, to: str, subject: str, html: str, attachments: Optional[list] = None, job_id: Optional[str] = None
) -> SentMessage:
    provider = "devinbox"
    if settings.smtp_host and not IS_TEST:
        msg = EmailMessage()
        msg["From"] = settings.email_from
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content("This message requires an HTML-capable email client.")
        msg.add_alternative(html, subtype="html")
        for a in attachments or []:
            mime = a.get("mime", "text/calendar").split("/")
            msg.add_attachment(a["content"].encode(), maintype=mime[0], subtype=mime[1], filename=a["filename"])
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as s:
                s.starttls()
                if settings.smtp_user:
                    s.login(settings.smtp_user, settings.smtp_password)
                s.send_message(msg)
            provider = "smtp"
        except Exception as exc:  # surfaced to job retry logic
            raise MessagingError(str(exc)) from exc
    row = SentMessage(
        channel="email", recipient=to, subject=subject, body=html, attachments_json=attachments or [],
        provider=provider, job_id=job_id,
    )
    db.add(row)
    db.flush()
    row.provider_id = row.id
    return row


def send_sms(db: Session, to: str, body: str, job_id: Optional[str] = None) -> SentMessage:
    provider = "devinbox"
    if settings.twilio_account_sid and not IS_TEST:
        import httpx

        try:
            r = httpx.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Messages.json",
                auth=(settings.twilio_account_sid, settings.twilio_auth_token),
                data={"From": settings.twilio_from, "To": to, "Body": body},
                timeout=20,
            )
            r.raise_for_status()
            provider = "twilio"
        except Exception as exc:
            raise MessagingError(str(exc)) from exc
    row = SentMessage(channel="sms", recipient=to, subject=None, body=body, provider=provider, job_id=job_id)
    db.add(row)
    db.flush()
    row.provider_id = row.id
    return row
