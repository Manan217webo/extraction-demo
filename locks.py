"""Record locks: one person edits a record at a time.

Opening a record for review takes its lock; the review screen renews it every
RECORD_LOCK_RENEW_SECONDS while it stays open, and gives it back on close. A
lock nobody renews lapses after RECORD_LOCK_SECONDS, so a closed laptop frees
the record by itself. Anyone else who opens the record meanwhile gets it
read-only, with who has it and since when. An administrator can release a lock.

The lock is `SET NX` in Redis, so two people can never both take it. The same
person may hold it from two tabs (the tab lease in the browser handles that).
Every write to a record (draft, preflight, submit, reprocess, archive) checks
it on the server, not only the screen.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any, Optional

import store


def lock_seconds() -> int:
    return max(30, int(os.getenv("RECORD_LOCK_SECONDS") or 120))


def renew_seconds() -> int:
    return max(10, min(lock_seconds() // 3, int(os.getenv("RECORD_LOCK_RENEW_SECONDS") or 40)))


def _key(document_id: str) -> str:
    return f"edit:{document_id}"


def _decode(raw: Any) -> Optional[dict[str, Any]]:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("username") else None


async def holder(document_id: str) -> Optional[dict[str, Any]]:
    return _decode(await store.get_redis().get(_key(document_id)))


async def holders(document_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not document_ids:
        return {}
    raw = await store.get_redis().mget([_key(i) for i in document_ids])
    return {i: h for i, h in zip(document_ids, (_decode(r) for r in raw)) if h}


async def acquire(document_id: str, username: str, full_name: Optional[str]
                  ) -> tuple[bool, dict[str, Any]]:
    """(True, lock) when this person holds the lock now; (False, theirs) otherwise."""
    redis = store.get_redis()
    mine = {"username": username, "full_name": full_name or username,
            "since": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")}
    if await redis.set(_key(document_id), json.dumps(mine), ex=lock_seconds(), nx=True):
        return True, mine
    current = await holder(document_id)
    if current is None:
        # Lapsed between the two calls; try once more.
        if await redis.set(_key(document_id), json.dumps(mine), ex=lock_seconds(), nx=True):
            return True, mine
        current = await holder(document_id) or mine
    if current["username"] != username:
        return False, current
    # Ours already: renew, keeping when it was first taken.
    await redis.set(_key(document_id), json.dumps(current), ex=lock_seconds())
    return True, current


async def release(document_id: str, username: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Give the lock back. With a username, only if that person holds it.
    Returns the lock that was released, or None."""
    current = await holder(document_id)
    if current is None or (username and current["username"] != username):
        return None
    await store.get_redis().delete(_key(document_id))
    return current


def public(lock: Optional[dict[str, Any]], username: str) -> Optional[dict[str, Any]]:
    if not lock:
        return None
    return {"username": lock["username"], "full_name": lock.get("full_name") or lock["username"],
            "since": lock.get("since"), "mine": lock["username"] == username,
            "renew_seconds": renew_seconds()}
