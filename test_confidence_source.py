"""Local-run fixes Task 6: every confidence carries a confidence_source -
"model" when the model rated it, "default:<route>" when it is the fixed number
for the processing route - stored on the Document and returned by the API so
the UI can show defaults differently.

Throwaway SQLite (set before `app` is imported); LLM and OCR calls stubbed.

Run: pytest test_confidence_source.py -v
"""
import os
import tempfile

_DB_FILE = os.path.join(tempfile.mkdtemp(), "confidence_source_test.db")
os.environ["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + _DB_FILE.replace("\\", "/")

import json

import pytest
from PIL import Image

import qwen_service as qs
import document_routes
from app import app
from extensions import db
from document_model import Document


# -- confidence_with_source ------------------------------------------------------

@pytest.mark.parametrize("raw,score", [
    ("high", 0.90), ("medium", 0.65), ("LOW", 0.35),
    ("87%", 0.87), (0.72, 0.72), (1, 1.0), ("0.4", 0.4), (1.7, 1.0),
])
def test_model_rating_is_model(raw, score):
    assert qs.confidence_with_source(raw, "text") == (pytest.approx(score), "model")


@pytest.mark.parametrize("raw", [None, "", "sure", "abc%", True])
@pytest.mark.parametrize("route,score", [("text", 0.65), ("fallback", 0.35), ("vision_full", 0.85)])
def test_missing_or_unusable_rating_is_route_default(raw, route, score):
    assert qs.confidence_with_source(raw, route) == (score, f"default:{route}")


def test_normalize_confidence_unchanged():
    assert qs.normalize_confidence("high") == 0.90
    assert qs.normalize_confidence(None, source="vision_thumb") == 0.90
    assert qs.normalize_confidence("junk", source="fallback") == 0.35


# -- classify_document --------------------------------------------------------------

@pytest.fixture
def llm(monkeypatch):
    state = {"answer": ""}
    monkeypatch.setattr(qs, "call_qwen_text", lambda prompt, max_tokens=1000: state["answer"])
    monkeypatch.setattr(qs, "get_all_active_doc_type_names", lambda: ["invoice", "receipt"])
    monkeypatch.setattr(qs, "get_prompt_override", lambda key: None)
    return state


@pytest.mark.parametrize("answer,conf,source", [
    ('{"document_type": "invoice", "confidence": "medium"}', 0.65, "model"),
    ('{"document_type": "invoice"}', 0.65, "default:text"),           # no rating given
    ('{"document_type": "invoice", "confidence": "dunno"}', 0.65, "default:text"),
    ("I think it's an invoice", 0.35, "default:fallback"),           # unparseable
])
def test_classify_reports_source(llm, answer, conf, source):
    llm["answer"] = answer
    r = qs.classify_document("INVOICE No. 1")
    assert (r["confidence"], r["confidence_source"]) == (conf, source)


def test_classify_result_without_source_tolerated():
    assert qs._classify_confidence({"confidence": "high"}) == (0.90, "model")
    assert qs._classify_confidence({"confidence": None}) == (0.65, "default:text")


# -- process_document -> saved Document -> API -------------------------------------

@pytest.fixture
def image_upload(monkeypatch, tmp_path):
    monkeypatch.setattr(document_routes, "UPLOAD_FOLDER", str(tmp_path))
    monkeypatch.setattr(qs, "extract_fields", lambda text, doc_type, **k: {"invoice_no": "BK1"})
    name = "conf_test.png"
    Image.new("RGB", (200, 100), "white").save(tmp_path / name)
    yield {"path": str(tmp_path / name), "name": name}
    with app.app_context():
        Document.query.filter_by(unique_upload_filename=name).delete()
        db.session.commit()


def _process_and_save(path, name):
    with app.app_context():
        result = qs.process_document(path)
    saved = document_routes._save_document_to_db(result, "scan.png", name, "png")
    with app.test_client() as c:
        body = c.get(f"/api/documents/{saved['id']}").get_json()["document"]
    return result, body


def test_paddle_text_path_keeps_model_rating(image_upload, monkeypatch):
    monkeypatch.setattr(qs, "OCR_TEXT_SOURCE", "paddle")
    monkeypatch.setattr(qs, "_ocr_text_for_image", lambda p: ("INVOICE BK1 TOTAL 10.00", []))
    monkeypatch.setattr(qs, "classify_document", lambda text, hint_type=None: {
        "document_type": "invoice", "confidence": 0.65, "confidence_source": "model"})
    result, body = _process_and_save(image_upload["path"], image_upload["name"])
    assert result["confidence_source"] == "model"
    assert (body["confidence"], body["confidence_source"]) == (0.65, "model")


def test_vision_image_path_is_route_default(image_upload, monkeypatch):
    monkeypatch.setattr(qs, "OCR_TEXT_SOURCE", "vision")
    monkeypatch.setattr(qs, "classify_and_extract_combined_vision", lambda *a, **k: {
        "document_type": "invoice", "raw_text": "INVOICE BK1", "fields": {"invoice_no": "BK1"},
        "fields_reliable": True})
    result, body = _process_and_save(image_upload["path"], image_upload["name"])
    assert result["confidence_source"] == "default:vision_full"
    assert (body["confidence"], body["confidence_source"]) == (0.90, "default:vision_full")


def test_old_document_has_null_source():
    with app.app_context():
        doc = Document(filename="old.pdf", file_type="pdf", document_type="invoice", confidence=0.9)
        doc.set_fields({})
        db.session.add(doc)
        db.session.commit()
        try:
            assert doc.to_dict()["confidence_source"] is None
        finally:
            db.session.delete(doc)
            db.session.commit()
