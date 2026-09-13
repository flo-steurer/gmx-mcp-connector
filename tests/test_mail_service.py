from datetime import date

import pytest

from app.config import Settings
from app.mail_service import MailService, ValidationError, normalize_subject, validate_addresses


def settings(**overrides) -> Settings:
    values = dict(
        gmx_email="me@example.com",
        gmx_app_password="app-password",
        mcp_api_token="t" * 40,
        mcp_hostname="mail.example.com",
    )
    values.update(overrides)
    return Settings(**values)


def test_recipient_and_subject_validation() -> None:
    assert validate_addresses(["A <a@example.com>"]) == ["A <a@example.com>"]
    with pytest.raises(ValidationError):
        validate_addresses(["attacker@example.com\r\nBcc: x@example.com"])
    with pytest.raises(ValidationError):
        normalize_subject("bad\nsubject", 255)


def test_search_date_range_validation_without_mailbox() -> None:
    service = MailService(settings(), object(), object(), object())  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        service._validate_search(101, date(2026, 1, 2), date(2026, 1, 3))


def test_sending_is_disabled_before_any_mail_connection() -> None:
    service = MailService(settings(allow_send=False), object(), object(), object())  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="disabled"):
        service.send_draft(object())  # type: ignore[arg-type]
