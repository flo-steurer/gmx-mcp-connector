from __future__ import annotations

import os
import re
import ssl
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    """Raised when startup configuration is missing or unsafe."""


def _secret(name: str) -> str:
    direct = os.getenv(name)
    file_name = os.getenv(f"{name}_FILE")
    if direct and file_name:
        raise ConfigurationError(f"Set only one of {name} and {name}_FILE")
    if file_name:
        try:
            value = Path(file_name).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigurationError(f"Unable to read {name}_FILE") from exc
    else:
        value = (direct or "").strip()
    if not value:
        raise ConfigurationError(f"{name} or {name}_FILE is required")
    return value


def _int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


@dataclass(frozen=True, slots=True)
class Settings:
    gmx_email: str
    gmx_app_password: str
    mcp_api_token: str
    mcp_hostname: str
    mcp_port: int = 8000
    mcp_bind_host: str = "0.0.0.0"
    allow_send: bool = False
    log_level: str = "INFO"
    imap_host: str = "imap.gmx.com"
    imap_port: int = 993
    smtp_host: str = "mail.gmx.com"
    smtp_port: int = 587
    mail_timeout_seconds: int = 30
    max_email_results: int = 100
    max_body_chars: int = 100_000
    max_attachment_bytes: int = 10 * 1024 * 1024
    max_recipients: int = 20
    max_subject_chars: int = 255
    max_draft_body_chars: int = 100_000
    audit_db_path: str = "/data/audit.db"

    @property
    def allowed_hosts(self) -> list[str]:
        host = self.mcp_hostname
        return [host, f"{host}:*", "127.0.0.1:*", "localhost:*", "[::1]:*"]

    @property
    def allowed_origins(self) -> list[str]:
        return [f"https://{self.mcp_hostname}", f"https://{self.mcp_hostname}:*"]

    def tls_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    @classmethod
    def from_env(cls) -> Settings:
        token = _secret("MCP_API_TOKEN")
        if len(token.encode()) < 32 or token.lower() in {
            "change-me",
            "changeme",
            "replace-me",
        }:
            raise ConfigurationError("MCP_API_TOKEN must be a strong value of at least 32 bytes")
        hostname = os.getenv("MCP_HOSTNAME", "").strip().lower().rstrip(".")
        if not hostname or not re.fullmatch(r"[a-z0-9.-]+", hostname):
            raise ConfigurationError("MCP_HOSTNAME must be a valid hostname")
        email = os.getenv("GMX_EMAIL", "").strip()
        if not email or "@" not in email or any(c in email for c in "\r\n"):
            raise ConfigurationError("GMX_EMAIL must be a valid email address")
        log_level = os.getenv("LOG_LEVEL", "INFO").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("LOG_LEVEL is invalid")
        return cls(
            gmx_email=email,
            gmx_app_password=_secret("GMX_APP_PASSWORD"),
            mcp_api_token=token,
            mcp_hostname=hostname,
            mcp_port=_int("MCP_PORT", 8000, 1, 65535),
            mcp_bind_host=os.getenv("MCP_BIND_HOST", "0.0.0.0"),
            allow_send=_bool("ALLOW_SEND"),
            log_level=log_level,
            imap_host=os.getenv("IMAP_HOST", "imap.gmx.com"),
            imap_port=_int("IMAP_PORT", 993, 1, 65535),
            smtp_host=os.getenv("SMTP_HOST", "mail.gmx.com"),
            smtp_port=_int("SMTP_PORT", 587, 1, 65535),
            mail_timeout_seconds=_int("MAIL_TIMEOUT_SECONDS", 30, 5, 120),
            max_email_results=_int("MAX_EMAIL_RESULTS", 100, 1, 100),
            max_body_chars=_int("MAX_BODY_CHARS", 100_000, 1_000, 250_000),
            max_attachment_bytes=_int(
                "MAX_ATTACHMENT_BYTES", 10 * 1024 * 1024, 1_024, 25 * 1024 * 1024
            ),
            max_recipients=_int("MAX_RECIPIENTS", 20, 1, 50),
            max_subject_chars=_int("MAX_SUBJECT_CHARS", 255, 1, 998),
            max_draft_body_chars=_int(
                "MAX_DRAFT_BODY_CHARS", 100_000, 1_000, 250_000
            ),
            audit_db_path=os.getenv("AUDIT_DB_PATH", "/data/audit.db"),
        )
