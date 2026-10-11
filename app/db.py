"""MongoDB connection and small helpers.

The code uses only single-document atomic operations (no multi-document
transactions), so it runs on a standalone mongod, a replica set or Atlas.
Consistency comes from unique indexes, conditional updates on `status`, and
a short per-doctor lease (see services.locks).
"""

from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import HTTPException
from pymongo import MongoClient
from pymongo.database import Database

from app.config import get_settings


@lru_cache
def get_client() -> MongoClient:
    s = get_settings()
    if s.mongo_url.startswith("mongomock://"):
        import mongomock

        return mongomock.MongoClient(tz_aware=True)
    return MongoClient(s.mongo_url, tz_aware=True)


def get_db() -> Database:
    return get_client()[get_settings().mongo_db]


def now() -> datetime:
    """Current UTC time truncated to milliseconds, MongoDB's precision."""
    t = datetime.now(UTC)
    return t.replace(microsecond=t.microsecond // 1000 * 1000)


def oid(value: str | ObjectId, what: str = "Record") -> ObjectId:
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise HTTPException(404, f"{what} not found")


def sid(doc: dict[str, Any] | None) -> str | None:
    return str(doc["_id"]) if doc else None


def next_sequence(db: Database, name: str) -> int:
    from pymongo import ReturnDocument

    doc = db.counters.find_one_and_update(
        {"_id": name}, {"$inc": {"seq": 1}}, upsert=True, return_document=ReturnDocument.AFTER
    )
    return doc["seq"]
