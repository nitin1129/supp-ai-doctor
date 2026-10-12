import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class PermanentFailure(Exception):
    pass


def send_email(to: str, name: str, subject: str, text: str) -> None:
    s = get_settings()
    if s.email_provider == "log":
        logger.info("EMAIL to=%s name=%r subject=%r\n%s", to, name, subject, text)
        return
    html = "<br>".join(line for line in text.splitlines())
    payload = {
        "sender": {"name": s.email_from_name, "email": s.email_from},
        "to": [{"email": to, "name": name}],
        "subject": subject,
        "textContent": text,
        "htmlContent": f"<p>{html}</p>",
    }
    r = httpx.post(
        "https://api.brevo.com/v3/smtp/email",
        json=payload,
        headers={"api-key": s.brevo_api_key, "Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code in (400, 401, 403):
        raise PermanentFailure(f"Brevo permanent error {r.status_code}: {r.text}")
    r.raise_for_status()
