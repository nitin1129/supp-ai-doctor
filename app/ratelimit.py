"""Per-IP rate limits on the public application and doctor sign-in endpoints (slowapi).

Limits are read from settings on every request, e.g. SUPPAI_RATE_LIMIT_LOGIN="5/minute".
Counters live in SUPPAI_RATE_LIMIT_STORAGE_URI: "memory://" is per process, so
use a shared store (for example a mongodb:// or redis:// URI) when running
more than one API process.
"""

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

from app.config import get_settings
from app.deps import client_ip

_s = get_settings()

limiter = Limiter(
    key_func=client_ip,
    storage_uri=_s.rate_limit_storage_uri,
    enabled=_s.rate_limit_enabled,
)


def applications_limit() -> str:
    return get_settings().rate_limit_applications


def login_limit() -> str:
    return get_settings().rate_limit_login


def forgot_password_limit() -> str:
    return get_settings().rate_limit_forgot_password


def rate_limit_exceeded(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    # Same {"detail": ...} shape as the API's other errors.
    return JSONResponse({"detail": "Too many requests. Please wait a minute and try again."}, status_code=429)
