"""Check extracted field values against the document's own text.

A value the model returned that doesn't occur in raw_text (a hallucination,
a misread, or a value it reformatted beyond recognition) is flagged as
"not in document" in field_sources; the value itself is never changed.

A value counts as found when it occurs in raw_text ignoring case and all
whitespace, or - for a date-like value - when any of its printed variants
does. The date rules are a port of static/date_highlight.js (the UI's
field-click highlighting) and must stay in step with it:
test_value_check.py compares the two variant lists under node.
"""
import re

NOT_IN_DOCUMENT = "not in document"

# Empty-ish values the model writes instead of null - nothing to check.
_PLACEHOLDERS = {"", "not found", "not detected", "n/a", "na", "null", "none", "-"}

EN_FULL = ["january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december"]
EN_SHORT = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
MS_FULL = ["januari", "februari", "mac", "april", "mei", "jun", "julai",
           "ogos", "september", "oktober", "november", "disember"]
MS_SHORT = ["jan", "feb", "mac", "apr", "mei", "jun", "jul", "ogo", "sep", "okt", "nov", "dis"]

MONTH_LOOKUP = {}
for _names in (EN_FULL, EN_SHORT, MS_FULL, MS_SHORT):
    for _i, _name in enumerate(_names):
        MONTH_LOOKUP[_name] = _i + 1
MONTH_LOOKUP["sept"] = 9


def _pad2(n: int) -> str:
    return f"{n:02d}"


def _full_year(y) -> int:
    y = int(y)
    if y >= 100:
        return y
    return 2000 + y if y < 70 else 1900 + y


def _valid_date(y: int, m: int, d: int) -> bool:
    if m < 1 or m > 12 or d < 1 or y < 1000:
        return False
    leap = y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
    days = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return d <= days[m - 1]


def parse_dates(value) -> list:
    """The date(s) a value may mean, as [(y, m, d)], most likely first; [] if
    it doesn't look like a date. A numeric value that could be D/M or M/D
    (05/06/2026) yields both readings, day-first first."""
    s = re.sub(r"\s+", " ", str("" if value is None else value).strip().lower())
    out = []

    def add(y, m, d):
        if _valid_date(y, m, d) and (y, m, d) not in out:
            out.append((y, m, d))

    mt = re.match(r"^(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})(?:[t ]\d{1,2}:\d{2}.*)?$", s)
    if mt:
        add(int(mt[1]), int(mt[2]), int(mt[3]))
        return out
    mt = re.match(r"^(\d{1,2})\s*([-/.])\s*(\d{1,2})\s*\2\s*(\d{2}|\d{4})$", s)
    if mt:
        a, b, y = int(mt[1]), int(mt[3]), _full_year(mt[4])
        add(y, b, a)   # day-first
        add(y, a, b)   # month-first
        return out
    mt = re.match(r"^(\d{1,2})(?:st|nd|rd|th)?[\s\-/.,]*([a-z]+)\.?[\s\-/.,]*(\d{2}|\d{4})$", s)
    if mt:
        if mt[2] in MONTH_LOOKUP:
            add(_full_year(mt[3]), MONTH_LOOKUP[mt[2]], int(mt[1]))
        return out
    mt = re.match(r"^([a-z]+)\.?[\s\-/.]*(\d{1,2})(?:st|nd|rd|th)?[\s,]*(\d{2}|\d{4})$", s)
    if mt:
        if mt[1] in MONTH_LOOKUP:
            add(_full_year(mt[3]), MONTH_LOOKUP[mt[1]], int(mt[2]))
        return out
    return out


def date_variants(value) -> list:
    """Printed variants of a date value (lowercase), in the order they're
    tried - same list and order as dateVariants() in static/date_highlight.js.
    [] for a non-date value."""
    dates = parse_dates(value)
    if not dates:
        return []
    out, seen = [], set()

    def push(v):
        if v not in seen:
            seen.add(v)
            out.append(v)

    push(re.sub(r"\s+", " ", str(value).strip().lower()))
    seps = ["/", "-", "."]
    for y, m, d in dates:
        Y, YY = str(y), str(y)[-2:]
        days, months = [_pad2(d), str(d)], [_pad2(m), str(m)]
        for sep in seps:
            push(Y + sep + _pad2(m) + sep + _pad2(d))
        for yr in (Y, YY):
            for sep in seps:
                for dd in days:
                    for mm in months:
                        push(dd + sep + mm + sep + yr)   # day-first
                        push(mm + sep + dd + sep + yr)   # month-first
    for y, m, d in dates:
        Y, YY = str(y), str(y)[-2:]
        names = [EN_FULL[m - 1], EN_SHORT[m - 1], MS_FULL[m - 1], MS_SHORT[m - 1]]
        if m == 9:
            names.append("sept")
        days = [str(d), _pad2(d)]
        for mon in names:
            for yr in (Y, YY):
                for dd in days:
                    push(f"{dd} {mon} {yr}")
                    push(f"{dd}-{mon}-{yr}")
                    push(f"{mon} {dd}, {yr}")
                    push(f"{mon} {dd} {yr}")
    return out


def _escape_re(s: str) -> str:
    """escapeRe() from date_highlight.js - same escaped characters, so the
    two pattern sources are identical."""
    return re.sub(r"[.*+?^${}()|\[\]\\]", lambda m: "\\" + m.group(0), s)


def variant_pattern(variant: str) -> str:
    """Regex for one variant: whitespace (or none) around separators and
    between tokens, optional "." after a short month name, optional ordinal
    on a day in a month-name form, no digit/letter directly before or after."""
    tokens = re.findall(r"\d+|[a-z]+|[^\s\da-z]|\s+", variant)
    has_month_name = bool(re.search(r"[a-z]", variant))
    src = ""
    for t in tokens:
        if t.isspace():
            src += r"\s*"
        elif t.isdigit():
            src += t + ("(?:st|nd|rd|th)?" if has_month_name and len(t) <= 2 else "")
        elif t.isalpha():
            src += _escape_re(t) + (r"\.?" if t in MONTH_LOOKUP and len(t) <= 4 else "")
        else:
            src += r"\s*" + _escape_re(t) + r"\s*"
    pre = "(?<![0-9])" if variant[:1].isdigit() else "(?<![a-z])"
    post = "(?![0-9])" if variant[-1:].isdigit() else "(?![a-z])"
    return pre + src + post


def find_date_variant(text: str, value):
    """The first variant of a date value found in `text`, or None."""
    if not text:
        return None
    for v in date_variants(value):
        if re.search(variant_pattern(v), text, re.IGNORECASE):
            return v
    return None


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def value_in_text(value, raw_text: str) -> bool:
    """True when a scalar field value occurs in the document text: ignoring
    case and whitespace, or as any printed variant of a date value."""
    v = str(value).strip()
    if not v:
        return True
    if _squash(v) in _squash(raw_text or ""):
        return True
    return find_date_variant(raw_text or "", v) is not None


def flag_values_not_in_document(fields: dict, raw_text: str, field_sources: dict,
                                skip_sources=("barcode scan",)) -> list:
    """Set field_sources[name] = "not in document" for every non-empty scalar
    value that value_in_text() can't find; returns the flagged names. Tables
    and other list/dict values, and fields whose source is in `skip_sources`
    (read from the image, not the text), are skipped. No-op without raw_text
    (nothing to check against). Field values are never changed."""
    if not raw_text or not raw_text.strip() or not isinstance(fields, dict):
        return []
    flagged = []
    for name, value in fields.items():
        if value is None or isinstance(value, (bool, list, dict, tuple, set)):
            continue
        if isinstance(value, str) and value.strip().lower() in _PLACEHOLDERS:
            continue
        if field_sources.get(name) in skip_sources:
            continue
        if not value_in_text(value, raw_text):
            field_sources[name] = NOT_IN_DOCUMENT
            flagged.append(name)
    return flagged
