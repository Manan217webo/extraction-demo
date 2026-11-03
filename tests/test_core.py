"""Unit tests for the record-keeping core: audit chain, sources, versions, deltas, reads."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("AUDIT_HMAC_KEY", raising=False)
    monkeypatch.delenv("EDC_AUDIT_URL", raising=False)
    import db
    asyncio.run(db.close())
    asyncio.run(db.init())
    yield tmp_path
    asyncio.run(db.close())


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- audit


def test_audit_chain_verifies_and_detects_tampering(isolated):
    import audit
    import db
    from sqlalchemy import text

    async def scenario():
        async with db.session() as s:
            for i in range(5):
                await audit.record(s, "field.changed", username="alice", field=f"f{i}",
                                   old_value=i, new_value=i + 1, reason="test")
            await s.commit()
        async with db.session() as s:
            ok = await audit.verify(s)
        assert ok["ok"] and ok["checked"] == 5

        # The table refuses edits outright...
        async with db.session() as s:
            with pytest.raises(Exception):
                await s.execute(text("UPDATE audit_events SET new_value='x' WHERE seq=3"))
                await s.commit()
        # ...and if someone drops the trigger to edit anyway, verify() notices.
        async with db.session() as s:
            await s.execute(text("DROP TRIGGER audit_events_no_update"))
            await s.execute(text("UPDATE audit_events SET new_value='999' WHERE seq=3"))
            await s.commit()
        async with db.session() as s:
            broken = await audit.verify(s)
        assert broken["ok"] is False and broken["broken_at"] == 3

    run(scenario())


def test_audit_outbox_held_without_endpoint(isolated):
    import audit
    import db
    from sqlalchemy import select

    async def scenario():
        event = await audit.record_now("auth.login", username="alice")
        async with db.session() as s:
            box = await s.get(db.AuditOutbox, event.id)
        assert box.status == "held"

    run(scenario())


def test_hmac_changes_hash(isolated, monkeypatch):
    import audit
    import db
    row = {"id": "x", "ts": db.utcnow(), "action": "auth.login", "username": "a"}
    plain = audit.chain_hash(audit.GENESIS, row)
    monkeypatch.setenv("AUDIT_HMAC_KEY", "k")
    assert audit.chain_hash(audit.GENESIS, row) != plain


# --------------------------------------------------------------------------- sources


def test_paths_cannot_escape_the_source(tmp_path):
    import sources
    root = tmp_path / "share"
    (root / "a").mkdir(parents=True)
    (root / "a" / "x.pdf").write_bytes(b"%PDF-1.4 test")
    (root / "a" / "notes.txt").write_text("no")
    (tmp_path / "secret.pdf").write_bytes(b"%PDF-1.4 secret")
    (root / "a" / "link.pdf").symlink_to(tmp_path / "secret.pdf")
    conn = sources.LocalConnector("s", "S", str(root))

    for bad in ("../secret.pdf", "a/../../secret.pdf", "a/link.pdf"):
        with pytest.raises(sources.SourceError):
            run(conn.read(bad))
    entries, _ = run(conn.list_dir("a"))
    assert [e.name for e in entries] == ["x.pdf"]  # no .txt, no escaping symlink
    assert sources.clean_rel("/a//./x.pdf") == "a/x.pdf"
    assert [e.path for e in run(conn.walk_pdfs(""))] == ["a/x.pdf"]
    assert run(conn.read("a/x.pdf")).startswith(b"%PDF")


# --------------------------------------------------------------------------- documents


def test_versions_and_duplicate_classification(isolated):
    import db
    import documents

    async def scenario():
        async with db.session() as s:
            first = await documents.ingest(s, source_id="fs", path="v1.pdf", filename="v1.pdf",
                                           data=b"%PDF one", username="alice")
            assert first.new_version and first.version.version == 1
            ex = await documents.new_extraction(s, first.document, username="alice")
            ex.status = "ready"
            await s.commit()

            sha_one = first.version.sha256
            assert (await documents.classify(s, "fs", "v1.pdf", sha_one))[0] == "processed"
            assert (await documents.classify(s, "fs", "copy.pdf", sha_one))[0] == "processed"
            assert (await documents.classify(s, "fs", "v1.pdf", "f" * 64))[0] == "changed"
            assert (await documents.classify(s, "fs", "other.pdf", "e" * 64))[0] == "new"

            again = await documents.ingest(s, source_id="fs", path="v1.pdf", filename="v1.pdf",
                                           data=b"%PDF one", username="alice")
            assert not again.new_version
            changed = await documents.ingest(s, source_id="fs", path="v1.pdf", filename="v1.pdf",
                                             data=b"%PDF two", username="bob", reason="rescan")
            assert changed.new_version and changed.version.version == 2
            assert changed.document.id == first.document.id
            await s.commit()

    run(scenario())


# --------------------------------------------------------------------------- reads


def test_bigger_model_is_primary(monkeypatch):
    import fields
    monkeypatch.delenv("OPENAI_PREFERRED_READ", raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("OPENAI_SECOND_READ_MODEL", "gpt-4o")
    assert fields.read_models(True) == ("gpt-4o", "gpt-4o-mini")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5-pro")
    assert fields.read_models(True) == ("gpt-5-pro", "gpt-4o")
    monkeypatch.setenv("OPENAI_PREFERRED_READ", "second")
    assert fields.read_models(True) == ("gpt-4o", "gpt-5-pro")


def test_conflict_keeps_primary_value_and_shows_other():
    import fields
    row = lambda v: dict(section_id="s", group_id=None, instance=1, field_id="f", value=v,
                         evidence=v, page=1, rect=None, confidence=0.9, corrected=False,
                         struck_value=None)
    out = fields.reconcile([row("12")], [row("17")], prefer_second=False)[0]
    assert out["value"] == "12" and out["second_value"] == "17" and out["agreed"] is False
    assert out["confidence"] <= fields.DISAGREE_CONFIDENCE


# --------------------------------------------------------------------------- EDC delta


def _definition():
    import visit_forms
    field = lambda i, fid, name, value: {"index": i, "field_id": fid, "fieldName": name,
                                         "value": value, "record": {"field_name": name}}
    return {"edc": {"protocolNo": "P", "screeningNo": "S", "visitName": "Visit 1", "crfs": [{
        "section_id": "vitals", "crfName": "Vitals", "crfId": 7,
        "address": {"vitals||1|sbp": 0, "vitals||1|dbp": 1, "vitals||1|wt": 2},
        "fields": [field(0, 101, "SBP", "120"), field(1, 102, "DBP", "80"),
                   field(2, 103, "Weight", "")]}]}}


def test_only_changed_values_are_sent():
    import visit_forms
    form = {"sections": [{"section_id": "vitals", "fields": [
        {"field_id": "sbp", "label": "SBP", "value": "120", "key": "vitals.sbp"},
        {"field_id": "dbp", "label": "DBP", "value": "85", "key": "vitals.dbp"},
        {"field_id": "wt", "label": "Weight", "value": "71.0", "key": "vitals.wt"}]}]}
    body, _ = visit_forms.build_save(_definition(), form, {
        "vitals.sbp": {"base64Data": "AAAA"}, "vitals.dbp": {"base64Data": "BBBB"}})
    delta = body.pop("_delta")
    sent = {f["field_id"]: f["value"] for f in body["crfs"][0]["fields"]}
    assert sent == {102: "85", 103: "71.0"}  # SBP already 120 in the EDC
    assert [i["field_id"] for i in body["crfs"][0]["images"]] == [102]
    assert len(delta["skipped"]) == 1 and len(delta["changes"]) == 2


def test_nothing_sent_when_everything_matches():
    import visit_forms
    form = {"sections": [{"section_id": "vitals", "fields": [
        {"field_id": "sbp", "label": "SBP", "value": "120.0", "key": "vitals.sbp"},
        {"field_id": "dbp", "label": "DBP", "value": " 80 ", "key": "vitals.dbp"}]}]}
    body, _ = visit_forms.build_save(_definition(), form, {})
    assert body["crfs"] == []


def test_same_value_rules():
    from visit_forms import same_value
    assert same_value("16 Feb 2026", "16/02/2026")
    assert same_value("Yes", "yes")
    assert not same_value("12", "13")
    assert not same_value("", "x")


# --------------------------------------------------------------------------- EDC audit delivery


def test_audit_delivery_posts_in_order_and_backs_off(isolated, monkeypatch):
    import httpx

    import audit
    import db
    import edc_audit

    sent: list[dict] = []
    answers = iter([503, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        sent.append({"body": _json.loads(request.content), "key": request.headers["Idempotency-Key"]})
        return httpx.Response(next(answers), json={})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    async def scenario():
        await audit.record_now("auth.login", username="alice")  # before an endpoint: held
        monkeypatch.setenv("EDC_AUDIT_URL", "https://edc.example/api/audit")
        await audit.record_now("auth.logout", username="alice")
        await edc_audit._release_held()
        assert await edc_audit.deliver_once() == 0  # 503: kept, retried later
        state = await edc_audit.status()
        assert state["counts"]["pending"] == 2 and state["last_error"]
        async with db.session() as s:  # make the retry due now
            from sqlalchemy import update
            await s.execute(update(db.AuditOutbox).values(next_attempt_at=db.utcnow()))
            await s.commit()
        assert await edc_audit.deliver_once() == 2
        assert (await edc_audit.status())["counts"]["sent"] == 2

    run(scenario())
    records = sent[-1]["body"]["records"]
    assert [r["Action"] for r in records] == ["auth.login", "auth.logout"]
    assert records[1]["PrevHash"] == records[0]["Hash"]
    assert sent[0]["key"] == sent[1]["key"]  # the same batch retried is recognisable


def test_reviewer_clearing_a_value_is_sent_but_an_unread_blank_is_not():
    import visit_forms
    form = {"sections": [{"section_id": "vitals", "fields": [
        {"field_id": "sbp", "label": "SBP", "value": None, "status": "edited", "key": "vitals.sbp"},
        {"field_id": "dbp", "label": "DBP", "value": None, "status": "empty", "key": "vitals.dbp"}]}]}
    body, _ = visit_forms.build_save(_definition(), form, {})
    assert {f["field_id"]: f["value"] for f in body["crfs"][0]["fields"]} == {101: ""}


def test_crf_without_field_ids_is_sent_in_full():
    import visit_forms
    definition = _definition()
    for f in definition["edc"]["crfs"][0]["fields"]:
        f["field_id"] = None
    form = {"sections": [{"section_id": "vitals", "fields": [
        {"field_id": "dbp", "label": "DBP", "value": "85", "key": "vitals.dbp"}]}]}
    body, _ = visit_forms.build_save(definition, form, {})
    assert [f["value"] for f in body["crfs"][0]["fields"]] == ["120", "85", ""]


# --------------------------------------------------------------------------- EDC pull


def _pull_client():
    import app
    from fastapi.testclient import TestClient
    return TestClient(app.app)


def test_edc_pull_is_off_without_a_key_and_refuses_a_wrong_one(isolated, monkeypatch):
    monkeypatch.delenv("EDC_PULL_API_KEY", raising=False)
    client = _pull_client()
    assert client.get("/api/edc/audit").status_code == 404

    monkeypatch.setenv("EDC_PULL_API_KEY", "old-key, new-key")
    assert client.get("/api/edc/audit").status_code == 401
    assert client.get("/api/edc/audit",
                      headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/edc/audit",
                      headers={"Authorization": "Basic new-key"}).status_code == 401
    for key in ("old-key", "new-key"):
        assert client.get("/api/edc/audit",
                          headers={"Authorization": f"Bearer {key}"}).status_code == 200
    # The key opens this endpoint only, never the signed-in API.
    assert client.get("/api/audit",
                      headers={"Authorization": "Bearer new-key"}).status_code == 401


def test_edc_pull_pages_through_the_trail_in_order(isolated, monkeypatch):
    import audit
    import db

    async def write():
        async with db.session() as s:
            for i in range(5):
                await audit.record(s, "field.changed", username="alice", field=f"f{i}",
                                   old_value=i, new_value=i + 1, reason="test",
                                   protocol_no="ICR-23-001", screening_no="S-01")
            await s.commit()

    run(write())
    monkeypatch.setenv("EDC_PULL_API_KEY", "k")
    client = _pull_client()
    auth_ = {"Authorization": "Bearer k"}

    first = client.get("/api/edc/audit?limit=2", headers=auth_).json()
    assert [r["ItemRef"] for r in first["records"]] == ["f0", "f1"]
    assert first["has_more"] and first["head_seq"] == 5
    assert first["records"][0]["StudyOID"] == "ICR-23-001"
    assert first["records"][0]["SubjectKey"] == "S-01"
    assert first["records"][1]["PrevHash"] == first["records"][0]["Hash"]

    rest = client.get(f"/api/edc/audit?after={first['next_after']}&limit=10",
                      headers=auth_).json()
    assert [r["ItemRef"] for r in rest["records"]] == ["f2", "f3", "f4"]
    assert rest["records"][0]["PrevHash"] == first["records"][-1]["Hash"]
    assert not rest["has_more"]

    caught_up = client.get(f"/api/edc/audit?after={rest['next_after']}", headers=auth_).json()
    assert caught_up["records"] == [] and caught_up["next_after"] == rest["next_after"]

    # Pulling leaves push delivery alone.
    async def outbox():
        from sqlalchemy import select
        async with db.session() as s:
            return {o.status for o in (await s.execute(select(db.AuditOutbox))).scalars()}
    assert run(outbox()) == {"held"}
