from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.models import MessageRef


@dataclass(frozen=True, slots=True)
class SendReservation:
    key: str
    state: str


class AuditLedger:
    """Durable send audit and replay guard. Bodies are never stored."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        parent = Path(path).parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            # /data is the Docker volume. During local runs the example
            # configuration may still point there, so use project-local data.
            if path == "/data/audit.db" and not Path("/.dockerenv").exists():
                self.path = str(Path.cwd() / "data" / "audit.db")
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            else:
                raise
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS send_state (
                    send_key TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    message_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    folder TEXT NOT NULL,
                    uid INTEGER NOT NULL,
                    uid_validity INTEGER NOT NULL,
                    message_id TEXT,
                    recipients_json TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    error_code TEXT
                );
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def content_hash(raw: bytes) -> str:
        return hashlib.sha256(raw).hexdigest()

    def reserve(self, message_id: str, raw: bytes) -> SendReservation:
        content_hash = self.content_hash(raw)
        key = hashlib.sha256(f"{message_id}\0{content_hash}".encode()).hexdigest()
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM send_state WHERE send_key = ?", (key,)
            ).fetchone()
            if row:
                return SendReservation(key=key, state=str(row[0]))
            connection.execute(
                "INSERT INTO send_state(send_key, state, updated_at, message_id) VALUES(?, ?, ?, ?)",
                (key, "attempting", now, message_id),
            )
        return SendReservation(key=key, state="reserved")

    def set_state(self, key: str, state: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE send_state SET state = ?, updated_at = ? WHERE send_key = ?",
                (state, datetime.now(UTC).isoformat(), key),
            )

    def release_retryable(self, key: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM send_state WHERE send_key = ? AND state = 'attempting'", (key,)
            )

    def record(
        self,
        *,
        draft: MessageRef,
        message_id: str,
        recipients: list[str],
        subject: str,
        content_hash: str,
        outcome: str,
        error_code: str | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO audit_events(
                    timestamp, operation, folder, uid, uid_validity, message_id,
                    recipients_json, subject, content_hash, outcome, error_code
                ) VALUES (?, 'send_draft', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(UTC).isoformat(),
                    draft.folder,
                    draft.uid,
                    draft.uid_validity,
                    message_id,
                    json.dumps(recipients, ensure_ascii=False),
                    subject,
                    content_hash,
                    outcome,
                    error_code,
                ),
            )
