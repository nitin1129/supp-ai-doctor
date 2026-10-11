from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SUPPAI_", extra="ignore")

    env: str = "dev"
    database_url: str = "postgresql+psycopg://suppai:suppai@localhost:5432/suppai"

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

    # Video
    video_provider: str = "jitsi"
    jitsi_base_url: str = "https://meet.jit.si"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()
