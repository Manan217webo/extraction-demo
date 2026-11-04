"""Accounts, sign-in and sessions.

Accounts live in the database (db.User): a unique login that is never reused,
a printed full name (shown on every signature), a role, and a lockout counter.
Accounts are disabled, never deleted. A legacy USERS_FILE ({"alice": "<scrypt>"})
is imported once at start-up so existing deployments keep their logins.

Roles:
    reviewer  review documents, save drafts, sign and submit to the EDC
    monitor   read-only: documents, history, audit trail and its export
    admin     users and configuration; may also review

Sessions are a random token in Redis (HttpOnly cookie). A session ends after
SESSION_IDLE_MINUTES without user activity (default 15) and in any case after
SESSION_MAX_HOURS (default 12). Background polling does not count as activity:
only writes and an explicit /api/session/ping extend a session.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("extraction.auth")

COOKIE_NAME = "webo_session"
_HASH_PREFIX = "scrypt"
ROLES = ("reviewer", "monitor", "admin")
MAX_FAILED = int(os.getenv("LOGIN_MAX_FAILED") or 5)
LOCK_MINUTES = int(os.getenv("LOGIN_LOCK_MINUTES") or 30)
MIN_PASSWORD = int(os.getenv("PASSWORD_MIN_LENGTH") or 12)


def idle_seconds() -> int:
    return max(2, int(os.getenv("SESSION_IDLE_MINUTES") or 15)) * 60


def max_seconds() -> int:
    return max(1, int(os.getenv("SESSION_MAX_HOURS") or 12)) * 3600


def is_production() -> bool:
    return (os.getenv("APP_ENV") or "").strip().lower() == "production"


# --------------------------------------------------------------------------- passwords


def hash_password(password: str, *, salt: Optional[bytes] = None) -> str:
    """stdlib scrypt — no extra dependency. Format: scrypt$n$r$p$salt$hash."""
    salt = salt or secrets.token_bytes(16)
    n, r, p = 2**14, 8, 1
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return "$".join([_HASH_PREFIX, str(n), str(r), str(p),
                     base64.b64encode(salt).decode("ascii"),
                     base64.b64encode(digest).decode("ascii")])


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
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n_s), r=int(r_s),
                                p=int(p_s), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(digest, expected)


def password_problem(password: str, username: str = "") -> Optional[str]:
    if len(password or "") < MIN_PASSWORD:
        return f"Passwords must be at least {MIN_PASSWORD} characters."
    if username and username.lower() in password.lower():
        return "The password can't contain the username."
    if len(set(password)) < 5:
        return "That password is too simple."
    return None


# --------------------------------------------------------------------------- legacy file


def users_path() -> Path:
    raw = (os.getenv("USERS_FILE") or "users.json").strip()
    path = Path(raw)
    return path if path.is_absolute() else Path(__file__).resolve().parent / path


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


async def import_legacy_users() -> int:
    """Copy users.json accounts that the database doesn't have yet. The first
    imported account becomes an admin when there is no admin at all."""
    import audit
    import db
    from sqlalchemy import select

    legacy = load_users()
    if not legacy:
        return 0
    added = 0
    async with db.session() as session:
        has_admin = (await session.execute(
            select(db.User).where(db.User.role == "admin").limit(1))).scalar() is not None
        for username, encoded in sorted(legacy.items()):
            if await session.get(db.User, username) is not None:
                continue
            role = "reviewer" if has_admin else "admin"
            has_admin = True
            session.add(db.User(username=username, full_name=username, role=role,
                                password_hash=encoded))
            await session.flush()
            await audit.record(session, "user.created", username="system", field=username,
                               new_value=role, reason="Imported from USERS_FILE")
            added += 1
        await session.commit()
    if added:
        log.info("imported %s account(s) from %s", added, users_path())
    return added


# --------------------------------------------------------------------------- accounts


class Locked(Exception):
    def __init__(self, until: dt.datetime) -> None:
        super().__init__("locked")
        self.until = until


def _iso(value: Optional[dt.datetime]) -> Optional[str]:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def user_dict(user: Any) -> dict[str, Any]:
    return {"username": user.username, "full_name": user.full_name, "role": user.role,
            "active": user.active,
            "locked_until": _iso(user.locked_until),
            "last_login_at": _iso(user.last_login_at),
            "auth_source": is_cronos(user) and "cronos" or "local",
            "cronos_user_id": getattr(user, "cronos_user_id", None)}


def is_cronos(user: Any) -> bool:
    return getattr(user, "auth_source", None) == "cronos"


# A cronos account's password_hash: not a scrypt string, so a local check never matches.
CRONOS_PASSWORD = "!cronos"


def uses_local_login(user: Optional[Any]) -> bool:
    """With Cronos sign-in on, only local admin accounts sign in here (break-glass)."""
    import cronos_auth
    if not cronos_auth.enabled():
        return True
    return (user is not None and not is_cronos(user) and user.role == "admin"
            and cronos_auth.local_admin_allowed())


async def sign_in_with_cronos(username: str, password: str, *,
                              client_ip: Optional[str] = None) -> tuple[Any, Any]:
    """(user, grant) after Cronos accepts the password. Creates or updates the local
    row: matched on the Cronos user id first, so a change of case in the login still
    finds the same person. Raises cronos_auth.CronosAuthError, audited."""
    import audit
    import cronos_auth
    import db
    from sqlalchemy import select

    try:
        grant = await cronos_auth.login(username, password)
    except cronos_auth.CronosAuthError as exc:
        await audit.record_now("auth.locked" if exc.code == "AUTH_ACCOUNT_LOCKED"
                               else "auth.login_failed",
                               username=username or "unknown", client_ip=client_ip,
                               details={"via": "cronos", "why": exc.code})
        raise
    role = cronos_auth.map_role(grant.roles)
    if role is None:
        await audit.record_now("auth.login_failed", username=username, client_ip=client_ip,
                               details={"via": "cronos", "why": "no mapped role",
                                        "cronos_roles": grant.roles})
        raise cronos_auth.CronosAuthError(
            403, "ROLE_NOT_MAPPED",
            "Your Cronos role doesn't give access to eSource. Ask an administrator.")
    async with db.session() as session:
        user = (await session.execute(select(db.User).where(
            db.User.cronos_user_id == grant.user_id).limit(1))).scalar()
        if user is None:
            user = await session.get(db.User, username)
            if user is not None and user.cronos_user_id and user.cronos_user_id != grant.user_id:
                await audit.record(session, "auth.login_failed", username=username,
                                   client_ip=client_ip,
                                   details={"via": "cronos", "why": "username linked to "
                                            "another Cronos user"})
                await session.commit()
                raise cronos_auth.CronosAuthError(
                    409, "USERNAME_TAKEN",
                    "This username belongs to a different person in eSource. "
                    "Ask an administrator.")
        if user is None:
            user = db.User(username=username, full_name=username, role=role,
                           password_hash=CRONOS_PASSWORD, auth_source="cronos",
                           cronos_user_id=grant.user_id)
            session.add(user)
            await session.flush()
            await audit.record(session, "user.created", username="system", field=username,
                               new_value=role, client_ip=client_ip,
                               reason="First sign-in through Cronos",
                               details={"cronos_user_id": grant.user_id,
                                        "cronos_roles": grant.roles})
        else:
            if not is_cronos(user) or user.cronos_user_id != grant.user_id:
                await audit.record(session, "user.updated", username="system",
                                   field=f"{user.username}.auth_source",
                                   old_value=user.auth_source or "local", new_value="cronos",
                                   client_ip=client_ip, reason="Signed in through Cronos",
                                   details={"cronos_user_id": grant.user_id})
                user.auth_source, user.cronos_user_id = "cronos", grant.user_id
                user.password_hash = CRONOS_PASSWORD
            if user.role != role:
                await audit.record(session, "user.updated", username="system",
                                   field=f"{user.username}.role", old_value=user.role,
                                   new_value=role, client_ip=client_ip,
                                   reason="Role follows Cronos",
                                   details={"cronos_roles": grant.roles})
                user.role = role
        if not user.active:
            await audit.record(session, "auth.login_failed", username=user.username,
                               client_ip=client_ip,
                               details={"via": "cronos", "why": "disabled in eSource"})
            await session.commit()
            raise cronos_auth.CronosAuthError(
                403, "DISABLED", "Your eSource access is turned off. Ask an administrator.")
        user.last_login_at = db.utcnow()
        user.failed_logins, user.locked_until = 0, None
        await audit.record(session, "auth.login", username=user.username, client_ip=client_ip,
                           details={"via": "cronos", "cronos_user_id": grant.user_id,
                                    "cronos_roles": grant.roles})
        await session.commit()
        return user, grant


async def confirm_signature(user: Any, password: str, *, client_ip: Optional[str] = None
                            ) -> tuple[bool, Optional[Any]]:
    """Re-authentication at the moment of signing. (ok, fresh Cronos grant or None).

    Cronos accounts are checked by Cronos, whose lockout then applies; local
    accounts by the local password and lockout. Raises Locked for a locked local
    account and cronos_auth.CronosAuthError when Cronos can't decide."""
    import audit
    import cronos_auth

    if not is_cronos(user):
        ok = await authenticate(user.username, password, client_ip=client_ip,
                                purpose="signature")
        return ok is not None, None
    try:
        grant = await cronos_auth.login(user.username, password)
    except cronos_auth.CronosAuthError as exc:
        if exc.code in ("AUTH_INVALID_CREDENTIALS", "AUTH_ACCOUNT_LOCKED"):
            await audit.record_now("signature.failed", username=user.username,
                                   client_ip=client_ip,
                                   details={"via": "cronos", "why": exc.code})
        if exc.code == "AUTH_INVALID_CREDENTIALS":
            return False, None
        raise
    if grant.user_id != user.cronos_user_id:
        await audit.record_now("signature.failed", username=user.username, client_ip=client_ip,
                               details={"via": "cronos", "why": "different Cronos user"})
        return False, None
    return True, grant


async def authenticate(username: str, password: str, *, client_ip: Optional[str] = None,
                       purpose: str = "login") -> Optional[Any]:
    """The user when the password is right; None when not; Locked when locked.

    Every failure counts towards the lockout, including a wrong password at a
    signature. Every outcome is audited.
    """
    import audit
    import db

    username = (username or "").strip()
    async with db.session() as session:
        user = await session.get(db.User, username) if username else None
        now = db.utcnow()
        if user is None or not user.active:
            hash_password(password or "")  # same cost as a real check
            await audit.record(session, "auth.login_failed" if purpose == "login" else "signature.failed",
                               username=username or "unknown", client_ip=client_ip,
                               details={"why": "unknown or disabled account", "purpose": purpose})
            await session.commit()
            return None
        if user.locked_until and user.locked_until > now:
            raise Locked(user.locked_until)
        if verify_password(password or "", user.password_hash):
            user.failed_logins = 0
            user.locked_until = None
            if purpose == "login":
                user.last_login_at = now
                await audit.record(session, "auth.login", username=user.username,
                                   client_ip=client_ip)
            await session.commit()
            return user
        # Atomic, so simultaneous wrong guesses can't slip under the lockout.
        from sqlalchemy import update
        attempts = (await session.execute(
            update(db.User).where(db.User.username == user.username)
            .values(failed_logins=db.User.failed_logins + 1)
            .returning(db.User.failed_logins))).scalar()
        user.failed_logins = attempts
        details = {"attempt": attempts, "purpose": purpose}
        await audit.record(session, "auth.login_failed" if purpose == "login" else "signature.failed",
                           username=user.username, client_ip=client_ip, details=details)
        if user.failed_logins >= MAX_FAILED:
            user.locked_until = now + dt.timedelta(minutes=LOCK_MINUTES)
            user.failed_logins = 0
            await audit.record(session, "auth.locked", username=user.username,
                               client_ip=client_ip,
                               details={"until": user.locked_until.isoformat()})
            await session.commit()
            raise Locked(user.locked_until)
        await session.commit()
        return None


async def get_user(username: str) -> Optional[Any]:
    import db
    async with db.session() as session:
        return await session.get(db.User, username)


# --------------------------------------------------------------------------- sessions


def cookie_kwargs() -> dict[str, Any]:
    return {"key": COOKIE_NAME, "httponly": True, "samesite": "strict",
            "secure": is_production(), "max_age": max_seconds(), "path": "/"}


async def create_session(redis, username: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    await redis.set(f"session:{token}",
                    json.dumps({"u": username, "created": now, "seen": now}),
                    ex=idle_seconds())
    return token


async def destroy_session(redis, token: Optional[str]) -> None:
    if token:
        await redis.delete(f"session:{token}")


def session_ref(token: Optional[str]) -> Optional[str]:
    """A stable, non-secret reference to a session for the audit trail."""
    return hashlib.sha256(token.encode()).hexdigest()[:16] if token else None


async def session_info(redis, token: Optional[str], *, touch: bool) -> Optional[dict[str, Any]]:
    """{"u", "created", "expires_in"} for a live session; touching extends it."""
    if not token:
        return None
    raw = await redis.get(f"session:{token}")
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        data = json.loads(raw)
    except ValueError:
        # A session from before sessions carried a creation time.
        data = {"u": raw, "created": time.time()}
    now = time.time()
    remaining_max = data["created"] + max_seconds() - now
    if remaining_max <= 0:
        await redis.delete(f"session:{token}")
        return None
    if touch:
        await redis.set(f"session:{token}", json.dumps({**data, "seen": now}),
                        ex=int(min(idle_seconds(), remaining_max)) or 1)
        ttl = min(idle_seconds(), remaining_max)
    else:
        ttl = await redis.ttl(f"session:{token}")
    return {**data, "expires_in": int(ttl)}


async def user_from_token(redis, token: Optional[str], *, touch: bool = False) -> Optional[str]:
    info = await session_info(redis, token, touch=touch)
    return info["u"] if info else None
