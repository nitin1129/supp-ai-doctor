"""Video rooms, one per appointment.

Jitsi needs no API call: a long random room name is the access control. For
Daily or Twilio, implement create_room() against their API and set
SUPPAI_VIDEO_PROVIDER.
"""

import secrets

from app.config import get_settings


def create_room(appointment_id: str) -> str:
    s = get_settings()
    if s.video_provider == "jitsi":
        return f"{s.jitsi_base_url}/suppai-{appointment_id}-{secrets.token_urlsafe(12)}"
    raise NotImplementedError(f"Video provider {s.video_provider!r} is not configured")
