"""Unit tests for ocr_service.py. PaddleOCR is mocked (no model downloads, no
paddle install needed) and pytesseract.image_to_data is faked (no tesseract
binary needed), so this runs in CI as-is.

Run: pytest test_ocr_service.py -v
"""
import sys
import types

import numpy as np
import pytest
from PIL import Image

import ocr_service


def _line(text, box, score=0.99, tokens=None, token_boxes=None):
    return {"text": text, "box": box, "score": score, "tokens": tokens, "token_boxes": token_boxes}


class FakePaddle:
    """Mimics PaddleOCR 3.7.0's predict() result for one page."""

    def __init__(self, lines, word_boxes=True, fail=None):
        self.lines, self.word_boxes, self.fail = lines, word_boxes, fail
        self.calls = []

    def predict(self, arr, **kwargs):
        self.calls.append((arr, kwargs))
        if self.fail:
            raise self.fail
        res = {
            "rec_texts": [ln["text"] for ln in self.lines],
            "rec_scores": [ln["score"] for ln in self.lines],
            "rec_boxes": np.array([ln["box"] for ln in self.lines], dtype=np.int16).reshape(-1, 4),
        }
        if self.word_boxes and kwargs.get("return_word_box"):
            res["text_word"] = [ln["tokens"] for ln in self.lines]
            res["text_word_boxes"] = [np.array(ln["token_boxes"]) if ln["token_boxes"] else None
                                      for ln in self.lines]
        return [res]


@pytest.fixture(autouse=True)
def fresh_engine_state(monkeypatch):
    """Each test starts with no loaded engine and paddle as the configured one."""
    monkeypatch.setattr(ocr_service, "OCR_ENGINE", "paddle")
    monkeypatch.setattr(ocr_service, "_paddle", None)
    monkeypatch.setattr(ocr_service, "_paddle_import_failed", False)
    monkeypatch.setattr(ocr_service, "_paddle_retry_at", 0.0)
    monkeypatch.setattr(ocr_service, "_logged_engine", None)
    # _get_paddle imports torch first (Windows DLL order); keep tests from loading it
    monkeypatch.setitem(sys.modules, "torch", types.ModuleType("torch"))


def use_fake(monkeypatch, fake):
    monkeypatch.setattr(ocr_service, "_paddle", fake)
    return fake


IMG = Image.new("RGB", (400, 200), "white")


# ── Lines: box conversion, confidence filter, reading order ───────────────────

def test_lines_filter_low_conf_and_empty_and_sort(monkeypatch):
    use_fake(monkeypatch, FakePaddle([
        _line("second", (10, 100, 120, 120)),
        _line("faint watermark", (0, 50, 300, 70), score=0.3),
        _line("   ", (0, 0, 10, 10)),
        _line("first", (10, 10, 100, 30)),
    ], word_boxes=False))
    lines = ocr_service.ocr_lines(IMG)
    assert [ln["text"] for ln in lines] == ["first", "second"]
    assert lines[0]["box"] == (10, 10, 100, 30)
    assert set(lines[0]) == {"text", "box", "conf"}   # internal word_spans not exposed
    assert 0.0 <= lines[0]["conf"] <= 1.0


def test_same_row_sorted_left_to_right(monkeypatch):
    # "Total:" sits 3px lower than its value but is on the same visual row
    use_fake(monkeypatch, FakePaddle([
        _line("RM 128.50", (200, 50, 300, 70)),
        _line("Total:", (10, 53, 80, 73)),
        _line("Date:", (10, 10, 80, 30)),
    ], word_boxes=False))
    assert [ln["text"] for ln in ocr_service.ocr_lines(IMG)] == ["Date:", "Total:", "RM 128.50"]


def test_downscaled_input_and_boxes_scaled_back(monkeypatch):
    monkeypatch.setattr(ocr_service, "PADDLE_OCR_MAX_SIDE", 200)
    img = Image.new("RGB", (400, 200), (255, 0, 0))            # pure red
    fake = use_fake(monkeypatch, FakePaddle([_line("x", (10, 5, 50, 25))], word_boxes=False))
    lines = ocr_service.ocr_lines(img)
    arr = fake.calls[0][0]
    assert arr.shape == (100, 200, 3)                          # longest side capped at 200
    assert tuple(arr[0, 0]) == (0, 0, 255)                     # handed over as BGR
    assert lines[0]["box"] == (20, 10, 100, 50)                # back in original pixels


def test_boxes_clamped_to_image(monkeypatch):
    use_fake(monkeypatch, FakePaddle([_line("edge", (-5, -3, 450, 210))], word_boxes=False))
    assert ocr_service.ocr_lines(IMG)[0]["box"] == (0, 0, 400, 200)


# ── ocr_words: real word boxes, punctuation merging, char-count fallback ──────

def test_ocr_words_uses_word_boxes_and_merges_punctuation(monkeypatch):
    # Real PaddleOCR tokenization of a mixed-font header line: punctuation
    # tokens like ". (" span the space between "BHD" and "Company".
    tokens = ["SIMEN", " ", "UTARA", " ", "SDN", ". ", "BHD", ". (", "Company", " ", "No", ": ", "117752-D", ")"]
    xs = [(10, 60), (60, 64), (64, 120), (120, 124), (124, 160), (160, 170), (170, 210), (210, 222),
          (222, 270), (270, 273), (273, 285), (285, 292), (292, 340), (340, 345)]
    fake = use_fake(monkeypatch, FakePaddle([_line(
        "SIMEN UTARA SDN. BHD. (Company No: 117752-D)", (10, 20, 345, 60),
        tokens=tokens, token_boxes=[(a, 20, b, 60) for a, b in xs])]))
    words = ocr_service.ocr_words(IMG)
    assert fake.calls[0][1].get("return_word_box") is True
    assert [w["text"] for w in words] == ["SIMEN", "UTARA", "SDN.", "BHD.", "(Company", "No:", "117752-D)"]
    by_text = {w["text"]: w for w in words}
    assert (by_text["SIMEN"]["left"], by_text["SIMEN"]["width"]) == (10, 50)
    assert by_text["(Company"]["left"] == 218        # "(" is the last third of the ". (" token
    assert by_text["(Company"]["left"] + by_text["(Company"]["width"] == 270
    for w in words:
        assert set(w) == {"text", "left", "top", "width", "height", "conf"}
        assert (w["top"], w["height"]) == (20, 40) and 0 <= w["conf"] <= 100
        assert 10 <= w["left"] and w["left"] + w["width"] <= 345


def _assert_char_count_split(words, line_box, expected_texts):
    x0, y0, x1, y1 = line_box
    assert [w["text"] for w in words] == expected_texts
    for prev, nxt in zip(words, words[1:]):
        gap = nxt["left"] - (prev["left"] + prev["width"])
        assert 0 <= gap <= (x1 - x0) / 10                 # contiguous: only a space-sized gap
    for w in words:
        assert x0 <= w["left"] and w["left"] + w["width"] <= x1
        assert (w["top"], w["top"] + w["height"]) == (y0, y1)
    # proportional to character count
    assert words[0]["width"] > words[1]["width"]


def test_ocr_words_char_count_fallback_without_word_boxes(monkeypatch):
    box = (100, 40, 320, 60)
    use_fake(monkeypatch, FakePaddle([_line("Invoice No: INV-1", box)], word_boxes=False))
    _assert_char_count_split(ocr_service.ocr_words(IMG), box, ["Invoice", "No:", "INV-1"])


def test_ocr_words_char_count_fallback_when_tokens_dont_match_text(monkeypatch):
    box = (100, 40, 320, 60)
    use_fake(monkeypatch, FakePaddle([_line(
        "Invoice No: INV-1", box, tokens=["Invoice", " ", "No"], token_boxes=[(100, 40, 180, 60)] * 3)]))
    _assert_char_count_split(ocr_service.ocr_words(IMG), box, ["Invoice", "No:", "INV-1"])


# ── Normalized blocks ─────────────────────────────────────────────────────────

def test_line_blocks_normalized_shape_and_range(monkeypatch):
    use_fake(monkeypatch, FakePaddle([_line("a b", (0, 0, 400, 20)), _line("c", (100, 150, 250, 200))],
                                     word_boxes=False))
    blocks = ocr_service.ocr_line_blocks_normalized(IMG, page=3)
    assert len(blocks) == 2
    for b in blocks:
        assert set(b) == {"page", "text", "bbox"} and b["page"] == 3
        bb = b["bbox"]
        assert all(0.0 <= v <= 1.0 for v in bb.values())
        assert bb["x1"] < bb["x2"] and bb["y1"] < bb["y2"]
    assert blocks[1]["bbox"] == {"x1": 0.25, "y1": 0.75, "x2": 0.625, "y2": 1.0}


# ── Engine selection, fallback and errors ─────────────────────────────────────

FAKE_TESS = {
    "text": ["", "Invoice", "No:", "junk", "Date:"], "conf": ["-1", "96", "91.5", "12", "95"],
    "left": [0, 10, 80, 300, 10], "top": [0, 10, 12, 11, 50], "width": [0, 60, 30, 20, 50],
    "height": [0, 20, 18, 20, 20], "block_num": [1] * 5, "par_num": [1] * 5, "line_num": [0, 1, 1, 1, 2],
}


def test_paddle_import_failure_falls_back_to_tesseract_permanently(monkeypatch):
    monkeypatch.setitem(sys.modules, "paddleocr", None)        # import paddleocr -> ImportError
    monkeypatch.setattr(ocr_service.pytesseract, "image_to_data", lambda *a, **k: FAKE_TESS)
    assert ocr_service.active_engine() == "tesseract"
    assert ocr_service._paddle_import_failed is True
    assert [ln["text"] for ln in ocr_service.ocr_lines(IMG)] == ["Invoice No:", "Date:"]
    words = ocr_service.ocr_words(IMG)
    assert [(w["text"], w["conf"]) for w in words] == [("Invoice", 96), ("No:", 91), ("Date:", 95)]
    assert set(words[0]) == {"text", "left", "top", "width", "height", "conf"}


def test_paddle_load_error_retries_after_delay(monkeypatch):
    attempts = []

    class FlakyPaddleOCR:
        def __init__(self, **kwargs):
            attempts.append(kwargs)
            if len(attempts) == 1:
                raise RuntimeError("model download timed out")

    monkeypatch.setitem(sys.modules, "paddleocr", types.SimpleNamespace(PaddleOCR=FlakyPaddleOCR))
    clock = [1000.0]
    monkeypatch.setattr(ocr_service.time, "monotonic", lambda: clock[0])
    assert ocr_service.active_engine() == "tesseract"
    clock[0] += ocr_service.PADDLE_RETRY_SECONDS - 1
    assert ocr_service.active_engine() == "tesseract" and len(attempts) == 1   # too early to retry
    clock[0] += 2
    assert ocr_service.active_engine() == "paddle" and len(attempts) == 2
    assert ocr_service.active_engine() == "paddle" and len(attempts) == 2      # singleton reused
    assert attempts[0]["text_detection_model_name"] == "PP-OCRv5_mobile_det"


def test_tesseract_engine_forced_by_config(monkeypatch):
    monkeypatch.setattr(ocr_service, "OCR_ENGINE", "tesseract")
    monkeypatch.setitem(sys.modules, "paddleocr", None)
    assert ocr_service.active_engine() == "tesseract"
    assert ocr_service._paddle_import_failed is False          # paddle never even tried


def test_paddle_predict_failure_raises_ocr_error(monkeypatch, tmp_path):
    use_fake(monkeypatch, FakePaddle([], fail=RuntimeError("boom")))
    with pytest.raises(ocr_service.OCRError):
        ocr_service.ocr_lines(IMG)
    with pytest.raises(ocr_service.OCRError):
        ocr_service.ocr_words(IMG)
    with pytest.raises(ocr_service.OCRError):
        ocr_service.ocr_line_blocks_normalized(IMG)
    path = tmp_path / "x.png"
    IMG.save(path)
    with pytest.raises(ocr_service.OCRError):
        ocr_service.extract_text(str(path))


def test_tesseract_failure_raises_ocr_error(monkeypatch):
    monkeypatch.setattr(ocr_service, "OCR_ENGINE", "tesseract")

    def missing(*a, **k):
        raise ocr_service.pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(ocr_service.pytesseract, "image_to_data", missing)
    with pytest.raises(ocr_service.OCRError):
        ocr_service.ocr_lines(IMG)
    with pytest.raises(ocr_service.OCRError):
        ocr_service.ocr_words(IMG)


def test_extract_text_unreadable_file_raises_ocr_error(tmp_path):
    bad = tmp_path / "not_an_image.png"
    bad.write_bytes(b"not an image")
    with pytest.raises(ocr_service.OCRError):
        ocr_service.extract_text(str(bad))


def test_extract_text_joins_lines_and_alias(monkeypatch, tmp_path):
    use_fake(monkeypatch, FakePaddle([_line("line two", (0, 100, 50, 120)), _line("line one", (0, 0, 50, 20))],
                                     word_boxes=False))
    path = tmp_path / "x.png"
    IMG.save(path)
    assert ocr_service.extract_text(str(path)) == "line one\nline two"
    assert ocr_service.extract_with_tesseract is ocr_service.extract_text
