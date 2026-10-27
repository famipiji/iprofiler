import os
import io
import hashlib
from datetime import datetime
import fitz  # PyMuPDF
from PIL import Image

import ocr_service

# All possible upload folder locations (current + legacy path from old double-dirname bug)
_HERE = os.path.dirname(os.path.abspath(__file__))
_UPLOAD_FOLDERS = [
    os.path.join(_HERE, "uploads"),
    os.path.join(os.path.dirname(_HERE), "uploads"),
]

# _searchable_from_image/_searchable_from_pdf run a full OCR pass on every
# export request for the same document. Cache the generated PDF on disk keyed
# by doc id + configured OCR engine + a content hash of the source file, so a
# second export of the same (unchanged) document is served from disk instead
# of re-OCRing. The hash makes this self-invalidating whenever the source
# file actually changes; the engine keeps one engine's output from being
# served after switching OCR_ENGINE. The configured engine (not
# ocr_service.active_engine()) is used so this stays a cheap lookup -
# is_searchable_cached() runs for every row of the document list, and
# active_engine() can trigger loading PaddleOCR.
_SEARCHABLE_CACHE_DIR = os.path.join(_HERE, "searchable_pdf_cache")


def _file_content_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:20]


def _searchable_cache_path(doc_id, source_path: str) -> str | None:
    if doc_id is None:
        return None
    try:
        content_hash = _file_content_hash(source_path)
    except OSError:
        return None
    return os.path.join(_SEARCHABLE_CACHE_DIR, f"{doc_id}_{ocr_service.OCR_ENGINE}_{content_hash}.pdf")


def is_searchable_cached(doc_dict: dict, upload_folder: str) -> bool:
    """Return True if the on-disk searchable PDF cache entry already exists for this document."""
    doc_id = doc_dict.get("id")
    unique_upload_filename = doc_dict.get("unique_upload_filename")
    if not unique_upload_filename:
        return False
    original_path = None
    for folder in [upload_folder] + _UPLOAD_FOLDERS:
        candidate = os.path.join(folder, unique_upload_filename)
        if os.path.exists(candidate):
            original_path = candidate
            break
    if not original_path:
        return False
    cache_path = _searchable_cache_path(doc_id, original_path)
    return bool(cache_path and os.path.isfile(cache_path))


def generate_searchable_pdf(doc_dict: dict, upload_folder: str) -> bytes:  # noqa: C901
    """
    Generate a searchable PDF from the original uploaded file.

    Strategy:
    - Image files (JPG/PNG/WEBP): embed as full-page image, overlay invisible OCR text
    - PDF files: re-render each page as image, overlay invisible OCR text on top
      (this preserves the original visual while making text selectable)

    Returns PDF as bytes.
    """
    file_type    = doc_dict.get("file_type", "").lower()
    filename     = doc_dict.get("filename", "")
    raw_text     = doc_dict.get("raw_text", "") or ""
    doc_id       = doc_dict.get("id")

    # ── Locate the original uploaded file ─────────────────────────────────────
    # Flask saves files with uuid names — we search uploads folder for a match
    # by scanning for files that match the doc_id pattern, or fall back to
    # looking at the most recently modified file of the right extension.
    created_at    = doc_dict.get("created_at")
    unique_upload_filename = doc_dict.get("unique_upload_filename")
    if unique_upload_filename:
        # Search all known upload folders for the UUID-named file
        original_path = None
        for folder in [upload_folder] + _UPLOAD_FOLDERS:
            candidate = os.path.join(folder, unique_upload_filename)
            if os.path.exists(candidate):
                original_path = candidate
                break
        if not original_path:
            original_path = _find_uploaded_file(upload_folder, filename, file_type, created_at)
    else:
        original_path = _find_uploaded_file(upload_folder, filename, file_type, created_at)

    is_pdf   = file_type == "pdf" and original_path
    is_image = file_type in ("jpg", "jpeg", "png", "webp", "tif", "tiff") and original_path
    cache_path = _searchable_cache_path(doc_id, original_path) if (is_pdf or is_image) else None
    if cache_path and os.path.isfile(cache_path):
        try:
            with open(cache_path, "rb") as f:
                return f.read()
        except OSError:
            pass  # fall through and regenerate

    # Set by the builders when OCR couldn't place the text on some page (OCR
    # error, or a different engine than configured answered); such output is
    # returned but not cached, so it's regenerated once OCR is healthy again.
    ocr_status = {"degraded": False}
    try:
        if is_pdf:
            pdf_bytes = _searchable_from_pdf(original_path, raw_text, ocr_status)
        elif is_image:
            pdf_bytes = _searchable_from_image(original_path, raw_text, ocr_status)
        else:
            return _text_only_pdf(doc_dict)
    except BaseException as _top_e:
        print(f"[SearchablePDF] top-level failure ({_top_e!r}), falling back to text-only")
        import traceback; traceback.print_exc()
        return _text_only_pdf(doc_dict)

    if cache_path and ocr_status["degraded"]:
        print("[SearchablePDF] OCR degraded for this export, not caching it")
    elif cache_path:
        try:
            os.makedirs(_SEARCHABLE_CACHE_DIR, exist_ok=True)
            with open(cache_path, "wb") as f:
                f.write(pdf_bytes)
        except OSError as e:
            print(f"[SearchablePDF] Failed to write cache entry {cache_path}: {e}")
    return pdf_bytes


def _find_uploaded_file(upload_folder: str, original_filename: str, file_type: str, created_at_iso: str = None) -> str | None:
    """
    Find the uploaded file by searching multiple possible upload folders.
    Uses created_at timestamp to pick the best match for old records that
    don't have unique_upload_filename stored.
    """
    # Build deduplicated list of folders to search
    seen = set()
    search_folders = []
    for folder in [upload_folder] + _UPLOAD_FOLDERS:
        if folder not in seen and os.path.isdir(folder):
            seen.add(folder)
            search_folders.append(folder)

    ext = file_type.lower()
    if ext == "jpeg":
        ext = "jpg"

    candidates = []
    for folder in search_folders:
        for f in os.listdir(folder):
            if f.lower().endswith(f".{ext}") or (ext == "jpg" and f.lower().endswith(".jpeg")):
                full_path = os.path.join(folder, f)
                if os.path.isfile(full_path):
                    candidates.append((os.path.getmtime(full_path), full_path))

    if not candidates:
        return None

    if created_at_iso:
        try:
            # Parse created_at (set after processing finishes, so file mtime < created_at)
            created_at = datetime.fromisoformat(created_at_iso.replace("Z", "+00:00"))
            created_ts = created_at.timestamp()
            # Prefer files modified before created_at, pick the most recent of those
            before = [(mtime, path) for mtime, path in candidates if mtime <= created_ts]
            if before:
                before.sort(reverse=True)
                return before[0][1]
            # All files are after created_at — pick the closest one
            candidates.sort(key=lambda x: abs(x[0] - created_ts))
            return candidates[0][1]
        except Exception:
            pass

    # Final fallback: most recently modified
    candidates.sort(reverse=True)
    return candidates[0][1]


def _ocr_word_rects(img: Image.Image, page_w: float, page_h: float, ocr_status: dict):
    """OCR word boxes scaled from image pixels to page points, as the
    (x0, y0, x1, y1, text) tuples _insert_words_at_positions() takes.
    Returns None (and marks ocr_status degraded) if OCR failed."""
    try:
        words = ocr_service.ocr_words(img)
    except Exception as e:
        print(f"[SearchablePDF] OCR failed ({e}), placing raw text evenly instead")
        ocr_status["degraded"] = True
        return None
    if ocr_service.active_engine() != ocr_service.OCR_ENGINE:
        # e.g. paddle failed to load and Tesseract answered - usable output,
        # but not what the cache key claims.
        ocr_status["degraded"] = True
    sx, sy = page_w / img.width, page_h / img.height
    return [(wd["left"] * sx, wd["top"] * sy,
             (wd["left"] + wd["width"]) * sx, (wd["top"] + wd["height"]) * sy,
             wd["text"]) for wd in words]


def _searchable_from_image(image_path: str, raw_text: str, ocr_status: dict) -> bytes:
    """
    Create a searchable PDF from an image: the image as the page, plus an
    invisible text layer at the OCR word positions so Ctrl+F highlights land
    on the right words in any PDF viewer including PDF.js.
    """
    return _searchable_from_image_fallback(image_path, raw_text, ocr_status)


def _searchable_from_image_fallback(image_path: str, raw_text: str, ocr_status: dict) -> bytes:
    """Embed the image as a full page + OCR word layer; if OCR finds no words,
    fall back to spreading raw_text evenly over the page."""
    src_doc = fitz.open(image_path)
    w, h = src_doc[0].rect.width, src_doc[0].rect.height
    src_doc.close()
    pdf = fitz.open()
    page = pdf.new_page(width=w, height=h)
    page.insert_image(fitz.Rect(0, 0, w, h), filename=image_path, keep_proportion=False)
    with Image.open(image_path) as img:
        img.load()
        words = _ocr_word_rects(img, w, h, ocr_status)
    if words:
        _insert_words_at_positions(page, words, fit_width=True)
    else:
        _insert_invisible_text(page, raw_text, w, h)
    buf = io.BytesIO()
    pdf.save(buf, garbage=4, deflate=True)
    pdf.close()
    return buf.getvalue()


def _searchable_from_pdf(pdf_path: str, raw_text: str, ocr_status: dict) -> bytes:
    """
    Create a searchable PDF from an original PDF, page by page:
    - Digital pages (already have text): their own word positions are reused.
    - Scanned/image-only pages: OCR text layer added at the word positions.
    """
    return _searchable_from_pdf_fallback(pdf_path, raw_text, ocr_status)


def _searchable_from_pdf_fallback(pdf_path: str, raw_text: str, ocr_status: dict) -> bytes:
    """Re-render each page + text layer: the page's own words if it has a text
    layer, else OCR words, else raw_text spread evenly over the page."""
    src = fitz.open(pdf_path)
    try:
        out = fitz.open()
        try:
            raw_pages = [p.strip() for p in raw_text.split("\f") if p.strip()]
            for page_num in range(len(src)):
                src_page = src[page_num]
                w, h = src_page.rect.width, src_page.rect.height
                mat = fitz.Matrix(2, 2)
                pix = src_page.get_pixmap(matrix=mat, alpha=False)
                new_page = out.new_page(width=w, height=h)
                new_page.insert_image(fitz.Rect(0, 0, w, h), stream=pix.tobytes("png"))
                words = src_page.get_text("words")
                from_ocr = not words
                if from_ocr:
                    mode = "RGB" if pix.n == 3 else "L"
                    img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
                    words = _ocr_word_rects(img, w, h, ocr_status)
                if words:
                    _insert_words_at_positions(new_page, words, fit_width=from_ocr)
                else:
                    page_text = raw_pages[page_num] if page_num < len(raw_pages) else raw_text
                    _insert_invisible_text(new_page, page_text, w, h)
            buf = io.BytesIO()
            out.save(buf, garbage=4, deflate=True)
            return buf.getvalue()
        finally:
            out.close()
    finally:
        src.close()


def _insert_words_at_positions(page: fitz.Page, words: list, fit_width: bool = False):
    """
    Burn each word at its exact bbox position as an invisible text layer.
    words: list of (x0, y0, x1, y1, text, ...) - PyMuPDF get_text("words")
    tuples, or the (x0, y0, x1, y1, text) tuples from _ocr_word_rects().
    Baseline = bottom of bbox (y1), font size scaled to bbox height.
    fit_width: stretch/squeeze each word horizontally to exactly span its
    bbox. Used for OCR words - Helvetica's natural width rarely matches the
    scanned font, so without it Ctrl+F highlights cover only part of a word,
    or run into the next one (which also merges adjacent words).
    """
    for word in words:
        x0, y0, x1, y1, text = word[0], word[1], word[2], word[3], word[4]
        if not text.strip():
            continue
        fontsize = max((y1 - y0) * 0.85, 4)
        morph = None
        if fit_width:
            natural_w = fitz.get_text_length(text, fontname="helv", fontsize=fontsize)
            if natural_w > 0:
                morph = (fitz.Point(x0, y1), fitz.Matrix((x1 - x0) / natural_w, 1))
        try:
            page.insert_text(
                fitz.Point(x0, y1),   # baseline at bottom of word bbox
                text,
                fontname="helv",
                fontsize=fontsize,
                color=(1, 1, 1),
                render_mode=3,        # invisible text (OCR layer standard)
                morph=morph,
            )
        except Exception:
            pass


def _insert_invisible_text(page: fitz.Page, raw_text: str, page_w: float, page_h: float):
    """
    Insert OCR text as an invisible layer over the page.
    Text is rendered in white (invisible) at font size 0 effectively —
    but remains selectable and copyable in PDF readers.

    Two modes:
    - If text is short: single block centered
    - If text has lines: distribute lines vertically across the page
    """
    if not raw_text.strip():
        return

    lines = [l for l in raw_text.splitlines() if l.strip()]
    if not lines:
        return

    # Calculate font size and line spacing to fill the page
    margin_x = page_w * 0.05
    margin_y = page_h * 0.05
    usable_h = page_h - (margin_y * 2)
    usable_w = page_w - (margin_x * 2)

    # Font size: distribute lines evenly across page height
    fontsize = min(
        max(usable_h / max(len(lines), 1) * 0.75, 6),  # min 6pt
        14  # max 14pt to avoid overflow
    )

    # Use a standard font that maps well for text selection
    fontname = "helv"  # Helvetica built-in

    y = margin_y + fontsize

    for line in lines:
        if y > page_h - margin_y:
            break  # Don't overflow page

        try:
            page.insert_text(
                fitz.Point(margin_x, y),
                line,
                fontname=fontname,
                fontsize=fontsize,
                color=(1, 1, 1),   # white = visually invisible
                render_mode=3,      # 3 = invisible text (for OCR layer)
            )
        except Exception:
            pass  # Skip problematic characters

        y += fontsize * 1.4  # line height


def _text_only_pdf(doc_dict: dict) -> bytes:
    """
    Fallback: plain text PDF if original image not found.
    Still selectable and copyable.
    """
    pdf = fitz.open()
    page = pdf.new_page()  # A4

    raw_text = doc_dict.get("raw_text", "") or "(no text available)"
    filename = doc_dict.get("filename", "Unknown")
    doc_type = doc_dict.get("document_type", "other")
    confidence = doc_dict.get("confidence", "")

    # Header
    page.insert_text(fitz.Point(50, 50), "VaultMind — Searchable Document", fontsize=16, color=(0, 0.2, 0.6))
    page.insert_text(fitz.Point(50, 75), f"File: {filename}  |  Type: {doc_type}  |  Confidence: {confidence}", fontsize=9, color=(0.35, 0.47, 0.6))

    # Divider line
    page.draw_line(fitz.Point(50, 90), fitz.Point(545, 90), color=(0.7, 0.85, 1.0), width=0.5)

    # OCR text
    y = 110
    for line in raw_text.splitlines():
        if y > 800:
            page = pdf.new_page()
            y = 50
        page.insert_text(fitz.Point(50, y), line, fontsize=9, color=(0.04, 0.1, 0.2))
        y += 14

    buf = io.BytesIO()
    pdf.save(buf)
    pdf.close()
    return buf.getvalue()