"""
Benchmarks the vision model's ability to "read" barcodes/QR codes against
real decoded values from zxing-cpp (already a project dependency, used
elsewhere for barcode-separator detection in detect_separator_groups()).

Ground truth comes from zxing-cpp's actual decode of each code found in the
page. The vision model is then shown a crop of each individual code and
asked to transcribe it. The two are compared to see how reliable VLM
"reading" of barcodes/QR codes really is, before deciding whether to wire
this into the extraction pipeline (spoiler: zxing-cpp should very likely
be the actual extraction path for these fields — a VLM cannot algorithmically
decode a barcode/QR bit pattern, it can only OCR human-readable text printed
near one).

Usage:
    python test_barcode_benchmark.py [path/to/file.pdf|.jpg|.png]

Defaults to the sample file supplied for the "Out Patient Slip" doc type if
no path is given. Requires the LM Studio/Ollama vision backend reachable at
OLLAMA_BASE_URL (see qwen_service.py) for the AI-read half of the benchmark;
the zxing-cpp ground-truth half works with no backend at all.
"""
import os
import sys
import json
import base64
import io

import fitz
import zxingcpp
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qwen_service import call_qwen_vision, MODEL

DEFAULT_FILE = r"C:\Users\batri\Downloads\testing files\Demo\Extracted areas_260907.pdf"
DPI = 300
PAD_PX = 40  # extra margin around each detected code before cropping, so the
             # VLM sees a bit of surrounding context (e.g. printed digits below a barcode)

READ_PROMPT = (
    "This image contains exactly one barcode or QR code, possibly with printed "
    "text near it. Decode/read the exact value it represents.\n"
    "Reply with ONLY the decoded text/number, nothing else. "
    "If you genuinely cannot determine it, reply exactly: UNREADABLE"
)


def load_page_image(path: str) -> Image.Image:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        doc = fitz.open(path)
        try:
            page = doc[0]
            mat = fitz.Matrix(DPI / 72, DPI / 72)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        finally:
            doc.close()
    return Image.open(path).convert("RGB")


def crop_to_b64(img: Image.Image, box: tuple) -> str:
    x0, y0, x1, y1 = box
    w, h = img.size
    x0 = max(0, x0 - PAD_PX)
    y0 = max(0, y0 - PAD_PX)
    x1 = min(w, x1 + PAD_PX)
    y1 = min(h, y1 + PAD_PX)
    crop = img.crop((x0, y0, x1, y1))
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def normalize(s: str) -> str:
    return "".join(s.split()).upper() if s else ""


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_FILE
    if not os.path.exists(path):
        print(f"[Benchmark] File not found: {path}")
        sys.exit(1)

    print(f"[Benchmark] Loading {path}")
    img = load_page_image(path)
    print(f"[Benchmark] Page image: {img.size[0]}x{img.size[1]}")

    detections = zxingcpp.read_barcodes(img)
    if not detections:
        print("[Benchmark] zxing-cpp found no barcodes/QR codes on this page.")
        sys.exit(1)

    # Reading order: top-to-bottom, then left-to-right
    def sort_key(r):
        p = r.position
        cx = (p.top_left.x + p.top_right.x + p.bottom_left.x + p.bottom_right.x) / 4
        cy = (p.top_left.y + p.top_right.y + p.bottom_left.y + p.bottom_right.y) / 4
        return (round(cy, -2), cx)  # bucket rows to ~100px bands so same-row codes sort by x

    detections = sorted(detections, key=sort_key)

    print(f"[Benchmark] zxing-cpp ground truth: {len(detections)} code(s) found\n")

    results = []
    for i, r in enumerate(detections, start=1):
        p = r.position
        xs = [p.top_left.x, p.top_right.x, p.bottom_left.x, p.bottom_right.x]
        ys = [p.top_left.y, p.top_right.y, p.bottom_left.y, p.bottom_right.y]
        box = (min(xs), min(ys), max(xs), max(ys))
        truth = r.text

        print(f"[{i}] {r.format} at {box}")
        print(f"    zxing (ground truth): {truth!r}")

        try:
            b64 = crop_to_b64(img, box)
            ai_answer = call_qwen_vision(b64, "image/png", READ_PROMPT, max_tokens=60).strip()
        except Exception as e:
            ai_answer = f"<error: {e}>"

        match = normalize(ai_answer) == normalize(truth)
        print(f"    AI ({MODEL}) reads:     {ai_answer!r}")
        print(f"    Match: {'YES' if match else 'no'}\n")

        results.append({
            "index": i,
            "format": str(r.format),
            "box": box,
            "zxing_ground_truth": truth,
            "ai_reads": ai_answer,
            "match": match,
        })

    n_match = sum(1 for r in results if r["match"])
    print(f"[Benchmark] Summary: AI matched zxing ground truth on {n_match}/{len(results)} codes")

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "barcode_benchmark_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"[Benchmark] Full results written to {out_path}")


if __name__ == "__main__":
    main()
