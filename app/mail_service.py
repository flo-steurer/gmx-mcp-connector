from __future__ import annotations

import imaplib
import re
from copy import deepcopy
from datetime import date
from email.message import EmailMessage
from email.policy import default
from email.utils import formatdate, getaddresses, make_msgid

from app.audit import AuditLedger
from app.config import Settings
from app.imap_client import IMAPClient, MailboxError, SelectedMailbox, envelope_recipients
from app.mail_parser import (
    addresses,
    attachment_metadata,
    find_attachment,
    header,
    parse_message,
    parsed_date,
    readable_body,
    safe_filename,
)
from app.models import (
    AttachmentMetadata,
    ConnectivityResult,
    DraftResult,
    EmailDetail,
    EmailMetadata,
    FolderInfo,
    MessageRef,
    OperationResult,
    SendResult,
)
from app.smtp_client import SMTPClient, SMTPDeliveryError


class ValidationError(ValueError):
    pass


def validate_addresses(values: list[str], *, allow_empty: bool = True) -> list[str]:
    parsed: list[str] = []
    for value in values:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValidationError("Email addresses contain invalid control characters")
        entries = [(name, address) for name, address in getaddresses([value]) if address]
        if len(entries) != 1 or "@" not in entries[0][1]:
            raise ValidationError("One or more email addresses are invalid")
        parsed.append(value.strip())
    if not parsed and not allow_empty:
        raise ValidationError("At least one recipient is required")
    return parsed


def normalize_subject(value: str, max_chars: int) -> str:
    if any(char in value for char in "\r\n"):
        raise ValidationError("Subject cannot contain line breaks")
    if len(value) > max_chars:
        raise ValidationError("Subject exceeds the configured limit")
    return value.strip()


def _replace_header(message: EmailMessage, name: str, value: str | None) -> None:
    if name in message:
        del message[name]
    if value:
        message[name] = value


def replace_draft_body(original: EmailMessage, body: str) -> EmailMessage:
    replacement = EmailMessage(policy=default)
    skipped = {
        "content-type",
        "content-transfer-encoding",
        "mime-version",
        "to",
        "cc",
        "bcc",
        "subject",
    }
    for name, value in original.items():
        if name.casefold() not in skipped:
            replacement[name] = value
    replacement.set_content(body)
    for part in original.walk():
        if part.is_multipart() or not (
            part.get_content_disposition() == "attachment" or part.get_filename()
        ):
            continue
        payload = part.get_payload(decode=True) or b""
        maintype, subtype = part.get_content_type().split("/", 1)
        replacement.add_attachment(
            payload,
            maintype=maintype,
            subtype=subtype,
            filename=safe_filename(part.get_filename()),
            disposition=part.get_content_disposition() or "attachment",
        )
    return replacement


class MailService:
    def __init__(
        self, settings: Settings, imap: IMAPClient, smtp: SMTPClient, audit: AuditLedger
    ) -> None:
        self.settings = settings
        self.imap = imap
        self.smtp = smtp
        self.audit = audit

    def list_folders(self) -> list[FolderInfo]:
        with self.imap.connect() as connection:
            folders, _ = self.imap.folders(connection)
            return folders

    def _select_folder(
        self, connection: imaplib.IMAP4_SSL, folder: str, *, readonly: bool
    ) -> SelectedMailbox:
        resolved = self.imap.resolve_folder(connection, folder)
        return self.imap.select(connection, resolved, readonly=readonly)

    def _require_role(
        self,
        connection: imaplib.IMAP4_SSL,
        reference: MessageRef,
        role: str,
        *,
        readonly: bool,
    ) -> SelectedMailbox:
        _, roles = self.imap.folders(connection)
        expected = roles.get(role)
        if expected is None or expected.casefold() != reference.folder.casefold():
            raise ValidationError(f"Message must be in the resolved {role} folder")
        return self.imap.select_ref(connection, reference, readonly=readonly)

    def list_emails(
        self,
        folder: str,
        limit: int,
        unread_only: bool,
        before: date | None,
        after: date | None,
    ) -> list[EmailMetadata]:
        self._validate_search(limit, before, after)
        with self.imap.connect() as connection:
            selected = self._select_folder(connection, folder, readonly=True)
            return self.imap.search(
                selected,
                limit=limit,
                unread=True if unread_only else None,
                before=before,
                after=after,
            )

    def _validate_search(self, limit: int, before: date | None, after: date | None) -> None:
        if not 1 <= limit <= self.settings.max_email_results:
            raise ValidationError("Limit exceeds the configured maximum")
        if before and after and before <= after:
            raise ValidationError("before must be later than after")

    def search_emails(
        self,
        *,
        folder: str,
        limit: int,
        text: str | None,
        sender: str | None,
        recipient: str | None,
        subject: str | None,
        unread: bool | None,
        before: date | None,
        after: date | None,
    ) -> list[EmailMetadata]:
        self._validate_search(limit, before, after)
        with self.imap.connect() as connection:
            selected = self._select_folder(connection, folder, readonly=True)
            return self.imap.search(
                selected,
                limit=limit,
                unread=unread,
                text=text,
                sender=sender,
                recipient=recipient,
                subject=subject,
                before=before,
                after=after,
            )

    def read_email(self, reference: MessageRef) -> EmailDetail:
        with self.imap.connect() as connection:
            selected = self.imap.select_ref(connection, reference, readonly=True)
            raw, _ = self.imap.fetch_raw(selected, reference.uid)
        message = parse_message(raw)
        body, truncated = readable_body(message, self.settings.max_body_chars)
        return EmailDetail(
            message=reference,
            from_=addresses(message, "From"),
            to=addresses(message, "To"),
            cc=addresses(message, "Cc"),
            subject=header(message, "Subject"),
            date=parsed_date(message),
            message_id=header(message, "Message-ID") or None,
            in_reply_to=header(message, "In-Reply-To") or None,
            references=header(message, "References").split(),
            body=body,
            body_truncated=truncated,
            attachments=attachment_metadata(message),
        )

    def list_attachments(self, reference: MessageRef) -> list[AttachmentMetadata]:
        with self.imap.connect() as connection:
            selected = self.imap.select_ref(connection, reference, readonly=True)
            raw, _ = self.imap.fetch_raw(selected, reference.uid)
        return attachment_metadata(parse_message(raw))

    def download_attachment(
        self, reference: MessageRef, attachment_id: str
    ) -> tuple[bytes, str, str]:
        with self.imap.connect() as connection:
            selected = self.imap.select_ref(connection, reference, readonly=True)
            raw, _ = self.imap.fetch_raw(selected, reference.uid)
        part = find_attachment(parse_message(raw), attachment_id)
        payload = part.get_payload(decode=True) or b""
        if len(payload) > self.settings.max_attachment_bytes:
            raise ValidationError("Attachment exceeds the configured size limit")
        return payload, part.get_content_type(), safe_filename(part.get_filename())

    def _validate_draft_fields(
        self, to: list[str], cc: list[str], bcc: list[str], subject: str, body: str
    ) -> tuple[list[str], list[str], list[str], str]:
        to = validate_addresses(to, allow_empty=False)
        cc = validate_addresses(cc)
        bcc = validate_addresses(bcc)
        if len(to) + len(cc) + len(bcc) > self.settings.max_recipients:
            raise ValidationError("Recipient count exceeds the configured limit")
        if len(body) > self.settings.max_draft_body_chars:
            raise ValidationError("Draft body exceeds the configured limit")
        return to, cc, bcc, normalize_subject(subject, self.settings.max_subject_chars)

    def _append_draft(self, message: EmailMessage) -> DraftResult:
        with self.imap.connect() as connection:
            _, roles = self.imap.folders(connection)
            drafts = roles.get("drafts")
            if not drafts:
                raise MailboxError("Drafts folder could not be resolved")
            reference = self.imap.append(connection, drafts, message)
        return DraftResult(draft=reference, message_id=header(message, "Message-ID"))

    def create_draft(
        self, to: list[str], cc: list[str], bcc: list[str], subject: str, body: str
    ) -> DraftResult:
        to, cc, bcc, subject = self._validate_draft_fields(to, cc, bcc, subject, body)
        message = EmailMessage(policy=default)
        message["From"] = self.settings.gmx_email
        message["To"] = ", ".join(to)
        if cc:
            message["Cc"] = ", ".join(cc)
        if bcc:
            message["Bcc"] = ", ".join(bcc)
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain=self.settings.gmx_email.split("@", 1)[1])
        message.set_content(body)
        return self._append_draft(message)

    def create_reply_draft(self, reference: MessageRef, body: str) -> DraftResult:
        if len(body) > self.settings.max_draft_body_chars:
            raise ValidationError("Draft body exceeds the configured limit")
        with self.imap.connect() as connection:
            selected = self.imap.select_ref(connection, reference, readonly=True)
            raw, _ = self.imap.fetch_raw(selected, reference.uid)
        original = parse_message(raw)
        candidates = addresses(original, "Reply-To") or addresses(original, "From")
        own = self.settings.gmx_email.casefold()
        recipients = [address.email for address in candidates if address.email.casefold() != own]
        if not recipients:
            raise ValidationError("Reply has no recipient other than the configured mailbox")
        subject = header(original, "Subject")
        if not re.match(r"^\s*re\s*:", subject, re.IGNORECASE):
            subject = f"Re: {subject}"
        subject = normalize_subject(subject, self.settings.max_subject_chars)
        source_id = header(original, "Message-ID")
        references = header(original, "References").split() + ([source_id] if source_id else [])
        reply = EmailMessage(policy=default)
        reply["From"] = self.settings.gmx_email
        reply["To"] = ", ".join(validate_addresses(recipients, allow_empty=False))
        reply["Subject"] = subject
        reply["Date"] = formatdate(localtime=True)
        reply["Message-ID"] = make_msgid(domain=self.settings.gmx_email.split("@", 1)[1])
        if source_id:
            reply["In-Reply-To"] = source_id
        if references:
            reply["References"] = " ".join(references)
        reply.set_content(body)
        return self._append_draft(reply)

    def update_draft(
        self,
        reference: MessageRef,
        *,
        to: list[str] | None,
        cc: list[str] | None,
        bcc: list[str] | None,
        subject: str | None,
        body: str | None,
    ) -> DraftResult:
        with self.imap.connect() as connection:
            selected = self._require_role(connection, reference, "drafts", readonly=False)
            raw, flags = self.imap.fetch_raw(selected, reference.uid)
            if "\\draft" not in {flag.casefold() for flag in flags}:
                raise ValidationError("Message is not marked as a draft")
            original = parse_message(raw)
            current_body, _ = readable_body(original, self.settings.max_draft_body_chars)
            next_body = body if body is not None else current_body
            fields = self._validate_draft_fields(
                to if to is not None else [a.email for a in addresses(original, "To")],
                cc if cc is not None else [a.email for a in addresses(original, "Cc")],
                bcc if bcc is not None else [a.email for a in addresses(original, "Bcc")],
                subject if subject is not None else header(original, "Subject"),
                next_body,
            )
            next_to, next_cc, next_bcc, next_subject = fields
            updated = replace_draft_body(original, next_body)
            _replace_header(updated, "From", self.settings.gmx_email)
            _replace_header(updated, "To", ", ".join(next_to))
            _replace_header(updated, "Cc", ", ".join(next_cc) or None)
            _replace_header(updated, "Bcc", ", ".join(next_bcc) or None)
            _replace_header(updated, "Subject", next_subject)
            new_reference = self.imap.append(connection, selected.folder, updated)
            try:
                self.imap.safe_delete(selected, reference.uid)
            except Exception as exc:
                raise MailboxError(
                    f"Replacement draft UID {new_reference.uid} was created; old draft removal failed"
                ) from exc
        return DraftResult(draft=new_reference, message_id=header(updated, "Message-ID"))

    def delete_draft(self, reference: MessageRef) -> OperationResult:
        with self.imap.connect() as connection:
            selected = self._require_role(connection, reference, "drafts", readonly=False)
            _, flags = self.imap.fetch_raw(selected, reference.uid)
            if "\\draft" not in {flag.casefold() for flag in flags}:
                raise ValidationError("Message is not marked as a draft")
            self.imap.safe_delete(selected, reference.uid)
        return OperationResult(success=True, status="draft_deleted")

    def mark_seen(self, reference: MessageRef, *, seen: bool) -> OperationResult:
        with self.imap.connect() as connection:
            selected = self.imap.select_ref(connection, reference, readonly=False)
            self.imap.set_seen(selected, reference.uid, seen)
        return OperationResult(success=True, status="read" if seen else "unread")

    def send_draft(self, reference: MessageRef) -> SendResult:
        if not self.settings.allow_send:
            raise ValidationError("Sending is disabled by server configuration")
        with self.imap.connect() as connection:
            selected = self._require_role(connection, reference, "drafts", readonly=False)
            raw, flags = self.imap.fetch_raw(selected, reference.uid)
            if "\\draft" not in {flag.casefold() for flag in flags}:
                raise ValidationError("Message is not marked as a draft")
            message = parse_message(raw)
            message_id = header(message, "Message-ID")
            if not message_id:
                raise ValidationError("Draft has no Message-ID")
            recipients = envelope_recipients(message)
            validate_addresses(recipients, allow_empty=False)
            if len(recipients) > self.settings.max_recipients:
                raise ValidationError("Recipient count exceeds the configured limit")
            senders = addresses(message, "From")
            if senders and not any(
                item.email.casefold() == self.settings.gmx_email.casefold() for item in senders
            ):
                raise ValidationError("Draft sender does not match the configured mailbox")
            _, roles = self.imap.folders(connection)
            sent_folder = roles.get("sent")
            if not sent_folder:
                raise MailboxError("Sent folder could not be resolved")
            reservation = self.audit.reserve(message_id, raw)
            if reservation.state in {"sent", "attempting", "unknown"}:
                if self.imap.contains_message_id(connection, sent_folder, message_id):
                    self.audit.set_state(reservation.key, "sent")
                    return SendResult(
                        success=True,
                        status="already_sent",
                        message_id=message_id,
                        sent_copy="existing",
                    )
                if reservation.state != "sent":
                    return SendResult(success=False, status="unknown", message_id=message_id)
                return SendResult(success=True, status="already_sent", message_id=message_id)
            subject = header(message, "Subject")
            content_hash = self.audit.content_hash(raw)
            try:
                self.smtp.send(message, recipients)
            except SMTPDeliveryError as exc:
                outcome = "unknown" if exc.ambiguous else "failed"
                if exc.ambiguous:
                    self.audit.set_state(reservation.key, "unknown")
                else:
                    self.audit.release_retryable(reservation.key)
                self.audit.record(
                    draft=reference,
                    message_id=message_id,
                    recipients=recipients,
                    subject=subject,
                    content_hash=content_hash,
                    outcome=outcome,
                    error_code="smtp_delivery_error",
                )
                return SendResult(success=False, status=outcome, message_id=message_id)
            sent_copy = "existing"
            try:
                if not self.imap.contains_message_id(connection, sent_folder, message_id):
                    outbound = deepcopy(message)
                    if "Bcc" in outbound:
                        del outbound["Bcc"]
                    self.imap.append_sent(
                        connection, sent_folder, outbound.as_bytes(policy=default)
                    )
                    sent_copy = "appended"
                self.audit.set_state(reservation.key, "sent")
                self.audit.record(
                    draft=reference,
                    message_id=message_id,
                    recipients=recipients,
                    subject=subject,
                    content_hash=content_hash,
                    outcome="sent",
                )
                # Sent reconciliation selects a different mailbox on this
                # connection, so re-select the draft before expunging its UID.
                selected = self.imap.select(connection, reference.folder, readonly=False)
                if selected.uid_validity != reference.uid_validity:
                    raise MailboxError("Draft changed while sending")
                self.imap.safe_delete(selected, reference.uid)
            except Exception:
                self.audit.set_state(reservation.key, "unknown")
                self.audit.record(
                    draft=reference,
                    message_id=message_id,
                    recipients=recipients,
                    subject=subject,
                    content_hash=content_hash,
                    outcome="unknown",
                    error_code="post_delivery_reconciliation_error",
                )
                return SendResult(success=False, status="unknown", message_id=message_id)
            return SendResult(
                success=True, status="sent", message_id=message_id, sent_copy=sent_copy
            )

    def check_connectivity(self) -> ConnectivityResult:
        imap_status = "failed"
        smtp_status = "failed"
        roles: dict[str, str] = {}
        try:
            with self.imap.connect() as connection:
                _, roles = self.imap.folders(connection)
            imap_status = "ok"
        except MailboxError:
            pass
        try:
            self.smtp.check_connectivity()
            smtp_status = "ok"
        except SMTPDeliveryError:
            pass
        return ConnectivityResult(  # type: ignore[arg-type]
            imap=imap_status, smtp=smtp_status, folders_resolved=roles
        )
