"""Cronos sign-in, token refresh and duty checks, against a fake Cronos."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class FakeLock:
    def __init__(self, name: str, held: dict) -> None:
        self.lock = held.setdefault(name, asyncio.Lock())

    async def acquire(self) -> bool:
        await self.lock.acquire()
        return True

    async def release(self) -> None:
        self.lock.release()


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value

    async def delete(self, key):
        self.data.pop(key, None)

    async def ttl(self, key):
        return 900

    def lock(self, name, timeout=None, blocking_timeout=None):
        return FakeLock(name, self.locks)


class FakeCronos:
    """Users: jsmith/P@ss (Investigator, id 42), boss/P@ss (Admin, id 1),
    viewer/P@ss (CRA, id 7), locked/any, reset/any."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.refreshes = 0
        self.spent: set[str] = set()
        self.rights: dict[tuple[str, str], dict] = {}
        self.rights_status = 200
        self.users = {"jsmith": ("42", ["Investigator"]), "boss": ("1", ["Admin"]),
                      "viewer": ("7", ["CRA"])}

    def tokens(self, user_id, roles=None, expires_in=900):
        body = {"accessToken": f"at-{user_id}-{time.time_ns()}",
                "refreshToken": f"rt-{user_id}-{time.time_ns()}", "tokenType": "Bearer",
                "expiresIn": expires_in, "userId": user_id}
        if roles is not None:
            body.update(roles=roles, assignments=[{"siteId": "S1", "projectId": "P1"}])
        return body

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/Cronos_Sun_EDC")
        body = json.loads(request.content or b"{}") if request.method == "POST" else {}
        self.calls.append((path, {**dict(request.url.params), **body,
                                  "auth": request.headers.get("authorization")}))
        if path == "/api/auth/login":
            name = body.get("username")
            if name == "locked":
                return httpx.Response(403, json={"errorCode": "AUTH_ACCOUNT_LOCKED"})
            if name == "reset":
                return httpx.Response(200, json={"passwordResetRequired": True,
                                                  "resetToken": "x"})
            if name in self.users and body.get("password") == "P@ss":
                uid, roles = self.users[name]
                return httpx.Response(200, json=self.tokens(uid, roles))
            return httpx.Response(401, json={"errorCode": "AUTH_INVALID_CREDENTIALS"})
        if path == "/api/auth/refresh":
            token = body.get("refreshToken")
            if token in self.spent:
                return httpx.Response(401, json={"errorCode": "AUTH_TOKEN_INVALID"})
            self.spent.add(token)
            self.refreshes += 1
            return httpx.Response(200, json=self.tokens(token.split("-")[1]))
        if path == "/api/duty-delegation/effective-rights":
            if self.rights_status != 200:
                return httpx.Response(self.rights_status,
                                      json={"status": "error", "messageCode": "DD_UNAVAILABLE"})
            params = request.url.params
            data = self.rights.get((params["userId"], params["dutyCode"]),
                                   {"holdsDuty": False, "source": "None"})
            return httpx.Response(200, json={"status": "success", "messageCode": "DD_OK",
                                             "data": data})
        return httpx.Response(404, json={})


@pytest.fixture
def cronos(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("USERS_FILE", str(tmp_path / "none.json"))
    monkeypatch.setenv("AUTH_MODE", "cronos")
    monkeypatch.setenv("CRONOS_AUTH_URL", "https://cronos.test/Cronos_Sun_EDC")
    monkeypatch.setenv("CRONOS_CLIENT_CODE", "DEMO")
    for name in ("CRONOS_ROLE_MAP", "CRONOS_DEFAULT_ROLE", "CRONOS_ENFORCE_DUTIES",
                 "EDC_AUDIT_URL", "AUDIT_HMAC_KEY"):
        monkeypatch.delenv(name, raising=False)
    import cronos_auth
    import db
    import store
    fake, redis = FakeCronos(), FakeRedis()
    monkeypatch.setattr(cronos_auth, "_transport", httpx.MockTransport(fake))
    monkeypatch.setattr(store, "get_redis", lambda: redis)
    cronos_auth.clear_cache()
    asyncio.run(db.close())
    asyncio.run(db.init())
    yield fake, redis
    asyncio.run(db.close())


def client():
    import app
    from fastapi.testclient import TestClient
    return TestClient(app.app)


def events(action):
    import db
    from sqlalchemy import select

    async def go():
        async with db.session() as session:
            return (await session.execute(select(db.AuditEvent).where(
                db.AuditEvent.action == action).order_by(db.AuditEvent.seq))).scalars().all()
    return asyncio.run(go())


def sign_in(c):
    response = c.post("/api/login", json={"username": "jsmith", "password": "P@ss"})
    assert response.status_code == 200, response.text
    return response


def check_duty(token, action="submit", protocol="FMT-SAH-01"):
    import api
    import auth
    import db
    from starlette.requests import Request

    async def go():
        user = await auth.get_user("jsmith")
        request = Request({"type": "http", "client": ("10.0.0.5", 1234),
                           "headers": [(b"cookie", f"webo_session={token}".encode())]})
        document = db.Document(id="doc1", protocol_no=protocol, screening_no="S-1",
                               visit_name="Visit 2")
        return await api.require_duty(request, user, action, document, fresh=True)
    return asyncio.run(go())


# --------------------------------------------------------------------------- sign-in


def test_sign_in_creates_the_account_and_keeps_tokens_server_side(cronos):
    _, redis = cronos
    c = client()
    response = sign_in(c)
    assert response.json()["role"] == "reviewer"
    assert response.json()["auth_source"] == "cronos"
    assert "accessToken" not in response.text and "refreshToken" not in response.text
    me = c.get("/api/me").json()
    assert me["cronos"]["user_id"] == "42" and me["cronos"]["roles"] == ["Investigator"]
    assert any(key.startswith("cronos:") for key in redis.data)
    login = events("auth.login")[-1]
    assert login.details["via"] == "cronos" and login.details["cronos_user_id"] == "42"
    assert events("user.created")[-1].field == "jsmith"

    c.post("/api/logout")
    assert not any(key.startswith("cronos:") for key in redis.data)


def test_roles_map_highest_wins_and_unmapped_is_read_only(cronos, monkeypatch):
    import cronos_auth
    assert cronos_auth.map_role(["CRA"]) == "monitor"
    assert cronos_auth.map_role(["Investigator", "Admin"]) == "admin"
    monkeypatch.setenv("CRONOS_ROLE_MAP", '{"CRA": "reviewer"}')
    assert cronos_auth.map_role(["cra"]) == "reviewer"
    monkeypatch.setenv("CRONOS_DEFAULT_ROLE", "none")
    assert cronos_auth.map_role(["Pharmacist"]) is None
    response = client().post("/api/login", json={"username": "boss", "password": "P@ss"})
    assert response.status_code == 403  # Admin isn't in this map and the default refuses


@pytest.mark.parametrize("username,password,status,phrase", [
    ("jsmith", "wrong", 401, "Incorrect username or password"),
    ("locked", "x", 423, "locked"),
    ("reset", "x", 403, "has to be changed"),
])
def test_cronos_refusals_read_as_sentences(cronos, username, password, status, phrase):
    response = client().post("/api/login", json={"username": username, "password": password})
    assert response.status_code == status and phrase in response.json()["detail"]
    assert events("auth.login_failed") or events("auth.locked")


def test_local_admin_still_signs_in_when_cronos_is_down(cronos, monkeypatch):
    import auth
    import cronos_auth
    import db

    async def make():
        async with db.session() as session:
            session.add(db.User(username="root", full_name="Break Glass", role="admin",
                                password_hash=auth.hash_password("a-long-local-pass")))
            await session.commit()
    asyncio.run(make())

    def down(request):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(cronos_auth, "_transport", httpx.MockTransport(down))
    c = client()
    assert c.post("/api/login", json={"username": "root",
                                      "password": "a-long-local-pass"}).status_code == 200
    response = c.post("/api/login", json={"username": "jsmith", "password": "P@ss"})
    assert response.status_code == 503 and "couldn't reach Cronos" in response.json()["detail"]


# --------------------------------------------------------------------------- tokens


def test_refresh_happens_once_under_the_lock(cronos):
    fake, redis = cronos
    import cronos_auth

    async def go():
        grant = cronos_auth.Grant(access_token="old", refresh_token="rt-42-1",
                                  expires_at=time.time() + 5, user_id="42")
        await cronos_auth.save_grant(redis, "sess", grant, 3600)
        return await asyncio.gather(*[cronos_auth.access_token(redis, "sess", 3600)
                                      for _ in range(5)])
    tokens = asyncio.run(go())
    assert fake.refreshes == 1 and len(set(tokens)) == 1 and tokens[0] != "old"


def test_a_spent_refresh_token_ends_the_session(cronos):
    fake, redis = cronos
    import cronos_auth
    fake.spent.add("rt-42-1")

    async def go():
        grant = cronos_auth.Grant(access_token="old", refresh_token="rt-42-1",
                                  expires_at=time.time(), user_id="42")
        await cronos_auth.save_grant(redis, "sess", grant, 3600)
        await cronos_auth.access_token(redis, "sess", 3600)
    with pytest.raises(cronos_auth.CronosAuthError) as exc:
        asyncio.run(go())
    assert exc.value.status == 401


def test_edc_calls_carry_the_person_token(cronos):
    import edc

    async def go():
        async def provider():
            return "person-token"
        edc.use_token(provider)
        return await edc._headers()
    assert asyncio.run(go())["Authorization"] == "Bearer person-token"


# --------------------------------------------------------------------------- duties


def test_duty_held_directly_or_by_delegation(cronos):
    fake, _ = cronos
    c = client()
    sign_in(c)
    token = c.cookies.get("webo_session")
    fake.rights[("42", "CRF_REVIEW")] = {"holdsDuty": True, "source": "Direct"}
    assert check_duty(token).audit() == {"duty": "CRF_REVIEW", "duty_source": "Direct"}

    fake.rights[("42", "CRF_REVIEW")] = {"holdsDuty": True, "source": "Delegation",
                                         "delegationId": 1024, "performerUserId": "42",
                                         "onBehalfOfUserId": "11"}
    details = check_duty(token).audit()
    assert details["on_behalf_of"] == "11" and details["delegation_id"] == "1024"
    sent = [call for path, call in fake.calls if path.endswith("effective-rights")][-1]
    assert sent["clientCode"] == "DEMO" and sent["studyNo"] == "FMT-SAH-01"
    assert sent["auth"].startswith("Bearer at-42-")


def test_no_duty_is_refused_and_audited(cronos):
    from fastapi import HTTPException
    c = client()
    sign_in(c)
    with pytest.raises(HTTPException) as exc:
        check_duty(c.cookies.get("webo_session"))
    assert exc.value.status_code == 403 and "CRF_REVIEW" in exc.value.detail
    denied = events("duty.denied")[-1]
    assert denied.field == "CRF_REVIEW" and denied.new_value == "submit"


def test_duty_check_fails_closed(cronos):
    fake, _ = cronos
    import cronos_auth
    c = client()
    sign_in(c)
    fake.rights_status = 500
    with pytest.raises(cronos_auth.CronosAuthError) as exc:
        check_duty(c.cookies.get("webo_session"))
    assert exc.value.status == 503


def test_local_accounts_cannot_review_while_duties_are_enforced(cronos):
    import api
    import db
    from fastapi import HTTPException
    from starlette.requests import Request

    user = db.User(username="root", full_name="Root", role="admin", password_hash="x")
    document = db.Document(id="d", protocol_no="P")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.require_duty(Request({"type": "http", "headers": []}), user,
                                     "review", document))
    assert exc.value.status_code == 403


def test_signature_is_checked_by_cronos(cronos):
    import auth
    sign_in(client())
    user = asyncio.run(auth.get_user("jsmith"))
    ok, grant = asyncio.run(auth.confirm_signature(user, "P@ss"))
    assert ok and grant.user_id == "42"
    ok, grant = asyncio.run(auth.confirm_signature(user, "nope"))
    assert not ok and events("signature.failed")[-1].details["via"] == "cronos"


# --------------------------------------------------------------------------- dev Cronos shapes


def test_cronos_role_names_from_the_dev_deployment():
    import cronos_auth
    dev_user_1 = ["Application Administrator", "Clinical Research Associate",
                  "Clinical Research Coordinator", "Data Coordinator", "Data Entry Operator",
                  "Data Manager", "Database Designer", "Investigators", "Medical Coder",
                  "Medical Monitor", "Super Admin", "Super User"]
    assert cronos_auth.map_role(dev_user_1) == "admin"
    assert cronos_auth.map_role(["Investigators"]) == "reviewer"
    assert cronos_auth.map_role(["Data Entry Operator", "Medical Monitor"]) == "reviewer"
    assert cronos_auth.map_role(["Clinical Research Associate"]) == "monitor"
    assert cronos_auth.map_role(["Medical Coder"]) == "monitor"  # unmapped -> default


def _redirecting(fake, page):
    def handler(request):
        path = request.url.path.removeprefix("/Cronos_Sun_EDC")
        if path in ("/api/auth/refresh", "/api/duty-delegation/effective-rights"):
            return httpx.Response(302, headers={"location": f"../{page}"},
                                  text="<h1>Object Moved</h1>")
        return fake(request)
    return httpx.MockTransport(handler)


def test_redirect_to_401_page_means_the_session_ended(cronos, monkeypatch):
    fake, redis = cronos
    import cronos_auth
    monkeypatch.setattr(cronos_auth, "_transport", _redirecting(fake, "401.html"))

    async def go():
        grant = cronos_auth.Grant(access_token="old", refresh_token="rt-42-1",
                                  expires_at=time.time(), user_id="42")
        await cronos_auth.save_grant(redis, "sess", grant, 3600)
        await cronos_auth.access_token(redis, "sess", 3600)
    with pytest.raises(cronos_auth.CronosAuthError) as exc:
        asyncio.run(go())
    assert exc.value.status == 401 and "sign in again" in exc.value.message
    with pytest.raises(cronos_auth.CronosAuthError) as exc:
        asyncio.run(cronos_auth.effective_right("t", "42", "CRF_REVIEW", "P", fresh=True))
    assert exc.value.status == 401


def test_redirect_to_error_page_is_a_cronos_error_not_unreachable(cronos, monkeypatch):
    fake, _ = cronos
    import cronos_auth
    monkeypatch.setattr(cronos_auth, "_transport", _redirecting(fake, "error.html"))
    with pytest.raises(cronos_auth.CronosAuthError) as exc:
        asyncio.run(cronos_auth.effective_right("t", "42", "CRF_REVIEW", "P", fresh=True))
    assert exc.value.status == 502 and exc.value.code == "CRONOS_ERROR"
    assert "couldn't reach" not in exc.value.message


def test_redirect_to_403_page_is_a_refusal(cronos, monkeypatch):
    fake, _ = cronos
    import cronos_auth
    monkeypatch.setattr(cronos_auth, "_transport", _redirecting(fake, "403.html"))
    with pytest.raises(cronos_auth.CronosAuthError) as exc:
        asyncio.run(cronos_auth.effective_right("t", "42", "CRF_REVIEW", "P", fresh=True))
    assert exc.value.status == 403 and exc.value.code == "DD_FORBIDDEN"


def test_unsigned_cronos_submissions_are_refused_while_duties_are_enforced(cronos):
    c = client()
    sign_in(c)
    assert c.post("/api/cronos/submissions",
                  json={"form_id": "x", "payload": {}}).status_code == 410
