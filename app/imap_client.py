from __future__ import annotations

import base64
import imaplib
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from email.policy import default
from email.utils import getaddresses
from typing import Iterator

from app.config import Settings
from app.mail_parser import addresses, header, parse_message, parsed_date
from app.models import EmailMetadata, FolderInfo, MessageRef


class MailboxError(RuntimeError):
    pass


class NotFoundError(MailboxError):
    pass


class StaleReferenceError(MailboxError):
    pass


class UnsafeOperationError(MailboxError):
    pass


def encode_modutf7(value: str) -> str:
    result: list[str] = []
    buffered: list[str] = []

    def flush() -> None:
        if not buffered:
            return
        raw = "".join(buffered).encode("utf-16be")
        result.append("&" + base64.b64encode(raw).decode().rstrip("=").replace("/", ",") + "-")
        buffered.clear()

    for char in value:
        if " " <= char <= "~":
            flush()
            result.append("&-" if char == "&" else char)
        else:
            buffered.append(char)
    flush()
    return "".join(result)


def decode_modutf7(value: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(value):
        if value[index] != "&":
            output.append(value[index])
            index += 1
            continue
        end = value.find("-", index)
        if end < 0:
            output.append(value[index:])
            break
        token = value[index + 1 : end]
        if not token:
            output.append("&")
        else:
            token = token.replace(",", "/")
            token += "=" * (-len(token) % 4)
            try:
                output.append(base64.b64decode(token).decode("utf-16be"))
            except (ValueError, UnicodeError):
                output.append(value[index : end + 1])
        index = end + 1
    return "".join(output)


_LIST_RE = re.compile(rb"^\((?P<flags>[^)]*)\)\s+(?P<delimiter>NIL|\"(?:\\.|[^\"])*\")\s+(?P<name>.+)$")


def parse_list_line(line: bytes) -> FolderInfo:
    match = _LIST_RE.match(line)
    if not match:
        raise MailboxError("Malformed folder response")
    raw_name = match.group("name").strip()
    if raw_name.startswith(b'"') and raw_name.endswith(b'"'):
        raw_name = raw_name[1:-1].replace(b"\\\"", b'"').replace(b"\\\\", b"\\")
    name = decode_modutf7(raw_name.decode("ascii", errors="replace"))
    delimiter_raw = match.group("delimiter")
    delimiter = None if delimiter_raw == b"NIL" else delimiter_raw[1:-1].decode("ascii")
    flags = [flag.decode("ascii", errors="replace") for flag in match.group("flags").split()]
    return FolderInfo(name=name, delimiter=delimiter, flags=flags)


ROLE_FLAGS = {
    "sent": "\\sent",
    "drafts": "\\drafts",
    "trash": "\\trash",
    "spam": "\\junk",
}

ROLE_NAMES = {
    "sent": {"sent", "sent items", "gesendet", "gesendete objekte"},
    "drafts": {"drafts", "entwürfe", "entwuerfe"},
    "trash": {"trash", "deleted", "deleted items", "papierkorb", "gelöscht", "geloescht"},
    "spam": {"spam", "junk", "spamverdacht", "junk-e-mail"},
}


def assign_roles(folders: list[FolderInfo]) -> dict[str, str]:
    roles: dict[str, str] = {}
    inboxes = [folder for folder in folders if folder.name.casefold() == "inbox"]
    if len(inboxes) == 1:
        roles["inbox"] = inboxes[0].name
        inboxes[0].role = "inbox"
    for role, special_flag in ROLE_FLAGS.items():
        flagged = [folder for folder in folders if special_flag in {x.casefold() for x in folder.flags}]
        candidates = flagged
        if not candidates:
            names = ROLE_NAMES[role]
            candidates = [folder for folder in folders if folder.name.casefold() in names]
        if len(candidates) == 1:
            roles[role] = candidates[0].name
            candidates[0].role = role  # type: ignore[assignment]
    return roles


def _quote_mailbox(name: str) -> str:
    encoded = encode_modutf7(name)
    return '"' + encoded.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _quote_search(value: str) -> bytes:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Search fields cannot contain control characters")
    if len(value) > 500:
        raise ValueError("Search field is too long")
    return ('"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"').encode("utf-8")


@dataclass(slots=True)
class SelectedMailbox:
    connection: imaplib.IMAP4_SSL
    folder: str
    uid_validity: int


class IMAPClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @contextmanager
    def connect(self) -> Iterator[imaplib.IMAP4_SSL]:
        connection: imaplib.IMAP4_SSL | None = None
        try:
            connection = imaplib.IMAP4_SSL(
                self.settings.imap_host,
                self.settings.imap_port,
                ssl_context=self.settings.tls_context(),
                timeout=self.settings.mail_timeout_seconds,
            )
            status, _ = connection.login(self.settings.gmx_email, self.settings.gmx_app_password)
            if status != "OK":
                raise MailboxError("IMAP authentication failed")
            yield connection
        except (imaplib.IMAP4.error, OSError) as exc:
            raise MailboxError("IMAP operation failed") from exc
        finally:
            if connection is not None:
                try:
                    connection.logout()
                except (imaplib.IMAP4.error, OSError):
                    pass

    def folders(self, connection: imaplib.IMAP4_SSL) -> tuple[list[FolderInfo], dict[str, str]]:
        status, lines = connection.list()
        if status != "OK" or lines is None:
            raise MailboxError("Unable to list folders")
        folders = [parse_list_line(line) for line in lines if isinstance(line, bytes)]
        return folders, assign_roles(folders)

    def resolve_folder(self, connection: imaplib.IMAP4_SSL, requested: str) -> str:
        folders, roles = self.folders(connection)
        requested_key = requested.casefold()
        if requested_key in roles:
            return roles[requested_key]
        exact = [folder.name for folder in folders if folder.name.casefold() == requested_key]
        if len(exact) != 1:
            raise NotFoundError("Folder was not found or is ambiguous")
        return exact[0]

    def select(
        self, connection: imaplib.IMAP4_SSL, folder: str, *, readonly: bool
    ) -> SelectedMailbox:
        status, _ = connection.select(_quote_mailbox(folder), readonly=readonly)
        if status != "OK":
            raise NotFoundError("Mailbox folder is unavailable")
        _, values = connection.response("UIDVALIDITY")
        try:
            uid_validity = int(values[0])
        except (TypeError, ValueError, IndexError) as exc:
            raise MailboxError("Mailbox did not provide UIDVALIDITY") from exc
        return SelectedMailbox(connection, folder, uid_validity)

    def select_ref(
        self, connection: imaplib.IMAP4_SSL, reference: MessageRef, *, readonly: bool
    ) -> SelectedMailbox:
        folder = self.resolve_folder(connection, reference.folder)
        selected = self.select(connection, folder, readonly=readonly)
        if selected.uid_validity != reference.uid_validity:
            raise StaleReferenceError("Message reference is stale")
        return selected

    @staticmethod
    def _extract_fetch(data: list[bytes | tuple[bytes, bytes]] | None) -> tuple[bytes, bytes]:
        for item in data or []:
            if isinstance(item, tuple) and len(item) == 2:
                return item
        raise NotFoundError("Message was not found")

    def fetch_raw(self, selected: SelectedMailbox, uid: int) -> tuple[bytes, set[str]]:
        status, data = selected.connection.uid("FETCH", str(uid), "(UID FLAGS BODY.PEEK[])")
        if status != "OK":
            raise NotFoundError("Message was not found")
        descriptor, raw = self._extract_fetch(data)
        flags_match = re.search(rb"FLAGS \(([^)]*)\)", descriptor, re.IGNORECASE)
        flags = {
            token.decode("ascii", errors="replace")
            for token in (flags_match.group(1).split() if flags_match else [])
        }
        return raw, flags

    def fetch_attachment(self, selected: SelectedMailbox, uid: int, part_id: str) -> bytes:
        if not re.fullmatch(r"[1-9]\d*(?:\.[1-9]\d*)*", part_id):
            raise ValueError("Invalid attachment identifier")
        status, data = selected.connection.uid(
            "FETCH", str(uid), f"(UID BODY.PEEK[{part_id}])"
        )
        if status != "OK":
            raise NotFoundError("Attachment was not found")
        _, payload = self._extract_fetch(data)
        return payload

    def _metadata(self, selected: SelectedMailbox, uid: int) -> EmailMetadata:
        fields = "FROM TO CC SUBJECT DATE MESSAGE-ID CONTENT-TYPE CONTENT-DISPOSITION"
        status, data = selected.connection.uid(
            "FETCH",
            str(uid),
            f"(UID FLAGS RFC822.SIZE BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS ({fields})])",
        )
        if status != "OK":
            raise NotFoundError("Message was not found")
        descriptor, raw_headers = self._extract_fetch(data)
        message = parse_message(raw_headers)
        flags_match = re.search(rb"FLAGS \(([^)]*)\)", descriptor, re.IGNORECASE)
        flags = {token.casefold() for token in (flags_match.group(1).split() if flags_match else [])}
        upper = descriptor.upper()
        has_attachments = b"ATTACHMENT" in upper or b"FILENAME" in upper
        return EmailMetadata(
            message=MessageRef(
                folder=selected.folder, uid=uid, uid_validity=selected.uid_validity
            ),
            sender=addresses(message, "From"),
            recipients=addresses(message, "To", "Cc"),
            subject=header(message, "Subject"),
            date=parsed_date(message),
            unread=b"\\seen" not in flags,
            has_attachments=has_attachments,
            message_id=header(message, "Message-ID") or None,
        )

    def search(
        self,
        selected: SelectedMailbox,
        *,
        limit: int,
        unread: bool | None = None,
        text: str | None = None,
        sender: str | None = None,
        recipient: str | None = None,
        subject: str | None = None,
        after: date | None = None,
        before: date | None = None,
    ) -> list[EmailMetadata]:
        criteria: list[bytes] = []
        if unread is True:
            criteria.append(b"UNSEEN")
        elif unread is False:
            criteria.append(b"SEEN")
        for key, value in (("FROM", sender), ("TO", recipient), ("SUBJECT", subject)):
            if value:
                criteria.extend((key.encode(), _quote_search(value)))
        if text:
            value = _quote_search(text)
            # Search headers and body on the server without fetching the mailbox.
            criteria.extend((b"OR", b"OR", b"SUBJECT", value, b"FROM", value, b"BODY", value))
        if after:
            criteria.extend((b"SINCE", after.strftime("%d-%b-%Y").encode("ascii")))
        if before:
            criteria.extend((b"BEFORE", before.strftime("%d-%b-%Y").encode("ascii")))
        if not criteria:
            criteria = [b"ALL"]
        charset: str | None = "UTF-8" if any(
            value and not value.isascii() for value in (text, sender, recipient, subject)
        ) else None
        status, data = selected.connection.uid("SEARCH", charset, *criteria)
        if status != "OK" or not data:
            raise MailboxError("Mailbox search failed")
        uids = [int(value) for value in data[0].split() if value.isdigit()]
        uids = list(reversed(uids[-limit:]))
        return [self._metadata(selected, uid) for uid in uids]

    def append(self, connection: imaplib.IMAP4_SSL, folder: str, message: EmailMessage) -> MessageRef:
        raw = message.as_bytes(policy=default)
        status, data = connection.append(_quote_mailbox(folder), "(\\Draft)", None, raw)
        if status != "OK":
            raise MailboxError("Unable to create draft")
        response = b" ".join(x for x in (data or []) if isinstance(x, bytes))
        match = re.search(rb"APPENDUID\s+(\d+)\s+(\d+)", response, re.IGNORECASE)
        if not match:
            _, appenduid = connection.response("APPENDUID")
            response = b" ".join(x for x in (appenduid or []) if isinstance(x, bytes))
            match = re.search(rb"(\d+)\s+(\d+)", response)
        if match:
            return MessageRef(folder=folder, uid_validity=int(match.group(1)), uid=int(match.group(2)))
        selected = self.select(connection, folder, readonly=True)
        message_id = header(message, "Message-ID")
        status, found = connection.uid("SEARCH", None, b"HEADER", b"Message-ID", _quote_search(message_id))
        if status != "OK" or not found or not found[0].split():
            raise MailboxError("Draft created but its UID could not be determined")
        return MessageRef(
            folder=folder, uid_validity=selected.uid_validity, uid=int(found[0].split()[-1])
        )

    def contains_message_id(
        self, connection: imaplib.IMAP4_SSL, folder: str, message_id: str
    ) -> bool:
        self.select(connection, folder, readonly=True)
        status, data = connection.uid(
            "SEARCH", None, b"HEADER", b"Message-ID", _quote_search(message_id)
        )
        return status == "OK" and bool(data and data[0].split())

    def append_sent(self, connection: imaplib.IMAP4_SSL, folder: str, raw: bytes) -> None:
        status, _ = connection.append(_quote_mailbox(folder), "(\\Seen)", None, raw)
        if status != "OK":
            raise MailboxError("Unable to save Sent copy")

    def set_seen(self, selected: SelectedMailbox, uid: int, seen: bool) -> None:
        operation = "+FLAGS.SILENT" if seen else "-FLAGS.SILENT"
        status, _ = selected.connection.uid("STORE", str(uid), operation, "(\\Seen)")
        if status != "OK":
            raise NotFoundError("Unable to update message state")

    def safe_delete(self, selected: SelectedMailbox, uid: int) -> None:
        capabilities = {
            value.decode("ascii", errors="ignore").upper()
            if isinstance(value, bytes)
            else str(value).upper()
            for value in selected.connection.capabilities
        }
        if "UIDPLUS" not in capabilities:
            raise UnsafeOperationError("Server lacks UID-scoped expunge support")
        status, _ = selected.connection.uid("STORE", str(uid), "+FLAGS.SILENT", "(\\Deleted)")
        if status != "OK":
            raise NotFoundError("Unable to mark draft for deletion")
        status, _ = selected.connection.uid("EXPUNGE", str(uid))
        if status != "OK":
            selected.connection.uid("STORE", str(uid), "-FLAGS.SILENT", "(\\Deleted)")
            raise MailboxError("Unable to delete draft safely")


def envelope_recipients(message: EmailMessage) -> list[str]:
    return [
        email
        for _, email in getaddresses(
            [str(value) for name in ("To", "Cc", "Bcc") for value in message.get_all(name, [])]
        )
        if email
    ]
