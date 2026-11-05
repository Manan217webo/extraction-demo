"""Sign-in and duty checks against Cronos EDC.

With AUTH_MODE=cronos, people sign into eSource with their Cronos username and
password. Cronos is the source of identity, roles and duties; eSource keeps a
local account row for each person (for names on signatures, the audit trail and
disabling access here) but never their password.

    POST /api/auth/login     username + password -> access token (15 min) + refresh token
    POST /api/auth/refresh   refresh token -> new pair; the old refresh token is spent
    POST /api/auth/unlock    admin token + username
    GET  /api/duty-delegation/effective-rights
                             does user X hold duty D for study S now, directly or by delegation?

The Cronos tokens are held server-side in Redis next to the eSource session
(`cronos:<session token>`), never sent to the browser. They are refreshed under a
Redis lock: Cronos revokes the whole session when a refresh token is reused, so
two requests must never refresh the same one.

Duty checks fail closed: if Cronos can't answer, the action is refused.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import httpx

log = logging.getLogger("extraction.cronos_auth")

TIMEOUT = float(os.getenv("CRONOS_AUTH_TIMEOUT_SECONDS") or 20)
REFRESH_MARGIN = 60  # refresh when the access token has less than this many seconds left
RIGHTS_CACHE_SECONDS = float(os.getenv("CRONOS_RIGHTS_CACHE_SECONDS") or 60)

# Tests swap in an httpx.MockTransport here.
_transport: Optional[httpx.AsyncBaseTransport] = None


# --------------------------------------------------------------------------- configuration


def enabled() -> bool:
    return (os.getenv("AUTH_MODE") or "local").strip().lower() == "cronos"


def base_url() -> str:
    explicit = (os.getenv("CRONOS_AUTH_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    import edc
    return edc.base_url()


def client_code() -> str:
    return (os.getenv("CRONOS_CLIENT_CODE") or "").strip()


def local_admin_allowed() -> bool:
    """Local admin accounts keep signing in locally: the way in when Cronos is down."""
    return (os.getenv("CRONOS_LOCAL_ADMIN_LOGIN") or "true").strip().lower() != "false"


def enforce_duties() -> bool:
    return enabled() and (os.getenv("CRONOS_ENFORCE_DUTIES") or "true").strip().lower() != "false"


def edc_as_user() -> bool:
    """Send EDC calls made for a person with that person's Cronos token."""
    return enabled() and (os.getenv("EDC_AS_USER") or "true").strip().lower() != "false"


def duty_for(action: str) -> Optional[str]:
    """The Cronos duty code an eSource action needs. "review" = saving review work,
    "submit" = signing and sending to the EDC. Empty = not checked."""
    defaults = {"review": "CRF_REVIEW", "submit": "CRF_REVIEW"}
    raw = os.getenv(f"CRONOS_DUTY_{action.upper()}")
    code = (defaults.get(action, "") if raw is None else raw).strip()
    return code or None


_RANK = {"monitor": 1, "reviewer": 2, "admin": 3}
# Cronos's own role names (as the dev deployment returns them), plus the short
# forms in its API guide. Anything not listed falls to CRONOS_DEFAULT_ROLE.
_DEFAULT_ROLE_MAP = {
    "super admin": "admin", "application administrator": "admin",
    "admin": "admin", "administrator": "admin",
    "investigators": "reviewer", "investigator": "reviewer",
    "clinical research coordinator": "reviewer", "data entry operator": "reviewer",
    "data manager": "reviewer", "data coordinator": "reviewer",
    "clinical research associate": "monitor", "medical monitor": "monitor",
}


def role_map() -> dict[str, str]:
    raw = (os.getenv("CRONOS_ROLE_MAP") or "").strip()
    if not raw:
        return dict(_DEFAULT_ROLE_MAP)
    try:
        data = json.loads(raw)
    except ValueError:
        log.error("CRONOS_ROLE_MAP is not valid JSON; using the default map")
        return dict(_DEFAULT_ROLE_MAP)
    return {str(k).strip().lower(): str(v).strip().lower() for k, v in data.items()
            if str(v).strip().lower() in _RANK}


def map_role(roles: list[str]) -> Optional[str]:
    """The highest eSource role any of the person's Cronos roles maps to. A person
    with no mapped role gets CRONOS_DEFAULT_ROLE (monitor, read-only, unless set);
    "none" there refuses them."""
    mapping = role_map()
    mapped = [mapping[r.strip().lower()] for r in roles or [] if r and r.strip().lower() in mapping]
    if mapped:
        return max(mapped, key=_RANK.__getitem__)
    default = (os.getenv("CRONOS_DEFAULT_ROLE") or "monitor").strip().lower()
    return default if default in _RANK else None


# --------------------------------------------------------------------------- errors


class CronosAuthError(Exception):
    """Something a person can read. `status` is what eSource answers the browser."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


UNREACHABLE = "We couldn't reach Cronos. Please try again in a moment."
SESSION_ENDED = "Your Cronos session has ended. Please sign in again."

_LOGIN_ERRORS = {
    "AUTH_INVALID_CREDENTIALS": (401, "Incorrect username or password."),
    "AUTH_ACCOUNT_LOCKED": (423, "Your Cronos account is locked after too many attempts. "
                                 "Ask an administrator to unlock it."),
    "AUTH_ACCOUNT_INACTIVE": (403, "Your Cronos account is inactive. Ask an administrator."),
    "AUTH_ACCOUNT_EXPIRED": (403, "Your Cronos account has expired. Ask an administrator."),
    "AUTH_RATE_LIMITED": (429, "Too many sign-in attempts. Wait a few minutes and try again."),
    "AUTH_PASSWORD_RESET_REQUIRED": (403, "Your Cronos password has to be changed before you "
                                          "can sign in. Change it in Cronos, then sign in here."),
    "AUTH_HTTPS_REQUIRED": (502, "Cronos only accepts HTTPS. Ask an administrator to check "
                                 "CRONOS_AUTH_URL."),
    "AUTH_REQUEST_INVALID": (400, "Enter your username and password."),
}


# --------------------------------------------------------------------------- HTTP


async def _call(method: str, path: str, *, token: Optional[str] = None,
                json_body: Optional[dict[str, Any]] = None,
                params: Optional[dict[str, Any]] = None) -> tuple[int, dict[str, Any]]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False,
                                     transport=_transport) as http:
            response = await http.request(method, f"{base_url()}{path}", headers=headers,
                                          json=json_body, params=params)
    except httpx.HTTPError as exc:
        log.warning("cronos %s %s failed: %s", method, path, exc)
        raise CronosAuthError(503, "CRONOS_UNREACHABLE", UNREACHABLE) from exc
    if response.is_redirect:
        # Cronos (IIS) answers some failures with a redirect to an HTML page
        # rather than JSON: 401.html for a spent or invalid token, 403.html for
        # a refusal, error.html / 404.html when the request failed on its side.
        page = (response.headers.get("location") or "").rsplit("/", 1)[-1].lower()
        log.warning("cronos %s %s redirected to %s", method, path, page or "?")
        if page.startswith("401"):
            return 401, {"errorCode": "AUTH_TOKEN_INVALID"}
        if page.startswith("403"):
            return 403, {"errorCode": "AUTH_FORBIDDEN", "messageCode": "DD_FORBIDDEN"}
        raise CronosAuthError(502, "CRONOS_ERROR",
                              "Cronos had an error with this request. Please try again, and "
                              "tell an administrator if it keeps happening.")
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return response.status_code, body


# --------------------------------------------------------------------------- tokens


@dataclass
class Grant:
    access_token: str
    refresh_token: str
    expires_at: float  # epoch seconds
    user_id: str
    roles: list[str] = field(default_factory=list)
    assignments: list[dict[str, Any]] = field(default_factory=list)


def _grant(body: dict[str, Any], previous: Optional[Grant] = None) -> Grant:
    expires_in = int(body.get("expiresIn") or 900)
    return Grant(access_token=str(body["accessToken"]),
                 refresh_token=str(body.get("refreshToken") or ""),
                 expires_at=time.time() + expires_in,
                 user_id=str(body.get("userId") or (previous.user_id if previous else "")),
                 roles=list(body.get("roles") or (previous.roles if previous else [])),
                 assignments=list(body.get("assignments")
                                  or (previous.assignments if previous else [])))


async def login(username: str, password: str) -> Grant:
    status, body = await _call("POST", "/api/auth/login",
                               json_body={"username": username, "password": password})
    code = str(body.get("errorCode") or "")
    if status == 200 and (body.get("passwordResetRequired") or code == "AUTH_PASSWORD_RESET_REQUIRED"):
        code = "AUTH_PASSWORD_RESET_REQUIRED"
    elif status == 200 and body.get("accessToken"):
        if not body.get("userId"):
            raise CronosAuthError(502, "CRONOS_BAD_RESPONSE",
                                  "Cronos signed you in but didn't say who you are.")
        return _grant(body)
    if code not in _LOGIN_ERRORS:
        code = {401: "AUTH_INVALID_CREDENTIALS", 429: "AUTH_RATE_LIMITED"}.get(status, code)
    if code in _LOGIN_ERRORS:
        http_status, message = _LOGIN_ERRORS[code]
        raise CronosAuthError(http_status, code, message)
    if status >= 500:
        raise CronosAuthError(503, "CRONOS_UNAVAILABLE", UNREACHABLE)
    raise CronosAuthError(502, "CRONOS_BAD_RESPONSE",
                          f"Cronos answered the sign-in with {status}.")


async def refresh(previous: Grant) -> Grant:
    status, body = await _call("POST", "/api/auth/refresh",
                               json_body={"refreshToken": previous.refresh_token})
    if status == 200 and body.get("accessToken"):
        return _grant(body, previous)
    if status in (400, 401, 403):
        raise CronosAuthError(401, str(body.get("errorCode") or "AUTH_TOKEN_INVALID"), SESSION_ENDED)
    raise CronosAuthError(503, "CRONOS_UNAVAILABLE", UNREACHABLE)


async def unlock(admin_token: str, username: str) -> None:
    status, body = await _call("POST", "/api/auth/unlock", token=admin_token,
                               json_body={"username": username})
    if status == 200:
        return
    code = str(body.get("errorCode") or "")
    if status == 403 or code == "AUTH_FORBIDDEN":
        raise CronosAuthError(403, code or "AUTH_FORBIDDEN",
                              "Cronos says your account can't unlock users.")
    if status == 404 or code == "AUTH_USER_NOT_FOUND":
        raise CronosAuthError(404, code or "AUTH_USER_NOT_FOUND",
                              "Cronos has no account with that username.")
    if status == 401:
        raise CronosAuthError(401, code or "AUTH_TOKEN_INVALID", SESSION_ENDED)
    raise CronosAuthError(503, "CRONOS_UNAVAILABLE", UNREACHABLE)


def _key(session_token: str) -> str:
    return f"cronos:{session_token}"


async def save_grant(redis, session_token: str, grant: Grant, ttl: int) -> None:
    await redis.set(_key(session_token), json.dumps(asdict(grant)), ex=max(1, int(ttl)))


async def load_grant(redis, session_token: Optional[str]) -> Optional[Grant]:
    if not session_token:
        return None
    raw = await redis.get(_key(session_token))
    if not raw:
        return None
    try:
        return Grant(**json.loads(raw))
    except (ValueError, TypeError):
        return None


async def drop_grant(redis, session_token: Optional[str]) -> None:
    if session_token:
        await redis.delete(_key(session_token))


async def access_token(redis, session_token: Optional[str], ttl: int) -> str:
    """A live access token for this session, refreshed when close to expiry."""
    grant = await load_grant(redis, session_token)
    if grant is None:
        raise CronosAuthError(401, "CRONOS_NO_SESSION", SESSION_ENDED)
    if grant.expires_at - time.time() > REFRESH_MARGIN:
        return grant.access_token
    lock = redis.lock(f"lock:cronos-refresh:{session_token}", timeout=TIMEOUT + 5,
                      blocking_timeout=TIMEOUT + 5)
    if not await lock.acquire():
        raise CronosAuthError(503, "CRONOS_BUSY", UNREACHABLE)
    try:
        # Another request may have refreshed while this one waited.
        grant = await load_grant(redis, session_token)
        if grant is None:
            raise CronosAuthError(401, "CRONOS_NO_SESSION", SESSION_ENDED)
        if grant.expires_at - time.time() > REFRESH_MARGIN:
            return grant.access_token
        fresh = await refresh(grant)
        await save_grant(redis, session_token, fresh, ttl)
        return fresh.access_token
    finally:
        try:
            await lock.release()
        except Exception:
            pass


# --------------------------------------------------------------------------- duties


@dataclass
class Right:
    duty: str
    holds: bool
    source: str  # Direct | Delegation | None
    delegation_id: Optional[str] = None
    performer: Optional[str] = None
    on_behalf_of: Optional[str] = None
    as_of: Optional[str] = None

    def audit(self) -> dict[str, Any]:
        out: dict[str, Any] = {"duty": self.duty, "duty_source": self.source}
        if self.source == "Delegation":
            out.update(delegation_id=self.delegation_id, on_behalf_of=self.on_behalf_of)
        return out


_rights_cache: dict[tuple, tuple[float, Right]] = {}

_DUTY_ERRORS = {
    "DD_CONFIG_MISSING": (503, "Duty delegation isn't set up in Cronos for this client yet. "
                               "Ask an administrator."),
    "DD_FORBIDDEN": (403, "Cronos won't let your account check duties."),
    "DD_VALIDATION": (502, "Cronos didn't accept the duty check. Ask an administrator to "
                           "check the duty codes."),
}


async def effective_right(token: str, user_id: str, duty: str, study_no: Optional[str], *,
                          fresh: bool = False) -> Right:
    """Does this Cronos user hold `duty` for `study_no` right now? Fails closed."""
    key = (user_id, duty, study_no or "")
    now = time.time()
    if not fresh:
        cached = _rights_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
    params: dict[str, Any] = {"clientCode": client_code(), "userId": user_id, "dutyCode": duty,
                              "asOfUtc": dt.datetime.now(dt.timezone.utc)
                              .strftime("%Y-%m-%dT%H:%M:%SZ")}
    if study_no:
        params["studyNo"] = study_no
    module = (os.getenv("CRONOS_MODULE_CODE") or "").strip()
    if module:
        params["moduleCode"] = module
    status, body = await _call("GET", "/api/duty-delegation/effective-rights", token=token,
                               params=params)
    code = str(body.get("messageCode") or "")
    if status == 401 or code == "DD_UNAUTHORIZED":
        raise CronosAuthError(401, "DD_UNAUTHORIZED", SESSION_ENDED)
    data = body.get("data") if isinstance(body.get("data"), dict) else None
    if status != 200 or body.get("status") != "success" or data is None:
        if code in _DUTY_ERRORS:
            http_status, message = _DUTY_ERRORS[code]
            raise CronosAuthError(http_status, code, message)
        log.warning("duty check %s for %s answered %s %s", duty, user_id, status, code)
        raise CronosAuthError(503, code or "CRONOS_UNAVAILABLE",
                              "Cronos couldn't confirm your duties, so this was not saved. "
                              "Please try again.")
    right = Right(duty=duty, holds=bool(data.get("holdsDuty")),
                  source=str(data.get("source") or "None"),
                  delegation_id=_text(data.get("delegationId")),
                  performer=_text(data.get("performerUserId")),
                  on_behalf_of=_text(data.get("onBehalfOfUserId")),
                  as_of=_text(data.get("asOfUtc")))
    _rights_cache[key] = (now + RIGHTS_CACHE_SECONDS, right)
    return right


def _text(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def clear_cache() -> None:
    _rights_cache.clear()
