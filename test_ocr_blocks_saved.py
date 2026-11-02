"""OCR_TEXT_SOURCE=paddle: the OCR lines read at upload are saved as the
document's text_blocks, so opening it (GET /<id>/blocks for the highlight
overlay) doesn't run PaddleOCR a second time.

Runs process_document() -> _save_document_to_db() -> GET /blocks against a
throwaway SQLite file (set before `app` is imported). OCR is faked and
counted; the text classifier and field extraction are stubbed - no models,
no LM Studio, no MySQL.

Run: pytest test_ocr_blocks_saved.py -v
"""
import os
import tempfile

# Always a throwaway DB - never the real one from the environment. (If another
# test module imported `app` first, its own SQLite override is the one in use.)
_DB_FILE = os.path.join(tempfile.mkdtemp(), "ocr_blocks_test.db")
os.environ["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + _DB_FILE.replace("\\", "/")

import pytest
from PIL import Image

import ocr_service
import qwen_service as qs
import document_routes
from app import app
from extensions import db
from document_model import Document

LINES = [
    {"text": "SIMEN UTARA SDN. BHD.", "box": (0, 0, 100, 8), "conf": 0.98},
    {"text": "INVOICE BK1141557", "box": (0, 10, 90, 18), "conf": 0.97},
    {"text": "TOTAL 11,494.50", "box": (0, 20, 80, 28), "conf": 0.99},
]


@pytest.fixture
def paddle_upload(monkeypatch, tmp_path):
    """An uploaded image in UPLOAD_FOLDER, paddle mode on, OCR calls counted."""
    calls = []

    def _ocr(img):
        calls.append(img.size)
        return LINES

    monkeypatch.setattr(ocr_service, "ocr_lines", _ocr)
    monkeypatch.setattr(qs, "OCR_TEXT_SOURCE", "paddle")
    monkeypatch.setattr(qs, "call_qwen_vision", lambda *a, **k: pytest.fail("no Vision calls in paddle mode"))
    monkeypatch.setattr(qs, "classify_document", lambda text, hint_type=None: {"document_type": "invoice", "confidence": 0.9})
    monkeypatch.setattr(qs, "extract_fields", lambda text, doc_type, **k: {"invoice_no": "BK1141557"})
    monkeypatch.setattr(document_routes, "UPLOAD_FOLDER", str(tmp_path))
    name = "scan_test.png"
    Image.new("RGB", (200, 100), "white").save(tmp_path / name)
    yield {"path": str(tmp_path / name), "name": name, "ocr_calls": calls}
    with app.app_context():
        Document.query.filter_by(unique_upload_filename=name).delete()
        db.session.commit()


def test_upload_blocks_saved_and_reused_on_open(paddle_upload, monkeypatch):
    with app.app_context():
        result = qs.process_document(paddle_upload["path"])
    expected = ocr_service.normalize_line_blocks(LINES, (200, 100), 1)
    assert result["text_blocks"] == expected
    assert len(paddle_upload["ocr_calls"]) == 1

    saved = document_routes._save_document_to_db(result, "scan.png", paddle_upload["name"], "png")
    monkeypatch.setattr(qs, "compute_text_blocks_lazy",
                        lambda path: pytest.fail("opening the document must not recompute blocks"))
    with app.test_client() as c:
        body = c.get(f"/api/documents/{saved['id']}/blocks").get_json()
    assert body["blocks"] == expected
    assert len(paddle_upload["ocr_calls"]) == 1        # still only the upload-time OCR


def test_vision_mode_still_computes_blocks_lazily(paddle_upload, monkeypatch):
    # Default mode leaves text_blocks empty at upload (unchanged behaviour).
    monkeypatch.setattr(qs, "OCR_TEXT_SOURCE", "vision")
    monkeypatch.setattr(qs, "classify_and_extract_combined_vision", lambda *a, **k: {
        "document_type": "invoice", "raw_text": "INVOICE BK1141557", "fields": {}, "fields_reliable": False})
    monkeypatch.setattr(qs, "extract_fields_vision", lambda *a, **k: {"invoice_no": "BK1141557"})
    with app.app_context():
        result = qs.process_document(paddle_upload["path"])
    assert result["text_blocks"] == []
    assert paddle_upload["ocr_calls"] == []
