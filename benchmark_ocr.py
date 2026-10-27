"""
Benchmarks the OCR engines behind ocr_service.py (PaddleOCR, and Tesseract
when its binary is installed) on real files from uploads/.

Per file and engine it records warm OCR time, line count and mean line
confidence. Digital PDFs double as ground truth: page 1 is rendered to an
image and OCR'd, and the recognized words are scored against the page's own
text layer (word precision / recall / F1, case-insensitive, surrounding
punctuation stripped). Word matching is order-insensitive on purpose - OCR
reading order and a PDF's internal text order often differ, which an
order-sensitive character error rate would count as recognition errors.

Usage:
    python benchmark_ocr.py                    # distinct files from uploads/ (see --limit)
    python benchmark_ocr.py a.jpg b.pdf ...    # specific files
    python benchmark_ocr.py --limit 30

Byte-identical uploads are benchmarked once. PDFs use page 1 only, rendered
at 200 DPI (the resolution compute_text_blocks_lazy() uses). The paddle model
size comes from PADDLE_OCR_MODEL (mobile|server), same as the app. Results go
to ocr_benchmark_results.json.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ocr_service  # noqa: E402  (imports torch before paddle - see _get_paddle)

import fitz  # noqa: E402
from PIL import Image  # noqa: E402

UPLOADS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
OUT_FILE = "ocr_benchmark_results.json"
DPI = 200
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff")
MAX_DIGITAL = 6          # digital PDFs included by default (they're the ground-truth set)


def _kind(path):
    if path.lower().endswith(IMAGE_EXTS):
        return "image"
    with fitz.open(path) as d:
        n = len(d[0].get_text("words"))
    return "pdf-digital" if n > 20 else "pdf-scanned" if n == 0 else "pdf-sparse"


def pick_files(limit):
    seen, picked = set(), {"image": [], "pdf-scanned": [], "pdf-digital": []}
    for path in sorted(glob.glob(os.path.join(UPLOADS, "*"))):
        if not path.lower().endswith(IMAGE_EXTS + (".pdf",)):
            continue
        digest = hashlib.md5(open(path, "rb").read()).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        try:
            kind = _kind(path)
        except Exception:
            continue
        if kind in picked:
            picked[kind].append(path)
    files = picked["image"] + picked["pdf-scanned"] + picked["pdf-digital"][:MAX_DIGITAL]
    return files[:limit]


def load_page(path):
    """-> (PIL image, ground-truth text or None)"""
    if path.lower().endswith(IMAGE_EXTS):
        img = Image.open(path)
        img.load()
        return img, None
    with fitz.open(path) as d:
        page = d[0]
        pix = page.get_pixmap(dpi=DPI)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        truth = page.get_text() if len(page.get_text("words")) > 20 else None
    return img, truth


_EDGE_PUNCT = re.compile(r"^[^\w]+|[^\w]+$")


def _norm_words(text):
    words = (_EDGE_PUNCT.sub("", w).lower() for w in text.split())
    return Counter(w for w in words if w)


def word_scores(ocr_text, truth):
    got, want = _norm_words(ocr_text), _norm_words(truth)
    hit = sum((got & want).values())
    p = hit / max(1, sum(got.values()))
    r = hit / max(1, sum(want.values()))
    return {"precision": round(p, 3), "recall": round(r, 3),
            "f1": round(2 * p * r / (p + r), 3) if p + r else 0.0}


def available_engines():
    engines = []
    ocr_service.OCR_ENGINE = "paddle"
    if ocr_service.active_engine() == "paddle":
        engines.append("paddle")
    try:
        ocr_service.pytesseract.get_tesseract_version()
        engines.append("tesseract")
    except Exception as e:
        print(f"[bench] Tesseract not available, skipping it: {e}")
    return engines


def run_engine(engine, img):
    ocr_service.OCR_ENGINE = engine
    t = time.time()
    lines = ocr_service.ocr_lines(img)
    dt = time.time() - t
    confs = [ln["conf"] for ln in lines]
    return {
        "time_s": round(dt, 2),
        "lines": len(lines),
        "mean_conf": round(sum(confs) / len(confs), 3) if confs else None,
        "text": "\n".join(ln["text"] for ln in lines),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    files = args.files or pick_files(args.limit)
    engines = available_engines()
    if not engines:
        sys.exit("No OCR engine available.")
    print(f"[bench] {len(files)} files, engines: {engines}, paddle model: {ocr_service.PADDLE_OCR_MODEL}")

    # Warm-up so the first file's time doesn't include model loading
    warm_img, _ = load_page(files[0])
    for engine in engines:
        try:
            run_engine(engine, warm_img)
        except ocr_service.OCRError:
            pass

    results, skipped = [], []
    for path in files:
        try:
            img, truth = load_page(path)
        except Exception as e:
            print(f"{os.path.basename(path)[:14]:14s} SKIPPED - unreadable file: {e}")
            skipped.append({"file": os.path.basename(path), "error": str(e)})
            continue
        row = {"file": os.path.basename(path), "kind": _kind(path), "size": list(img.size), "engines": {}}
        for engine in engines:
            try:
                r = run_engine(engine, img)
            except ocr_service.OCRError as e:
                row["engines"][engine] = {"error": str(e)}
                continue
            if truth:
                r["vs_text_layer"] = word_scores(r["text"], truth)
            r["text_preview"] = r.pop("text")[:200]
            row["engines"][engine] = r
        results.append(row)
        cells = []
        for engine in engines:
            r = row["engines"][engine]
            if "error" in r:
                cells.append(f"{engine}: ERROR")
                continue
            f1 = f" F1 {r['vs_text_layer']['f1']:.2f}" if "vs_text_layer" in r else ""
            cells.append(f"{engine}: {r['time_s']:5.1f}s {r['lines']:3d} lines conf {r['mean_conf'] or 0:.2f}{f1}")
        print(f"{row['file'][:14]:14s} {row['kind']:11s} {img.width}x{img.height:<5d} | " + " | ".join(cells))

    summary = {}
    for engine in engines:
        rows = [r["engines"][engine] for r in results if "error" not in r["engines"][engine]]
        scored = [r["vs_text_layer"] for r in rows if "vs_text_layer" in r]
        summary[engine] = {
            "files_ok": len(rows),
            "files_failed": len(results) - len(rows),
            "total_time_s": round(sum(r["time_s"] for r in rows), 1),
            "mean_time_s": round(sum(r["time_s"] for r in rows) / max(1, len(rows)), 2),
            "mean_conf": round(sum(r["mean_conf"] or 0 for r in rows) / max(1, len(rows)), 3),
            "ground_truth_files": len(scored),
            "mean_word_precision": round(sum(s["precision"] for s in scored) / max(1, len(scored)), 3),
            "mean_word_recall": round(sum(s["recall"] for s in scored) / max(1, len(scored)), 3),
            "mean_word_f1": round(sum(s["f1"] for s in scored) / max(1, len(scored)), 3),
        }
    out = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "dpi": DPI,
           "paddle_model": ocr_service.PADDLE_OCR_MODEL, "paddle_max_side": ocr_service.PADDLE_OCR_MAX_SIDE,
           "engines": engines, "summary": summary, "files": results, "skipped": skipped}
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print("\n[bench] summary:", json.dumps(summary, indent=2))
    print(f"[bench] wrote {OUT_FILE}")


if __name__ == "__main__":
    main()
