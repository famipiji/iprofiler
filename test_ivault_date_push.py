"""iVault push: values of Date-type properties (iVault propertyType 3) are sent
as IVAULT_DATE_FORMAT (default YYYY-MM-DD), parsed with value_check.parse_dates;
ambiguous numeric dates follow DATE_ORDER (DMY default, or MDY). Unparseable
dates and non-date properties are sent unchanged; the stored field value is
never changed.

No iVault, no DB: the upload session is a fake that records the form data.

Run: pytest test_ivault_date_push.py -v
"""
import os
import tempfile

_DB_FILE = os.path.join(tempfile.mkdtemp(), "ivault_date_test.db")
os.environ.setdefault("SQLALCHEMY_DATABASE_URI", "sqlite:///" + _DB_FILE.replace("\\", "/"))

from types import SimpleNamespace

import pytest

import document_routes as dr


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("DATE_ORDER", raising=False)
    monkeypatch.delenv("IVAULT_DATE_FORMAT", raising=False)


# -- ivault_property_value ---------------------------------------------------------

@pytest.mark.parametrize("printed,expected", [
    ("23/09/2026", "2026-09-23"),
    ("9/23/2026", "2026-09-23"),        # only valid month-first -> no ambiguity
    ("5/6/2026", "2026-06-05"),         # ambiguous -> day-first by default
    ("05-06-26", "2026-06-05"),
    ("2026-09-23", "2026-09-23"),
    ("2026-09-23 10:15", "2026-09-23"),
    ("23 Sep 2026", "2026-09-23"),
    ("September 23, 2026", "2026-09-23"),
    ("5 Ogos 1990", "1990-08-05"),       # Malay month name
])
def test_date_property_normalized(printed, expected):
    assert dr.ivault_property_value(printed, 3) == expected
    assert dr.ivault_property_value(printed, "3") == expected   # type comes as str too


def test_mdy_order_for_ambiguous_dates(monkeypatch):
    monkeypatch.setenv("DATE_ORDER", "MDY")
    assert dr.ivault_property_value("5/6/2026", 3) == "2026-05-06"
    assert dr.ivault_property_value("23/09/2026", 3) == "2026-09-23"   # unambiguous stays right
    monkeypatch.setenv("DATE_ORDER", "dmy")
    assert dr.ivault_property_value("5/6/2026", 3) == "2026-06-05"


def test_custom_output_format(monkeypatch):
    monkeypatch.setenv("IVAULT_DATE_FORMAT", "%d/%m/%Y")
    assert dr.ivault_property_value("2026-09-23", 3) == "23/09/2026"


@pytest.mark.parametrize("printed", [
    "October 10 to November 9",          # a period, not one date
    "October 10",                        # no year - never completed
    "31/02/2026",                        # no such day
    "not a date",
])
def test_unparseable_date_sent_unchanged_with_warning(printed, capsys):
    assert dr.ivault_property_value(printed, 3) == printed
    assert "WARNING" in capsys.readouterr().out


@pytest.mark.parametrize("ptype", [0, 1, None, "", "abc"])
def test_non_date_property_unchanged(ptype, capsys):
    assert dr.ivault_property_value("5/6/2026", ptype) == "5/6/2026"
    assert "WARNING" not in capsys.readouterr().out


@pytest.mark.parametrize("value,expected", [(None, ""), ("", ""), ("  ", "  ")])
def test_empty_date_value(value, expected, capsys):
    assert dr.ivault_property_value(value, 3) == expected
    assert "WARNING" not in capsys.readouterr().out


# -- _push_fields_to_ivault form data --------------------------------------------------

class FakeSession:
    def __init__(self):
        self.data = None

    def post(self, url, headers=None, files=None, data=None, timeout=None, verify=None):
        self.data = dict(data)
        return SimpleNamespace(status_code=200, text="{}", json=lambda: {"ok": True})


def test_push_sends_normalized_date_and_keeps_stored_value(monkeypatch, tmp_path):
    monkeypatch.setattr(dr, "UPLOAD_FOLDER", str(tmp_path))
    (tmp_path / "u.pdf").write_bytes(b"%PDF-1.4")
    fields = {"date": "5/6/2026", "agency": "JKR", "period": "October 10 to November 9"}
    doc = SimpleNamespace(unique_upload_filename="u.pdf", filename="statement.pdf",
                          get_fields=lambda: fields)
    doc_type = SimpleNamespace(fields=[
        SimpleNamespace(field_name="date", external_property_id="p-date", external_property_type=3),
        SimpleNamespace(field_name="agency", external_property_id="p-agency", external_property_type=0),
        SimpleNamespace(field_name="period", external_property_id="p-period", external_property_type=3),
        SimpleNamespace(field_name="local_only", external_property_id=None, external_property_type=None),
    ])
    session = FakeSession()

    ok, _ = dr._push_fields_to_ivault(doc, doc_type, "class-1", "t.example", session, "Bearer x", "https://api")

    assert ok
    values = {session.data[f"files[0][properties][{i}][propertyId]"]: session.data[f"files[0][properties][{i}][value]"]
              for i in range(3)}
    assert values == {"p-date": "2026-06-05", "p-agency": "JKR", "p-period": "October 10 to November 9"}
    assert session.data["files[0][properties][0][propertyType]"] == "3"
    assert fields["date"] == "5/6/2026"   # stored value as printed
