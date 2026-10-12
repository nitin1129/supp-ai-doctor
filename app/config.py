from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SUPPAI_", extra="ignore")

    env: str = "dev"
    mongo_url: str = "mongodb://localhost:27017"  # "mongomock://" for an in-memory test database
    mongo_db: str = "suppai"

    # Auth
    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60 * 12
    invite_link_hours: int = 24
    login_max_failures: int = 5
    login_lock_minutes: int = 15

    # Field-level encryption for PAN and bank account (Fernet key, urlsafe base64, 32 bytes)
    data_encryption_key: str = "Zm9yLWRldi1vbmx5LWNoYW5nZS1tZS0zMi1ieXRlcyE="

    # URLs used in emails
    public_site_url: str = "https://suppai.in"

    # Private document storage
    storage_dir: str = "./storage/private"
    signed_url_seconds: int = 300

    # Booking rules
    timezone: str = "Asia/Kolkata"
    default_slot_minutes: int = 15
    default_notice_minutes: int = 120
    booking_window_days: int = 30
    slot_hold_minutes: int = 5
    reschedule_cutoff_hours: int = 4
    cancel_full_refund_cutoff_hours: int = 4
    video_room_lead_minutes: int = 10
    reminder_offsets_minutes: list[int] = [24 * 60, 60]
    unmarked_review_after_hours: int = 24

    # Money
    commission_percent: float = 15.0
    patient_no_show_refund_percent: int = 0

    # Razorpay
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = "dev-webhook-secret"
    payments_provider: str = "fake"  # "razorpay" in production

    # Doctor payouts (RazorpayX uses the Razorpay key id/secret above)
    payouts_provider: str = "fake"  # "razorpayx" in production
    razorpayx_account_number: str = ""  # the RazorpayX business account payouts are sent from
    razorpayx_payout_mode: str = "IMPS"  # IMPS | NEFT | RTGS

    # Video
    video_provider: str = "jitsi"
    jitsi_base_url: str = "https://meet.jit.si"

    # Email
    email_provider: str = "log"  # "log" or "brevo"
    brevo_api_key: str = ""
    email_from: str = "no-reply@suppai.in"
    email_from_name: str = "SuppAI"

    # OTP
    otp_ttl_minutes: int = 10
    otp_max_attempts: int = 5
    otp_resend_seconds: int = 60
    otp_max_per_email_hour: int = 5
    otp_max_per_ip_hour: int = 20
    otp_max_verify_per_ip_15min: int = 10
    member_otp_signup: bool = True
    members_collection: str = "users"
    member_email_field: str = "email"
    member_name_field: str = "name"

    # Member contact lookup for notifications: "memory" (dev/tests) or "platform"
    member_contacts_provider: str = "memory"

    # Rate limits per client IP (slowapi / limits syntax, e.g. "10/minute")
    rate_limit_enabled: bool = True
    rate_limit_storage_uri: str = "memory://"
    rate_limit_applications: str = "10/minute"  # POST /doctors/applications and …/{id}/submit
    rate_limit_login: str = "5/minute"  # POST /auth/doctor/login
    rate_limit_forgot_password: str = "5/minute"  # POST /auth/doctor/forgot-password

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()
