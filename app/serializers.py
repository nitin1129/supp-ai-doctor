from datetime import UTC, datetime, timedelta

from app.config import get_settings
from app.models import Appointment, AppointmentStatus, Doctor
from app.schemas.booking import AppointmentOut, DoctorBrief, DoctorCard, ist_label
from app.services.appointments import member_cancel_refund_percent, member_view_status


def rating(d: Doctor) -> float | None:
    return round(d.rating_sum / d.rating_count, 1) if d.rating_count else None


def appointment_out(appt: Appointment, now: datetime | None = None) -> AppointmentOut:
    s = get_settings()
    now = now or datetime.now(UTC)
    booked = appt.status == AppointmentStatus.BOOKED
    join_open = booked and now >= appt.start_at - timedelta(minutes=s.video_room_lead_minutes)
    return AppointmentOut(
        id=appt.id,
        doctor=DoctorBrief(id=appt.doctor.id, full_name=appt.doctor.full_name, specialties=appt.doctor.specialties),
        patient=appt.patient,
        mode=appt.mode,
        start_at=appt.start_at,
        end_at=appt.end_at,
        start_ist=ist_label(appt.start_at),
        status=appt.status,
        member_status=member_view_status(appt, now),
        fee_inr=appt.fee_paise / 100,
        amount_paid_inr=appt.amount_paid_paise / 100,
        refund_inr=appt.refund_paise / 100,
        refund_status=appt.refund_status,
        razorpay_order_id=appt.razorpay_order_id if appt.status == AppointmentStatus.PENDING_PAYMENT else None,
        hold_expires_at=appt.hold_expires_at if appt.status == AppointmentStatus.PENDING_PAYMENT else None,
        video_room_url=appt.video_room_url if join_open else None,
        clinic_address=appt.clinic_address,
        can_reschedule=booked and now <= appt.start_at - timedelta(hours=s.reschedule_cutoff_hours),
        cancel_refund_percent=member_cancel_refund_percent(appt, now) if booked else None,
    )


def doctor_card(d: Doctor, next_at: datetime | None) -> DoctorCard:
    return DoctorCard(
        id=d.id,
        full_name=d.full_name,
        specialties=d.specialties,
        qualifications=d.qualifications,
        languages=d.languages,
        modes=d.modes,
        experience_years=d.experience_years,
        fee_inr=d.fee_paise / 100,
        rating=rating(d),
        rating_count=d.rating_count,
        clinic_pincode=d.clinic_pincode,
        next_available_at=next_at,
        next_available_ist=ist_label(next_at) if next_at else None,
    )
