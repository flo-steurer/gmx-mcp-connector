import imaplib
from typing import cast

from app.config import Settings
from app.imap_client import IMAPClient, SelectedMailbox
from app.models import EmailDetail


def _settings() -> Settings:
    return Settings(
        gmx_email="me@example.com",
        gmx_app_password="app-password",
        mcp_api_token="t" * 40,
        mcp_hostname="mail.example.com",
    )


class _EmptySearchConnection:
    def uid(self, *_args: object) -> tuple[str, list[None]]:
        return "OK", [None]


class _MetadataConnection:
    def uid(self, *_args: object) -> tuple[str, list[tuple[bytes, bytes]]]:
        descriptor = (
            b"1 (UID 7 FLAGS (\\Seen) RFC822.SIZE 42 "
            b"BODYSTRUCTURE (\"TEXT\" \"PLAIN\") "
            b"BODY[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID CONTENT-TYPE "
            b"CONTENT-DISPOSITION)] {71}"
        )
        headers = (
            b"From: Sender <sender@example.com>\r\n"
            b"To: Me <me@example.com>\r\n"
            b"Subject: Test\r\n"
            b"Date: Tue, 01 Jan 2030 00:00:00 +0000\r\n"
            b"\r\n"
        )
        return "OK", [(descriptor, headers)]


def test_empty_search_payload_is_an_empty_result() -> None:
    selected = SelectedMailbox(
        cast(imaplib.IMAP4_SSL, _EmptySearchConnection()), "INBOX", 1
    )

    result = IMAPClient(_settings()).search(selected, limit=20)

    assert result == []


def test_metadata_decodes_byte_flags_before_casefolding() -> None:
    selected = SelectedMailbox(
        cast(imaplib.IMAP4_SSL, _MetadataConnection()), "INBOX", 1
    )

    result = IMAPClient(_settings())._metadata(selected, 7)

    assert result.unread is False
    assert result.subject == "Test"


def test_email_detail_uses_wire_alias_for_from_field() -> None:
    detail = EmailDetail(
        message={"folder": "INBOX", "uid": 1, "uid_validity": 2},
        from_=[],
        to=[],
        cc=[],
        subject="",
        date=None,
        message_id=None,
        in_reply_to=None,
        references=[],
        body="",
        body_truncated=False,
        attachments=[],
    )

    wire = detail.model_dump(by_alias=True)

    assert "from" in wire
    assert "from_" not in wire
