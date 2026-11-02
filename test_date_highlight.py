"""Tests for static/date_highlight.js - date-aware field-click highlighting:
a date the model normalized (field "2026-09-23") must still be found where the
document prints "9/23/2026", "23.09.26", "23 Sept 2026", "23 Ogos 2026", ...

The JS module is run under node (skipped if node isn't installed).

Run: pytest test_date_highlight.py -v
"""
import json
import os
import shutil
import subprocess

import pytest

MODULE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "date_highlight.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _js(expr: str, **args):
    """Evaluate `expr` with the module as `DH` and `args` as JS consts; returns the JSON result."""
    consts = "".join(f"const {k} = {json.dumps(v)};\n" for k, v in args.items())
    script = (f"const DH = require({json.dumps(MODULE)});\n{consts}"
              f"process.stdout.write(JSON.stringify({expr}));\n")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _found(text, value):
    """The matched substring(s) of `text` for field `value`, or None."""
    res = _js("DH.findDateMatches(text, value)", text=text, value=value)
    return None if res is None else [text[m["index"]:m["index"] + m["length"]] for m in res["matches"]]


# -- parsing ----------------------------------------------------------------------

@pytest.mark.parametrize("value, dates", [
    ("2026-09-23", [[2026, 9, 23]]),
    ("2026/9/3", [[2026, 9, 3]]),
    ("2026-09-23T10:15:00", [[2026, 9, 23]]),
    ("23/09/2026", [[2026, 9, 23]]),                   # only day-first is valid
    ("9/23/2026", [[2026, 9, 23]]),                    # only month-first is valid
    ("05/06/2026", [[2026, 6, 5], [2026, 5, 6]]),      # ambiguous: both, day-first first
    ("23-09-26", [[2026, 9, 23]]),
    ("23.09.2026", [[2026, 9, 23]]),
    ("23 Sep 2026", [[2026, 9, 23]]),
    ("23rd September, 2026", [[2026, 9, 23]]),
    ("23 Ogos 2026", [[2026, 8, 23]]),                 # Malay
    ("1 Disember 2025", [[2025, 12, 1]]),
    ("Sep 23, 2026", [[2026, 9, 23]]),
    ("Sept. 23 2026", [[2026, 9, 23]]),
    ("31/02/2026", []),                                # not a real date
    ("BK1141557", []),
    ("11,494.50", []),
    ("12.5", []),
    ("", []),
])
def test_parse_dates(value, dates):
    got = _js("DH.parseDates(value).map(d => [d.y, d.m, d.d])", value=value)
    assert got == dates


# -- variant list -----------------------------------------------------------------

def test_variants_cover_common_printed_forms():
    v = _js("DH.dateVariants(value)", value="2026-09-23")
    assert v[0] == "2026-09-23"
    for expected in [
        "2026/09/23", "2026.09.23",
        "23/09/2026", "09/23/2026", "9/23/2026", "23/9/2026",
        "23-09-2026", "23.09.2026", "9.23.2026",
        "23/09/26", "9/23/26",
        "23 september 2026", "23 sep 2026", "sep 23, 2026", "september 23 2026", "23-sep-2026",
        "23 sept 2026", "23 sep 26",
    ]:
        assert expected in v, expected
    # numeric forms are tried before month names, 4-digit years before 2-digit
    assert v.index("23/09/2026") < v.index("23 sep 2026")
    assert v.index("23/09/2026") < v.index("23/09/26")
    assert len(v) == len(set(v))


def test_malay_month_names():
    v = _js("DH.dateVariants(value)", value="2026-08-05")
    for expected in ["5 ogos 2026", "05 ogos 2026", "ogos 5, 2026", "5 aug 2026", "5 august 2026"]:
        assert expected in v, expected
    v = _js("DH.dateVariants(value)", value="2026-03-01")
    assert "1 mac 2026" in v and "1 march 2026" in v


def test_non_date_has_no_variants():
    assert _js("DH.dateVariants(value)", value="INV-2026-0923") == []
    assert _js("DH.findDateMatches(text, value)", text="anything", value="Ahmad bin Ali") is None


# -- matching page text -----------------------------------------------------------

@pytest.mark.parametrize("page_text, found", [
    ("Invoice Date: 9/23/2026  Due: 10/23/2026", "9/23/2026"),
    ("Tarikh: 23/09/2026", "23/09/2026"),
    ("Date 23.09.26 ref", "23.09.26"),
    ("Date: 9 / 23 / 2026", "9 / 23 / 2026"),            # spaces around separators
    ("Date:9/23/2026Total", "9/23/2026"),                # text-layer spans joined without spaces
    ("Issued on 23 September 2026.", "23 September 2026"),
    ("Issued on 23rd Sept. 2026", "23rd Sept. 2026"),
    ("Dated September 23, 2026", "September 23, 2026"),
    ("Dated Sep 23rd, 2026", "Sep 23rd, 2026"),
    ("TARIKH 23 OGOS 2026", None),                       # wrong month for 2026-09-23
    ("TARIKH 23 SEPTEMBER 2026", "23 SEPTEMBER 2026"),   # case-insensitive
    ("2026-09-23", "2026-09-23"),
])
def test_finds_printed_form(page_text, found):
    assert _found(page_text, "2026-09-23") == (None if found is None else [found])


def test_no_match_inside_longer_numbers():
    assert _found("Ref 19/23/20261 and 9/23/20265", "2026-09-23") is None
    assert _found("Acct 123-09-2026", "2026-09-23") is None


def test_first_variant_wins_and_all_its_occurrences_found():
    # The value itself (ISO) comes first, so both ISO copies are returned,
    # not the 9/23/2026 that appears earlier on the page.
    text = "9/23/2026 ... 2026-09-23 ... 2026-09-23"
    assert _found(text, "2026-09-23") == ["2026-09-23", "2026-09-23"]


def test_malay_date_found_for_normalized_value():
    assert _found("Tarikh Lahir: 5 Ogos 1990", "1990-08-05") == ["5 Ogos 1990"]


def test_ambiguous_numeric_value_matches_either_order():
    # Model kept "05/06/2026"; document prints June 5 the US way.
    assert _found("Date: 6/5/2026", "05/06/2026") == ["6/5/2026"]


# -- the page's matcher (templates/index.html) ------------------------------------

def _page_matcher_source():
    html = open(os.path.join(os.path.dirname(MODULE), "..", "templates", "index.html"), encoding="utf-8").read()
    start = html.index("function _findValueMatches(")
    end = html.index("\n  function highlightInPdf(", start)
    return html[start:end]


def _page_find(text, value):
    script = (f"global.window = {{ DateHighlight: require({json.dumps(MODULE)}) }};\n"
              f"const DateHighlight = window.DateHighlight;\n{_page_matcher_source()}\n"
              f"process.stdout.write(JSON.stringify(_findValueMatches({json.dumps(text)}, {json.dumps(value)})"
              f".map(m => {json.dumps(text)}.substr(m.index, m.length))));\n")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_page_matcher_exact_match_unchanged():
    assert _page_find("Invoice BK1141557 / bk1141557", "BK1141557") == ["BK1141557", "bk1141557"]
    assert _page_find("Total 11,494.50", "11,494.50") == ["11,494.50"]
    assert _page_find("nothing here", "BK1141557") == []


def test_page_matcher_falls_back_to_date_variants():
    assert _page_find("Tarikh: 9 / 23 / 2026", "2026-09-23") == ["9 / 23 / 2026"]
    assert _page_find("Tarikh: 23 Ogos 2026", "2026-08-23") == ["23 Ogos 2026"]
