"""
Unit-level assertions for the Out-patient Prescription barcode extractor.

Decodes page 1 of Sample1_merged.pdf (the page that carries both an MRN
Barcode and a ticket QR sticker) and asserts the two values that matter:

  MRN Barcode (Code 128) = 082614010186
  QR 5                   = TSHWKS1260820260186

The sample file is not checked into the repo.  Set SAMPLE_PDF below to its
path, or pass it as the first CLI argument:

    python test_barcode_decode.py path/to/Sample1_merged.pdf
    pytest  test_barcode_decode.py   # skips automatically if file is absent

Requires:  zxing-cpp, Pillow, PyMuPDF  (all in requirements.txt)
"""
import os
import sys
import pytest

# ── Path resolution ──────────────────────────────────────────────────────────
# Override SAMPLE_PDF here or supply the path as the first CLI arg.
SAMPLE_PDF = os.environ.get(
    "SAMPLE_PDF",
    r"C:\Users\Dell\Downloads\Sample1_merged.pdf",
)
if len(sys.argv) > 1 and sys.argv[1].endswith(".pdf"):
    SAMPLE_PDF = sys.argv[1]

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_sample():
    if not os.path.exists(SAMPLE_PDF):
        pytest.skip(f"Sample PDF not found: {SAMPLE_PDF}")


def _require_zxingcpp():
    try:
        import zxingcpp  # noqa: F401
    except ImportError:
        pytest.skip("zxing-cpp not installed")


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_prescription_page1_mrn_and_qr():
    """
    extract_out_patient_prescription_barcodes on page 1 must return:
      mrn_barcode = '082614010186'  (Code 128 sticker)
      qr_5        = 'TSHWKS1260820260186'  (ticket QR sticker)
    """
    _require_sample()
    _require_zxingcpp()

    from qwen_service import extract_out_patient_prescription_barcodes

    result = extract_out_patient_prescription_barcodes(SAMPLE_PDF)

    assert result, "Extractor returned empty dict (zxingcpp likely not installed)"
    assert result.get("mrn_barcode") == "082614010186", (
        f"MRN Barcode mismatch: got {result.get('mrn_barcode')!r}"
    )
    assert result.get("qr_5") == "TSHWKS1260820260186", (
        f"QR 5 mismatch: got {result.get('qr_5')!r}"
    )


def test_map_barcode_keys_label_fallback():
    """
    map_barcode_keys_to_schema must map 'qr_5' onto a schema field whose
    field_label is 'QR 5' even when field_name does NOT normalise to 'qr5'
    (e.g. 'qr_code_5' -> 'qrcode5' misses by name but hits by label).
    """
    from qwen_service import map_barcode_keys_to_schema

    custom_fields = [
        {"field_name": "mrn_barcode",  "field_label": "MRN Barcode"},
        # field_name 'qr_code_5' normalises to 'qrcode5' which does NOT equal
        # 'qr5' -- the label 'QR 5' is the only path to a match.
        {"field_name": "qr_code_5",    "field_label": "QR 5"},
    ]
    barcode_data = {
        "mrn_barcode": "082614010186",
        "qr_5":        "TSHWKS1260820260186",
    }
    mapped = map_barcode_keys_to_schema(barcode_data, custom_fields)

    assert mapped.get("mrn_barcode") == "082614010186"
    assert mapped.get("qr_code_5")   == "TSHWKS1260820260186", (
        "qr_5 extractor key must fall back to label 'QR 5' match and land on "
        "field_name 'qr_code_5'"
    )


# ── Script entry-point ────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[Test] Sample PDF: {SAMPLE_PDF}")

    try:
        test_map_barcode_keys_label_fallback()
        print("[Test] map_barcode_keys_to_schema label-fallback: PASSED")
    except AssertionError as e:
        print(f"[Test] map_barcode_keys_to_schema label-fallback: FAILED — {e}")

    try:
        test_prescription_page1_mrn_and_qr()
        print("[Test] prescription page-1 decode: PASSED")
    except pytest.skip.Exception as e:
        print(f"[Test] prescription page-1 decode: SKIPPED — {e}")
    except AssertionError as e:
        print(f"[Test] prescription page-1 decode: FAILED — {e}")
