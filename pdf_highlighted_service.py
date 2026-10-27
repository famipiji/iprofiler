import os
import io
import hashlib
import threading
import fitz           # PyMuPDF
from PIL import Image

from ocr_service import active_engine, ocr_words


# ── Highlight colour (yellow, semi-transparent) ────────────────────────────────
HIGHLIGHT_COLOR = (1.0, 0.95, 0.0)   # RGB yellow for highlight rect stroke/fill
HIGHLIGHT_ALPHA = 0.35               # transparency


def generate_highlighted_pdf(doc_dict: dict, upload_folder: str) -> bytes:
    """
    Generate a PDF from the original image with:
        - Original image as background (full page, full quality)
        - Yellow highlight rectangle per detected word
        - Invisible selectable text per word (copyable)

    Falls back to plain image PDF if OCR fails, or text-only PDF if file not found.
    """
    file_type = doc_dict.get("file_type", "").lower()
    filename  = doc_dict.get("filename", "")
    raw_text  = doc_dict.get("raw_text", "") or ""

    # Prefer exact UUID filename stored at upload time
    original_path = None
    unique_fn = doc_dict.get("unique_upload_filename")
    if unique_fn:
        candidate = os.path.join(upload_folder, unique_fn)
        if os.path.isfile(candidate):
            original_path = candidate
    if not original_path:
        original_path = _find_uploaded_file(upload_folder, filename, file_type)

    if not original_path:
        return _fallback_pdf(doc_dict)

    doc_id = doc_dict.get("id")

    if file_type in ("jpg", "jpeg", "png", "webp", "tif", "tiff"):
        try:
            return _highlighted_from_image(original_path, raw_text, doc_id)
        except Exception as e:
            # NOT _image_only_pdf(original_path) - it re-opens the same file
            # via Image.open() at its own start, so a corrupt/unreadable image
            # would just raise the identical error again, unhandled. Fall
            # back to _fallback_pdf(doc_dict), which never re-opens the
            # original file at all.
            print(f"[Highlighted] Warning: highlight pass failed ({e}), using fallback PDF")
            return _fallback_pdf(doc_dict)
    elif file_type == "pdf":
        try:
            return _highlighted_from_pdf(original_path, raw_text, doc_id)
        except Exception as e:
            print(f"[Highlighted] Warning: PDF highlight pass failed ({e}), using fallback PDF")
            return _fallback_pdf(doc_dict)
    else:
        return _fallback_pdf(doc_dict)


# ── Core: image → highlighted PDF ─────────────────────────────────────────────

def _highlighted_from_image(image_path: str, raw_text: str, doc_id=None) -> bytes:
    """
    Open image → run OCR for word coords → build PDF page with
    image background + yellow highlight rects + invisible text.
    """
    img = Image.open(image_path).convert("RGB")
    img_w, img_h = img.size

    # ── Run OCR to get word-level bounding boxes (cached per doc) ──────────────
    word_data = _get_word_boxes_cached(img, doc_id, "img")

    # ── Build PDF ──────────────────────────────────────────────────────────────
    pdf = fitz.open()
    try:
        # Page size matches image aspect ratio, scaled to A4 width
        # Keep proportion: A4 width = 595pt
        PAGE_W = 595.0
        PAGE_H = PAGE_W * (img_h / img_w)
        page = pdf.new_page(width=PAGE_W, height=PAGE_H)

        # Scale factors from image pixels → PDF points
        sx = PAGE_W / img_w
        sy = PAGE_H / img_h

        # Layer 1: original image as background
        img_bytes = _pil_to_bytes(img)
        page.insert_image(
            fitz.Rect(0, 0, PAGE_W, PAGE_H),
            stream=img_bytes,
        )

        # Layer 2 + 3: highlight rect + invisible text per word
        for word in word_data:
            text = word["text"].strip()
            if not text:
                continue

            # Convert pixel coords → PDF points
            x0 = word["left"]  * sx
            y0 = word["top"]   * sy
            x1 = (word["left"] + word["width"])  * sx
            y1 = (word["top"]  + word["height"]) * sy

            rect = fitz.Rect(x0, y0, x1, y1)

            # Yellow highlight rectangle (semi-transparent)
            annot = page.add_highlight_annot(rect)
            annot.set_colors(stroke=HIGHLIGHT_COLOR)
            annot.set_opacity(HIGHLIGHT_ALPHA)
            annot.update()

            # Invisible text placed over the word (render_mode=3 = invisible)
            fontsize = max((y1 - y0) * 0.85, 4)
            try:
                page.insert_text(
                    fitz.Point(x0, y1 - 1),
                    text,
                    fontname="helv",
                    fontsize=fontsize,
                    color=(1, 1, 1),
                    render_mode=3,
                    morph=_fit_width_morph(text, fontsize, x0, x1, y1 - 1),
                )
            except Exception:
                pass

        buf = io.BytesIO()
        pdf.save(buf, garbage=4, deflate=True)
        return buf.getvalue()
    finally:
        pdf.close()


def _highlighted_from_pdf(pdf_path: str, raw_text: str, doc_id=None) -> bytes:
    """
    For uploaded PDFs: render each page as image, run OCR,
    add highlights and invisible text layer.
    """
    src = fitz.open(pdf_path)
    try:
        out = fitz.open()
        try:
            for page_num in range(len(src)):
                src_page = src[page_num]

                # Render at 2x for OCR accuracy
                mat = fitz.Matrix(2, 2)
                pix = src_page.get_pixmap(matrix=mat, alpha=False)
                img_bytes = pix.tobytes("png")

                # Convert to PIL for OCR
                img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                img_w, img_h = img.size

                word_data = _get_word_boxes_cached(img, doc_id, f"pdf:{page_num}")

                # PDF page at original (1x) dimensions
                orig_w = src_page.rect.width
                orig_h = src_page.rect.height
                new_page = out.new_page(width=orig_w, height=orig_h)

                # Scale: rendered image coords → original PDF points
                sx = orig_w / img_w
                sy = orig_h / img_h

                # Background image
                new_page.insert_image(
                    fitz.Rect(0, 0, orig_w, orig_h),
                    stream=img_bytes,
                )

                # Highlights + invisible text
                for word in word_data:
                    text = word["text"].strip()
                    if not text:
                        continue

                    x0 = word["left"]  * sx
                    y0 = word["top"]   * sy
                    x1 = (word["left"] + word["width"])  * sx
                    y1 = (word["top"]  + word["height"]) * sy

                    rect = fitz.Rect(x0, y0, x1, y1)

                    annot = new_page.add_highlight_annot(rect)
                    annot.set_colors(stroke=HIGHLIGHT_COLOR)
                    annot.set_opacity(HIGHLIGHT_ALPHA)
                    annot.update()

                    fontsize = max((y1 - y0) * 0.85, 4)
                    try:
                        new_page.insert_text(
                            fitz.Point(x0, y1 - 1),
                            text,
                            fontname="helv",
                            fontsize=fontsize,
                            color=(1, 1, 1),
                            render_mode=3,
                            morph=_fit_width_morph(text, fontsize, x0, x1, y1 - 1),
                        )
                    except Exception:
                        pass

            buf = io.BytesIO()
            out.save(buf, garbage=4, deflate=True)
            return buf.getvalue()
        finally:
            out.close()
    finally:
        src.close()


# ── OCR word boxes ─────────────────────────────────────────────────────────────
# _get_word_boxes() runs a fresh OCR pass (ocr_service.ocr_words) every call;
# for a multi-page PDF, repeated exports of the same document re-rasterize
# and re-OCR every page again from scratch. Cache by (doc_id, page tag,
# content hash of the rendered image) - the hash makes this self-invalidating
# whenever the source image actually changes (re-upload, re-extraction at a
# different DPI, etc.) without needing an explicit invalidation hook. The
# active OCR engine is part of the key so switching engines (or paddle coming
# back after a failed load) never serves the other engine's boxes.
_word_box_cache = {}
_word_box_cache_lock = threading.Lock()
_WORD_BOX_CACHE_MAX_ENTRIES = 300


def _get_word_boxes_cached(img: Image.Image, doc_id, page_tag: str) -> list:
    key = f"{active_engine()}:{doc_id}:{page_tag}:{hashlib.sha256(img.tobytes()).hexdigest()}"
    with _word_box_cache_lock:
        cached = _word_box_cache.get(key)
        if cached is not None:
            return cached
    words = _get_word_boxes(img)
    if not words:
        # Don't cache: [] is also what an OCR failure returns, and a transient
        # failure shouldn't stick to this page until the process restarts.
        return words
    with _word_box_cache_lock:
        if len(_word_box_cache) >= _WORD_BOX_CACHE_MAX_ENTRIES:
            _word_box_cache.pop(next(iter(_word_box_cache)))  # evict oldest insertion
        _word_box_cache[key] = words
    return words


def _get_word_boxes(img: Image.Image) -> list:
    """
    Word boxes from the active OCR engine (ocr_service.ocr_words):
    [{ text, left, top, width, height, conf }, ...]
    Empty and low-confidence words are already filtered out. Returns [] if
    OCR fails, so the export still produces a (highlight-free) PDF.
    """
    try:
        return ocr_words(img)
    except Exception:
        return []


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fit_width_morph(text: str, fontsize: float, x0: float, x1: float, baseline_y: float):
    """insert_text() morph that stretches/squeezes the invisible word to span
    exactly x0..x1. Helvetica's natural width rarely matches the scanned font,
    so without this Ctrl+F hits cover only part of a word, or run into the
    next one (and adjacent words merge, breaking phrase search)."""
    natural_w = fitz.get_text_length(text, fontname="helv", fontsize=fontsize)
    if natural_w <= 0 or x1 <= x0:
        return None
    return (fitz.Point(x0, baseline_y), fitz.Matrix((x1 - x0) / natural_w, 1))


def _pil_to_bytes(img: Image.Image, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _find_uploaded_file(upload_folder: str, original_filename: str, file_type: str):
    """Find most recently uploaded file of matching extension."""
    if not os.path.exists(upload_folder):
        return None

    ext = file_type.lower()
    if ext == "jpeg":
        ext = "jpg"

    candidates = []
    for f in os.listdir(upload_folder):
        flow = f.lower()
        if flow.endswith(f".{ext}") or (ext == "jpg" and flow.endswith(".jpeg")):
            full = os.path.join(upload_folder, f)
            if os.path.isfile(full):
                candidates.append((os.path.getmtime(full), full))

    if not candidates:
        return None

    candidates.sort(reverse=True)
    return candidates[0][1]


def _image_only_pdf(image_path: str) -> bytes:
    """Embed image directly into a PDF page with no OCR/highlight layer."""
    img = Image.open(image_path).convert("RGB")
    img_w, img_h = img.size
    PAGE_W = 595.0
    PAGE_H = PAGE_W * (img_h / img_w)
    pdf  = fitz.open()
    page = pdf.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_image(fitz.Rect(0, 0, PAGE_W, PAGE_H), stream=_pil_to_bytes(img))
    buf = io.BytesIO()
    pdf.save(buf, garbage=4, deflate=True)
    pdf.close()
    return buf.getvalue()


def _fallback_pdf(doc_dict: dict) -> bytes:
    """Plain text PDF fallback when original image not found."""
    pdf = fitz.open()
    page = pdf.new_page()

    raw_text  = doc_dict.get("raw_text", "") or "(no text available)"
    filename  = doc_dict.get("filename", "Unknown")
    doc_type  = doc_dict.get("document_type", "other")
    confidence = doc_dict.get("confidence", "")

    page.insert_text(fitz.Point(50, 50),
        "VaultMind — Highlighted Document (original not found)",
        fontsize=13, color=(0, 0.2, 0.6))
    page.insert_text(fitz.Point(50, 72),
        f"File: {filename}  |  Type: {doc_type}  |  Confidence: {confidence}",
        fontsize=9, color=(0.35, 0.47, 0.6))
    page.draw_line(fitz.Point(50, 85), fitz.Point(545, 85),
        color=(0.7, 0.85, 1.0), width=0.5)

    y = 105
    for line in raw_text.splitlines():
        if y > 800:
            page = pdf.new_page()
            y = 50
        page.insert_text(fitz.Point(50, y), line,
            fontsize=9, color=(0.04, 0.1, 0.2))
        y += 14

    buf = io.BytesIO()
    pdf.save(buf)
    pdf.close()
    return buf.getvalue()