from __future__ import annotations

import re
from datetime import datetime
from email.message import EmailMessage, Message
from email.policy import default
from email.utils import getaddresses, parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import PurePath

from app.models import Address, AttachmentMetadata


class _HTMLTextExtractor(HTMLParser):
    _breaks = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "head"}:
            self.hidden += 1
        elif tag in self._breaks and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "head"} and self.hidden:
            self.hidden -= 1
        elif tag in self._breaks and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def html_to_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(value)
    text = unescape("".join(parser.parts)).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_message(raw: bytes) -> EmailMessage:
    from email.parser import BytesParser

    parsed = BytesParser(policy=default).parsebytes(raw)
    if not isinstance(parsed, EmailMessage):  # pragma: no cover - policy.default guarantees this
        raise ValueError("Unsupported message type")
    return parsed


def header(message: Message, name: str) -> str:
    value = message.get(name)
    return str(value).strip() if value is not None else ""


def addresses(message: Message, *names: str) -> list[Address]:
    values = [str(value) for name in names for value in message.get_all(name, [])]
    return [Address(name=name, email=email) for name, email in getaddresses(values) if email]


def parsed_date(message: Message) -> datetime | None:
    value = header(message, "Date")
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        return parsed if parsed.tzinfo else parsed.astimezone()
    except (TypeError, ValueError, OverflowError):
        return None


def safe_filename(value: str | None, fallback: str = "attachment") -> str:
    name = PurePath((value or "").replace("\\", "/")).name
    name = "".join(ch if ch >= " " and ch not in '\x7f/:\\' else "_" for ch in name)
    name = name.strip(" .")[:255]
    return name or fallback


def _payload_text(part: Message) -> str:
    try:
        return part.get_content()  # type: ignore[no-any-return, union-attr]
    except (LookupError, UnicodeError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace")


def readable_body(message: EmailMessage, max_chars: int) -> tuple[str, bool]:
    plain: list[str] = []
    html: list[str] = []
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain":
            plain.append(_payload_text(part))
        elif content_type == "text/html":
            html.append(_payload_text(part))
    body = "\n\n".join(x.strip() for x in plain if x.strip())
    if not body:
        body = "\n\n".join(html_to_text(x) for x in html if x.strip())
    truncated = len(body) > max_chars
    if truncated:
        body = body[:max_chars] + "\n\n[Body truncated by awita-mail]"
    return body, truncated


def attachment_metadata(message: EmailMessage) -> list[AttachmentMetadata]:
    result: list[AttachmentMetadata] = []

    def visit(part: Message, prefix: str) -> None:
        if part.is_multipart():
            for index, child in enumerate(part.iter_parts(), 1):  # type: ignore[attr-defined]
                visit(child, f"{prefix}.{index}" if prefix else str(index))
            return
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        if disposition == "attachment" or filename:
            payload = part.get_payload(decode=True) or b""
            result.append(
                AttachmentMetadata(
                    attachment_id=prefix or "1",
                    filename=safe_filename(filename),
                    mime_type=part.get_content_type(),
                    size=len(payload),
                    disposition=disposition,
                )
            )

    visit(message, "")
    return result


def find_attachment(message: EmailMessage, attachment_id: str) -> Message:
    if not re.fullmatch(r"[1-9]\d*(?:\.[1-9]\d*)*", attachment_id):
        raise ValueError("Invalid attachment identifier")
    part: Message = message
    for component in attachment_id.split("."):
        if not part.is_multipart():
            raise ValueError("Attachment does not exist")
        children = list(part.iter_parts())  # type: ignore[attr-defined]
        index = int(component) - 1
        if index >= len(children):
            raise ValueError("Attachment does not exist")
        part = children[index]
    if part.is_multipart() or not (
        part.get_content_disposition() == "attachment" or part.get_filename()
    ):
        raise ValueError("Attachment does not exist")
    return part
