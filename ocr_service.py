"""Single OCR entry point. No other module should import pytesseract or
paddleocr directly - go through ocr_lines / ocr_words / extract_text /
ocr_line_blocks_normalized so the engine can be switched in one place.

PaddleOCR 3.7.0 result keys relied on below (per page, from .predict()):
  rec_texts  - list[str], one per detected text line
  rec_scores - list[float], 0..1
  rec_boxes  - (N, 4) int array, axis-aligned [x0, y0, x1, y1] in input pixels
  text_word / text_word_boxes - only with predict(..., return_word_box=True):
               per line, the recognizer's tokens and their [x0, y0, x1, y1]
               boxes. Tokens are words, spaces, or punctuation runs that can
               span a space (". (" ) - see _merge_word_tokens.
"""
import os
import threading
import time

import numpy as np
from PIL import Image
import pytesseract

# ── Config ────────────────────────────────────────────────────────────────────
# OCR_ENGINE: "paddle" (default) | "tesseract". Paddle falls back to Tesseract
# automatically if it can't be imported or loaded (see _get_paddle for retry).
OCR_ENGINE = os.environ.get("OCR_ENGINE", "paddle").strip().lower()
PADDLE_OCR_LANG = os.environ.get("PADDLE_OCR_LANG", "en")
# "mobile" is ~3x faster than "server" on CPU and read our receipts at least as
# well (see .tasks/2026-10-27-paddleocr-migration.md, Task 1 notes).
PADDLE_OCR_MODEL = os.environ.get("PADDLE_OCR_MODEL", "mobile").strip().lower()
# Images are downscaled so their longest side is at most this before predict();
# boxes are scaled back to original pixels. Bigger = slower, rarely more accurate.
PADDLE_OCR_MAX_SIDE = int(os.environ.get("PADDLE_OCR_MAX_SIDE", "1600"))
# Paddle lines below this score are dropped - also filters most of the faint
# background watermark text the mobile model picks up.
MIN_LINE_CONF = 0.5
# Tesseract word conf is 0..100; matches the old pdf_highlighted_service filter.
MIN_TESSERACT_WORD_CONF = 30

# ── Tesseract path ────────────────────────────────────────────────────────────
# Set TESSERACT_CMD (e.g. Windows: C:\Program Files\Tesseract-OCR\tesseract.exe) for
# local dev where tesseract isn't on PATH. Leave unset on Docker/Linux, where
# pytesseract finds the `tesseract-ocr` package via PATH automatically.
_tesseract_cmd = os.environ.get("TESSERACT_CMD")
if _tesseract_cmd:
    pytesseract.pytesseract.tesseract_cmd = _tesseract_cmd


class OCRError(Exception):
    """Raised when Tesseract OCR fails - callers must not store/display this
    as if it were extracted document text (the previous behavior returned
    'OCR extraction failed: ...' through the same path as real OCR output,
    so a real failure could end up saved as a document's raw_text and shown
    to users as if it were the document's actual content)."""


# ── PaddleOCR singleton ───────────────────────────────────────────────────────
# A PaddleOCR instance is heavy (hundreds of MB, seconds to load) and not
# thread-safe, while document_routes runs one thread per uploaded file: build
# it once lazily, and serialize every predict() call.
_paddle = None
# ImportError means paddleocr isn't installed - permanent until restart. Any
# other load failure (model download hiccup, DLL load error, out of memory)
# may be transient, so loading is retried after PADDLE_RETRY_SECONDS instead
# of leaving the app on Tesseract until the next restart.
_paddle_import_failed = False
_paddle_retry_at = 0.0          # time.monotonic() before which no reload is tried
PADDLE_RETRY_SECONDS = 60
_paddle_init_lock = threading.Lock()
_paddle_infer_lock = threading.Lock()
_logged_engine = None


def _get_paddle():
    """Return the shared PaddleOCR instance, or None if it can't be loaded."""
    global _paddle, _paddle_import_failed, _paddle_retry_at
    if _paddle is not None or _paddle_import_failed or time.monotonic() < _paddle_retry_at:
        return _paddle
    with _paddle_init_lock:
        if _paddle is not None or _paddle_import_failed or time.monotonic() < _paddle_retry_at:
            return _paddle
        try:
            # Windows: torch must be loaded before paddle. Both wheels ship
            # libiomp5md.dll and whichever loads first wins; paddle's older
            # copy makes a later `import torch` (e.g. rag_service's
            # sentence-transformers) fail with WinError 127 on shm.dll.
            try:
                import torch  # noqa: F401
            except ImportError:
                pass
            from paddleocr import PaddleOCR

            kwargs = dict(
                lang=PADDLE_OCR_LANG,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            )
            if PADDLE_OCR_MODEL == "mobile":
                kwargs["text_detection_model_name"] = "PP-OCRv5_mobile_det"
                # The mobile recognizer is per-language; for anything other
                # than English let `lang` pick the recognition model.
                if PADDLE_OCR_LANG == "en":
                    kwargs["text_recognition_model_name"] = "en_PP-OCRv5_mobile_rec"
            t = time.time()
            _paddle = PaddleOCR(**kwargs)
            print(f"[OCR] PaddleOCR loaded in {time.time() - t:.1f}s")
        except ImportError as e:
            print(f"[OCR] PaddleOCR not installed, using Tesseract: {e}")
            _paddle_import_failed = True
        except Exception as e:
            print(f"[OCR] PaddleOCR failed to load, using Tesseract; "
                  f"retrying in {PADDLE_RETRY_SECONDS}s: {e}")
            _paddle_retry_at = time.monotonic() + PADDLE_RETRY_SECONDS
    return _paddle


def active_engine() -> str:
    """'paddle' or 'tesseract' - the engine the helpers below will actually use."""
    global _logged_engine
    engine = "paddle" if OCR_ENGINE != "tesseract" and _get_paddle() is not None else "tesseract"
    # Logged on every change, not just once: a paddle load retry can move the
    # engine from tesseract to paddle mid-run.
    if engine != _logged_engine:
        _logged_engine = engine
        if engine == "paddle":
            print(f"[OCR] Engine: paddle (lang={PADDLE_OCR_LANG}, model={PADDLE_OCR_MODEL})")
        else:
            print("[OCR] Engine: tesseract (lang=eng)")
    return engine


# ── Engine backends ───────────────────────────────────────────────────────────

def _paddle_lines(img: Image.Image) -> list:
    ocr = _get_paddle()
    w, h = img.size
    scale = min(1.0, PADDLE_OCR_MAX_SIDE / max(w, h))
    src = img.convert("RGB")
    if scale < 1.0:
        src = src.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    # PaddleX treats ndarray input as OpenCV-style BGR.
    arr = np.ascontiguousarray(np.array(src)[:, :, ::-1])

    t = time.time()
    with _paddle_infer_lock:
        # Word boxes cost nothing measurable (4.3s vs 4.4s on an A4 scan).
        res = ocr.predict(arr, return_word_box=True)[0]
    texts, scores, boxes = res["rec_texts"], res["rec_scores"], res["rec_boxes"]
    word_tokens = res.get("text_word")
    word_boxes = res.get("text_word_boxes")
    if word_tokens is None or word_boxes is None or len(word_tokens) != len(texts):
        word_tokens = word_boxes = [None] * len(texts)

    lines = []
    for text, score, box, tokens, tboxes in zip(texts, scores, boxes, word_tokens, word_boxes):
        text = str(text).strip()
        conf = float(score)
        if not text or conf < MIN_LINE_CONF:
            continue
        x0, y0, x1, y1 = (float(v) / scale for v in box)
        x0, x1 = max(0, round(x0)), min(w, round(x1))
        y0, y1 = max(0, round(y0)), min(h, round(y1))
        if x1 <= x0 or y1 <= y0:
            continue
        lines.append({"text": text, "box": (x0, y0, x1, y1), "conf": conf,
                      "word_spans": _merge_word_tokens(text, tokens, tboxes, scale, x0, x1)})
    print(f"[OCR] paddle {time.time() - t:.1f}s, {len(lines)} lines")
    return lines


def _merge_word_tokens(line_text: str, tokens, boxes, scale: float, line_x0: int, line_x1: int):
    """Turn one line's recognizer tokens into whitespace-delimited words:
    [(text, x0, x1), ...] in original-image pixels, or None if the tokens are
    missing or don't reproduce line_text's words exactly (callers then fall
    back to splitting the line box by character count).

    Tokens can be a word ("SIMEN"), a space, or punctuation that spans a space
    (". (" between "BHD" and "Company"), so this works per character: each
    token's box is shared evenly among its characters, whitespace ends a word,
    and anything else extends the current word."""
    if not tokens or boxes is None or len(tokens) != len(boxes):
        return None
    words, cur = [], None
    for tok, box in zip(tokens, boxes):
        tok = str(tok)
        if not tok:
            continue
        bx0, bx1 = float(box[0]) / scale, float(box[2]) / scale
        step = (bx1 - bx0) / len(tok)
        for i, ch in enumerate(tok):
            if ch.isspace():
                if cur:
                    words.append(cur)
                cur = None
                continue
            cx0, cx1 = bx0 + i * step, bx0 + (i + 1) * step
            if cur is None:
                cur = [ch, cx0, cx1]
            else:
                cur[0] += ch
                cur[1], cur[2] = min(cur[1], cx0), max(cur[2], cx1)
    if cur:
        words.append(cur)
    if [wd[0] for wd in words] != line_text.split():
        return None
    return [(t, max(line_x0, round(a)), min(line_x1, round(b))) for t, a, b in words]


def _tesseract_words(img: Image.Image) -> list:
    """Tesseract image_to_data() as word dicts [{text, left, top, width,
    height, conf (0..100), line_key}], empty/low-confidence words removed."""
    data = pytesseract.image_to_data(
        img,
        output_type=pytesseract.Output.DICT,
        config="--psm 3"   # auto page segmentation
    )
    words = []
    for i in range(len(data["text"])):
        text = str(data["text"][i]).strip()
        conf = int(float(data["conf"][i])) if str(data["conf"][i]).lstrip("-").replace(".", "", 1).isdigit() else -1
        if not text or conf < MIN_TESSERACT_WORD_CONF:
            continue
        words.append({
            "text":   text,
            "left":   int(data["left"][i]),
            "top":    int(data["top"][i]),
            "width":  int(data["width"][i]),
            "height": int(data["height"][i]),
            "conf":   conf,
            "line_key": (data["block_num"][i], data["par_num"][i], data["line_num"][i]),
        })
    return words


def _tesseract_lines(img: Image.Image) -> list:
    grouped = {}
    for wd in _tesseract_words(img):
        grouped.setdefault(wd["line_key"], []).append(wd)
    lines = []
    for words in grouped.values():
        words.sort(key=lambda wd: wd["left"])
        lines.append({
            "text": " ".join(wd["text"] for wd in words),
            "box": (min(wd["left"] for wd in words),
                    min(wd["top"] for wd in words),
                    max(wd["left"] + wd["width"] for wd in words),
                    max(wd["top"] + wd["height"] for wd in words)),
            "conf": sum(wd["conf"] for wd in words) / len(words) / 100.0,
        })
    return lines


def _reading_order(lines: list) -> list:
    """Sort top-to-bottom, then left-to-right. Lines whose vertical centres are
    within half a typical line height count as the same row, so a label and
    its value on one row don't get split by a pixel or two of skew."""
    if not lines:
        return lines
    heights = sorted(ln["box"][3] - ln["box"][1] for ln in lines)
    tol = max(1, heights[len(heights) // 2]) / 2
    by_y = sorted(lines, key=lambda ln: (ln["box"][1] + ln["box"][3]) / 2)
    rows, row, row_y = [], [], None
    for ln in by_y:
        cy = (ln["box"][1] + ln["box"][3]) / 2
        if row and cy - row_y > tol:
            rows.append(row)
            row = []
        if not row:
            row_y = cy
        row.append(ln)
    rows.append(row)
    return [ln for r in rows for ln in sorted(r, key=lambda ln: ln["box"][0])]


# ── Public API ────────────────────────────────────────────────────────────────

def ocr_lines(img: Image.Image) -> list:
    """Text lines in reading order:
    [{"text": str, "box": (x0, y0, x1, y1) in image pixels, "conf": 0..1}, ...]
    Raises OCRError on failure."""
    return [{k: v for k, v in ln.items() if k != "word_spans"} for ln in _ocr_lines_with_spans(img)]


def _ocr_lines_with_spans(img: Image.Image) -> list:
    """ocr_lines() plus, for paddle lines, "word_spans" from _merge_word_tokens."""
    engine = active_engine()
    try:
        lines = _paddle_lines(img) if engine == "paddle" else _tesseract_lines(img)
    except Exception as e:
        print(f"[OCR] {engine} failed: {e}")
        raise OCRError(f"{engine} OCR failed: {e}") from e
    return _reading_order(lines)


def ocr_words(img: Image.Image) -> list:
    """Tesseract-shaped word boxes [{text, left, top, width, height, conf (0..100)}].

    With PaddleOCR, word x-positions come from the recognizer's own word boxes
    (see _merge_word_tokens); y-positions are the line's. If a line has no
    usable word boxes, its box is divided horizontally in proportion to each
    word's character count instead (the space between words counts as one
    character) - wrong on lines that mix font sizes, hence only a fallback.
    Raises OCRError on failure."""
    if active_engine() == "tesseract":
        try:
            words = _tesseract_words(img)
        except Exception as e:
            print(f"[OCR] tesseract failed: {e}")
            raise OCRError(f"tesseract OCR failed: {e}") from e
        for wd in words:
            del wd["line_key"]
        return words

    words = []
    for ln in _ocr_lines_with_spans(img):
        x0, y0, x1, y1 = ln["box"]
        conf = round(ln["conf"] * 100)
        if ln.get("word_spans"):
            for text, left, right in ln["word_spans"]:
                words.append({
                    "text": text,
                    "left": left,
                    "top": y0,
                    "width": max(1, right - left),
                    "height": y1 - y0,
                    "conf": conf,
                })
            continue
        parts = ln["text"].split()
        units = sum(len(p) for p in parts) + len(parts) - 1
        unit_w = (x1 - x0) / units
        cursor = 0
        for p in parts:
            left = x0 + round(cursor * unit_w)
            right = x0 + round((cursor + len(p)) * unit_w)
            words.append({
                "text": p,
                "left": left,
                "top": y0,
                "width": max(1, min(right, x1) - left),
                "height": y1 - y0,
                "conf": conf,
            })
            cursor += len(p) + 1
    return words


def ocr_line_blocks_normalized(img: Image.Image, page: int = 1) -> list:
    """[{"page", "text", "bbox": {x1, y1, x2, y2}}] with coords as 0..1
    fractions of the image size (top-left origin) - the same shape
    qwen_service.build_image_blocks() returns. Raises OCRError on failure."""
    w, h = img.size
    return [{
        "page": page,
        "text": ln["text"],
        "bbox": {
            "x1": round(ln["box"][0] / w, 4),
            "y1": round(ln["box"][1] / h, 4),
            "x2": round(ln["box"][2] / w, 4),
            "y2": round(ln["box"][3] / h, 4),
        },
    } for ln in ocr_lines(img)]


def extract_text(image_path: str) -> str:
    """Plain text of an image, one OCR line per output line.
    Raises OCRError on failure - callers should catch this explicitly rather
    than treating any string return value as real extracted text."""
    try:
        with Image.open(image_path) as image:
            lines = ocr_lines(image)
    except OCRError:
        raise
    except Exception as e:
        print(f"[OCR] Could not read image: {e}")
        raise OCRError(f"OCR failed for '{os.path.basename(image_path)}': {e}") from e
    return "\n".join(ln["text"] for ln in lines)


# Kept so any external caller of the old name keeps working; now uses the
# configured engine rather than always Tesseract.
extract_with_tesseract = extract_text
