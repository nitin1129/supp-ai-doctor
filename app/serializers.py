from datetime import datetime, timedelta

from app.config import get_settings
from app.db import now as db_now
from app.models import AppointmentStatus
from app.schemas.booking import AppointmentOut, DoctorBrief, DoctorCard, ist_label
from app.services.appointments import member_cancel_refund_percent, member_view_status


def rating(d: dict) -> float | None:
    return round(d["rating_sum"] / d["rating_count"], 1) if d.get("rating_count") else None


def appointment_out(appt: dict, now: datetime | None = None) -> AppointmentOut:
    s = get_settings()
    now = now or db_now()
    status = appt["status"]
    booked = status == AppointmentStatus.BOOKED
    pending = status == AppointmentStatus.PENDING_PAYMENT
    join_open = booked and now >= appt["start_at"] - timedelta(minutes=s.video_room_lead_minutes)
    doc = appt["doctor"]
    return AppointmentOut(
        id=str(appt["_id"]),
        doctor=DoctorBrief(id=str(doc["id"]), full_name=doc["full_name"], specialties=doc["specialties"]),
        patient=appt["patient"],
        mode=appt["mode"],
        start_at=appt["start_at"],
        end_at=appt["end_at"],
        start_ist=ist_label(appt["start_at"]),
        status=status,
        member_status=member_view_status(appt, now),
        fee_inr=appt["fee_paise"] / 100,
        amount_paid_inr=appt["amount_paid_paise"] / 100,
        refund_inr=appt["refund_paise"] / 100,
        refund_status=appt.get("refund_status"),
        razorpay_order_id=appt.get("razorpay_order_id") if pending else None,
        hold_expires_at=appt.get("hold_expires_at") if pending else None,
        video_room_url=appt.get("video_room_url") if join_open else None,
        clinic_address=appt.get("clinic_address"),
        can_reschedule=booked and now <= appt["start_at"] - timedelta(hours=s.reschedule_cutoff_hours),
        cancel_refund_percent=member_cancel_refund_percent(appt, now) if booked else None,
    )


def doctor_card(d: dict, next_at: datetime | None) -> DoctorCard:
    return DoctorCard(
        id=str(d["_id"]),
        full_name=d["full_name"],
        specialties=d["specialties"],
        qualifications=d["qualifications"],
        languages=d["languages"],
        modes=d["modes"],
        experience_years=d["experience_years"],
        fee_inr=d["fee_paise"] / 100,
        rating=rating(d),
        rating_count=d.get("rating_count", 0),
        clinic_pincode=d.get("clinic_pincode"),
        next_available_at=next_at,
        next_available_ist=ist_label(next_at) if next_at else None,
    )
