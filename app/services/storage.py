"""Private document storage with short-lived signed links.

Local-disk implementation. To move to S3 or GCS, keep the same three functions
and return the bucket's presigned URL from signed_url().
"""

import hashlib
import hmac
import time
import uuid
from pathlib import Path

from app.config import get_settings

ALLOWED_TYPES = {"application/pdf", "image/jpeg", "image/png"}
MAX_BYTES = 10 * 1024 * 1024


def _root() -> Path:
    root = Path(get_settings().storage_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def save(content: bytes, suffix: str = "") -> str:
    key = f"{uuid.uuid4().hex}{suffix}"
    (_root() / key).write_bytes(content)
    return key


def read(key: str) -> bytes:
    path = (_root() / key).resolve()
    if _root().resolve() not in path.parents:
        raise FileNotFoundError(key)
    return path.read_bytes()


def _sig(key: str, exp: int) -> str:
    secret = get_settings().jwt_secret.encode()
    return hmac.new(secret, f"{key}:{exp}".encode(), hashlib.sha256).hexdigest()


def signed_url(key: str) -> str:
    exp = int(time.time()) + get_settings().signed_url_seconds
    return f"/files/{key}?exp={exp}&sig={_sig(key, exp)}"


def check_signature(key: str, exp: int, sig: str) -> bool:
    return exp >= int(time.time()) and hmac.compare_digest(sig, _sig(key, exp))
