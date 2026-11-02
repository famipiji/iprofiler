"""Unit tests for OCR_TEXT_SOURCE=paddle in qwen_service.py (PaddleOCR
migration, Task 7, option B / local-run fixes Task 5): scanned PDFs and images
get raw_text from the local OCR engine and run the text path
(classify_document + extract_fields) with no Vision calls, falling back to
the Vision path per document when the OCR is weak or fails.

OCR is mocked (ocr_service.ocr_lines), PyMuPDF is faked, Vision and the text
classifier are captured - no models, no LM Studio, no DB needed.

Run: pytest test_ocr_text_source.py -v
"""
import importlib
import sys
import types

import pytest
from PIL import Image

import ocr_service
import qwen_service as qs


def _lines(*items):
    return [{"text": t, "box": (0, i * 10, 100, i * 10 + 8), "conf": c} for i, (t, c) in enumerate(items)]


GOOD = _lines(("SIMEN UTARA SDN. BHD.", 0.98), ("INVOICE BK1141557", 0.97), ("TOTAL 11,494.50", 0.99))
WEAK = _lines(("abcdefghij klmnop", 0.6), ("qrstuvwxyz 12345", 0.7))   # e.g. a Jawi page
GOOD_TEXT = "SIMEN UTARA SDN. BHD.\nINVOICE BK1141557\nTOTAL 11,494.50"


def _img():
    return Image.new("RGB", (200, 100), "white")


def _ocr_results(monkeypatch, *results):
    """ocr_lines returns each of `results` in turn (an Exception is raised)."""
    it = iter(results)

    def _fake(img):
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(ocr_service, "ocr_lines", _fake)


# -- flag parsing ---------------------------------------------------------------

def test_default_is_vision(monkeypatch):
    monkeypatch.delenv("OCR_TEXT_SOURCE", raising=False)
    assert importlib.reload(qs).OCR_TEXT_SOURCE == "vision"
    monkeypatch.setenv("OCR_TEXT_SOURCE", " Paddle ")
    assert importlib.reload(qs).OCR_TEXT_SOURCE == "paddle"
    monkeypatch.delenv("OCR_TEXT_SOURCE")
    importlib.reload(qs)


# -- _ocr_text_for_pages guard ---------------------------------------------------

def test_good_pages_joined(monkeypatch):
    _ocr_results(monkeypatch, GOOD, GOOD)
    assert qs._ocr_text_for_pages([(_img(), "page 1"), (_img(), "page 2")]) == GOOD_TEXT + "\n\n" + GOOD_TEXT


def test_any_weak_page_falls_back_to_vision(monkeypatch):
    _ocr_results(monkeypatch, GOOD, WEAK)
    assert qs._ocr_text_for_pages([(_img(), "page 1"), (_img(), "page 2")]) is None


def test_blank_page_is_not_weak(monkeypatch):
    _ocr_results(monkeypatch, GOOD, [], _lines(("x", 0.2)))
    pages = [(_img(), f"page {i}") for i in (1, 2, 3)]
    assert qs._ocr_text_for_pages(pages) == GOOD_TEXT + "\n\nx"


def test_too_little_text_falls_back(monkeypatch):
    _ocr_results(monkeypatch, _lines(("RM 5", 0.99)))
    assert qs._ocr_text_for_pages([(_img(), "image")]) is None


def test_ocr_failure_falls_back(monkeypatch):
    _ocr_results(monkeypatch, ocr_service.OCRError("paddle exploded"))
    assert qs._ocr_text_for_pages([(_img(), "image")]) is None


# -- _ocr_text_for_image / _ocr_text_for_pdf -------------------------------------

def test_image_path(monkeypatch, tmp_path):
    _ocr_results(monkeypatch, GOOD)
    p = tmp_path / "x.png"
    _img().save(p)
    assert qs._ocr_text_for_image(str(p)) == GOOD_TEXT


def test_unreadable_image_falls_back(tmp_path):
    p = tmp_path / "bad.png"
    p.write_bytes(b"not an image")
    assert qs._ocr_text_for_image(str(p)) is None


class _FakePix:
    def __init__(self, w, h):
        self.width, self.height, self.samples = w, h, b"\xff" * (w * h * 3)


def _fake_fitz(monkeypatch, n_pages=0, open_error=None):
    zooms = []

    class _Doc:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            for _ in range(n_pages):
                def get_pixmap(matrix):
                    zooms.append(matrix)
                    return _FakePix(40, 20)
                yield types.SimpleNamespace(get_pixmap=get_pixmap)

    def _open(path):
        if open_error:
            raise open_error
        return _Doc()

    monkeypatch.setitem(sys.modules, "fitz", types.SimpleNamespace(open=_open, Matrix=lambda a, b: (a, b)))
    return zooms


def test_pdf_pages_rendered_at_ocr_dpi(monkeypatch):
    _ocr_results(monkeypatch, GOOD, GOOD)
    zooms = _fake_fitz(monkeypatch, n_pages=2)
    assert qs._ocr_text_for_pdf("scan.pdf") == GOOD_TEXT + "\n\n" + GOOD_TEXT
    zoom = qs.OCR_PRETEXT_PDF_DPI / 72.0
    assert zooms == [(zoom, zoom)] * 2


def test_pdf_render_failure_falls_back(monkeypatch):
    _fake_fitz(monkeypatch, open_error=RuntimeError("broken pdf"))
    assert qs._ocr_text_for_pdf("broken.pdf") is None


# -- process_document wiring (stops right after classification) ------------------

class _StopAfterClassify(Exception):
    pass


@pytest.fixture
def pipeline(monkeypatch):
    """process_document() with Vision + classifier captured, stopping at language detection."""
    calls = {"vision": [], "classify": []}

    def _vision(*a, **k):
        calls["vision"].append(1)
        return "TYPE: invoice\nTEXT:\nvision text"

    def _classify(text, hint_type=None):
        calls["classify"].append(text)
        return {"document_type": "invoice", "confidence": "high"}

    def _stop(text):
        raise _StopAfterClassify(text)

    monkeypatch.setattr(qs, "call_qwen_vision", _vision)
    monkeypatch.setattr(qs, "classify_document", _classify)
    monkeypatch.setattr(qs, "detect_language", _stop)
    monkeypatch.setattr(qs, "get_all_active_doc_type_names", lambda: ["invoice"])
    monkeypatch.setattr(qs, "_build_vision_extraction_hints", lambda t, raw_text=None: ([], "", ""))
    monkeypatch.setattr(qs, "_ocr_hints_for_pdf", lambda p: [])
    monkeypatch.setattr(qs, "is_scanned_pdf", lambda p: True)
    monkeypatch.setattr(qs, "pdf_pages_to_b64", lambda p, dpi=150, max_dim=1240: [
        ("page1", "image/jpeg", 100, 100), ("page2", "image/jpeg", 100, 100)])
    return calls


def _run(path):
    with pytest.raises(_StopAfterClassify) as exc:
        qs.process_document(path)
    return exc.value.args[0]   # raw_text handed to detect_language


def test_scanned_pdf_paddle_uses_text_path(pipeline, monkeypatch):
    monkeypatch.setattr(qs, "OCR_TEXT_SOURCE", "paddle")
    monkeypatch.setattr(qs, "_ocr_text_for_pdf", lambda p: GOOD_TEXT)
    assert _run("scan.pdf") == GOOD_TEXT
    assert pipeline["vision"] == []
    assert pipeline["classify"] == [GOOD_TEXT]


def test_scanned_pdf_weak_ocr_falls_back_to_vision(pipeline, monkeypatch):
    monkeypatch.setattr(qs, "OCR_TEXT_SOURCE", "paddle")
    monkeypatch.setattr(qs, "_ocr_text_for_pdf", lambda p: None)
    assert "vision text" in _run("scan.pdf")
    assert pipeline["vision"] and pipeline["classify"] == []


def test_scanned_pdf_vision_default_never_runs_ocr(pipeline, monkeypatch):
    def _no(p):
        raise AssertionError("OCR text source must not run with OCR_TEXT_SOURCE=vision")

    monkeypatch.setattr(qs, "OCR_TEXT_SOURCE", "vision")
    monkeypatch.setattr(qs, "_ocr_text_for_pdf", _no)
    _run("scan.pdf")
    assert pipeline["vision"]


def test_image_paddle_uses_text_path(pipeline, monkeypatch, tmp_path):
    p = tmp_path / "id.png"
    _img().save(p)
    monkeypatch.setattr(qs, "OCR_TEXT_SOURCE", "paddle")
    monkeypatch.setattr(qs, "_ocr_text_for_image", lambda path: GOOD_TEXT)
    assert _run(str(p)) == GOOD_TEXT
    assert pipeline["vision"] == []
    assert pipeline["classify"] == [GOOD_TEXT]


def test_image_paddle_forced_type_skips_classify(pipeline, monkeypatch, tmp_path):
    p = tmp_path / "id.png"
    _img().save(p)
    monkeypatch.setattr(qs, "OCR_TEXT_SOURCE", "paddle")
    monkeypatch.setattr(qs, "_ocr_text_for_image", lambda path: GOOD_TEXT)
    with pytest.raises(_StopAfterClassify):
        qs.process_document(str(p), forced_document_type="invoice")
    assert pipeline["vision"] == [] and pipeline["classify"] == []
