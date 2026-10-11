"""Per-doctor lease so a booking's "is this slot free?" check and its insert
can't interleave with another booking, a day off or a suspension.

The unique index on (doctor_id, start_at) already stops two bookings at the
same start. The lease additionally covers overlaps when a doctor changes slot
length, and check-then-act flows like "day off with bookings".
"""

import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

from bson import ObjectId
from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.database import Database

from app.db import now

LEASE_SECONDS = 10
WAIT_SECONDS = 5


@contextmanager
def doctor_lock(db: Database, doctor_id: ObjectId) -> Iterator[dict]:
    token = secrets.token_hex(8)
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        t = now()
        doc = db.doctors.find_one_and_update(
            {"_id": doctor_id, "$or": [{"lock_until": None}, {"lock_until": {"$lt": t}}]},
            {"$set": {"lock_until": t + timedelta(seconds=LEASE_SECONDS), "lock_token": token}},
            return_document=ReturnDocument.AFTER,
        )
        if doc is not None:
            break
        if db.doctors.count_documents({"_id": doctor_id}, limit=1) == 0:
            raise HTTPException(404, "Doctor not found")
        if time.monotonic() > deadline:
            raise HTTPException(409, "Busy, please try again")
        time.sleep(0.05)
    try:
        yield doc
    finally:
        db.doctors.update_one({"_id": doctor_id, "lock_token": token}, {"$set": {"lock_until": None, "lock_token": None}})
