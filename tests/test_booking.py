from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app import worker
from app.config import get_settings
from tests.conftest import (
    ADMIN,
    MEMBER,
    MEMBER2,
    PATIENT,
    book_and_pay,
    booking_day,
    first_free_slot,
    onboard_doctor,
    pay,
)


def _book(client, doctor_id, start_at, member=MEMBER, mode="video"):
    return client.post("/appointments", headers=member,
                       json={"doctor_id": doctor_id, "start_at": start_at, "mode": mode, "patient": PATIENT})


def _move_to_past(db, appt_id, minutes_ago=30):
    db.execute(text("UPDATE appointments SET start_at = now() - make_interval(mins => :m), "
                    "end_at = now() - make_interval(mins => :m) + interval '15 minutes' WHERE id = :i"),
               {"m": minutes_ago, "i": appt_id})
    db.commit()


# --- Hours and slots ------------------------------------------------------------

def test_hours_reject_overlapping_windows(client, db):
    d = onboard_doctor(client, db)
    r = client.put("/doctor/hours", headers=d["headers"], json={"windows": [
        {"weekday": 0, "start": "09:00", "end": "13:00"}, {"weekday": 0, "start": "12:00", "end": "14:00"}]})
    assert r.status_code == 422


def test_slots_follow_two_windows(client, db):
    d = onboard_doctor(client, db)
    day = booking_day()
    body = client.get(f"/doctors/{d['doctor_id']}/slots", params={"date": day.isoformat()}).json()
    # 9–13 and 16–19 at 15 minutes = 16 + 12 slots
    assert len(body["slots"]) == 28
    assert body["slots"][0]["start_ist"].endswith("09:00 AM IST")
    assert body["slots"][16]["start_ist"].endswith("04:00 PM IST")
    # Stored in UTC: 09:00 IST is 03:30 UTC
    assert body["slots"][0]["start_at"].startswith(f"{day.isoformat()}T03:30:00")


def test_notice_period_hides_near_slots(client, db):
    d = onboard_doctor(client, db)
    client.put("/doctor/hours", headers=d["headers"], json={
        "windows": [{"weekday": w, "start": "00:00", "end": "23:45"} for w in range(7)],
        "notice_minutes": 120})
    today = datetime.now(get_settings().tz).date()
    slots = client.get(f"/doctors/{d['doctor_id']}/slots", params={"date": today.isoformat()}).json()["slots"]
    earliest = datetime.now(UTC) + timedelta(minutes=120)
    assert all(datetime.fromisoformat(s["start_at"]) >= earliest for s in slots)


def test_day_off_removes_slots_and_shows_note(client, db):
    d = onboard_doctor(client, db)
    day = booking_day()
    r = client.post("/doctor/leave", headers=d["headers"], json={"date": day.isoformat(), "note": "At a conference"})
    assert r.status_code == 201
    body = client.get(f"/doctors/{d['doctor_id']}/slots", params={"date": day.isoformat()}).json()
    assert body["day_off"] is True and body["day_off_note"] == "At a conference" and body["slots"] == []
    client.delete(f"/doctor/leave/{r.json()['id']}", headers=d["headers"])
    assert client.get(f"/doctors/{d['doctor_id']}/slots", params={"date": day.isoformat()}).json()["slots"]


def test_find_doctors_filters(client, db):
    a = onboard_doctor(client, db)
    onboard_doctor(client, db, name="Dr. Arjun Rao", email="arjun@x.in", modes=("clinic",))
    cards = client.get("/doctors", params={"specialty": "diabetology"}).json()
    assert {c["full_name"] for c in cards} == {"Dr. Leela Menon", "Dr. Arjun Rao"}
    assert cards[0]["next_available_at"] is not None and cards[0]["fee_inr"] == 600
    # Video doctors show for any pincode; clinic doctors only at their pincode
    names = {c["full_name"] for c in client.get("/doctors", params={"pincode": "110001"}).json()}
    assert names == {"Dr. Leela Menon"}
    names = {c["full_name"] for c in client.get("/doctors", params={"pincode": "834005", "mode": "clinic"}).json()}
    assert names == {"Dr. Arjun Rao"}
    assert client.get(f"/doctors/{a['doctor_id']}").status_code == 200


# --- Booking and payment ----------------------------------------------------------

def test_book_pay_and_both_sides_see_it(client, db):
    d = onboard_doctor(client, db)
    start = first_free_slot(client, d["doctor_id"])
    r = _book(client, d["doctor_id"], start)
    assert r.status_code == 201
    b = r.json()
    assert b["member_status"] == "awaiting_payment" and b["amount_paise"] == 60000
    assert b["razorpay_order_id"].startswith("order_")

    # Held slot is crossed out for others
    slots = client.get(f"/doctors/{d['doctor_id']}/slots", params={"date": booking_day().isoformat()}).json()["slots"]
    assert next(s for s in slots if s["start_at"] == start)["available"] is False

    assert pay(client, b["razorpay_order_id"], 60000).status_code == 200
    mine = client.get(f"/appointments/{b['id']}", headers=MEMBER).json()
    assert mine["status"] == "booked" and mine["member_status"] == "upcoming" and mine["amount_paid_inr"] == 600
    docs = client.get("/doctor/appointments", headers=d["headers"],
                      params={"date": booking_day().isoformat()}).json()
    assert [a["id"] for a in docs] == [b["id"]]
    # Another member can't see it
    assert client.get(f"/appointments/{b['id']}", headers=MEMBER2).status_code == 404
    templates = set(db.execute(text("SELECT template FROM outbox WHERE appointment_id = :i"), {"i": b["id"]}).scalars())
    assert {"appointment_booked", "appointment_confirmed", "appointment_reminder",
            "create_video_room", "check_unmarked"} <= templates


def test_webhook_rejects_bad_signature_and_is_idempotent(client, db):
    d = onboard_doctor(client, db)
    b = _book(client, d["doctor_id"], first_free_slot(client, d["doctor_id"])).json()
    bad = client.post("/payments/razorpay/webhook", content=b"{}", headers={"X-Razorpay-Signature": "x"})
    assert bad.status_code == 400
    assert pay(client, b["razorpay_order_id"], 60000, event_id="evt_1").status_code == 200
    assert pay(client, b["razorpay_order_id"], 60000, event_id="evt_1").json()["duplicate"] is True
    assert pay(client, b["razorpay_order_id"], 60000, event_id="evt_2").status_code == 200
    n = db.execute(text("SELECT count(*) FROM outbox WHERE template = 'appointment_booked'")).scalar()
    assert n == 1


def test_double_booking_is_impossible(client, db):
    d = onboard_doctor(client, db)
    start = first_free_slot(client, d["doctor_id"])
    assert _book(client, d["doctor_id"], start).status_code == 201
    assert _book(client, d["doctor_id"], start, member=MEMBER2).status_code == 409


def test_unique_index_backstops_the_api(client, db):
    d = onboard_doctor(client, db)
    b = book_and_pay(client, d["doctor_id"])
    try:
        db.execute(text("INSERT INTO appointments (doctor_id, member_id, patient, mode, start_at, end_at, status, "
                        "fee_paise, amount_paid_paise, refund_paise, reschedule_count) "
                        "SELECT doctor_id, 'x', patient, mode, start_at, end_at, 'booked', 0, 0, 0, 0 "
                        "FROM appointments WHERE id = :i"), {"i": b["id"]})
        db.commit()
        raise AssertionError("duplicate insert should fail")
    except Exception as e:
        db.rollback()
        assert "uq_appointment_doctor_start_active" in str(e)


def test_expired_hold_frees_slot(client, db):
    d = onboard_doctor(client, db)
    start = first_free_slot(client, d["doctor_id"])
    first = _book(client, d["doctor_id"], start).json()
    db.execute(text("UPDATE appointments SET hold_expires_at = now() - interval '1 minute' WHERE id = :i"),
               {"i": first["id"]})
    db.commit()
    second = _book(client, d["doctor_id"], start, member=MEMBER2)
    assert second.status_code == 201
    # The first member's late payment is refunded, not double-booked
    pay(client, first["razorpay_order_id"], 60000)
    row = db.execute(text("SELECT status, refund_paise, refund_status FROM appointments WHERE id = :i"),
                     {"i": first["id"]}).one()
    assert row == ("cancelled", 60000, "pending")


def test_late_payment_still_books_if_slot_is_free(client, db):
    d = onboard_doctor(client, db)
    b = _book(client, d["doctor_id"], first_free_slot(client, d["doctor_id"])).json()
    db.execute(text("UPDATE appointments SET hold_expires_at = now() - interval '1 minute' WHERE id = :i"), {"i": b["id"]})
    db.commit()
    worker.run_once()  # expires the hold
    pay(client, b["razorpay_order_id"], 60000)
    assert client.get(f"/appointments/{b['id']}", headers=MEMBER).json()["status"] == "booked"


def test_mode_must_be_offered(client, db):
    d = onboard_doctor(client, db)  # video only
    assert _book(client, d["doctor_id"], first_free_slot(client, d["doctor_id"]), mode="clinic").status_code == 422


def test_off_grid_time_rejected(client, db):
    d = onboard_doctor(client, db)
    start = datetime.fromisoformat(first_free_slot(client, d["doctor_id"])) + timedelta(minutes=7)
    assert _book(client, d["doctor_id"], start.isoformat()).status_code == 422


# --- Reschedule and cancel ---------------------------------------------------------

def test_reschedule_frees_old_slot(client, db):
    d = onboard_doctor(client, db)
    old = first_free_slot(client, d["doctor_id"])
    b = book_and_pay(client, d["doctor_id"], old)
    new = first_free_slot(client, d["doctor_id"], index=3)
    r = client.patch(f"/appointments/{b['id']}/reschedule", headers=MEMBER, json={"start_at": new})
    assert r.status_code == 200
    assert datetime.fromisoformat(r.json()["start_at"]) == datetime.fromisoformat(new)
    assert r.json()["status"] == "booked"
    slots = {s["start_at"]: s["available"] for s in
             client.get(f"/doctors/{d['doctor_id']}/slots", params={"date": booking_day().isoformat()}).json()["slots"]}
    assert slots[old] is True and slots[new] is False


def test_reschedule_closed_near_start(client, db):
    d = onboard_doctor(client, db)
    b = book_and_pay(client, d["doctor_id"])
    db.execute(text("UPDATE appointments SET start_at = now() + interval '3 hours', "
                    "end_at = now() + interval '3 hours 15 minutes' WHERE id = :i"), {"i": b["id"]})
    db.commit()
    r = client.patch(f"/appointments/{b['id']}/reschedule", headers=MEMBER,
                     json={"start_at": first_free_slot(client, d["doctor_id"], index=5)})
    assert r.status_code == 409


def test_member_cancel_refund_rules(client, db):
    d = onboard_doctor(client, db)
    early = book_and_pay(client, d["doctor_id"])
    r = client.post(f"/appointments/{early['id']}/cancel", headers=MEMBER, json={"reason": "Feeling better"})
    assert r.json()["status"] == "cancelled" and r.json()["refund_inr"] == 600

    late = book_and_pay(client, d["doctor_id"], first_free_slot(client, d["doctor_id"], index=4))
    db.execute(text("UPDATE appointments SET start_at = now() + interval '2 hours', "
                    "end_at = now() + interval '2 hours 15 minutes' WHERE id = :i"), {"i": late["id"]})
    db.commit()
    r = client.post(f"/appointments/{late['id']}/cancel", headers=MEMBER, json={})
    assert r.json()["status"] == "cancelled" and r.json()["refund_inr"] == 0


# --- Doctor dashboard actions ---------------------------------------------------------

def test_done_queues_payout_and_review_updates_rating(client, db):
    d = onboard_doctor(client, db)
    b = book_and_pay(client, d["doctor_id"])
    url = f"/doctor/appointments/{b['id']}/status"
    assert client.patch(url, headers=d["headers"], json={"status": "completed"}).status_code == 409  # not started
    _move_to_past(db, b["id"])
    r = client.patch(url, headers=d["headers"], json={"status": "completed"})
    assert r.status_code == 200 and r.json()["member_status"] == "completed"
    payout = db.execute(text("SELECT gross_paise, commission_paise, net_paise FROM payouts")).one()
    assert payout == (60000, 9000, 51000)

    assert client.post(f"/appointments/{b['id']}/review", headers=MEMBER, json={"rating": 5}).status_code == 201
    assert client.post(f"/appointments/{b['id']}/review", headers=MEMBER, json={"rating": 1}).status_code == 409
    assert client.get(f"/doctors/{d['doctor_id']}").json()["rating"] == 5.0


def test_patient_no_show(client, db):
    d = onboard_doctor(client, db)
    b = book_and_pay(client, d["doctor_id"])
    _move_to_past(db, b["id"])
    r = client.patch(f"/doctor/appointments/{b['id']}/status", headers=d["headers"], json={"status": "no_show"})
    assert r.json()["member_status"] == "no_show" and r.json()["refund_inr"] == 0
    assert db.execute(text("SELECT net_paise FROM payouts")).scalar() == 51000


def test_doctor_cannot_touch_other_doctors_appointments(client, db):
    a = onboard_doctor(client, db)
    b = onboard_doctor(client, db, name="Dr. Arjun Rao", email="arjun@x.in")
    appt = book_and_pay(client, a["doctor_id"])
    assert client.get(f"/doctor/appointments/{appt['id']}", headers=b["headers"]).status_code == 404
    assert client.patch(f"/doctor/appointments/{appt['id']}/status", headers=b["headers"],
                        json={"status": "completed"}).status_code == 404


def test_day_off_with_bookings_needs_confirmation(client, db):
    d = onboard_doctor(client, db)
    b = book_and_pay(client, d["doctor_id"])
    day = booking_day().isoformat()
    r = client.post("/doctor/leave", headers=d["headers"], json={"date": day, "note": "Family emergency"})
    assert r.status_code == 409 and len(r.json()["detail"]["appointments"]) == 1
    r = client.post("/doctor/leave", headers=d["headers"],
                    json={"date": day, "note": "Family emergency", "cancel_booked": True})
    assert r.status_code == 201 and r.json()["cancelled_appointments"] == 1
    mine = client.get(f"/appointments/{b['id']}", headers=MEMBER).json()
    assert mine["status"] == "cancelled" and mine["refund_inr"] == 600


def test_fee_change_needs_admin_approval(client, db):
    d = onboard_doctor(client, db)
    r = client.put("/doctor/me", headers=d["headers"], json={"fee_inr": 900, "bio": "Diabetologist, 12 years"})
    assert r.json()["fee_inr"] == 600 and r.json()["bio"] == "Diabetologist, 12 years"
    assert r.json()["pending_changes"][0]["changes"] == {"fee_inr": 900}
    req = client.get("/admin/doctors/profile-changes", headers=ADMIN).json()[0]
    assert req["current"] == {"fee_inr": 600}
    client.post(f"/admin/doctors/profile-changes/{req['id']}", headers=ADMIN, json={"decision": "approve"})
    assert client.get("/doctor/me", headers=d["headers"]).json()["fee_inr"] == 900


def test_suspend_cancels_upcoming_and_signs_out(client, db):
    d = onboard_doctor(client, db)
    b = book_and_pay(client, d["doctor_id"])
    r = client.post(f"/admin/doctors/{d['doctor_id']}/suspend", headers=ADMIN,
                    json={"note": "Registration lapsed, renewal pending"})
    assert r.json()["cancelled_appointments"] == 1
    assert client.get("/doctor/me", headers=d["headers"]).status_code == 401
    assert client.post("/auth/doctor/login", json={"username": d["username"], "password": "Maple2891secure"}).status_code == 403
    assert client.get(f"/doctors/{d['doctor_id']}").status_code == 404
    assert client.get(f"/appointments/{b['id']}", headers=MEMBER).json()["refund_inr"] == 600


# --- Worker ------------------------------------------------------------------------

def test_worker_creates_video_room_and_flags_unmarked(client, db):
    d = onboard_doctor(client, db)
    b = book_and_pay(client, d["doctor_id"])
    _move_to_past(db, b["id"], minutes_ago=5)
    # Video room job is due 10 min before start
    db.execute(text("UPDATE outbox SET due_at = now() - interval '1 minute' "
                    "WHERE appointment_id = :i AND template = 'create_video_room'"), {"i": b["id"]})
    db.commit()
    worker.run_once()
    mine = client.get(f"/appointments/{b['id']}", headers=MEMBER).json()
    assert mine["member_status"] == "in_progress"
    assert mine["video_room_url"].startswith("https://meet.jit.si/suppai-")

    worker.run_once(now=datetime.now(UTC) + timedelta(days=3))
    assert client.get(f"/appointments/{b['id']}", headers=MEMBER).json()["status"] == "needs_review"
    flagged = client.get("/admin/appointments", headers=ADMIN).json()
    assert [a["id"] for a in flagged] == [b["id"]]
    r = client.post(f"/admin/appointments/{b['id']}/resolve", headers=ADMIN,
                    json={"outcome": "doctor_no_show", "note": "Doctor confirmed they missed it"})
    assert r.json()["status"] == "cancelled" and r.json()["refund_inr"] == 600


def test_worker_processes_refunds(client, db):
    d = onboard_doctor(client, db)
    b = book_and_pay(client, d["doctor_id"])
    client.post(f"/appointments/{b['id']}/cancel", headers=MEMBER, json={})
    worker.run_once()
    refund_id = db.execute(text("SELECT razorpay_refund_id FROM appointments WHERE id = :i"), {"i": b["id"]}).scalar()
    assert refund_id.startswith("rfnd_")
    # Reminders for the cancelled appointment were cancelled, not sent
    sent = db.execute(text("SELECT count(*) FROM outbox WHERE appointment_id = :i AND template = 'appointment_reminder' "
                           "AND status = 'pending'"), {"i": b["id"]}).scalar()
    assert sent == 0
