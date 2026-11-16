"""Study/site scope, record locks, archive, process-again and forms missing from the EDC."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_cronos_auth import FakeCronos, FakeRedis  # noqa: E402


class LockingRedis(FakeRedis):
    """FakeRedis plus the SET NX/XX and MGET the record locks use."""

    async def set(self, key, value, ex=None, nx=False, xx=False):
        if nx and key in self.data:
            return None
        if xx and key not in self.data:
            return None
        self.data[key] = value
        return True

    async def mget(self, keys):
        return [self.data.get(k) for k in keys]


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Cronos sign-in, two site folders of study P1 (S1, S2) and one untagged folder.
    Every Cronos user is assigned P1 / S1."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("USERS_FILE", str(tmp_path / "none.json"))
    monkeypatch.setenv("AUTH_MODE", "cronos")
    monkeypatch.setenv("CRONOS_AUTH_URL", "https://cronos.test/Cronos_Sun_EDC")
    monkeypatch.setenv("CRONOS_CLIENT_CODE", "DEMO")
    for name in ("CRONOS_ROLE_MAP", "CRONOS_DEFAULT_ROLE", "CRONOS_ENFORCE_DUTIES",
                 "CRONOS_SITE_SCOPE", "EDC_AUDIT_URL", "AUDIT_HMAC_KEY"):
        monkeypatch.delenv(name, raising=False)
    folders = {}
    for name in ("s1", "s2", "loose"):
        folders[name] = tmp_path / name
        folders[name].mkdir()
        (folders[name] / f"{name}.pdf").write_bytes(b"%PDF-1.4 test")
    config = {"sources": [
        {"id": "s1", "name": "P1 · Site 1", "location": str(folders["s1"]), "study": "P1", "site": "S1"},
        {"id": "s2", "name": "P1 · Site 2", "location": str(folders["s2"]), "study": "P1", "site": "S2"},
        {"id": "loose", "name": "Untagged", "location": str(folders["loose"])},
    ]}
    (tmp_path / "sources.json").write_text(json.dumps(config))
    monkeypatch.setenv("SOURCES_FILE", str(tmp_path / "sources.json"))

    import cronos_auth
    import db
    import store
    fake, redis = FakeCronos(), LockingRedis()
    fake.users["amy"] = ("43", ["Clinical Research Coordinator"])
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


def signed_in(username):
    c = client()
    response = c.post("/api/login", json={"username": username, "password": "P@ss"})
    assert response.status_code == 200, response.text
    return c


def add_document(source_id, path, *, created_by="system", protocol=None, submitted=False):
    import audit
    import db

    async def go():
        async with db.session() as s:
            document = db.Document(source_id=source_id, path=path, filename=path,
                                   created_by=created_by, protocol_no=protocol,
                                   screening_no="001", visit_name="Visit 1", status="ready")
            s.add(document)
            await s.flush()
            await audit.record(s, "file.ingested", username=created_by, document=document)
            if submitted:
                extraction = db.Extraction(document_id=document.id, version=1, run=1,
                                           job_id=f"job-{document.id}", status="ready",
                                           created_by=created_by)
                s.add(extraction)
                await s.flush()
                s.add(db.Submission(document_id=document.id, extraction_id=extraction.id,
                                    submitted_by=created_by, status="sent"))
                document.status = "submitted"
            await s.commit()
            return document.id
    return asyncio.run(go())


def actions(name):
    import db
    from sqlalchemy import select

    async def go():
        async with db.session() as s:
            return (await s.execute(select(db.AuditEvent).where(
                db.AuditEvent.action == name))).scalars().all()
    return asyncio.run(go())


# --------------------------------------------------------------------------- study/site scope


def test_site_user_sees_only_their_sites_folders_and_records(world):
    mine = add_document("s1", "s1.pdf", protocol="P1")
    other = add_document("s2", "s2.pdf", protocol="P1")
    loose = add_document("loose", "loose.pdf")
    c = signed_in("jsmith")

    assert [s["id"] for s in c.get("/api/sources").json()["sources"]] == ["s1"]
    assert c.get("/api/sources/s2/browse").status_code == 404
    assert c.get("/api/sources/loose/browse").status_code == 404
    assert c.get("/api/sources/s1/browse").status_code == 200

    listed = [d["id"] for d in c.get("/api/docs").json()["documents"]]
    assert listed == [mine]
    # Another site's record is unreachable by its link too.
    for doc in (other, loose):
        assert c.get(f"/api/docs/{doc}").status_code == 404
        assert c.get(f"/api/docs/{doc}/history").status_code == 404
        assert c.post(f"/api/docs/{doc}/archive", json={"reason": "x"}).status_code == 404
    # ...and so is its audit trail.
    trail = {e["document_id"] for e in c.get("/api/audit").json()["events"]}
    assert mine in trail and other not in trail and loose not in trail


def test_scanning_another_sites_folder_is_refused(world):
    c = signed_in("jsmith")
    response = c.post("/api/batches/preview", json={"source_id": "s2", "paths": [""]})
    assert response.status_code == 404


def test_admin_and_scope_switch_see_everything(world, monkeypatch):
    add_document("s1", "s1.pdf")
    add_document("s2", "s2.pdf")
    boss = signed_in("boss")
    assert len(boss.get("/api/docs").json()["documents"]) == 2
    assert len(boss.get("/api/sources").json()["sources"]) == 3

    monkeypatch.setenv("CRONOS_SITE_SCOPE", "false")
    assert len(signed_in("jsmith").get("/api/docs").json()["documents"]) == 2


def test_uploads_follow_the_uploader_or_the_study(world):
    own = add_document("upload", "sha-own", created_by="jsmith", protocol="P9")
    same_study = add_document("upload", "sha-p1", created_by="someone", protocol="p1")
    other_study = add_document("upload", "sha-p9", created_by="someone", protocol="P9")
    listed = {d["id"] for d in signed_in("jsmith").get("/api/docs").json()["documents"]}
    assert listed == {own, same_study}
    assert other_study not in listed


def test_assignments_are_kept_from_sign_in(world):
    import auth
    signed_in("jsmith")
    user = asyncio.run(auth.get_user("jsmith"))
    assert user.cronos_assignments == [{"siteId": "S1", "projectId": "P1"}]


# --------------------------------------------------------------------------- record lock


def test_second_person_gets_the_record_read_only(world):
    doc = add_document("s1", "s1.pdf", protocol="P1")
    jsmith, amy = signed_in("jsmith"), signed_in("amy")

    first = jsmith.post(f"/api/docs/{doc}/lock").json()
    assert first["acquired"] and first["lock"]["mine"]
    second = amy.post(f"/api/docs/{doc}/lock").json()
    assert not second["acquired"] and second["lock"]["username"] == "jsmith"

    # The server refuses amy's changes, not only her screen.
    refused = amy.post(f"/api/docs/{doc}/draft", json={"payload": {"form": {}}})
    assert refused.status_code == 423
    assert "jsmith" in refused.json()["detail"]["message"]
    assert amy.post(f"/api/docs/{doc}/archive", json={"reason": "wrong site"}).status_code == 423

    listed = amy.get("/api/docs").json()["documents"][0]
    assert listed["lock"]["username"] == "jsmith" and not listed["lock"]["mine"]

    # Closing gives it back; amy's page then takes it.
    assert jsmith.delete(f"/api/docs/{doc}/lock").json()["released"]
    assert amy.post(f"/api/docs/{doc}/lock").json()["acquired"]


def test_only_the_holder_or_an_admin_releases_a_lock(world):
    doc = add_document("s1", "s1.pdf", protocol="P1")
    jsmith, amy, boss = signed_in("jsmith"), signed_in("amy"), signed_in("boss")
    jsmith.post(f"/api/docs/{doc}/lock")

    assert not amy.delete(f"/api/docs/{doc}/lock").json()["released"]
    assert amy.delete(f"/api/docs/{doc}/lock?force=true").status_code == 403
    assert boss.delete(f"/api/docs/{doc}/lock?force=true").json()["released"]
    released = actions("document.lock_released")
    assert len(released) == 1 and released[0].old_value == "jsmith"
    assert amy.post(f"/api/docs/{doc}/lock").json()["acquired"]


def test_a_lock_lapses_when_nobody_renews_it(world):
    import locks
    doc = add_document("s1", "s1.pdf", protocol="P1")
    _, redis = world
    jsmith, amy = signed_in("jsmith"), signed_in("amy")
    jsmith.post(f"/api/docs/{doc}/lock")
    redis.data.pop(locks._key(doc))  # what Redis does when the TTL runs out
    assert amy.post(f"/api/docs/{doc}/lock").json()["acquired"]


# --------------------------------------------------------------------------- archive


def test_archive_needs_a_reason_and_takes_the_record_off_the_list(world):
    doc = add_document("s1", "s1.pdf", protocol="P1")
    c = signed_in("jsmith")
    assert c.post(f"/api/docs/{doc}/archive", json={"reason": " "}).status_code == 422

    archived = c.post(f"/api/docs/{doc}/archive", json={"reason": "Pushed to the wrong site"})
    assert archived.status_code == 200
    assert archived.json()["document"]["archived"]["reason"] == "Pushed to the wrong site"

    body = c.get("/api/docs").json()
    assert body["documents"] == [] and body["counts"]["archived"] == 1
    assert [d["id"] for d in c.get("/api/docs?archived=true").json()["documents"]] == [doc]
    # Read-only while archived.
    assert c.post(f"/api/docs/{doc}/lock").status_code == 409
    assert c.post(f"/api/docs/{doc}/draft", json={"payload": {"form": {}}}).status_code == 409

    assert c.post(f"/api/docs/{doc}/restore", json={"reason": ""}).status_code == 422
    assert c.post(f"/api/docs/{doc}/restore", json={"reason": "Archived by mistake"}).status_code == 200
    assert [d["id"] for d in c.get("/api/docs").json()["documents"]] == [doc]
    assert len(actions("document.archived")) == 1 and len(actions("document.restored")) == 1


# --------------------------------------------------------------------------- process again


def test_submitted_record_needs_remarks_before_it_can_change(world):
    doc = add_document("s1", "s1.pdf", protocol="P1", submitted=True)
    c = signed_in("jsmith")

    assert c.post(f"/api/docs/{doc}/lock").status_code == 409
    refused = c.post(f"/api/docs/{doc}/draft", json={"payload": {"form": {}}})
    assert refused.status_code == 409 and "Process again" in refused.json()["detail"]

    assert c.post(f"/api/docs/{doc}/reopen", json={"reason": ""}).status_code == 422
    opened = c.post(f"/api/docs/{doc}/reopen", json={"reason": "Value entered wrongly"})
    assert opened.status_code == 200 and opened.json()["lock"]["mine"]
    assert c.post(f"/api/docs/{doc}/lock").json()["acquired"]
    event = actions("document.reopened")[0]
    assert event.reason == "Value entered wrongly" and event.username == "jsmith"


def test_cancelling_process_again_changes_nothing(world):
    """Cancel is the browser not calling /reopen: the record stays as it was."""
    doc = add_document("s1", "s1.pdf", protocol="P1", submitted=True)
    c = signed_in("jsmith")
    assert c.get(f"/api/docs/{doc}").json()["document"]["reopened"] is None
    assert c.post(f"/api/docs/{doc}/lock").status_code == 409
    assert actions("document.reopened") == []


def test_unsubmitted_record_is_not_reopened(world):
    doc = add_document("s1", "s1.pdf", protocol="P1")
    c = signed_in("jsmith")
    assert c.post(f"/api/docs/{doc}/reopen", json={"reason": "x y z"}).status_code == 409


# --------------------------------------------------------------------------- missing forms


def test_forms_on_the_scan_that_the_edc_visit_lacks():
    import visit_forms
    definition = {"edc": {"crfs": [{"crfName": "Vital Signs"}, {"crfName": "Haematology"}]},
                  "sections": [{"name": "Demographics"}]}
    titles = [{"title": "VITAL SIGNS", "page": 1}, {"title": "Hematology", "page": 2},
              {"title": "Demographics", "page": 1}, {"title": "Urinalysis", "page": 3}]
    assert visit_forms.missing_forms(titles, definition) == [{"title": "Urinalysis", "page": 3}]
    assert visit_forms.missing_forms(None, definition) == []


def test_missing_forms_become_warnings():
    import esource
    payload = {"form": {"edc": {"visitName": "Visit 3"}},
               "missing_forms": [{"title": "Urinalysis", "page": 3}]}
    [warning] = esource.missing_form_warnings(payload)
    assert "Urinalysis" in warning and "page 3" in warning and "Visit 3" in warning
    payload["missing_forms"].append({"title": "12-lead ECG", "page": 2})
    [both] = esource.missing_form_warnings(payload)  # one warning, not one per form
    assert both.startswith("2 forms") and "Urinalysis" in both and "12-lead ECG" in both
    assert esource.missing_form_warnings({"form": {}}) == []
