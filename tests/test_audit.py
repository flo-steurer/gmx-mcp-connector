from app.audit import AuditLedger
from app.models import MessageRef


def test_audit_reservation_and_state_are_durable(tmp_path) -> None:
    ledger = AuditLedger(str(tmp_path / "audit.db"))
    reservation = ledger.reserve("<id@example.com>", b"body")
    assert reservation.state == "reserved"
    assert ledger.reserve("<id@example.com>", b"body").state == "attempting"
    ledger.set_state(reservation.key, "sent")
    assert ledger.reserve("<id@example.com>", b"body").state == "sent"
    ledger.record(
        draft=MessageRef(folder="Drafts", uid=1, uid_validity=2),
        message_id="<id@example.com>",
        recipients=["to@example.com"],
        subject="Subject",
        content_hash=ledger.content_hash(b"body"),
        outcome="sent",
    )
