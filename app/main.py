from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response

from app.config import get_settings
from app.db import get_db
from app.models import ensure_indexes
from app.routers import admin, applications, auth, doctor, members, payments
from app.services import storage

_s = get_settings()
_DEV_KEY = "Zm9yLWRldi1vbmx5LWNoYW5nZS1tZS0zMi1ieXRlcyE="
if _s.env == "production" and (_s.jwt_secret == "change-me" or _s.data_encryption_key == _DEV_KEY):
    raise RuntimeError("Set SUPPAI_JWT_SECRET and SUPPAI_DATA_ENCRYPTION_KEY before running in production")


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes(get_db())  # idempotent; the unique indexes are what prevent double bookings
    yield


app = FastAPI(
    title="SuppAI Doctors API",
    version="1.1.0",
    description="Doctor onboarding, doctor dashboard and member consult booking for suppai.in (MongoDB).",
    lifespan=lifespan,
)

for r in (applications.router, admin.router, auth.router, doctor.router, members.router, payments.router):
    app.include_router(r)


@app.get("/health", tags=["Ops"])
def health() -> dict:
    get_db().command("ping")
    return {"ok": True}


@app.get("/files/{key}", tags=["Ops"], include_in_schema=False)
def signed_file(key: str, exp: int = Query(), sig: str = Query()) -> Response:
    """Serves private documents through short-lived signed links."""
    if not storage.check_signature(key, exp, sig):
        raise HTTPException(403, "Link expired")
    try:
        content = storage.read(key)
    except FileNotFoundError:
        raise HTTPException(404, "Not found")
    media = "application/pdf" if key.endswith(".pdf") else "image/png" if key.endswith(".png") else "image/jpeg"
    return Response(content, media_type=media, headers={"Cache-Control": "private, no-store"})
