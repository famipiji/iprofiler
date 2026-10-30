"""Unit tests for the OCR_PRETEXT_HINT feature in qwen_service.py (PaddleOCR
migration, Task 7, option A): local OCR text is prepended to Qwen's Vision
transcription prompts as a reference.

OCR is mocked (ocr_service.ocr_lines), PyMuPDF is faked, and call_qwen_vision
is captured - no models, no LM Studio, no DB needed.

Run: pytest test_ocr_hint.py -v
"""
import sys
import types

import pytest
from PIL import Image

import ocr_service
import qwen_service as qs


def _lines(*items):
    return [{"text": t, "box": (0, i * 10, 100, i * 10 + 8), "conf": c} for i, (t, c) in enumerate(items)]


GOOD = _lines(("SIMEN UTARA SDN. BHD.", 0.98), ("INVOICE BK1141557", 0.97), ("TOTAL 11,494.50", 0.99))


@pytest.fixture
def hint_on(monkeypatch):
    monkeypatch.setattr(qs, "OCR_PRETEXT_HINT", True)


@pytest.fixture
def fake_ocr(monkeypatch):
    """Replace ocr_service.ocr_lines; returns the list of images it was called with."""
    state = {"result": GOOD, "calls": []}

    def _fake(img):
        state["calls"].append(img)
        if isinstance(state["result"], Exception):
            raise state["result"]
        return state["result"]

    monkeypatch.setattr(ocr_service, "ocr_lines", _fake)
    return state


def _img():
    return Image.new("RGB", (200, 100), "white")


# -- flag off: nothing changes ------------------------------------------------

def test_off_by_default_no_ocr_and_prompt_unchanged(monkeypatch, fake_ocr):
    monkeypatch.setattr(qs, "OCR_PRETEXT_HINT", False)
    assert qs._ocr_hint_for_image(_img()) == ""
    assert qs._ocr_hints_for_pdf("whatever.pdf") == []
    assert qs._ocr_hint_for_path("missing.jpg") == ""
    assert fake_ocr["calls"] == []
    assert qs._with_ocr_hint("PROMPT", "") == "PROMPT"


def test_env_flag_parsing(monkeypatch):
    import importlib
    for val, expected in [("1", True), ("true", True), ("ON", True), ("0", False), ("", False), ("no", False)]:
        monkeypatch.setenv("OCR_PRETEXT_HINT", val)
        mod = importlib.reload(qs)
        assert mod.OCR_PRETEXT_HINT is expected, val
    monkeypatch.delenv("OCR_PRETEXT_HINT")
    assert importlib.reload(qs).OCR_PRETEXT_HINT is False


# -- _ocr_hint_for_image ------------------------------------------------------

def test_good_ocr_becomes_hint_text(hint_on, fake_ocr):
    assert qs._ocr_hint_for_image(_img()) == "SIMEN UTARA SDN. BHD.\nINVOICE BK1141557\nTOTAL 11,494.50"


def test_low_mean_conf_gives_no_hint(hint_on, fake_ocr):
    # e.g. the English model on a Jawi page
    fake_ocr["result"] = _lines(("abcdefghij klmnop", 0.6), ("qrstuvwxyz 12345", 0.7))
    assert qs._ocr_hint_for_image(_img()) == ""


def test_too_little_text_gives_no_hint(hint_on, fake_ocr):
    fake_ocr["result"] = _lines(("RM 5", 0.99))
    assert qs._ocr_hint_for_image(_img()) == ""
    fake_ocr["result"] = []
    assert qs._ocr_hint_for_image(_img()) == ""


def test_ocr_failure_never_raises(hint_on, fake_ocr):
    fake_ocr["result"] = ocr_service.OCRError("paddle exploded")
    assert qs._ocr_hint_for_image(_img()) == ""
    fake_ocr["result"] = RuntimeError("anything else")
    assert qs._ocr_hint_for_image(_img()) == ""


def test_long_text_truncated_on_a_line_boundary(hint_on, fake_ocr, monkeypatch):
    monkeypatch.setattr(qs, "OCR_PRETEXT_MAX_CHARS", 30)
    fake_ocr["result"] = _lines(("0123456789 abcdefgh", 0.99), ("second line that overflows", 0.99))
    assert qs._ocr_hint_for_image(_img()) == "0123456789 abcdefgh"


# -- _ocr_hint_for_path / _ocr_hints_for_pdf ----------------------------------

def test_hint_for_path_reads_the_image(hint_on, fake_ocr, tmp_path):
    p = tmp_path / "x.png"
    _img().save(p)
    assert qs._ocr_hint_for_path(str(p)).startswith("SIMEN UTARA")
    assert fake_ocr["calls"][0].size == (200, 100)


def test_hint_for_unreadable_path_is_empty(hint_on, fake_ocr, tmp_path):
    p = tmp_path / "bad.png"
    p.write_bytes(b"not an image")
    assert qs._ocr_hint_for_path(str(p)) == ""


class _FakePix:
    def __init__(self, w, h):
        self.width, self.height, self.samples = w, h, b"\xff" * (w * h * 3)


class _FakeDoc:
    def __init__(self, n_pages):
        self.n = n_pages
        self.zooms = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        for _ in range(self.n):
            page = types.SimpleNamespace()

            def get_pixmap(matrix, _doc=self):
                _doc.zooms.append(matrix)
                return _FakePix(40, 20)

            page.get_pixmap = get_pixmap
            yield page


def _fake_fitz(monkeypatch, doc=None, open_error=None):
    def _open(path):
        if open_error:
            raise open_error
        return doc

    mod = types.SimpleNamespace(open=_open, Matrix=lambda a, b: (a, b))
    monkeypatch.setitem(sys.modules, "fitz", mod)


def test_pdf_one_hint_per_page_at_ocr_dpi(hint_on, fake_ocr, monkeypatch):
    results = iter([GOOD, _lines(("weak", 0.3)), GOOD])
    monkeypatch.setattr(ocr_service, "ocr_lines", lambda img: next(results))
    doc = _FakeDoc(3)
    _fake_fitz(monkeypatch, doc)
    hints = qs._ocr_hints_for_pdf("scan.pdf")
    assert len(hints) == 3
    assert hints[0].startswith("SIMEN") and hints[1] == "" and hints[2].startswith("SIMEN")
    zoom = qs.OCR_PRETEXT_PDF_DPI / 72.0
    assert doc.zooms == [(zoom, zoom)] * 3


def test_pdf_render_failure_gives_no_hints(hint_on, fake_ocr, monkeypatch):
    _fake_fitz(monkeypatch, open_error=RuntimeError("broken pdf"))
    assert qs._ocr_hints_for_pdf("broken.pdf") == []


# -- _with_ocr_hint -----------------------------------------------------------

def test_with_hint_prepends_reference_and_keeps_prompt_last():
    out = qs._with_ocr_hint("TRANSCRIBE PROMPT", "line one\nline two")
    assert out.endswith("TRANSCRIBE PROMPT")
    assert "<<<OCR\nline one\nline two\nOCR>>>" in out
    assert out.index("line two") < out.index("TRANSCRIBE PROMPT")
    assert "follow the image" in out


# -- classify_and_extract_combined_vision wiring ------------------------------

@pytest.fixture
def capture_vision(monkeypatch):
    prompts = []

    def _fake_call(b64, media_type, prompt, max_tokens=100, **kw):
        prompts.append(prompt)
        return "TEXT:\nhello\nFIELDS:\n{\"name\": \"x\"}"

    monkeypatch.setattr(qs, "call_qwen_vision", _fake_call)
    monkeypatch.setattr(qs, "get_all_active_doc_type_names", lambda: ["invoice", "resume"])
    return prompts


def test_combined_text_only_path_gets_hint(capture_vision, monkeypatch):
    monkeypatch.setattr(qs, "_build_vision_extraction_hints", lambda t, raw_text=None: ([], "", ""))
    qs.classify_and_extract_combined_vision("b64", "image/jpeg", forced_document_type="no_schema_type",
                                            ocr_hint="REF TEXT")
    assert len(capture_vision) == 1
    assert "REF TEXT" in capture_vision[0]
    assert capture_vision[0].endswith(qs.DEFAULT_COMBINED_TEXT_ONLY_PROMPT)


def test_combined_table_path_gets_hint(capture_vision, monkeypatch):
    monkeypatch.setattr(qs, "_build_vision_extraction_hints", lambda t, raw_text=None: ([], "", ""))
    monkeypatch.setitem(qs.DOCUMENT_STRUCTURE_HINTS, "invoice", qs.DOCUMENT_STRUCTURE_HINTS.get("invoice", "x"))
    res = qs.classify_and_extract_combined_vision("b64", "image/jpeg", forced_document_type="invoice",
                                                  ocr_hint="REF TEXT")
    assert res["fields_reliable"] is False
    assert len(capture_vision) == 1 and "REF TEXT" in capture_vision[0]


def test_combined_fields_path_gets_hint_but_classify_call_does_not(capture_vision, monkeypatch):
    monkeypatch.setattr(qs, "_build_vision_extraction_hints",
                        lambda t, raw_text=None: ([{"field_name": "name", "field_type": "text"}], "", ""))
    res = qs.classify_and_extract_combined_vision("b64", "image/jpeg", ocr_hint="REF TEXT")
    assert len(capture_vision) == 2           # classify-only + combined OCR/fields
    assert "REF TEXT" not in capture_vision[0]
    assert "REF TEXT" in capture_vision[1]
    assert res["raw_text"] == "hello" and res["fields"] == {"name": "x"}


def test_combined_without_hint_sends_original_prompts(capture_vision, monkeypatch):
    monkeypatch.setattr(qs, "_build_vision_extraction_hints", lambda t, raw_text=None: ([], "", ""))
    qs.classify_and_extract_combined_vision("b64", "image/jpeg", forced_document_type="no_schema_type")
    assert capture_vision == [qs.DEFAULT_COMBINED_TEXT_ONLY_PROMPT]


# -- process_document wiring (stops right after the OCR stage) ----------------

class _StopAfterOCR(Exception):
    pass


@pytest.fixture
def pipeline(monkeypatch, capture_vision):
    """process_document() with Vision captured and a stop just after OCR."""
    def _stop(text):
        raise _StopAfterOCR(text)

    monkeypatch.setattr(qs, "detect_language", _stop)
    monkeypatch.setattr(qs, "_build_vision_extraction_hints", lambda t, raw_text=None: ([], "", ""))
    return capture_vision


def _fake_pages(monkeypatch, n):
    monkeypatch.setattr(qs, "is_scanned_pdf", lambda p: True)
    monkeypatch.setattr(qs, "pdf_pages_to_b64", lambda p, dpi=150, max_dim=1240: [
        (f"page{i}", "image/jpeg", 100, 100) for i in range(1, n + 1)])


def _run(path):
    with pytest.raises(_StopAfterOCR):
        qs.process_document(path)


def test_scanned_multipage_each_page_gets_its_own_hint(pipeline, monkeypatch):
    _fake_pages(monkeypatch, 3)
    monkeypatch.setattr(qs, "_ocr_hints_for_pdf", lambda p: ["HINT-P1", "", "HINT-P3"])
    _run("scan.pdf")
    assert len(pipeline) == 3
    page1 = [p for p in pipeline if "TYPE:" in p]          # combined classify+OCR prompt
    others = [p for p in pipeline if "TYPE:" not in p]
    assert len(page1) == 1 and "HINT-P1" in page1[0]
    assert sum("HINT-P3" in p for p in others) == 1
    assert sum("Reference OCR text" not in p for p in others) == 1   # page 2: no hint
    assert not any("HINT-P1" in p for p in others)


def test_scanned_single_page_hint_reaches_combined_call(pipeline, monkeypatch):
    _fake_pages(monkeypatch, 1)
    monkeypatch.setattr(qs, "_ocr_hints_for_pdf", lambda p: ["HINT-ONLY"])
    _run("scan.pdf")
    assert any("HINT-ONLY" in p for p in pipeline)


def test_scanned_flag_off_prompts_have_no_reference(pipeline, monkeypatch):
    monkeypatch.setattr(qs, "OCR_PRETEXT_HINT", False)
    _fake_pages(monkeypatch, 2)
    _run("scan.pdf")
    assert len(pipeline) == 2
    assert not any("Reference OCR text" in p for p in pipeline)


def test_image_upload_hint_reaches_combined_call(pipeline, monkeypatch, tmp_path):
    p = tmp_path / "id.png"
    _img().save(p)
    monkeypatch.setattr(qs, "_ocr_hint_for_path", lambda path: "HINT-IMG")
    _run(str(p))
    assert any("HINT-IMG" in pr for pr in pipeline)
