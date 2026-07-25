"""Outbound email for auth flows: verification and password-reset links.

`EmailSender` is a narrow Protocol so a real provider (SES, Postgres
LISTEN/NOTIFY relay, SendGrid, ...) is a drop-in replacement for
`LoggingEmailSender` without touching `app.services.auth_service`.
Sending is explicitly stubbed per spec ("password reset email sending may
be stubbed") — token generation and storage, which live in
`auth_service`, are not.
"""

import logging
from typing import Protocol

from app.core.config import Settings, get_settings

logger = logging.getLogger("sentinelx.email")


class EmailSender(Protocol):
    async def send(self, to: str, subject: str, body: str) -> None: ...


class LoggingEmailSender:
    """Logs instead of sending. Must be replaced with a real provider
    before production use.

    The event (recipient + subject) is logged at INFO; the body — which
    embeds the verification/reset link, a bearer secret — is logged at
    DEBUG only, so it doesn't appear in a default-configured (INFO)
    production log stream even before a real sender is wired in.
    """

    async def send(self, to: str, subject: str, body: str) -> None:
        logger.info("email.stub_send", extra={"to": to, "subject": subject})
        logger.debug("email.stub_body", extra={"to": to, "body": body})


class EmailService:
    def __init__(self, sender: EmailSender, settings: Settings) -> None:
        self._sender = sender
        self._settings = settings

    async def send_verification_email(self, to: str, token: str) -> None:
        link = f"{self._settings.frontend_base_url}/verify-email?token={token}"
        await self._sender.send(
            to=to,
            subject="Verify your SentinelX email address",
            body=f"Verify your email address by visiting: {link}",
        )

    async def send_password_reset_email(self, to: str, token: str) -> None:
        link = f"{self._settings.frontend_base_url}/reset-password?token={token}"
        await self._sender.send(
            to=to,
            subject="Reset your SentinelX password",
            body=f"Reset your password by visiting: {link}",
        )


def get_email_service() -> EmailService:
    return EmailService(sender=LoggingEmailSender(), settings=get_settings())
