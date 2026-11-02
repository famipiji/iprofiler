"""Tests for local-run fixes Task 4: opendataloader-pdf (ODL) blocks and the
Java check.

- parse_odl_blocks() reads ODL's real JSON ("kids" tree with "content",
  "page number", "bounding box" [l, b, r, t] in PDF points, bottom-left
  origin) - it used to expect Docling's format and returned 0 blocks.
- Without Java on PATH, ODL is skipped (no per-PDF 'java' errors), digital
  PDFs go straight to the pdfplumber fallback, and app.check_java() prints one
  [Startup] warning.

`app` is imported against a throwaway SQLite file. The last test runs the real
ODL on a generated PDF and is skipped when Java isn't available.

Run: pytest test_odl_java.py -v
"""
import json
import os
import shutil
import sys
import tempfile
import types

# Always a throwaway DB - never the real one from the environment. (If another
# test module imported `app` first, its own SQLite override is the one in use.)
_DB_FILE = os.path.join(tempfile.mkdtemp(), "odl_java_test.db")
os.environ["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + _DB_FILE.replace("\\", "/")

import pytest

import qwen_service as qs

# Trimmed from a real ODL run on a 2-page clinic record (2026-11-02).
ODL_JSON = {
    "file name": "record.pdf",
    "number of pages": 2,
    "kids": [
        {"type": "heading", "id": 1, "page number": 1,
         "bounding box": [179.59, 768.722, 415.628, 784.705], "content": "KLINIK SEJAHTERA MEDIKA"},
        {"type": "table", "id": 7, "page number": 1, "bounding box": [54.024, 406.92, 535.9, 627.29],
         "rows": [
             {"type": "table row", "row number": 1, "cells": [
                 {"type": "table cell", "page number": 1, "bounding box": [54.504, 605.45, 204.312, 626.81],
                  "kids": [{"type": "paragraph", "page number": 1,
                            "bounding box": [61.224, 610.773, 109.275, 620.228], "content": "Full Name"}]},
                 {"type": "table cell", "page number": 1, "bounding box": [204.31, 605.45, 535.4, 626.81],
                  "kids": [{"type": "paragraph", "page number": 1,
                            "bounding box": [211.0, 610.773, 300.5, 620.228], "content": "Ahmad bin Ali"}]},
             ]},
         ]},
        {"type": "caption", "page number": 1, "bounding box": [60.213, 378.662, 188.394, 389.017],
         "content": "RECORD INFORMATION"},
        {"type": "paragraph", "page number": 2, "bounding box": [72.0, 700.0, 300.0, 712.0], "content": "Page two text"},
        {"type": "image", "page number": 2, "bounding box": [72.0, 400.0, 300.0, 600.0]},          # no text
        {"type": "paragraph", "page number": 2, "bounding box": [72.0, 380.0, 300.0, 392.0], "content": "   "},
        {"type": "paragraph", "page number": 2, "bounding box": "garbage", "content": "bad box"},
    ],
}


def _write_json(tmp_path, data):
    p = tmp_path / "odl.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


# -- parse_odl_blocks -------------------------------------------------------------

def test_parses_real_odl_structure(tmp_path):
    blocks = qs.parse_odl_blocks(_write_json(tmp_path, ODL_JSON))
    assert [(b["page"], b["text"]) for b in blocks] == [
        (1, "KLINIK SEJAHTERA MEDIKA"),
        (1, "Full Name"),                 # table cell paragraphs, each with its own box
        (1, "Ahmad bin Ali"),
        (1, "RECORD INFORMATION"),
        (2, "Page two text"),
    ]
    assert blocks[1]["bbox"] == [61.224, 610.773, 109.275, 620.228]


def test_bbox_convention_matches_normalizer(tmp_path):
    # [l, b, r, t] bottom-left origin -> 0..1 top-left origin, as the pipeline does.
    blocks = qs.parse_odl_blocks(_write_json(tmp_path, ODL_JSON))
    rel = qs.normalize_bbox_to_relative(blocks[0]["bbox"], 595.2, 841.92)
    assert rel["x1"] == pytest.approx(179.59 / 595.2)
    assert rel["y1"] == pytest.approx(1 - 784.705 / 841.92)      # top edge, measured from the top
    assert rel["y2"] == pytest.approx(1 - 768.722 / 841.92)
    assert 0 <= rel["y1"] < rel["y2"] <= 1


def test_empty_or_docling_shaped_json_gives_no_blocks(tmp_path):
    assert qs.parse_odl_blocks(_write_json(tmp_path, {"kids": []})) == []
    assert qs.parse_odl_blocks(_write_json(tmp_path, {"texts": [{"text": "x", "prov": []}]})) == []


# -- no Java ----------------------------------------------------------------------

@pytest.fixture
def no_java(monkeypatch):
    qs.java_available.cache_clear()
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None if name == "java" else "/bin/" + name)
    # Any attempt to run ODL fails the test.
    monkeypatch.setitem(sys.modules, "opendataloader_pdf", types.SimpleNamespace(
        convert=lambda **k: pytest.fail("ODL must not run without Java")))
    yield
    qs.java_available.cache_clear()


def test_odl_skipped_without_java(no_java):
    assert qs.java_available() is False
    assert qs.extract_pdf_text_with_odl("digital.pdf") == ("", [])


class _Stop(Exception):
    pass


def test_digital_pdf_goes_straight_to_pdfplumber_without_java(no_java, monkeypatch, capsys):
    text = "\n".join(f"Line {i}: value {i}" for i in range(40))
    monkeypatch.setattr(qs, "is_scanned_pdf", lambda p: False)
    monkeypatch.setattr(qs, "extract_pdf_text_with_tables", lambda p: text)

    def _classify(raw_text, hint_type=None):
        raise _Stop(raw_text)

    monkeypatch.setattr(qs, "classify_document", _classify)
    with pytest.raises(_Stop) as exc:
        qs.process_document("digital.pdf")
    assert exc.value.args[0] == text
    out = capsys.readouterr().out
    assert "falling back to pdfplumber" in out and "(no Java)" in out
    assert "java" not in out.replace("(no Java)", "").lower()


def test_startup_warns_once_without_java(no_java, capsys):
    import app as app_module
    capsys.readouterr()
    assert app_module.check_java() is False
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith("[Startup]")]
    assert len(lines) == 1 and "Java not found" in lines[0]


def test_startup_silent_with_java(monkeypatch, capsys):
    import app as app_module
    qs.java_available.cache_clear()
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/usr/bin/" + name)
    try:
        capsys.readouterr()
        assert app_module.check_java() is True
        assert "[Startup]" not in capsys.readouterr().out
    finally:
        qs.java_available.cache_clear()


# -- real ODL run (needs Java + opendataloader-pdf) -------------------------------

@pytest.mark.skipif(shutil.which("java") is None, reason="Java not on PATH")
def test_real_odl_output_gives_blocks(tmp_path):
    pytest.importorskip("opendataloader_pdf")
    import fitz
    pdf = tmp_path / "digital.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "INVOICE NUMBER BK1141557", fontsize=14)
    page.insert_text((72, 140), "Total amount payable 11,494.50", fontsize=11)
    doc.save(str(pdf))
    doc.close()

    qs.java_available.cache_clear()
    text, blocks = qs.extract_pdf_text_with_odl(str(pdf))
    assert "BK1141557" in text
    assert blocks, "parse_odl_blocks found no blocks in real ODL output"
    hit = next(b for b in blocks if "BK1141557" in b["text"])
    rel = qs.normalize_bbox_to_relative(hit["bbox"], 595, 842)
    # Text baseline at y=100 from the top -> box near the top of the page.
    assert 0.08 < rel["y1"] < rel["y2"] < 0.14
    assert rel["x1"] == pytest.approx(72 / 595, abs=0.02)
