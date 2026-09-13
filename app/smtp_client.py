from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import default

from app.config import Settings


class SMTPDeliveryError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.ambiguous = ambiguous


@dataclass(frozen=True, slots=True)
class SMTPResult:
    refused_recipients: list[str]


class SMTPClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def check_connectivity(self) -> None:
        try:
            with smtplib.SMTP(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=self.settings.mail_timeout_seconds,
            ) as client:
                client.ehlo()
                client.starttls(context=self.settings.tls_context())
                client.ehlo()
                client.login(self.settings.gmx_email, self.settings.gmx_app_password)
        except (smtplib.SMTPException, OSError) as exc:
            raise SMTPDeliveryError("SMTP connectivity check failed", retryable=True) from exc

    def send(self, message: EmailMessage, recipients: list[str]) -> SMTPResult:
        outbound = EmailMessage(policy=default)
        for key, value in message.items():
            if key.casefold() != "bcc":
                outbound[key] = value
        if message.is_multipart():
            outbound.set_payload(message.get_payload())
        else:
            outbound.set_content(message.get_content())
        phase = "connect"
        try:
            with smtplib.SMTP(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=self.settings.mail_timeout_seconds,
            ) as client:
                client.ehlo()
                phase = "starttls"
                client.starttls(context=self.settings.tls_context())
                client.ehlo()
                phase = "authenticate"
                client.login(self.settings.gmx_email, self.settings.gmx_app_password)
                phase = "data"
                refused = client.send_message(
                    outbound, from_addr=self.settings.gmx_email, to_addrs=recipients
                )
                phase = "accepted"
            return SMTPResult(refused_recipients=sorted(refused))
        except smtplib.SMTPRecipientsRefused as exc:
            raise SMTPDeliveryError("All recipients were refused", retryable=True) from exc
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPDataError) as exc:
            raise SMTPDeliveryError("SMTP rejected the message", retryable=True) from exc
        except (smtplib.SMTPServerDisconnected, TimeoutError, OSError) as exc:
            raise SMTPDeliveryError(
                "SMTP connection failed",
                retryable=phase != "accepted",
                ambiguous=phase == "data",
            ) from exc
        except smtplib.SMTPException as exc:
            raise SMTPDeliveryError("SMTP operation failed", retryable=True) from exc
