"""Tests for local-run fixes Task 3: changing a document's record type saves
the type immediately and re-extracts its fields in a background thread; the
existing fields are only replaced by a non-empty result.

Runs the real Flask app against a throwaway SQLite file (set before `app` is
imported) with qwen_service.extract_fields stubbed - no LM Studio, no MySQL.

Run: pytest test_reextract_background.py -v
"""
import os
import tempfile
import threading
import time

_DB_FILE = os.path.join(tempfile.mkdtemp(), "reextract_test.db")
os.environ["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + _DB_FILE.replace("\\", "/")

import pytest

import qwen_service
import document_routes
from app import app
from extensions import db
from document_model import Document


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def doc_id():
    with app.app_context():
        doc = Document(filename="t.pdf", file_type="pdf", document_type="invoice",
                       raw_text="INVOICE No. 123\nTOTAL 10.00", confidence=0.9)
        doc.set_fields({"invoice_no": "123"})
        db.session.add(doc)
        db.session.commit()
        did = doc.id
    yield did
    with app.app_context():
        Document.query.filter_by(id=did).delete()
        db.session.commit()
    document_routes.reextract_jobs.pop(did, None)


@pytest.fixture
def extract(monkeypatch):
    """Stub extract_fields. state["result"] is returned (an Exception is
    raised); while state["gate"] is unset the call blocks."""
    state = {"result": {"receipt_no": "R1"}, "gate": threading.Event(), "calls": []}
    state["gate"].set()

    def _fake(raw_text, doc_type, *a, **k):
        state["calls"].append(doc_type)
        state["gate"].wait(10)
        if isinstance(state["result"], Exception):
            raise state["result"]
        return state["result"]

    monkeypatch.setattr(qwen_service, "extract_fields", _fake)
    monkeypatch.setattr(qwen_service, "learn_type_correction", lambda *a, **k: None)
    return state


def _wait_done(client, did, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/documents/{did}/reextract-status").get_json()
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("re-extraction did not finish")


def _fields(did):
    with app.app_context():
        return Document.query.get(did).get_fields()


def _type(did):
    with app.app_context():
        return Document.query.get(did).document_type


def test_patch_returns_immediately_and_type_is_saved(client, doc_id, extract):
    extract["gate"].clear()                     # extraction "takes forever"
    r = client.patch(f"/api/documents/{doc_id}", json={"document_type": "receipt"})
    body = r.get_json()
    assert r.status_code == 200 and body["success"] and body["reextract"] == "started"
    assert _type(doc_id) == "receipt"           # saved before the LLM call finishes
    assert _fields(doc_id) == {"invoice_no": "123"}
    assert client.get(f"/api/documents/{doc_id}/reextract-status").get_json()["status"] == "running"
    extract["gate"].set()
    job = _wait_done(client, doc_id)
    assert job["status"] == "done" and job["n_fields"] == 1
    assert job["document"]["fields"] == {"receipt_no": "R1"}
    assert _fields(doc_id) == {"receipt_no": "R1"}


def test_failure_keeps_existing_fields(client, doc_id, extract):
    extract["result"] = qwen_service.QwenServiceUnavailableError("model down")
    client.patch(f"/api/documents/{doc_id}", json={"document_type": "receipt"})
    job = _wait_done(client, doc_id)
    assert job["status"] == "failed" and "model down" in job["error"]
    assert _fields(doc_id) == {"invoice_no": "123"}
    assert _type(doc_id) == "receipt"


def test_empty_result_keeps_existing_fields(client, doc_id, extract):
    extract["result"] = {}
    client.patch(f"/api/documents/{doc_id}", json={"document_type": "receipt"})
    job = _wait_done(client, doc_id)
    assert job["status"] == "empty"
    assert _fields(doc_id) == {"invoice_no": "123"}


def test_type_change_rejected_while_running(client, doc_id, extract):
    extract["gate"].clear()
    client.patch(f"/api/documents/{doc_id}", json={"document_type": "receipt"})
    r = client.patch(f"/api/documents/{doc_id}", json={"document_type": "contract"})
    assert r.status_code == 409
    assert _type(doc_id) == "receipt"
    extract["gate"].set()
    _wait_done(client, doc_id)
    assert extract["calls"] == ["receipt"]


def test_user_edits_during_run_are_kept(client, doc_id, extract):
    with app.app_context():
        doc = Document.query.get(doc_id)
        doc.set_fields({"invoice_no": "123", "total": "10.00", "date": "2026-01-01"})
        db.session.commit()
    extract["result"] = {"invoice_no": "AI-1", "total": "AI-10", "date": "AI-DATE"}
    extract["gate"].clear()
    client.patch(f"/api/documents/{doc_id}", json={"document_type": "receipt"})
    # While it runs: the user corrects one field and adds another.
    r = client.patch(f"/api/documents/{doc_id}", json={"fields": {"total": "99.00", "note": "x"}})
    assert r.status_code == 200
    extract["gate"].set()
    job = _wait_done(client, doc_id)
    assert job["status"] == "done"
    assert sorted(job["user_kept"]) == ["note", "total"]
    assert _fields(doc_id) == {"invoice_no": "AI-1", "date": "AI-DATE", "total": "99.00", "note": "x"}


def test_no_user_edits_means_full_replace(client, doc_id, extract):
    client.patch(f"/api/documents/{doc_id}", json={"document_type": "receipt"})
    job = _wait_done(client, doc_id)
    assert job["user_kept"] == []
    assert _fields(doc_id) == {"receipt_no": "R1"}


def test_all_null_result_keeps_existing_fields(client, doc_id, extract):
    extract["result"] = {"receipt_no": None, "total": "  ", "items": [{"desc": None, "qty": ""}], "meta": {}}
    client.patch(f"/api/documents/{doc_id}", json={"document_type": "receipt"})
    job = _wait_done(client, doc_id)
    assert job["status"] == "empty"
    assert _fields(doc_id) == {"invoice_no": "123"}


@pytest.mark.parametrize("value, expected", [
    (None, False), ("", False), ("   ", False), ([], False), ({}, False),
    ({"a": None, "b": ""}, False), ([{"x": None}, {"y": " "}], False),
    ("0", True), (0, True), (False, True), ({"a": None, "b": "x"}, True), ([{"x": None}, {"y": 1}], True),
])
def test_has_any_value(value, expected):
    assert document_routes._has_any_value(value) is expected


def test_idle_status_for_untouched_document(client, doc_id):
    assert client.get(f"/api/documents/{doc_id}/reextract-status").get_json() == {"status": "idle"}
