import logging

import pytest
from bson import ObjectId

from app import worker
from app.config import get_settings
from app.services import contacts
from app.services.outbox import enqueue


def test_in_memory_source_resolves_member_recipient():
    contacts.source().add("member-1", phone="+919876543210", email="ravi@example.com")
    c = contacts.resolve_recipient("member:member-1")
    assert (c.phone, c.email) == ("+919876543210", "ravi@example.com")


def test_unknown_or_empty_member_is_not_found():
    contacts.source().add("member-2")  # no phone, no email
    for recipient in ("member:nobody", "member:member-2", "member:"):
        with pytest.raises(contacts.ContactNotFound):
            contacts.resolve_recipient(recipient)


def test_only_member_recipients_are_resolved():
    assert contacts.is_member_recipient("member:1")
    assert not contacts.is_member_recipient("doctor:1")
    assert not contacts.is_member_recipient("ops")
    assert not contacts.is_member_recipient(None)
    with pytest.raises(ValueError):
        contacts.resolve_recipient("doctor:1")


def test_provider_setting_switches_source(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "member_contacts_provider", "platform")
    contacts.reset()
    assert isinstance(contacts.source(), contacts.PlatformContactSource)
    with pytest.raises(NotImplementedError):
        contacts.resolve_member("member-1")

    monkeypatch.setattr(s, "member_contacts_provider", "carrier-pigeon")
    contacts.reset()
    with pytest.raises(ValueError):
        contacts.source()


def test_worker_sends_member_message_to_resolved_contact(db, monkeypatch):
    contacts.source().add("member-1", phone="+919876543210", email="ravi@example.com")
    sent = []
    monkeypatch.setattr(worker, "send", lambda row, contact: sent.append((row["recipient"], contact)))
    enqueue(db, channel="notify", template="appointment_confirmed", recipient="member:member-1",
            appointment_id=ObjectId())
    enqueue(db, channel="email", template="application_received", recipient="leela@example.com")
    worker.run_once()
    assert sent == [
        ("member:member-1", contacts.Contact(phone="+919876543210", email="ravi@example.com")),
        ("leela@example.com", None),
    ]
    assert db.outbox.count_documents({"status": "sent"}) == 2


def test_worker_retries_then_fails_unknown_member(db):
    from datetime import UTC, datetime, timedelta

    enqueue(db, channel="notify", template="appointment_confirmed", recipient="member:ghost")
    t = datetime.now(UTC)
    worker.run_once(now=t)
    row = db.outbox.find_one()
    assert (row["status"], row["attempts"]) == ("pending", 1)
    assert "member ghost" in row["last_error"]
    for _ in range(worker.MAX_ATTEMPTS):
        t += timedelta(hours=1)
        worker.run_once(now=t)
    assert db.outbox.find_one()["status"] == "failed"


def test_contact_details_are_masked_in_logs(db, caplog):
    contacts.source().add("member-1", phone="+919876543210", email="ravi@example.com")
    enqueue(db, channel="notify", template="appointment_confirmed", recipient="member:member-1")
    with caplog.at_level(logging.INFO, logger="suppai.worker"):
        worker.run_once()
    assert "SEND" in caplog.text and "3210" in caplog.text
    assert "+919876543210" not in caplog.text and "ravi@example.com" not in caplog.text
