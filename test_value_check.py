"""Tests for value_check.py: extracted scalar values that don't occur in the
document's raw_text are flagged "not in document" in field_sources (values
untouched), with dates matched via the same printed variants as the UI's
static/date_highlight.js.

Run: pytest test_value_check.py -v
"""
import json
import os
import shutil
import subprocess

import pytest
from PIL import Image

import ocr_service
import qwen_service as qs
import value_check as vc

RAW = """SIMEN UTARA SDN. BHD.
Invoice No: BK1141557      Invoice Date: 9/23/2026
Bill To:   AHMAD   BIN ALI
Item | Qty | Amount
Cement 50kg | 10 | 11,494.50
TOTAL 11,494.50
Tarikh Lahir: 5 Ogos 1990"""


# -- value_in_text ------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "BK1141557",
    "bk1141557",                 # case-insensitive
    "Ahmad bin Ali",             # extra spaces in the document
    "AHMADBINALI",               # whitespace ignored entirely
    "11,494.50",
    11,                          # number -> "11" occurs ("11,494.50")
    "2026-09-23",                # printed 9/23/2026
    "23 Sep 2026",
    "1990-08-05",                # printed in Malay: 5 Ogos 1990
])
def test_found(value):
    assert vc.value_in_text(value, RAW)


@pytest.mark.parametrize("value", [
    "BK1141558",
    "Siti binti Abu",
    "2026-09-24",                # wrong day
    "2026-08-23",                # right day, wrong month
    "12,000.00",
])
def test_not_found(value):
    assert not vc.value_in_text(value, RAW)


# -- flag_values_not_in_document ------------------------------------------------------

def test_flags_only_missing_scalars_and_keeps_values():
    fields = {
        "invoice_no": "BK1141557",
        "invoice_date": "2026-09-23",
        "customer": "Siti binti Abu",          # hallucinated
        "total": "12,000.00",                  # misread
        "items": [{"desc": "Not in doc"}],      # table - skipped
        "tags": ["nope"],                      # array - skipped
        "notes": None,
        "paid": True,                          # checkbox - skipped
        "po_number": "N/A",                    # placeholder - skipped
        "qr_code": "QR-ONLY-IN-IMAGE",         # from a barcode decode - skipped
    }
    before = json.loads(json.dumps(fields))
    sources = {"invoice_no": "model", "customer": "model", "qr_code": "barcode scan"}
    flagged = vc.flag_values_not_in_document(fields, RAW, sources)
    assert sorted(flagged) == ["customer", "total"]
    assert sources == {"invoice_no": "model", "customer": "not in document",
                       "total": "not in document", "qr_code": "barcode scan"}
    assert fields == before


def test_no_raw_text_means_no_flags():
    sources = {}
    assert vc.flag_values_not_in_document({"a": "x"}, "", sources) == []
    assert vc.flag_values_not_in_document({"a": "x"}, "   ", sources) == []
    assert sources == {}


# -- same date rules as the UI (static/date_highlight.js) ------------------------------

JS_MODULE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "date_highlight.js")
PARITY_VALUES = ["2026-09-23", "2026/9/3", "2026-09-23T10:15:00", "23/09/2026", "9/23/2026",
                 "05/06/2026", "23-09-26", "23.09.2026", "23 Sep 2026", "23rd September, 2026",
                 "5 Ogos 1990", "1 Disember 2025", "Sep 23, 2026", "Sept. 23 2026", "2024-02-29",
                 "31/02/2026", "BK1141557", "11,494.50", ""]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_python_port_matches_js_variants_and_patterns():
    script = (f"const DH = require({json.dumps(JS_MODULE)});\n"
              f"const vals = {json.dumps(PARITY_VALUES)};\n"
              "process.stdout.write(JSON.stringify(vals.map(v => { const vs = DH.dateVariants(v);"
              " return {variants: vs, patterns: vs.map(DH.variantPattern)}; })));\n")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    js = json.loads(out.stdout)
    for value, j in zip(PARITY_VALUES, js):
        py = vc.date_variants(value)
        assert py == j["variants"], value
        assert [vc.variant_pattern(v) for v in py] == j["patterns"], value


# -- process_document: flags land in field_sources -----------------------------------

def test_process_document_flags_values_not_in_text(monkeypatch, tmp_path):
    lines = [{"text": t, "box": (0, i * 10, 100, i * 10 + 8), "conf": 0.98} for i, t in enumerate(RAW.splitlines())]
    monkeypatch.setattr(ocr_service, "ocr_lines", lambda img: lines)
    monkeypatch.setattr(qs, "OCR_TEXT_SOURCE", "paddle")
    monkeypatch.setattr(qs, "call_qwen_vision", lambda *a, **k: pytest.fail("no Vision calls"))
    monkeypatch.setattr(qs, "classify_document", lambda text, hint_type=None: {"document_type": "invoice", "confidence": 0.9})
    extracted = {"invoice_no": "BK1141557", "invoice_date": "2026-09-23",
                 "customer": "Siti binti Abu", "line_items": [{"desc": "Cement"}]}
    monkeypatch.setattr(qs, "extract_fields", lambda text, doc_type, **k: dict(extracted))
    monkeypatch.setattr(qs, "get_custom_doc_fields", lambda t: [])
    p = tmp_path / "inv.png"
    Image.new("RGB", (200, 100), "white").save(p)

    result = qs.process_document(str(p))
    assert result["fields"]["customer"] == "Siti binti Abu"            # value untouched
    assert result["field_sources"].get("customer") == "not in document"
    assert result["field_sources"].get("invoice_no") != "not in document"
    assert result["field_sources"].get("invoice_date") != "not in document"
    assert "line_items" not in result["field_sources"]


# -- tolerant matching: numbers and multi-part values ----------------------------

STATEMENT = """MAYBANK ISLAMIC BERHAD
Statement Period: October 10
November 9
Account Holder: JAMES C. MORRISON
No. 25, Jalan Damai 3,
Taman Damai Perdana, 56000 Kuala Lumpur
Opening balance $1,876.02
Total deposits RM 1,000.00
Invoice 2024 ref 01 qty 15
Fees: 12.50 USD"""


@pytest.mark.parametrize("value", [
    "1876.02",                                   # vs $1,876.02
    "$1876.02",
    "RM1000",                                    # vs RM 1,000.00
    "1000",
    "1,000",
    "RM 1 000.00",                               # spaces inside the amount
    "MYR 1000.00",
    "12.5",                                      # vs 12.50 USD
    "12.50 USD",
    "Morrison, James C.",                        # vs JAMES C. MORRISON
    "James Morrison",
    "No. 25, Jalan Damai 3, Taman Damai Perdana, 56000 Kuala Lumpur",   # two lines, comma-joined
    "October 10 - November 9",                   # vs two lines
])
def test_tolerant_matches_found(value):
    assert vc.value_in_text(value, STATEMENT)


@pytest.mark.parametrize("value", [
    "2024-01-15",            # fabricated date: 2024, 01 and 15 all occur, but not as a date
    "15/01/2024",
    "15 January 2024",
    "1876.03",               # wrong amount
    "RM1001",
    "10000",                 # 1,000.00 is not 10000
    "Morrison, John C.",     # one word missing
    "No. 27, Jalan Damai 3", # wrong house number
    "October 10 - December 9",
    "X",                     # nothing but 1-char tokens and no exact match
])
def test_tolerant_matches_still_flag_wrong_values(value):
    assert not vc.value_in_text(value, STATEMENT)


def test_parse_number():
    D = vc.Decimal
    assert vc.parse_number("RM1000") == D("1000")
    assert vc.parse_number("RM 1,000.00") == D("1000")
    assert vc.parse_number("$1,876.02") == D("1876.02")
    assert vc.parse_number("1,876.02 MYR") == D("1876.02")
    assert vc.parse_number("-12.5") == D("-12.5")
    for not_a_number in ["BK1141557", "012-3456789", "1,2,3", "12.5.6", "RM", "abc", "2024-01-15"]:
        assert vc.parse_number(not_a_number) is None, not_a_number


def test_numbers_in_text():
    D = vc.Decimal
    nums = vc.numbers_in_text("Total RM 1,000.00, tax 60.00. Ref 1,2,3 and 7.")
    assert {D("1000"), D("60"), D("1"), D("2"), D("3"), D("7")} <= nums


def test_flagging_uses_tolerant_checks():
    fields = {"opening": "1876.02", "holder": "Morrison, James C.",
              "period": "October 10 - November 9", "due_date": "2024-01-15", "amount": "RM1001"}
    sources = {}
    assert sorted(vc.flag_values_not_in_document(fields, STATEMENT, sources)) == ["amount", "due_date"]
