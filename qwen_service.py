import os
import json
import re
import base64
import io
import sys
import time
import threading
import tempfile
import difflib
from collections import deque
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from PIL import Image
import openai
from openai import OpenAI

# ?????? In-memory log buffer ??? captures all print() output ?????????????????????????????????????????????????????????????????????
_log_buffer: deque = deque(maxlen=500)

class _TeeStream:
    """Writes to both the original stdout and the in-memory buffer."""
    def __init__(self, original):
        self._orig = original
    def write(self, msg):
        self._orig.write(msg)
        if msg.strip():
            _log_buffer.append(f"{datetime.now().strftime('%H:%M:%S')} {msg.rstrip()}")
    def flush(self):
        self._orig.flush()
    def __getattr__(self, attr):
        return getattr(self._orig, attr)

sys.stdout = _TeeStream(sys.stdout)
sys.stderr = _TeeStream(sys.stderr)

def get_log_buffer() -> list:
    return list(_log_buffer)

# ?????? LM Studio / Ollama client ???????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????
# Base URL: env var ??? LM Studio default. Set OLLAMA_BASE_URL in docker-compose for Ollama.
_base_url = os.environ.get("OLLAMA_BASE_URL", "http://192.168.1.18:1234/v1")
_api_key  = os.environ.get("LLM_API_KEY", "lm-studio")

client = OpenAI(
    base_url=_base_url,
    api_key=_api_key,
    timeout=600.0,  # 10 minutes ??? 7B model needs more time for OCR + extraction
)


class QwenServiceUnavailableError(Exception):
    """Raised when the LM Studio/Ollama backend is unreachable or failing after retries are exhausted."""


# Only retry connection/timeout/5xx/rate-limit class failures ??? a transient LM
# Studio restart or dropped connection is worth retrying, a bad request (4xx,
# validation) is not and should surface immediately.
_RETRYABLE_EXCEPTIONS = (
    openai.APIConnectionError,   # covers APITimeoutError, which subclasses it
    openai.InternalServerError,  # 5xx from the backend
    openai.RateLimitError,       # 429
)


def _call_qwen_with_retry(fn, *, label: str, attempts: int = 3, base_delay: float = 1.0):
    """Run `fn()` (a Qwen/LM Studio client call) with exponential-backoff retry.

    Retries only transient connection/timeout/5xx/rate-limit errors. After the
    last attempt fails, raises QwenServiceUnavailableError instead of letting
    the raw client exception propagate, so callers can catch one clean type.
    """
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except _RETRYABLE_EXCEPTIONS as e:
            last_exc = e
            if attempt == attempts:
                break
            delay = base_delay * (2 ** (attempt - 1))
            print(f"[Qwen] {label} failed (attempt {attempt}/{attempts}): {e} ??? retrying in {delay:.1f}s")
            time.sleep(delay)
    raise QwenServiceUnavailableError(f"{label} failed after {attempts} attempts: {last_exc}") from last_exc


# ?????? Per-thread LM progress tracking ?????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????
_progress_ctx = threading.local()
_prompt_override_ctx = threading.local()

# Per-thread cache for DocumentType/custom-field DB lookups, scoped to a
# single process_document() run (document_routes.py spins up one background
# thread per uploaded file, so thread-local naturally isolates concurrent
# uploads from each other). get_custom_doc_fields/get_all_active_doc_type_names
# are each called several times per document with identical arguments -
# without this, one document triggers 5+ redundant DB round-trips for rows
# that can't change mid-run.
_doc_query_cache_ctx = threading.local()


def _doc_query_cache_get(key):
    cache = getattr(_doc_query_cache_ctx, "cache", None)
    if cache is not None and key in cache:
        return cache[key], True
    return None, False


def _doc_query_cache_set(key, value):
    cache = getattr(_doc_query_cache_ctx, "cache", None)
    if cache is not None:
        cache[key] = value


def _doc_query_cache_start():
    """Call once at the start of process_document() so lookups within this
    run get cached; leaving it unset (the default) makes callers outside a
    process_document run behave exactly as before - a real query every time."""
    _doc_query_cache_ctx.cache = {}


def _doc_query_cache_clear():
    _doc_query_cache_ctx.cache = None

def _init_lm_progress(cb):
    """Call once per processing thread. cb(label: str, step_frac: float) ??? updates SSE."""
    _progress_ctx.cb           = cb
    _progress_ctx.slot         = None
    _progress_ctx.current_step = 0.0

def _set_lm_slot(label: str, start: float, end: float):
    """Mark the step range the next LM call will animate through."""
    label = getattr(_progress_ctx, 'label_prefix', '') + label
    _progress_ctx.slot = (label, start, end)
    cb = getattr(_progress_ctx, 'cb', None)
    if cb:
        _progress_ctx.current_step = start
        cb(label, start)

def _get_current_step() -> float:
    return getattr(_progress_ctx, 'current_step', 0.0)

def _fire_token_frac(token_frac: float):
    cb   = getattr(_progress_ctx, 'cb',   None)
    slot = getattr(_progress_ctx, 'slot', None)
    if not (cb and slot):
        return
    label, start, end = slot
    step = start + token_frac * (end - start)
    _progress_ctx.current_step = step
    cb(label, step)

_LARGE_DOC_SUFFIX = " (large amount of text detected — this may take a few minutes)"


def _mark_stream_as_large_document():
    """Appends a one-time qualitative note to the CURRENT progress slot's
    label so the SSE step text (and thus the UI) sets the right expectation
    for a long-running generation instead of just a slowly-creeping
    percentage with no context -- see _STREAM_LARGE_DOC_TOKEN_THRESHOLD.
    Mutates _progress_ctx.slot in place: _fire_token_frac() re-reads the
    label from this slot on every subsequent call, so this takes effect on
    the very next progress update with no other wiring needed."""
    slot = getattr(_progress_ctx, 'slot', None)
    if not slot:
        return
    label, start, end = slot
    if label.endswith(_LARGE_DOC_SUFFIX):
        return
    _progress_ctx.slot = (label + _LARGE_DOC_SUFFIX, start, end)

def _mark_page_done(pages_done: int, pages_total: int):
    """
    Report that `pages_done` of `pages_total` pages have fully completed
    processing (through highlight generation). Drives the live page
    counter in the UI, separate from the animated step label.
    """
    if getattr(_progress_ctx, 'suppress_page_mark', False):
        return
    cb = getattr(_progress_ctx, 'cb', None)
    if cb:
        cb(None, None, pages_done=pages_done, pages_total=pages_total)

@contextmanager
def _split_record_progress(label_prefix: str):
    """
    Wrap the process_document() call for ONE record carved out of a larger
    split PDF. Left alone, that call reports the record's own page count as if
    it were the whole upload -- "0 / 2 pages" and "OCR page 1/2..." for a
    6-page file split into 2-page records. Inside this block:
      - its page-completion signals are silenced (the split pipeline reports
        cumulative progress against the full page count itself), and
      - its step labels are prefixed with the record's position, so the
        record-relative "OCR page 1/2..." reads "Record 1/3 - OCR page 1/2...".
    """
    _progress_ctx.suppress_page_mark = True
    _progress_ctx.label_prefix = label_prefix
    try:
        yield
    finally:
        _progress_ctx.suppress_page_mark = False
        _progress_ctx.label_prefix = ""

MODEL      = os.environ.get("OLLAMA_MODEL", "qwen2.5-vl-32b-instruct")
TEXT_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-vl-32b-instruct")

# Same knob/caveat as document_routes.MAX_CONCURRENT_UPLOADS: pages within one
# scanned PDF are network/GPU-wait bound, not CPU bound, so overlapping their
# OCR calls can help -- but only if the model server actually runs completions
# in parallel rather than just queuing them behind a single-GPU worker. Read
# independently here (qwen_service must not import document_routes, which
# already imports qwen_service) but intentionally the same env var, since both
# ultimately bound how many concurrent requests hit the same model server.
_PAGE_CONCURRENCY = max(1, int(os.environ.get("MAX_CONCURRENT_UPLOADS", "2")))

# Split-PDF pipelines (process_split_pdf, process_barcode_split_pdf,
# detect_separator_groups) run one sequential LLM call per page with no
# other bound -- a very large PDF could take many minutes and hold
# significant memory. Reject oversized jobs up front instead of grinding
# through them page by page.
MAX_SPLIT_PDF_PAGES = int(os.environ.get("MAX_SPLIT_PDF_PAGES", "200"))

# ?????? Confidence helpers ????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

CONFIDENCE_MAP = {"high": 0.90, "medium": 0.65, "low": 0.35}

def normalize_confidence(raw, source="text"):
    SOURCE_DEFAULTS = {"vision_thumb": 0.90, "vision_full": 0.85, "text": 0.65, "fallback": 0.35}
    if raw is None:
        return SOURCE_DEFAULTS.get(source, 0.65)
    if isinstance(raw, float):
        return max(0.0, min(1.0, raw))
    raw_str = str(raw).lower().strip()
    if raw_str.endswith("%"):
        try:
            return max(0.0, min(1.0, float(raw_str[:-1]) / 100))
        except ValueError:
            pass
    if raw_str in CONFIDENCE_MAP:
        return CONFIDENCE_MAP[raw_str]
    try:
        return max(0.0, min(1.0, float(raw_str)))
    except ValueError:
        pass
    return SOURCE_DEFAULTS.get(source, 0.65)

def format_confidence(score):
    return f"{round(score * 100)}%"

# ?????? Self-learning structures ??????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????
LEARNED_STRUCTURES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "learned_structures.json")

# ?????? Field definitions per document type ??????????????????????????????????????????????????????????????????????????????????????????????????????????????????
FIELD_MAP = {
    "invoice":        "invoice_number, date, due_date, vendor_name, customer_name, line_items, subtotal, tax, total_amount, currency",
    "receipt":        "receipt_number, date, merchant_name, items, subtotal, tax, total_amount, payment_method",
    "id_card":        "full_name, id_number, date_of_birth, gender, nationality, address, expiry_date",
    "passport":       "full_name, passport_number, nationality, date_of_birth, issue_date, expiry_date, mrz_line",
    "contract":       "contract_title, parties, effective_date, expiry_date, key_terms, signatures",
    "bond_purchase_agreement": (
        "contract_title, bond_purchaser, issuer, resolution_number, bond_series, "
        "principal_amount, purchase_price, interest_rate, repayment_period, "
        "total_interest_paid, effective_date, maturity_date, source_of_repayment, "
        "bond_counsel, financial_advisor, governing_law, parties, key_terms, signatures"
    ),
    "medical_report": "patient_name, date, doctor_name, diagnosis, medications, recommendations",
    "bank_statement": "account_holder, account_number, bank_name, statement_period, opening_balance, closing_balance, transactions",
    "resume":         "full_name, email, phone, address, linkedin, summary, work_experience, education, skills, certifications, languages",
    "form":           "all visible form fields and values",
    "letter":         "sender_name, recipient_name, date, subject, body_summary",
    "other":          "all key information as field-value pairs",
}


# ?????? Document-type specific structure hints for extraction ????????????????????????????????????????????????????????????
DOCUMENT_STRUCTURE_HINTS = {
    "resume": (
        'Extract these resume fields as structured arrays:\n'
        '- "work_experience": array of objects ??? each entry: {"role": "...", "company": "...", "period": "...", "responsibilities": ["bullet1", "bullet2"]}\n'
        '- "education": array of objects ??? each entry: {"degree": "...", "institution": "...", "period": "..."}\n'
        '- "certifications": array of objects ??? each entry: {"name": "...", "date": "..."}\n'
        '- "skills": plain string array ??? ["Python", "React", "Docker"]\n'
        '- "languages": array of objects ??? each entry: {"language": "...", "level": "..."}\n'
        'Scalar fields (full_name, email, phone, address, linkedin, summary) remain as plain strings.'
    ),
    "bank_statement": (
        'Extract "transactions" as an array of objects: {"date": "...", "description": "...", "debit": ..., "credit": ..., "balance": ...}. '
        'Limit to 10 rows. Extract opening_balance and closing_balance as scalar strings.'
    ),
    "contract": (
        'Extract "parties" as an array of objects: {"name": "...", "role": "..."}. '
        'Extract "key_terms" as a string array of the most important clauses.'
    ),
    "bond_purchase_agreement": (
        'You MUST extract ALL of these specific fields:\n'
        '- "contract_title": the document title (e.g. "BOND PURCHASE AGREEMENT")\n'
        '- "bond_purchaser": name of the purchasing entity\n'
        '- "issuer": name of the issuing county/entity\n'
        '- "resolution_number": the resolution number (e.g. "R-14-07")\n'
        '- "bond_series": full name of the bond series\n'
        '- "principal_amount": max authorized amount (e.g. "$7,500,000")\n'
        '- "purchase_price": actual purchase price (e.g. "$7,275,000")\n'
        '- "interest_rate": interest rate as string (e.g. "1.09%")\n'
        '- "repayment_period": repayment duration (e.g. "57 months")\n'
        '- "total_interest_paid": total interest over life of bond (e.g. "$200,010.46")\n'
        '- "effective_date": agreement date\n'
        '- "maturity_date": final repayment date\n'
        '- "source_of_repayment": how bond will be repaid (e.g. "ad valorem taxes")\n'
        '- "bond_counsel": law firm providing bond counsel opinion\n'
        '- "financial_advisor": financial advisor firm if mentioned\n'
        '- "governing_law": state whose laws govern the agreement\n'
        '- "parties": array of objects ??? [{"name": "...", "role": "..."}]\n'
        '- "key_terms": array of strings ??? summarize each numbered clause (e.g. ["Purchase and Sale: County agrees to sell bonds at $7,275,000", ...])\n'
        '- "signatures": array of objects ??? [{"signatory": "full name or blank", "title": "...", "organization": "..."}]\n'
        'Look across ALL pages including Schedule A for interest_rate, total_interest_paid, and maturity_date.'
    ),
    "medical_report": (
        'Extract "medications" as an array of objects: {"name": "...", "dosage": "...", "frequency": "..."}. '
        'Extract "recommendations" as a string array.'
    ),
    "utility_bill": (
        'Extract ANY section that contains rows of usage/charges as an array of objects: '
        '{"description": "...", "previous_reading": "...", "current_reading": "...", "usage": "...", "rate": "...", "amount": ...}. '
        'This applies to any section header followed by tabular rows (e.g. Electricity Usage, Water Usage, Gas Usage, Internet Usage, or any similar section). '
        'Copy the "rate" value EXACTLY as it appears (e.g. "$0.138/kWh") ??? do NOT round or strip units. '
        'Extract BILLING SUMMARY entries (Previous Balance, Payments Received, subtotals, taxes, etc.) as individual scalar fields ??? NOT as an array. '
        'Do NOT create a generic "Line Items" or "items" array. '
        'All other fields (account number, billing period, total amount due, etc.) remain as plain scalars.'
    ),
    "invoice": (
        'CRITICAL: The document contains one or more tables. For EACH table found, extract it as a named array using a key derived from the table\'s own column header row (e.g. "line_items", "charges", "description_of_charges", or whatever the column headers describe).\n'
        'IMPORTANT: Use the ACTUAL column headers from the document as the keys inside each row object. Do NOT force generic keys like "item_no", "description", "quantity" ??? mirror the exact column names you see (e.g. "Description of Charges", "Qty", "UoM", "Curr", "Unit Price", "ROE", "Total(MYR)" or Chinese headers like "????????????", "?????????", "??????", "??????", "??????", "??????").\n'
        'Each table row becomes one object in the array. Each column header becomes a key. Copy cell values exactly as they appear in the document.\n'
        'SECTION LABELS RULE: If there is a label or number printed ABOVE the table (e.g. "Rep : 04", "Rep No. 04", "Section A") it is NOT a column header and NOT an array name ??? capture it as a "section" scalar field.\n'
        'FOOTER ROWS RULE: Rows at the bottom of the table that are totals or summaries (e.g. SALES TAX, TOTAL, O.E., subtotals) are NOT line items ??? extract them as scalar fields OUTSIDE the array (e.g. "sales_tax": "0.00", "total_amount": "11,494.50").\n'
        'ADDRESS RULE: Company name, street address, city, postcode, phone, fax in the document header are NEVER line items ??? extract them as scalar fields (e.g. "vendor_name", "billing_address", "phone").\n'
        'ONE TABLE ONLY RULE: There is only ONE line items table in the document ??? the grid with column headers. Do NOT create multiple line item arrays. Do NOT create a second array from header text, address blocks, or any non-table content.\n'
        'EXAMPLE for a Maersk-style invoice: "charges": [{"Description of Charges": "Basic Ocean Freight", "Qty": "1.000", "UoM": "CNT", "Curr": "USD", "Unit Price": "900.00", "ROE": "4.17800", "Total(MYR)": "3,760.20"}, ...]\n'
        'EXAMPLE with section label: "line_items": [{"section": "Rep 04", "DESCRIPTION": "0801COM MPOSITE CMT", "UNIT": "BAG", "QUANTITY": "790", "UNIT PRICE": "17.750", "AMOUNT": "14,022.50"}, ...]\n'
        'DO NOT create separate flat fields for individual column values ??? they belong INSIDE each row object.\n'
        'DO NOT collect all values of one column into a single comma-separated string.\n'
        'Scalar fields (extract as plain strings/numbers OUTSIDE the table arrays): '
        '"invoice_number", "date", "due_date", "vendor_name", "customer_name", "billing_address", '
        '"subtotal", "tax", "sales_tax", "total_amount", "currency", "terms", "account_code", "analysis_code", "page", '
        '"po_number", "do_number", "reference_number", "bill_of_lading_number", "vessel_voyage", '
        '"port_of_loading", "port_of_discharge", "etd", "eta".\n'
        'CURRENCY RULE: For "currency", list EVERY distinct currency code that appears anywhere in the document '
        '(header, line items table, totals). If only one currency is used write it as a plain string (e.g. "USD"). '
        'If multiple currencies appear write them all comma-separated (e.g. "USD, KRW"). Never drop a currency that is visible.'
    ),
    "receipt": (
        'CRITICAL: If the document contains a table of purchased items, extract it as "line_items": an array of objects where EACH OBJECT IS ONE ROW.\n'
        'Use the ACTUAL column headers from the document as the keys in each row object ??? do NOT force generic names.\n'
        'DO NOT collect all values of one column into a single comma-separated string.\n'
        'Scalar fields: "receipt_number", "date", "vendor_name", "customer_name", "subtotal", "tax", "total_amount", "currency", "payment_method".\n'
        'CURRENCY RULE: For "currency", list every distinct currency code visible in the document, comma-separated (e.g. "USD, KRW").'
    ),
    "purchase_order": (
        'CRITICAL: The document contains a table of ordered items. Extract it as "line_items": an array of objects where EACH OBJECT IS ONE ROW.\n'
        'Each line item object should have: "item_no", "item_code", "description", "quantity", "unit_price", "amount".\n'
        'DO NOT create separate flat fields for each column ??? values belong INSIDE each line_items object.\n'
        'Scalar fields: "po_number", "date", "vendor_name", "customer_name", "billing_address", "delivery_address", '
        '"subtotal", "tax", "total_amount", "currency", "terms", "account_code".\n'
        'CURRENCY RULE: For "currency", list every distinct currency code visible in the document, comma-separated (e.g. "USD, KRW").'
    ),
}

# ?????? Document-type specific OCR hints ???????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????
OCR_HINTS = {
    "invoice": (
        "This is an invoice. Focus on: "
        "invoice number, date, vendor name and address, customer name and address, "
        "all line items with quantities and prices, subtotal, tax, total amount."
    ),
    "receipt": (
        "This is a receipt. Focus on: "
        "receipt number, date, merchant name, all items with prices, "
        "subtotal, tax, total, payment method."
    ),
    "bank_statement": (
        "This is a bank statement. Focus on: "
        "account holder full name, IC number, account number, "
        "bank name and branch, statement period dates, "
        "opening balance, closing balance, "
        "all transaction rows with date, description, debit, credit, balance."
    ),
    "id_card": (
        "This is an ID card. Focus on: "
        "full name, ID/IC number, date of birth, gender, nationality, address."
    ),
    "passport": (
        "This is a passport. Focus on: "
        "full name, passport number, nationality, date of birth, "
        "issue date, expiry date, MRZ lines at the bottom."
    ),
    "medical_report": (
        "This is a medical report. Focus on: "
        "patient name, date, doctor name, diagnosis, medications, recommendations."
    ),
    "contract": (
        "This is a contract. Focus on: "
        "contract title, all party names, effective date, expiry date, key terms."
    ),
    "bond_purchase_agreement": (
        "This is a bond purchase agreement. Transcribe ALL text including Schedule A. "
        "Specifically capture: bond purchaser name, issuer/county name, resolution number, "
        "bond series name, principal amount, purchase price, interest rate (%), "
        "repayment period in months, total interest paid over life of bond ($), "
        "maturity/final payment date, source of repayment, bond counsel firm name, "
        "financial advisor name, governing law state, all signatory names and titles."
    ),
    "resume": (
        "This is a resume or CV. Focus on: "
        "full name, email, phone number, address, LinkedIn URL, "
        "professional summary or objective, "
        "all work experience entries (job title, company, dates, responsibilities), "
        "all education entries (degree, institution, dates), "
        "skills list, certifications, languages."
    ),
    "other": (
        "Transcribe all visible text including headers, labels, values, "
        "dates, numbers, names, and any tables."
    ),
}

# ?????? Editable prompt templates (overridable via Prompt Manager) ????????????????????????????????????????????????

DEFAULT_CLASSIFY_PROMPT = """Classify this document. What specific type of document is this?
Known types (use one of these if it matches): {{KNOWN_TYPES}}
If none match, provide a precise snake_case type name (e.g., 'employment_contract', 'warranty_card').{{HINT}}
IMPORTANT: Reply in English only ??? do NOT use Chinese or any other language for the document_type value.
IMPORTANT: If the document header says "DOCUMENT TYPE: INVOICE" or "INVOICE" prominently, classify as 'invoice' even if it contains fields like "SALES ORDER TYPE".

Document text:
{{DOCUMENT_TEXT}}

Reply ONLY with this exact JSON, no explanation, no markdown:
{"document_type":"<precise_type>","confidence":"high|medium|low"}"""


DEFAULT_EXTRACT_STRUCTURED_PROMPT = """You are a document data extractor. Extract ALL fields from this {{DOCUMENT_TYPE}}.

REQUIRED fields: {{FIELDS}}
{{EXAMPLES_BLOCK}}{{INVOICE_HINT}}
FULL document text:
\"\"\"
{{TEXT_BLOCK}}
\"\"\"

Instructions:
- Read the ENTIRE text above carefully before extracting
- You MUST return a value for EVERY field listed above
- Use null ONLY if the information is truly NOT in the text
- Copy values exactly as they appear in the text
- For line_items/items/transactions: return array of objects
- For diagnosis/medications/recommendations: look for these sections even if they appear later in the text
- For work_experience: return array of objects with keys: job_title, company, start_date, end_date, responsibilities
- For education: return array of objects with keys: degree, institution, start_date, end_date, grade
- For skills/certifications/languages: return array of strings

Reply with ONLY this JSON format, nothing else:
{{JSON_TEMPLATE}}"""


DEFAULT_EXTRACT_GENERIC_DIRECTIVE_PROMPT = """Extract fields from this document into a JSON object using ONLY the field names defined in DOCUMENT STRUCTURE RULES below.

DOCUMENT:
{{TEXT_BLOCK}}
{{LEARNED_HINT}}{{STRUCTURE_HINT}}{{TABLE_HINT_BLOCK}}

Rules:
1. Output ONLY {...} ??? no markdown, no explanation, no code blocks
2. Extract ONLY the fields listed in DOCUMENT STRUCTURE RULES ??? do NOT add any extra fields
3. Do NOT infer or add fields not explicitly defined above
4. If a value is truly not in the document ??? null
5. Copy values exactly as they appear in the text
6. All on one line, no extra whitespace

Output JSON:"""


DEFAULT_EXTRACT_GENERIC_OPEN_PROMPT = """Extract all key-value fields from this document. Output ONLY a single-line JSON object.

DOCUMENT:
{{TEXT_BLOCK}}
{{LEARNED_HINT}}{{TABLE_HINT_BLOCK}}

Rules:
1. Output ONLY {...} ??? no markdown, no explanation, no code blocks
{{KEY_LANG_RULE}}
3. Copy field values EXACTLY as they appear next to their labels
4. If a value is not in the document ??? null
5. Tables already extracted separately ??? do NOT re-extract table rows as scalar fields
6. Address blocks ??? ONE string: "Name, Street, City, Country"
7. Bullet point lists ??? JSON array of strings
8. All on one line, no extra whitespace
{{EXAMPLE_BLOCK}}
Output JSON:"""


DEFAULT_EXTRACT_VISION_PROMPT = """You are a document field extractor. Output ONLY valid JSON on a single line.

{{LANG_RULE}}{{TABLE_HINT_BLOCK}}{{STRUCTURE_HINT_BLOCK}}{{STRICT_RULE}}

CRITICAL Instructions:
1. Output ONLY the JSON object, NOTHING ELSE - no markdown, no code blocks (no ```), no explanations
2. Start IMMEDIATELY with { and end with }
3. GROUND TRUTH RULE: Every field MUST be visibly present in the image. Do NOT infer, guess, or hallucinate. If not visible ??? null.
4. {{FIELD_SCOPE_RULE}}
5. {{KEY_RULE}}
6. Field values: Copy the value EXACTLY as it appears in the document.
7. {{TABLE_KEY_RULE}}
8. {{SUMMARY_KEY_RULE}}
9. Single line, no extra whitespace between tokens.
10. Ensure all opening braces { have matching closing braces }.
11. TABLE DETECTION RULE: If TABLE DETECTION above says "TABLE DETECTED" or "MARKDOWN TABLE DETECTED", you MUST extract rows as a JSON array of objects. NEVER extract table rows as plain strings.
12. BULLET POINTS: If "BULLET LIST DETECTED", extract as a JSON array. Objects if items have sub-fields, strings if plain text.
13. ADDRESS / CONTACT BLOCKS: If a label (e.g. "Payment:", "Bill to:", "Ship to:") is followed by multiple lines forming a name + address block, extract as ONE string joined with ", ". Do NOT split into separate fields. Example: {"Payment": "Mr. John Doe, Green Street 15, 1234 Vermut, New Caledonia"}
14. NO FORCED PAIRING: Only treat two pieces of text as key:value if one is genuinely a label/heading for the other. If the document is just parallel lines or columns of independent text (e.g. handwriting practice, a passage, unrelated sentences) with no real labels, do NOT invent a pairing and NEVER put the same text as both the key and the value. Extract each line as its own entry in a string array or as "line_1", "line_2", ... keys instead.
15. MULTI-COLUMN LAYOUTS: If the image has two or more side-by-side columns of independent text (not a label+value table), read each column fully in its own top-to-bottom order and do NOT cross-match lines from different columns unless they are genuinely a label and its value.

EXTRACTION APPROACH ??? go through the document image top to bottom:
- For each element: "Is this a label with a value?" ??? single key-value field.
- Column headers + rows below ??? TABLE ??? JSON array of objects.
- List of items, no columns ??? string array.
- Parallel/unrelated columns with no labels ??? do NOT pair them; see rules 14-15.
- Extract each field individually as encountered. Do NOT skip any.

EXAMPLES:
"Invoice no. DVT-AX-345678 / Payment date: 03/12/2006"
??? {"Invoice no.": "DVT-AX-345678", "Payment date": "03/12/2006"}
"??????: 700.00 / ??????: 6% / ???????????????: ????????????????????????????????????"
??? {"unit price": "700.00", "tax rate": "6%", "buyer name": "Hongbai Trading (Shanghai) Co., Ltd."}

NOW extract {{EXTRACTION_SCOPE}} from the image above:"""


# Single-page merged OCR+fields prompt -- collapses what used to be 2 full
# Vision passes (classify+OCR, then a separate extract_fields_vision() call)
# into 1 near-instant classify call (see classify_and_extract_combined_vision)
# plus 1 full pass that does OCR and field extraction together, cutting
# ~20-40s off single-page scanned-document processing. TEXT is requested
# BEFORE FIELDS on purpose: by the time the model generates the FIELDS JSON
# it has already transcribed the page in this same response, so the LANGUAGE
# RULE below asks it to self-determine language from what it just wrote,
# instead of the caller pre-computing source_lang from raw_text that doesn't
# exist yet at prompt-build time.
DEFAULT_COMBINED_VISION_PROMPT = """You are a document OCR and field extractor. This page has been classified as: {{DOCUMENT_TYPE}}.

Do TWO things, in order:

STEP 1 -- Transcribe ALL visible text exactly as it appears, line by line.
For tables: output a header row first, then each data row on its own line with columns separated by | (pipe). Transcribe ALL rows -- do NOT stop early or summarise.

STEP 2 -- Based on what you just transcribed, extract structured fields as JSON.

LANGUAGE RULE (HIGHEST PRIORITY, applies to STEP 2 only): Determine the document's language from the text you transcribed in STEP 1.
- If the document is in English: ALL JSON keys (field names, table column headers, summary labels) MUST be in English -- translate every label to English.
- If the document is in a non-English language: keep ALL JSON keys in that original language, using the labels exactly as they appear on the document -- do NOT translate them.
- In BOTH cases: field VALUES always stay exactly as written in the original language -- never translate values.
{{TABLE_HINT_BLOCK}}{{STRUCTURE_HINT_BLOCK}}{{STRICT_RULE}}

CRITICAL field rules:
1. Output ONLY valid JSON for the FIELDS section below -- no markdown, no code blocks, no explanations.
2. GROUND TRUTH RULE: Every field MUST be visibly present in the document. Do NOT infer, guess, or hallucinate. If not visible, use null.
3. {{FIELD_SCOPE_RULE}}
4. Field values: copy the value EXACTLY as it appears in the document.
5. TABLE DETECTION RULE: if a table hint above applies, or the document clearly has a table, extract rows as a JSON array of objects -- NEVER as plain strings.
6. Single line JSON, no extra whitespace between tokens. Ensure every opening brace has a matching closing brace.

Reply in EXACTLY this format, nothing else:
TEXT:
<all transcribed text from STEP 1>
FIELDS:
<single-line JSON object from STEP 2, extracting {{EXTRACTION_SCOPE}}>

NOW process the document image above."""


# OCR-only variant used when the classified type has no field schema at all
# (matches extract_fields_vision()'s "zero template" behavior -- a freshly
# created custom type or the unclassified catch-all gets no free-form AI
# guess at fields). Still worth one Vision call instead of two: skips the
# second, separate extract_fields_vision() call entirely since its own
# zero-template check would immediately return {} anyway.
DEFAULT_COMBINED_TEXT_ONLY_PROMPT = """Transcribe ALL visible text from this document image exactly as it appears, line by line.
For tables: output a header row first, then each data row on its own line with columns separated by | (pipe).
Transcribe ALL rows -- do NOT stop early or summarise.
Output ONLY the transcribed text, nothing else."""


DEFAULT_EXTRACT_CUSTOM_FIELDS_PROMPT = """You are a document data extractor. Extract ONLY the fields listed below from this {{DOCUMENT_TYPE}}.

Fields to extract:
{{FIELD_DESCRIPTIONS}}{{TYPE_HINT_BLOCK}}{{EXTRA_HINT_BLOCK}}

Document text:
\"\"\"
{{TEXT_BLOCK}}
\"\"\"

Rules:
- Output ONLY a single JSON object, no markdown, no explanation
- Use EXACTLY these field names (keys): {{FIELD_NAMES_LIST}}
- Use null for any field not found in the document
- Copy values exactly as they appear
- For table fields with MANDATORY COLUMN RULE: reshape rows to use only the specified keys

Reply with ONLY: {{JSON_TEMPLATE}}"""


EXTRACT_PROMPTS = {
    "extract_structured_prompt":        ("Generic Extraction ??? Known Field List", DEFAULT_EXTRACT_STRUCTURED_PROMPT),
    "extract_generic_directive_prompt": ("Generic Extraction ??? With Structure Rules", DEFAULT_EXTRACT_GENERIC_DIRECTIVE_PROMPT),
    "extract_generic_open_prompt":      ("Generic Extraction ??? Open Fields", DEFAULT_EXTRACT_GENERIC_OPEN_PROMPT),
    "extract_vision_prompt":             ("Vision Field Extraction", DEFAULT_EXTRACT_VISION_PROMPT),
    "extract_custom_fields_prompt":      ("Custom Fields Extraction", DEFAULT_EXTRACT_CUSTOM_FIELDS_PROMPT),
}


def get_prompt_override(key: str) -> str:
    """
    Look up a user-edited prompt template from the Prompt Manager (Settings page).
    Returns the override content if one exists, else None.
    Runs inside an app context so it can access SQLAlchemy.
    """
    try:
        override_map = getattr(_prompt_override_ctx, "overrides", None)
        if override_map and key in override_map and override_map[key]:
            return override_map[key]
    except Exception:
        pass

    if key.startswith("structure_hint:"):
        try:
            from prompt_studio_service import get_active_prompt_version
            from app import app
            doc_type_name = key.split(":", 1)[1]
            print(f"[PromptStudio] Looking up active prompt for '{doc_type_name}'...")
            with app.app_context():
                version = get_active_prompt_version(doc_type_name)
                _pv_text = (version.prompt_text or "") if version else ""
                _pv_label = version.version_label if version else None
            if _pv_text:
                print(f"[PromptStudio] Found version {_pv_label} ({len(_pv_text)} chars)")
                return _pv_text
            else:
                print(f"[PromptStudio] No active version in DB for '{doc_type_name}'")
        except Exception as e:
            print(f"[PromptStudio] DB lookup failed for '{doc_type_name}': {e}")

    try:
        from app import app
        from document_model import PromptTemplate
        with app.app_context():
            pt = PromptTemplate.query.filter_by(key=key).first()
            if pt and pt.content:
                return pt.content
    except Exception as e:
        print(f"[PromptManager] Could not query DB for '{key}': {e}")
    return None


def set_prompt_overrides(overrides: dict | None):
    _prompt_override_ctx.overrides = overrides or {}


def clear_prompt_overrides():
    _prompt_override_ctx.overrides = {}


# ?????? Explicit field display order per document type ????????????????????????????????????????????????????????????????????????????????????
# Fields listed here are shown in this exact order in the UI.
# Any extracted fields NOT in this list are appended at the end.
FIELD_ORDER = {
    "bond_purchase_agreement": [
        "contract title", "effective date", "bond purchaser", "issuer",
        "resolution number", "bond series", "principal amount", "purchase price",
        "interest rate", "repayment period", "total interest paid", "maturity date",
        "source of repayment", "bond counsel", "financial advisor", "governing law",
        "parties", "key terms", "signatures",
    ],
    "invoice": [
        "invoice number", "date", "due date", "vendor name", "customer name",
        "line items", "subtotal", "tax", "total amount", "currency",
    ],
    "receipt": [
        "receipt number", "date", "merchant name", "items",
        "subtotal", "tax", "total amount", "payment method",
    ],
    "resume": [
        "full name", "email", "phone", "address", "linkedin", "summary",
        "work experience", "education", "skills", "certifications", "languages",
    ],
    "bank statement": [
        "account holder", "account number", "bank name", "statement period",
        "opening balance", "closing balance", "transactions",
    ],
    "contract": [
        "contract title", "parties", "effective date", "expiry date", "key terms", "signatures",
    ],
    "medical report": [
        "patient name", "date", "doctor name", "diagnosis", "medications", "recommendations",
    ],
    "id card": [
        "full name", "id number", "date of birth", "gender", "nationality", "address", "expiry date",
    ],
    "passport": [
        "full name", "passport number", "nationality", "date of birth",
        "issue date", "expiry date", "mrz line",
    ],
}


# ?????? Image helpers ???????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

def encode_image(image_path: str, max_size: int = 1024, quality: int = 90) -> tuple:
    with Image.open(image_path) as img:
        img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_size:
            scale = max_size / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        resized_w, resized_h = img.size  # ??? capture after resize
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return b64, "image/jpeg", resized_w, resized_h  # ??? return dims


_STREAM_NO_PROGRESS_TIMEOUT = 45.0  # seconds between chunks before aborting a stalled stream
_STREAM_MAX_CHUNKS = 50000          # sane upper bound independent of max_tokens -- guards a runaway/looping generation
# A real production case (dense multi-column newspaper scan, 2026-08-21)
# generated ~15,900 tokens at this hardware's ~30 tok/s throughput -- over 9
# minutes for one call, with no indication to the user that this was normal,
# expected cost for a content-heavy page rather than a hang. Once a stream
# crosses this many tokens (well past what any of the short documents tested
# this session needed -- 92-303 tokens), the live progress label gets a
# one-time qualitative suffix instead of just a slowly-creeping percentage,
# so a multi-minute wait reads as "large document, working" not "stuck".
_STREAM_LARGE_DOC_TOKEN_THRESHOLD = 2500


def _consume_stream(stream, max_tokens, label):
    """Iterate a Qwen streaming response, aborting early instead of buffering
    forever / blocking up to the full client timeout, if either: no new chunk
    arrives for _STREAM_NO_PROGRESS_TIMEOUT seconds, or the stream exceeds
    _STREAM_MAX_CHUNKS chunks (a pathological/looping generation that ignores
    max_tokens)."""
    chunks, n, last_bucket = [], 0, -1
    last_chunk_at = time.time()
    warned_large = False
    for chunk in stream:
        now = time.time()
        if now - last_chunk_at > _STREAM_NO_PROGRESS_TIMEOUT:
            try:
                stream.close()
            except Exception:
                pass
            raise QwenServiceUnavailableError(
                f"{label}: no new output for over {_STREAM_NO_PROGRESS_TIMEOUT:.0f}s mid-stream, aborting"
            )
        last_chunk_at = now
        delta = (chunk.choices[0].delta.content or "") if chunk.choices else ""
        chunks.append(delta)
        n += 1
        if n > _STREAM_MAX_CHUNKS:
            try:
                stream.close()
            except Exception:
                pass
            raise QwenServiceUnavailableError(
                f"{label}: exceeded {_STREAM_MAX_CHUNKS} streamed chunks without completing, aborting a runaway generation"
            )
        if not warned_large and n >= _STREAM_LARGE_DOC_TOKEN_THRESHOLD:
            warned_large = True
            _mark_stream_as_large_document()
        bucket = int(min(0.99, n / max_tokens) * 20)
        if bucket != last_bucket:
            last_bucket = bucket
            _fire_token_frac(min(0.99, n / max_tokens))
    _fire_token_frac(1.0)
    return "".join(chunks).strip(), n


def call_qwen_vision(b64: str, media_type: str, prompt: str, max_tokens: int = 100, extra_images: list = None, timeout: float = None) -> str:
    """Send image + text prompt to Qwen Vision.

    `extra_images`, if given, is a list of (b64, media_type) tuples for
    additional images included in the same message, in order, right after
    the primary one and before the text prompt (e.g. showing two consecutive
    pages side by side for a boundary-detection comparison).

    `timeout`, if given, overrides the default client timeout (600s) for this
    specific call. Useful for non-critical lazy operations like highlight blocks.
    """
    t0   = time.time()
    content = [{"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}}]
    for extra_b64, extra_mt in (extra_images or []):
        content.append({"type": "image_url", "image_url": {"url": f"data:{extra_mt};base64,{extra_b64}"}})
    content.append({"type": "text", "text": prompt})
    msgs = [{"role": "user", "content": content}]

    def _do_call():
        if getattr(_progress_ctx, 'slot', None) is None:
            response = client.chat.completions.create(
                model=MODEL, messages=msgs, temperature=0.0, max_tokens=max_tokens,
                timeout=timeout  # Pass through timeout (None uses client default)
            )
            print(f"[Timing] Vision call ({max_tokens} tok): {time.time()-t0:.1f}s")
            return response.choices[0].message.content.strip()

        stream = client.chat.completions.create(
            model=MODEL, messages=msgs, temperature=0.0, max_tokens=max_tokens, stream=True,
            timeout=timeout  # Pass through timeout (None uses client default)
        )
        result, n = _consume_stream(stream, max_tokens, "call_qwen_vision")
        print(f"[Timing] Vision stream ({n} tok): {time.time()-t0:.1f}s")
        return result

    return _call_qwen_with_retry(_do_call, label="call_qwen_vision")


def call_qwen_text(prompt: str, max_tokens: int = 1000) -> str:
    """Send text-only prompt to Qwen 2.5 (faster, no vision overhead)."""
    t0   = time.time()
    msgs = [{"role": "user", "content": prompt}]

    def _do_call():
        if getattr(_progress_ctx, 'slot', None) is None:
            response = client.chat.completions.create(
                model=TEXT_MODEL, messages=msgs, temperature=0.1, max_tokens=max_tokens,
            )
            print(f"[Timing] Text call ({max_tokens} tok): {time.time()-t0:.1f}s")
            return response.choices[0].message.content.strip()

        stream = client.chat.completions.create(
            model=TEXT_MODEL, messages=msgs, temperature=0.1, max_tokens=max_tokens, stream=True,
        )
        result, n = _consume_stream(stream, max_tokens, "call_qwen_text")
        print(f"[Timing] Text stream ({n} tok): {time.time()-t0:.1f}s")
        return result

    return _call_qwen_with_retry(_do_call, label="call_qwen_text")


def call_qwen_messages(messages: list, max_tokens: int = 1000, temperature: float = 0.1, model_name: str = None) -> str:
    """Send a chat-style message list to the active LLM backend."""
    def _do_call():
        response = client.chat.completions.create(
            model=model_name or TEXT_MODEL,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content.strip()

    return _call_qwen_with_retry(_do_call, label="call_qwen_messages")


# Letters used in Jawi (Malay written in Arabic script) that don't exist in
# standard Arabic: ca, nga, pa, nya, va, ga. Arabic-range codepoint detection
# alone can't tell Jawi from Arabic since Jawi reuses the same Unicode block.
_JAWI_ONLY_LETTERS = set('چڠڤڽۏݢ')

# Dictionary-based Jawi -> Rumi (Latin Malay) word converter, from the
# goodmami/rumi-jawi word list (~66k pairs). Used to deterministically
# romanize known words before asking the LLM to translate -- the LLM alone
# frequently hallucinates on unfamiliar Jawi spellings.
_RUMI_JAWI_TSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "rumi_jawi.tsv")
_JAWI_TO_RUMI_DICT = None
_JAWI_HARAKAT_RE = re.compile(r'[ً-ْٰ]')  # optional Arabic diacritics
_JAWI_WORD_RE = re.compile(r'[؀-ۿݐ-ݿ]+')


def _normalize_jawi_letterforms(s: str) -> str:
    """Fold common letter-form variants seen in Jawi/OCR text onto the
    dictionary's canonical forms (kaf vs. keheh, yeh vs. farsi yeh, etc.)."""
    return (s.replace('ك', 'ک')   # Arabic kaf -> Jawi/Farsi keheh
             .replace('ي', 'ی')   # Arabic yeh -> Farsi yeh
             .replace('ى', 'ی'))  # alef maksura -> Farsi yeh


def _load_jawi_to_rumi_dict() -> dict:
    global _JAWI_TO_RUMI_DICT
    if _JAWI_TO_RUMI_DICT is not None:
        return _JAWI_TO_RUMI_DICT
    mapping = {}
    try:
        with open(_RUMI_JAWI_TSV_PATH, encoding="utf-8") as f:
            next(f, None)  # header row: rumi\tjawi
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 2:
                    continue
                rumi, jawi = parts
                key = _normalize_jawi_letterforms(_JAWI_HARAKAT_RE.sub("", jawi).strip())
                # First-seen Rumi spelling wins when a Jawi form has multiple Rumi variants
                mapping.setdefault(key, rumi)
        print(f"[Jawi] Loaded {len(mapping)} Jawi->Rumi dictionary entries")
    except FileNotFoundError:
        print("[Jawi] data/rumi_jawi.tsv not found, skipping dictionary-based conversion")
    _JAWI_TO_RUMI_DICT = mapping
    return mapping


def jawi_to_rumi(text: str) -> str:
    """
    Best-effort word-level Jawi -> Rumi (Latin Malay) conversion using the
    goodmami/rumi-jawi dictionary. Words not found in the dictionary are left
    in Jawi script as-is, so the result is only ever a partial romanization.
    """
    mapping = _load_jawi_to_rumi_dict()
    if not mapping or not text:
        return text

    def _replace(m):
        word = m.group(0)
        key = _normalize_jawi_letterforms(_JAWI_HARAKAT_RE.sub("", word))
        return mapping.get(key, word)

    return _JAWI_WORD_RE.sub(_replace, text)


def detect_language(text: str) -> str:
    """
    Detect the dominant script in text.
    Returns a lowercase language name string or 'english' as fallback.
    """
    if not text or len(text.strip()) < 10:
        return "english"
    sample = text[:500]
    non_ascii = [c for c in sample if c.strip() and ord(c) > 127]
    total_chars = max(len([c for c in sample if c.strip()]), 1)

    def ratio(chars):
        return len(chars) / total_chars

    thai_chars    = [c for c in non_ascii if '\u0e00' <= c <= '\u0e7f']
    chinese_chars = [c for c in non_ascii if '\u4e00' <= c <= '\u9fff']
    korean_chars  = [c for c in non_ascii if '\uac00' <= c <= '\ud7af' or '\u1100' <= c <= '\u11ff']
    japanese_chars= [c for c in non_ascii if '\u3040' <= c <= '\u309f' or '\u30a0' <= c <= '\u30ff']
    arabic_chars  = [c for c in non_ascii if '\u0600' <= c <= '\u06ff' or '\u0750' <= c <= '\u077f']
    cyrillic_chars= [c for c in non_ascii if '\u0400' <= c <= '\u04ff']

    candidates = [
        ("thai",     ratio(thai_chars)),
        ("chinese",  ratio(chinese_chars)),
        ("korean",   ratio(korean_chars)),
        ("japanese", ratio(japanese_chars)),
        ("arabic",   ratio(arabic_chars)),
        ("russian",  ratio(cyrillic_chars)),
    ]
    best_lang, best_ratio = max(candidates, key=lambda x: x[1])
    if best_ratio > 0.05:
        if best_lang == "arabic" and any(c in _JAWI_ONLY_LETTERS for c in sample):
            # Jawi (Arabic script adapted for Malay) shares the Arabic Unicode
            # block, so it looks like Arabic by codepoint alone. These letters
            # (ca/nga/pa/nya/va/ga) don't exist in standard Arabic, so their
            # presence is a reliable signal the text is actually Jawi.
            return "jawi"
        return best_lang
    return "english"


def translate_to_english(text: str, source_language: str = "auto") -> str:
    """
    Translate raw text to English using Qwen before field extraction.
    Preserves numbers, dates, amounts, and proper nouns in transliterated form.
    Only called when non-English content is detected.
    """
    print(f"[Translation] Translating {source_language} text to English ({len(text)} chars)...")
    # Truncate to avoid context overflow ??? translation only needs the content, not entire document
    text_block = text[:10000] if len(text) > 10000 else text
    prompt = f"""Translate the following document text to English.
Rules:
- Translate ALL text to English
- Keep numbers, amounts, and dates in their original format (e.g. "??700.00", "2022???01???12???" ??? "January 12, 2022")
- For company names: transliterate AND provide English meaning if known (e.g. "????????????????????????????????????" ??? "Hongbai Trading (Shanghai) Co., Ltd.")
- Keep document structure, labels, and layout intact
- Do NOT add explanations ??? output only the translated text

TEXT TO TRANSLATE:
{text_block}

ENGLISH TRANSLATION:"""
    translated = call_qwen_text(prompt, max_tokens=2000)
    print(f"[Translation] Done: {len(translated)} chars")
    return translated


def safe_parse_json(text: str) -> dict:
    """Safely extract and parse JSON from model response, handling markdown code blocks and incomplete JSON."""
    # Strip markdown code block markers
    text = text.strip()
    # Remove ```json or ``` at start
    if text.startswith('```'):
        text = text[3:].lstrip()  # Remove ``` and whitespace
        if text.startswith('json'):
            text = text[4:].lstrip()  # Remove 'json' and whitespace
    # Remove ``` at end
    if text.endswith('```'):
        text = text[:-3].rstrip()  # Remove ``` from end
    text = text.strip()

    try:
        parsed = json.loads(text)
        # We always expect a dict; if the model returned a bare array, reject it
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Fallback 1: Find first { and use brace counting to find matching }
    start_idx = text.find('{')
    if start_idx != -1:
        brace_count = 0
        last_valid_end = -1

        for i in range(start_idx, len(text)):
            if text[i] == '{':
                brace_count += 1
            elif text[i] == '}':
                brace_count -= 1
                if brace_count == 0:
                    last_valid_end = i
                    json_str = text[start_idx:i + 1]
                    try:
                        return json.loads(json_str)
                    except json.JSONDecodeError:
                        continue  # Try next closing brace if this one fails

        # Fallback 2: If we found a partial valid object, try to close it
        if last_valid_end == -1 and brace_count > 0:
            # JSON was cut off, try to close unclosed strings and braces
            json_str = text[start_idx:]

            # Count quotes to see if a string is left open
            in_string = False
            escape_next = False
            for char in json_str:
                if escape_next:
                    escape_next = False
                    continue
                if char == '\\':
                    escape_next = True
                    continue
                if char == '"':
                    in_string = not in_string

            # Close unclosed string if needed
            if in_string:
                json_str = json_str.rstrip(',').rstrip() + '"'

            # Remove trailing incomplete key-value pairs (like: , "key": or , "key)
            json_str = re.sub(r',\s*"[^"]*$', '', json_str)  # Remove trailing incomplete key
            json_str = json_str.rstrip(',').rstrip()  # Remove trailing comma/whitespace

            # Close unclosed braces
            json_str += '}' * brace_count

            try:
                return json.loads(json_str)
            except json.JSONDecodeError:
                pass

    return {}


def clean_qwen_output(raw: str) -> str:
    """Strip bounding box tokens Qwen Vision sometimes returns."""
    raw = re.sub(r"<[|]box_start[|]>.*?<[|]box_end[|]>", "", raw, flags=re.DOTALL)
    raw = re.sub(r"<[|][^|]*[|]>", "", raw)
    return raw.strip()


# ?????? Step 1a: Quick classify from thumbnail ????????????????????????????????????????????????????????????????????????????????????????????????????????????

def quick_classify_from_image(image_path: str) -> str:
    """
    Fast Qwen Vision call on 512px thumbnail to detect document type
    BEFORE full OCR ??? so we can pass a targeted prompt for better results.
    Prefers known types defined in Document Type Manager.
    """
    print("[Pipeline] Step 1a: Quick classify from thumbnail...")
    known_types = get_all_active_doc_type_names()
    known_str = ", ".join(known_types)
    b64, media_type, _, _ = encode_image(image_path, max_size=512)
    prompt = (
        f"Look at this document image. What specific type of document is it?\n"
        f"Known types (prefer these if they match): {known_str}\n"
        f"If none match, provide a precise snake_case type name (e.g., 'employment_contract').\n"
        f"IMPORTANT: Reply in English only. Reply with ONLY the document type in snake_case, nothing else."
    )
    result = call_qwen_vision(b64, media_type, prompt, max_tokens=15)
    if not result.strip():
        print("[Pipeline] Quick classify: model returned empty response, falling back to 'other'")
        return "other"
    result = result.lower().strip().split()[0]

    print(f"[Pipeline] Quick classify: {result}")
    return result


# ?????? Steps 1b-1e: Full OCR with split + merge ??????????????????????????????????????????????????????????????????????????????????????????????????????

def extract_text_from_image(image_path: str, doc_type: str = "other") -> str:
    """
    Single-pass OCR: send the full image to Qwen Vision with a targeted prompt.
    doc_type used to build a targeted OCR prompt per document type.
    """
    default_hint = OCR_HINTS.get(doc_type, OCR_HINTS["other"])
    hint = get_prompt_override(f"ocr_hint:{doc_type}") or default_hint
    mt   = "image/jpeg"

    ocr_prompt = (
        f"{hint}\n"
        "Transcribe ALL text exactly as shown. Plain text, line by line. "
        "No bounding boxes, no JSON. Preserve numbers, dates, names exactly."
    )

    print("[Pipeline] Step 1b: OCR full image (1024px, single pass)...")
    b64, _, _, _ = encode_image(image_path, max_size=1024)
    raw_text = clean_qwen_output(call_qwen_vision(b64, mt, ocr_prompt, max_tokens=1500))

    print(f"[Pipeline] OCR complete: {len(raw_text)} characters")
    return raw_text.strip()


# ?????? PDF helpers ?????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

def is_scanned_pdf(pdf_path: str) -> bool:
    """Check if a PDF is scanned (no meaningful text layer)."""
    import fitz
    with fitz.open(pdf_path) as doc:
        total_text = ""
        for page in doc:
            total_text += page.get_text()
            if len(total_text.strip()) >= 50:
                return False
    return True


def pdf_pages_to_b64(pdf_path: str, dpi: int = 150, max_dim: int = 1240) -> list:
    """
    Render every page of a PDF to a base64 JPEG image in memory using PyMuPDF.
    Returns list of (b64_string, media_type) tuples.
    - Digital PDF:  150 DPI ??? ~1240px (minimal resize, fast)
    - Scanned PDF:  300 DPI ??? ~2480px ??? LANCZOS downsample to 1240px (sharper text)
    """
    import fitz

    zoom = dpi / 72.0
    print(f"[Pipeline] PDF ??? PyMuPDF rendering at {dpi} DPI (zoom {zoom:.2f}), max {max_dim}px...")
    pages = []
    with fitz.open(pdf_path) as doc:
        for page_num, page in enumerate(doc):
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            w, h = img.size
            if max(w, h) > max_dim:
                scale = max_dim / max(w, h)
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
            pages.append((b64, "image/jpeg", img.size[0], img.size[1]))
            print(f"[Pipeline] Rendered page {page_num + 1} ??? {img.size[0]}x{img.size[1]}px")
    return pages


# ?????? Step 2: Classify from text ?????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

def classify_document(raw_text: str, hint_type: str = None) -> dict:
    """
    Classify document type using Qwen LLM (text only).
    Prefers known types from the Document Type Manager DB.
    hint_type from quick_classify is passed as a soft hint.
    """
    known_types = get_all_active_doc_type_names()
    known_str   = ", ".join(f"'{t}'" for t in known_types)
    hint_str    = f"\nNote: visual scan suggests this may be a '{hint_type}'." if hint_type else ""

    template = get_prompt_override("classify_prompt") or DEFAULT_CLASSIFY_PROMPT
    prompt = (
        template
        .replace("{{KNOWN_TYPES}}", known_str)
        .replace("{{HINT}}", hint_str)
        .replace("{{DOCUMENT_TEXT}}", raw_text[:500])
    )

    result = safe_parse_json(call_qwen_text(prompt, max_tokens=50))
    doc_type = result.get("document_type", hint_type or "unknown")
    confidence = normalize_confidence(result.get("confidence"), source="text")

    # Normalize to snake_case
    doc_type = doc_type.lower().strip().replace(" ", "_").replace("-", "_")

    print(f"[Pipeline] Classified as: {doc_type} (confidence: {format_confidence(confidence)})")

    return {
        "document_type": doc_type,
        "confidence":    confidence,
    }


# ?????? Step 4: Extract fields ????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

def extract_fields_structured(raw_text: str, document_type: str, similar_docs: list = None) -> dict:
    """
    Extract structured fields for KNOWN document types with predefined FIELD_MAP.
    With RAG examples injected if available.
    Uses smart truncation to capture beginning + end of document.
    """
    fields = FIELD_MAP.get(document_type)
    if fields is None:
        print(f"[Pipeline] '{document_type}' not in FIELD_MAP, falling back to generic extraction")
        return extract_fields_generic(raw_text, document_type)
    field_list = [f.strip() for f in fields.split(",")]

    examples_block = ""
    if similar_docs:
        examples = []
        for i, doc in enumerate(similar_docs[:2], 1):
            if not doc.get("fields"):
                continue
            ex_fields = json.dumps(doc["fields"], ensure_ascii=False, separators=(',', ':'))
            examples.append(f"Ref {i}: {ex_fields}")
        if examples:
            examples_block = "\nReference extractions:\n" + "\n".join(examples) + "\n"

    # Smart truncation: keep beginning + end to capture all sections
    if len(raw_text) > 8000:
        text_block = raw_text[:5000] + "\n...\n" + raw_text[-3000:]
    else:
        text_block = raw_text

    # Build explicit JSON template so model knows exactly what to fill
    json_template = ", ".join([f'"{f}": "..."' for f in field_list])

    # Invoice-specific extra instructions to capture all rows across section headers
    invoice_hint = ""
    if document_type in ("invoice", "receipt", "purchase_order"):
        invoice_hint = """
IMPORTANT for line_items:
- Capture EVERY product/service row from ALL sections (e.g. "Work", "Exterior parts", "Labour", "Parts")
- Include section header as a "section" or "category" key on the row if present
- Each row must have: reference/code (if present), description, quantity, unit_price, amount
- Do NOT skip rows that have multi-line descriptions ??? join them into one description string
- Subtotal = sum of all line item amounts BEFORE any discounts
- List discounts separately with negative values
- total_amount = the final payable amount shown on the document (after all discounts)
"""

    template = get_prompt_override("extract_structured_prompt") or DEFAULT_EXTRACT_STRUCTURED_PROMPT
    prompt = (
        template
        .replace("{{DOCUMENT_TYPE}}", document_type)
        .replace("{{FIELDS}}", fields)
        .replace("{{EXAMPLES_BLOCK}}", examples_block)
        .replace("{{INVOICE_HINT}}", invoice_hint)
        .replace("{{TEXT_BLOCK}}", text_block)
        .replace("{{JSON_TEMPLATE}}", "{" + json_template + "}")
    )

    result = safe_parse_json(call_qwen_text(prompt, max_tokens=1500))

    # Ensure all expected fields exist in output
    if result:
        for field in field_list:
            if field not in result:
                result[field] = None

    return result if result else {f: None for f in field_list}


# ?????? Self-learning structure management ????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

# Both save_learned_structure() and learn_type_correction() do a
# read-modify-write on this same file from background threads (one per
# uploaded document, see document_routes.py) - this lock serializes those,
# and _atomic_write_learned_structures does a temp-file + os.replace so a
# reader never observes a half-written file either.
_learned_structures_lock = threading.Lock()


def _atomic_write_learned_structures(structures: dict) -> None:
    dir_name = os.path.dirname(LEARNED_STRUCTURES_PATH) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".learned_structures_", suffix=".tmp", dir=dir_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(structures, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, LEARNED_STRUCTURES_PATH)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


def load_learned_structures() -> dict:
    """Load the saved field structures for known document types."""
    if os.path.exists(LEARNED_STRUCTURES_PATH):
        try:
            with open(LEARNED_STRUCTURES_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[Learning] Failed to load structures: {e}")
            return {}
    return {}


def save_learned_structure(document_type: str, field_keys: list) -> None:
    """
    Save extracted field keys as learned structure for a document type.
    Each extraction is INDEPENDENT - only the current document's fields are saved,
    without merging/accumulating fields from previous documents.
    """
    if not document_type or document_type in ("unknown", "other"):
        return  # Don't save generic types

    with _learned_structures_lock:
        structures = load_learned_structures()

        # Check if this is new before overwriting
        is_new = document_type not in structures

        # Always overwrite with current extraction (don't merge)
        # This ensures no leftover fields from previous documents
        structures[document_type] = field_keys

        try:
            _atomic_write_learned_structures(structures)
        except Exception as e:
            print(f"[Learning] ??? Failed to save structure: {e}")
            return

    if is_new:
        print(f"[Learning] ??? New structure learned for '{document_type}': {field_keys}")
    else:
        print(f"[Learning] ??? Structure updated for '{document_type}': {field_keys}")


def _is_pipe_table_line(line: str) -> bool:
    """A table row has 2+ pipe separators and isn't just a URL or phone number."""
    stripped = line.strip()
    if not stripped or stripped.count('|') < 2:
        return False
    # Exclude separator rows (---|---|---)
    if re.match(r'^[\s\|\-:]+$', stripped):
        return False
    # Exclude lines that look like URLs or contact info (short segments)
    segments = [s.strip() for s in stripped.split('|')]
    # Must have at least 3 non-empty segments to be a table row
    return sum(1 for s in segments if s) >= 3


def parse_markdown_tables(raw_text: str) -> tuple:
    """
    Parse pipe-separated tables from ODL-extracted text.
    Handles both:
    - Standard markdown: | col1 | col2 | col3 |
    - ODL inline pipes:  col1 | col2 | col3
    Returns (tables_dict, cleaned_text).
    """
    lines = raw_text.split('\n')
    tables = {}
    cleaned_lines = []
    i = 0
    seen_headers = set()  # Avoid duplicate tables with same headers

    while i < len(lines):
        line = lines[i]

        if _is_pipe_table_line(line):
            # Look back for a section heading (skip [PAGE N] markers and placeholders)
            section_name = None
            for j in range(len(cleaned_lines) - 1, max(len(cleaned_lines) - 5, -1), -1):
                prev = cleaned_lines[j].strip()
                if not prev:
                    continue
                if _is_pipe_table_line(prev) or prev.startswith('[TABLE:') or re.match(r'^\[PAGE \d+\]', prev) or prev.startswith('[DUPLICATE'):
                    continue
                # Strip markdown heading markers and leading bullet/icon chars (I, ???, ???, ???)
                candidate = re.sub(r'^#+\s*', '', prev)
                candidate = re.sub(r'^[I????????????\-]\s+', '', candidate).strip()
                # Reject candidates that look like data values (contain pipes, start with $, are pure numbers)
                if '|' in candidate:
                    continue
                if re.match(r'^[\$\d]', candidate):
                    continue
                if len(candidate) > 2:
                    section_name = candidate
                    break

            # If still no name, look ahead in remaining lines for a section heading
            if not section_name:
                for k in range(i, min(i + 20, len(lines))):
                    ahead = lines[k].strip()
                    if ahead and not _is_pipe_table_line(ahead) and re.search(r'[A-Z]{3,}', ahead):
                        candidate = re.sub(r'^[I????????????\-#]\s*', '', ahead).strip()
                        if len(candidate) > 2:
                            section_name = candidate
                            break

            if not section_name:
                section_name = f"Table {len(tables) + 1}"

            # Collect consecutive pipe-table lines
            table_lines = []
            while i < len(lines) and (_is_pipe_table_line(lines[i]) or re.match(r'^[\s\|\-:]+$', lines[i].strip())):
                table_lines.append(lines[i])
                i += 1

            # Parse headers from first line
            header_row = table_lines[0]
            headers = [c.strip() for c in header_row.split('|') if c.strip()]
            # Build a fingerprint from headers + first data row to detect true duplicates
            first_data_line = next((tl for tl in table_lines[1:] if not re.match(r'^[\s\|\-:]+$', tl.strip())), '')
            first_cells = '|'.join(c.strip() for c in first_data_line.split('|') if c.strip())
            table_fingerprint = '|'.join(headers) + '::' + first_cells

            if table_fingerprint in seen_headers:
                cleaned_lines.append(f"[DUPLICATE TABLE SKIPPED: {section_name}]")
                continue
            seen_headers.add(table_fingerprint)

            rows = []
            for tl in table_lines[1:]:
                if re.match(r'^[\s\|\-:]+$', tl.strip()):
                    continue  # Skip separator rows
                cells = [c.strip() for c in tl.split('|')]
                # Remove empty boundary cells from leading/trailing pipes
                if cells and cells[0] == '':
                    cells = cells[1:]
                if cells and cells[-1] == '':
                    cells = cells[:-1]
                if not any(cells):
                    continue
                row_obj = {}
                for hi, h in enumerate(headers):
                    val = cells[hi] if hi < len(cells) else None
                    row_obj[h] = val if val else None
                # Skip rows where all values are None (blank rows)
                if any(v for v in row_obj.values()):
                    rows.append(row_obj)

            if rows:
                # If section name already used, append index
                key = section_name
                if key in tables:
                    key = f"{section_name} {len(tables) + 1}"
                tables[key] = rows
                cleaned_lines.append(f"[TABLE: {key} ??? {len(rows)} rows, extracted separately]")
            continue

        cleaned_lines.append(line)
        i += 1

    return tables, '\n'.join(cleaned_lines)


def detect_tables_in_text(raw_text: str) -> str:
    """
    Detect table patterns in extracted text and return hints for the AI.
    For digital PDFs with PyMuPDF extraction, tables lose formatting but patterns remain.
    Uses pattern matching to detect tables even when structure is lost.
    """
    hints = []
    lines = raw_text.split('\n')
    import re

    # ????????? Pattern 0: Pipe table detection (ODL output, both formats) ???????????????
    md_table_lines = [l for l in lines if _is_pipe_table_line(l)]
    if len(md_table_lines) >= 3:
        header_line = md_table_lines[0]
        cols = [c.strip() for c in header_line.split('|') if c.strip()]
        col_example = ', '.join(f'"{c}": ...' for c in cols[:4])
        hints.append(
            f"MARKDOWN TABLE DETECTED ??? {len(md_table_lines)} rows with columns: {', '.join(cols)}. "
            f"You MUST extract as a JSON array of objects: [{{{col_example}}}]. "
            "Do NOT extract rows as plain strings."
        )

    # ????????? Pattern 1: Header-based detection ?????????????????????????????????????????????????????????????????????????????????????????????
    common_headers = ['Description', 'Amount', 'Date', 'Usage', 'Rate', 'Reading', 'Charge',
                      'Total', 'Quantity', 'Price', 'Previous', 'Current', 'Balance',
                      'Item Code', 'Item', 'Code', 'Qty', 'Unit', 'U/Price', 'Unit Price',
                      'Invoice', 'Receipt', 'Transaction', 'Subtotal', 'Tax', 'Discount']

    for i, line in enumerate(lines[:150]):
        header_count = sum(1 for h in common_headers if h.lower() in line.lower())
        if header_count >= 2:
            table_name = None
            if i > 0:
                prev_line = lines[i-1].strip().upper()
                if any(keyword in prev_line for keyword in ['ITEM', 'USAGE', 'CHARGES', 'ITEMS', 'TRANSACTIONS', 'LINE', 'DETAIL']):
                    table_name = prev_line
            cols = [h for h in common_headers if h.lower() in line.lower()]
            col_example = ', '.join(f'"{c}": ...' for c in cols[:3])
            hint = (
                f"TABLE DETECTED ??? '{table_name or 'Line Items'}' with columns: {', '.join(cols)}. "
                f"You MUST extract rows as a JSON array of objects: [{{{col_example}}}]. "
                "Do NOT extract table rows as plain strings."
            )
            hints.append(hint)

    # ????????? Pattern 2: Item code + price pattern detection ?????????????????????????????????????????????????????????
    # Look for lines with item codes (alphanumeric) followed by descriptions and numbers
    # Pattern: "ABC123" or "LE1D09N7" followed by description and numbers
    item_code_pattern = r'^[A-Z0-9\-]{4,}[\s\t]+[A-Z].*[\d,\.]+\s*$'
    item_code_lines = [line for line in lines if re.match(item_code_pattern, line.strip())]

    if len(item_code_lines) >= 3:  # Multiple item lines found
        hints.append(
            f"TABLE DETECTED ??? {len(item_code_lines)} item code rows found. "
            'You MUST extract as a JSON array: [{"code": ..., "description": ..., "qty": ..., "unit_price": ..., "amount": ...}]. '
            "Do NOT flatten to plain text."
        )

    # ????????? Pattern 3: Repeated price/quantity lines ?????????????????????????????????????????????????????????????????????????????????
    # Lines with numbers that look like prices or quantities
    number_pattern = r'[\d,]+\.?\d*'
    lines_with_multiple_numbers = [line for line in lines
                                   if len(re.findall(number_pattern, line)) >= 2 and len(line.strip()) > 10]

    if len(lines_with_multiple_numbers) >= 5:
        hints.append(
            f"TABLE DETECTED ??? {len(lines_with_multiple_numbers)} rows with multiple numeric values. "
            'You MUST extract as a JSON array of objects using the column headers as keys: [{"description": ..., "amount": ...}]. '
            "Do NOT flatten to plain text."
        )

    # ????????? Pattern 4: Keyword-based table detection ??????????????????????????????????????????????????????????????????????????????
    raw_text_lower = raw_text.lower()

    # Check for invoice-specific keywords
    if any(kw in raw_text_lower for kw in ['item code', 'item no', 'item number']):
        hints.append(
            'TABLE DETECTED ??? Invoice line items. You MUST extract as: [{"item_code": ..., "description": ..., "qty": ..., "unit_price": ..., "amount": ...}].'
        )
    elif any(kw in raw_text_lower for kw in ['electricity usage', 'water usage', 'usage charges']):
        hints.append(
            'TABLE DETECTED ??? Utility usage table. You MUST extract as: [{"description": ..., "previous": ..., "current": ..., "usage": ..., "rate": ..., "amount": ...}]. Copy the "rate" value EXACTLY as it appears in the document (e.g. "$0.138/kWh", "$0.015/gal") ??? do NOT round or strip units.'
        )
    elif any(kw in raw_text_lower for kw in ['line item', 'line items', 'product', 'service']):
        hints.append(
            'TABLE DETECTED ??? Line items table. You MUST extract as: [{"description": ..., "quantity": ..., "unit_price": ..., "amount": ...}].'
        )

    # ????????? Pattern 5: Check for any table indicators ????????????????????????????????????????????????????????????????????????
    if not hints:
        # No tables detected yet, try broader patterns
        if re.search(r'\d{2,}\s*[,\.]\s*\d{2}', raw_text):  # Numbers like "123.45" or "1,234.56"
            if re.search(r'[A-Z]{3,}.*\d', raw_text):  # Code-like patterns
                hints.append("POSSIBLE TABLE: Detected numeric and code-like patterns - may contain tabular data")

    # ????????? Pattern 6: Bullet point list detection ?????????????????????????????????????????????????????????????????????????????????
    bullet_lines = [line for line in lines if re.match(r'^\s*[???\-\*\u2022\u2023\u25E6\u2043]\s+\S', line)]
    if len(bullet_lines) >= 3:
        hints.append(
            f"BULLET LIST DETECTED: {len(bullet_lines)} bullet point lines found. "
            "You MUST extract each bullet-point group as a JSON array under its nearest heading label. "
            "If bullet items have sub-fields (e.g. 'AWS Certified... (2023)', 'Engineer at Google 2020???2023'), "
            "extract each as an object: [{\"name\": ..., \"date\": ...}]. "
            "If bullets are plain text items, extract as a string array: [\"item1\", \"item2\"]. "
            "Do NOT join bullets into one string."
        )

    return '\n'.join(hints) if hints else ""


def extract_fields_generic(raw_text: str, document_type: str = None, source_lang: str = "english") -> dict:
    """
    Flexible field extraction for UNKNOWN document types.
    Asks AI to intelligently extract ALL meaningful key-value pairs.
    Returns whatever fields the AI identifies, without rigid structure.
    This allows the system to handle any future document type without code changes.

    If document_type is provided and has a learned structure, uses it as a hint.
    Automatically saves new field structures for future reuse.
    """
    # Truncation: keep as much as possible, always preserve the full document structure
    # ?????? Step 0: Parse markdown tables in Python (reliable, no LLM needed) ??????????????????
    parsed_tables, raw_text_no_tables = parse_markdown_tables(raw_text)
    if parsed_tables:
        print(f"[Generic Extraction] ??? Python-parsed {len(parsed_tables)} markdown table(s): {list(parsed_tables.keys())}")

    # Use cleaned text (tables replaced with placeholders) for Qwen scalar extraction
    source_text = raw_text_no_tables if parsed_tables else raw_text

    # Truncation on the cleaned text
    MAX_CHARS = 40000
    if len(source_text) > MAX_CHARS:
        text_block = source_text[:28000] + "\n[...]\n" + source_text[-10000:]
    else:
        text_block = source_text

    print(f"[Generic Extraction] Text block size: {len(text_block)} / {len(raw_text)} chars")
    if "electricity" in text_block.lower() or "water" in text_block.lower():
        print(f"[Generic Extraction] ??? Tables detected in text block")

    # Load learned structure for reference only (do NOT use as extraction hint)
    # This ensures each document extraction is independent
    learned_hint = ""
    if document_type and document_type not in ("unknown", "other"):
        structures = load_learned_structures()
        if document_type in structures:
            known_fields = structures[document_type]
            print(f"[Learning] Reference structure found for '{document_type}' ({len(known_fields)} fields)")

    # Detect table patterns in the text
    table_hints = detect_tables_in_text(raw_text)
    table_hint_block = f"\nTABLE DETECTION:\n{table_hints}" if table_hints else ""

    # Document-type specific structure hint ??? applied for ALL languages.
    # The hint controls HOW the table/fields are structured (language-agnostic).
    # The lang_rule below controls WHAT language the keys are in.
    structure_hint = ""
    if document_type and document_type in DOCUMENT_STRUCTURE_HINTS:
        hint_text = get_prompt_override(f"structure_hint:{document_type}") or DOCUMENT_STRUCTURE_HINTS[document_type]
        structure_hint = f"\nDOCUMENT STRUCTURE RULES:\n{hint_text}\n"
        if source_lang != "english":
            structure_hint += (
                f"\nNOTE for {source_lang} document: "
                "For table columns, use the column headers EXACTLY as they appear in the document "
                "(preserve the original language of the headers ??? do NOT translate them). "
                "For scalar field names, use the original document label as the key."
            )

    is_non_english = source_lang != "english"
    key_lang_rule_directive = (
        "3. Use the field names EXACTLY as defined in DOCUMENT STRUCTURE RULES above"
        if is_non_english else
        "3. ALL field names (keys) MUST be in English regardless of the document language"
    )
    key_lang_rule_generic = (
        f"2. Use the original document labels as field name keys (keep in {source_lang})"
        if is_non_english else
        '2. ALL field names (keys) MUST be in English regardless of the document language (e.g. "unit price" not "??????", "tax rate" not "??????")'
    )

    if structure_hint:
        # Directive mode: strict extraction ??? ONLY the fields defined in the structure hint
        template = get_prompt_override("extract_generic_directive_prompt") or DEFAULT_EXTRACT_GENERIC_DIRECTIVE_PROMPT
        prompt = (
            template
            .replace("{{TEXT_BLOCK}}", text_block)
            .replace("{{LEARNED_HINT}}", learned_hint)
            .replace("{{STRUCTURE_HINT}}", structure_hint)
            .replace("{{TABLE_HINT_BLOCK}}", table_hint_block)
        )
    else:
        example_block = (
            '\nEXAMPLE (Chinese invoice):\n"??????: 700.00\\n??????: 6%"\n??? {{"??????": "700.00", "??????": "6%"}}\n'
            if is_non_english else
            '\nEXAMPLE (Chinese invoice):\n"??????: 700.00\\n??????: 6%"\n??? {{"unit price": "700.00", "tax rate": "6%"}}\n'
        )
        template = get_prompt_override("extract_generic_open_prompt") or DEFAULT_EXTRACT_GENERIC_OPEN_PROMPT
        prompt = (
            template
            .replace("{{TEXT_BLOCK}}", text_block)
            .replace("{{LEARNED_HINT}}", learned_hint)
            .replace("{{TABLE_HINT_BLOCK}}", table_hint_block)
            .replace("{{KEY_LANG_RULE}}", key_lang_rule_generic)
            .replace("{{EXAMPLE_BLOCK}}", example_block)
        )

    response = call_qwen_text(prompt, max_tokens=16000)  # Increased for complex/long documents
    print(f"[Generic Extraction] Response length: {len(response)} chars")
    print(f"[Generic Extraction] Raw response: {response[:300]}...")

    result = safe_parse_json(response)

    # If parsing failed and response looks truncated, try to recover
    if not result and response.strip().endswith(('cu', '"', ',', '{')):
        print(f"[Generic Extraction] Response appears truncated, attempting recovery...")
        # Find last complete closing brace structure
        last_close = response.rfind('}')
        if last_close > 0:
            # Try parsing from start to last closing brace
            truncated_attempt = response[:last_close + 1]
            result = safe_parse_json(truncated_attempt)
            if result:
                print(f"[Generic Extraction] ??? Recovered {len(result)} fields from truncated response")

    if result and isinstance(result, dict):
        print(f"[Generic Extraction] ??? Extracted {len(result)} fields: {list(result.keys())}")
        # Save learned structure for this document type
        if document_type:
            save_learned_structure(document_type, list(result.keys()))
    elif result and isinstance(result, list):
        print(f"[Generic Extraction] ??? Model returned a bare array instead of an object ??? retrying not supported here")
        result = None
    if not result:
        print(f"[Generic Extraction] ??? Failed to parse JSON. Response was: {response[:500]}")
        # Last resort: find outermost { and } and extract JSON between them (handles nested structures)
        import json as json_module
        start_idx = response.find('{')
        if start_idx != -1:
            # Find matching closing brace by counting nesting
            brace_count = 0
            end_idx = -1
            for i in range(start_idx, len(response)):
                if response[i] == '{':
                    brace_count += 1
                elif response[i] == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        end_idx = i
                        break

            if end_idx != -1:
                json_str = response[start_idx:end_idx + 1]
                try:
                    result = json_module.loads(json_str)
                    if result and isinstance(result, dict):
                        print(f"[Generic Extraction] Recovered nested JSON with {len(result)} fields")
                        # Save learned structure if recovery succeeded
                        if document_type:
                            save_learned_structure(document_type, list(result.keys()))
                        return result
                except:
                    print(f"[Generic Extraction] Could not parse recovered JSON")

    scalar_result = result if result else {}

    # ?????? Merge Python-parsed tables with Qwen scalar fields ?????????????????????????????????????????????????????????
    if parsed_tables:
        # Remove any scalar fields that Qwen extracted from table column headers
        # (i.e. keys that match column headers in any parsed table)
        table_col_keys = set()
        for rows in parsed_tables.values():
            if rows:
                table_col_keys.update(rows[0].keys())

        cleaned_scalars = {k: v for k, v in scalar_result.items() if k not in table_col_keys}
        merged = {**cleaned_scalars, **parsed_tables}

        if document_type:
            save_learned_structure(document_type, list(merged.keys()))
        print(f"[Generic Extraction] ??? Final merged fields: {list(merged.keys())}")
        return merged

    return scalar_result


def _build_vision_extraction_hints(document_type: str, raw_text: str = None) -> tuple:
    """
    Shared by extract_fields_vision() (today's two-call path) and
    classify_and_extract_combined_vision() (the single-page merged path) --
    factored out so both build the exact same schema-aware
    custom_doc_fields/table_hint_block/structure_hint_block instead of two
    copies drifting apart over time.

    raw_text is optional: the combined path calls this BEFORE OCR has run
    (document_type is known from a fast classify-only call, but the page
    hasn't been transcribed yet), so raw_text=None there. In that case the
    OCR-text-based table-detection heuristic and the "inject already-known
    multi-page text" block are simply skipped -- everything else (forced
    table hints, custom field schemas, DOCUMENT_STRUCTURE_HINTS) still
    applies identically either way.

    Returns (custom_doc_fields, table_hint_block, structure_hint_block).
    The source_lang-conditional note extract_fields_vision used to append to
    structure_hint_block here is now the caller's responsibility -- the
    combined path has no source_lang yet (see its own prompt's unconditional
    non-English guidance instead), so it doesn't belong in shared code.
    """
    # Custom types carry an explicit schema (structure rules added below). For
    # them, SKIP the noisy OCR table auto-detection ??? those conflicting
    # "TABLE DETECTED" blocks overwhelm a simple few-field extraction and make
    # the model return nothing. The structure rules alone are clearer.
    custom_doc_fields = get_custom_doc_fields(document_type)

    # Detect tables if OCR text is available (generic extraction only)
    table_hint_block = ""
    if raw_text and not custom_doc_fields:
        table_hints = detect_tables_in_text(raw_text)
        if table_hints:
            table_hint_block = f"\n\nTABLE DETECTION FROM OCR:\n{table_hints}"
            print(f"[Vision Extraction] Table hints detected: {len(table_hints)} chars")
        else:
            print(f"[Vision Extraction] No table patterns detected in OCR text (Vision will extract from image structure)")

    # For invoice/receipt/PO: always force a table extraction hint regardless of OCR detection,
    # because the OCR text may be truncated or poorly formatted from image input.
    if document_type in ("invoice", "receipt", "purchase_order") and not table_hint_block and not custom_doc_fields:
        table_hint_block = (
            f'\n\nTABLE DETECTION (FORCED for {document_type}): '
            f'This document type always contains a table. '
            f'STEP 1 ??? Find the ONE grid/table that has column headers (e.g. DESCRIPTION, UNIT, QUANTITY, UNIT PRICE, AMOUNT). That is the only line items table. '
            f'STEP 2 ??? Use the ACTUAL column headers from that table as the JSON keys in each row object. '
            f'Do NOT use generic names like "item_code", "description", "quantity", "unit_price", "amount". '
            f'Extract ONLY that table as "line_items": [{{...one object per row, keys = real column headers...}}, ...]. '
            f'WHAT IS NOT A LINE ITEM (never include these as rows): '
            f'(a) Address or contact info in the document header (company name, street, city, phone, fax, postcode) ??? these are scalar fields. '
            f'(b) Labels/numbers printed above the table like "Rep : 04" ??? capture as a "section" scalar field, NOT a row. '
            f'(c) Footer rows at the bottom of the table (SALES TAX, TOTAL, O.E., subtotals) ??? extract as scalar fields outside the array. '
            f'(d) Any text that is not inside the table grid itself. '
            f'Include every actual data row ??? do NOT stop early. Each row is one object. '
            f'Never flatten rows into a single string.'
        )
        print(f"[Vision Extraction] Forced table hint injected for '{document_type}'")

    # Inject document-type structure hints
    structure_hint_block = ""
    # Barcode/QR-typed fields are never sent to the vision model -- see
    # extract_typed_barcode_fields() below, which decodes them directly from
    # the page image instead. Asking the AI to guess a value for these is
    # exactly the class of hallucination this field type exists to
    # eliminate (a wrong-but-plausible-looking value is worse than an honest
    # blank), so they're excluded from the prompt entirely rather than
    # asked for and then overridden. custom_doc_fields itself (returned
    # below) still includes them -- callers that need the full schema (e.g.
    # process_document()'s "_allowed" bookkeeping, which guarantees the key
    # shows up in the result even before the barcode decode step runs) get
    # that from custom_doc_fields, not from this prompt-only filtering.
    prompt_fields = [f for f in custom_doc_fields if f.get("field_type") != "barcode"]
    if prompt_fields:
        field_names  = [f["field_name"]  for f in prompt_fields]
        field_labels = [f["field_label"] for f in prompt_fields]
        field_types_map = {f["field_name"]: f["field_type"] for f in prompt_fields}
        field_subcols   = {f["field_name"]: (f.get("sub_columns") or []) for f in prompt_fields}

        field_aliases_map = {f["field_name"]: (f.get("aliases") or "") for f in prompt_fields}

        desc_parts = []
        for fn, fl in zip(field_names, field_labels):
            ft = field_types_map[fn]
            if ft == "table":
                cols = field_subcols.get(fn) or []
                if cols:
                    col_str = ", ".join(f'"{c}"' for c in cols)
                    desc_parts.append(
                        f'"{fn}" (table "{fl}"): array of row objects ??? '
                        f'EACH ROW must have EXACTLY these keys: [{col_str}]'
                    )
                else:
                    desc_parts.append(f'"{fn}" (table "{fl}"): array of row objects')
            else:
                alias_str = ", ".join(a.strip() for a in field_aliases_map.get(fn, "").split(",") if a.strip())
                label_part = f"{fl}" + (f' / also labelled "{alias_str}" on this document' if alias_str else "")
                desc_parts.append(f'"{fn}" ({label_part})')

        structure_hint_block = (
            f"\n\nDOCUMENT STRUCTURE RULES (CUSTOM): Extract ONLY these fields:\n"
            + "\n".join(f"  - {d}" for d in desc_parts)
            + f"\nUse exactly these field names as JSON keys: {', '.join(field_names)}."
            + "\nFor table fields: output a JSON array of row objects with the exact column keys listed above."
        )
        # Inject active Prompt Studio rules (PromptVersion) so vision path
        # honours the same tuning as the text path.
        try:
            _pv_hint = (get_prompt_override(f"structure_hint:{document_type}") or "").strip()
            if len(_pv_hint) >= 40 and not _pv_hint.lstrip().startswith(("{", "[")):
                structure_hint_block += "\n\nADDITIONAL EXTRACTION RULES:\n" + _pv_hint
                print(f"[Vision Extraction] Prompt Studio rules injected for '{document_type}'")
        except Exception:
            pass

        # The image above is page 1 only -- but raw_text (OCR) covers every
        # page. Without this, fields that only appear on later pages (e.g. a
        # signatory name on the closing page) are invisible to the vision
        # model no matter how the rule is worded.
        if raw_text and len(raw_text) > 200:
            text_for_prompt = raw_text if len(raw_text) <= 6000 else raw_text[:2500] + "\n...[middle truncated]...\n" + raw_text[-3000:]
            structure_hint_block += (
                "\n\nFULL DOCUMENT TEXT (OCR, all pages -- the image above shows only page 1; "
                "use this text to find fields that appear on later pages):\n" + text_for_prompt
            )
            print(f"[Vision Extraction] Multi-page OCR text injected for '{document_type}' ({len(text_for_prompt)} chars)")

        print(f"[Vision Extraction] Custom type '{document_type}' ??? {len(prompt_fields)} field(s) injected"
              + (f" ({len(custom_doc_fields) - len(prompt_fields)} barcode/QR field(s) excluded, decoded separately)"
                 if len(custom_doc_fields) != len(prompt_fields) else ""))
    elif document_type and document_type in DOCUMENT_STRUCTURE_HINTS:
        # Apply structure hints for ALL languages ??? the hint controls table structure
        # (how many columns, array vs scalar) which is language-agnostic.
        # Language-of-keys handling is the caller's job (source_lang branch in
        # extract_fields_vision, or the self-determining rule in the combined
        # prompt) -- this hint only covers table shape, not key language.
        hint_text = get_prompt_override(f"structure_hint:{document_type}") or DOCUMENT_STRUCTURE_HINTS[document_type]
        structure_hint_block = f"\n\nDOCUMENT STRUCTURE RULES:\n{hint_text}"
        print(f"[Vision Extraction] Structure hint injected for '{document_type}'")

    return custom_doc_fields, table_hint_block, structure_hint_block


def extract_fields_vision(b64: str, media_type: str, document_type: str, raw_text: str = None, source_lang: str = "english") -> dict:
    """
    Extract fields directly from image using Qwen Vision (for images only).
    Vision model reads the image directly instead of converting to text first.
    Optional raw_text parameter for adding table detection hints from OCR.
    """
    print(f"[Pipeline] Extracting fields from image using Vision...")

    # A type with no defined fields (a freshly created custom type, or the
    # "other"/unclassified catch-all) gets a zero template, not a free-form
    # AI guess ??? the user extracts fields themselves via the Ask chat's
    # "extract" flow once they've decided what this type should look like.
    # DOCUMENT_STRUCTURE_HINTS is still honored as a defensive fallback for
    # built-in structured types (resume, bank_statement, ...) in case their
    # seeded DocumentField rows are ever missing.
    if not get_custom_doc_fields(document_type) and document_type not in DOCUMENT_STRUCTURE_HINTS:
        print(f"[Vision Extraction] '{document_type}' has no defined fields ??? skipping auto-extraction (zero template)")
        return {}

    custom_doc_fields, table_hint_block, structure_hint_block = _build_vision_extraction_hints(document_type, raw_text)

    # source_lang IS known here (unlike the combined path) -- restore the
    # non-English column-header note that used to live inline in this function.
    if not custom_doc_fields and document_type in DOCUMENT_STRUCTURE_HINTS and source_lang != "english":
        structure_hint_block += (
            f"\n\nNOTE for {source_lang} document: "
            "For table columns, use the column headers EXACTLY as they appear in the document "
            "(preserve the original language of the column headers ??? do NOT translate them). "
            "For scalar field names, use the original document label as the key."
        )

    if structure_hint_block:
        strict_rule = (
            "\n\nSTRICT FIELD RULE: Extract ONLY the fields defined in DOCUMENT STRUCTURE RULES above. "
            "Do NOT add any extra fields beyond what is listed there."
        )
    else:
        strict_rule = ""

    if source_lang != "english":
        lang_rule = (
            f"LANGUAGE RULE (HIGHEST PRIORITY): Keep ALL JSON keys in {source_lang} ??? use the original document labels as-is. "
            f"Values stay in original language."
        )
        key_rule = f"Field names (keys): keep in {source_lang} ??? use the exact label as it appears in the document."
        table_key_rule = f"For tables: extract as a JSON array of objects ??? use original column headers as keys (in {source_lang})."
        summary_key_rule = f"For summary rows: use the original label as the key (in {source_lang})."
    else:
        lang_rule = (
            "LANGUAGE RULE (HIGHEST PRIORITY): ALL JSON keys MUST be in English. "
            "Translate every field label to English no matter what language the document is in. Values stay in original language.\n"
            'Examples: "??????"???"unit price", "??????"???"tax rate", "???????????????"???"buyer name", "????????????"???"invoice date", "????????????"???"invoice number".'
        )
        key_rule = "Field names (keys): English only ??? translate from any language (Chinese, Arabic, French, etc.) to English."
        table_key_rule = "For tables: extract as a JSON array of objects ??? translate column headers to English as keys. Limit to 10 rows max."
        summary_key_rule = "For summary rows: translate the label to English as the key."

    field_scope_rule = (
        "Extract ONLY the fields listed in DOCUMENT STRUCTURE RULES ??? no extra fields."
        if structure_hint_block else
        "Extract ALL fields you find ??? do NOT stop early. Cover the entire document."
    )
    extraction_scope = "ONLY the defined fields" if structure_hint_block else "every field"

    template = get_prompt_override("extract_vision_prompt") or DEFAULT_EXTRACT_VISION_PROMPT
    prompt = (
        template
        .replace("{{LANG_RULE}}", lang_rule)
        .replace("{{TABLE_HINT_BLOCK}}", table_hint_block)
        .replace("{{STRUCTURE_HINT_BLOCK}}", structure_hint_block)
        .replace("{{STRICT_RULE}}", strict_rule)
        .replace("{{FIELD_SCOPE_RULE}}", field_scope_rule)
        .replace("{{KEY_RULE}}", key_rule)
        .replace("{{TABLE_KEY_RULE}}", table_key_rule)
        .replace("{{SUMMARY_KEY_RULE}}", summary_key_rule)
        .replace("{{EXTRACTION_SCOPE}}", extraction_scope)
    )

    try:
        response = call_qwen_vision(b64, media_type, prompt, max_tokens=16000)
        print(f"[Vision Extraction] Response length: {len(response)} chars")
        result = safe_parse_json(response)

        if result and isinstance(result, dict):
            # Remove underscores from field names
            result = clean_field_names(result)
            print(f"[Vision Extraction] ??? Extracted {len(result)} fields: {list(result.keys())}")
            return result
        else:
            print(f"[Vision Extraction] ??? Failed to parse JSON, falling back to text extraction")
            # Fallback: return empty dict, caller will retry with text extraction
            return {}
    except Exception as e:
        print(f"[Vision Extraction] ??? Error: {e}")
        if "failed to process image" in str(e).lower():
            print(f"[Vision Extraction] Image size exceeded API limits, falling back to text extraction")
        return {}


def classify_and_extract_combined_vision(b64: str, media_type: str, forced_document_type: str = None) -> dict:
    """
    Single-page fast path: a cheap classify-only call, then ONE combined
    Vision call for OCR text + structured fields together -- collapses what
    used to be 2 full ~16000-token Vision passes (classify+OCR inside
    process_document(), then a separate extract_fields_vision() call) into
    one near-instant classify call plus one full pass, cutting ~20-40s off
    single-page scanned-document processing. Local-model latency is
    dominated by output token count, not by looking at the image twice, so
    a ~20-token classify call is cheap even though it re-sends the same page.

    Scope: single-page, non-table-schema documents only (see
    process_document()). Two exclusions, both correctness-driven:
    - Multi-page scanned PDFs keep their existing 2-call flow, since
      extract_fields_vision() injects OCR text from pages 2..N into the
      field prompt so the model can find fields that only appear on later
      pages it can't see visually; that genuinely requires OCR to finish
      first and can't be collapsed here.
    - Table-schema types (any custom field with field_type == "table") are
      deliberately routed back to extract_fields_vision() too: real-document
      side-by-side testing (2026-08-21) showed table extraction quality is
      inconsistent when merged into the same call as OCR -- see the
      has_table_field branch below for the actual before/after numbers.

    Returns {"document_type", "raw_text", "fields", "fields_reliable"}.
    fields_reliable=False means the caller should fall back to today's
    proven extract_fields_vision() call instead of trusting these fields --
    correctness over speed (same >80% null_ratio threshold used elsewhere
    in this pipeline, e.g. process_document()'s PDF vision-fallback check,
    plus the table-schema exclusion above).
    """
    if forced_document_type:
        document_type = forced_document_type
    else:
        known_types_str = ", ".join(get_all_active_doc_type_names())
        classify_prompt = (
            "Look at this document image.\n"
            f"What specific type of document is it? Known types (prefer if match): {known_types_str}\n"
            "If none match, give a precise snake_case type name. Reply in English only.\n"
            "Reply with ONLY the document type in snake_case. Nothing else."
        )
        # No broad try/except here on purpose: both callers (the scanned-PDF
        # retry loop and the plain-image resolution-retry loop) already catch
        # exceptions from this whole function to retry at a smaller image
        # size on openai.APIStatusError (image too large). Swallowing that
        # here instead would silently proceed with document_type="other" on
        # an oversized image rather than letting the proven retry logic
        # actually fix it -- a strictly worse outcome than today's behavior.
        raw_type = call_qwen_vision(b64, media_type, classify_prompt, max_tokens=20)
        document_type = _parse_type_line("TYPE: " + raw_type.strip(), default="other")
    print(f"[Combined] Classified as: {document_type}")

    custom_doc_fields, table_hint_block, structure_hint_block = _build_vision_extraction_hints(document_type, raw_text=None)
    has_schema = bool(custom_doc_fields) or document_type in DOCUMENT_STRUCTURE_HINTS
    # Two signals: a genuine field_type=="table" schema field, OR one of the
    # three types this file already hardcodes as table-containing elsewhere
    # (the FORCED table hint in _build_vision_extraction_hints). The second
    # check matters even when custom_doc_fields exists: this deployment's
    # real "invoice"/"receipt" schemas model line_items/items as a plain
    # "text" field (the model still outputs it as an array, driven by
    # Prompt Studio override text, not the field_type), so field_type alone
    # would silently miss the exact case real-document testing (2026-08-21)
    # found broken -- see the has_table_field branch below.
    has_table_field = (
        document_type in ("invoice", "receipt", "purchase_order")
        or any(f.get("field_type") == "table" for f in custom_doc_fields)
    )

    if not has_schema:
        # Matches extract_fields_vision()'s zero-template behavior -- OCR
        # still needs to happen, but skip asking for fields at all rather
        # than a free-form AI guess for an undefined/unclassified type.
        print(f"[Combined] '{document_type}' has no defined fields ??? OCR-only (zero template)")
        # No try/except here either -- same reasoning as the classify-only
        # call above: let a resolution-related failure propagate to the
        # caller's retry loop instead of silently returning an empty result.
        response = call_qwen_vision(b64, media_type, DEFAULT_COMBINED_TEXT_ONLY_PROMPT, max_tokens=16000)
        raw_text = clean_qwen_output(response)
        return {"document_type": document_type, "raw_text": raw_text, "fields": {}, "fields_reliable": True}

    if has_table_field:
        # Real-document side-by-side testing (2026-08-21) showed table
        # extraction quality is inconsistent when merged into the same call
        # as OCR: a 4-row invoice table came back byte-identical to
        # extract_fields_vision()'s output, but a 3-row receipt table came
        # back completely empty ([]) in the same run. Per explicit direction:
        # table-schema types keep the proven two-call design -- OCR only
        # here, fields_reliable=False routes the caller to extract_fields_vision()
        # for the actual field/table extraction, same as the null_ratio
        # fallback below just triggered unconditionally for this type.
        print(f"[Combined] '{document_type}' has a table field ??? OCR-only, deferring extraction to extract_fields_vision() (table quality)")
        response = call_qwen_vision(b64, media_type, DEFAULT_COMBINED_TEXT_ONLY_PROMPT, max_tokens=16000)
        raw_text = clean_qwen_output(response)
        return {"document_type": document_type, "raw_text": raw_text, "fields": {}, "fields_reliable": False}

    if structure_hint_block:
        strict_rule = (
            "\n\nSTRICT FIELD RULE: Extract ONLY the fields defined in DOCUMENT STRUCTURE RULES above. "
            "Do NOT add any extra fields beyond what is listed there."
        )
        field_scope_rule = "Extract ONLY the fields listed in DOCUMENT STRUCTURE RULES ??? no extra fields."
        extraction_scope = "ONLY the defined fields"
    else:
        strict_rule = ""
        field_scope_rule = "Extract ALL fields you find ??? do NOT stop early. Cover the entire document."
        extraction_scope = "every field"

    prompt = (
        DEFAULT_COMBINED_VISION_PROMPT
        .replace("{{DOCUMENT_TYPE}}", document_type)
        .replace("{{TABLE_HINT_BLOCK}}", table_hint_block)
        .replace("{{STRUCTURE_HINT_BLOCK}}", structure_hint_block)
        .replace("{{STRICT_RULE}}", strict_rule)
        .replace("{{FIELD_SCOPE_RULE}}", field_scope_rule)
        .replace("{{EXTRACTION_SCOPE}}", extraction_scope)
    )

    result = {"document_type": document_type, "raw_text": "", "fields": {}, "fields_reliable": False}
    # No try/except around this call either -- same reasoning as above: an
    # oversized-image failure needs to reach the caller's resolution-retry
    # loop, not be swallowed into an empty "unreliable" result here (that
    # would skip the retry entirely and go straight to a worse fallback).
    response = call_qwen_vision(b64, media_type, prompt, max_tokens=20000)
    raw = clean_qwen_output(response)
    print(f"[Combined] Response length: {len(raw)} chars")

    # Split on the LAST "FIELDS:" occurrence -- mirrors the TYPE:/TEXT:
    # marker parsing already used elsewhere in this file (e.g. the scanned-PDF
    # combined classify+OCR call). Using the last occurrence (not the first)
    # guards against a document that happens to contain the literal word
    # "FIELDS:" somewhere in its own transcribed text.
    if "FIELDS:" in raw:
        idx = raw.rindex("FIELDS:")
        text_part = raw[:idx]
        fields_part = raw[idx + len("FIELDS:"):]
    else:
        text_part = raw
        fields_part = ""

    text_part = text_part.strip()
    if text_part.startswith("TEXT:"):
        text_part = text_part[len("TEXT:"):].strip()
    result["raw_text"] = text_part

    fields = safe_parse_json(fields_part) if fields_part.strip() else {}
    if fields and isinstance(fields, dict):
        fields = clean_field_names(fields)

    # Same >80% null_ratio correctness gate used elsewhere in this pipeline
    # (see process_document()'s PDF vision-fallback check) -- an empty or
    # mostly-null result from the combined call is treated as unreliable
    # rather than trusted, so the caller falls back to the proven
    # extract_fields_vision() call instead of silently shipping bad fields.
    if not fields:
        fields_reliable = False
    else:
        null_ratio = sum(1 for v in fields.values() if v is None) / len(fields)
        fields_reliable = null_ratio <= 0.8

    result["fields"] = fields
    result["fields_reliable"] = fields_reliable
    print(f"[Combined] Extracted {len(fields)} fields, reliable={fields_reliable}")
    return result


def clean_field_names(fields: dict) -> dict:
    """
    Remove underscores from field names.
    contact_info_email ??? contact info email
    """
    cleaned = {}
    for key, value in fields.items():
        # Replace underscores with spaces
        clean_key = key.replace("_", " ")
        cleaned[clean_key] = value
    return cleaned


def parse_nested_json_fields(fields: dict) -> dict:
    """
    Parse any JSON strings in the extracted fields.
    Converts string values that contain JSON into actual objects/arrays.
    """
    for key, value in fields.items():
        if isinstance(value, str) and value.strip().startswith(('[', '{')):
            try:
                # Try to parse as JSON
                fields[key] = json.loads(value)
                print(f"[Post-process] Parsed nested JSON in field '{key}'")
            except (json.JSONDecodeError, ValueError):
                # Not valid JSON, keep as string
                pass
    return fields


def sort_fields_by_document_order(fields: dict, raw_text: str, document_type: str = None) -> dict:
    """
    Sort extracted fields by their appearance order in the document.
    If FIELD_ORDER defines an explicit order for the document type, use it.
    Otherwise: (1) label found in raw_text, (2) value found in raw_text, (3) Qwen output order.
    Null-value and array/object fields are placed after scalars, preserving their relative order.
    """
    # Normalize document_type to match FIELD_ORDER keys (snake_case ??? space-separated)
    doc_type_key = document_type.replace("_", " ").lower() if document_type else ""
    explicit_order = FIELD_ORDER.get(doc_type_key, [])

    if explicit_order:
        # Build a lookup: normalized field name ??? position in explicit order
        order_index = {name.lower(): i for i, name in enumerate(explicit_order)}
        ordered = {}
        unordered = {}
        for field_name, value in fields.items():
            normalized = field_name.lower()
            if normalized in order_index:
                ordered[field_name] = (order_index[normalized], value)
            else:
                unordered[field_name] = value
        # Combine: explicitly ordered fields first, then any extras at the end
        result = {name: val for name, (_, val) in sorted(ordered.items(), key=lambda x: x[1][0])}
        result.update(unordered)
        return result

    # Fallback: position-based sort for generic/unknown document types
    raw_text_lower = raw_text.lower()
    # bucket 0 = label found, 1 = value found, 2 = array/object, 3 = null
    sort_keys = {}

    for i, (field_name, value) in enumerate(fields.items()):
        label_pos = raw_text_lower.find(field_name.lower())
        if label_pos != -1:
            sort_keys[field_name] = (0, label_pos, i)
            continue

        if value is None:
            sort_keys[field_name] = (3, 0, i)
            continue

        if isinstance(value, (list, dict)):
            sort_keys[field_name] = (2, 0, i)
            continue

        val_pos = raw_text_lower.find(str(value).lower().strip())
        if val_pos != -1:
            sort_keys[field_name] = (1, val_pos, i)
        else:
            sort_keys[field_name] = (1, len(raw_text), i)  # unfound: after found, preserve Qwen order

    return {name: fields[name] for name in sorted(sort_keys, key=lambda x: sort_keys[x])}


def get_custom_doc_fields(document_type: str) -> list:
    """
    Look up a custom document type defined in the Settings page (document_types table).
    Returns list of field dicts if found and active, else empty list.
    Runs inside an app context so it can access SQLAlchemy.
    """
    if not document_type:
        return []
    _cache_key = f"custom_doc_fields:{document_type}"
    cached, hit = _doc_query_cache_get(_cache_key)
    if hit:
        return cached
    result = []
    try:
        from app import app
        from document_model import DocumentType
        with app.app_context():
            # Normalize so the classifier's display form ("TAX INVOICE" / "tax invoice")
            # still matches the stored type name ("tax_invoice"). Without this, a
            # space-vs-underscore mismatch silently falls through to generic
            # "extract everything" mode instead of honoring the defined fields.
            def _norm(s):
                return (s or "").lower().replace("_", " ").replace("-", " ").strip()
            target = _norm(document_type)
            actives = DocumentType.query.filter_by(is_active=True).all()
            dt = next((t for t in actives if _norm(t.name) == target), None)
            if not dt:
                # Fuzzy fallback: match by word overlap so "tax_invoice" → "invoice"
                # and "Tax Invoice (Freight)" → "invoice" etc.
                target_words = set(target.split())
                best, best_score = None, 0
                for t in actives:
                    if not t.fields:
                        continue
                    score = len(target_words & set(_norm(t.name).split()))
                    if score > best_score:
                        best, best_score = t, score
                if best and best_score > 0:
                    print(f"[CustomType] fuzzy matched '{document_type}' to '{best.name}'")
                    dt = best
            if dt and dt.fields:
                result = [f.to_dict() for f in dt.fields]
    except Exception as e:
        print(f"[CustomType] Could not query DB: {e}")
    _doc_query_cache_set(_cache_key, result)
    return result


def get_all_active_doc_type_names() -> list:
    """
    Return all active document type names from the DB.
    Falls back to FIELD_MAP keys if DB is unavailable.
    """
    _cache_key = "active_doc_type_names"
    cached, hit = _doc_query_cache_get(_cache_key)
    if hit:
        return cached
    result = list(FIELD_MAP.keys())
    try:
        from app import app
        from document_model import DocumentType
        with app.app_context():
            types = DocumentType.query.filter_by(is_active=True).order_by(DocumentType.name).all()
            if types:
                result = [t.name for t in types]
    except Exception as e:
        print(f"[DocTypes] Could not query DB: {e}")
    _doc_query_cache_set(_cache_key, result)
    return result


def extract_fields_custom(raw_text: str, document_type: str, custom_fields: list) -> dict:
    """
    Extract only the fields defined in a custom document type from Settings.
    Builds a targeted prompt using field_name (XML tag) and field_label (human name).
    Fields with field_type == "table" are extracted as arrays of row objects whose
    keys are the column names stored in sub_columns.
    """
    field_names   = [f["field_name"]  for f in custom_fields]
    field_labels  = [f["field_label"] for f in custom_fields]
    field_types   = {f["field_name"]: f["field_type"] for f in custom_fields}
    sub_columns   = {f["field_name"]: (f.get("sub_columns") or []) for f in custom_fields}

    desc_lines = []
    for fn, fl in zip(field_names, field_labels):
        ft = field_types[fn]
        if ft == "table":
            cols = sub_columns.get(fn) or []
            if cols:
                col_str = ", ".join(f'"{c}"' for c in cols)
                desc_lines.append(
                    f'  - "{fn}" (table): {fl}\n'
                    f'    *** MANDATORY COLUMN RULE: every row object MUST have EXACTLY these keys '
                    f'(and no others): [{col_str}]\n'
                    f'    Map document columns to the closest matching custom column. '
                    f'Set null for any custom column not present in the document. '
                    f'DO NOT add extra keys or rename the required keys.'
                )
            else:
                desc_lines.append(
                    f'  - "{fn}" (table): {fl} ??? extract as array of row objects '
                    f'using the actual column headers from the document as keys'
                )
        else:
            desc_lines.append(f'  - "{fn}" ({ft}): {fl}')
    field_descriptions = "\n".join(desc_lines)

    # JSON template: tables shown as arrays, scalars as strings
    tmpl_parts = []
    for fn in field_names:
        if field_types[fn] == "table":
            cols = sub_columns.get(fn) or []
            if cols:
                row_ex = "{" + ", ".join(f'"{c}": "..."' for c in cols) + "}"
                tmpl_parts.append(f'"{fn}": [{row_ex}, ...]')
            else:
                tmpl_parts.append(f'"{fn}": [{{...}}]')
        else:
            tmpl_parts.append(f'"{fn}": "..."')
    json_template = ", ".join(tmpl_parts)

    type_hints = []
    for fn in field_names:
        ft = field_types[fn]
        if ft == "date":
            type_hints.append(f'"{fn}": return a date string (e.g. "2024-01-15")')
        elif ft == "number":
            type_hints.append(f'"{fn}": return a numeric value (no currency symbols)')
        elif ft == "currency":
            type_hints.append(f'"{fn}": return as currency string (e.g. "1,234.56")')
        elif ft == "table":
            cols = sub_columns.get(fn) or []
            if cols:
                type_hints.append(
                    f'"{fn}": array of row objects, EACH ROW must have ONLY '
                    f'these exact keys: {json.dumps(cols)}'
                )
            else:
                type_hints.append(f'"{fn}": return a JSON array of row objects ??? one object per row, null if no table found')

    type_hint_block = "\nType hints:\n" + "\n".join(f"  - {t}" for t in type_hints) if type_hints else ""

    MAX_CHARS = 16000
    if len(raw_text) > MAX_CHARS:
        text_block = raw_text[:10000] + "\n[...]\n" + raw_text[-4000:]
    else:
        text_block = raw_text

    field_names_list = ", ".join(f'"{fn}"' for fn in field_names)

    # Connect the per-type workspace prompt (active prompt version edited in the
    # Doc Type workspace) so its tuning actually affects custom-type extraction.
    # Skip anything that isn't a real prompt (e.g. a bad merge that returned an
    # empty JSON result) so it can't sabotage extraction.
    extra_hint_block = ""
    try:
        wp = (get_prompt_override(f"structure_hint:{document_type}") or "").strip()
        _prompt_source = document_type
        if not wp or len(wp) < 40:
            # Fuzzy fallback: find nearest registered type and use its prompt
            from app import app as _app_ps
            from document_model import DocumentType as _DT_ps
            with _app_ps.app_context():
                def _norm_ps(s): return (s or "").lower().replace("_", " ").replace("-", " ").strip()
                _tw = set(_norm_ps(document_type).split())
                _actives = _DT_ps.query.filter_by(is_active=True).all()
                _best, _best_score = None, 0
                for _t in _actives:
                    _score = len(_tw & set(_norm_ps(_t.name).split()))
                    if _score > _best_score:
                        _best, _best_score = _t, _score
                if _best and _best_score > 0 and _best.name != document_type:
                    wp = (get_prompt_override(f"structure_hint:{_best.name}") or "").strip()
                    _prompt_source = _best.name
        if len(wp) >= 40 and not wp.lstrip().startswith(("{", "[")):
            extra_hint_block = "\n\nADDITIONAL EXTRACTION RULES (from this type's tuned prompt):\n" + wp
            print(f"[PromptStudio] Active prompt applied for '{_prompt_source}' ({len(wp)} chars)")
        else:
            print(f"[PromptStudio] No usable prompt found for '{document_type}' (got {len(wp)} chars)")
    except Exception as e:
        print(f"[PromptStudio] Error loading prompt for '{document_type}': {e}")

    template = get_prompt_override("extract_custom_fields_prompt") or DEFAULT_EXTRACT_CUSTOM_FIELDS_PROMPT
    prompt = (
        template
        .replace("{{DOCUMENT_TYPE}}", document_type)
        .replace("{{FIELD_DESCRIPTIONS}}", field_descriptions)
        .replace("{{TYPE_HINT_BLOCK}}", type_hint_block)
        .replace("{{EXTRA_HINT_BLOCK}}", extra_hint_block)
        .replace("{{TEXT_BLOCK}}", text_block)
        .replace("{{FIELD_NAMES_LIST}}", field_names_list)
        .replace("{{JSON_TEMPLATE}}", "{" + json_template + "}")
    )

    print(f"[CustomExtraction] Extracting {len(custom_fields)} custom fields for '{document_type}'...")
    print(f"[CustomExtraction] Table sub_columns: { {fn: sub_columns[fn] for fn in field_names if field_types[fn] == 'table'} }")
    result = safe_parse_json(call_qwen_text(prompt, max_tokens=2000))

    if result and isinstance(result, dict):
        # Keep ONLY the fields this type defines (in schema order), null for any
        # the model didn't return. The model often adds a key of its own (e.g.
        # "code") despite being told not to, and extract_fields() is also called
        # straight from document_routes (doc type change, /reextract) where
        # nothing downstream strips it - it would be saved as a field the user
        # never created. Same space-vs-underscore key tolerance as process_document.
        _dropped = [k for k in result if k not in field_names and k.replace(" ", "_") not in field_names]
        result = {
            fn: (result[fn] if fn in result else result.get(fn.replace("_", " ")))
            for fn in field_names
        }
        if _dropped:
            print(f"[CustomExtraction] Dropped {len(_dropped)} key(s) not defined for '{document_type}': {_dropped}")

        # Deterministic safety net for DATE fields. The small local model often
        # leaves dates null or mangles them (e.g. concatenating an adjacent
        # Customer No.) on messy column-layout OCR. For each date field:
        #   - if it has a value, keep only the date token (strip trailing junk);
        #   - if it's empty, fall back to the first date found in the document.
        # This guarantees a clean date without depending on prompt tuning.
        import re as _re_date
        _DATE_RE = _re_date.compile(r'\b\d{1,2}[-/.\s](?:\d{1,2}|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-/.\s]\d{2,4}\b', _re_date.IGNORECASE)
        for fn in field_names:
            if field_types.get(fn) != "date":
                continue
            val = result.get(fn)
            if val:
                m = _DATE_RE.search(str(val))
                if m and m.group(0) != str(val).strip():
                    result[fn] = m.group(0)
                    print(f"[CustomExtraction] Date cleanup: '{fn}' -> {m.group(0)}")
            else:
                m = _DATE_RE.search(raw_text or "")
                if m:
                    result[fn] = m.group(0)
                    print(f"[CustomExtraction] Date fallback: '{fn}' = {m.group(0)} (first date in document)")

        # Post-process: enforce exact column structure for table fields
        for fn in field_names:
            if field_types[fn] != "table":
                continue
            cols = sub_columns.get(fn) or []
            if not cols:
                continue
            rows = result.get(fn)
            if not isinstance(rows, list):
                continue
            enforced = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                # Build a case-insensitive lookup of the row's keys
                row_lower = {k.lower().strip(): v for k, v in row.items()}
                fixed = {}
                for col in cols:
                    # Exact match first, then case-insensitive
                    if col in row:
                        fixed[col] = row[col]
                    else:
                        fixed[col] = row_lower.get(col.lower().strip())
                enforced.append(fixed)
            result[fn] = enforced
            print(f"[CustomExtraction] ??? Enforced {len(cols)} columns for '{fn}': {cols}")

        # Deterministic post-processor for fields whose PDF layout is INVERTED:
        # values appear on the line BEFORE their labels, not after.
        # Example raw text:
        #   "27-12-2017  0134270"          <- values (date, customer_no)
        #   "Date:  Customer No.:"          <- labels
        #   "2000050105"                    <- next value (customer reference)
        #   "Customer reference: B/L No.:"  <- next labels
        import re as _re_lbl
        _LABEL_PATTERNS = [
            ("customer_no",        r"Customer\s+No\.?\s*[:\-]",          "last"),
            ("customer_reference", r"Customer\s+[Rr]eference\s*[:\-]",    "only"),
            ("customer_ref",       r"Customer\s+[Rr]ef\.?\s*[:\-]",      "only"),
        ]
        if raw_text:
            _lbl_lines = raw_text.split("\n")
            for _lbl_field, _lbl_pat, _lbl_tok in _LABEL_PATTERNS:
                if _lbl_field not in field_names:
                    continue
                _lbl_val = ""
                for _li, _lline in enumerate(_lbl_lines):
                    if not _re_lbl.search(_lbl_pat, _lline, _re_lbl.IGNORECASE):
                        continue
                    # Strategy 1: value on same line AFTER the label
                    # Require 5+ alphanumeric chars to avoid matching embedded labels
                    # like "B/L" in "Customer reference: B/L No.: Bkg Ref:"
                    _s1 = _re_lbl.search(
                        _lbl_pat + r"\s*([A-Z0-9]{5,})\b",
                        _lline, _re_lbl.IGNORECASE
                    )
                    if _s1:
                        _lbl_val = _s1.group(1).rstrip(".,;")
                        break
                    # Strategy 2: value on the line BEFORE (inverted layout)
                    for _pj in range(_li - 1, max(_li - 4, -1), -1):
                        _prev = _lbl_lines[_pj].strip()
                        if not _prev:
                            continue
                        _ptok = _prev.split()
                        if _ptok:
                            _pick = _ptok[-1] if _lbl_tok == "last" else (_ptok[0] if len(_ptok) == 1 else "")
                            _pick = _pick.rstrip(".,;")
                            if (_pick and
                                    _re_lbl.match(r"^[A-Z0-9]{4,}$", _pick, _re_lbl.I) and
                                    not _re_lbl.match(r"^\d{1,2}[-/]\d", _pick)):
                                _lbl_val = _pick
                        break
                    break
                if _lbl_val:
                    _cur = str(result.get(_lbl_field) or "")
                    if _lbl_val != _cur:
                        print(f"[CustomExtraction] {_lbl_field} override: {repr(_cur)} -> {repr(_lbl_val)}")
                        result[_lbl_field] = _lbl_val

        print(f"[CustomExtraction] ??? Extracted: {list(result.keys())}")
        return result

    print(f"[CustomExtraction] ??? Parsing failed, falling back to generic extraction")
    return {}


def extract_fields(raw_text: str, document_type: str, similar_docs: list = None, source_lang: str = "english") -> dict:
    """
    Extract fields using flexible generic extraction.

    Document type is kept for classification/display only, NOT for structured field extraction.
    AI intelligently identifies ALL meaningful fields without rigid templates.
    This allows consistent field extraction across all document types.

    If the document_type matches a custom type defined in Settings, those exact fields are used.
    """
    # Check for custom document type defined via Settings page
    custom_fields = get_custom_doc_fields(document_type)
    if custom_fields:
        print(f"[Pipeline] Custom type '{document_type}' ??? using {len(custom_fields)} defined fields")
        result = extract_fields_custom(raw_text, document_type, custom_fields)
        if result:
            result = parse_nested_json_fields(result)
            return result

    # No fields defined for this type (a freshly created custom type, or the
    # "other"/unclassified catch-all) ??? zero template, not a free-form AI
    # guess. The user extracts fields themselves via the Ask chat's "extract"
    # flow once they've decided what this type should look like. Built-in
    # structured types (resume, bank_statement, ...) still fall through to
    # generic extraction as a defensive fallback if their seeded fields are
    # ever missing.
    if document_type not in DOCUMENT_STRUCTURE_HINTS:
        print(f"[Pipeline] Type '{document_type}' has no defined fields ??? skipping auto-extraction (zero template)")
        return {}

    print(f"[Pipeline] Type '{document_type}' ??? using generic intelligent extraction (structure hint fallback)")
    fields = extract_fields_generic(raw_text, document_type=document_type, source_lang=source_lang)

    # Parse any nested JSON strings into actual objects/arrays
    fields = parse_nested_json_fields(fields)

    # Remove underscores from field names
    fields = clean_field_names(fields)

    # Sort fields by document order, with priority fields first
    fields = sort_fields_by_document_order(fields, raw_text, document_type=document_type)
    print(f"[Pipeline] Fields sorted by document order")

    return fields


# ?????? Table-aware PDF text extraction ??????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

def extract_pdf_text_with_tables(pdf_path: str) -> str:
    """
    Fallback: Extracts text from a PDF combining pdfplumber (tables) + PyMuPDF (prose).
    Used when opendataloader-pdf is unavailable.
    """
    import fitz
    import pdfplumber

    page_outputs = []

    try:
        with pdfplumber.open(pdf_path) as plumber_doc, fitz.open(pdf_path) as fitz_doc:
            for page_num in range(len(fitz_doc)):
                sections = []

                # ?????? pdfplumber: extract tables ????????????????????????????????????????????????????????????????????????????????????????????????
                try:
                    plumber_page = plumber_doc.pages[page_num]
                    tables = plumber_page.extract_tables()
                    table_text_blocks = []
                    for table in tables:
                        rows = []
                        for row in table:
                            cells = [str(c).strip() if c else "" for c in row]
                            rows.append(" | ".join(cells))
                        table_text_blocks.append("\n".join(rows))
                    if table_text_blocks:
                        sections.append("\n\n".join(table_text_blocks))
                except Exception as e:
                    print(f"[pdfplumber] Page {page_num+1} table error: {e}")

                # ?????? PyMuPDF: extract non-table prose text ???????????????????????????????????????????????????????????????
                try:
                    fitz_page = fitz_doc[page_num]
                    prose = fitz_page.get_text().strip()
                    if prose:
                        sections.append(prose)
                except Exception as e:
                    print(f"[PyMuPDF] Page {page_num+1} text error: {e}")

                if sections:
                    page_outputs.append(f"[PAGE {page_num+1}]\n" + "\n\n".join(sections))

    except Exception as e:
        print(f"[Extraction] pdfplumber failed ({e}), falling back to PyMuPDF only")
        import fitz
        with fitz.open(pdf_path) as doc:
            for i, page in enumerate(doc):
                t = page.get_text().strip()
                if t:
                    page_outputs.append(f"[PAGE {i+1}]\n{t}")

    return "\n\n".join(page_outputs).strip()


def parse_odl_blocks(json_path: str) -> list:
    """
    Parse Docling JSON output to extract text blocks with bounding boxes.
    Returns list of: {"page": int, "text": str, "bbox": [l, b, r, t]}
    bbox is in PDF points, bottom-left origin ??? same coordinate space as PDF.js.
    """
    with open(json_path, "r", encoding="utf-8") as f:
        doc = json.load(f)

    # Get page dimensions for TOPLEFT ??? BOTTOMLEFT conversion
    page_heights = {}
    for page_no_str, page_info in doc.get("pages", {}).items():
        size = page_info.get("size", {})
        page_heights[int(page_no_str)] = size.get("height", 792)

    blocks = []

    def add_item(text, prov_list):
        if not text or not text.strip():
            return
        text = _fix_odl_rtl_line_order(text)
        for prov in prov_list:
            page_no = prov.get("page_no", 1)
            bbox_data = prov.get("bbox", {})
            if not isinstance(bbox_data, dict):
                continue
            l = bbox_data.get("l", 0)
            t_raw = bbox_data.get("t", 0)
            r = bbox_data.get("r", 0)
            b_raw = bbox_data.get("b", 0)
            coord_origin = bbox_data.get("coord_origin", "BOTTOMLEFT")
            if coord_origin == "TOPLEFT":
                ph = page_heights.get(page_no, 792)
                b = ph - t_raw
                t = ph - b_raw
            else:
                b = b_raw
                t = t_raw
            blocks.append({"page": page_no, "text": text, "bbox": [l, b, r, t]})

    # Texts
    for item in doc.get("texts", []):
        add_item(item.get("text") or item.get("orig", ""), item.get("prov", []))

    # Tables ??? add whole-table block + each cell
    for table in doc.get("tables", []):
        table_prov = table.get("prov", [])
        for prov in table_prov:
            page_no = prov.get("page_no", 1)
            bbox_data = prov.get("bbox", {})
            if isinstance(bbox_data, dict):
                l = bbox_data.get("l", 0)
                t_raw = bbox_data.get("t", 0)
                r = bbox_data.get("r", 0)
                b_raw = bbox_data.get("b", 0)
                coord_origin = bbox_data.get("coord_origin", "BOTTOMLEFT")
                if coord_origin == "TOPLEFT":
                    ph = page_heights.get(page_no, 792)
                    b = ph - t_raw
                    t = ph - b_raw
                else:
                    b = b_raw
                    t = t_raw
                # Add individual cell texts under the table bbox
                for row in table.get("data", {}).get("grid", []):
                    for cell in row:
                        cell_text = _fix_odl_rtl_line_order((cell.get("text") or "").strip())
                        if cell_text:
                            blocks.append({"page": page_no, "text": cell_text, "bbox": [l, b, r, t]})

    print(f"[ODL] ??? Parsed {len(blocks)} text blocks with bboxes")
    return blocks


def normalize_bbox_to_relative(bbox: list, page_width: float, page_height: float) -> dict:
    """
    Converts ODL bbox [l, b, r, t] (PDF points, bottom-left origin)
    to normalized 0-1 coords (top-left origin) for frontend rendering.
    """
    l, b, r, t = bbox
    return {
        "x1": l / page_width,
        "y1": 1.0 - (t / page_height),  # flip Y: PDF bottom-left ??? top-left
        "x2": r / page_width,
        "y2": 1.0 - (b / page_height),  # flip Y
    }


def build_image_blocks(image_b64: str, media_type: str) -> list:
    """
    Ask Qwen Vision to return each text line with its bounding box
    as normalized 0-1 percentages (top-left origin).
    Returns list of {"page": 1, "text": str, "bbox": {x1,y1,x2,y2}}
    ready for the frontend ODL highlight path.

    Uses a 10-second timeout since this is a non-critical lazy operation
    (field highlighting overlay) that should fail fast if LM Studio is slow.
    """
    prompt = (
        "Return ONLY a JSON array. For every line of text visible in this image, "
        "provide its text and bounding box as a percentage of image dimensions (0.0 to 1.0).\n"
        "Format: [{\"text\": \"...\", \"x1\": 0.05, \"y1\": 0.10, \"x2\": 0.95, \"y2\": 0.14}, ...]\n"
        "Rules:\n"
        "- x1,y1 = top-left corner; x2,y2 = bottom-right corner\n"
        "- all values between 0.0 and 1.0\n"
        "- one entry per visible text line\n"
        "- output ONLY the JSON array, no other text"
    )
    try:
        raw = call_qwen_vision(image_b64, media_type, prompt, max_tokens=2000, timeout=10.0)
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not match:
            return []
        items = json.loads(match.group())
        blocks = []
        for item in items:
            text = str(item.get("text", "")).strip()
            x1 = float(item.get("x1", 0))
            y1 = float(item.get("y1", 0))
            x2 = float(item.get("x2", 0))
            y2 = float(item.get("y2", 0))
            if not text or x2 <= x1 or y2 <= y1:
                continue
            blocks.append({
                "page": 1,
                "text": text,
                "bbox": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
            })
        print(f"[ImageBlocks] ??? Got {len(blocks)} bbox blocks from Qwen")
        return blocks
    except Exception as e:
        print(f"[ImageBlocks] Failed: {e}")
        return []


def _ocr_page_blocks(img, page: int) -> list:
    """OCR line blocks for one image/page in build_image_blocks()' shape, or []
    if OCR fails or finds no text (callers then fall back to the Vision path)."""
    from ocr_service import ocr_line_blocks_normalized
    try:
        t = time.time()
        blocks = ocr_line_blocks_normalized(img, page=page)
        print(f"[TextBlocks] OCR page {page}: {len(blocks)} blocks in {time.time() - t:.1f}s")
    except Exception as e:
        print(f"[TextBlocks] OCR failed on page {page}, falling back to Vision: {e}")
        return []
    if not blocks:
        print(f"[TextBlocks] OCR found no text on page {page}, falling back to Vision")
    return blocks


def compute_text_blocks_lazy(file_path: str) -> list:
    """
    Compute per-line bbox blocks for the highlight overlay on demand (called
    from document_routes.get_text_blocks() on a document's first GET
    /<id>/blocks request), instead of eagerly during process_document()'s
    upload-time pipeline where it doubled the vision-model calls per page.

    Re-derives the same routing decision process_document() made at upload
    time (is_scanned_pdf() is a cheap PyMuPDF text-layer check, not a model
    call) so this works standalone from just the original file on disk:
      - Scanned/image-only PDF: rasterize each page at 200 DPI and run OCR
        (ocr_service) for real line boxes.
      - Digital PDF: re-run the (also model-free) ODL extraction to recover
        its bboxes -- normally already cached by the odl_blocks branch in
        process_document(), so reaching this path from that branch usually
        means ODL genuinely produced nothing usable for this file.
      - Plain image: OCR on the full-resolution original.
    For images and scanned pages, if OCR fails or finds nothing, that page
    falls back to build_image_blocks() (Vision bbox call) so the overlay is
    never worse than before.
    """
    ext = os.path.splitext(file_path)[1].lower()
    is_pdf = ext == ".pdf"

    if not is_pdf:
        try:
            # Full resolution, not encode_image()'s 1024px copy - the boxes are
            # normalized to 0..1 either way, and OCR reads small text better.
            # No EXIF transpose, matching the frame the old Vision path used.
            with Image.open(file_path) as img:
                img.load()
                blocks = _ocr_page_blocks(img, page=1)
        except Exception as e:
            print(f"[TextBlocks] Opening image failed: {e}")
            blocks = []
        if blocks:
            return blocks
        try:
            b64, mt, _, _ = encode_image(file_path, max_size=1024, quality=85)
        except Exception as e:
            print(f"[TextBlocks] encode_image failed: {e}")
            return []
        return build_image_blocks(b64, mt)

    if is_scanned_pdf(file_path):
        import fitz
        normalized_blocks = []
        vision_pages = None   # rasterized lazily, only if a page needs the Vision fallback
        try:
            with fitz.open(file_path) as _doc:
                for page_num, _page in enumerate(_doc, start=1):
                    pix = _page.get_pixmap(dpi=200)
                    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples) if pix.n == 3                         else Image.frombytes("RGBA" if pix.alpha else "L", (pix.width, pix.height), pix.samples).convert("RGB")
                    page_blocks = _ocr_page_blocks(img, page=page_num)
                    if not page_blocks:
                        if vision_pages is None:
                            vision_pages = pdf_pages_to_b64(file_path, dpi=150, max_dim=1200)
                        pb64, pmt, _pw, _ph = vision_pages[page_num - 1]
                        page_blocks = build_image_blocks(pb64, pmt)
                        for b in page_blocks:
                            b["page"] = page_num
                    normalized_blocks.extend(page_blocks)
        except Exception as e:
            print(f"[TextBlocks] Rasterizing scanned PDF failed: {e}")
            return normalized_blocks
        return normalized_blocks

    try:
        _, odl_blocks = extract_pdf_text_with_odl(file_path)
    except Exception as e:
        print(f"[TextBlocks] ODL re-extraction failed: {e}")
        return []
    if not odl_blocks:
        return []
    import fitz
    with fitz.open(file_path) as _doc:
        page_sizes = {i + 1: (_page.rect.width, _page.rect.height) for i, _page in enumerate(_doc)}
    normalized_blocks = []
    for block in odl_blocks:
        pw, ph = page_sizes.get(block["page"], (612, 792))
        normalized_blocks.append({
            "page": block["page"],
            "text": block["text"],
            "bbox": normalize_bbox_to_relative(block["bbox"], pw, ph),
        })
    return normalized_blocks


_ARABIC_SCRIPT_RE = re.compile(r'[؀-ۿݐ-ݿ]')
_ARABIC_SCRIPT_RUN_RE = re.compile(r'[؀-ۿݐ-ݿ]+|[^؀-ۿݐ-ݿ]+')
_ARABIC_LATIN_SEAM_RE = re.compile(
    r'(?<=[؀-ۿݐ-ݿ])(?=[A-Za-z0-9])|(?<=[A-Za-z0-9])(?=[؀-ۿݐ-ݿ])'
)


def _fix_odl_rtl_line_order(text: str) -> str:
    """
    opendataloader-pdf emits each line containing Arabic-script text (Jawi,
    used for Malay) with the ENTIRE line's reading order reversed - not a
    proper per-run bidi reversal, but close: within the line, the sequence of
    script runs (Jawi vs. Latin/digits) gets reversed, and the characters
    *inside* each Jawi run get reversed too, while Latin words and digit
    strings (phone numbers, dates, reference numbers) are internally left
    alone. Net effect: pure-Jawi text comes out completely backwards, embedded
    Latin/digit runs come out in the wrong position but spelled correctly.
    Confirmed by comparing ODL's output against PyMuPDF's (which extracts this
    same PDF correctly) on a real Jawi letter - this bug is specific to ODL's
    RTL handling, not something introduced elsewhere in this pipeline.

    Fix: re-reverse the run order per line, and re-reverse the characters
    within Arabic-script runs only, leaving Latin/digit runs untouched so
    numbers and abbreviations ("Dr", "E-mel", phone/reference numbers) don't
    get corrupted a second time. Lines with no Arabic-script characters at all
    (pure English sections) are returned unchanged.
    """
    def fix_line(line):
        if not _ARABIC_SCRIPT_RE.search(line):
            return line
        runs = _ARABIC_SCRIPT_RUN_RE.findall(line)
        fixed_runs = [r[::-1] if _ARABIC_SCRIPT_RE.search(r) else r for r in reversed(runs)]
        return _ARABIC_LATIN_SEAM_RE.sub(' ', ''.join(fixed_runs))

    return '\n'.join(fix_line(line) for line in text.split('\n'))


def extract_pdf_text_with_odl(pdf_path: str) -> tuple:
    """
    Extracts text from a PDF using opendataloader-pdf (primary extractor).
    Outputs clean markdown with proper table structure.
    Returns (markdown_text, text_blocks) where text_blocks is a list of
    {"page": int, "text": str, "bbox": [l, b, r, t]} dicts.
    """
    import tempfile
    import opendataloader_pdf

    print(f"[ODL] Extracting: {os.path.basename(pdf_path)}")
    blocks = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        # Request both markdown (for AI) and json (for bboxes) in one pass
        try:
            opendataloader_pdf.convert(
                input_path=[pdf_path],
                output_dir=tmp_dir,
                format=["markdown", "json"],
                quiet=True,
                table_method="cluster",
                reading_order="xycut",
            )
        except Exception:
            # Fallback: some versions only accept a single format string
            opendataloader_pdf.convert(
                input_path=[pdf_path],
                output_dir=tmp_dir,
                format="markdown",
                quiet=True,
                table_method="cluster",
                reading_order="xycut",
            )

        all_files = os.listdir(tmp_dir)
        md_files   = [f for f in all_files if f.endswith(".md")]
        json_files = [f for f in all_files if f.endswith(".json")]

        if not md_files:
            print(f"[ODL] ??? No markdown output. Files in tmp: {all_files}")
            return "", []

        with open(os.path.join(tmp_dir, md_files[0]), "r", encoding="utf-8") as f:
            content = f.read().strip()
        content = _fix_odl_rtl_line_order(content)
        print(f"[ODL] ??? {len(content)} chars extracted")

        if json_files:
            try:
                blocks = parse_odl_blocks(os.path.join(tmp_dir, json_files[0]))
            except Exception as e:
                print(f"[ODL] ??? Block parsing failed: {e}")

        return content, blocks


# ?????? Main pipeline ???????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

def _parse_type_line(text: str, default: str = "other") -> str:
    """Extract the classification token from a model's `TYPE: <type>` line.

    Falls back to `default` (and logs it) if the model emitted an empty or
    whitespace-only TYPE line instead of a real classification -- indexing
    split()[0] on that would otherwise raise IndexError and abort the job.
    """
    tokens = text.replace("TYPE:", "").strip().lower().replace(" ", "_").replace("-", "_").split()
    if not tokens:
        print(f"[Pipeline] Model returned empty TYPE: line, falling back to '{default}'")
        return default
    return tokens[0]



# ?????? Out Patient Slip barcode/QR extraction ??????????????????????????????????????????????????????????????????????????????
# Hospital-issued out-patient drug slips carry 5 machine-readable codes whose
# values a vision-language model cannot actually decode -- it can only OCR
# human-readable text printed near a code, not the underlying bit pattern.
# Benchmarked at 0/7 correct reads against zxing-cpp ground truth (see
# test_barcode_benchmark.py). zxing-cpp already ships as a dependency (used
# elsewhere in this file for barcode-separator detection in
# detect_separator_groups), so this doc type's 5 barcode/QR fields are filled
# from a real decode instead of a VLM guess.
_OUT_PATIENT_SLIP_TYPE_NAME = "out patient slip"
_OUT_PATIENT_PRESCRIPTION_TYPE_NAME = "out-patient prescription"
_OUT_PATIENT_DRUG_LIST_TYPE_NAME = "out-patient drug list"
# Exact-case DocumentType.name strings, as configured in the Doc Type Manager
# -- used for forced_document_type, which is set verbatim with no case
# normalization (see process_document()'s "if forced_document_type:" branch),
# unlike the lowercase constants above which are only used for matching an
# already-resolved document_type string.
_OUT_PATIENT_PRESCRIPTION_DB_NAME = "Out-patient Prescription"
_OUT_PATIENT_DRUG_LIST_DB_NAME = "Out-patient Drug List"

# Empirically confirmed against a real merged sample (2026-09-10): the
# Code 128 "MRN Barcode" sticker does not reliably decode below ~450 DPI --
# some pages that decoded fine at 300 DPI for other codes silently dropped
# just the MRN barcode. 450 DPI decoded every code on every page tested.
_BARCODE_DECODE_DPI = 450


def _load_first_page_image(image_path: str):
    """Renders page 1 of a PDF at _BARCODE_DECODE_DPI, or opens an image file directly."""
    if os.path.splitext(image_path)[1].lower() == ".pdf":
        import fitz
        with fitz.open(image_path) as doc:
            page = doc[0]
            mat = fitz.Matrix(_BARCODE_DECODE_DPI / 72, _BARCODE_DECODE_DPI / 72)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return Image.open(image_path).convert("RGB")


def _decode_out_patient_page_codes(image_path: str) -> dict | None:
    """
    Shared low-level decode step for all three Out-Patient doc types (Slip,
    Prescription, Drug List) -- they're all variations of the same printed
    hospital form family and share the same code layout/semantics, just with
    different field_name spellings per doc type (handled by each caller).

    Decodes page 1 via zxing-cpp, dedupes literal duplicate detections (same
    format + same value -- some pages redundantly print an order reference
    as both a Code 39 barcode and a QR code), and classifies by format:
      - "code39": linear barcodes, sorted top-to-bottom. Case Number and the
        order-reference "Barcode 3" are always Code 39; the MRN Barcode is
        never Code 39, so no position/count ambiguity between them.
      - "code128": the MRN Barcode specifically -- always this format,
        distinct from every other code on the page, so identifying it needs
        no position heuristic at all (unlike qr_left/qr_right below).
      - "qr_left" / "qr_right": QR codes split by an absolute page-half zone
        (not by ranking them against each other -- a relative comparison
        silently breaks if the QR count changes for any reason). QR 4 is
        only ever found on the left half of the page, QR 5 only on the
        right (confirmed against a real production redeploy on 2026-09-08).
      - "qr_all": every QR code, position-agnostic -- for Prescription pages,
        which only ever carry at most one QR, so left/right doesn't apply.

    Returns None ONLY if zxing-cpp isn't installed -- a genuine "we can't
    determine anything" case where callers should leave the VLM's guess
    alone rather than overriding it with nothing. A page that decodes but
    has zero codes on it still returns the normal dict (all lists empty),
    since that's real information -- "confirmed absent", not "unknown" --
    and callers use it to authoritatively clear a field the VLM hallucinated
    a value for (see extract_out_patient_prescription_barcodes and friends).
    """
    try:
        import zxingcpp
    except ImportError:
        print("[Barcode] zxing-cpp not installed ??? skipping Out Patient barcode extraction")
        return None

    img = _load_first_page_image(image_path)
    detections = [r for r in zxingcpp.read_barcodes(img) if r.valid and r.text]
    if not detections:
        print("[Barcode] No barcodes/QR codes found on page 1 (confirmed absent)")
        return {"code39": [], "code128": [], "qr_left": [], "qr_right": [], "qr_all": []}

    def _center(r):
        p = r.position
        cx = (p.top_left.x + p.top_right.x + p.bottom_left.x + p.bottom_right.x) / 4
        cy = (p.top_left.y + p.top_right.y + p.bottom_left.y + p.bottom_right.y) / 4
        return cx, cy

    # Dedup keys on format + text + a coarse position bucket (10% of page
    # size) -- NOT on format + text alone. Some real forms print the same
    # order-reference value twice, redundantly, as two physically separate
    # stickers on opposite sides of the page (confirmed on a real sample,
    # 2026-09-10: two distinct QR codes 90% of the page width apart, both
    # encoding the same text). Deduping on value alone silently discarded
    # one of them; only near-identical position now collapses a detection,
    # which is what actually indicates the same physical sticker seen twice.
    w, h = img.size
    deduped = {}
    for r in detections:
        cx, cy = _center(r)
        pos_bucket = (round(cx / w, 1), round(cy / h, 1))
        key = (str(r.format), r.text, pos_bucket)
        if key not in deduped:
            deduped[key] = r
    detections = list(deduped.values())

    code39 = [r for r in detections if str(r.format) == "Code 39"]
    code128 = [r for r in detections if str(r.format) == "Code 128"]
    qr = [r for r in detections if "QR" in str(r.format)]
    code39.sort(key=lambda r: _center(r)[1])   # top -> bottom

    page_width = img.size[0]
    qr_left = [r for r in qr if _center(r)[0] < page_width / 2]
    qr_right = [r for r in qr if _center(r)[0] >= page_width / 2]

    # A page-half can legitimately contain more than one QR: some forms
    # redundantly print the order-reference value as a QR on BOTH sides, in
    # addition to the real "QR 5" ticket QR on the right (confirmed on a
    # real sample, 2026-09-10 -- page had a right-half QR duplicating
    # Barcode 3's value *and* the distinct ticket QR). Whichever QR's value
    # doesn't match an already-decoded Code 39/128 is the real, distinct
    # one, so it's sorted first without discarding the other candidate.
    known_values = {r.text for r in code39} | {r.text for r in code128}
    qr_left.sort(key=lambda r: r.text in known_values)
    qr_right.sort(key=lambda r: r.text in known_values)

    print(f"[Barcode] Decoded {len(detections)} code(s) on page 1: "
          f"{len(code39)} Code39, {len(code128)} Code128, {len(qr)} QR")
    return {"code39": code39, "code128": code128, "qr_left": qr_left, "qr_right": qr_right, "qr_all": qr}


def extract_out_patient_slip_barcodes(image_path: str) -> dict:
    """
    Decodes case_number, mrn, barcode_3, qr_4, qr_5 for "Out Patient Slip".
    Every one of these 5 keys is always present in the result, with value
    None when that code is confirmed absent from the page -- the caller
    uses this to authoritatively clear a field instead of leaving whatever
    the VLM guessed (these fields are never something a VLM can read
    correctly to begin with; a wrong guess is worse than an honest blank).
    Returns {} only when zxing-cpp itself isn't installed.
    """
    codes = _decode_out_patient_page_codes(image_path)
    if codes is None:
        return {}
    result = {
        "case_number": codes["code39"][0].text if len(codes["code39"]) >= 1 else None,
        "mrn":         codes["code128"][0].text if codes["code128"] else None,
        "barcode_3":   codes["code39"][-1].text if len(codes["code39"]) >= 2 else None,
        "qr_4":        codes["qr_left"][0].text if codes["qr_left"] else None,
        "qr_5":        codes["qr_right"][0].text if codes["qr_right"] else None,
    }
    print(f"[Barcode] Out Patient Slip -> {result}")
    return result


def extract_out_patient_drug_list_barcodes(image_path: str) -> dict:
    """Decodes case_no_barcode_1, mrn_barcode_barcode, barcode_3, qr_4, qr_5
    for "Out-patient Drug List" -- same code layout/None-means-confirmed-
    absent contract as extract_out_patient_slip_barcodes, different
    field_name spellings."""
    codes = _decode_out_patient_page_codes(image_path)
    if codes is None:
        return {}
    result = {
        "case_no_barcode_1":   codes["code39"][0].text if len(codes["code39"]) >= 1 else None,
        "mrn_barcode_barcode": codes["code128"][0].text if codes["code128"] else None,
        "barcode_3":           codes["code39"][-1].text if len(codes["code39"]) >= 2 else None,
        "qr_4":                codes["qr_left"][0].text if codes["qr_left"] else None,
        "qr_5":                codes["qr_right"][0].text if codes["qr_right"] else None,
    }
    print(f"[Barcode] Out-patient Drug List -> {result}")
    return result


def extract_out_patient_prescription_barcodes(image_path: str) -> dict:
    """
    Decodes mrn_barcode, qr_5 for "Out-patient Prescription" -- these pages
    only ever carry the MRN Barcode and (sometimes) a single QR, no Case
    Number/Barcode 3/QR 4, so left/right zoning doesn't apply.

    QR 5 is genuinely optional on this doc type (some samples have no QR
    sticker at all) -- when confirmed absent, this reports qr_5: None so
    the caller clears the field, rather than leaving a VLM guess in place.
    A real bug: the VLM was observed returning the barcode's own printed
    queue number (e.g. "184") as its QR 5 guess on a page with no QR at
    all, because that digit string appears elsewhere on the page next to
    the barcode -- same class of hallucination the barcode-decode override
    exists to eliminate everywhere else.
    """
    codes = _decode_out_patient_page_codes(image_path)
    if codes is None:
        return {}
    result = {
        "mrn_barcode": codes["code128"][0].text if codes["code128"] else None,
        "qr_5":        codes["qr_all"][0].text if codes["qr_all"] else None,
    }
    print(f"[Barcode] Out-patient Prescription -> {result}")
    return result


# Maps a barcode-typed field's expected symbology (stored in the
# DocumentField.aliases column -- repurposed for barcode-type fields since
# "find-by labels" is meaningless for a field the AI never reads at all,
# same trick this codebase already uses to store table sub_columns inside
# field_label without a DB migration) to a predicate over zxing-cpp's
# str(r.format).
_BARCODE_FORMAT_MATCHERS = {
    "code128": lambda fmt: fmt == "Code 128",
    "code39":  lambda fmt: fmt == "Code 39",
    "qr":      lambda fmt: "QR" in fmt,
    "any":     lambda fmt: True,
}


def extract_typed_barcode_fields(image_path: str, barcode_fields: list) -> dict:
    """
    Generic Barcode/QR field-type extraction for any document type -- NOT
    the 3 hand-tuned Out-patient functions above, which encode more precise
    format+position disambiguation specific to that one form family (e.g.
    "QR 4 is always the left-half QR, QR 5 always right-half"). This is the
    declarative version: a Doc Type Manager field marked type "Barcode / QR
    Code" carries an optional expected-symbology dropdown (Any / Code 128 /
    Code 39 / QR), and disambiguation is format-only -- each field claims
    the first not-yet-claimed decoded code matching its expected format.
    Two fields on the same doc type sharing the same expected format (e.g.
    two plain "QR" fields) will be assigned in whatever order zxing-cpp
    happened to detect them, since there's no position hint to break the
    tie -- a doc type needing that precision should get its own dedicated
    function instead, the way the Out-patient family does.

    barcode_fields: list of DocumentField dicts (from get_custom_doc_fields)
    already filtered to field_type == "barcode".

    Every field_name is always present in the result (value or None for
    confirmed-absent) -- there is deliberately no AI/OCR fallback for these
    fields. A vision model cannot algorithmically decode a barcode/QR bit
    pattern; letting it guess is exactly the hallucination this field type
    exists to eliminate (proven at 0/7 correct reads in an earlier
    benchmark -- see test_barcode_benchmark.py), so an honest blank beats a
    plausible-looking wrong value.
    """
    try:
        import zxingcpp
    except ImportError:
        print("[Barcode] zxing-cpp not installed ??? skipping typed-barcode-field extraction")
        return {f["field_name"]: None for f in barcode_fields}

    img = _load_first_page_image(image_path)
    detections = [r for r in zxingcpp.read_barcodes(img) if r.valid and r.text]

    claimed = set()
    result = {}
    for f in barcode_fields:
        expected = (f.get("aliases") or "any").strip().lower()
        matcher = _BARCODE_FORMAT_MATCHERS.get(expected, _BARCODE_FORMAT_MATCHERS["any"])
        match_idx = next(
            (i for i, r in enumerate(detections) if i not in claimed and matcher(str(r.format))),
            None,
        )
        if match_idx is not None:
            claimed.add(match_idx)
            result[f["field_name"]] = detections[match_idx].text
        else:
            result[f["field_name"]] = None

    print(f"[Barcode] Typed-field extraction -> {result}")
    return result


def map_barcode_keys_to_schema(barcode_fields: dict, custom_fields: list) -> dict:
    """
    Re-key a hand-tuned extractor's result (extract_out_patient_*_barcodes,
    whose key names are hardcoded, e.g. "qr_5") onto the field_names the
    document type actually defines. Names are compared with case and
    punctuation stripped, so the extractor's "qr_5" lands on a user-created
    "QR5" field (stored as "qr5") instead of being saved as a separate field
    the user never created. A key with no counterpart in the schema is left
    out of the result. Values pass through unchanged (None = confirmed absent).

    Matching is tried on field_name first, then field_label as a fallback so
    that a doc type synced from iVault (where _snake_case("QR 5") yields
    "q_r_5") still matches the extractor's hardcoded "qr_5" key via the
    shared normalised form "qr5" -- but so does a field whose name was
    generated completely differently (e.g. "qr_code_5" -> "qrcode5" would
    miss by name, yet its label "QR 5" -> "qr5" still lands the value).
    """
    def _key(s):
        return re.sub(r'[^a-z0-9]', '', (s or "").lower())

    # field_name takes priority; field_label fills in keys that name alone misses
    schema_by_key = {}
    for f in custom_fields:
        k = _key(f["field_name"])
        if k and k not in schema_by_key:
            schema_by_key[k] = f["field_name"]
    for f in custom_fields:
        k = _key(f.get("field_label", ""))
        if k and k not in schema_by_key:
            schema_by_key[k] = f["field_name"]

    mapped = {}
    for src_name, val in barcode_fields.items():
        target = schema_by_key.get(_key(src_name))
        if target:
            mapped[target] = val
        else:
            print(f"[Barcode] Decoded '{src_name}' has no matching field in this document type's schema - not saved")
    return mapped


def process_document(image_path: str, forced_document_type: str = None) -> dict:
    """
    Unified pipeline for ALL file types (images and PDFs):

    PDF path:
      1. PyMuPDF renders pages ??? JPEG at 300 DPI (in memory)
      2. Qwen Vision           ??? OCR text + classify doc type (per page)
      3. RAG                   ??? retrieve similar past docs
      4. Qwen LLM              ??? extract structured fields

    Image path:
      1a. Qwen Vision (512px)  ??? quick classify from thumbnail
      1b. Qwen Vision (1024px) ??? OCR full image with targeted prompt
      2.  Qwen LLM             ??? confirm classification from text
      3-4. same as above

    If forced_document_type is set, OCR still runs as usual but the detected
    document type is overridden ??? useful when the caller already knows the
    target type (e.g. a synced iVault record type) and wants field extraction
    to target that type's field list directly.
    """
    t_start = time.time()
    # Fresh per-run cache for get_custom_doc_fields/get_all_active_doc_type_names
    # -- run_queue() in document_routes.py processes multiple files sequentially
    # on the same background thread, so this must be reset (not just lazily
    # populated) on every call, or a later document could reuse an earlier
    # document's cached DB results.
    _doc_query_cache_start()
    ext    = os.path.splitext(image_path)[1].lower()
    is_pdf = ext == ".pdf"

    # Track image data for direct Vision extraction
    image_b64 = None
    media_type = None
    odl_blocks = []
    scanned_pages = []  # pages from scanned PDF OCR ??? used for bbox extraction
    # Set by the single-page merged OCR+fields path (see
    # classify_and_extract_combined_vision) when it returns fields it trusts
    # -- Step 3 below uses these directly instead of calling
    # extract_fields_vision() a second time, which is the whole point of the
    # merge (collapses 2 full Vision passes into 1 tiny + 1 full pass).
    _precomputed_fields = None

    if is_pdf:
        # ?????? Step 1: Quick scanned check before running ODL (saves JVM startup time) ???
        print("[Pipeline] Step 1: PDF extraction...")
        t_ocr = time.time()

        odl_blocks = []

        if is_scanned_pdf(image_path):
            print("[Pipeline] PyMuPDF: no text layer detected ??? skipping ODL, routing directly to Vision OCR")
            raw_text = ""
        else:
            try:
                raw_text, odl_blocks = extract_pdf_text_with_odl(image_path)
            except Exception as odl_err:
                print(f"[Pipeline] ??? ODL failed: {odl_err}")
                raw_text = ""

            # If ODL failed or returned nothing, fall back to pdfplumber+PyMuPDF
            if not raw_text.strip():
                print("[Pipeline] ODL returned empty ??? falling back to pdfplumber+PyMuPDF...")
                try:
                    raw_text = extract_pdf_text_with_tables(image_path)
                    print(f"[Pipeline] pdfplumber fallback: {len(raw_text)} chars")
                except Exception as fb_err:
                    print(f"[Pipeline] ??? pdfplumber fallback failed: {fb_err}")
                    raw_text = ""

        print(f"[Pipeline] Extracted: {len(raw_text)} chars ({time.time()-t_ocr:.1f}s)")

        # ?????? Detect: Scanned vs Normal/Hybrid PDF ?????????????????????????????????????????????????????????????????????????????????????????????????????????
        # Hybrid PDFs (e.g. scanned with thin text layer) have >100 chars but no real values.
        # Treat as scanned if: total text < 500 chars, OR fewer than 3 lines contain actual values.
        # Also treat as scanned if ODL embedded image markers (![image N]) ??? means pages are image-based.
        text_lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
        value_lines = [l for l in text_lines if len(l) > 3 and not l.endswith(":") and not l.endswith(":.")]
        odl_has_images = bool(re.search(r'!\[image\s*\d*\]', raw_text))
        is_scanned = len(raw_text) < 500 or len(value_lines) < 3 or odl_has_images
        if odl_has_images:
            print("[Pipeline] ODL detected image-based pages ??? routing to Vision OCR")
            raw_text = ""  # discard garbage OCR text, Vision will re-extract cleanly
        print(f"[Pipeline] PDF Type: {'SCANNED/HYBRID' if is_scanned else 'NORMAL'} "
              f"({len(raw_text)} chars, {len(value_lines)} value lines)")

        if is_scanned:
            # ?????? SCANNED PDF PATH: Single Vision call ??? classify + OCR together ??????
            print("[Pipeline] Step 2: Scanned PDF detected ??? combined classify + OCR (single Vision call)...")

            _known_types_str = ", ".join(get_all_active_doc_type_names())
            combined_prompt = (
                "Look at this scanned document image.\n"
                f"1. What specific type of document is it? Known types (prefer if match): {_known_types_str}\n"
                "   If none match, provide a precise snake_case type. Reply in English only.\n"
                "2. Transcribe ALL visible text exactly as it appears, line by line.\n"
                "   For tables: output a header row first, then each data row on its own line with columns separated by | (pipe).\n"
                "   Example table output:\n"
                "   Item Code | Description | Qty | Unit Price | Amount\n"
                "   ABC123 | Widget A | 2 | 50.00 | 100.00\n"
                "   Transcribe ALL rows ??? do NOT stop early or summarise.\n\n"
                "Reply in this EXACT format:\n"
                "TYPE: <document_type_in_snake_case>\n"
                "TEXT:\n<all transcribed text here>"
            )

            ocr_done = False
            document_type = "other"
            confidence = normalize_confidence(None, source="fallback")

            # OCR-only prompt for pages 2+ (type already known from page 1)
            ocr_only_prompt = (
                "Transcribe ALL visible text from this document page exactly as it appears, line by line.\n"
                "For tables: each row on its own line with columns separated by spaces.\n"
                "Output ONLY the transcribed text, nothing else."
            )

            for dpi_try, dim_try in [(150, 1200), (100, 900), (72, 700), (72, 512), (72, 384)]:
                try:
                    ocr_pages = pdf_pages_to_b64(image_path, dpi=dpi_try, max_dim=dim_try)
                    if not ocr_pages:
                        continue

                    _n_pages = max(len(ocr_pages), 1)
                    _ocr_per_page = 1.5 / _n_pages
                    _mark_page_done(0, _n_pages)

                    if _n_pages == 1:
                        # Single-page fast path -- collapses classify+OCR (this
                        # loop's normal job) AND the separate field-extraction
                        # call later in this function into 1 tiny classify call
                        # + 1 full combined pass instead of 2 full passes,
                        # cutting ~20-40s off single-page scanned-document
                        # processing. Multi-page documents (the `else` branch
                        # below, unchanged) still need OCR to finish first
                        # before field extraction, so they keep the old flow.
                        _set_lm_slot("Classifying + extracting...", 0.0, _ocr_per_page)
                        _combined = classify_and_extract_combined_vision(
                            ocr_pages[0][0], "image/jpeg", forced_document_type=forced_document_type,
                        )
                        document_type = _combined["document_type"]
                        page1_text = _combined["raw_text"]
                        if _combined["fields_reliable"]:
                            _precomputed_fields = _combined
                        _mark_page_done(1, 1)
                    else:
                        # Page 1: classify + OCR together
                        _set_lm_slot(f"OCR page 1/{_n_pages}...", 0.0, _ocr_per_page)
                        result = call_qwen_vision(ocr_pages[0][0], "image/jpeg", combined_prompt, max_tokens=16000)
                        result = clean_qwen_output(result)
                        print(f"[Pipeline] Page 1 classify+OCR at {dpi_try} DPI: {len(result)} chars")

                        # Parse TYPE and TEXT from page 1 response
                        page1_text = result
                        if "TYPE:" in result and "TEXT:" in result:
                            type_part = result.split("TEXT:")[0]
                            page1_text = result.split("TEXT:", 1)[1].strip()
                            document_type = _parse_type_line(type_part, default="other")
                        elif "TYPE:" in result:
                            lines = result.split("\n", 1)
                            document_type = _parse_type_line(lines[0], default="other")
                            page1_text = lines[1].strip() if len(lines) > 1 else result

                    # Pages 2+: OCR only ??? bounded concurrency (_PAGE_CONCURRENCY).
                    # The actual call_qwen_vision() calls run on worker threads with
                    # NO progress-reporting calls inside them: _progress_ctx (see
                    # _init_lm_progress) is thread-local, populated only on the
                    # thread that called process_document(), so a _set_lm_slot()
                    # call made from inside a worker thread would silently no-op.
                    # Progress is instead reported from THIS thread as each future
                    # completes (via as_completed, true completion order) rather
                    # than in submission/page order, since pages can now finish
                    # out of sequence.
                    remaining_pages = list(enumerate(ocr_pages[1:], start=2))
                    page_results = {}

                    def _ocr_one_page(page_b64, page_mt):
                        text = call_qwen_vision(page_b64, page_mt, ocr_only_prompt, max_tokens=16000)
                        return clean_qwen_output(text)

                    if remaining_pages:
                        with ThreadPoolExecutor(max_workers=_PAGE_CONCURRENCY, thread_name_prefix="page-ocr") as _page_pool:
                            futures = {
                                _page_pool.submit(_ocr_one_page, page_b64, page_mt): page_num
                                for page_num, (page_b64, page_mt, _pw, _ph) in remaining_pages
                            }
                            pages_completed = 0
                            for fut in as_completed(futures):
                                page_num = futures[fut]
                                pages_completed += 1
                                try:
                                    page_results[page_num] = fut.result()
                                    print(f"[Pipeline] Page {page_num} OCR: {len(page_results[page_num])} chars")
                                except Exception as page_err:
                                    print(f"[Pipeline] Page {page_num} OCR failed: {page_err}")
                                _set_lm_slot(
                                    f"OCR page {pages_completed + 1}/{_n_pages}...",
                                    _ocr_per_page * pages_completed,
                                    _ocr_per_page * (pages_completed + 1),
                                )

                    all_pages_text = [page1_text] + [
                        page_results[page_num] for page_num, _ in remaining_pages if page_num in page_results
                    ]

                    raw_text = "\n\n".join(all_pages_text)
                    confidence = normalize_confidence("high", source="vision_thumb")
                    scanned_pages = ocr_pages  # save for bbox extraction later
                    print(f"[Pipeline] Classified as: {document_type} | Total OCR: {len(raw_text)} chars ({len(ocr_pages)} pages)")
                    ocr_done = True
                    break
                except Exception as ocr_err:
                    print(f"[Pipeline] Combined call at {dpi_try} DPI failed: {ocr_err}, trying lower resolution...")

            if not ocr_done:
                print("[Pipeline] All Vision OCR attempts failed ??? proceeding with empty text")
                raw_text = raw_text or ""

            # Keep the first page image available so field extraction can use
            # Vision directly (same as the JPG path) instead of relying solely
            # on OCR text quality ??? critical for scanned PDFs with small text.
            if scanned_pages:
                image_b64, media_type = scanned_pages[0][0], scanned_pages[0][1]
            else:
                image_b64 = None
                media_type = None

            if forced_document_type:
                document_type = forced_document_type
                print(f"[Pipeline] Forced document type: {document_type}")

        else:
            # ?????? NORMAL PDF PATH: Use text extraction + text model ??????????????????????????????????????????????????????????????????
            if forced_document_type:
                document_type = forced_document_type
                confidence = normalize_confidence("high", source="forced")
                print(f"[Pipeline] Step 2: Forced document type: {document_type} (skipping classification)")
            else:
                print("[Pipeline] Step 2: Normal PDF detected ??? classifying from extracted text...")

                _set_lm_slot("Classifying...", 0.0, 1.5)
                cls_result = classify_document(raw_text)
                document_type = cls_result.get("document_type", "other")
                confidence = normalize_confidence(cls_result.get("confidence"), source="text")
                print(f"[Pipeline] Classified as: {document_type}")

    else:
        # ?????? Image: classify + combined OCR/fields (single-page fast path) ??
        # Collapses today's [classify+OCR call] + [separate extract_fields_vision()
        # call] into 1 tiny classify call + 1 combined pass (see
        # classify_and_extract_combined_vision) -- images are always exactly
        # one page, so this always applies here, cutting ~20-40s per document.
        print("[Pipeline] Step 1: Classify + combined OCR/fields (single-page fast path)...")
        _combined = None
        image_b64 = None
        media_type = None
        _set_lm_slot("Classifying + extracting...", 0.0, 2.0)
        for _sz, _q in [(1024, 85), (768, 75), (512, 70), (384, 60)]:
            try:
                b64, mt, _, _ = encode_image(image_path, max_size=_sz, quality=_q)
                print(f"[Pipeline] Trying vision at {_sz}px q={_q} ({len(b64)//1024}KB)...")
                _combined = classify_and_extract_combined_vision(b64, mt, forced_document_type=forced_document_type)
                image_b64 = b64
                media_type = mt
                break
            except openai.APIStatusError as _e:
                # A real HTTP response from the server (as opposed to a
                # connection/timeout failure, which call_qwen_vision already
                # retries internally and surfaces as QwenServiceUnavailableError
                # if still failing) -- only this class of error plausibly means
                # "image too large/invalid at this resolution", so only these
                # are worth silently retrying at a lower resolution. Anything
                # else (including QwenServiceUnavailableError) propagates
                # immediately instead of being masked as a resolution issue.
                err = str(_e)
                if "400" in err or "failed to process image" in err.lower():
                    print(f"[Pipeline] Vision rejected at {_sz}px, retrying smaller...")
                    continue
                raise
        if _combined is None:
            raise RuntimeError("Image could not be processed at any resolution")

        document_type = _combined["document_type"]
        raw_text = _combined["raw_text"]
        if _combined["fields_reliable"]:
            _precomputed_fields = _combined

        if forced_document_type:
            print(f"[Pipeline] Forced document type: {document_type}")
        else:
            print(f"[Pipeline] Classified as: {document_type}")
        print(f"[Pipeline] Total extracted: {len(raw_text)} characters")

        confidence = normalize_confidence("high", source="vision_full")

    print(f"[Pipeline] Total extracted: {len(raw_text)} characters")
    print(f"[Pipeline] Type: {document_type} | Confidence: {confidence}")

    # Step 2.5: Detect language ??? all non-English documents are extracted in their original language.
    detected_lang = detect_language(raw_text)
    extraction_text = raw_text
    if detected_lang != "english":
        print(f"[Pipeline] Step 2.5: {detected_lang} detected ??? extracting in original language (no translation)")
    else:
        print(f"[Pipeline] Step 2.5: English detected")

    # Step 3: Extract fields
    print(f"[Pipeline] Step 3: Extracting fields ({document_type})...")
    _extract_start = 2.0 if (not is_pdf or image_b64) else 1.5
    _set_lm_slot("Extracting fields...", _extract_start, 3.5)

    # For images: use vision extraction (reads image directly for best accuracy)
    # For PDFs: always use text model
    # All non-English documents: keys+values stay in original language
    if image_b64 and media_type:
        if _precomputed_fields is not None:
            # Reuse the fields the single-page combined call already produced
            # instead of a second Vision pass -- this is the actual speed win
            # from classify_and_extract_combined_vision(); only reached when
            # that call's fields already passed the null_ratio reliability
            # check, so no extra correctness risk versus calling it again here.
            fields = _precomputed_fields["fields"]
            print(f"[Pipeline] Using precomputed fields from combined call ({len(fields)} fields)")
        else:
            fields = extract_fields_vision(image_b64, media_type, document_type, raw_text=extraction_text, source_lang=detected_lang)
            if not fields:
                print(f"[Pipeline] Vision extraction failed, falling back to text extraction...")
                fields = extract_fields(extraction_text, document_type, source_lang=detected_lang)
        # Post-process: enforce custom table column structure for image documents
        _custom = get_custom_doc_fields(document_type)
        if _custom and fields:
            for _f in _custom:
                if _f["field_type"] != "table":
                    continue
                _fn   = _f["field_name"]
                _cols = _f.get("sub_columns") or []
                if not _cols or _fn not in fields:
                    continue
                _rows = fields[_fn]
                if not isinstance(_rows, list):
                    continue
                _enforced = []
                for _row in _rows:
                    if not isinstance(_row, dict):
                        continue
                    _row_lower = {k.lower().strip(): v for k, v in _row.items()}
                    _enforced.append({
                        col: (_row.get(col) if col in _row else _row_lower.get(col.lower().strip()))
                        for col in _cols
                    })
                fields[_fn] = _enforced
                print(f"[Pipeline] Enforced {len(_cols)} columns on '{_fn}': {_cols}")
    else:
        if detected_lang != "english":
            print(f"[Pipeline] {detected_lang} document ??? extracting in original language")
        fields = extract_fields(extraction_text, document_type, source_lang=detected_lang)

    # For a custom (Settings-defined) type, keep extraction faithful to the
    # defined fields. Do NOT wipe a mostly-null result and fall back to generic
    # Vision extraction ??? that leaks arbitrary fields (e.g. raw_text, total_amount)
    # the user never defined. A mostly-null custom result just means the document
    # lacks those values; show them as null and let prompt iteration improve it.
    _custom_defs = get_custom_doc_fields(document_type)
    field_sources = {}   # per-field detection source for UI feedback
    # {field_name: {"model_value":, "alias_value":, "alias_label":}} - only
    # populated when an alias match OVERRIDES a non-empty model answer (see
    # Pass 0/3 below), not when it merely fills a blank field. Surfaced by
    # _save_document_to_db as a field_ambiguities candidate so the user can
    # pick the model's own answer back instead of the alias silently winning.
    field_alias_conflicts = {}

    # ?????? Vision fallback for PDFs with empty extraction (hybrid/sparse PDFs) ?????????
    # Also trigger if >80% of fields are null (text extraction got labels but no values)
    if fields and not _custom_defs:
        null_ratio = sum(1 for v in fields.values() if v is None) / len(fields)
        if null_ratio > 0.8:
            print(f"[Pipeline] ??? {round(null_ratio*100)}% of fields are null ??? text extraction got labels but no values")
            fields = {}
    if not fields and is_pdf and not _custom_defs and document_type not in DOCUMENT_STRUCTURE_HINTS:
        # A type with no defined fields at all (no custom schema AND not a
        # DOCUMENT_STRUCTURE_HINTS built-in) can never produce fields via
        # extract_fields_vision() either -- it has this exact same
        # zero-template early-return before making any Vision call. Skipping
        # the retry entirely here avoids uselessly re-rendering every page of
        # the PDF at 3 different DPIs for a result that was guaranteed empty
        # from the first attempt -- confirmed as real wasted time on a dense
        # multi-page, unclassified document in production (2026-08-21).
        print(f"[Pipeline] '{document_type}' has no defined fields ??? skipping Vision fallback retry (would be a no-op)")
    elif not fields and is_pdf and not _custom_defs:
        print("[Pipeline] ??? No fields from text extraction ??? retrying with Vision (hybrid PDF)...")
        _set_lm_slot("Extracting fields (Vision)...", _get_current_step(), 3.5)
        for dpi_try, dim_try in [(150, 1200), (100, 900), (72, 700)]:
            try:
                pages = pdf_pages_to_b64(image_path, dpi=dpi_try, max_dim=dim_try)
                if not pages:
                    continue
                fb_b64, fb_mt, _, _ = pages[0]
                fields = extract_fields_vision(fb_b64, fb_mt, document_type, raw_text=raw_text)
                if fields:
                    print(f"[Pipeline] ??? Vision fallback extracted {len(fields)} fields at {dpi_try} DPI")
                    confidence = normalize_confidence("high", source="vision_full")
                    break
                print(f"[Pipeline] Vision fallback at {dpi_try} DPI returned empty, trying lower...")
            except Exception as vfb_err:
                print(f"[Pipeline] Vision fallback at {dpi_try} DPI failed: {vfb_err}, trying lower...")

    # Guarantee a custom type exposes exactly its defined fields (in order),
    # filling any the model missed with null ??? and never extra/leaked keys.
    if _custom_defs:
        _allowed = [f["field_name"] for f in _custom_defs]
        # clean_field_names() converts underscore keys ??? space keys inside extract_fields_vision,
        # so look up both "invoice_number" and "invoice number" to avoid dropping AI results.
        fields = {
            fn: (fields.get(fn) or fields.get(fn.replace("_", " ")))
            if isinstance(fields, dict) else None
            for fn in _allowed
        }
        print(f"[Pipeline] Custom type ??? constrained output to defined fields: {_allowed}")

        # Deterministic date cleanup/fallback for ANY extraction path (text or
        # vision). Handles numeric (27-12-2017) and month-name (05.Dec.2017)
        # dates: strip junk from a value, or fill the first date in the document.
        import re as _re_dt
        _DT_RE = _re_dt.compile(r'\b\d{1,2}[-/.\s](?:\d{1,2}|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-/.\s]\d{2,4}\b', _re_dt.IGNORECASE)
        _dtypes = {f["field_name"]: f.get("field_type") for f in _custom_defs}
        # Record HOW each field was filled, for UI feedback: "model" / "date pattern"
        # / "alias 'X'" / "not detected". Anything the model already returned = model.
        for _fn in _allowed:
            if fields.get(_fn) not in (None, "", []):
                field_sources[_fn] = "model"

        for _fn in _allowed:
            if _dtypes.get(_fn) != "date":
                continue
            _v = fields.get(_fn)
            if _v:
                _m = _DT_RE.search(str(_v))
                if _m and _m.group(0) != str(_v).strip():
                    fields[_fn] = _m.group(0)
                    print(f"[Pipeline] Date cleanup: '{_fn}' -> {_m.group(0)}")
            else:
                _m = _DT_RE.search(raw_text or "")
                if _m:
                    fields[_fn] = _m.group(0)
                    field_sources[_fn] = "date pattern"
                    print(f"[Pipeline] Date fallback: '{_fn}' = {_m.group(0)} (first date in document)")

        # Deterministic label-based fallback for empty SCALAR fields (text/number/
        # currency). Finds "Label: value" in the document so key fields like the
        # invoice number populate even when the model returns nothing.
        #
        # ODL outputs markdown with pipe-separated tables. Two common formats:
        #   3-col: "| INVOICE NO | : | INV18010097 |"  ??? after norm: "INVOICE NO : INV18010097"
        #   2-col: "| INVOICE NO | INV18010097 |"      ??? after norm: "INVOICE NO  INV18010097"
        # Normalize pipes ??? spaces so both formats become searchable plain text.
        _alias_search_text = _re_dt.sub(r'\s*\|\s*', '  ', raw_text or "")
        for _f in _custom_defs:
            _fn = _f["field_name"]
            _user_aliases = [a.strip() for a in (_f.get("aliases") or "").split(",") if a.strip()]
            if _dtypes.get(_fn) in ("date", "table"):
                continue

            _ai_val = fields.get(_fn)
            _value_pat = r'([A-Za-z0-9][\w\-/]{2,40})'

            # When the AI returned a blob (long value) and aliases are set:
            # try to rescue the correct value without running Pass 1/2, which
            # misfire on reversed-column layouts ("Label:" follows its value).
            if _ai_val and _user_aliases and isinstance(_ai_val, str) and len(_ai_val) > 40:
                # Pass 0: alias label inside the AI blob itself
                # ("Invoice Ref.: MYEX..." sometimes appears in the blob verbatim)
                _blob_matched = None
                for _alias in _user_aliases:
                    try:
                        _bp = _re_dt.compile(
                            _re_dt.escape(_alias) + r'\s*[:#]\s*' + _value_pat,
                            _re_dt.IGNORECASE
                        )
                        _blob_matched = _bp.search(_ai_val)
                    except Exception:
                        pass
                    if _blob_matched:
                        # The alias match wins as the displayed value (unchanged
                        # behavior), but the model's own long answer is a real
                        # second candidate, not noise - keep it so the
                        # ambiguity picker can offer it back instead of just
                        # discarding it.
                        field_alias_conflicts[_fn] = {
                            "model_value": _ai_val, "alias_value": _blob_matched.group(1), "alias_label": _alias,
                        }
                        fields[_fn] = _blob_matched.group(1)
                        field_sources[_fn] = "alias '" + _alias + "' (from AI result)"
                        print(f"[Pipeline] Alias in blob: '{_fn}' = {_blob_matched.group(1)}")
                        break

                # Pass 3 (blob fallback): value appears BEFORE the label in the raw
                # text ??? reversed column layout. Find the alias label position, then
                # take the last mixed-alphanumeric code that precedes it.
                if not _blob_matched:
                    for _alias3 in _user_aliases:
                        try:
                            _a3_esc = _re_dt.escape(_alias3.strip())
                            _lpos = _re_dt.search(_a3_esc + r'[:#\s]', _alias_search_text, _re_dt.IGNORECASE)
                            if _lpos:
                                _before = _alias_search_text[:_lpos.start()]
                                _ref_codes = _re_dt.findall(
                                    r'[A-Za-z][A-Za-z0-9]{2,}[0-9][A-Za-z0-9\-/]*'
                                    r'|[0-9]{2,}[A-Za-z][A-Za-z0-9\-/]{2,}',
                                    _before
                                )
                                if _ref_codes:
                                    field_alias_conflicts[_fn] = {
                                        "model_value": _ai_val, "alias_value": _ref_codes[-1], "alias_label": _alias3.strip(),
                                    }
                                    fields[_fn] = _ref_codes[-1]
                                    field_sources[_fn] = "alias '" + _alias3.strip() + "' (reversed)"
                                    print(f"[Pipeline] Reversed layout: '{_fn}' = {_ref_codes[-1]}")
                                    break
                        except Exception:
                            pass
                # Skip Pass 1/2 ??? they misfire on reversed layouts
                continue

            # Skip raw-text search if the AI already returned a clean (short) value
            if _ai_val and not _user_aliases:
                continue
            if _ai_val and _user_aliases and len(str(_ai_val)) <= 40:
                continue

            # Labels to search = field label + name + any user-defined aliases
            _labels = [_f.get("field_label"), _fn.replace("_", " ")]
            _labels += _user_aliases
            for _lab in _labels:
                if not _lab:
                    continue
                _escaped = _re_dt.escape(_lab.strip())
                matched = None
                # Pass 1: label followed by colon or hash separator (most reliable)
                try:
                    _pat = _re_dt.compile(_escaped + r'\s*[:#]\s*' + _value_pat, _re_dt.IGNORECASE)
                    matched = _pat.search(_alias_search_text)
                except Exception:
                    pass
                # Pass 2: label followed by 2+ spaces/tabs only (2-col table, no colon column)
                if not matched:
                    try:
                        _pat2 = _re_dt.compile(_escaped + r'[ \t]{2,}' + _value_pat, _re_dt.IGNORECASE)
                        matched = _pat2.search(_alias_search_text)
                    except Exception:
                        pass
                if matched:
                    fields[_fn] = matched.group(1)
                    field_sources[_fn] = "alias '" + _lab.strip() + "'"
                    print(f"[Pipeline] Label fallback: '{_fn}' = {matched.group(1)} (alias '{_lab.strip()}')")
                    break

        # Vision fallback for PDFs: some values never make it into raw_text at
        # all (e.g. a bold two-column header block ODL's layout parser skips),
        # so no amount of text-based extraction/alias matching can find them.
        # Scoped to fields that already have a user-confirmed alias (added
        # automatically once someone corrects the field via chat) ??? asking the
        # vision model to guess at fields nobody has ever confirmed risks
        # fabricating a plausible-looking wrong answer instead of correctly
        # reporting "not detected". Barcode/QR-typed fields are excluded even
        # though their `aliases` column is set: it holds the expected symbology
        # there, not a label, and their value only ever comes from the barcode
        # decode further down - never a VLM guess.
        if is_pdf:
            _still_missing = [
                _f for _f in _custom_defs
                if _dtypes.get(_f["field_name"]) not in ("table", "barcode")
                and fields.get(_f["field_name"]) in (None, "", [])
                and (_f.get("aliases") or "").strip()
            ]
            if _still_missing:
                try:
                    _missing_by_name = {_f["field_name"]: _f for _f in _still_missing}
                    _vision_found = find_missing_fields_in_image(image_path, _still_missing)
                    for _fn, _val in _vision_found.items():
                        _mf = _missing_by_name.get(_fn, {})
                        if not is_plausible_field_value(
                            _dtypes.get(_fn), _val,
                            field_name=_fn, field_label=_mf.get("field_label", ""),
                        ):
                            print(f"[Pipeline] Vision fallback rejected implausible value for '{_fn}': {_val!r}")
                            continue
                        fields[_fn] = _val
                        field_sources[_fn] = "vision re-scan"
                        print(f"[Pipeline] Vision fallback: '{_fn}' = {_val}")
                except Exception as _vf_err:
                    print(f"[Pipeline] Vision fallback failed: {_vf_err}")

        # Final pass: mark anything still empty as not detected
        for _fn in _allowed:
            if fields.get(_fn) in (None, "", []):
                field_sources[_fn] = "not detected"

    _norm_doc_type = (document_type or "").strip().lower().replace("_", " ")
    _out_patient_barcode_extractors = {
        _OUT_PATIENT_SLIP_TYPE_NAME: extract_out_patient_slip_barcodes,
        _OUT_PATIENT_PRESCRIPTION_TYPE_NAME: extract_out_patient_prescription_barcodes,
        _OUT_PATIENT_DRUG_LIST_TYPE_NAME: extract_out_patient_drug_list_barcodes,
    }
    _bc_extractor = _out_patient_barcode_extractors.get(_norm_doc_type)
    if _bc_extractor:
        try:
            barcode_fields = _bc_extractor(image_path)
            # Authoritative for every key returned, including None (a
            # confirmed-absent code) -- these fields are never something a
            # VLM can read correctly, so a real decode's "absent" verdict
            # must overwrite a hallucinated guess, not just skip past it.
            # (e.g. the VLM was observed returning a barcode's printed
            # queue number as its "QR 5" guess on a page with no QR at all.)
            # These extractors use hardcoded key names ("qr_5"); re-key them onto
            # the fields this type defines ("qr5") so a decode fills the user's
            # field rather than being saved as a new one they never created.
            barcode_fields = map_barcode_keys_to_schema(barcode_fields, _custom_defs)
            for _bc_fn, _bc_val in barcode_fields.items():
                fields[_bc_fn] = _bc_val or ""
                field_sources[_bc_fn] = "barcode scan" if _bc_val else "not detected"
        except Exception as _bc_err:
            print(f"[Pipeline] {document_type} barcode decode failed: {_bc_err}")
    else:
        # Generic Barcode/QR field type (any OTHER doc type -- the 3 above
        # already get more precise, hand-tuned format+position logic, so
        # this deliberately doesn't run for them) -- covers any doc type
        # whose Doc Type Manager schema has fields marked type "barcode",
        # with no bespoke Python function needed per form.
        try:
            _barcode_typed_fields = [f for f in get_custom_doc_fields(document_type) if f.get("field_type") == "barcode"]
            if _barcode_typed_fields:
                barcode_fields = extract_typed_barcode_fields(image_path, _barcode_typed_fields)
                for _bc_fn, _bc_val in barcode_fields.items():
                    fields[_bc_fn] = _bc_val or ""
                    field_sources[_bc_fn] = "barcode scan" if _bc_val else "not detected"
        except Exception as _bc_err:
            print(f"[Pipeline] {document_type} typed-barcode-field extraction failed: {_bc_err}")

    elapsed = time.time() - t_start
    print(f"[Pipeline] Done! Type: {document_type} | Confidence: {confidence} | Total: {elapsed:.1f}s")

    if odl_blocks and is_pdf:
        # Digital PDF ??? normalize ODL bboxes (PDF points ??? 0-1)
        import fitz
        with fitz.open(image_path) as _doc:
            page_sizes = {
                i + 1: (_page.rect.width, _page.rect.height)
                for i, _page in enumerate(_doc)
            }
        normalized_blocks = []
        for block in odl_blocks:
            pw, ph = page_sizes.get(block["page"], (612, 792))
            normalized_blocks.append({
                "page": block["page"],
                "text": block["text"],
                "bbox": normalize_bbox_to_relative(block["bbox"], pw, ph),
            })
    else:
        # Scanned PDF / plain image ??? bbox blocks for the highlight overlay
        # used to cost a second Vision call per page on TOP of the OCR call
        # already made above (build_image_blocks re-sends the same page just
        # to ask for line-level bboxes). That doubled the vision-model calls
        # for every upload even though the highlight overlay is opened for
        # only a fraction of documents. Computed lazily now, on first request
        # to GET /<id>/blocks, and cached on the Document row from there ???
        # see compute_text_blocks_lazy() and document_routes.get_text_blocks().
        normalized_blocks = []

    return {
        "raw_text":      raw_text,
        "document_type": document_type,
        "confidence":    confidence,
        "fields":        fields,
        "field_sources": field_sources,
        "field_alias_conflicts": field_alias_conflicts,
        "text_blocks":   normalized_blocks,
    }


# ?????? Split-PDF pipeline ????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

def classify_single_page(page_b64: str, media_type: str) -> str:
    """
    Quick Vision classify of one rendered PDF page.
    Sends a tiny max_tokens=20 request ??? just enough to get the doc type word.
    Returns a snake_case doc type string (e.g. "invoice", "bank_statement").
    """
    known_types_str = ", ".join(get_all_active_doc_type_names())
    prompt = (
        "Look at this document page.\n"
        f"What type of document is it? Known types (use one if it matches): {known_types_str}\n"
        "If none match, give a precise snake_case type name.\n"
        "Reply with ONLY the document type in snake_case. Nothing else.\n"
        "Examples: invoice    bank_statement    receipt    utility_bill"
    )
    try:
        raw = call_qwen_vision(page_b64, media_type, prompt, max_tokens=20)
        # Normalise: lowercase, underscores, take the first word only
        cleaned = raw.strip().lower().replace(" ", "_").replace("-", "_")
        return cleaned.split()[0] if cleaned.split() else "other"
    except Exception as e:
        print(f"[Split] classify_single_page failed: {e}")
        return "other"


def is_new_document_boundary(prev_b64: str, prev_mt: str, cur_b64: str, cur_mt: str) -> bool:
    """
    Given two consecutive pages (prev, cur) ??? which may have been classified
    as the same or different document types ??? ask Vision to look at BOTH
    pages at once and decide whether `cur` starts a brand-new document or is
    a continuation/attachment of `prev`.

    This is more robust than comparing per-page extracted IDs or type labels:
    a scanned page 2 of a multi-page invoice often shows the SAME invoice
    number as page 1, but OCR/vision may transcribe it slightly differently
    each time it's read in isolation ??? and a trailing "Terms & Conditions" or
    signature page often has no identifying info at all and may even get
    classified as a different document type. By showing both pages in a
    single call, the model can visually compare them directly (matching
    numbers, "Page X of Y" markers, repeated letterhead, continued tables,
    declaration/signature pages with no new header, etc.) instead of relying
    on independently-transcribed strings or per-page type labels to match.

    Returns True if `cur` should START A NEW document (i.e. split here),
    False if it's a continuation of `prev` (i.e. keep them in one group).
    Defaults to False (merge) on any error ??? under-splitting a multi-page
    document is a smaller mistake than over-splitting one.
    """
    prompt = (
        "You are shown two consecutive pages from a scanned PDF, in this order: PAGE A, then PAGE B.\n\n"
        "Decide: is PAGE B part of the SAME overall document/record as PAGE A "
        "(e.g. page 2 of the same form/invoice/letter ??? same reference number, "
        "a 'Page X of Y' marker, a continued table, or a terms-and-conditions/"
        "declaration/signature page with no new header, letterhead, or "
        "patient/customer details), "
        "or does PAGE B begin a SEPARATE, NEW document/record "
        "(e.g. a different reference number, or a new header/letterhead/logo/title/date/"
        "patient/customer for a different transaction or person)?\n\n"
        "IMPORTANT: if PAGE B shows its OWN distinct letterhead, company logo/name, "
        "title, or reference number that is not present on PAGE A, treat it as NEW ??? "
        "even if PAGE A itself has little or no identifying information (e.g. PAGE A "
        "is a generic terms-and-conditions/declaration page).\n\n"
        "EXCEPTION for bound books/manuscripts: a cover, title page, frontispiece, or "
        "dedication page naturally looks very different from the pages around it (e.g. "
        "a photographed cloth/leather cover vs. a handwritten or printed title page vs. "
        "dense body text) even though it is the SAME physical item, not a new one. If "
        "PAGE A looks like a book/manuscript cover, spine, or blank flyleaf, OR PAGE B's "
        "main content IS a title, author name, or dedication (rather than a distinct "
        "transactional header or a body of running text), treat PAGE B as a "
        "CONTINUATION ??? do NOT treat its own title text as a new document's letterhead. "
        "This exception does not apply if PAGE B's title/content is clearly about an "
        "unrelated subject, transaction, or person than what PAGE A implies.\n\n"
        "Reply with ONLY one word: CONTINUATION or NEW."
    )
    try:
        answer = call_qwen_vision(
            prev_b64, prev_mt, prompt, max_tokens=5, extra_images=[(cur_b64, cur_mt)],
        )
        return answer.strip().upper().startswith("NEW")
    except Exception as e:
        print(f"[Split] is_new_document_boundary failed: {e}")
        return False


_NONE_VALUES = {"", "none", "n/a", "na", "-", "unknown"}


def _looks_like_page_number(val: str) -> bool:
    """True if `val` is a bare page number (e.g. "2", "(2)", "p. 3", "- 4 -")
    rather than a real invoice/reference/document number. Guards the split
    boundary check's "number" field from being fooled by a page number
    printed on a book/manuscript page ??? which otherwise reads as a fresh
    document identity and forces a false split (see process_split_pdf)."""
    core = re.sub(r'^[\s\(\[\{\-\.]*p?a?g?e?\.?\s*', '', val.strip().lower())
    core = re.sub(r'[\s\)\]\}\-\.]*$', '', core)
    return core.isdigit() and len(core) <= 3


def extract_page_keys(page_b64: str, media_type: str) -> dict:
    """
    Cheap per-page extraction of a few "identity" fields used to compare
    whether two pages belong to the same document: invoice/reference number,
    document date, and total amount. Any field not visible on the page comes
    back as "" (empty).

    This is intentionally much lighter than full field extraction (a handful
    of values, ~40 tokens) ??? it's only used to compare adjacent same-type
    pages, not to populate the final record.
    """
    prompt = (
        "Look at this document page. Extract these fields if visible:\n"
        "- invoice/reference/document number (NOT a plain page number like \"2\" or \"(2)\" "
        "printed at the top/bottom of a page ??? leave that as NONE)\n"
        "- document date\n"
        "- total amount\n"
        "If a field is not visible on this page, write NONE for it.\n\n"
        "Reply in EXACTLY this format (three lines, nothing else):\n"
        "NUMBER: <value or NONE>\n"
        "DATE: <value or NONE>\n"
        "TOTAL: <value or NONE>"
    )
    keys = {"number": "", "date": "", "total": ""}
    try:
        raw = call_qwen_vision(page_b64, media_type, prompt, max_tokens=40)
        for line in raw.strip().splitlines():
            line = line.strip()
            if line.upper().startswith("NUMBER:"):
                val = line.split(":", 1)[1].strip()
                if val.lower() in _NONE_VALUES or _looks_like_page_number(val):
                    keys["number"] = ""
                else:
                    keys["number"] = val
            elif line.upper().startswith("DATE:"):
                val = line.split(":", 1)[1].strip()
                keys["date"] = "" if val.lower() in _NONE_VALUES else val
            elif line.upper().startswith("TOTAL:"):
                val = line.split(":", 1)[1].strip()
                keys["total"] = "" if val.lower() in _NONE_VALUES else val
    except Exception as e:
        print(f"[Split] extract_page_keys failed: {e}")
    return keys


def _normalize_key(value: str) -> str:
    """Lowercase and strip everything but letters/digits, for fuzzy matching."""
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _similar(a: str, b: str, threshold: float = 0.8) -> bool:
    """True if two normalized strings are identical or close enough (handles
    small OCR slips like O/0, I/1, missing dashes, etc.)."""
    na, nb = _normalize_key(a), _normalize_key(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= threshold


def fields_indicate_same_document(keys_prev: dict, keys_cur: dict):
    """
    Compare the "identity" fields extracted from two consecutive same-type
    pages and decide whether they look like the SAME document continued, or
    TWO DIFFERENT documents.

    Returns:
      True  ??? fields strongly suggest the same document (continuation)
      False ??? fields strongly suggest two different documents
      None  ??? inconclusive (missing/empty fields on one or both pages) ???
              caller should fall back to a visual boundary check.

    The invoice/reference number is the strongest signal; date + total
    together are used when the number isn't visible on both pages (common
    on continuation pages, which often omit the header).
    """
    num_prev, num_cur   = keys_prev.get("number", ""), keys_cur.get("number", "")
    date_prev, date_cur = keys_prev.get("date", ""),   keys_cur.get("date", "")
    tot_prev, tot_cur   = keys_prev.get("total", ""),  keys_cur.get("total", "")

    if num_prev and num_cur:
        return _similar(num_prev, num_cur)

    if (date_prev and date_cur) and (tot_prev and tot_cur):
        return _similar(date_prev, date_cur) and _similar(tot_prev, tot_cur)

    return None


def process_split_pdf(pdf_path: str, upload_folder: str) -> list:
    """
    Split a single PDF that contains multiple record types into separate
    logical documents, then fully process each one.

    How it works
    ???????????????????????????????????????
    1. Render every page at low DPI (fast, just for classification).
    2. Run classify_single_page() on each page ??? a doc type per page.
    3. Walk through every consecutive page pair and decide whether page B
       starts a new logical document or continues page A's:
       - If both pages got the SAME type, try the cheap "identity field"
         comparison (fields_indicate_same_document) first ??? same invoice/
         reference number, or same date+total, strongly implies one document.
       - If that's inconclusive (including whenever the two pages got
         DIFFERENT types ??? e.g. an admission form's page 1 vs. its trailing
         "Terms & Conditions" page, which has no patient info and may
         classify differently), fall back to is_new_document_boundary() ???
         showing BOTH page images in one call and asking the model to decide
         CONTINUATION vs NEW based on layout/headers/declaration text, not
         just the per-page type label.
       e.g. [invoice, invoice, invoice, receipt, invoice]
            ??? boundary checks find page 2 starts a new invoice, but the
              trailing "receipt" page is actually part of the previous invoice
            ??? groups: [(invoice,[0,1]), (invoice,[2,3]), (invoice,[4])]
    4. For each group: carve out a sub-PDF using PyMuPDF, save it to
       upload_folder with a UUID filename, run the full process_document()
       pipeline on it, and collect the result.

    Each result dict gets two extra keys:
      _sub_pdf_filename  ??? UUID filename of the saved sub-PDF (used as
                           unique_upload_filename in the Document row)
      _split_pages       ??? list of 1-based page numbers in this group
                           (e.g. [1, 2] for a 2-page invoice)

    Returns a list of result dicts ??? one per logical document found.
    """
    import fitz
    import uuid as _uuid

    print(f"[Split] Starting page-split pipeline on: {os.path.basename(pdf_path)}")

    # classify_single_page() below calls get_all_active_doc_type_names() per
    # page - without this, an N-page PDF issues N identical DB round-trips
    # for the same list. Starting the cache here makes it a single query for
    # the whole split job (see _doc_query_cache_ctx near process_document).
    _doc_query_cache_start()

    # ?????? Step 1: render all pages at low DPI for fast classification ?????????????????????????????????
    pages = pdf_pages_to_b64(pdf_path, dpi=72, max_dim=900)
    n_pages = len(pages)
    if n_pages == 0:
        raise ValueError("PDF has no renderable pages")
    if n_pages > MAX_SPLIT_PDF_PAGES:
        raise ValueError(f"PDF has {n_pages} pages, exceeding the {MAX_SPLIT_PDF_PAGES}-page split limit")
    print(f"[Split] {n_pages} page(s) to classify")
    _mark_page_done(0, n_pages)

    # ?????? Step 2: classify each page's document type ??? bounded concurrency ??????????
    # (_PAGE_CONCURRENCY, same knob/caveat as process_document()'s page-OCR loop).
    # classify_single_page() catches its own exceptions internally and never
    # raises (falls back to "other"), so no extra error handling is needed
    # around the futures. Progress is reported from this thread in true
    # completion order as pages finish, not submission order.
    page_types = [None] * n_pages

    def _classify_one(i, b64, mt):
        return i, classify_single_page(b64, mt)

    with ThreadPoolExecutor(max_workers=_PAGE_CONCURRENCY, thread_name_prefix="split-classify") as _pool:
        futures = {_pool.submit(_classify_one, i, b64, mt): i for i, (b64, mt, _w, _h) in enumerate(pages)}
        completed = 0
        for fut in as_completed(futures):
            i, doc_type = fut.result()
            page_types[i] = doc_type
            completed += 1
            print(f"[Split] Page {i+1}/{n_pages} ??? {doc_type}")
            _set_lm_slot(
                f"Classifying page {completed}/{n_pages}...",
                0.1 + (completed - 1) / n_pages * 0.3,
                0.1 + completed / n_pages * 0.3,
            )

    # ?????? Step 3: check every consecutive page pair for a document boundary ????????????
    # A type change between two pages does NOT automatically mean a new
    # document ??? e.g. a trailing "Terms & Conditions"/consent/signature page
    # often has no patient/invoice info and gets classified differently from
    # page 1, but is still part of the same record. So every pair gets a
    # boundary decision; only same-type pairs first try the cheap
    # "identity field" comparison before falling back to the visual check.
    #
    # Each pair's decision depends only on that pair's own two pages, never
    # on a NEIGHBORING pair's decision - so the decisions themselves are safe
    # to compute concurrently. What IS inherently sequential is turning the
    # resulting sequence of is_new flags into actual page-index groups (each
    # group is built by walking left to right) - that final pass stays a
    # plain, fast, CPU-only loop over the already-computed decisions, done
    # once every pair's result is in (in original pair order, not completion
    # order, since group-building itself is order-dependent).
    groups_indices = [[0]]
    page_keys_cache = {}
    if n_pages > 1:
        # Pass A: precompute every page's cheap "identity" keys up front,
        # concurrently. extract_page_keys() also never raises. This computes
        # keys for a couple more pages than the original lazy version would
        # (e.g. page 0 when its only pair turns out different-type and
        # inconclusive-without-cur-having-a-number) - a handful of extra
        # cheap (~40 token) calls at most, never a different final grouping,
        # since a page's keys are a pure function of that page alone.
        def _extract_keys_one(i, b64, mt):
            return i, extract_page_keys(b64, mt)

        with ThreadPoolExecutor(max_workers=_PAGE_CONCURRENCY, thread_name_prefix="split-keys") as _pool:
            futures = {_pool.submit(_extract_keys_one, i, b64, mt): i for i, (b64, mt, _w, _h) in enumerate(pages)}
            for fut in as_completed(futures):
                i, keys = fut.result()
                page_keys_cache[i] = keys

        # Pass B: compute each pair's is_new decision concurrently.
        def _pair_decision(i):
            prev_idx, cur_idx = i - 1, i
            prev_b64, prev_mt, _, _ = pages[prev_idx]
            cur_b64, cur_mt, _, _   = pages[cur_idx]

            same_doc = None
            if page_types[prev_idx] == page_types[cur_idx]:
                same_doc = fields_indicate_same_document(page_keys_cache[prev_idx], page_keys_cache[cur_idx])
            else:
                if page_keys_cache[cur_idx].get("number"):
                    same_doc = False

            if same_doc is None:
                return i, is_new_document_boundary(prev_b64, prev_mt, cur_b64, cur_mt)
            return i, not same_doc

        pair_is_new = {}
        with ThreadPoolExecutor(max_workers=_PAGE_CONCURRENCY, thread_name_prefix="split-boundary") as _pool:
            futures = {_pool.submit(_pair_decision, i): i for i in range(1, n_pages)}
            completed = 0
            for fut in as_completed(futures):
                i, is_new = fut.result()
                pair_is_new[i] = is_new
                completed += 1
                _set_lm_slot(
                    f"Checking boundaries ({completed}/{n_pages - 1})...",
                    0.4 + (completed - 1) / (n_pages - 1) * 0.1,
                    0.4 + completed / (n_pages - 1) * 0.1,
                )

        # Sequential, model-call-free pass: build groups in original page order.
        for i in range(1, n_pages):
            if pair_is_new[i]:
                groups_indices.append([i])
            else:
                groups_indices[-1].append(i)

    # group "type" (used only for the error-fallback document_type below) is
    # the type of the group's first page.
    groups = [(page_types[g[0]], g) for g in groups_indices]

    print(f"[Split] Found {len(groups)} logical document(s):")
    for g_type, g_pages in groups:
        print(f"  {g_type}: pages {[p + 1 for p in g_pages]}")

    # ?????? Step 5: carve sub-PDFs and run full processing on each group ??????????????????????????????
    results = []
    pages_done = 0
    for group_idx, (group_type, page_indices) in enumerate(groups):
        _set_lm_slot(
            f"Processing record {group_idx + 1}/{len(groups)} ({group_type})...",
            0.5 + (group_idx / len(groups)) * 0.5,
            0.5 + ((group_idx + 1) / len(groups)) * 0.5,
        )

        # Give each sub-PDF its own UUID filename so it lives permanently in
        # upload_folder ??? the Document row's unique_upload_filename points here.
        sub_filename = f"{_uuid.uuid4().hex}.pdf"
        sub_pdf_path = os.path.join(upload_folder, sub_filename)

        try:
            # Carve the selected pages out of the source PDF
            with fitz.open(pdf_path) as src:
                sub = fitz.open()
                for pi in page_indices:
                    sub.insert_pdf(src, from_page=pi, to_page=pi)
                sub.save(sub_pdf_path)
                sub.close()
            print(f"[Split] Group {group_idx + 1}: pages {[p+1 for p in page_indices]} ??? {sub_filename}")

            # Full extraction pipeline (OCR + classify + field extract).
            # Suppress this sub-call's own page-completion signals ??? they're
            # relative to the sub-PDF, not the overall split ??? and report our
            # own cumulative page count once the group is fully done instead.
            with _split_record_progress(f"Record {group_idx + 1}/{len(groups)} - "):
                result = process_document(sub_pdf_path)
            result["_sub_pdf_filename"] = sub_filename
            result["_split_pages"]      = [p + 1 for p in page_indices]
            results.append(result)

        except Exception as e:
            print(f"[Split] Group {group_idx + 1} processing failed: {e}")
            # Keep the sub-PDF only if it was actually written; remove otherwise
            if os.path.exists(sub_pdf_path) and os.path.getsize(sub_pdf_path) == 0:
                os.remove(sub_pdf_path)
            results.append({
                "raw_text":          "",
                "document_type":     group_type,
                "confidence":        0.35,
                "fields":            {},
                "text_blocks":       [],
                "_sub_pdf_filename": sub_filename if os.path.exists(sub_pdf_path) else None,
                "_split_pages":      [p + 1 for p in page_indices],
                "_error":            str(e),
            })

        pages_done += len(page_indices)
        _mark_page_done(pages_done, n_pages)

    print(f"[Split] Done ??? {len(results)} record(s) extracted from {n_pages}-page PDF")
    return results


def _make_content_groups(n_pages: int, separator_pages: set) -> list:
    """Return lists of 0-based page indices grouped between separator pages."""
    groups, current = [], []
    for i in range(n_pages):
        if i in separator_pages:
            if current:
                groups.append(current)
                current = []
        else:
            current.append(i)
    if current:
        groups.append(current)
    return groups


def _pil_img_to_b64(img, max_dim: int = 900) -> tuple:
    """Downscale a PIL Image and return (base64_str, media_type)."""
    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"


def detect_separator_groups(pdf_path: str) -> list:
    """
    Detect logical document groups inside a PDF without running full extraction.

    Strategy (barcode-first, AI fallback):
    1. If a barcode/QR decoder is available, scan every page at 150 DPI.
       Pages that contain a code become separator sheets and are discarded.
       Content pages between separators form groups.
    2. If no decoder is installed OR no separator pages are detected, fall back
       to classifying every page individually with Qwen Vision and grouping
       consecutive pages that share the same document type.

    Returns a list of dicts:
      [{"pages": [1, 2], "detected_type": "ap_invoice"}, ...]
    All page numbers are 1-based.
    """
    import fitz

    # Same per-job DB-query cache as process_split_pdf -- the AI-classification
    # fallback path below also calls classify_single_page() per page.
    _doc_query_cache_start()

    # ?????? Lazy-load barcode decoder ????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????
    _barcode_decoder = None
    try:
        import zxingcpp as _zx
        _barcode_decoder = lambda img: len(_zx.read_barcodes(img)) > 0
        print("[Detect] Barcode decoder: zxing-cpp")
    except ImportError:
        try:
            from pyzbar import pyzbar as _pz
            _barcode_decoder = lambda img: len(_pz.decode(img)) > 0
            print("[Detect] Barcode decoder: pyzbar")
        except ImportError:
            print("[Detect] No barcode decoder ??? using AI classification only")

    with fitz.open(pdf_path) as fitz_doc:
        n_pages = len(fitz_doc)
        if n_pages == 0:
            raise ValueError("PDF has no renderable pages")
        if n_pages > MAX_SPLIT_PDF_PAGES:
            raise ValueError(
                f"PDF has {n_pages} pages, exceeding the {MAX_SPLIT_PDF_PAGES}-page split limit"
            )
        print(f"[Detect] {n_pages} page(s) ??? {os.path.basename(pdf_path)}")

        # ?????? Barcode scan path ????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????
        if _barcode_decoder:
            separator_pages = set()
            # Each page's PIL image is scanned then dropped immediately -
            # not held for the whole document (previously every rendered
            # page stayed in memory simultaneously via a pil_pages list).
            for i, page in enumerate(fitz_doc):
                _set_lm_slot(
                    f"Scanning page {i+1}/{n_pages} for barcode...",
                    0.02 + (i / n_pages) * 0.18,
                    0.02 + ((i + 1) / n_pages) * 0.18,
                )
                mat = fitz.Matrix(150 / 72, 150 / 72)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                if _barcode_decoder(img):
                    separator_pages.add(i)
                    print(f"[Detect] Page {i+1}: SEPARATOR (barcode/QR)")
                else:
                    print(f"[Detect] Page {i+1}: content")

            if separator_pages:
                groups_idx = _make_content_groups(n_pages, separator_pages)
                result = []
                n_groups = max(len(groups_idx), 1)
                for gi, grp in enumerate(groups_idx):
                    _set_lm_slot(
                        f"Classifying group {gi+1}/{n_groups}...",
                        0.2 + (gi / n_groups) * 0.8,
                        0.2 + ((gi + 1) / n_groups) * 0.8,
                    )
                    # Re-render just this group's first page now that the
                    # groups are known, rather than keeping every page's
                    # image from the scan above alive the whole time.
                    first_page = fitz_doc[grp[0]]
                    mat = fitz.Matrix(150 / 72, 150 / 72)
                    pix = first_page.get_pixmap(matrix=mat, alpha=False)
                    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    b64, mt = _pil_img_to_b64(img)
                    doc_type = classify_single_page(b64, mt)
                    result.append({"pages": [p + 1 for p in grp], "detected_type": doc_type})
                    print(f"[Detect] Group pages {[p+1 for p in grp]} ??? {doc_type}")
                return result

            print("[Detect] No barcode separators found ??? falling back to AI classification")

        # ?????? AI classification path ?????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????
        # Encode + classify one page at a time (fitz_doc stays open across the
        # classify calls so we never buffer every page's base64 upfront), then
        # discard the encoded image before moving to the next page.
        print("[Detect] Running AI page-by-page classification...")
        page_types = []
        for i, page in enumerate(fitz_doc):
            _set_lm_slot(
                f"Classifying page {i+1}/{n_pages}...",
                0.05 + (i / n_pages) * 0.95,
                0.05 + ((i + 1) / n_pages) * 0.95,
            )
            mat = fitz.Matrix(1.0, 1.0)   # 72 DPI ??? fast, just for classification
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            b64, _ = _pil_img_to_b64(img)
            doc_type = classify_single_page(b64, "image/jpeg")
            page_types.append(doc_type)
            print(f"[Detect] Page {i+1} ??? {doc_type}")

    # Group consecutive same-type pages
    groups = []
    cur_type, cur_pages = page_types[0], [0]
    for i in range(1, n_pages):
        if page_types[i] == cur_type:
            cur_pages.append(i)
        else:
            groups.append({"pages": [p + 1 for p in cur_pages], "detected_type": cur_type})
            cur_type, cur_pages = page_types[i], [i]
    groups.append({"pages": [p + 1 for p in cur_pages], "detected_type": cur_type})

    return groups


def process_barcode_split_pdf(pdf_path: str, upload_folder: str) -> list:
    """
    Split a PDF using barcode/QR code separator pages.

    Pages that contain any barcode or QR code are treated as separator sheets
    and discarded.  The content pages between separators are grouped into
    logical documents, each carved out as a sub-PDF and run through the full
    process_document() pipeline.

    Falls back to treating the whole PDF as a single document when no
    separator pages are found.

    Returns the same list-of-result-dicts format as process_split_pdf().
    """
    import fitz
    import uuid as _uuid

    # ?????? Lazy-load barcode decoder ????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????
    try:
        import zxingcpp as _zx
        def _has_barcode(pil_img):
            return len(_zx.read_barcodes(pil_img)) > 0
        print("[BarcodeSpilt] Using zxing-cpp decoder")
    except ImportError:
        try:
            from pyzbar import pyzbar as _pyzbar
            def _has_barcode(pil_img):
                return len(_pyzbar.decode(pil_img)) > 0
            print("[BarcodeSpilt] Using pyzbar decoder")
        except ImportError:
            raise ImportError(
                "No barcode decoder found. Install one:\n"
                "  pip install zxing-cpp   (recommended, no system deps)\n"
                "  pip install pyzbar      (requires zbar DLL on Windows)"
            )

    print(f"[BarcodeSpilt] Starting barcode-split pipeline on: {os.path.basename(pdf_path)}")

    with fitz.open(pdf_path) as doc:
        n_pages = len(doc)
        if n_pages == 0:
            raise ValueError("PDF has no renderable pages")
        if n_pages > MAX_SPLIT_PDF_PAGES:
            raise ValueError(f"PDF has {n_pages} pages, exceeding the {MAX_SPLIT_PDF_PAGES}-page split limit")

        print(f"[BarcodeSpilt] Scanning {n_pages} page(s) for barcode/QR separators...")
        _mark_page_done(0, n_pages)

        separator_pages = set()
        for i, page in enumerate(doc):
            _set_lm_slot(
                f"Scanning page {i+1}/{n_pages} for barcode...",
                0.05 + (i / n_pages) * 0.15,
                0.05 + ((i + 1) / n_pages) * 0.15,
            )
            mat = fitz.Matrix(150 / 72, 150 / 72)   # 150 DPI ??? enough for barcode detection
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            if _has_barcode(img):
                separator_pages.add(i)
                print(f"[BarcodeSpilt] Page {i+1}: SEPARATOR (barcode/QR detected)")
            else:
                print(f"[BarcodeSpilt] Page {i+1}: content page")

    if not separator_pages:
        print("[BarcodeSpilt] No barcode separators found ??? treating entire PDF as one document")
        result = process_document(pdf_path)
        result["_split_pages"]      = list(range(1, n_pages + 1))
        result["_sub_pdf_filename"] = os.path.basename(pdf_path)
        return [result]

    # ?????? Group content pages between separators ????????????????????????????????????????????????????????????????????????????????????????????????
    groups: list[list[int]] = []
    current: list[int] = []
    for i in range(n_pages):
        if i in separator_pages:
            if current:
                groups.append(current)
                current = []
        else:
            current.append(i)
    if current:
        groups.append(current)

    if not groups:
        print("[BarcodeSpilt] All pages are separator sheets ??? nothing to process")
        return []

    print(
        f"[BarcodeSpilt] {len(separator_pages)} separator(s) found ??? "
        f"{len(groups)} document group(s)"
    )
    for gi, g in enumerate(groups):
        print(f"  Group {gi+1}: pages {[p+1 for p in g]}")

    # ?????? Carve sub-PDFs and run full extraction on each group ??????????????????????????????????????????????????????
    # Separator sheets need no further processing, so they count as done as
    # soon as the scan finds them; each content group then adds its own pages.
    pages_done = len(separator_pages)
    _mark_page_done(pages_done, n_pages)

    results = []
    with fitz.open(pdf_path) as src:
        for group_idx, page_indices in enumerate(groups):
            _set_lm_slot(
                f"Processing document {group_idx+1}/{len(groups)}...",
                0.2 + (group_idx / len(groups)) * 0.8,
                0.2 + ((group_idx + 1) / len(groups)) * 0.8,
            )

            sub_filename = f"{_uuid.uuid4().hex}.pdf"
            sub_pdf_path = os.path.join(upload_folder, sub_filename)

            try:
                sub = fitz.open()
                for pi in page_indices:
                    sub.insert_pdf(src, from_page=pi, to_page=pi)
                sub.save(sub_pdf_path)
                sub.close()

                print(f"[BarcodeSpilt] Group {group_idx+1}: pages {[p+1 for p in page_indices]} ??? {sub_filename}")

                with _split_record_progress(f"Document {group_idx+1}/{len(groups)} - "):
                    result = process_document(sub_pdf_path)
                result["_sub_pdf_filename"] = sub_filename
                result["_split_pages"]      = [p + 1 for p in page_indices]
                results.append(result)

            except Exception as e:
                print(f"[BarcodeSpilt] Group {group_idx+1} failed: {e}")
                if os.path.exists(sub_pdf_path) and os.path.getsize(sub_pdf_path) == 0:
                    os.remove(sub_pdf_path)
                results.append({
                    "raw_text":          "",
                    "document_type":     "unknown",
                    "confidence":        0.35,
                    "fields":            {},
                    "text_blocks":       [],
                    "_sub_pdf_filename": sub_filename if os.path.exists(sub_pdf_path) else None,
                    "_split_pages":      [p + 1 for p in page_indices],
                    "_error":            str(e),
                })

            pages_done += len(page_indices)
            _mark_page_done(pages_done, n_pages)

    print(f"[BarcodeSpilt] Done ??? {len(results)} document(s) from {n_pages}-page PDF")
    return results


def process_mrn_barcode_split_pdf(pdf_path: str, upload_folder: str) -> list:
    """
    Split a merged PDF using the MRN Barcode (a Code 128 barcode) as the
    "a new document starts here" signal -- NOT the same model as
    process_barcode_split_pdf() above, which treats barcode/QR pages as
    separator sheets to be discarded. Here, the page carrying the MRN
    Barcode IS the first page of real content, not a throwaway separator.

    Every page that decodes a Code 128 barcode starts a new document group;
    a page without one is a continuation page of the current group (e.g. a
    2-page "Out-patient Drug List" document's 2nd page, which repeats the
    case number/order-reference barcode but not its own MRN sticker -- the
    MRN sticker is only printed once per physical document).

    Chosen over QR 5 as the split signal because QR 5 isn't present on every
    page (some "Out-patient Prescription" pages have no QR sticker at all),
    while the MRN Barcode reliably is.

    Renders at _BARCODE_DECODE_DPI (450) -- empirically confirmed necessary;
    150 DPI (used by detect_separator_groups' presence-only scan) misses
    this specific barcode on some real pages.

    Returns the same list-of-result-dicts format as process_barcode_split_pdf().
    A leading page with no MRN barcode (nothing earlier to attach it to)
    starts its own single-page group rather than being silently dropped.
    """
    import fitz
    import uuid as _uuid

    try:
        import zxingcpp
    except ImportError:
        raise ImportError(
            "No barcode decoder found. Install one:\n"
            "  pip install zxing-cpp   (recommended, no system deps)"
        )

    print(f"[MrnSplit] Starting MRN-barcode-split pipeline on: {os.path.basename(pdf_path)}")

    with fitz.open(pdf_path) as doc:
        n_pages = len(doc)
        if n_pages == 0:
            raise ValueError("PDF has no renderable pages")
        if n_pages > MAX_SPLIT_PDF_PAGES:
            raise ValueError(f"PDF has {n_pages} pages, exceeding the {MAX_SPLIT_PDF_PAGES}-page split limit")

        print(f"[MrnSplit] Scanning {n_pages} page(s) for MRN Barcode (Code 128)...")
        _mark_page_done(0, n_pages)

        # groups: list of {"pages": [0-based indices], "mrn_barcode": str|None, "has_code39": bool}
        groups: list[dict] = []
        for i, page in enumerate(doc):
            _set_lm_slot(
                f"Scanning page {i+1}/{n_pages} for MRN Barcode...",
                0.05 + (i / n_pages) * 0.15,
                0.05 + ((i + 1) / n_pages) * 0.15,
            )
            mat = fitz.Matrix(_BARCODE_DECODE_DPI / 72, _BARCODE_DECODE_DPI / 72)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            detections = zxingcpp.read_barcodes(img)
            valid = [r for r in detections if r.valid]
            mrn = next((r.text for r in valid if str(r.format) == "Code 128"), None)
            has_code39 = any(str(r.format) == "Code 39" for r in valid)

            if mrn or not groups:
                groups.append({"pages": [i], "mrn_barcode": mrn, "has_code39": has_code39})
                print(f"[MrnSplit] Page {i+1}: {'NEW document, MRN=' + mrn if mrn else 'NEW document (no MRN barcode found -- nothing earlier to attach to)'}")
            else:
                groups[-1]["pages"].append(i)
                groups[-1]["has_code39"] = groups[-1]["has_code39"] or has_code39
                print(f"[MrnSplit] Page {i+1}: continuation of document starting at page {groups[-1]['pages'][0]+1}")

    # ?????? Deterministic doc-type classification, not the VLM's ??????????????????????????????????????????????????????????
    # Out-patient Prescription and Out-patient Drug List are visually similar
    # enough (both carry an MRN Barcode + printed Chinese text) that the VLM
    # classifier was observed returning "Unclassified" for real Drug List
    # pages instead of picking the right one from the known-types list. But
    # the distinction is already known with certainty from the scan above:
    # Drug List pages always carry Code 39 barcodes (Case No. + Barcode 3);
    # Prescription pages never do (MRN Barcode + optional QR 5 only). No
    # need to trust the VLM's guess when the barcode format already answers
    # the question unambiguously.
    for g in groups:
        g["forced_type"] = _OUT_PATIENT_DRUG_LIST_DB_NAME if g["has_code39"] else _OUT_PATIENT_PRESCRIPTION_DB_NAME

    print(f"[MrnSplit] {len(groups)} document group(s) from {n_pages} page(s)")
    for gi, g in enumerate(groups):
        print(f"  Group {gi+1}: pages {[p+1 for p in g['pages']]}, MRN={g['mrn_barcode']}, type={g['forced_type']}")

    # ?????? Carve sub-PDFs and run full extraction on each group ??????????????????????????????????????????????????????
    pages_done = 0
    results = []
    with fitz.open(pdf_path) as src:
        for group_idx, group in enumerate(groups):
            page_indices = group["pages"]
            _set_lm_slot(
                f"Processing document {group_idx+1}/{len(groups)}...",
                0.2 + (group_idx / len(groups)) * 0.8,
                0.2 + ((group_idx + 1) / len(groups)) * 0.8,
            )

            sub_filename = f"{_uuid.uuid4().hex}.pdf"
            sub_pdf_path = os.path.join(upload_folder, sub_filename)

            try:
                sub = fitz.open()
                for pi in page_indices:
                    sub.insert_pdf(src, from_page=pi, to_page=pi)
                sub.save(sub_pdf_path)
                sub.close()

                print(f"[MrnSplit] Group {group_idx+1}: pages {[p+1 for p in page_indices]} ??? {sub_filename} ({group['forced_type']})")

                with _split_record_progress(f"Document {group_idx+1}/{len(groups)} - "):
                    result = process_document(sub_pdf_path, forced_document_type=group["forced_type"])
                result["_sub_pdf_filename"] = sub_filename
                result["_split_pages"]      = [p + 1 for p in page_indices]
                results.append(result)

            except Exception as e:
                print(f"[MrnSplit] Group {group_idx+1} failed: {e}")
                if os.path.exists(sub_pdf_path) and os.path.getsize(sub_pdf_path) == 0:
                    os.remove(sub_pdf_path)
                results.append({
                    "raw_text":          "",
                    "document_type":     "unknown",
                    "confidence":        0.35,
                    "fields":            {},
                    "text_blocks":       [],
                    "_sub_pdf_filename": sub_filename if os.path.exists(sub_pdf_path) else None,
                    "_split_pages":      [p + 1 for p in page_indices],
                    "_error":            str(e),
                })

            pages_done += len(page_indices)
            _mark_page_done(pages_done, n_pages)

    print(f"[MrnSplit] Done ??? {len(results)} document(s) from {n_pages}-page PDF")
    return results


# ?????? Field-label translation ?????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

def translate_field_labels(labels: list, source_lang: str, target_lang: str) -> dict:
    """
    Translate a list of field label strings from source_lang to target_lang
    using the local Qwen text model.

    Returns a dict mapping every original label to its translation.
    Labels that cannot be translated are mapped to themselves.

    Example:
      translate_field_labels(["vendor name", "total amount"], "english", "thai")
      ??? {"vendor name": "??????????????????????????????", "total amount": "????????????????????????????????????"}
    """
    if not labels:
        return {}

    # Nothing to do if already in the right language
    if source_lang.lower() == target_lang.lower():
        return {lbl: lbl for lbl in labels}

    # For Jawi, do a deterministic word-level dictionary pass FIRST -- the LLM
    # alone frequently hallucinates on unfamiliar Jawi spellings (e.g. turning
    # a repeated word into "Chocha Chocha Chocha"). Words not in the
    # dictionary are left in Jawi script for the LLM to still attempt.
    # `translate_input` maps each original label to what actually gets sent
    # to the LLM, so results can be mapped back to the caller's original keys.
    if source_lang.lower() == "jawi":
        translate_input = {lbl: jawi_to_rumi(lbl) for lbl in labels}
        effective_source_lang = (
            "Malay (romanized from Jawi via dictionary lookup; a few rare "
            "words not in the dictionary may still be in Jawi/Arabic script)"
        )
    else:
        translate_input = {lbl: lbl for lbl in labels}
        effective_source_lang = source_lang

    romanized_labels = list(translate_input.values())
    labels_json = json.dumps(romanized_labels, ensure_ascii=False)
    script_note = ""
    if source_lang.lower() in ("arabic", "jawi"):
        script_note = (
            "\nNOTE: Arabic-script input may actually be Jawi (Malay written in Arabic "
            "letters), not standard Arabic. Read the actual words first, and if they don't "
            "form real Arabic vocabulary, treat them as Jawi/Malay and translate that meaning "
            "instead of failing.\n"
        )
    prompt = (
        f"You are a document field name translator.\n"
        f"Translate each field label from {effective_source_lang} to {target_lang}.\n"
        f"Keep translations short and natural for a document field context.\n"
        f"{script_note}"
        f"Input labels (JSON array): {labels_json}\n\n"
        f"Reply with ONLY a JSON object mapping each original label to its translation.\n"
        f"Example format: {{\"vendor name\": \"translated\", \"total amount\": \"translated\"}}\n"
        f"Output ONLY valid JSON, no explanation, no markdown."
    )

    try:
        msgs = [{"role": "user", "content": prompt}]
        response = client.chat.completions.create(
            model=TEXT_MODEL, messages=msgs, temperature=0.1, max_tokens=1500, stream=False
        )
        raw = response.choices[0].message.content.strip()
        result = safe_parse_json(raw)
        if isinstance(result, dict):
            # Map back from the (possibly romanized) text the LLM saw to the
            # ORIGINAL label the caller passed in -- fall back to the
            # romanized/original text if the model dropped that entry.
            return {lbl: result.get(translate_input[lbl], translate_input[lbl]) for lbl in labels}
    except Exception as e:
        print(f"[Translate] Failed: {e}")

    return {lbl: translate_input[lbl] for lbl in labels}


# ?????? Document Q&A ??????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

_QA_CHAR_BUDGET = 14000  # shared with the single-page fallback below -- single source of truth


def _truncate_to_budget(text: str, char_budget: int = _QA_CHAR_BUDGET) -> str:
    """Keep the start and end of `text` within char_budget, mirroring the
    head+tail bias _select_qa_pages already applies across pages (it always
    keeps the first and last page) -- so a single-blob document (no blank
    lines to split on) gets the same truncation discipline instead of being
    sent to the model completely unbounded."""
    if len(text) <= char_budget:
        return text
    head = int(char_budget * 0.7)
    tail = char_budget - head
    return text[:head] + "\n...[truncated]...\n" + text[-tail:]


def _select_qa_pages(pages: list, question: str, char_budget: int = _QA_CHAR_BUDGET) -> str:
    """
    Pick the most relevant pages for a question within a character budget.
    Always includes page 1 (document header/intro) and the last page
    (signatures, conclusions, 'Disediakan oleh', etc.).
    Remaining budget is filled with pages ranked by keyword overlap with the question.
    """
    _STOP = {
        # English
        'the','a','an','is','are','was','were','what','who','where','when','how',
        'why','which','this','that','of','in','on','at','to','for','and','or',
        # Malay
        'siapa','apa','bila','bagaimana','kenapa','yang','dan','untuk','dengan',
        'pada','dari','ini','itu','oleh','di','ke','akan','telah','dalam','adalah',
    }
    q_words = set(re.sub(r'[^\w\s]', '', question.lower()).split()) - _STOP

    annotated = [f"[PAGE {i+1}]\n{p}" for i, p in enumerate(pages)]
    n = len(annotated)

    # Score pages by keyword overlap with the question
    def _score(text):
        return sum(1 for w in q_words if w in text.lower())

    must = {0, n - 1}                        # always first + last
    selected = dict()                         # index ? text
    chars = 0

    for idx in sorted(must):
        if idx < n:
            selected[idx] = annotated[idx]
            chars += len(annotated[idx])

    ranked = sorted(
        [(i, annotated[i]) for i in range(n) if i not in must],
        key=lambda x: _score(x[1]),
        reverse=True,
    )
    for idx, text in ranked:
        if chars + len(text) > char_budget:
            continue                          # skip if over budget, try next
        selected[idx] = text
        chars += len(text)

    return "\n\n".join(selected[i] for i in sorted(selected))


def answer_question(raw_text: str, question: str, history: list = None) -> dict:
    """
    Answer a user question about a specific document's content.
    history is a list of {q, a} dicts from previous turns (most recent last).
    Returns answer text + a source quote from the document.
    Page numbers are estimated by splitting raw_text on double-newlines (one chunk per page).
    """
    # Split on the [PAGE N] markers written by the OCR pipeline so each entry
    # corresponds to one real PDF page.  Falling back to the old "\n\n" split
    # caused raw_text's within-page double-newlines (between sections/tables)
    # to be counted as page boundaries, producing pseudo-page numbers far
    # beyond the document's real page count.
    _page_parts = re.split(r'\[PAGE \d+\]\s*', raw_text)
    pages = [p.strip() for p in _page_parts if p.strip()]
    if not pages:
        pages = [raw_text.strip()] if raw_text.strip() else []
    if len(pages) > 1:
        annotated = _select_qa_pages(pages, question)
    else:
        annotated = _truncate_to_budget(pages[0] if pages else raw_text)

    system_msg = (
        "You are an intelligent document assistant ??? like ChatGPT but focused on a specific document. "
        "You have two sources of knowledge:\n"
        "1. The document the user has uploaded (provided below).\n"
        "2. Your own general world knowledge (laws, rates, formulas, definitions, best practices, etc.).\n\n"
        "How to answer:\n"
        "- If the answer is directly in the document, quote and use it.\n"
        "- If the question requires a calculation (e.g. EPF, SOCSO, tax, interest, discount), extract the relevant "
        "numbers from the document and apply your knowledge to calculate ??? show step-by-step working.\n"
        "- If the question is a general knowledge question related to the document's topic, answer from your knowledge.\n"
        "- If the document provides partial info and your knowledge can fill the gap, combine both.\n"
        "- Never say 'I can only use the document' ??? use all your knowledge to give the most helpful answer.\n"
        "- For follow-up questions (e.g. 'translate it', 'explain that', 'what does that mean'), refer to your "
        "previous answer in this conversation ??? do NOT re-summarise the whole document.\n"
        "- You are READ-ONLY: you cannot save, add, remove, or modify any field on this document, no matter how "
        "the user phrases the request. If asked to add/save/update a field, answer with the value itself, but "
        "explicitly say you have NOT saved it and that they should ask again more directly (e.g. \"add payment "
        "address field\") so the app can actually save it. NEVER write a response implying something was "
        "added/saved/updated when you have not actually done so - you have no ability to do that.\n\n"
        "Always reply with ONLY a JSON object, no extra text:\n"
        '{\n'
        '  "answer": "full helpful answer; include step-by-step working for calculations",\n'
        '  "source_quote": "exact verbatim text from the document used as input (max 200 chars), or empty string",\n'
        '  "value": "the standalone extracted value only — a name, number, date, code, amount, etc. No sentence, no prefix like \'The X is\', no trailing punctuation. Empty string if the question is not asking for a specific value (e.g. a general explanation or calculation).",\n'
        '  "page_number": null\n'
        '}\n'
        "- page_number: integer [PAGE N] where the key value was found, or null.\n"
        "- source_quote: must be an exact substring from the document, or \"\" if not applicable.\n"
        "- value: the distilled value to store in a field (e.g. for 'who is the signatory?' return the person's name). Empty string for general questions."
    )

    doc_context = f"Document:\n---\n{annotated}\n---\n\n"

    # Build multi-turn message list
    prior = (history or [])[-6:]  # keep last 6 turns to avoid blowing context
    if prior:
        def _hist_asst(a):
            # Re-wrap stored answer text back into the JSON format the model expects,
            # so each assistant turn stays consistent with the system prompt.
            return json.dumps({"answer": a, "source_quote": "", "page_number": None})

        # First user message carries the document context + first historical question
        messages = [
            {"role": "system",    "content": system_msg},
            {"role": "user",      "content": doc_context + prior[0]["q"]},
            {"role": "assistant", "content": _hist_asst(prior[0]["a"])},
        ]
        for turn in prior[1:]:
            messages.append({"role": "user",      "content": turn["q"]})
            messages.append({"role": "assistant", "content": _hist_asst(turn["a"])})
        messages.append({"role": "user", "content": question})
    else:
        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user",   "content": doc_context + "Question: " + question},
        ]

    try:
        response = client.chat.completions.create(
            model=TEXT_MODEL,
            messages=messages,
            temperature=0.0,
            max_tokens=800,
        )
        text = response.choices[0].message.content.strip()
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            result = json.loads(match.group())
        else:
            result = json.loads(text)
        return {
            "answer":       result.get("answer", "").strip(),
            "source_quote": result.get("source_quote", "").strip(),
            "value":        result.get("value", "").strip(),
            "page_number":  result.get("page_number", None),
        }
    except Exception as e:
        print(f"[QA] Error: {e}")
        return {"answer": "Failed to generate an answer. Please try again.", "source_quote": "", "page_number": None}


# ?????? Field Correction Intent Detection ????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

def detect_correction_intent(question: str, fields: dict, raw_text: str = "") -> dict:
    """
    Detect if the user's message is correcting an extracted field value.
    The user may give a literal value ("the correct value is X") or a rule/description
    ("find the full name at the end of this page") -- in the latter case the document
    text is used to resolve the actual value, not just echo the description back.
    Returns:
      {
        "is_correction": bool,
        "field_name": str | None,
        "field_label": str | None,
        "corrected_value": str | None,
        "rule_hint": str | None,
        "explanation": str
      }
    """
    empty = {"is_correction": False, "field_name": None, "field_label": None, "corrected_value": None, "rule_hint": None, "explanation": ""}
    if not fields:
        return empty

    fields_summary = "\n".join(f"  {k}: {v}" for k, v in list(fields.items())[:20])

    doc_excerpt = ""
    if raw_text:
        if len(raw_text) <= 6000:
            doc_excerpt = raw_text
        else:
            doc_excerpt = raw_text[:2500] + "\n...[middle truncated]...\n" + raw_text[-3500:]

    system_msg = (
        "You are a field-correction classifier and resolver. "
        "The user may write in ANY language (English, Malay, or mixed ? handle all). "
        "The user is viewing a document with extracted fields and the document's text. "
        "Decide whether the user's message is correcting a wrong field value (not asking a question). "
        "The user may state the correct value literally (e.g. \"the correct value is X\"), or describe "
        "a RULE for where to find it (e.g. \"find the full name at the end of this page\"). "
        "When the user describes a rule instead of a literal value, READ the provided document text "
        "yourself and RESOLVE the actual correct value -- do not just repeat the user's description back "
        "as if it were the value. "
        "Return ONLY a JSON object, no extra text:\n"
        '{\n'
        '  "is_correction": true/false,\n'
        '  "field_name": "exact key from the fields list, or null",\n'
        '  "corrected_value": "the actual resolved value (read from the document text if the user only gave a rule), or null",\n'
        '  "rule_hint": "the user\'s instruction rephrased as a reusable extraction rule for future documents of this type, or null",\n'
        '  "explanation": "one short sentence describing the correction for display, or empty string"\n'
        '}\n'
        "Rules:\n"
        "- is_correction=true only if the user is clearly stating a value is wrong and/or describing how to find the correct one.\n"
        "- Match field_name to the closest key in the provided fields list.\n"
        "- corrected_value must always be the actual literal value, never a description of where to find it.\n"
        "- If no specific field is identifiable, return is_correction=false."
    )

    user_msg = (
        f"Extracted fields:\n{fields_summary}\n\n"
        f"User message: {question}"
    )
    if doc_excerpt:
        user_msg += f"\n\nDocument text:\n{doc_excerpt}"

    try:
        response = client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.0,
            max_tokens=400,
        )
        text = response.choices[0].message.content.strip()
        match = re.search(r'\{.*\}', text, re.DOTALL)
        result = json.loads(match.group() if match else text)
        fn = result.get("field_name") or None
        return {
            "is_correction":   bool(result.get("is_correction", False)),
            "field_name":      fn,
            "field_label":     fn.replace("_", " ").title() if fn else None,
            "corrected_value": result.get("corrected_value") or None,
            "rule_hint":       result.get("rule_hint") or None,
            "explanation":     result.get("explanation", "").strip(),
        }
    except Exception as e:
        print(f"[CorrectionIntent] Error: {e}")
        return empty


def generate_field_extraction_rule(field_name: str, field_label: str, corrected_value: str, doc_type: str, raw_text: str = "", rule_hint: str = "") -> str:
    """
    Given a field correction, generate a concise extraction rule to append to the
    document type's structure_hint prompt so future extractions get it right.
    If rule_hint is provided (the user's own description of how to find the value),
    it is used as the primary basis for the rule instead of reverse-engineering one
    purely from the literal corrected_value.
    Returns a plain-text rule string (1-3 sentences).
    """
    context = f"Document type: {doc_type}\nField name: {field_name}\nField label: {field_label}\nCorrect value: {corrected_value}"
    if rule_hint:
        context += f"\nUser's own description of how to find this value: {rule_hint}"
    if raw_text:
        excerpt = raw_text if len(raw_text) <= 3500 else raw_text[:1500] + "\n...[middle truncated]...\n" + raw_text[-2000:]
        context += f"\n\nDocument excerpt:\n{excerpt}"

    system_msg = (
        "You are an extraction prompt engineer. "
        "A user has corrected a wrongly extracted field value. "
        "Write a concise, GENERALIZABLE extraction rule (1-3 sentences) to add to the prompt so the AI "
        "extracts this field correctly across future documents of this type, not just this one document. "
        "If the user described how to find the value (a position, pattern, or heuristic), build the rule "
        "around that description rather than around the literal value of this one document -- the exact "
        "string here will differ on other documents. "
        "Be specific: name the position/label/pattern to look for, the correct format, "
        "and what to ignore or exclude. "
        "Return ONLY the rule text -- no JSON, no preamble, no bullet points."
    )

    try:
        response = client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user",   "content": context},
            ],
            temperature=0.2,
            max_tokens=200,
        )
        rule = response.choices[0].message.content.strip()
        return rule
    except Exception as e:
        print(f"[ExtractionRule] Error: {e}")
        return f"For field '{field_label}': extract the value that matches '{corrected_value}'. Ignore unrelated reference numbers."


def detect_chat_intent(question: str, fields: dict | None = None, raw_text: str = "", history: list | None = None) -> dict:
    """
    Single LLM call that classifies the user's message into one of seven intents.
    Works in ANY language ? no keyword pre-filter.

    When fields + raw_text are supplied, the classifier can also resolve corrections
    (intent "correct") and identify the target field + corrected value inline,
    so no second LLM call is needed.

    history (list of {q, a} dicts, most recent last) gives the classifier the last
    few turns so it can resolve short/ambiguous follow-ups ? e.g. a bare value
    reply to a question the assistant just asked.

    Returns:
      {
        "intent":           "extract"|"add"|"remove"|"correct"|"qa"|"teach_alias"|"chitchat"|"set_field_type",
        "field_hints":      [...],
        "count_hint":       N | None,
        "fields_to_add":    [...],
        "fields_to_remove": [...],
        # only for intent=="correct":
        "field_name":       str | None,   # snake_case key from fields dict
        "field_label":      str | None,
        "corrected_value":  str | None,   # the actual resolved value
        "rule_hint":        str | None,   # user's own description rephrased
        # only for intent=="set_field_type" (field_name applies here too):
        "field_type":       "number"|"date"|"currency"|"text" | None,
      }
    """
    fields_block = ""
    if fields:
        lines = "\n".join(f"  {k}: {v}" for k, v in list(fields.items())[:20])
        fields_block = f"\n\nCurrently extracted fields:\n{lines}"

    doc_block = ""
    if raw_text:
        excerpt = raw_text if len(raw_text) <= 5000 else raw_text[:2000] + "\n...[truncated]...\n" + raw_text[-3000:]
        doc_block = f"\n\nDocument text (use this to resolve values if the user refers to a location or pattern):\n{excerpt}"

    history_block = ""
    prior_turns = [t for t in (history or []) if t.get("q") or t.get("a")][-4:]
    if prior_turns:
        lines = []
        for t in prior_turns:
            if t.get("q"):
                lines.append(f"  User: {t['q']}")
            if t.get("a"):
                lines.append(f"  Assistant: {t['a']}")
        history_block = (
            "\n\nRecent conversation (most recent last ??? use this to resolve short/ambiguous "
            "follow-ups, e.g. a bare value replying to a question the assistant just asked):\n"
            + "\n".join(lines)
        )

    system_msg = (
        "You are an intent classifier for a document AI assistant.\n"
        "The user may write in ANY language ? English, Malay, mixed, or anything else. Handle all.\n\n"
        "Classify the message into exactly ONE of these intents:\n\n"
        "1. \"extract\" ? user wants to (re-)extract fields from the document and save them,\n"
        "   replacing ALL current fields with the result.\n"
        "   (e.g. \"extract 20 fields\", \"ekstrak semua field\", \"tolong ekstrak 15 field\")\n"
        "   This ALSO covers \"extract those/these to fields\", \"save those as fields\", \"extract the\n"
        "   labels you just gave me\" right after you just listed some labels/values from the document ?\n"
        "   these mean the same as naming those fields directly. Look at your own last answer in the\n"
        "   conversation history to figure out what \"those\"/\"these\"/\"them\" refers to, and set\n"
        "   field_hints to those field names (snake_case, e.g. \"Company Newspaper\" ? \"company_newspaper\").\n"
        "   Do NOT leave field_hints empty just because the names aren't spelled out in this message ? they're\n"
        "   in the immediately preceding turn. If the user did not reference a specific set of labels, leave\n"
        "   field_hints empty (full re-extraction of every field in the document).\n\n"
        "2. \"add\" ? user wants to ADD a field. The name may or may not be given.\n"
        "   Use \"add\" even when NO field name is specified ? e.g. \"i want to add a field\",\n"
        "   \"boleh tambah field lagi?\", \"can you add one more\", \"i want to add one more field\".\n"
        "   Leave fields_to_add empty if no name was given; the system will ask.\n"
        "   (e.g. \"add Bank field\", \"tambah field Bank\", \"include NRIC\", \"i want to add one more field\")\n"
        "   This ALSO covers \"add that/this/it to a field\", \"save that as a field\", \"please add that to a\n"
        "   field\", \"create me the field\", and \"create that field\" right after you just told the user\n"
        "   some value from the document ? these mean the same as\n"
        "   naming the field directly. Look at your own last answer in the conversation history to figure out\n"
        "   what \"that\"/\"this\"/\"it\" refers to (usually the field the user just asked about) and set\n"
        "   fields_to_add to that field's name. For these contextual create/save/add requests, NEVER leave\n"
        "   fields_to_add empty when the immediately preceding question or answer identifies a field; infer a\n"
        "   concise field label from it (e.g. \"Who signed this?\" ? \"signer\"). Do NOT classify these as\n"
        "   \"qa\" just because the field name isn't spelled out in this message ? it's in the immediately\n"
        "   preceding turn.\n\n"
        "3. \"remove\" ? user wants to REMOVE a field. The name may or may not be given.\n"
        "   Use \"remove\" even when NO field name is specified ? e.g. \"i want to remove a field\",\n"
        "   \"buang satu field\", \"delete one of the fields\".\n"
        "   Leave fields_to_remove empty if no name was given; the system will ask.\n"
        "   (e.g. \"remove due date\", \"buang field due date\", \"hapus nombor akaun\")\n\n"
        "4. \"correct\" ? user is saying a field value is WRONG and telling you the right value,\n"
        "   OR describing where/how to find the correct value in the document.\n"
        "   This includes complaints like \"salah\", \"tak betul\", \"wrong\", \"should be\",\n"
        "   \"sepatutnya\", \"patutnya\", \"bukan\", \"isn't\", \"not correct\", etc.\n"
        "   corrected_value rules:\n"
        "   - Set ONLY when the user explicitly states the replacement value in their message\n"
        "     (e.g. 'it should be INV-001', 'the correct one is MYEX123', 'sepatutnya 2024-01-15').\n"
        "   - If the user describes a location or pattern (e.g. 'the name at the bottom'), READ the\n"
        "     document text yourself and extract the actual value from there.\n"
        "   - If the user ONLY says the value is wrong/incorrect/salah WITHOUT giving or describing the\n"
        "     replacement, set corrected_value to null — the guided flow will ask the user for it.\n\n"
        "5. \"qa\" ? user is asking a factual question about the document content, OR\n"
        "   asking the assistant to DO something WITH the document's content ? translate\n"
        "   it, summarize it, explain a term in it, rephrase it, answer a general-knowledge\n"
        "   question related to it, etc. If the request references the document (\"this\n"
        "   document\", \"the topic\", \"it\", or is answerable using the document/its\n"
        "   subject matter), classify as \"qa\" even if it's phrased as an instruction\n"
        "   rather than a question.\n"
        "   (e.g. \"what is the account number?\", \"siapa pemegang akaun?\", \"who signed this?\",\n"
        "   \"translate the topic of this document in korean\", \"summarize this\", \"explain FOB\")\n\n"
        "6. \"teach_alias\" ? user wants to teach an ALTERNATE LABEL/SYNONYM for an\n"
        "   EXISTING field, without complaining a value is wrong. No \"salah\"/\"wrong\"/\n"
        "   \"should be\" framing ? just naming another term that means the same field.\n"
        "   (e.g. \"invoice number should be detected as invoice no too\",\n"
        "   \"also look for 'INV No'\", \"X is also called Y\", \"detect 'Tax Amount' for tax too\")\n"
        "   - field_name: the EXISTING field this teaches a synonym for.\n"
        "   - new_alias: the new label/synonym text to recognize (the term itself, not a sentence).\n\n"
        "7. \"chitchat\" ? greetings, thanks, small talk, or anything genuinely unrelated\n"
        "   to the document (small talk, chit-chat, testing the assistant, etc.). Do NOT\n"
        "   use this for translate/summarize/explain/rephrase requests about the document\n"
        "   or its content ? those are \"qa\" (see above), even though they're not phrased\n"
        "   as questions. Only fall back to \"chitchat\" for genuinely ambiguous messages\n"
        "   with no discernible document-related request at all.\n\n"
        "8. \"set_field_type\" - user wants an EXISTING field to only hold a certain KIND of value: numbers only,\n"
        "   a date, a currency amount, or plain text. This is a rule about the field's FORMAT - not a complaint that\n"
        "   one particular value is wrong, and not a new label for the field.\n"
        "   (e.g. \"can you detect this only numeric value?\", \"numbers only\", \"only digits please\",\n"
        "   \"make this a date\", \"treat it as currency\", \"this should be text not a number\",\n"
        "   \"nombor sahaja\", \"hanya angka\")\n"
        "   - field_name: the EXISTING field the rule applies to.\n"
        "   - field_type: \"number\" (numeric / digits / numbers / integer), \"date\", \"currency\" (a money\n"
        "     amount) or \"text\".\n"
        "   If the user instead gives the actual replacement value (\"it should be 31\") or says the current value\n"
        "   is wrong, that is \"correct\", NOT \"set_field_type\".\n\n"
        "Return ONLY a single JSON object, no other text:\n"
        "{\n"
        "  \"intent\": \"extract\" | \"add\" | \"remove\" | \"correct\" | \"qa\" | \"teach_alias\" | \"chitchat\" | \"set_field_type\",\n"
        "  \"field_hints\": [],\n"
        "  \"count_hint\": null,\n"
        "  \"fields_to_add\": [],\n"
        "  \"fields_to_remove\": [],\n"
        "  \"field_name\": null,\n"
        "  \"corrected_value\": null,\n"
        "  \"rule_hint\": null,\n"
        "  \"new_alias\": null,\n"
        "  \"field_type\": null\n"
        "}\n\n"
        "Rules:\n"
        "- For \"correct\", \"teach_alias\" and \"set_field_type\": set field_name to the exact key from the fields list that matches best (or null if unknown).\n"
        "- field_type: only for \"set_field_type\" - exactly one of \"number\", \"date\", \"currency\", \"text\".\n"
        "- corrected_value must be the actual literal value, never a description. Set null if unsure.\n"
        "- rule_hint: rephrase the user's description as a reusable extraction rule, or null.\n"
        "- new_alias: only for \"teach_alias\" ? the bare label/synonym term, or null if not stated.\n"
        "- For all other intents, field_name/corrected_value/rule_hint/new_alias/field_type must be null."
    )

    user_msg = f"User message: {question}{history_block}{fields_block}{doc_block}"

    try:
        response = client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.0,
            max_tokens=300,
        )
        text = response.choices[0].message.content.strip()
        result = safe_parse_json(text)
        if not result:
            print(f"[ChatIntent] Could not parse JSON from model response: {text[:300]!r}")
        fn = result.get("field_name") or None
        ft = str(result.get("field_type") or "").strip().lower()
        return {
            "intent":           result.get("intent", "qa"),
            "field_hints":      result.get("field_hints") or [],
            "count_hint":       result.get("count_hint") or None,
            "fields_to_add":    result.get("fields_to_add") or [],
            "fields_to_remove": result.get("fields_to_remove") or [],
            "field_name":       fn,
            "field_label":      fn.replace("_", " ").title() if fn else None,
            "corrected_value":  result.get("corrected_value") or None,
            "rule_hint":        result.get("rule_hint") or None,
            "new_alias":        result.get("new_alias") or None,
            "field_type":       ft if ft in ("number", "date", "currency", "text") else None,
        }
    except Exception as e:
        print(f"[ChatIntent] Error: {e}")
        return {
            "intent": "qa", "field_hints": [], "count_hint": None,
            "fields_to_add": [], "fields_to_remove": [],
            "field_name": None, "field_label": None,
            "corrected_value": None, "rule_hint": None, "new_alias": None,
            "field_type": None,
        }


def is_reply_to_guided_prompt(message: str, asked_for: str) -> bool:
    """
    Lightweight classifier for the guided multi-turn flows in document_routes.py
    (document_routes.py's ask_document): right after the assistant asks the user
    to type something specific ("what's the value for Bill To?", "which field
    should I remove?", etc.), decide whether their next message is actually an
    attempt to answer that, or a new question/command that happens not to match
    any hardcoded keyword (e.g. "try to rescan" instead of "can you rescan?").
    A cheap complement to the confirm-before-write step every guided flow already
    ends on - this only decides whether to show that confirmation at all, so a
    wrong guess here is a UX annoyance, not a data-safety issue.
    Returns True (treat as an answer) on error/ambiguity, since the confirm step
    downstream is what actually protects saved data.
    """
    message = (message or "").strip()
    if not message:
        return False
    prompt = (
        f"The assistant just asked the user for: \"{asked_for}\".\n"
        f"The user replied: \"{message}\"\n\n"
        "Is this reply an actual attempt to provide that (even if it's a full "
        "address, a name, a date, an ID, or any other real piece of data) - "
        "or is it a new question, a command (e.g. asking to rescan/retry/redo "
        "something), or an unrelated remark that just doesn't happen to contain "
        "an obvious keyword?\n"
        "Reply with exactly one word: ANSWER or OTHER."
    )
    try:
        response = client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=5,
        )
        verdict = (response.choices[0].message.content or "").strip().upper()
        return "OTHER" not in verdict
    except Exception as e:
        print(f"[GuidedReply] Error: {e}")
        return True


def detect_extraction_request(question: str) -> dict:
    """
    Detect if the user is asking the AI to extract and save fields from the document.
    A fast keyword pre-check avoids an LLM call for normal Q&A messages.
    Returns:
      {
        "is_extraction_request": bool,
        "field_hints": ["specific field names if user listed them, else []"],
        "count_hint": int | None
      }
    """
    empty = {"is_extraction_request": False, "field_hints": [], "count_hint": None}

    _TRIGGERS = ("extract", "pull out", "identify fields", "get all fields",
                 "find all fields", "list all fields", "show all fields",
                 "give me the fields", "retrieve fields")
    if not any(t in question.lower() for t in _TRIGGERS):
        return empty

    system_msg = (
        "You are an intent classifier for a document AI assistant.\n"
        "Decide if the user's message is a request to EXTRACT and SAVE data fields from a document "
        "(not just ask a question about it).\n"
        "Return ONLY a JSON object:\n"
        '{\n'
        '  "is_extraction_request": true/false,\n'
        '  "field_hints": ["snake_case field names if the user listed specific ones, otherwise []"],\n'
        '  "count_hint": N_or_null\n'
        '}\n'
        "Examples:\n"
        '- "extract 20 fields" ? {"is_extraction_request":true,"field_hints":[],"count_hint":20}\n'
        '- "pull out name, NRIC, date of birth" ? {"is_extraction_request":true,"field_hints":["name","nric","date_of_birth"],"count_hint":null}\n'
        '- "extract all fields from this form" ? {"is_extraction_request":true,"field_hints":[],"count_hint":null}\n'
        '- "what is the account number?" ? {"is_extraction_request":false,"field_hints":[],"count_hint":null}'
    )
    try:
        response = client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user",   "content": f"User message: {question}"},
            ],
            temperature=0.0,
            max_tokens=150,
        )
        text = response.choices[0].message.content.strip()
        result = safe_parse_json(text)
        if not result:
            print(f"[ExtractionRequest] Could not parse JSON from model response: {text[:300]!r}")
        return {
            "is_extraction_request": bool(result.get("is_extraction_request", False)),
            "field_hints":           result.get("field_hints") or [],
            "count_hint":            result.get("count_hint") or None,
        }
    except Exception as e:
        print(f"[ExtractionRequest] Error: {e}")
        return empty


def detect_add_field_request(question: str) -> dict:
    """
    Detect if the user wants to ADD one or more named fields to the existing
    extracted set (merge, not replace). Distinct from detect_extraction_request
    which replaces the whole field set.
    Returns:
      {
        "is_add_field": bool,
        "fields_to_add": ["snake_case_field_name", ...]   # fields user wants added
      }
    """
    empty = {"is_add_field": False, "fields_to_add": []}

    q = question.lower()
    _TRIGGERS = (
        "add field", "add a field", "add the field", "add one more", "add more field",
        "another field", "one more field", "also include", "also add", "include field",
        "append field", "insert field", "add new field",
    )
    # Also catch "add <word>" patterns where the intent is clearly field-addition
    _WORD_TRIGGERS = ("add", "include", "append")
    has_trigger = any(t in q for t in _TRIGGERS) or (
        any(t in q for t in _WORD_TRIGGERS) and "field" in q
    )
    if not has_trigger:
        return empty

    system_msg = (
        "You are an intent classifier for a document AI assistant.\n"
        "Decide if the user wants to ADD one or more specific data fields to the document's "
        "existing extracted fields (not re-extract everything ? just add the named field(s)).\n"
        "Return ONLY a JSON object:\n"
        '{\n'
        '  "is_add_field": true/false,\n'
        '  "fields_to_add": ["snake_case_field_name", ...]\n'
        '}\n'
        "Examples:\n"
        '- "add Bank field" ? {"is_add_field":true,"fields_to_add":["bank"]}\n'
        '- "please add one more field which is Bank" ? {"is_add_field":true,"fields_to_add":["bank"]}\n'
        '- "also include branch code and NRIC" ? {"is_add_field":true,"fields_to_add":["branch_code","nric"]}\n'
        '- "add up the total amounts" ? {"is_add_field":false,"fields_to_add":[]}\n'
        '- "what is the bank name?" ? {"is_add_field":false,"fields_to_add":[]}'
    )
    try:
        response = client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user",   "content": f"User message: {question}"},
            ],
            temperature=0.0,
            max_tokens=150,
        )
        text = response.choices[0].message.content.strip()
        result = safe_parse_json(text)
        if not result:
            print(f"[AddField] Could not parse JSON from model response: {text[:300]!r}")
        return {
            "is_add_field":  bool(result.get("is_add_field", False)),
            "fields_to_add": result.get("fields_to_add") or [],
        }
    except Exception as e:
        print(f"[AddField] Error: {e}")
        return empty


_AMOUNT_VALUE_RE = re.compile(r'^[^\d]{0,5}\d[\d,]*(?:\.\d+)?[^\d]{0,3}$')
# Month names the date check recognises: English (anything starting with the
# 3-letter abbreviation, as before) plus the Malay names that DON'T - Mac, Mei,
# Ogos, Okt(ober), Dis(ember). Januari/Februari/April/Jun/Julai/September/
# November already start with their English abbreviation.
_MONTH_NAME_RE = (
    r'(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*'
    r'|mac|mei|ogos|okt(?:ober)?|dis(?:ember)?)'
)
_DATE_VALUE_RE = re.compile(
    '|'.join([
        r'\b\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}\b',   # 12/03/2025
        r'\b\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2}\b',     # 2025-03-12
        # 12 Mar 2025, 12-Mar-2025 (hospital-slip style), 12th March 2025
        r'\b\d{1,2}(?:st|nd|rd|th)?[\s/\-.]+' + _MONTH_NAME_RE + r'\.?[\s/\-.,]+\d{2,4}\b',
        # March 12, 2025
        r'\b' + _MONTH_NAME_RE + r'\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{2,4}\b',
    ]),
    re.IGNORECASE,
)
_ID_FIELD_HINT_RE = re.compile(r'number|\bno\.?\b|\bid\b|code|reference|\bref\b|account', re.IGNORECASE)
_ID_SHAPE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9\-/#.\s]*$')
# A "-" only counts as a minus sign when it isn't glued to a preceding word
# character, so the hyphen in "ABC-123" isn't read as a negative number.
_NUMBER_TOKEN_RE = re.compile(r'(?:(?<![A-Za-z0-9])-)?\d[\d,]*(?:\.\d+)?')
# Multi-line block fields (a name + street + city + country, sometimes an email
# too) are legitimately long - the generic 100-char free-text cap below exists
# to catch the model latching onto a stray sentence, not to reject a real
# address block. Fields whose own name/label reads as one of these get a much
# higher cap instead of the generic one.
_BLOCK_FIELD_HINT_RE = re.compile(
    r'\bbill(?:ed)?[\s_-]*to\b|\bship(?:ped)?[\s_-]*to\b|\bsold[\s_-]*to\b|'
    r'\bremit[\s_-]*to\b|\bdeliver(?:y)?[\s_-]*to\b|\baddress\b|\bmailing\b|'
    r'\bcorrespondence\b|\bregistered\s+office\b',
    re.IGNORECASE,
)


def extract_single_number(value) -> str | None:
    """
    Reduce a value to just its number for a "numbers only" field: "30y" -> "30",
    "RM 1,200" -> "1,200". Returns None when there is no unambiguous answer -
    zero numbers ("thirty") or several ("12 Jan 2024", "30-40") - so the caller
    leaves the value alone instead of guessing which digits the user meant.
    An already-numeric value comes back unchanged.
    """
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    tokens = _NUMBER_TOKEN_RE.findall(str(value))
    if len(tokens) != 1:
        return None
    return tokens[0].rstrip(",")


def is_plausible_field_value(field_type: str, v, field_name: str = "", field_label: str = "") -> bool:
    """
    Per-field-type sanity check shared by every place that trusts a candidate
    value found by regex, AI text re-scan, or vision re-scan ??? the chat
    "correct a field" flow (document_routes.py) and the upload pipeline's
    vision fallback (find_missing_fields_in_image, used inside process_document)
    alike. A candidate that fails this is almost certainly the model latching
    onto the wrong text (a label, an unrelated number, a stray phrase) rather
    than the real value ??? reject it rather than present/apply it as a confident
    guess, so the caller falls through to the next tier (or, if all tiers fail,
    to asking the user directly) instead of showing a wrong "is this correct?"
    suggestion or silently writing bad data.
    """
    v = str(v).strip()
    if not v:
        return False

    # Universal guard, all types: a candidate made ENTIRELY of the field's own
    # label/name words (an exact match, or just a reordering) is a classic sign
    # of grabbing the label instead of the value (e.g. "Payment Due Date" as the
    # value of "payment_due_date"). Word-set comparison, not substring ??? a real
    # value legitimately containing one label word (e.g. "MERIDIAN TRUST BANK"
    # for a "Bank" field) must NOT be rejected just because "bank" appears in it.
    label_words = set((field_label or "").strip().lower().split())
    name_words = set((field_name or "").replace("_", " ").strip().lower().split())
    v_words = v.lower().split()
    if label_words and v_words and not (set(v_words) - label_words):
        return False
    if name_words and v_words and not (set(v_words) - name_words):
        return False

    if field_type == "date":
        return len(v) <= 40 and bool(_DATE_VALUE_RE.search(v))

    if field_type in ("currency", "number"):
        return len(v) <= 20 and bool(_AMOUNT_VALUE_RE.match(v))

    if field_type == "table":
        return True  # structured rows, not a scalar ??? not this validator's job

    # "text" (or unset): distinguish ID/reference-shaped fields from genuinely
    # free-form ones (names, addresses, descriptions) using the field's own
    # name/label as a hint, since this system has no separate "id" type.
    looks_like_id_field = bool(
        _ID_FIELD_HINT_RE.search(field_name or "") or _ID_FIELD_HINT_RE.search(field_label or "")
    )
    if looks_like_id_field:
        return len(v) <= 30 and len(v.split()) <= 3 and bool(_ID_SHAPE_RE.match(v))

    looks_like_block_field = bool(
        _BLOCK_FIELD_HINT_RE.search(field_name or "") or _BLOCK_FIELD_HINT_RE.search(field_label or "")
    )
    if looks_like_block_field:
        return len(v) <= 500

    # Genuinely free text: can't validate a "shape" strictly, so just reject
    # obvious prose (a full sentence/description is never a name/address/etc.)
    return len(v) <= 100


def find_field_value_in_text(raw_text: str, labels: list):
    """
    Deterministic label:value lookup for a single field, given a list of candidate
    label strings (field label, field name, user-defined aliases). Mirrors the
    Pass 1/2 alias fallback used during initial document processing (see
    process_document), so a flagged field can be re-located in the chat "correct
    a field" flow before falling back to asking the user to type the value.
    Returns (value, matched_label) or (None, None).
    """
    if not raw_text or not labels:
        return None, None
    search_text = re.sub(r'\s*\|\s*', '  ', raw_text)
    value_pat = r'([A-Za-z0-9][\w\-/]{2,40})'
    for label in labels:
        if not label:
            continue
        escaped = re.escape(label.strip())
        try:
            match = re.search(escaped + r'\s*[:#]\s*' + value_pat, search_text, re.IGNORECASE)
            if not match:
                match = re.search(escaped + r'[ \t]{2,}' + value_pat, search_text, re.IGNORECASE)
            if match:
                return match.group(1), label.strip()
        except Exception:
            continue
    return None, None


def find_field_value_in_image(file_path: str, is_pdf: bool, field_label: str, aliases: list = None) -> str:
    """
    Last-resort fallback for the chat "correct a field" flow: some values never
    make it into raw_text at all (e.g. a bold two-column header block the OCR/ODL
    pass skipped), so no amount of re-reading raw_text can find them. This looks
    at the original page image(s) again with the vision model, which can see
    layout that text extraction missed. Returns the found value, or None.
    """
    try:
        if is_pdf:
            pages = pdf_pages_to_b64(file_path, dpi=150, max_dim=1240)
        else:
            b64, media_type, w, h = encode_image(file_path)
            pages = [(b64, media_type, w, h)]
    except Exception as e:
        print(f"[SmartLookup] Could not render source file for vision re-scan: {e}")
        return None

    alias_str = f" (also called: {', '.join(aliases)})" if aliases else ""
    # A "Bill To"/"Ship To"/address-shaped field is a whole block (name, street,
    # city, country, sometimes an email) - give it a much higher length cap and
    # more room to answer than a short scalar value like a date or ID gets.
    is_block = bool(
        _BLOCK_FIELD_HINT_RE.search(field_label or "")
        or any(_BLOCK_FIELD_HINT_RE.search(a or "") for a in (aliases or []))
    )
    max_len = 500 if is_block else 100
    max_tokens = 200 if is_block else 60
    prompt = (
        f"Look at this document image carefully, including any header or label/value "
        f"blocks. Find the value for \"{field_label}\"{alias_str}.\n"
        "Return ONLY the exact value as printed, with no extra words or explanation"
        + (" - if it spans multiple lines (e.g. a name and address block), include all of them.\n" if is_block else ".\n")
        + "If it is not present anywhere in the image, return exactly: NONE"
    )

    for b64, media_type, _w, _h in pages[:3]:
        try:
            answer = call_qwen_vision(b64, media_type, prompt, max_tokens=max_tokens)
        except Exception as e:
            print(f"[SmartLookup] Vision re-scan error: {e}")
            continue
        cleaned = (answer or "").strip().strip('"').strip("'")
        if cleaned and cleaned.upper() != "NONE" and len(cleaned) <= max_len:
            return cleaned
    return None


def find_missing_fields_in_image(file_path: str, fields_info: list) -> dict:
    """
    Batched sibling of find_field_value_in_image, used inside the main
    processing pipeline (not the chat flow): given several scalar fields that
    came back empty from text-based extraction, render the original PDF page(s)
    and ask the vision model to find all of them in one call. Some values (e.g.
    a bold two-column header block) never make it into the text layer at all,
    so re-reading text can never find them ??? only looking at the image again can.
    fields_info: list of DocumentField.to_dict()-shaped dicts (field_name,
    field_label, aliases). Returns a flat dict of field_name -> value for
    whatever was found; fields still not found are simply omitted.
    """
    if not fields_info or not file_path:
        return {}
    try:
        pages = pdf_pages_to_b64(file_path, dpi=150, max_dim=1240)
    except Exception as e:
        print(f"[Pipeline] Could not render PDF for vision fallback: {e}")
        return {}
    if not pages:
        return {}

    field_lines = []
    for f in fields_info:
        aliases = [a.strip() for a in (f.get("aliases") or "").split(",") if a.strip()]
        alias_str = f" (also called: {', '.join(aliases)})" if aliases else ""
        field_lines.append(f"- \"{f['field_label']}\" (key: {f['field_name']}){alias_str}")

    prompt = (
        "Look at this document image carefully, including any header or label/value blocks.\n"
        "Find the values for these fields:\n" + "\n".join(field_lines) + "\n\n"
        "Return ONLY a JSON object mapping each key to its exact value as printed "
        "(e.g. {\"invoice_number\": \"00000135\"}). Omit any key you cannot find. "
        "Return {} if none are present. No other text."
    )

    requested = {f["field_name"] for f in fields_info}
    found = {}
    for b64, media_type, _w, _h in pages[:2]:
        try:
            answer = call_qwen_vision(b64, media_type, prompt, max_tokens=500)
        except Exception as e:
            print(f"[Pipeline] Vision fallback call error: {e}")
            continue
        try:
            match = re.search(r'\{.*\}', answer, re.DOTALL)
            page_result = json.loads(match.group() if match else answer)
        except Exception:
            page_result = {}
        for k, v in page_result.items():
            # Only keys we asked for. The model sometimes answers with a key of
            # its own (e.g. "code": <text printed on the page>) and the caller
            # has no schema entry to validate it against, so it would be saved
            # as a brand-new field the user never defined.
            if k not in requested:
                print(f"[Pipeline] Vision re-scan ignored unrequested key '{k}': {v!r}")
                continue
            if k not in found and v not in (None, "", "null", "None", "N/A", "n/a"):
                found[k] = v
        if len(found) >= len(fields_info):
            break
    return found


def extract_fields_on_demand(raw_text: str, field_hints: list, count_hint, doc_type: str = "") -> dict:
    """
    Extract fields from a document's OCR text on user request via the ASK chatbox.
    Returns a flat dict of snake_case_field_name ? value (nulls removed).
    """
    if not raw_text:
        return {}

    if field_hints:
        field_instruction = f"Extract these specific fields: {', '.join(field_hints)}."
    elif count_hint:
        field_instruction = f"Identify and extract the {count_hint} most important data fields from this document."
    else:
        field_instruction = "Identify and extract every key data field present in this document."

    if len(raw_text) > 8000:
        text_block = raw_text[:4000] + "\n...[middle truncated]...\n" + raw_text[-4000:]
    else:
        text_block = raw_text

    doc_hint = f" This is a {doc_type.replace('_', ' ')} document." if doc_type else ""

    system_msg = (
        "You are a document field extraction expert."
        f"{doc_hint}\n"
        "Extract data fields from the document text below and return them as a flat JSON object.\n"
        "Rules:\n"
        "- Keys: snake_case (e.g. full_name, date_of_birth, account_number)\n"
        "- Values: exact text from the document ? do not paraphrase\n"
        "- If a field naturally holds multiple discrete items (e.g. a list of topics, responsibilities,\n"
        "  symptoms, attendees), return its value as a JSON array of strings, one item per element ?\n"
        "  do NOT join them into a single comma-separated string\n"
        "- For checkboxes/radio buttons: record the selected option(s) as a string value\n"
        "- Omit fields with no value (do not include null entries)\n"
        "- Return ONLY the JSON object, no explanation or markdown\n"
    )
    user_msg = f"{field_instruction}\n\nDocument text:\n{text_block}"

    try:
        response = client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.1,
            max_tokens=2500,
        )
        text = response.choices[0].message.content.strip()
        match = re.search(r'\{.*\}', text, re.DOTALL)
        result = json.loads(match.group() if match else text)
        return {
            k: v for k, v in result.items()
            if v is not None and str(v).strip() not in ("", "null", "None", "N/A", "n/a")
        }
    except Exception as e:
        print(f"[ExtractionOnDemand] Error: {e}")
        return {}


# ?????? iVault Recognize ???????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

# Maps iVault propertyType integer ??? human-readable hint for the prompt
_IVAULT_TYPE_HINTS = {
    1: "text",
    2: "date (keep format exactly as it appears in the document)",
    3: "decimal number (digits and decimal point only, no currency symbols)",
    4: "true or false",
    5: "text",
}


def _enhance_image_for_ocr(image_path: str) -> str:
    """
    Apply contrast + sharpness enhancement to a scanned image before OCR.
    Helps Qwen read low-quality or slightly blurry scanner output.
    Returns the path to the enhanced temp file (caller must delete it).
    """
    import tempfile
    from PIL import ImageEnhance, ImageFilter

    with Image.open(image_path) as img:
        img = img.convert("RGB")
        img = ImageEnhance.Contrast(img).enhance(1.6)
        img = ImageEnhance.Sharpness(img).enhance(2.0)
        img = ImageEnhance.Brightness(img).enhance(1.1)
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        img.save(tmp.name, format="JPEG", quality=95)
        print(f"[iVault OCR] Enhanced image saved: {tmp.name}")
        return tmp.name


def _ocr_file_for_ivault(file_path: str) -> tuple:
    """
    OCR a file for iVault recognition.
    Returns (raw_text, page_images) where page_images is a list of (b64, media_type)
    tuples used for Vision-based extraction on scanned files.

    Uses 300 DPI for scanned PDFs and image enhancement for all image inputs
    to maximise OCR accuracy on scanner-captured documents.
    """
    ext = os.path.splitext(file_path)[1].lower()
    is_pdf = ext == ".pdf"

    ocr_prompt = (
        "Transcribe ALL visible text from this document page exactly as it appears, "
        "line by line. For tables: columns separated by | (pipe). "
        "Output ONLY the transcribed text, nothing else."
    )

    if is_pdf:
        if is_scanned_pdf(file_path):
            # 300 DPI ??? much sharper text than the default 150 DPI used elsewhere
            print("[iVault OCR] Scanned PDF detected ??? rendering at 300 DPI for higher accuracy")
            ocr_pages = pdf_pages_to_b64(file_path, dpi=300, max_dim=2480)
            texts = []
            for b64, mt, _, _ in ocr_pages:
                page_text = clean_qwen_output(
                    call_qwen_vision(b64, mt, ocr_prompt, max_tokens=4000)
                )
                texts.append(page_text)
            raw_text = "\n\n".join(texts)
            page_images = [(b64, mt) for b64, mt, _, _ in ocr_pages]
            print(f"[iVault OCR] Vision OCR done: {len(raw_text)} chars, {len(page_images)} pages")
            return raw_text, page_images
        else:
            print("[iVault OCR] Digital PDF detected ??? using ODL/pdfplumber")
            try:
                raw_text, _ = extract_pdf_text_with_odl(file_path)
            except Exception:
                raw_text = ""
            if not raw_text.strip():
                try:
                    raw_text = extract_pdf_text_with_tables(file_path)
                except Exception:
                    raw_text = ""
            print(f"[iVault OCR] Text extraction done: {len(raw_text)} chars")
            return raw_text, []
    else:
        # Image file ??? enhance first, then Vision OCR at higher resolution
        print("[iVault OCR] Image file ??? enhancing then Vision OCR at 1536px")
        enhanced_path = None
        try:
            enhanced_path = _enhance_image_for_ocr(file_path)
            b64, mt, _, _ = encode_image(enhanced_path, max_size=1536, quality=95)
        except Exception as enh_err:
            print(f"[iVault OCR] Enhancement failed ({enh_err}), using original")
            b64, mt, _, _ = encode_image(file_path, max_size=1536, quality=95)
        finally:
            if enhanced_path and os.path.exists(enhanced_path):
                os.remove(enhanced_path)

        raw_text = clean_qwen_output(
            call_qwen_vision(b64, mt, ocr_prompt, max_tokens=4000)
        )
        print(f"[iVault OCR] Vision OCR done: {len(raw_text)} chars")
        return raw_text, [(b64, mt)]


def recognize_for_ivault(file_path: str, properties: list) -> dict:
    if not properties:
        return {}

    t_start = time.time()
    print(f"[iVault Recognize] Starting ??? {len(properties)} properties")

    ext    = os.path.splitext(file_path)[1].lower()
    is_pdf = ext == ".pdf"

    image_b64     = None
    media_type    = None
    raw_text      = ""
    enhanced_path = None

    ocr_prompt = (
        "Transcribe ALL visible text from this document exactly as it appears, line by line.\n"
        "For tables: output a header row first, then each data row with columns separated by | (pipe).\n"
        "Example:\n"
        "  Name | Date | Amount\n"
        "  John | 1/1/2026 | 100.00\n"
        "Transcribe ALL rows ??? do NOT stop early or summarise.\n"
        "Output ONLY the transcribed text, nothing else."
    )

    if is_pdf:
        if is_scanned_pdf(file_path):
            print("[iVault Recognize] Step 1: Scanned PDF ??? Vision OCR at 300 DPI...")
            ocr_pages = pdf_pages_to_b64(file_path, dpi=300, max_dim=2480)
            if not ocr_pages:
                return {p["propertyId"]: None for p in properties}
            n_pages   = len(ocr_pages)
            all_texts = []
            for i, (b64, mt, _, _) in enumerate(ocr_pages):
                _set_lm_slot(f"OCR page {i+1}/{n_pages}...", i / n_pages * 1.5, (i+1) / n_pages * 1.5)
                page_text = clean_qwen_output(
                    call_qwen_vision(b64, mt, ocr_prompt, max_tokens=4000)
                )
                all_texts.append(page_text)
                print(f"[iVault Recognize] Page {i+1} OCR: {len(page_text)} chars")
            raw_text   = "\n\n".join(all_texts)
            image_b64  = ocr_pages[0][0]
            media_type = ocr_pages[0][1]
        else:
            print("[iVault Recognize] Step 1: Digital PDF ??? text extraction...")
            try:
                raw_text, _ = extract_pdf_text_with_odl(file_path)
            except Exception:
                raw_text = ""
            if not raw_text.strip():
                try:
                    raw_text = extract_pdf_text_with_tables(file_path)
                except Exception:
                    raw_text = ""
            print(f"[iVault Recognize] Text extraction: {len(raw_text)} chars")
    else:
        print("[iVault Recognize] Step 1: Image ??? enhancing + Vision OCR...")
        _set_lm_slot("OCR...", 0.0, 1.5)
        try:
            enhanced_path     = _enhance_image_for_ocr(file_path)
            source_for_encode = enhanced_path
            print(f"[iVault Recognize] Image enhanced: {enhanced_path}")
        except Exception as enh_err:
            print(f"[iVault Recognize] Enhancement failed ({enh_err}), using original")
            source_for_encode = file_path
        try:
            for _sz, _q in [(1536, 95), (1024, 85), (768, 75), (512, 70)]:
                try:
                    b64, mt, _, _ = encode_image(source_for_encode, max_size=_sz, quality=_q)
                    print(f"[iVault Recognize] Trying Vision OCR at {_sz}px ({len(b64)//1024}KB)...")
                    raw_text  = clean_qwen_output(
                        call_qwen_vision(b64, mt, ocr_prompt, max_tokens=4000)
                    )
                    image_b64  = b64
                    media_type = mt
                    print(f"[iVault Recognize] OCR done: {len(raw_text)} chars")
                    break
                except Exception as e:
                    if "400" in str(e) or "failed to process image" in str(e).lower():
                        print(f"[iVault Recognize] Vision rejected at {_sz}px, retrying smaller...")
                        continue
                    raise
        finally:
            if enhanced_path and os.path.exists(enhanced_path):
                os.remove(enhanced_path)
                enhanced_path = None

    if not raw_text.strip() and not image_b64:
        print("[iVault Recognize] Step 1 returned nothing ??? returning nulls")
        return {p["propertyId"]: None for p in properties}

    detected_lang = detect_language(raw_text)
    print(f"[iVault Recognize] Step 2: Language detected ??? {detected_lang}")

    print(f"[iVault Recognize] Step 3: Extracting {len(properties)} iVault properties...")
    _set_lm_slot("Extracting fields...", 1.5, 3.5)

    field_lines, json_null_pairs = [], []
    for prop in properties:
        pid      = prop["propertyId"]
        pname    = prop["propertyName"]
        ptype    = prop.get("propertyType", 1)
        required = prop.get("isRequired", False)
        hint     = _IVAULT_TYPE_HINTS.get(ptype, "text")
        tag      = " [REQUIRED]" if required else ""
        field_lines.append(f'- "{pname}"{tag} ({hint}) ??? output key: "{pid}"')
        json_null_pairs.append(f'"{pid}": ""')

    fields_block  = "\n".join(field_lines)
    json_template = "{" + ", ".join(json_null_pairs) + "}"

    lang_note = (
        f"\nNOTE: Document is in {detected_lang}. Copy values exactly as they appear ??? do not translate."
        if detected_lang != "english" else ""
    )

    extraction_prompt = f"""Look at this document and extract the values for the fields listed below.
The field names may be in Malay or another language ??? use semantic understanding to find the matching value.{lang_note}

CRITICAL READING RULES:
- PRIMARY PERSON: The main applicant's name appears at the TOP of the form next to "Name:" label ??? use THAT name, NOT emergency contact, witness, guarantor, or approver names
- REASON/REMARKS: Look for "Reason:", "Sebab:", "Tujuan:", or a text area ??? extract the short text inside it
- DATE FIELDS: "Date From"/"Mula" = start date, "Date Until"/"Tamat" = end date
- Read ALL text carefully including handwritten or typed entries in boxes/fields

FIELDS TO EXTRACT:
{fields_block}

Rules:
1. Output ONLY a JSON object ??? no markdown, no explanation
2. Use the UUID strings (output key) as JSON keys ??? NOT the field names
3. Copy values exactly as they appear in the document
4. For decimal/number fields: return only the numeric value (e.g. 1234.56)
5. If a value is not found: use null
6. PRIMARY PERSON RULE: Use the main applicant/employee name at the TOP of the form ??? NOT emergency contact, witness, or approver
7. DATE RANGE RULE: "Starting From"/"Date From"/"Mula" ??? start date; "Ending At"/"Date Until"/"Tamat" ??? end date
8. REASON RULE: For "Reasons"/"Reason"/"Remarks"/"Sebab"/"Tujuan" ??? find the short reason/purpose text

Reply with ONLY this JSON:
{json_template}"""

    try:
        if image_b64 and media_type:
            response = call_qwen_vision(image_b64, media_type, extraction_prompt, max_tokens=1000)
        else:
            text_block = raw_text[:8000] if len(raw_text) > 8000 else raw_text
            response   = call_qwen_text(
                extraction_prompt.replace(
                    "Look at this document and extract",
                    "Read this document text and extract",
                ) + f"\n\nDOCUMENT TEXT:\n\"\"\"\n{text_block}\n\"\"\"",
                max_tokens=600,
            )
    except Exception as e:
        # Final extraction failed (stage=final_extraction) after Step 1 already
        # spent time OCR'ing the document ??? don't discard that OCR work by
        # letting this propagate to the generic job-level error handler.
        # Return a partial result: nulls for every property (same convention as
        # the "Step 1 returned nothing" case above), plus reserved (non-UUID)
        # keys so callers can tell this apart from "model found nothing".
        print(f"[iVault Recognize] Step 3 (final_extraction) failed for {file_path}: {e}")
        result = {p["propertyId"]: None for p in properties}
        result["_extractionFailed"] = True
        result["_extractionError"]  = str(e)
        result["_ocrText"]          = raw_text
        return result

    result = safe_parse_json(response)

    for prop in properties:
        if prop["propertyId"] not in result:
            result[prop["propertyId"]] = None

    found   = sum(1 for v in result.values() if v is not None)
    elapsed = time.time() - t_start
    print(f"[iVault Recognize] Done ??? {found}/{len(properties)} fields extracted ({elapsed:.1f}s)")
    return result


# ?????? User-corrected type learning ??????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

def learn_type_correction(old_type: str, new_type: str, field_keys: list) -> None:
    """
    Called when a user manually renames a document type.
    Migrates the learned field structure from old_type ??? new_type
    so future documents of this kind are classified and extracted better.
    """
    new_type = new_type.lower().strip().replace(" ", "_").replace("-", "_")
    if not new_type or new_type == old_type:
        return

    with _learned_structures_lock:
        structures = load_learned_structures()

        # Carry over existing fields from old type as base, then layer current doc's fields on top
        base_fields = structures.get(old_type, [])
        merged = list(dict.fromkeys(base_fields + field_keys))  # deduplicated, order-preserved
        structures[new_type] = merged

        try:
            _atomic_write_learned_structures(structures)
        except Exception as e:
            print(f"[Learning] ??? Failed to save correction: {e}")
            return

    print(f"[Learning] ??? Type corrected: '{old_type}' ??? '{new_type}' ({len(merged)} fields)")






