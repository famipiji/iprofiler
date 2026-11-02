"""Tests for field highlighting on image documents:

- static/image_highlight.js (run under node; those tests skip without node):
  matching OCR text blocks (exact, date variants, values spanning lines) and
  scaling to the displayed image size.
- qwen_service.open_upright: OCR and Vision get a phone photo upright (EXIF
  orientation applied, as Pillow's ImageOps.exif_transpose - the rotation
  browsers apply for `image-orientation: from-image`), so the boxes line up
  with the image the viewer shows.

Run: pytest test_image_highlight.py -v
"""
import io
import json
import os
import shutil
import subprocess

import pytest
from PIL import Image, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
IMG_JS = os.path.join(HERE, "static", "image_highlight.js")
DATE_JS = os.path.join(HERE, "static", "date_highlight.js")


def _page_matcher_source():
    html = open(os.path.join(HERE, "templates", "index.html"), encoding="utf-8").read()
    start = html.index("function _findValueMatches(")
    return html[start:html.index("\n  function highlightInPdf(", start)]


def _js(expr, **args):
    """Evaluate `expr` with IH (image_highlight), DateHighlight and the page's
    _findValueMatches in scope; `args` become JS consts."""
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    consts = "".join(f"const {k} = {json.dumps(v)};\n" for k, v in args.items())
    script = (f"const IH = require({json.dumps(IMG_JS)});\n"
              f"global.window = {{ DateHighlight: require({json.dumps(DATE_JS)}) }};\n"
              f"const DateHighlight = window.DateHighlight;\n{_page_matcher_source()}\n{consts}"
              f"process.stdout.write(JSON.stringify({expr}));\n")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _block(text, x1, y1, x2, y2, page=1):
    return {"page": page, "text": text, "bbox": {"x1": x1, "y1": y1, "x2": x2, "y2": y2}}


BLOCKS = [
    _block("SIMEN UTARA SDN. BHD.", 0.10, 0.05, 0.60, 0.08),
    _block("Invoice No: BK1141557", 0.10, 0.10, 0.50, 0.13),
    _block("Date: 9/23/2026", 0.55, 0.10, 0.85, 0.13),
    _block("No. 25, Jalan Damai 3,", 0.10, 0.20, 0.45, 0.23),
    _block("Taman Damai Perdana", 0.10, 0.24, 0.40, 0.27),
    _block("TOTAL 11,494.50", 0.60, 0.80, 0.90, 0.83),
]


def _find(value, blocks=BLOCKS):
    return _js("IH.findBlockMatches(blocks, value, _findValueMatches)", blocks=blocks, value=value)


# -- findBlockMatches ---------------------------------------------------------------

def test_exact_match_narrowed_to_matched_characters():
    rects = _find("bk1141557")                       # case-insensitive
    assert len(rects) == 1
    r = rects[0]
    text = "Invoice No: BK1141557"
    w = 0.50 - 0.10
    assert r["x1"] == pytest.approx(0.10 + w * text.index("BK") / len(text))
    assert r["x2"] == pytest.approx(0.50)              # match runs to the end of the line
    assert (r["y1"], r["y2"]) == (0.10, 0.13)


def test_normalized_date_found_via_variants():
    rects = _find("2026-09-23")
    assert len(rects) == 1 and rects[0]["x1"] > 0.55 and rects[0]["x2"] == pytest.approx(0.85)


def test_value_spanning_two_ocr_lines_marks_both():
    rects = _find("Jalan Damai 3, Taman Damai Perdana")
    assert [(r["y1"], r["y2"]) for r in rects] == [(0.20, 0.23), (0.24, 0.27)]
    assert rects[0]["x1"] == 0.10 and rects[1]["x2"] == 0.40   # whole lines


def test_single_line_match_preferred_over_joins():
    # "Damai" occurs in two lines - each line's own match, no joined marks.
    assert len(_find("Damai")) == 2


def test_no_match_and_no_blocks():
    assert _find("Siti binti Abu") == []
    assert _find("BK1141557", blocks=[]) == []


def test_joins_stay_on_one_page():
    blocks = [_block("Jalan Damai 3,", 0.1, 0.9, 0.4, 0.93, page=1),
              _block("Taman Damai Perdana", 0.1, 0.05, 0.4, 0.08, page=2)]
    assert _find("Jalan Damai 3, Taman Damai Perdana", blocks=blocks) == []


# -- toPixels -------------------------------------------------------------------------

def test_to_pixels_scales_to_displayed_size():
    px = _js("IH.toPixels({x1: 0.1, y1: 0.2, x2: 0.5, y2: 0.25}, 800, 600)")
    assert px == pytest.approx({"left": 80, "top": 120, "width": 320, "height": 30})


# -- EXIF: the server OCRs photos upright (qwen_service.open_upright) ------------------
# Phones store photos sideways with an EXIF rotation tag; the browser shows
# them upright (image-orientation: from-image), so the OCR boxes must be found
# on the upright image too. (OCR on the sideways image of a real test photo
# found no text at all.) These run without node.

def _stored_photo(tmp_path, orientation):
    """A 200x100 stored JPEG with a red box at x 0.1-0.3, y 0.1-0.3 and the
    given EXIF orientation; returns (path, where the box is when displayed)."""
    stored = Image.new("RGB", (200, 100), "white")
    stored.paste((255, 0, 0), (20, 10, 60, 30))
    exif = Image.Exif()
    exif[0x0112] = orientation
    path = tmp_path / f"photo_{orientation}.jpg"
    stored.save(path, "JPEG", exif=exif.tobytes(), quality=100)
    with Image.open(path) as im:
        displayed = ImageOps.exif_transpose(im)   # what the browser shows
        return str(path), displayed.size, _marker_bbox(displayed)


def _marker_bbox(img):
    """Normalized bbox of the red pixels in `img`."""
    img = img.convert("RGB")
    px = img.load()
    xs, ys = [], []
    for y in range(img.height):
        for x in range(img.width):
            r, g, b = px[x, y]
            if r > 180 and g < 90 and b < 90:
                xs.append(x)
                ys.append(y)
    return (min(xs) / img.width, min(ys) / img.height,
            (max(xs) + 1) / img.width, (max(ys) + 1) / img.height)


@pytest.fixture
def qs_mod():
    os.environ.setdefault("OLLAMA_BASE_URL", "http://127.0.0.1:1/v1")
    os.environ.setdefault("LLM_API_KEY", "test")
    import qwen_service
    return qwen_service


@pytest.mark.parametrize("orientation", range(1, 9))
def test_ocr_gets_the_upright_image(qs_mod, monkeypatch, tmp_path, orientation):
    import ocr_service
    path, shown_size, shown_marker = _stored_photo(tmp_path, orientation)
    seen = []
    monkeypatch.setattr(ocr_service, "ocr_lines", lambda img: seen.append(img.copy()) or [])
    monkeypatch.setattr(qs_mod, "build_image_blocks", lambda b64, mt: [])   # no Vision fallback

    qs_mod.compute_text_blocks_lazy(path)              # highlight blocks (lazy path)
    qs_mod._ocr_text_for_image(path)                   # OCR_TEXT_SOURCE=paddle upload path
    assert len(seen) == 2
    for img in seen:
        assert img.size == shown_size
        assert _marker_bbox(img) == pytest.approx(shown_marker, abs=0.02)


@pytest.mark.parametrize("orientation", [1, 3, 6, 8])
def test_vision_gets_the_upright_image(qs_mod, tmp_path, orientation):
    import base64
    path, shown_size, shown_marker = _stored_photo(tmp_path, orientation)
    b64, _, w, h = qs_mod.encode_image(path, max_size=1024)
    assert (w, h) == shown_size
    sent = Image.open(io.BytesIO(base64.b64decode(b64)))
    assert _marker_bbox(sent) == pytest.approx(shown_marker, abs=0.02)
