"""Redis-backed job meta, parse cache, sessions, and distributed locks.

Job meta (owner, visit_definition, header_rows, …) lives at job:{id}.
The heavy parse payload lives separately at job:{id}:parse so a meta write
does not rewrite megabytes of layout. Every key carries a TTL (default 7 days).
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from redis.asyncio import Redis
from redis.asyncio.lock import Lock

log = logging.getLogger("extraction.store")

_client: Optional[Redis] = None


def redis_url() -> str:
    return (os.getenv("REDIS_URL") or "redis://localhost:6379/0").strip()


def job_ttl_seconds() -> int:
    days = int(os.getenv("JOB_TTL_DAYS") or 7)
    return max(1, days) * 24 * 60 * 60


def get_redis() -> Redis:
    global _client
    if _client is None:
        _client = Redis.from_url(redis_url(), decode_responses=True)
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def ping() -> bool:
    try:
        return bool(await get_redis().ping())
    except Exception as exc:
        log.warning("redis ping failed: %s", exc)
        return False


def _job_key(job_id: str) -> str:
    return f"job:{job_id}"


def _parse_key(job_id: str) -> str:
    return f"job:{job_id}:parse"


async def get_job(job_id: str) -> Optional[dict[str, Any]]:
    raw = await get_redis().get(_job_key(job_id))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def put_job(job_id: str, meta: dict[str, Any]) -> None:
    await get_redis().set(
        _job_key(job_id),
        json.dumps(meta, ensure_ascii=False, default=str),
        ex=job_ttl_seconds(),
    )


async def get_parse(job_id: str) -> Optional[dict[str, Any]]:
    raw = await get_redis().get(_parse_key(job_id))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def put_parse(job_id: str, parse: dict[str, Any]) -> None:
    await get_redis().set(
        _parse_key(job_id),
        json.dumps(parse, ensure_ascii=False, default=str),
        ex=job_ttl_seconds(),
    )


@asynccontextmanager
async def lock(name: str, *, timeout: float = 300.0) -> AsyncIterator[Lock]:
    """Distributed lock. timeout is how long the lock is held before Redis frees it."""
    redis = get_redis()
    held = redis.lock(f"lock:{name}", timeout=timeout, blocking_timeout=timeout)
    await held.acquire()
    try:
        yield held
    finally:
        try:
            await held.release()
        except Exception:
            # Lock may have already expired; never fail the request on release.
            pass


async def audit_save(record: dict[str, Any]) -> None:
    """Append a save audit row. Newest first; keep the last 1000."""
    redis = get_redis()
    await redis.lpush("audit:saves", json.dumps(record, ensure_ascii=False, default=str))
    await redis.ltrim("audit:saves", 0, 999)
