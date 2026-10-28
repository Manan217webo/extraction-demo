"""Per-user login — passwords in a JSON file, sessions in Redis.

Users come from USERS_FILE (default users.json) as {"alice": "<scrypt hash>"}.
A successful login stores a random token in Redis under session:{token} with a
12-hour TTL; the browser holds that token in an HttpOnly cookie.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("extraction.auth")

COOKIE_NAME = "webo_session"
SESSION_TTL = 12 * 60 * 60
_HASH_PREFIX = "scrypt"


def is_production() -> bool:
    return (os.getenv("APP_ENV") or "").strip().lower() == "production"


def users_path() -> Path:
    raw = (os.getenv("USERS_FILE") or "users.json").strip()
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path


def load_users() -> dict[str, str]:
    path = users_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("users file unreadable: %s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if k and v}


def save_users(users: dict[str, str]) -> None:
    path = users_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(users, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def hash_password(password: str, *, salt: Optional[bytes] = None) -> str:
    """stdlib scrypt — no extra dependency. Format: scrypt$n$r$p$salt$hash."""
    salt = salt or secrets.token_bytes(16)
    n, r, p = 2**14, 8, 1
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return "$".join([
        _HASH_PREFIX,
        str(n),
        str(r),
        str(p),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    ])


def verify_password(password: str, encoded: str) -> bool:
    try:
        prefix, n_s, r_s, p_s, salt_b64, hash_b64 = encoded.split("$", 5)
    except ValueError:
        return False
    if prefix != _HASH_PREFIX:
        return False
    try:
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected = base64.b64decode(hash_b64.encode("ascii"))
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_s),
            r=int(r_s),
            p=int(p_s),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(digest, expected)


def authenticate(username: str, password: str) -> bool:
    users = load_users()
    encoded = users.get(username.strip())
    if not encoded:
        # Constant-time-ish miss: still hash something so timing does not leak.
        hash_password(password)
        return False
    return verify_password(password, encoded)


def cookie_kwargs() -> dict[str, Any]:
    return {
        "key": COOKIE_NAME,
        "httponly": True,
        "samesite": "lax",
        "secure": is_production(),
        "max_age": SESSION_TTL,
        "path": "/",
    }


async def create_session(redis, username: str) -> str:
    token = secrets.token_urlsafe(32)
    await redis.set(f"session:{token}", username, ex=SESSION_TTL)
    return token


async def destroy_session(redis, token: Optional[str]) -> None:
    if token:
        await redis.delete(f"session:{token}")


async def user_from_token(redis, token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    username = await redis.get(f"session:{token}")
    if username is None:
        return None
    if isinstance(username, bytes):
        username = username.decode("utf-8")
    # Sliding expiry: keep active reviewers signed in.
    await redis.expire(f"session:{token}", SESSION_TTL)
    return username
