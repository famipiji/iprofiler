import os
import uuid
import json
import time
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from flask import Blueprint, request, jsonify, Response, stream_with_context
from werkzeug.utils import secure_filename
from extensions import db
from document_model import Document, DocumentType, DocumentField, ChatMessage, Batch, SourceFile, DocumentJob
from qwen_service import process_document, _init_lm_progress
from pdf_service import pdf_to_images
import batch_service

document_bp = Blueprint("documents", __name__)

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "pdf", "docx"}

MAX_FILES_PER_UPLOAD = 20
MAX_FILE_SIZE_BYTES  = 100 * 1024 * 1024  # 100MB — mirrors app.py's MAX_CONTENT_LENGTH, per-file

# A multi-file upload's per-file time is dominated by waiting on the vision/
# LLM model server (network I/O), not local CPU work, so files can usefully
# overlap instead of running strictly one-at-a-time. Kept conservative by
# default: a single-GPU LM Studio/Ollama backend still serializes inference
# internally even when it accepts concurrent HTTP requests (it just queues
# them server-side), so this only buys back the CPU/IO time between model
# calls (PDF rasterization, ODL/pdfplumber text extraction, DB writes) unless
# the backend is confirmed to actually run completions in parallel (e.g. a
# multi-GPU or batched-serving setup) — raise it only in that case.
MAX_CONCURRENT_UPLOADS = max(1, int(os.environ.get("MAX_CONCURRENT_UPLOADS", "2")))

# ── Shared FX-rate cache ──────────────────────────────────────────────────────
# Both currency-conversion code paths in /ask (the guided "add field" flow and
# the inline currency-conversion check) hit the same third-party API on every
# relevant chat message with no caching, coupling /ask's latency/availability
# to a third party. Cache per currency pair for a short TTL instead.
_FX_RATE_CACHE = {}   # {(from_cur, to_cur): (rate, fetched_at)}
_FX_RATE_CACHE_TTL = 180  # seconds
_FX_RATE_LOCK = threading.Lock()


def _fetch_live_fx_rate(from_cur: str, to_cur: str):
    """Return (rate, source) where source is 'cache', 'live', 'stale_cache',
    or None if never available. A cache hit (fresh or stale) never touches
    the network, so /ask stays fast even if the FX API is slow or down."""
    key = (from_cur, to_cur)
    now = time.time()
    with _FX_RATE_LOCK:
        cached = _FX_RATE_CACHE.get(key)
        if cached and (now - cached[1]) < _FX_RATE_CACHE_TTL:
            return cached[0], "cache"
    try:
        url = f"https://open.er-api.com/v6/latest/{from_cur}"
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
        rate = data.get("rates", {}).get(to_cur)
    except Exception as e:
        print(f"[FX] Rate lookup failed for {from_cur}->{to_cur}: {e}")
        rate = None
    if rate:
        with _FX_RATE_LOCK:
            _FX_RATE_CACHE[key] = (rate, now)
        return rate, "live"
    with _FX_RATE_LOCK:
        cached = _FX_RATE_CACHE.get(key)
    if cached:
        return cached[0], "stale_cache"  # better than nothing if the API is down
    return None, None

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ?????? In-memory job store ?????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????
# { job_id: { "files": [...], "status": "pending/done/error" } }
# Every completed job's full extracted fields/text stays here forever unless
# swept - JOB_TTL_SECONDS + _sweep_expired_jobs() below bound that growth.
jobs = {}
jobs_lock = threading.Lock()
JOB_TTL_SECONDS = 45 * 60  # 45 minutes after a job finishes


def _sweep_expired_jobs():
    """Remove job entries that reached 'done' more than JOB_TTL_SECONDS ago.
    Call with jobs_lock already held. Only terminal jobs are ever removed -
    an in-progress job has no completed_at yet and is left untouched, so this
    never races with an active /progress poll."""
    now = time.time()
    expired = [
        jid for jid, j in jobs.items()
        if j.get("status") == "done" and j.get("completed_at") and (now - j["completed_at"]) > JOB_TTL_SECONDS
    ]
    for jid in expired:
        del jobs[jid]


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def update_job_step(job_id: str, file_index: int, step: str = None, step_num: float = None,
                     pages_done: int = None, pages_total: int = None):
    """Called from qwen_service to push live step label + progress to SSE."""
    if not job_id:
        return
    with jobs_lock:
        if job_id in jobs and file_index < len(jobs[job_id]["files"]):
            f = jobs[job_id]["files"][file_index]
            if step is not None:
                f["step"]     = step
            if step_num is not None:
                f["step_num"] = step_num
            if pages_done is not None:
                f["pages_done"] = pages_done
            if pages_total is not None:
                f["pages_total"] = pages_total


def _values_are_near_duplicate(a: str, b: str) -> bool:
    """True if two field values are close enough (identical, or one is a
    substring/superset of the other after whitespace/case normalization)
    that flagging them as "different candidates" would just be noise - e.g.
    an English-only reading vs. a bilingual letterhead line naming the same
    ministry."""
    _a = " ".join(a.strip().lower().split())
    _b = " ".join(b.strip().lower().split())
    return _a == _b or _a in _b or _b in _a


def _lookup_alias_value(raw_text: str, aliases: list):
    """Two tiers of lookup for what a field's taught alias(es) resolve to in
    this document's text:
      1. Literal label search (find_field_value_in_text) - cheap, no LLM
         call, catches the common case where the alias text is genuinely
         printed as a label somewhere in the document.
      2. Q&A fallback, only when tier 1 finds nothing - some aliases describe
         a value that's never introduced by a matching label at all (e.g. a
         letterhead name, or any other positional/contextual value), so a
         text search can never find it no matter how the alias is worded.
         Asks the same question "Let AI find it" would (answer_question).
         Stops at the first alias that yields a usable answer, to bound how
         many extra LLM calls a field with several aliases can cost.
    Returns (value, source) or (None, None) if nothing usable was found."""
    from qwen_service import find_field_value_in_text, answer_question
    try:
        found_val, matched_alias = find_field_value_in_text(raw_text, aliases)
        if found_val and found_val.strip():
            return found_val, f"alias '{matched_alias}'"
    except Exception as e:
        print(f"[Ambiguity] text-search tier failed for aliases {aliases}: {e}")

    print(f"[Ambiguity] text search found nothing for aliases {aliases}, trying Q&A fallback...")
    for alias in aliases:
        try:
            qa_result = answer_question(raw_text, f"what is the {alias.lower()}?", history=[])
            quote = (qa_result.get("source_quote") or "").strip()
        except Exception as e:
            print(f"[Ambiguity] Q&A fallback failed for alias '{alias}': {e}")
            continue
        print(f"[Ambiguity] Q&A answer for alias '{alias}' -> {qa_result!r}")
        if quote:
            return quote, f"alias '{alias}' (via Q&A)"
    return None, None


def _detect_field_ambiguities(document_type_name: str, fields_dict: dict, raw_text: str) -> dict:
    """For every field on this document type that has a taught alias, always
    surface a "matches" badge so the user can double-check it - whether or
    not a genuinely different value could be confirmed automatically. If the
    alias resolves to a real, different value, both are offered as ready-to-
    pick candidates (the "which one did you mean" situation as confirm_alias's
    doc-only apply, just caught proactively on a fresh upload). If no
    confirmed alternate is found (nothing matched, too long, or a near-
    duplicate of the extracted value), the badge still appears with a
    "let AI check" placeholder instead of disappearing outright - see
    ambiguity_ai_find below for the on-demand lookup that placeholder
    triggers. Deliberately never picks a side when a real alternate exists:
    which value is "correct" can genuinely vary document to document (a
    broad distribution list vs. a specific ministry name, say), so the
    choice is left to the user via the badge/picker instead of ever being
    decided automatically."""
    if not document_type_name or not raw_text or not fields_dict:
        return {}
    dt = DocumentType.query.filter_by(name=document_type_name).first()
    if not dt:
        return {}
    ambiguities = {}
    for df_obj in dt.fields:
        if not df_obj.aliases:
            continue
        fn = df_obj.field_name
        if fn not in fields_dict:
            continue
        current_val = fields_dict.get(fn)
        if not isinstance(current_val, str) or not current_val.strip():
            continue
        aliases = [a.strip() for a in df_obj.aliases.split(",") if a.strip()]
        if not aliases:
            continue

        alt_val, alt_source = _lookup_alias_value(raw_text, aliases)

        confirmed = False
        if alt_val:
            if len(alt_val) > 300:
                print(f"[Ambiguity] '{fn}': alternate value too long ({len(alt_val)} chars), treating as unconfirmed.")
            elif _values_are_near_duplicate(current_val, alt_val):
                print(f"[Ambiguity] '{fn}': alternate value is a near-duplicate of current value ({alt_source}), treating as unconfirmed.")
            else:
                confirmed = True

        if confirmed:
            print(f"[Ambiguity] '{fn}': flagging conflict - extracted={current_val!r} vs {alt_source}={alt_val!r}")
            ambiguities[fn] = [
                {"value": current_val, "source": "extracted"},
                {"value": alt_val, "source": alt_source},
            ]
        else:
            print(f"[Ambiguity] '{fn}': no confirmed alternate - badge shown with on-demand check.")
            ambiguities[fn] = [
                {"value": current_val, "source": "extracted"},
                {"value": None, "source": "unconfirmed"},
            ]
    return ambiguities


def _save_document_to_db(result: dict, original_filename: str, unique_filename: str, ext: str, tenant_id: str = "default", document_class: str = "production", force_document_type: str = None):
    """
    Save one processed result dict to the DB and auto-register unknown types.
    Returns the saved Document as a dict (via to_dict()).

    This is a shared helper used by both the normal single-doc path and the
    new split-PDF path (which calls it once per logical record found).
    """
    from app import app
    with app.app_context():
        # The AI may classify a document as a type that isn't registered in
        # the Document Type Manager. Rather than auto-creating a new type,
        # mark such documents as "unclassified" so they can be reviewed and
        # assigned to a type manually.
        doc_type_name = result["document_type"]
        # AI returns snake_case (e.g. "tax_invoice") but DB stores display names
        # (e.g. "Tax Invoice"). Normalize both to lowercase+spaces for comparison.
        def _norm(s):
            return s.lower().replace("_", " ").replace("-", " ").strip()

        all_active = DocumentType.query.filter_by(is_active=True).all()
        if force_document_type:
            # Caller pinned the type (e.g. a sample uploaded from the Doc Type
            # workspace). Honor it instead of AI classification so the doc
            # reliably attaches to the type being edited.
            forced = next((t for t in all_active if _norm(t.name) == _norm(force_document_type)), None)
            final_type = forced.name if forced else force_document_type
        else:
            ai_norm = _norm(doc_type_name)
            known_type = next((t for t in all_active if _norm(t.name) == ai_norm), None)
            if known_type:
                final_type = known_type.name
            else:
                final_type = "unclassified"
                print(f"[Classify] AI detected type '{doc_type_name}' which is not registered ??? marked as 'unclassified'")

        doc = Document(
            tenant_id=tenant_id or "default",
            document_class=document_class or "production",
            filename=original_filename,
            unique_upload_filename=unique_filename,
            storage_key=unique_filename,
            file_type=ext,
            document_type=final_type,
            extractedText=result.get("raw_text", ""),
            confidence=result["confidence"],
        )
        doc.set_fields(result["fields"])
        if result.get("field_sources"):
            import json as _json_fs
            doc.field_sources = _json_fs.dumps(result["field_sources"])
        if result.get("text_blocks"):
            doc.set_text_blocks(result["text_blocks"])
        try:
            print(f"[Ambiguity] Running detection for document type '{final_type}'...")
            _ambig = _detect_field_ambiguities(final_type, result["fields"], result.get("raw_text", ""))
            # An alias match that overrode a non-empty model answer during
            # extraction itself (qwen_service's Pass 0/3, e.g. a long AI blob
            # beaten by a reversed-layout alias match) never shows up as a
            # conflict to _detect_field_ambiguities above - by the time that
            # runs, doc.fields already only holds the alias's own value, so
            # comparing the alias against itself finds nothing to flag. Merge
            # in the model's discarded answer as a second candidate here,
            # straight from where the override actually happened.
            for _fn, _conflict in (result.get("field_alias_conflicts") or {}).items():
                # Only skip if _detect_field_ambiguities already confirmed a
                # real second candidate for this field - an "unconfirmed"
                # placeholder entry (value None) should still be overwritten
                # with this genuine conflict, since it's strictly better info.
                if _fn in _ambig and _ambig[_fn][1].get("value") is not None:
                    continue
                _model_val = str(_conflict.get("model_value") or "")
                # Skip rather than truncate-and-keep: a truncated value could
                # get silently applied for real if the user picks it, which
                # is worse than just not flagging this one as pickable.
                if not _model_val or len(_model_val) > 300:
                    continue
                _ambig[_fn] = [
                    {"value": _conflict["alias_value"], "source": f"alias '{_conflict['alias_label']}'"},
                    {"value": _model_val, "source": "extracted"},
                ]
            print(f"[Ambiguity] Result: {_ambig!r}")
            doc.set_field_ambiguities(_ambig)
        except Exception as e:
            import traceback
            print(f"[Ambiguity] Detection crashed for document type '{final_type}': {e}")
            traceback.print_exc()
        db.session.add(doc)
        db.session.commit()
        return doc.to_dict()


def _check_job_done(job_id: str):
    """Call with jobs_lock already held. Marks the job 'done' once every file
    has reached a terminal status (done/error/cancelled)."""
    if all(f["status"] in ("done", "error", "cancelled") for f in jobs[job_id]["files"]):
        jobs[job_id]["status"] = "done"
        jobs[job_id]["completed_at"] = time.time()


def _discard_cancelled_file(job_id: str, file_index: int, file_path: str):
    """A file finished its (uninterruptible) processing step after cancel was
    requested mid-flight - mark it cancelled instead of done and drop the
    result on the floor rather than saving it, so a cancelled upload never
    ends up in the document list no matter how far it got."""
    with jobs_lock:
        jobs[job_id]["files"][file_index]["status"] = "cancelled"
        _check_job_done(job_id)
    try:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
    except Exception:
        pass


def _update_document_job_status(job_id: str, file_index: int, status: str, result_ref: int = None, last_error: str = None):
    """
    Dual-write helper: sync DocumentJob row with in-memory job progress (Phase 1, Task 3).

    Updates the persistent DocumentJob table to mirror the in-memory job dict,
    so batch status survives a restart and Task 4's worker can see what the
    synchronous path already processed.

    Sets attempts=1 even though this synchronous path doesn't retry - Task 4's
    worker expects attempts>0 on terminal jobs (attempts=0 means never started).
    """
    with jobs_lock:
        if job_id not in jobs:
            return
        job_id_map = jobs[job_id].get("job_id_map")
        batch_id = jobs[job_id].get("batch_id")

    if not job_id_map or file_index not in job_id_map:
        return  # Batch tracking not enabled (shouldn't happen in Phase 1+)

    doc_job_id = job_id_map[file_index]

    # Runs inside a background ThreadPoolExecutor worker (see run_queue in
    # the /upload route), which has no Flask app context of its own - needed
    # here since this is called directly, unlike _save_document_to_db which
    # pushes its own context around its DB work.
    from app import app
    with app.app_context():
        doc_job = DocumentJob.query.get(doc_job_id)
        if not doc_job:
            return

        doc_job.status = status
        doc_job.attempts = 1  # Mark as attempted even on synchronous path (not 0)

        if status == "processing":
            doc_job.started_at = datetime.utcnow()
        elif status in ("succeeded", "failed", "dead_letter"):
            doc_job.finished_at = datetime.utcnow()
            if result_ref:
                doc_job.result_ref = result_ref
            if last_error:
                doc_job.last_error = last_error

        db.session.commit()

        # Update batch rollup on terminal states
        if status in ("succeeded", "failed", "dead_letter") and batch_id:
            batch_service.update_batch_status(batch_id)


def process_file_task(job_id: str, file_index: int, file_path: str, original_filename: str, ext: str, unique_filename: str = None, split_mode: str = "none", tenant_id: str = "default", document_class: str = "production", force_document_type: str = None):
    """
    Background thread: process a single file and update the job store.

    split_mode values:
      "none"    ??? normal single-document processing.
      "ai"      ??? AI classifies each page; consecutive same-type pages are
                  grouped into separate Document rows (process_split_pdf).
      "barcode" ??? barcode/QR pages act as separator sheets; content pages
                  between them become separate Document rows
                  (process_barcode_split_pdf).
      "mrn_barcode" ??? a page carrying an MRN Barcode (Code 128) starts a new
                  Document row; pages without one are continuation pages of
                  the current one (process_mrn_barcode_split_pdf). For the
                  Out-patient Prescription/Drug List family, where QR 5 isn't
                  present on every page but the MRN Barcode reliably is.

    Cancellation is cooperative, not preemptive: process_document()/
    process_split_pdf() are blocking calls (OCR + vision model), so there's
    no safe way to kill them mid-call from another thread. Instead this
    checks jobs[job_id]["cancelled"] right before starting and right after
    the blocking call returns - a cancel requested while a file is mid-flight
    still lets that one step finish, but the result is discarded instead of
    saved (see _discard_cancelled_file), so nothing from a cancelled upload
    ever reaches the document list.
    """
    try:
        with jobs_lock:
            if jobs[job_id].get("cancelled"):
                jobs[job_id]["files"][file_index]["status"] = "cancelled"
                _check_job_done(job_id)
                return
            jobs[job_id]["files"][file_index]["status"]     = "processing"
            jobs[job_id]["files"][file_index]["started_at"] = time.time()
            jobs[job_id]["files"][file_index]["step"]       = "Starting..."
            jobs[job_id]["files"][file_index]["step_num"]   = 0

        # Sync to DocumentJob table (Phase 1, Task 3)
        _update_document_job_status(job_id, file_index, "processing")

        _init_lm_progress(lambda label=None, frac=None, pages_done=None, pages_total=None:
                          update_job_step(job_id, file_index, label, frac, pages_done, pages_total))

        # ?????? DOCX ??? render to PDF first, then process exactly like a native PDF ???
        # (page images, text extraction, viewer, export, highlighting all key off
        # the file being a real PDF on disk).
        if ext == "docx":
            update_job_step(job_id, file_index, "Converting document...", 0.02)
            from docx_service import docx_to_pdf
            pdf_unique_filename = os.path.splitext(unique_filename)[0] + ".pdf"
            pdf_path = os.path.join(UPLOAD_FOLDER, pdf_unique_filename)
            docx_to_pdf(file_path, pdf_path)
            os.remove(file_path)
            file_path       = pdf_path
            unique_filename = pdf_unique_filename
            ext             = "pdf"

        # ?????? Split-PDF paths (AI, Barcode, or MRN Barcode) ??????????????????????????????????????????????????????????????????????????????????????????
        if split_mode in ("ai", "barcode", "mrn_barcode") and ext == "pdf":
            if split_mode == "ai":
                from qwen_service import process_split_pdf
                update_job_step(job_id, file_index, "Scanning pages...", 0.05)
                split_results = process_split_pdf(file_path, UPLOAD_FOLDER)
            elif split_mode == "barcode":
                from qwen_service import process_barcode_split_pdf
                update_job_step(job_id, file_index, "Scanning for barcode separators...", 0.05)
                split_results = process_barcode_split_pdf(file_path, UPLOAD_FOLDER)
            else:
                from qwen_service import process_mrn_barcode_split_pdf
                update_job_step(job_id, file_index, "Scanning for MRN Barcode...", 0.05)
                split_results = process_mrn_barcode_split_pdf(file_path, UPLOAD_FOLDER)

            with jobs_lock:
                cancelled_now = jobs[job_id].get("cancelled")
            if cancelled_now:
                _discard_cancelled_file(job_id, file_index, file_path)
                return

            sub_docs = []
            for idx, sr in enumerate(split_results):
                pages_label = f"p{sr['_split_pages'][0]}" if len(sr["_split_pages"]) == 1 \
                              else f"p{sr['_split_pages'][0]}-{sr['_split_pages'][-1]}"
                base = original_filename.rsplit(".", 1)[0]
                sub_orig_name = f"{base} ({pages_label}).pdf"
                sub_unique    = sr.get("_sub_pdf_filename") or unique_filename

                doc_dict = _save_document_to_db(sr, sub_orig_name, sub_unique, "pdf", tenant_id=tenant_id, document_class=document_class, force_document_type=force_document_type)
                sub_docs.append(doc_dict)
                print(f"[Split] Saved record {idx+1}/{len(split_results)}: {doc_dict['document_type']} ??? id={doc_dict['id']}")

            with jobs_lock:
                jobs[job_id]["files"][file_index]["status"]      = "done"
                jobs[job_id]["files"][file_index]["sub_results"] = sub_docs
                jobs[job_id]["files"][file_index]["result"]      = sub_docs[-1] if sub_docs else None
                _check_job_done(job_id)
            return

        # ?????? Normal (single-document) path ???????????????????????????????????????????????????????????????????????????????????????????????????????????????
        result = process_document(file_path)

        with jobs_lock:
            cancelled_now = jobs[job_id].get("cancelled")
        if cancelled_now:
            _discard_cancelled_file(job_id, file_index, file_path)
            return

        doc_dict = _save_document_to_db(result, original_filename, unique_filename, ext, tenant_id=tenant_id, document_class=document_class, force_document_type=force_document_type)

        with jobs_lock:
            jobs[job_id]["files"][file_index]["status"] = "done"
            jobs[job_id]["files"][file_index]["result"] = doc_dict
            _check_job_done(job_id)

        # Sync success to DocumentJob table (Phase 1, Task 3)
        _update_document_job_status(job_id, file_index, "succeeded", result_ref=doc_dict.get("id"))

    except Exception as e:
        with jobs_lock:
            jobs[job_id]["files"][file_index]["status"] = "error"
            jobs[job_id]["files"][file_index]["error"]  = str(e)
            _check_job_done(job_id)

        # Sync failure to DocumentJob table (Phase 1, Task 3)
        _update_document_job_status(job_id, file_index, "failed", last_error=str(e))


# ?????? POST /api/documents/upload ????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

@document_bp.route("/upload", methods=["POST"])
def upload_documents():
    """
    Multi-file upload handler.
    Accepts multiple files, queues each for background processing.
    Returns job_id to track progress via SSE.
    """
    with jobs_lock:
        _sweep_expired_jobs()

    files = request.files.getlist("files")

    if not files or all(f.filename == "" for f in files):
        return jsonify({"error": "No files provided"}), 400

    if len(files) > MAX_FILES_PER_UPLOAD:
        return jsonify({"error": f"Too many files. Maximum {MAX_FILES_PER_UPLOAD} files per upload."}), 400

    # split_mode: "none" | "ai" | "barcode" | "mrn_barcode"
    # "ai"          ??? AI classifies each page and groups by document type
    # "barcode"     ??? barcode/QR pages are separator sheets; content pages become separate docs
    # "mrn_barcode" ??? a page with an MRN Barcode (Code 128) starts a new doc; pages without one continue it
    split_mode = request.form.get("split_mode", "none")
    if split_mode not in ("none", "ai", "barcode", "mrn_barcode"):
        split_mode = "none"

    tenant_id = (request.form.get("tenant_id") or "default").strip() or "default"
    document_class = (request.form.get("document_class") or "production").strip().lower() or "production"
    if document_class not in ("sample", "production"):
        document_class = "production"

    # Optional: pin the saved document to a specific type (used by the Doc Type
    # workspace when uploading a sample for the type being edited), bypassing
    # AI classification so it reliably appears under that type.
    force_document_type = (request.form.get("force_document_type") or "").strip() or None

    job_id    = uuid.uuid4().hex
    job_files = []
    saved_paths = []  # tracks files already written to disk in this request, for cleanup on a mid-loop failure

    try:
        for file in files:
            if file.filename == "" or not allowed_file(file.filename):
                continue

            # Check size before writing to disk — file.seek/tell reads the length
            # off the already-buffered upload stream, no need to save it first.
            file.seek(0, os.SEEK_END)
            file_size = file.tell()
            file.seek(0)
            if file_size > MAX_FILE_SIZE_BYTES:
                continue

            original_filename = secure_filename(file.filename)
            ext               = original_filename.rsplit(".", 1)[1].lower()
            unique_filename   = f"{uuid.uuid4().hex}.{ext}"
            file_path         = os.path.join(UPLOAD_FOLDER, unique_filename)
            file.save(file_path)
            saved_paths.append(file_path)

            # Split mode only applies to PDFs
            effective_mode = split_mode if ext == "pdf" else "none"

            job_files.append({
                "filename":        original_filename,
                "unique_filename": unique_filename,
                "file_path":       file_path,
                "ext":             ext,
                "split_mode":      effective_mode,
                "tenant_id":       tenant_id,
                "document_class":  document_class,
                "force_document_type": force_document_type,
                "status":      "pending",
                "step":        "Queued",
                "step_num":    0,
                "total_steps": 4,
                "started_at":  None,
                "result":      None,
                "sub_results": None,
                "error":       None,
            })
    except Exception as e:
        # A later file failing to save (e.g. disk full) shouldn't leave the
        # earlier files in this request orphaned on disk with no job to
        # reference them.
        for p in saved_paths:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError as _rm_err:
                print(f"[Upload] Failed to clean up orphaned file {p}: {_rm_err}")
        print(f"[Upload] Save failed mid-batch: {e}")
        return jsonify({"success": False, "error": f"Upload failed while saving files: {e}"}), 500

    if not job_files:
        return jsonify({"error": "No valid files found"}), 400

    # Create batch and jobs in the database (Phase 1, Task 3)
    # Build files list for create_batch()
    batch_files = []
    for jf in job_files:
        batch_files.append({
            "file_path": jf["file_path"],
            "page_count": None,  # Will be set during processing if it's a PDF
        })

    try:
        batch = batch_service.create_batch(
            files=batch_files,
            source="manual_upload",
            client_id=tenant_id,
            split_mode="none",  # Phase 1: no splitting
        )
        batch_id = batch.id

        # Map file_index -> DocumentJob.id for status updates during processing
        document_jobs = DocumentJob.query.filter_by(batch_id=batch_id).order_by(DocumentJob.id).all()
        job_id_map = {i: dj.id for i, dj in enumerate(document_jobs)}
    except Exception as e:
        # Failed to create batch - clean up saved files
        for p in saved_paths:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        print(f"[Upload] Failed to create batch: {e}")
        return jsonify({"success": False, "error": f"Failed to create batch: {e}"}), 500

    # Store job (in-memory dict for SSE progress tracking)
    with jobs_lock:
        jobs[job_id] = {
            "status": "processing",
            "cancelled": False,
            "files":  job_files,
            "batch_id": batch_id,  # Link to DB batch
            "job_id_map": job_id_map,  # file_index -> DocumentJob.id
        }

    # Launch background thread ??? bounded worker pool (MAX_CONCURRENT_UPLOADS)
    # instead of one file at a time, so multiple files' network-bound model
    # calls can overlap. process_file_task already re-checks jobs[...]["cancelled"]
    # itself at its own start and right after its blocking call returns (see its
    # docstring), so a file already queued in the pool when cancel is requested
    # still resolves correctly - it just returns near-instantly via that check
    # instead of never being dispatched at all.
    def run_queue():
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_UPLOADS, thread_name_prefix=f"upload-{job_id[:8]}") as executor:
            for i, f in enumerate(job_files):
                with jobs_lock:
                    if jobs[job_id].get("cancelled"):
                        # Files not yet submitted can be cancelled outright - never
                        # queue them for the (slow, blocking) processing call at all.
                        for remaining in jobs[job_id]["files"][i:]:
                            if remaining["status"] == "pending":
                                remaining["status"] = "cancelled"
                        _check_job_done(job_id)
                        break
                executor.submit(
                    process_file_task, job_id, i, f["file_path"], f["filename"], f["ext"], f["unique_filename"],
                    split_mode=f.get("split_mode", "none"), tenant_id=f.get("tenant_id", "default"),
                    document_class=f.get("document_class", "production"), force_document_type=f.get("force_document_type"),
                )
            # Context manager exit blocks here (shutdown(wait=True)) until every
            # submitted file has finished, so this background thread - and thus
            # the job's lifetime - still ends only once all files are terminal.

    threading.Thread(target=run_queue, daemon=True).start()

    return jsonify({
        "success":     True,
        "job_id":      job_id,
        "batch_id":    batch_id,  # Include batch_id for frontend link to Batches view
        "total_files": len(job_files),
        "files":       [{"filename": f["filename"], "status": "pending"} for f in job_files],
    }), 202


# ?????? GET /api/documents/progress/<job_id> ??? SSE stream ???????????????????????????????????????????????????????????????????????????

@document_bp.route("/progress/<job_id>")
def progress(job_id: str):
    """
    SSE endpoint ??? streams live progress updates for a job.
    Each event carries: status, step label, step_num, total_steps, elapsed, eta.
    """
    def generate():
        last_states = {}
        start_time = time.time()
        last_yield_at = start_time
        _MAX_DURATION = 30 * 60       # 30 min ceiling - a stuck worker thread shouldn't poll forever
        _HEARTBEAT_INTERVAL = 15      # also surfaces a dead client connection promptly on the next write

        while True:
            if time.time() - start_time > _MAX_DURATION:
                yield f"data: {json.dumps({'status': 'error', 'error': 'Progress stream timed out'})}\n\n"
                break

            with jobs_lock:
                job = jobs.get(job_id)

            if not job:
                yield f"data: {json.dumps({'error': 'Job not found'})}\n\n"
                break

            for i, f in enumerate(job["files"]):
                # Re-emit whenever step, status, or page progress changes
                key = f"{i}_{f['status']}_{f.get('step_num', 0)}_{f.get('step', '')}_{f.get('pages_done')}_{f.get('pages_total')}"
                if last_states.get(i) != key:
                    last_states[i] = key

                    elapsed = (time.time() - f["started_at"]) if f.get("started_at") else 0
                    step_num    = f.get("step_num", 0)
                    total_steps = f.get("total_steps", 4)

                    # ETA: avg time-per-step ?? remaining steps
                    if step_num > 0 and elapsed > 0:
                        eta = max(0, int((elapsed / step_num) * (total_steps - step_num)))
                    else:
                        eta = None

                    payload = {
                        "file_index":  i,
                        "filename":    f["filename"],
                        "status":      f["status"],
                        "step":        f.get("step", ""),
                        "step_num":    step_num,
                        "total_steps": total_steps,
                        "elapsed":     int(elapsed),
                        "eta":         eta,
                        "pages_done":  f.get("pages_done"),
                        "pages_total": f.get("pages_total"),
                        "result":      f.get("result"),
                        "sub_results": f.get("sub_results"),
                        "error":       f.get("error"),
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                    last_yield_at = time.time()

            if job["status"] == "done":
                yield f"data: {json.dumps({'status': 'all_done'})}\n\n"
                break

            if time.time() - last_yield_at > _HEARTBEAT_INTERVAL:
                # SSE comment line -- ignored by the client's onmessage handler,
                # but the write attempt is what lets the WSGI layer notice (and
                # raise into this generator) a client that's already gone,
                # instead of only discovering that whenever real data next changes.
                yield ": keep-alive\n\n"
                last_yield_at = time.time()

            time.sleep(0.4)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


# ?????? POST /api/documents/progress/<job_id>/cancel ??????????????????????????

@document_bp.route("/progress/<job_id>/cancel", methods=["POST"])
def cancel_job(job_id: str):
    """
    Cancels the rest of an in-progress upload. Files still waiting in line
    are cancelled immediately and never processed. A file already mid-
    processing can't be safely interrupted (see process_file_task) - it's
    left to finish that one step, then process_file_task itself discards the
    result once it notices the cancelled flag, instead of saving it.
    """
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        job["cancelled"] = True
        for f in job["files"]:
            if f["status"] == "pending":
                f["status"] = "cancelled"
                try:
                    if f.get("file_path") and os.path.exists(f["file_path"]):
                        os.remove(f["file_path"])
                except Exception:
                    pass
        _check_job_done(job_id)
    return jsonify({"ok": True})


# ?????? GET /api/documents/ ?????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

MAX_DOCUMENT_LIST_PAGE_SIZE = 200
DEFAULT_DOCUMENT_LIST_PAGE_SIZE = 1000  # generous safety net when no limit/offset is given at all


def _paginate_document_query(query):
    """Shared limit/offset handling for the document list endpoints. limit/
    offset are optional (existing callers that don't pass them still get
    everything, up to DEFAULT_DOCUMENT_LIST_PAGE_SIZE, so this doesn't change
    behavior for callers that were never paginating); explicit limits are
    capped at MAX_DOCUMENT_LIST_PAGE_SIZE so a caller can't request an
    unbounded page on purpose."""
    total = query.count()
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = int(request.args["limit"]) if "limit" in request.args else DEFAULT_DOCUMENT_LIST_PAGE_SIZE
    except (TypeError, ValueError):
        limit = DEFAULT_DOCUMENT_LIST_PAGE_SIZE
    limit = max(1, min(limit, MAX_DOCUMENT_LIST_PAGE_SIZE if "limit" in request.args else DEFAULT_DOCUMENT_LIST_PAGE_SIZE))
    rows = query.offset(offset).limit(limit).all()
    meta = {"total": total, "limit": limit, "offset": offset, "has_more": offset + len(rows) < total}
    return rows, meta


@document_bp.route("/", methods=["GET"])
def get_all_documents():
    from pdf_searchable_service import is_searchable_cached
    query = Document.query.order_by(Document.created_at.desc())
    docs, meta = _paginate_document_query(query)
    dicts = []
    for d in docs:
        dd = d.to_dict(include_raw_text=False)
        dd["searchable_cached"] = is_searchable_cached(dd, UPLOAD_FOLDER)
        dicts.append(dd)
    resp = {"documents": dicts}
    resp.update(meta)
    return jsonify(resp), 200


# ?????? GET /api/documents/batches ?????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

@document_bp.route("/batches", methods=["GET"])
def list_batches_api():
    """
    List batches for the batches view.

    Query params:
        tenant_id: Optional tenant filter (defaults to "default")

    Security note / TODO:
        This endpoint inherits the same tenant isolation gap as /api/documents
        (line 836) and /upload (line 542). The "default" tenant_id is a shared
        literal string, not a session-scoped value. Before multi-client
        production deployment, proper authentication must be added. See
        batch_service.list_batches() for full security notes.
    """
    tenant_id = (request.args.get("tenant_id") or "default").strip() or "default"
    batches = batch_service.list_batches(client_id=tenant_id, limit=100)
    return jsonify({"success": True, "batches": batches})


# ?????? GET /api/documents/batches/<batch_id> ?????????????????????????????????????????????????????????????????????????????????????????????

@document_bp.route("/batches/<int:batch_id>", methods=["GET"])
def get_batch_details_api(batch_id):
    """
    Get detailed information for a specific batch, including all jobs.

    Security note / TODO:
        Same tenant isolation gap as /batches endpoint above.
    """
    tenant_id = (request.args.get("tenant_id") or "default").strip() or "default"
    details = batch_service.get_batch_details(batch_id=batch_id, client_id=tenant_id)

    if not details:
        return jsonify({"error": "Batch not found"}), 404

    return jsonify({"success": True, **details})


# ?????? GET /api/documents/batches/<batch_id>/progress ??? SSE stream ?????????????????????

@document_bp.route("/batches/<int:batch_id>/progress")
def batch_progress(batch_id: int):
    """
    SSE endpoint ??? streams live batch progress updates.
    Sends updates whenever job status changes.
    """
    tenant_id = (request.args.get("tenant_id") or "default").strip() or "default"

    def generate():
        from document_model import DocumentJob, Batch

        # Verify batch exists and belongs to tenant
        batch = Batch.query.filter_by(id=batch_id, client_id=tenant_id).first()
        if not batch:
            yield f"data: {json.dumps({'error': 'Batch not found'})}\n\n"
            return

        last_state_hash = None
        start_time = time.time()
        _MAX_DURATION = 60 * 60  # 1 hour max
        _HEARTBEAT_INTERVAL = 15
        last_yield_at = start_time

        while True:
            if time.time() - start_time > _MAX_DURATION:
                yield f"data: {json.dumps({'status': 'timeout'})}\n\n"
                break

            # Fetch current batch state
            batch = Batch.query.get(batch_id)
            if not batch:
                yield f"data: {json.dumps({'error': 'Batch deleted'})}\n\n"
                break

            # Fetch all jobs with eager-loading of source_file relationship
            # to avoid N+1 queries when accessing job.source_file.page_count in to_dict()
            jobs = DocumentJob.query.filter_by(batch_id=batch_id)\
                                    .options(db.joinedload(DocumentJob.source_file))\
                                    .order_by(DocumentJob.created_at.asc())\
                                    .all()

            # Create state hash to detect changes
            state_hash = f"{batch.status}_{batch.completed_count}_{batch.failed_count}_" + \
                        "_".join([f"{j.id}:{j.status}:{j.attempts}" for j in jobs])

            if state_hash != last_state_hash:
                last_state_hash = state_hash

                payload = {
                    "batch": batch.to_dict(),
                    "jobs": [j.to_dict() for j in jobs]
                }
                yield f"data: {json.dumps(payload)}\n\n"
                last_yield_at = time.time()

            # Check if batch is complete
            if batch.status in ("completed", "completed_with_errors"):
                yield f"data: {json.dumps({'status': 'batch_complete'})}\n\n"
                break

            # Heartbeat
            if time.time() - last_yield_at > _HEARTBEAT_INTERVAL:
                yield ": keep-alive\n\n"
                last_yield_at = time.time()

            time.sleep(1)  # Poll every second

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


# ?????? POST /api/documents/batches/<batch_id>/retry ????????????????????????????????????????????????????????

@document_bp.route("/batches/<int:batch_id>/retry", methods=["POST"])
def retry_batch_failed_jobs(batch_id):
    """
    Retry all failed and dead-letter jobs in a batch.

    Resets jobs to queued status and transitions batch back to processing.

    Security note / TODO:
        Same tenant isolation gap as other batch endpoints.
    """
    tenant_id = (request.args.get("tenant_id") or "default").strip() or "default"
    result = batch_service.retry_failed_jobs(batch_id=batch_id, client_id=tenant_id)

    if not result:
        return jsonify({"error": "Batch not found"}), 404

    return jsonify({"success": True, **result})


# ?????? POST /api/documents/batches/<batch_id>/cancel ????????????????????????????????????????

@document_bp.route("/batches/<int:batch_id>/cancel", methods=["POST"])
def cancel_batch_api(batch_id):
    """
    Cancel a batch by marking all queued jobs as cancelled.

    Jobs already claimed/processing finish naturally.

    Security note / TODO:
        Same tenant isolation gap as other batch endpoints.
    """
    tenant_id = (request.args.get("tenant_id") or "default").strip() or "default"
    result = batch_service.cancel_batch(batch_id=batch_id, client_id=tenant_id)

    if not result:
        return jsonify({"error": "Batch not found"}), 404

    return jsonify({"success": True, **result})


# ?????? PATCH /api/documents/batches/<batch_id>/label ?????????????????????????????????????????????

@document_bp.route("/batches/<int:batch_id>/label", methods=["PATCH"])
def update_batch_label_api(batch_id):
    """
    Update the label for a batch.

    Request body:
        {"label": "My Batch Name"}  # or null to clear

    Security note / TODO:
        Same tenant isolation gap as other batch endpoints.
    """
    tenant_id = (request.args.get("tenant_id") or "default").strip() or "default"
    data = request.get_json() or {}
    label = data.get("label")

    result = batch_service.update_batch_label(batch_id, label, client_id=tenant_id)

    if not result:
        return jsonify({"success": False, "error": "Batch not found"}), 404

    return jsonify({"success": True, **result})


# ?????? DELETE /api/documents/batches/<batch_id> ?????????????????????????????????????????????

@document_bp.route("/batches/<int:batch_id>", methods=["DELETE"])
def delete_batch_api(batch_id):
    """
    Delete a batch and all associated SourceFile/DocumentJob records.

    Document records (result_ref) remain in the library - only the batch
    metadata and job tracking is removed.

    Blocks deletion if batch status is 'processing' - must cancel first
    to avoid worker crashes.

    Security note / TODO:
        Same tenant isolation gap as other batch endpoints.
    """
    tenant_id = (request.args.get("tenant_id") or "default").strip() or "default"
    result = batch_service.delete_batch(batch_id, client_id=tenant_id)

    if not result["success"]:
        status_code = 400 if "processing" in result.get("error", "") else 404
        return jsonify(result), status_code

    return jsonify(result)


@document_bp.route("/batches/clear-all", methods=["POST"])
def clear_all_batches_api():
    """
    Delete all batches that are NOT currently processing.

    Processing batches are skipped to avoid worker crashes.
    If the table is completely empty after deletion, resets auto-increment to 1.

    Returns:
        JSON with deleted_count, skipped_count, auto_increment_reset flag
    """
    tenant_id = (request.args.get("tenant_id") or "default").strip() or "default"
    result = batch_service.clear_all_batches(client_id=tenant_id)
    return jsonify(result)


# ?????? GET /api/documents/batches/<batch_id>/results ?????????????????????????????????????????????

@document_bp.route("/batches/<int:batch_id>/results", methods=["GET"])
def get_batch_results_api(batch_id):
    """
    Get all documents created by jobs in this batch.

    Returns documents where result_ref matches job results from this batch.

    Security note / TODO:
        Same tenant isolation gap as other batch endpoints.
    """
    tenant_id = (request.args.get("tenant_id") or "default").strip() or "default"
    result = batch_service.get_batch_results(batch_id=batch_id, client_id=tenant_id)

    if not result:
        return jsonify({"error": "Batch not found"}), 404

    return jsonify({"success": True, **result})


# ?????? GET /api/documents/batches/all-results ????????????????????????????????????????????????????????????????????????

@document_bp.route("/batches/all-results", methods=["GET"])
def get_all_batch_results_api():
    """
    Get all documents produced by ALL batches (across all batches for this tenant).

    Returns a flat list of documents ordered by creation date, for the
    "View all processed documents" link on the Batches list page.

    Security note / TODO:
        Same tenant isolation gap as other batch endpoints.
    """
    tenant_id = (request.args.get("tenant_id") or "default").strip() or "default"
    result = batch_service.get_all_batch_documents(client_id=tenant_id)

    return jsonify({"success": True, **result})


# ?????? POST /api/documents/batches/<batch_id>/files ????????????????????????????????????????????

@document_bp.route("/batches/<int:batch_id>/files", methods=["POST"])
def add_files_to_batch_api(batch_id):
    """
    Add files to an existing batch.

    Accepts multipart/form-data with file uploads.
    Creates new jobs for each file and adds to batch.

    Security note / TODO:
        Same tenant isolation gap as other batch endpoints.
    """
    tenant_id = (request.form.get("tenant_id") or "default").strip() or "default"

    # Handle file uploads
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files provided"}), 400

    # Save uploaded files and collect file info
    uploaded_files = []
    for file in files:
        if not file or not file.filename:
            continue

        # Save file to uploads directory
        upload_folder = os.path.join(os.getcwd(), "uploads")
        os.makedirs(upload_folder, exist_ok=True)

        # Generate unique filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_filename = secure_filename(file.filename)
        unique_filename = f"{timestamp}_{safe_filename}"
        file_path = os.path.join(upload_folder, unique_filename)

        file.save(file_path)

        # Get page count if PDF
        page_count = None
        if safe_filename.lower().endswith('.pdf'):
            try:
                import pdf_service
                page_count = len(pdf_service.pdf_to_images(file_path))
            except:
                pass

        uploaded_files.append({
            "file_path": file_path,
            "page_count": page_count
        })

    # Add files to batch
    result = batch_service.add_files_to_batch(
        batch_id=batch_id,
        files=uploaded_files,
        client_id=tenant_id
    )

    if not result:
        # Clean up uploaded files if batch not found
        for file_info in uploaded_files:
            try:
                os.remove(file_info["file_path"])
            except:
                pass
        return jsonify({"error": "Batch not found"}), 404

    return jsonify({"success": True, **result})


# ?????? POST /api/documents/batches/<batch_id>/start ????????????????????????????????????????????

@document_bp.route("/batches/<int:batch_id>/start", methods=["POST"])
def start_batch_processing_api(batch_id):
    """
    Start processing an open batch.

    Transitions all held jobs to queued, making them visible to worker claim query.
    Changes batch status from 'open' to 'processing'.

    Security note / TODO:
        Same tenant isolation gap as other batch endpoints.
    """
    tenant_id = (request.args.get("tenant_id") or "default").strip() or "default"
    result = batch_service.start_batch_processing(batch_id=batch_id, client_id=tenant_id)

    if not result:
        return jsonify({"error": "Batch not found"}), 404

    return jsonify({"success": True, **result})


# ?????? GET /api/documents/<id> ?????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

@document_bp.route("/<int:doc_id>", methods=["GET"])
def get_document(doc_id):
    from pdf_searchable_service import is_searchable_cached
    doc = Document.query.get_or_404(doc_id)
    # ad_hoc_fields does its own DocumentType/DocumentField query, so it's
    # computed only here (single-document fetch) rather than inside
    # to_dict() itself, which would add an extra query per row on the bulk
    # document-list endpoint for no benefit there.
    _payload = doc.to_dict()
    _payload["ad_hoc_fields"] = doc.get_ad_hoc_fields()
    _payload["searchable_cached"] = is_searchable_cached(_payload, UPLOAD_FOLDER)
    return jsonify({"document": _payload}), 200


# ?????? GET /api/documents/<id>/blocks ?????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

@document_bp.route("/<int:doc_id>/blocks", methods=["GET"])
def get_text_blocks(doc_id):
    """Return bounding-box blocks for the field highlight overlay.

    Computed lazily on first request and cached on the Document row from
    then on (doc.text_blocks stays None until the first successful
    computation). Failures/timeouts don't get cached, so retries are allowed.

    Uses a 10-second server-side timeout on the LM Studio call to prevent
    hung requests when the vision model is slow/unresponsive.
    """
    doc = Document.query.get_or_404(doc_id)

    # Check if already cached
    if doc.text_blocks is not None:
        cached_blocks = doc.get_text_blocks()
        print(f"[Blocks] Doc {doc_id}: returning {len(cached_blocks)} cached blocks (no LM Studio call)")
        return jsonify({"blocks": cached_blocks}), 200

    # Not cached - compute lazily
    if not doc.unique_upload_filename:
        print(f"[Blocks] Doc {doc_id}: no file stored, returning empty")
        return jsonify({"blocks": []}), 200

    file_path = os.path.join(UPLOAD_FOLDER, doc.unique_upload_filename)
    if not os.path.isfile(file_path):
        print(f"[Blocks] Doc {doc_id}: file not found on disk, returning empty")
        return jsonify({"blocks": []}), 200

    print(f"[Blocks] Doc {doc_id}: text_blocks not cached, computing (OCR, Vision fallback)...")
    from qwen_service import compute_text_blocks_lazy
    try:
        blocks = compute_text_blocks_lazy(file_path)
        if blocks:
            # Success - cache the result
            doc.set_text_blocks(blocks)
            db.session.commit()
            print(f"[Blocks] Doc {doc_id}: ✓ computed {len(blocks)} blocks, cached for future requests")
            return jsonify({"blocks": blocks}), 200
        else:
            # Computation returned empty (ODL found nothing, or genuine empty document)
            # Don't cache - allow retry on next request
            print(f"[Blocks] Doc {doc_id}: computation returned 0 blocks (not cached, retry allowed)")
            return jsonify({"blocks": []}), 200
    except Exception as e:
        # Timeout or other failure - don't cache, allow retry
        print(f"[Blocks] Doc {doc_id}: ✗ computation failed ({type(e).__name__}: {e}) - not cached, retry allowed")
        return jsonify({"blocks": []}), 200


# ?????? GET /api/documents/<id>/original ???????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

@document_bp.route("/<int:doc_id>/original", methods=["GET"])
def get_original_file(doc_id):
    """Serve the original uploaded file for accurate PDF.js text-layer extraction."""
    from flask import send_file
    doc = Document.query.get_or_404(doc_id)
    if not doc.unique_upload_filename:
        return jsonify({"error": "Original file not stored"}), 404
    file_path = os.path.join(UPLOAD_FOLDER, doc.unique_upload_filename)
    if not os.path.exists(file_path):
        return jsonify({"error": "Original file not found on disk"}), 404
    ext = doc.unique_upload_filename.rsplit(".", 1)[-1].lower()
    mime_map = {
        "pdf": "application/pdf",
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "webp": "image/webp",
    }
    # Without an explicit download_name, Flask suggests the file's actual
    # on-disk name -- a raw UUID (documents are stored as
    # {uuid4().hex}.{ext}, see the upload route) -- to anything that saves
    # this response directly (a browser's native PDF-viewer download button,
    # not just this app's own export buttons).
    mrn_basename = _mrn_export_basename(doc)
    download_name = f"{mrn_basename}.{ext}" if mrn_basename else (doc.filename or doc.unique_upload_filename)
    # This route is used two ways: the app's own PDF.js viewer loads it
    # inline (must stay as_attachment=False, or the built-in viewer breaks),
    # while the document card's "Export > original" dropdown option wants an
    # actual download -- ?download=1 selects that, same pattern as /export
    # and /searchable below. Without as_attachment=True, send_file() defaults
    # to inline regardless of download_name, so the "export" click was just
    # opening the PDF in-browser instead of downloading it.
    as_attachment = request.args.get("download", "0") == "1"
    # conditional=True enables ETag/If-None-Match + Last-Modified/If-Modified-Since
    # so the browser serves from its cache on repeated opens (304 Not Modified).
    resp = send_file(
        file_path,
        mimetype=mime_map.get(ext, "application/octet-stream"),
        download_name=download_name,
        as_attachment=as_attachment,
        conditional=True,
    )
    # Original files are immutable per document id — safe to cache for a day.
    if not as_attachment:
        resp.cache_control.max_age = 86400
        resp.cache_control.public = False
        resp.cache_control.private = True
    return resp


# ?????? GET /api/documents/<id>/thumbnail ????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

@document_bp.route("/<int:doc_id>/thumbnail", methods=["GET"])
def get_thumbnail(doc_id):
    """Return a small JPEG thumbnail (page 1 for PDFs, resized for images)."""
    import io
    from flask import send_file
    import fitz  # PyMuPDF
    from PIL import Image

    doc = Document.query.get_or_404(doc_id)
    if not doc.unique_upload_filename:
        return jsonify({"error": "File not stored"}), 404
    file_path = os.path.join(UPLOAD_FOLDER, doc.unique_upload_filename)
    if not os.path.exists(file_path):
        return jsonify({"error": "File not found"}), 404

    ext = doc.unique_upload_filename.rsplit(".", 1)[-1].lower()
    thumb_size = (320, 440)

    try:
        if ext == "pdf":
            pdf = fitz.open(file_path)
            page = pdf[0]
            mat = fitz.Matrix(1.5, 1.5)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            pdf.close()
        else:
            img = Image.open(file_path).convert("RGB")

        img.thumbnail(thumb_size, Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        buf.seek(0)
        return send_file(buf, mimetype="image/jpeg")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ?????? DELETE /api/documents/<id> ????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

@document_bp.route("/<int:doc_id>", methods=["DELETE"])
def delete_document(doc_id):
    doc = Document.query.get_or_404(doc_id)
    file_path = (
        os.path.join(UPLOAD_FOLDER, doc.unique_upload_filename)
        if doc.unique_upload_filename else None
    )

    db.session.delete(doc)
    db.session.commit()

    if file_path:
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except OSError as e:
            print(f"[Delete] Failed to remove file {file_path}: {e}")

    return jsonify({"success": True, "message": f"Document {doc_id} deleted"}), 200


@document_bp.route("/erase", methods=["POST"])
def erase_documents():
    data = request.get_json(force=True) or {}
    doc_id = data.get("doc_id")
    tenant_id = data.get("tenant_id")
    if doc_id is None and not tenant_id:
        return jsonify({"error": "doc_id or tenant_id is required"}), 400

    try:
        doc_id = int(doc_id) if doc_id is not None else None
    except (TypeError, ValueError):
        return jsonify({"error": "doc_id must be an integer"}), 400

    from app import app
    from retention_service import purge_documents

    result = purge_documents(
        app,
        doc_id=doc_id,
        tenant_id=(tenant_id or None),
        reason=(data.get("reason") or "right_to_erasure").strip() or "right_to_erasure",
    )
    return jsonify({"success": True, **result}), 200


# ?????? PATCH /api/documents/<id> ???????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

@document_bp.route("/<int:doc_id>", methods=["PATCH"])
def update_document(doc_id):
    doc  = Document.query.get_or_404(doc_id)
    data = request.get_json() or {}
    if "fields" in data:
        if not isinstance(data["fields"], dict):
            return jsonify({"success": False, "error": "'fields' must be an object"}), 400
        existing = doc.get_fields() or {}
        existing.update(data["fields"])
        doc.set_fields(existing)
    if "document_type" in data:
        raw_input = (data["document_type"] or "").strip()
        # Resolve to the canonical DB display name (handles "tax_invoice" ??? "Tax Invoice")
        def _norm(s):
            return s.lower().replace("_", " ").replace("-", " ").strip()
        all_active = DocumentType.query.filter_by(is_active=True).all()
        matched = next((t for t in all_active if _norm(t.name) == _norm(raw_input)), None)
        new_type = matched.name if matched else raw_input
        if new_type:
            type_changed = new_type != doc.document_type
            if type_changed:
                old_type = doc.document_type
                doc.document_type = new_type
                # Trigger learning from the correction - only meaningful when
                # the type actually changed, unlike the re-extraction below.
                try:
                    from qwen_service import learn_type_correction
                    learn_type_correction(old_type, new_type, list(doc.get_fields().keys()))
                except Exception as e:
                    print(f"[PATCH] Learning update failed: {e}")
            # Re-extract fields for this type, even when re-confirming the
            # SAME type - e.g. after fixing that type's field list, this is
            # how the user retries extraction without a pointless rename
            # away-and-back round trip.
            if doc.raw_text:
                try:
                    from qwen_service import extract_fields
                    new_fields = extract_fields(doc.raw_text, new_type)
                    doc.set_fields(new_fields)
                    print(f"[PATCH] Re-extracted {len(new_fields)} fields for type '{new_type}'"
                          + ("" if type_changed else " (re-confirmed, same type)"))
                except Exception as e:
                    print(f"[PATCH] Field re-extraction failed: {e}")
    db.session.commit()
    return jsonify({"success": True, "document": doc.to_dict()}), 200


# ?????? GET /api/documents/type/<doc_type> ?????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

@document_bp.route("/type/<string:doc_type>", methods=["GET"])
def get_by_type(doc_type):
    query = Document.query.filter_by(document_type=doc_type).order_by(Document.created_at.desc())
    docs, meta = _paginate_document_query(query)
    resp = {"documents": [d.to_dict(include_raw_text=False) for d in docs], "count": len(docs)}
    resp.update(meta)
    return jsonify(resp), 200


def _best_field_offer_value(answer: str, source_quote: str, model_value: str = "") -> str | None:
    """
    Pick the cleanest standalone value to store in a field from a Q&A result.

    Priority:
      a) model_value   — the model already distilled just the value (preferred)
      b) short answer  — strip common lead-in ("The X is …") and use if ≤ 120 chars
      c) answer in quote — if the stripped answer appears verbatim in source_quote
      d) short clean quote — source_quote ≤ 80 chars with no sentence lead-in

    Returns None if no clean value can be determined (suppresses the save offer).
    """
    import re as _re2

    _LEAD_IN = _re2.compile(
        r'^(?:the\s+\w+(?:\s+\w+){0,3}\s+(?:is|are|was|were)\s*[:\-]?\s*'
        r'|its\s+\w+(?:\s+\w+){0,2}\s+(?:is|are|was|were)\s*'
        r'|(?:it|this)\s+is\s*)',
        _re2.IGNORECASE,
    )
    _SENTENCE_MID = _re2.compile(r'\b(?:is|are|was|were|has|have|had|will|would|can|could)\b', _re2.IGNORECASE)

    def _strip_lead(s):
        s = _LEAD_IN.sub('', s).strip().strip('.,;:')
        return s

    # a) Model-returned clean value
    mv = (model_value or "").strip().strip('.,;: ')
    if mv and len(mv) <= 200:
        return mv

    ans = (answer or "").strip()
    sq  = (source_quote or "").strip()

    # b) Short answer — strip lead-in
    if ans and len(ans) <= 120:
        stripped = _strip_lead(ans)
        if stripped and len(stripped) <= 120:
            return stripped

    # c) Stripped answer appears verbatim inside source_quote
    if ans:
        stripped = _strip_lead(ans)
        if stripped and stripped in sq:
            return stripped

    # d) Short, sentence-free source_quote
    if sq and len(sq) <= 80 and not _SENTENCE_MID.search(sq):
        return sq.strip('.,;: ')

    return None


# ?????? POST /api/documents/<id>/ask ???????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

@document_bp.route("/<int:doc_id>/ask", methods=["POST"])
def ask_document(doc_id):
    doc = Document.query.get_or_404(doc_id)
    data = request.get_json() or {}
    question = (data.get("question") or "").strip()
    history  = data.get("history") or []
    client_text = (data.get("client_text") or "").strip()[:20000]
    if not question:
        return jsonify({"error": "No question provided"}), 400
    if not doc.raw_text:
        return jsonify({"error": "No text available for this document"}), 400

    # Social phrase guard — must run before intent classifier to avoid misclassification
    _SOCIAL_TOP = {
        "thank you","thanks","tq","ty","thx","thank u",
        "ok","okay","ok noted","noted","alright","got it","sure","cool","great","nice","good",
        "done","yes","no","nope","bye","goodbye","see you","cheers",
        "terima kasih","tq banyak","terima kasih banyak","sama sama","ok terima kasih",
        "oklah","boleh","baik","ok baik",
    }
    if question.lower() in _SOCIAL_TOP and not (data.get("guided_context") or {}).get("step"):
        _replies = {
            "thank you": "You're welcome! Let me know if there's anything else I can help with.",
            "thanks": "You're welcome! Anything else?",
            "tq": "You're welcome!",
            "terima kasih": "Sama-sama! Ada lagi yang boleh saya bantu?",
            "terima kasih banyak": "Sama-sama! Jangan segan untuk bertanya lagi.",
            "sama sama": "Salam! Ada lagi yang perlu dibantu?",
        }
        _reply = _replies.get(question.lower(), "You're welcome! Feel free to ask if you need anything else.")
        return jsonify({"type": "answer", "answer": _reply, "source_quote": "", "page_number": None})


    from qwen_service import detect_chat_intent, extract_fields_on_demand, answer_question, find_field_value_in_text, find_field_value_in_image, is_plausible_field_value, is_reply_to_guided_prompt, extract_single_number

    from qwen_service import generate_field_extraction_rule
    import re as _re, json as _json

    def _lookup_field_id(doc_type_name, field_name):
        if not doc_type_name or not field_name:
            return None
        from document_model import DocumentType, DocumentField as _DF
        dt = DocumentType.query.filter_by(name=doc_type_name).first()
        if not dt:
            return None
        df_obj = _DF.query.filter_by(document_type_id=dt.id, field_name=field_name).first()
        return df_obj.id if df_obj else None

    def _generate_guided_rule(fn, fl, val, hint=""):
        try:
            return generate_field_extraction_rule(fn, fl, val, doc.document_type or "", doc.raw_text or "", hint)
        except Exception:
            return f"For {fn}: extract the value \"{val}\"."

    def _is_topic_change(q, asked_for=""):
        """Guard for every guided multi-turn step that otherwise treats the
        user's next message as a literal answer (a field value, an alias, a
        y/n confirmation): without this, typing a new question or command
        while mid-flow (e.g. "can you rescan again?" right after being asked
        to type a field value) gets silently saved as that value instead of
        being handled as what it actually is.

        Cheap keyword pass first (no network call, catches the obvious cases
        instantly); anything it doesn't recognize falls through to an AI
        judgment call when `asked_for` is given (a short description of what
        was requested, e.g. "the value for the field \"Bill To\"") so phrasing
        the keyword list doesn't cover (e.g. "try to rescan") still gets caught.
        Every guided step this feeds into ends on an explicit user confirmation
        before writing anything, so a wrong guess here is a UX annoyance
        (an odd confirmation card shown, or a real answer briefly bounced to
        QA) rather than a data-safety issue."""
        _ql = q.strip().lower()
        _QW = ('what ', 'who ', 'how ', 'when ', 'where ', 'which ', 'does ', 'can ',
               'could ', 'would ', 'will ', 'should ', 'why ',
               'apa ', 'siapa ', 'berapa ', 'bila ', 'bagaimana ', 'adakah ', 'boleh ')
        if any(_ql.startswith(w) for w in _QW): return True
        if _ql.endswith('?'): return True
        _ADD = ('add ', 'tambah ', 'include ', 'create field', 'new field', 'baru field')
        if any(w in _ql for w in _ADD): return True
        _COMPLAINT = ('is wrong', 'is incorrect', 'is not right', 'is missing', 'looks wrong',
                      'salah', 'tidak betul', 'tak betul', 'not correct', 'is invalid')
        if any(c in _ql for c in _COMPLAINT): return True
        if asked_for:
            try:
                return not is_reply_to_guided_prompt(q, asked_for)
            except Exception:
                return False
        return False

    def _infer_label_for_value(raw_text, val):
        """Given a confirmed field value, find the label it's printed next to in
        the document text (e.g. "Invoice No" for "00000135"), so it can be saved
        as a Find-by label — letting the fast deterministic pass catch it on
        future documents without needing an AI call at all."""
        if not raw_text or not val:
            return None
        search_text = _re.sub(r'\s*\|\s*', '  ', raw_text)
        pat = _re.compile(
            r'([A-Za-z][A-Za-z0-9 /\-\.#]{2,30}?)\s*[:#]\s*' + _re.escape(str(val)),
            _re.IGNORECASE,
        )
        m = pat.search(search_text)
        if m:
            label = m.group(1).strip()
            if 2 <= len(label) <= 40:
                return label
        return None

    def _infer_label_via_qa(raw_text, val, field_label):
        """Fallback for _infer_label_for_value when there's no literal
        "<label>: <value>" text to find a match in - e.g. a letterhead value
        that's never introduced by any label at all, only recognizable from
        context. Asks the Q&A engine to NAME the role/heading the value plays
        instead of searching for adjacent text. Deliberately conservative:
        a wrong regex match is at least real text from the document, but a
        wrong LLM label guess is pure hallucination risk, so this rejects
        anything that isn't a short, plain label - no sentences, no "none"/
        empty answers, and never just echoing the field's own name back."""
        if not raw_text or not val:
            print(f"[LabelInfer] skipped - raw_text={bool(raw_text)}, val={val!r}")
            return None
        try:
            q = (f'In this document, what field or heading name would best describe where the '
                 f'value "{val}" appears (e.g. a heading it is printed under, or the role it '
                 f'plays, like "Issuing Ministry" or "Prepared By")? Reply with ONLY a short '
                 f'label (2-5 words), or reply "none" if there truly is no clear label for it.')
            result = answer_question(raw_text, q, history=[])
            label = (result.get("answer") or "").strip().strip('"\'.')
            print(f"[LabelInfer] Q&A raw answer for val={val!r}: {label!r}")
        except Exception as e:
            print(f"[LabelInfer] answer_question call failed: {e}")
            return None
        if not label or label.lower() in ("none", "n/a", "unclear", "unknown", "no label", "not applicable"):
            print(f"[LabelInfer] rejected - empty/none-ish answer: {label!r}")
            return None
        if len(label) > 40 or len(label.split()) > 6:
            print(f"[LabelInfer] rejected - too long/sentence-like ({len(label)} chars, {len(label.split())} words): {label!r}")
            return None
        if label.lower() == (field_label or "").lower():
            print(f"[LabelInfer] rejected - same as field's own label: {label!r}")
            return None
        print(f"[LabelInfer] accepted label: {label!r}")
        return label

    def _is_plausible_value(field_type, v, field_name="", field_label=""):
        return is_plausible_field_value(field_type, v, field_name=field_name, field_label=field_label)

    def _get_field_type(fn):
        if not doc.document_type:
            return None
        dt_obj = DocumentType.query.filter_by(name=doc.document_type).first()
        if not dt_obj:
            return None
        df_obj = DocumentField.query.filter_by(document_type_id=dt_obj.id, field_name=fn).first()
        return df_obj.field_type if df_obj else None

    def _field_label_for(fn):
        """Best-effort display label for a field name: the registered
        DocumentField's own label if there is one, else a title-cased fallback
        (same convention used everywhere else in this file for ad-hoc fields)."""
        if doc.document_type:
            dt_obj = DocumentType.query.filter_by(name=doc.document_type).first()
            if dt_obj:
                df_obj = DocumentField.query.filter_by(document_type_id=dt_obj.id, field_name=fn).first()
                if df_obj:
                    return df_obj.field_label
        return fn.replace("_", " ").title()

    def _apply_field_type_change(fn, fl, new_type):
        """Apply "this field should only hold <new_type>" right away - no confirm
        step, the user stated it outright. Two things change:
          1. the saved DocumentField.field_type, so future extractions of this
             document type ask for / validate that type (skipped when the field
             isn't in the type's schema yet - there's no row to update);
          2. this document's current value, for "number" only: "30y" -> "30".
             Left alone when there isn't exactly one number to keep, since
             guessing digits would silently corrupt the value.
        Returns the field_type_applied response the frontend refreshes the
        fields panel from."""
        kind = {"number": "a numbers-only", "date": "a date", "currency": "a currency", "text": "a plain text"}[new_type]
        dt_obj = DocumentType.query.filter_by(name=doc.document_type).first() if doc.document_type else None
        df_obj = (DocumentField.query.filter_by(document_type_id=dt_obj.id, field_name=fn).first()
                  if dt_obj else None)
        if df_obj and df_obj.field_type == "table":
            return jsonify({"type": "answer", "answer": f"\"{fl}\" is a table field, so I can't change it to {new_type}.",
                             "source_quote": "", "page_number": None})

        schema_changed = bool(df_obj) and df_obj.field_type != new_type
        fields = doc.get_fields()
        before = fields.get(fn)
        after = before
        value_changed = value_unconvertible = False
        if new_type == "number" and isinstance(before, (str, int, float)) and str(before).strip():
            cleaned = extract_single_number(before)
            if cleaned is None:
                value_unconvertible = True
            elif cleaned != str(before).strip():
                after, value_changed = cleaned, True

        try:
            if schema_changed:
                df_obj.field_type = new_type
            if value_changed:
                fields[fn] = after
                doc.set_fields(fields)
                # An edit made on the user's say-so, same as an explicit
                # correction (see _apply_rule / patch_document_field): clears
                # the "not detected" badge and any stale "which value?" pick.
                sources = doc.get_field_sources()
                sources[fn] = "corrected"
                doc.field_sources = _json.dumps(sources)
                ambiguities = doc.get_field_ambiguities()
                if fn in ambiguities:
                    del ambiguities[fn]
                    doc.set_field_ambiguities(ambiguities)
            if schema_changed or value_changed:
                db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"[ASK] set_field_type failed (doc_id={doc.id}, field={fn}, type={new_type}): {e}")
            return jsonify({"type": "answer", "answer": f"Sorry, I couldn't change \"{fl}\" to {new_type}. Please try again.",
                             "source_quote": "", "page_number": None})

        notes = []
        if value_changed:
            notes.append(f"On this document I changed its value from \"{before}\" to \"{after}\".")
        elif value_unconvertible:
            notes.append(f"Its current value \"{before}\" doesn't have a single number I could keep, so I left it as it is - you can edit it by hand.")
        if df_obj:
            notes.append(f"Future {doc.document_type} documents will extract it that way too." if schema_changed
                         else f"The {doc.document_type} document type was already set up that way.")
        elif doc.document_type:
            notes.append(f"It isn't saved in the {doc.document_type} document type yet, so this only applies to this document.")
        else:
            notes.append("This only applies to this document.")
        return jsonify({
            "type": "field_type_applied",
            "message": f"Done! \"{fl}\" is now {kind} field. " + " ".join(notes),
            "field_name": fn, "field_label": fl, "field_type": new_type,
            "fields": fields, "field_sources": doc.get_field_sources(),
            "field_ambiguities": doc.get_field_ambiguities(),
        })

    def _find_duplicate_value_field(fn, val):
        """Detect whether another field on this document already holds the
        exact same value as `val` ??? a strong signal that `fn` is a redundant
        field invented under a different name for the same concept (e.g.
        "Invoice Ref" / "Invoice Reference" / "Invoice References" all ending
        up holding the same invoice number, because the source document
        prints it under several captions and each got picked up separately).
        Name similarity alone is unreliable for this ("Invoice Number" vs
        "Invoice Reference" score low on string similarity despite meaning
        the same thing here), so this keys off value equality instead - a
        non-trivial value repeated verbatim under a different field is a much
        stronger signal than the field names looking alike. Trivially short
        values are skipped since those collide by coincidence far too often.
        Returns (existing_field_name, existing_field_label) or (None, None)."""
        val_norm = str(val or "").strip().lower()
        if len(val_norm) < 4:
            return None, None
        try:
            existing = doc.get_fields()
        except Exception:
            return None, None
        for k, v in existing.items():
            if k == fn or not v:
                continue
            if str(v).strip().lower() == val_norm:
                return k, _field_label_for(k)
        return None, None

    _QUESTION_STOPWORDS = {
        "what", "whats", "who", "when", "where", "which", "does", "do", "is", "are",
        "the", "a", "an", "of", "this", "document", "for", "in", "on", "value",
        "apa", "itu", "siapa", "bila", "di", "mana", "adakah", "nilai", "yang",
    }

    # A question can have a clean, short source_quote (the usual trigger for
    # offering to save it as a field) while still being a request to
    # TRANSFORM how a value is presented, not to find/confirm one - "translate
    # this", "summarize it", "explain that". Offering to save the output would
    # silently replace the field's real value with a translation/summary that
    # often isn't even faithful to the source (e.g. an English gloss
    # overwriting the original Chinese), for a request that was never a
    # proposed correction in the first place.
    _TRANSFORM_KEYWORDS = (
        "translate", "translation", "terjemah",
        "summarize", "summarise", "summary", "ringkaskan", "ringkasan",
        "explain", "explanation", "terangkan", "jelaskan",
        "rephrase", "paraphrase", "reword", "in english", "in malay", "in chinese",
    )

    def _is_transformation_request(question_text):
        ql = (question_text or "").lower()
        return any(kw in ql for kw in _TRANSFORM_KEYWORDS)

    def _match_question_to_field(question_text, doc_fields):
        """Does this free-form question look like it's asking for the value of
        a specific existing field (e.g. "what is the subject?" -> the "subject"
        field)? Used to offer saving the AI's sourced answer straight into that
        field - the only realistic way to correct a field for scripts (e.g.
        Jawi) a user can't type on a normal keyboard. Deliberately conservative
        (exact word-set match after stripping question words), since a false
        match would surface an unwanted "save this?" prompt on an unrelated
        question. Returns (field_name, field_label) or (None, None)."""
        q_words = set(_re.sub(r'[^a-z0-9 ]', ' ', question_text.lower()).split()) - _QUESTION_STOPWORDS
        if not q_words:
            return None, None
        for fn in doc_fields.keys():
            fl = _field_label_for(fn)
            for candidate in (fn.replace("_", " "), fl):
                c_words = set(_re.sub(r'[^a-z0-9 ]', ' ', candidate.lower()).split()) - _QUESTION_STOPWORDS
                if c_words and c_words == q_words:
                    return fn, fl
        return None, None

    def _answer_with_field_offer(question_text):
        """Shared by every "user asked something unrelated, just answer it as
        a fresh question" escape hatch - both the plain default fallback and
        the topic-change guards inside other guided steps (await_value,
        await_alias_text, await_hint_keyword, etc.) - so the field-save offer
        below works consistently whether the question arrives cold or
        interrupts some other in-progress flow."""
        result = answer_question(doc.raw_text, question_text, history=history)
        result["type"] = "answer"
        _off_quote = (result.get("source_quote") or "").strip()
        if not _is_transformation_request(question_text):
            try:
                _off_fields = doc.get_fields()
            except Exception:
                _off_fields = {}
            _off_fn, _off_fl = _match_question_to_field(question_text, _off_fields)
            if _off_fn:
                _offer_val = _best_field_offer_value(
                    result.get("answer", ""),
                    _off_quote,
                    result.get("value", ""),
                )
                if _offer_val:
                    result["field_offer"] = {"field_name": _off_fn, "field_label": _off_fl, "value": _offer_val}
        return result

    def _add_alias_to_field(fn, alias):
        """De-duplicated append of `alias` to a field's Find-by labels. Returns
        the DocumentField if the alias was added (or already present), else None
        if the field/document type couldn't be resolved."""
        if not fn or not alias or not doc.document_type:
            return None
        dt_obj = DocumentType.query.filter_by(name=doc.document_type).first()
        if not dt_obj:
            return None
        df_obj = DocumentField.query.filter_by(document_type_id=dt_obj.id, field_name=fn).first()
        if not df_obj:
            return None
        existing_aliases = [a.strip() for a in (df_obj.aliases or "").split(",") if a.strip()]
        if not any(a.lower() == alias.lower() for a in existing_aliases):
            existing_aliases.append(alias)
            df_obj.aliases = ", ".join(existing_aliases)
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                print(f"[_add_alias_to_field] Commit failed (field={fn}, alias={alias!r}): {e}")
                return None
        return df_obj

    def _apply_rule(fn, fl, val, rule_text):
        """Append rule to the active prompt version and patch the field on
        this document. Returns (version_label, learned_alias) - learned_alias
        is the label auto-taught for future documents, if any, so the caller
        can mention it in the confirmation message instead of it silently
        appearing with no explanation the next time someone looks at the
        field's aliases."""
        import json as _json
        learned_alias = None
        if doc.document_type and rule_text:
            from prompt_studio_service import get_active_prompt_version, create_prompt_version, activate_prompt_version
            active = get_active_prompt_version(doc.document_type)
            base = (active.prompt_text or "").strip() if active else ""
            # Replace existing rule for this field instead of duplicating
            marker = f"# Field correction rule ({fl})"
            if marker in base:
                import re as _re2
                base = _re2.sub(
                    r'# Field correction rule \(' + _re2.escape(fl) + r'\)\n.*?(?=\n#|\Z)',
                    '', base, flags=_re2.DOTALL
                ).strip()
            new_text = base + ("\n\n" if base else "") + f"# Field correction rule ({fl})\n{rule_text}"
            new_ver = create_prompt_version(
                doc.document_type, new_text,
                change_summary=f"Confirmed: {fl} ? {val}",
                created_by="guided-correction", bump="minor",
            )
            activate_prompt_version(new_ver.id)
            ver_label = new_ver.version_label
        else:
            ver_label = None
        # Patch the current document's field value
        if fn and val:
            try:
                fields = doc.get_fields()
                fields[fn] = val
                doc.set_fields(fields)
                # Value is now known — clear any stale "not detected" badge.
                try:
                    sources = doc.get_field_sources()
                    sources[fn] = "corrected"
                    doc.field_sources = _json.dumps(sources)
                except Exception:
                    pass
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                print(f"[_apply_rule] Field-patch commit failed (doc_id={doc.id}, field={fn}): {e}")
                # Re-raise so the caller (await_confirmation handler) reports
                # this as an explicit failure instead of a silent no-op that
                # still returns success -- previously this swallowed the
                # error and continued as if the field had actually been saved.
                raise
            # Saving a rule for future docs (not "just this document") ??? also
            # seed a Find-by label if we can spot how `val` appears in the text,
            # so the fast deterministic pass catches it next time without AI.
            # Literal regex match first (cheap, no LLM call); if the value
            # isn't introduced by any adjacent label at all (e.g. an
            # unlabeled letterhead value), fall back to asking the Q&A engine
            # what role/heading it plays instead of giving up.
            print(f"[LabelInfer] _apply_rule reached label-inference gate: rule_text={bool(rule_text)}, document_type={doc.document_type!r}")
            if rule_text and doc.document_type:
                try:
                    inferred_label = _infer_label_for_value(doc.raw_text or "", val)
                    print(f"[LabelInfer] regex pass result: {inferred_label!r}")
                    if not inferred_label:
                        inferred_label = _infer_label_via_qa(doc.raw_text or "", val, fl)
                    if inferred_label:
                        _add_alias_to_field(fn, inferred_label)
                        learned_alias = inferred_label
                except Exception as e:
                    import traceback
                    print(f"[LabelInfer] label-inference block crashed: {e}")
                    traceback.print_exc()
        return ver_label, learned_alias

    def _infer_field_type_from_name(name):
        """Crude type guess from a field's own name/label, used when there's no
        DocumentField row yet to read a real field_type from (e.g. a brand-new
        field being added for the first time) — just enough to pick the right
        plausibility check (date-shaped vs amount-shaped vs generic text).
        User-taught hints (FieldTypeHint, via the "Teach a type hint" chat flow)
        are checked first and win over the hardcoded lists below, since an
        explicit correction is more trustworthy than a hardcoded guess; the
        longest matching keyword wins among taught hints so a more specific
        word (e.g. "invoice_date") isn't shadowed by a shorter unrelated one."""
        n = (name or "").lower()
        try:
            from document_model import FieldTypeHint
            hints = FieldTypeHint.query.all()
            hints.sort(key=lambda h: len(h.keyword or ""), reverse=True)
            for hint in hints:
                if hint.keyword and hint.keyword.lower() in n:
                    return hint.field_type
        except Exception:
            pass
        if any(k in n for k in ("date", "tarikh", "deadline")):
            return "date"
        if any(k in n for k in ("amount", "total", "price", "subtotal", "tax", "balance", "fee", "cost", "payment")):
            return "currency"
        return "text"

    def _smart_locate_field_value(fn, fl):
        """Try to re-locate a flagged field's correct value in the document before
        asking the user to type it manually. Cheap regex/alias pass first, then an
        LLM re-scan scoped to just this field. Also reused for brand-new fields
        being added for the first time ??? current_val is naturally empty for a key
        that doesn't exist yet, so the "must differ from current" checks below
        just pass through. Returns (value, source) or (None, None)."""
        # Barcode-scan sourced fields are authoritative: the decode is more
        # reliable than any OCR/text/AI re-scan, so never overwrite them here.
        try:
            if doc.get_field_sources().get(fn) == "barcode scan":
                return None, None
        except Exception:
            pass
        # A "total pages" style field has a ground-truth answer that has nothing
        # to do with document content - it's just how many pages the uploaded
        # file has, which PyMuPDF already knows deterministically. Asking an LLM
        # to guess it from OCR/raw text is guaranteed to be unreliable: raw_text
        # often has no real page boundaries preserved (single unbroken block for
        # some documents), so both the narrow field re-scan and the general Q&A
        # engine end up latching onto unrelated numbers in the text (a postal
        # code, a numbered list item) rather than genuinely counting pages.
        # Short-circuit straight to the real count whenever the source file is
        # still on disk, before any of the guess-based tiers below ever run.
        if _re.search(r'\b(total|number\s+of|num\.?)\s*pages?\b|\bpages?\s*(total|count)\b|\bpage\s*count\b',
                       f"{fn or ''} {fl or ''}", _re.IGNORECASE):
            if doc.unique_upload_filename:
                _pc_path = os.path.join(UPLOAD_FOLDER, doc.unique_upload_filename)
                if os.path.exists(_pc_path):
                    try:
                        if (doc.file_type or "").lower() == "pdf":
                            import fitz
                            with fitz.open(_pc_path) as _pc_pdf:
                                _pc_count = _pc_pdf.page_count
                        else:
                            _pc_count = 1  # a single image upload is inherently one page
                        return str(_pc_count), "counted directly from the uploaded file's actual pages"
                    except Exception:
                        pass  # fall through to the guess-based tiers below
        aliases = []
        field_type = None
        if doc.document_type:
            from document_model import DocumentType as _DT_al, DocumentField as _DF_al
            dt_obj = _DT_al.query.filter_by(name=doc.document_type).first()
            if dt_obj:
                df_obj = _DF_al.query.filter_by(document_type_id=dt_obj.id, field_name=fn).first()
                if df_obj:
                    field_type = df_obj.field_type
                    if df_obj.aliases:
                        aliases = [a.strip() for a in df_obj.aliases.split(",") if a.strip()]
        if field_type is None:
            field_type = _infer_field_type_from_name(fn or fl)
        try:
            existing_fields = doc.get_fields()
        except Exception:
            existing_fields = {}
        current_val = str(existing_fields.get(fn) or "").strip().lower()

        def _plausible(v):
            return _is_plausible_value(field_type, v, field_name=fn, field_label=fl)

        labels = [fl, fn.replace("_", " ")] + aliases
        value, matched_label = find_field_value_in_text(doc.raw_text or "", labels)
        if value and value.strip().lower() != current_val and _plausible(value):
            return value, f"found next to \"{matched_label}\" in the document"

        # The browser's own PDF.js text layer sometimes captures text the
        # server's OCR/layout pass skipped (e.g. a large stylised heading like
        # a bank name) ??? it's already loaded client-side for viewing, so this
        # costs nothing extra: same free regex search, just a second text source.
        if client_text:
            cvalue, cmatched = find_field_value_in_text(client_text, labels)
            if cvalue and cvalue.strip().lower() != current_val and _plausible(cvalue):
                return cvalue, f"found next to \"{cmatched}\" in the document (from the on-screen PDF text)"
        try:
            # Prefer the client's PDF.js text when available ??? it's proven more
            # complete than the server's raw_text for layouts (headings, logos)
            # the OCR/layout pass skips, so the AI re-scan should see it too
            # instead of guessing from an incomplete source.
            _rescan_text = client_text if client_text else (doc.raw_text or "")
            extracted = extract_fields_on_demand(_rescan_text, [fn], None, doc.document_type or "")
            ext_val = extracted.get(fn)
            if ext_val and str(ext_val).strip().lower() != current_val and _plausible(ext_val):
                return ext_val, "AI re-scanned the document"
        except Exception:
            pass

        # Last resort: some values never made it into raw_text at all (e.g. a
        # header block the initial OCR/ODL pass skipped). Re-look at the actual
        # source image/PDF with the vision model, which can see layout that
        # text extraction missed.
        if doc.unique_upload_filename:
            file_path = os.path.join(UPLOAD_FOLDER, doc.unique_upload_filename)
            if os.path.exists(file_path):
                import time
                print(f"\n[Tier 4 Vision] Starting vision re-scan for field '{fl}' (doc_id={doc.id}, file={doc.unique_upload_filename})")
                print(f"[Tier 4 Vision] Aliases: {aliases}")
                print(f"[Tier 4 Vision] File path: {file_path}")
                print(f"[Tier 4 Vision] Is PDF: {(doc.file_type or '').lower() == 'pdf'}")
                tier4_start = time.time()
                try:
                    is_pdf = (doc.file_type or "").lower() == "pdf"
                    vision_val = find_field_value_in_image(file_path, is_pdf, fl, aliases)
                    tier4_elapsed = time.time() - tier4_start
                    print(f"[Tier 4 Vision] Completed in {tier4_elapsed:.2f}s")
                    print(f"[Tier 4 Vision] Result: {vision_val!r}")
                    if vision_val:
                        print(f"[Tier 4 Vision] Plausibility check: current_val={current_val!r}, vision_val={vision_val!r}")
                        if vision_val.strip().lower() != current_val:
                            is_plausible = _plausible(vision_val)
                            print(f"[Tier 4 Vision] Plausible: {is_plausible}")
                            if is_plausible:
                                print(f"[Tier 4 Vision] SUCCESS - Returning value")
                                return vision_val, "found by re-reading the original document image"
                            else:
                                print(f"[Tier 4 Vision] REJECTED - Failed plausibility check")
                        else:
                            print(f"[Tier 4 Vision] REJECTED - Same as current value")
                    else:
                        print(f"[Tier 4 Vision] FAILED - Vision model returned None/empty")
                except Exception as e:
                    tier4_elapsed = time.time() - tier4_start
                    print(f"[Tier 4 Vision] EXCEPTION after {tier4_elapsed:.2f}s: {type(e).__name__}: {e}")
                    import traceback
                    traceback.print_exc()
                    print(f"[Tier 4 Vision] Field lookup will report 'not found' but the real issue was an exception")
            else:
                print(f"[Tier 4 Vision] SKIPPED - File does not exist: {file_path}")
        else:
            print(f"[Tier 4 Vision] SKIPPED - No unique_upload_filename for doc_id={doc.id}")
        return None, None

    def _guided_start_or_suggest(fn, fl):
        """Return a value suggestion if we can auto-locate one; otherwise fall back
        to the manual "type the correct value" prompt."""
        val, source = _smart_locate_field_value(fn, fl)
        if val:
            return jsonify({"type": "guided_suggest_value", "field_name": fn, "field_label": fl,
                             "suggested_value": val, "source": source})
        return jsonify({"type": "guided_start", "field_name": fn, "field_label": fl, "explanation": "",
                         "searched": True})

    def _apply_new_field(fn, fl, val):
        """Write a newly-added field's value onto this document and return the
        same 'extraction' response shape the existing add-field flow already
        used, so the frontend's existing extraction-card + "apply to future
        docs?" handling works unchanged."""
        try:
            existing = doc.get_fields()
        except Exception:
            existing = {}
        existing[fn] = val
        doc.set_fields(existing)
        db.session.commit()
        return jsonify({"type": "extraction", "fields": existing, "new_fields": {fn: val},
                         "new_count": 1, "suggest_future": bool(doc.document_type),
                         "doc_type": doc.document_type or ""})

    def _locate_new_field_or_ask(fn, fl):
        """Add-field counterpart to _guided_start_or_suggest: same multi-tier
        search + plausibility validation, but for a field that doesn't exist on
        this document yet ??? confirm before writing it, and offer to type it
        manually if nothing plausible turns up, instead of a dead-end message."""
        val, source = _smart_locate_field_value(fn, fl)
        if val:
            return jsonify({"type": "guided_add_suggest", "field_name": fn, "field_label": fl,
                             "suggested_value": val, "source": source})
        return jsonify({"type": "guided_add_ask_value", "field_name": fn, "field_label": fl,
                         "message": f"I couldn't find a value for \"{fl}\" anywhere in the document "
                                    "(checked the extracted text, the on-screen PDF text, and the "
                                    "image itself). Want to type it in yourself?"})

    guided_context = data.get("guided_context") or {}

    # -- The browser keeps the last unambiguous field question as explicit
    # context.  A follow-up such as "create me the field" can therefore enter
    # the normal find-and-confirm flow without relying on intent/history
    # reconstruction. --
    if guided_context.get("step") == "create_last_asked_field":
        _last_fn = (guided_context.get("field_name") or "").strip().lower()
        _last_fl = (guided_context.get("field_label") or _last_fn.replace("_", " ").title()).strip()
        _last_fn = _re.sub(r"[^a-z0-9]+", "_", _last_fn).strip("_")
        if _last_fn:
            _last_av = (guided_context.get("last_answer_value") or "").strip()
            if _last_av and len(_last_av) <= 200:
                # The user asked about this field in the previous turn and already
                # got a clean answer; use it directly instead of running a fresh
                # re-scan (which would pick a different, often wrong, token).
                return jsonify({"type": "guided_add_suggest", "field_name": _last_fn, "field_label": _last_fl,
                                 "suggested_value": _last_av, "source": "from your last question"})
            return _locate_new_field_or_ask(_last_fn, _last_fl)
        return jsonify({"type": "guided_add", "message": "What field would you like to add?"})

    # -- A field-level type change is confirmed from the chat UI.  This changes
    # the saved DocumentField definition (rather than merely answering a
    # question about the current value), so future extractions validate the
    # field as the selected type too. --
    if guided_context.get("step") == "confirm_field_type":
        _ft_fn = (guided_context.get("field_name") or "").strip()
        _ft_fl = guided_context.get("field_label") or _ft_fn.replace("_", " ").title()
        _ft_type = (guided_context.get("field_type") or "").strip().lower()
        if _ft_fn and _ft_type in ("date", "currency", "number", "text") and doc.document_type:
            _ft_doc_type = DocumentType.query.filter_by(name=doc.document_type).first()
            _ft_field = (DocumentField.query.filter_by(document_type_id=_ft_doc_type.id, field_name=_ft_fn).first()
                         if _ft_doc_type else None)
            if _ft_field:
                _ft_field.field_type = _ft_type
                db.session.commit()
                return jsonify({"type": "answer",
                                "answer": f"Done! \"{_ft_fl}\" is now a {_ft_type} field. Future extractions will only accept valid {_ft_type} values.",
                                "source_quote": "", "page_number": None})
        return jsonify({"type": "answer", "answer": "I couldn't update that field type. It may not be part of this document type yet.", "source_quote": "", "page_number": None})

    # ?? Field-panel quick actions: the user CLICKED a specific field's icon in
    # the fields panel (not typed anything), so both "which field" and "what
    # kind of action" are already unambiguous ??? no intent classification
    # needed at all here. Each action just jumps straight into the same
    # already-hardened flow chat would eventually reach (correction with
    # confirm, remove with confirm, alias with confirm), so it inherits all of
    # that safety for free instead of re-implementing it.
    if guided_context.get("step") == "field_quick_action":
        _fq_fn = (guided_context.get("field_name") or "").strip()
        _fq_fl = guided_context.get("field_label") or _fq_fn.replace("_", " ").title()
        _fq_action = guided_context.get("action") or ""
        if not _fq_fn:
            return jsonify({"type": "answer", "answer": "Which field did you mean?", "source_quote": "", "page_number": None})
        if _fq_action == "correct":
            return _guided_start_or_suggest(_fq_fn, _fq_fl)
        if _fq_action == "remove":
            # If this field's current value is a duplicate of another field's,
            # offer merging as an alternative to the confirm dialog below,
            # instead of only ever offering to delete it outright.
            _rm_dup_fn, _rm_dup_fl = None, None
            try:
                _rm_existing = doc.get_fields()
                _rm_cur_val = _rm_existing.get(_fq_fn)
                if _rm_cur_val:
                    _rm_dup_fn, _rm_dup_fl = _find_duplicate_value_field(_fq_fn, _rm_cur_val)
            except Exception:
                pass
            return jsonify({"type": "guided_remove_confirm", "field_name": _fq_fn, "field_label": _fq_fl,
                             "duplicate_of_name": _rm_dup_fn, "duplicate_of_label": _rm_dup_fl})
        if _fq_action == "alias":
            return jsonify({"type": "guided_teach_alias_ask", "field_name": _fq_fn, "field_label": _fq_fl,
                             "message": f"Sure — what label should I also look for when detecting \"{_fq_fl}\"?"})
        if _fq_action == "type_hint":
            return jsonify({"type": "guided_teach_hint_ask", "field_name": _fq_fn, "field_label": _fq_fl,
                             "message": f"Sure — what word in a field's name should always mean a "
                                        f"certain type? (e.g. a word from \"{_fq_fl}\")"})
        return jsonify({"type": "answer", "answer": f"What would you like me to do with \"{_fq_fl}\"?", "source_quote": "", "page_number": None})

    # ?? Guided multi-turn: user confirms a re-extract. "replace" on an
    # untargeted request (user asked to re-extract everything) swaps in the
    # freshly extracted set wholesale; "replace" on a targeted request (user
    # named specific field(s)) only overwrites those field(s), same as
    # mode="merge" - it must never wipe fields that were never part of the
    # request. mode="merge" always keeps existing fields and only
    # adds/overwrites the newly extracted ones.
    if guided_context.get("step") == "confirm_extract":
        _ce_fields = guided_context.get("new_fields") or {}
        _ce_mode   = guided_context.get("mode") or "replace"
        _ce_targeted = bool(guided_context.get("targeted"))
        if _ce_fields:
            if _ce_mode == "merge" or _ce_targeted:
                try:
                    _ce_final = doc.get_fields()
                except Exception:
                    _ce_final = {}
                _ce_final.update(_ce_fields)
            else:
                _ce_final = _ce_fields
            doc.set_fields(_ce_final)
            db.session.commit()
            print(f"[ASK] Extracted {len(_ce_fields)} fields for doc {doc.id} (confirmed, mode={_ce_mode})")
            # Same "apply for future docs?" offer every other extraction path
            # already makes (qaHandleExtractionResult) - this step just never
            # populated the two fields it keys off. save_add_rule already
            # no-ops for names already on the schema, so it's safe to offer
            # the full set here rather than pre-filtering for ad-hoc ones.
            return jsonify({
                "type":            "extraction",
                "fields":          _ce_final,
                "new_fields":      _ce_fields,
                "new_count":       len(_ce_fields),
                "suggest_future":  bool(doc.document_type),
                "doc_type":        doc.document_type or "",
            })
        return jsonify({"type": "answer", "answer": "Something went wrong re-extracting the fields.", "source_quote": "", "page_number": None})

    # ?? Guided multi-turn: user confirms a found value for a NEW field ???????
    if guided_context.get("step") == "confirm_add_value":
        _cv_fn = (guided_context.get("field_name") or "").strip()
        _cv_fl = guided_context.get("field_label") or _cv_fn.replace("_", " ").title()
        _cv_val = (guided_context.get("value") or "").strip()
        if _cv_fn and _cv_val:
            return _apply_new_field(_cv_fn, _cv_fl, _cv_val)
        return jsonify({"type": "answer", "answer": "Something went wrong adding that field.", "source_quote": "", "page_number": None})

    # ?? Guided multi-turn: user types the value for a NEW field manually ????
    if guided_context.get("step") == "await_add_value":
        _av_fn = (guided_context.get("field_name") or "").strip()
        _av_fl = guided_context.get("field_label") or _av_fn.replace("_", " ").title()
        _av_val = question.strip()
        if _av_fn and _av_val and _is_topic_change(_av_val):
            # User asked/typed something unrelated instead of supplying a
            # value — drop guided state, handle as fresh QA instead of
            # silently saving their message as the field's value.
            return jsonify(_answer_with_field_offer(question))
        if _av_fn and _av_val:
            # Don't write a manually-typed value straight to the document —
            # confirm it first, same as the auto-located-value path already
            # does. Keeps a stray/ambiguous message that slipped past the
            # topic-change check above from ever landing in saved data.
            return jsonify({"type": "guided_confirm_typed_value", "field_name": _av_fn,
                             "field_label": _av_fl, "value": _av_val})
        return jsonify({"type": "answer", "answer": "Please provide a value.", "source_quote": "", "page_number": None})

    # ?? Guided multi-turn: user asks us to re-locate a specific field's value ???
    if guided_context.get("step") == "resolve_value":
        _rv_fn = (guided_context.get("field_name") or "").strip()
        _rv_fl = guided_context.get("field_label") or _rv_fn.replace("_", " ").title()
        if _rv_fn:
            return _guided_start_or_suggest(_rv_fn, _rv_fl)

    # ?? Guided multi-turn: user provides the label text for a new alias ??????
    if guided_context.get("step") == "await_alias_text":
        _aa_fn = (guided_context.get("field_name") or "").strip()
        _aa_fl = guided_context.get("field_label") or _aa_fn.replace("_", " ").title()
        _aa_alias = question.strip()
        # No `asked_for` here (unlike the other guided steps): alias text is
        # often itself a field-name-like phrase (e.g. "Pay to" as a synonym for
        # "Bill To"), which the AI judgment call in _is_topic_change tends to
        # misread as naming a *different* field rather than an alias for this
        # one. The explicit "Apply this?" confirm below still guards against a
        # genuine topic change slipping through.
        if _aa_fn and _aa_alias and _is_topic_change(_aa_alias):
            return jsonify(_answer_with_field_offer(question))
        if _aa_fn and _aa_alias:
            return jsonify({
                "type": "guided_teach_alias", "field_name": _aa_fn, "field_label": _aa_fl,
                "new_alias": _aa_alias,
                "message": f"Got it — I'll also look for \"{_aa_alias}\" when detecting \"{_aa_fl}\" "
                            "for future documents of this type. Apply this?",
            })

    # -- Guided multi-turn: user picked one of the candidates from the
    # field_ambiguities "N matches" picker. Reuses the exact same rule-
    # generation pipeline as a normal "Value is wrong" correction (scope
    # "future" runs _generate_guided_rule + _apply_rule; "doc_only" calls
    # _apply_rule with no rule text, which still patches the field but skips
    # the prompt-version step) - picking a candidate here IS a correction,
    # just reached from a different starting point. Deliberately does NOT
    # touch doc.field_ambiguities either way: the whole point of this flow is
    # that the pick is reversible, so the badge and both candidates stay
    # available in case the user changes their mind later. --
    if guided_context.get("step") == "resolve_ambiguity":
        _ra_fn = (guided_context.get("field_name") or "").strip()
        _ra_fl = guided_context.get("field_label") or _ra_fn.replace("_", " ").title()
        _ra_val = guided_context.get("value") or ""
        if _ra_fn and _ra_val:
            # Deliberately never generates a rule_text here (unlike other
            # _apply_rule callers) - doing so would teach the MODEL ITSELF to
            # prefer this value on every future document of this type,
            # corrupting the "extracted" baseline that future ambiguity
            # checks compare against. Picking a candidate here is meant to be
            # reversible and document-scoped; teaching the field's alias
            # (which _apply_rule still does) is enough to help future docs
            # surface the same candidate as an option without ever silently
            # overriding what the model itself reads.
            try:
                ver_label, learned_alias = _apply_rule(_ra_fn, _ra_fl, _ra_val, "")
            except Exception as _e_amb:
                print(f"[resolve_ambiguity] _apply_rule failed (doc_id={doc.id}, field={_ra_fn}): {_e_amb}")
                return jsonify({
                    "type": "guided_ambiguity_resolved", "success": False,
                    "error": str(_e_amb), "field_name": _ra_fn, "field_label": _ra_fl,
                })
            try:
                updated_fields = doc.get_fields()
            except Exception:
                updated_fields = None
            return jsonify({
                "type":           "guided_ambiguity_resolved",
                "success":        True,
                "field_name":     _ra_fn, "field_label": _ra_fl, "value": _ra_val,
                "version_label":  ver_label, "learned_alias": learned_alias,
                "fields":         updated_fields, "field_sources": doc.get_field_sources(),
                "field_ambiguities": doc.get_field_ambiguities(),
            })
        return jsonify({"type": "answer", "answer": "Something went wrong applying that value.", "source_quote": "", "page_number": None})

    # -- Guided multi-turn: user clicked "Let AI check" on a "matches" badge
    # that couldn't confirm a second value automatically at upload time (see
    # _detect_field_ambiguities) - runs the same alias lookup live, on
    # demand, and if it finds something, persists it as a real candidate
    # pair so the badge shows a confirmed match next time instead of the
    # placeholder. --
    if guided_context.get("step") == "ambiguity_ai_find":
        _aa_fn = (guided_context.get("field_name") or "").strip()
        _aa_fl = guided_context.get("field_label") or _aa_fn.replace("_", " ").title()
        if _aa_fn:
            _aa_dt = DocumentType.query.filter_by(name=doc.document_type).first()
            _aa_df = next((f for f in _aa_dt.fields if f.field_name == _aa_fn), None) if _aa_dt else None
            _aa_aliases = [a.strip() for a in (_aa_df.aliases or "").split(",") if a.strip()] if _aa_df else []
            if not _aa_aliases:
                return jsonify({"type": "answer",
                                 "answer": f"\"{_aa_fl}\" doesn't have any taught aliases to check.",
                                 "source_quote": "", "page_number": None})
            try:
                _aa_fields = doc.get_fields()
            except Exception:
                _aa_fields = {}
            _aa_current = _aa_fields.get(_aa_fn)
            _aa_val, _aa_source = _lookup_alias_value(doc.raw_text or "", _aa_aliases)
            if (_aa_val and len(_aa_val) <= 300 and isinstance(_aa_current, str)
                    and not _values_are_near_duplicate(_aa_current, _aa_val)):
                _aa_ambig = doc.get_field_ambiguities() or {}
                _aa_ambig[_aa_fn] = [
                    {"value": _aa_current, "source": "extracted"},
                    {"value": _aa_val, "source": _aa_source},
                ]
                doc.set_field_ambiguities(_aa_ambig)
                db.session.commit()
                return jsonify({"type": "guided_ambiguity_found", "field_name": _aa_fn, "field_label": _aa_fl,
                                 "value": _aa_val, "source": _aa_source, "field_ambiguities": doc.get_field_ambiguities()})
            return jsonify({"type": "answer",
                             "answer": f"I checked the taught alias{'es' if len(_aa_aliases) != 1 else ''} for \"{_aa_fl}\" "
                                       "but couldn't find a different value on this document.",
                             "source_quote": "", "page_number": None})
        return jsonify({"type": "answer", "answer": "Something went wrong.", "source_quote": "", "page_number": None})

    # -- Guided multi-turn: user clicked a candidate field from the
    # guided_disambig_alias options instead of typing one - field_name is now
    # certain, so this just re-renders the same "Apply this?" confirm that
    # await_alias_text produces once it has both pieces. --
    if guided_context.get("step") == "teach_alias_pick_field":
        _tp_fn = (guided_context.get("field_name") or "").strip()
        _tp_alias = (guided_context.get("new_alias") or "").strip()
        if _tp_fn and _tp_alias:
            _tp_fl = _tp_fn.replace("_", " ").title()
            return jsonify({
                "type": "guided_teach_alias", "field_name": _tp_fn, "field_label": _tp_fl,
                "new_alias": _tp_alias,
                "message": f"Got it — I'll also look for \"{_tp_alias}\" when detecting \"{_tp_fl}\" "
                            "for future documents of this type. Apply this?",
            })
        return jsonify({"type": "answer", "answer": "Something went wrong.", "source_quote": "", "page_number": None})

    # ?? Guided multi-turn: user confirms a new alias should be saved ?????????
    if guided_context.get("step") == "confirm_alias":
        _ca_fn = (guided_context.get("field_name") or "").strip()
        _ca_fl = guided_context.get("field_label") or _ca_fn.replace("_", " ").title()
        _ca_alias = (guided_context.get("new_alias") or "").strip()
        # "future" (default): save the alias for future documents of this type,
        # same as before. "doc_only": skip saving it - the other label might be
        # specific to this one document (e.g. a one-off "Target Ministry" line
        # that won't appear on every future document of this type), so the user
        # only wants this document's value swapped in, not a standing rule.
        _ca_scope = guided_context.get("scope") or "future"
        if _ca_fn and _ca_alias:
            _alias_saved = False
            if _ca_scope != "doc_only":
                # _add_alias_to_field silently no-ops if this field isn't a
                # registered DocumentField on the current document type (e.g. an
                # ad-hoc field the AI extracted that was never added via the
                # Document Type Manager) - track whether it actually persisted
                # so the message below doesn't promise something that didn't
                # happen.
                _alias_saved = _add_alias_to_field(_ca_fn, _ca_alias) is not None
            # Try applying THIS alias's own value to the current document,
            # searching specifically under the alias label rather than the
            # field's usual label/name - the whole point is finding the more
            # specific value filed under a different heading, not re-finding
            # the value already sitting in the field. Same fallback tiers as
            # the "Value is wrong" flow (plain text, then the client-side
            # PDF.js text layer, then a vision re-read of the image) instead of
            # the single regex pass this used to be limited to, so it's no
            # less reliable here than an explicit correction would be.
            applied_val = None
            try:
                _ca_type = _get_field_type(_ca_fn) or _infer_field_type_from_name(_ca_fn or _ca_fl)
                _found_val, _ = find_field_value_in_text(doc.raw_text or "", [_ca_alias])
                if not _found_val and client_text:
                    _found_val, _ = find_field_value_in_text(client_text, [_ca_alias])
                if not _found_val and doc.unique_upload_filename:
                    _ca_file_path = os.path.join(UPLOAD_FOLDER, doc.unique_upload_filename)
                    if os.path.exists(_ca_file_path):
                        _ca_is_pdf = (doc.file_type or "").lower() == "pdf"
                        _found_val = find_field_value_in_image(_ca_file_path, _ca_is_pdf, _ca_alias, [])
                if _found_val and _is_plausible_value(_ca_type, _found_val, field_name=_ca_fn, field_label=_ca_fl):
                    _existing = doc.get_fields()
                    _cur = str(_existing.get(_ca_fn) or "").strip().lower()
                    if _found_val.strip().lower() != _cur:
                        _existing[_ca_fn] = _found_val
                        doc.set_fields(_existing)
                        _srcs = doc.get_field_sources()
                        _srcs[_ca_fn] = "alias '" + _ca_alias + "'"
                        doc.field_sources = _json.dumps(_srcs)
                        db.session.commit()
                        applied_val = _found_val
            except Exception as _e_ca:
                db.session.rollback()
                print(f"[correct] Alias-based lookup/commit failed (doc_id={doc.id}, field={_ca_fn}): {_e_ca}")

            if _ca_scope == "doc_only":
                _msg = (f"Done! I've updated \"{_ca_fl}\" on this document to: {applied_val}."
                        if applied_val else
                        f"I looked for \"{_ca_alias}\" on this document but couldn't find a confident "
                        f"value for it, so \"{_ca_fl}\" is unchanged.")
            elif _alias_saved:
                _msg = (f"Done! I'll now also look for \"{_ca_alias}\" when detecting "
                        f"\"{_ca_fl}\" for future documents of this type.")
                if applied_val:
                    _msg += f" I also found and applied it to this document: {applied_val}."
            else:
                _msg = (f"\"{_ca_fl}\" isn't a saved field on this document type yet, so I "
                        f"can't teach a permanent alias for it - add it as a field in the "
                        f"Document Type Manager first.")
                if applied_val:
                    _msg += f" I did find and apply a value to this document though: {applied_val}."
            try:
                _updated_fields = doc.get_fields()
            except Exception:
                _updated_fields = None
            return jsonify({
                "type": "guided_alias_done", "message": _msg,
                "field_name": _ca_fn, "field_label": _ca_fl,
                "fields": _updated_fields, "field_sources": doc.get_field_sources(),
            })
        return jsonify({"type": "answer", "answer": "Something went wrong saving that label.", "source_quote": "", "page_number": None})

    # -- Guided multi-turn: user provides a keyword for a new field-type hint --
    # (used by _infer_field_type_from_name to guess a type for fields that
    # aren't registered in any Document Type yet, instead of defaulting to a
    # hardcoded keyword list baked into the code)
    if guided_context.get("step") == "await_hint_keyword":
        _hk_kw = question.strip().lower()
        if _hk_kw and _is_topic_change(_hk_kw):
            return jsonify(_answer_with_field_offer(question))
        if _hk_kw:
            return jsonify({
                "type": "guided_pick_hint_type", "keyword": _hk_kw,
                "message": f"Got it. What type should a field be treated as when its name contains "
                           f"\"{_hk_kw}\"?",
            })
        return jsonify({"type": "answer", "answer": "Please type a keyword.", "source_quote": "", "page_number": None})

    # -- Guided multi-turn: user picked the type for a new field-type hint ----
    if guided_context.get("step") == "confirm_type_hint":
        from document_model import FieldTypeHint
        _ht_kw   = (guided_context.get("keyword") or "").strip().lower()
        _ht_type = (guided_context.get("field_type") or "").strip().lower()
        if _ht_kw and _ht_type in ("date", "currency", "number", "text"):
            existing = FieldTypeHint.query.filter_by(keyword=_ht_kw).first()
            if existing:
                existing.field_type = _ht_type
            else:
                db.session.add(FieldTypeHint(keyword=_ht_kw, field_type=_ht_type))
            db.session.commit()
            return jsonify({
                "type": "guided_hint_done",
                "message": f"Done! Any field that isn't already saved in a Document Type, but has "
                           f"\"{_ht_kw}\" in its name, will now be guessed as {_ht_type} instead of "
                           f"a hardcoded guess. This won't change fields you've already added to a "
                           f"Document Type - those keep their own saved type.",
            })
        return jsonify({"type": "answer", "answer": "Something went wrong saving that hint.", "source_quote": "", "page_number": None})

    # ?? Guided multi-turn: user tells us which field to add ?????????????????
    if guided_context.get("step") == "save_add_rule":
        _ar_fields = guided_context.get("field_names") or []
        _ar_dtype  = guided_context.get("doc_type") or doc.document_type or ""
        # Whatever happens below, the user has now answered "apply to future
        # docs?" for this document - don't ask again on a later reopen (see
        # get_ad_hoc_fields).
        if not doc.ad_hoc_fields_dismissed:
            doc.ad_hoc_fields_dismissed = True
            db.session.commit()
        if _ar_fields and _ar_dtype:
            try:
                from document_model import DocumentType as _DT_ar, DocumentField as _DF_ar
                def _norm_ar(s): return (s or "").lower().replace("_"," ").replace("-"," ").strip()
                # Find the DocumentType by normalized name, or fuzzy match
                _dt_obj = next((t for t in _DT_ar.query.all() if _norm_ar(t.name) == _norm_ar(_ar_dtype)), None)
                if not _dt_obj:
                    _all_types = _DT_ar.query.all()
                    _tw = set(_norm_ar(_ar_dtype).split())
                    _best, _best_score = None, 0
                    for _t in _all_types:
                        _score = len(_tw & set(_norm_ar(_t.name).split()))
                        if _score > _best_score:
                            _best, _best_score = _t, _score
                    if _best and _best_score > 0:
                        _dt_obj = _best
                        print(f"[save_add_rule] fuzzy matched '{_ar_dtype}' to '{_dt_obj.name}'")
                if _dt_obj:
                    _added_fields = []
                    _existing_fnames = {f.field_name for f in (_dt_obj.fields or [])}
                    _max_sort = max((f.sort_order for f in (_dt_obj.fields or [])), default=-1)
                    # The field's value was already saved onto THIS document by
                    # the time the user gets here (manual entry or an AI guess
                    # they confirmed) - use it to seed a Find-by alias from the
                    # actual label text in front of it, same as the correction
                    # flow does, so future documents of this type get a
                    # deterministic hint instead of relying on the AI to find
                    # this field from its name alone every single time.
                    try:
                        _ar_cur_fields = doc.get_fields()
                    except Exception:
                        _ar_cur_fields = {}
                    for _arf in _ar_fields:
                        _arl = _arf.replace("_", " ").title()
                        if _arf not in _existing_fnames:
                            _max_sort += 1
                            _ar_inferred_alias = None
                            _ar_cur_val = _ar_cur_fields.get(_arf)
                            if _ar_cur_val:
                                try:
                                    _ar_inferred_alias = _infer_label_for_value(doc.raw_text or "", _ar_cur_val)
                                except Exception:
                                    _ar_inferred_alias = None
                            _new_field = _DF_ar(
                                document_type_id=_dt_obj.id,
                                field_label=_arl,
                                field_name=_arf,
                                field_type="text",
                                is_required=False,
                                sort_order=_max_sort,
                                aliases=_ar_inferred_alias,
                            )
                            db.session.add(_new_field)
                            _added_fields.append(_arl)
                    if _added_fields:
                        db.session.commit()
                        _arl_names = ", ".join(_added_fields)
                        print(f"[save_add_rule] added fields {_added_fields} to type '{_dt_obj.name}'")
                        return jsonify({"type": "answer",
                                        "answer": f"Done! I'll now automatically extract **{_arl_names}** from future **{_ar_dtype}** documents. Re-upload or re-extract this document to apply it here too.",
                                        "source_quote": "", "page_number": None})
                    else:
                        return jsonify({"type": "answer", "answer": "This field is already set up for future documents.",
                                        "source_quote": "", "page_number": None})
                else:
                    print(f"[save_add_rule] doc type '{_ar_dtype}' not found in DB")
            except Exception as _e_ar:
                import traceback as _tb_ar
                print(f"[save_add_rule] error: {_e_ar}\n{_tb_ar.format_exc()}")
        return jsonify({"type": "answer", "answer": "Saved! This field will be extracted from future documents.",
                        "source_quote": "", "page_number": None})

    # -- User answered "just this document" to the ad-hoc-fields prompt -
    # leave the fields off the type's saved schema, just remember not to ask
    # again for this specific document (see get_ad_hoc_fields). --
    if guided_context.get("step") == "dismiss_adhoc_fields":
        doc.ad_hoc_fields_dismissed = True
        db.session.commit()
        return jsonify({"type": "answer",
                        "answer": "Got it — I'll leave these as one-off fields for this document only.",
                        "source_quote": "", "page_number": None})

    # -- User answered "no" to the ad-hoc-fields prompt - unlike "just this
    # document" (which keeps the values but skips the schema), this drops the
    # values themselves from this document too. Same ad_hoc_fields_dismissed
    # flag either way, so the prompt doesn't reappear on reopen. --
    if guided_context.get("step") == "reject_adhoc_fields":
        _raf_names = guided_context.get("field_names") or []
        try:
            _raf_fields = doc.get_fields()
        except Exception:
            _raf_fields = {}
        _raf_removed = [k for k in _raf_names if k in _raf_fields]
        for k in _raf_removed:
            del _raf_fields[k]
        doc.set_fields(_raf_fields)
        doc.ad_hoc_fields_dismissed = True
        db.session.commit()
        return jsonify({"type": "remove", "fields": _raf_fields, "removed": _raf_removed,
                        "answer": "Got it — I've removed those fields from this document."})

    # -- User answered "never suggest again for this type" to the ad-hoc-fields
    # prompt - adds the field names to the document type's permanent exclude list,
    # removes the values from this document, and sets ad_hoc_fields_dismissed so
    # the prompt doesn't reappear. Future documents of this type will silently
    # drop these fields in get_ad_hoc_fields() without prompting. --
    if guided_context.get("step") == "reject_adhoc_permanently":
        _rap_names = guided_context.get("field_names") or []
        _rap_dtype = doc.document_type
        if _rap_names and _rap_dtype:
            try:
                from document_model import DocumentType as _DT_rap
                _dt_rap = _DT_rap.query.filter_by(name=_rap_dtype).first()
                if _dt_rap:
                    for _fn_rap in _rap_names:
                        _dt_rap.add_excluded_field(_fn_rap)
                    # Remove the fields from this document too
                    try:
                        _rap_fields = doc.get_fields()
                    except Exception:
                        _rap_fields = {}
                    _rap_removed = [k for k in _rap_names if k in _rap_fields]
                    for k in _rap_removed:
                        del _rap_fields[k]
                    doc.set_fields(_rap_fields)
                    doc.ad_hoc_fields_dismissed = True
                    db.session.commit()
                    _rap_labels = ", ".join(_rap_names)
                    print(f"[reject_adhoc_permanently] added {_rap_names} to '{_rap_dtype}' exclude list")
                    return jsonify({
                        "type": "remove",
                        "fields": _rap_fields,
                        "removed": _rap_removed,
                        "answer": f"Got it — I've removed **{_rap_labels}** from this document and will never suggest them again for **{_rap_dtype}** documents."
                    })
                else:
                    print(f"[reject_adhoc_permanently] doc type '{_rap_dtype}' not found")
            except Exception as _e_rap:
                import traceback as _tb_rap
                print(f"[reject_adhoc_permanently] error: {_e_rap}\n{_tb_rap.format_exc()}")
        # Fallback
        doc.ad_hoc_fields_dismissed = True
        db.session.commit()
        return jsonify({"type": "answer", "answer": "Saved! Those fields won't be suggested again."})

    if guided_context.get("step") == "remove_all_confirmed":
        import json as _json_ra
        try:
            doc.set_fields({})
            db.session.commit()
        except Exception as _e_ra:
            db.session.rollback()
            return jsonify({"type": "answer", "answer": f"Sorry, I couldn't remove the fields: {_e_ra}", "source_quote": "", "page_number": None})
        return jsonify({"type": "remove", "removed": "all", "fields": {}})

    if guided_context.get("step") == "await_field_name_add":
        _SOCIAL = {
            'thank you','thanks','tq','ty','ok','okay','alright','noted','great',
            'good','nice','cool','done','got it','yes','no','sure','bye','goodbye',
            'terima kasih','ok terima kasih','terima kasih banyak','sama sama',
        }
        if question.strip().lower() in _SOCIAL:
            return jsonify({"type": "answer", "answer": "You're welcome! Let me know if there's anything else I can help with.", "source_quote": "", "page_number": None})
        field_names = [q.strip() for q in question.replace(",", " ").split() if q.strip()]
        # Use whole question as one field name if it reads like a label (not a list)
        if len(field_names) == 1 or " " in question.strip():
            field_names = [question.strip()]
        # Detect currency conversion requests
        import re as _re3
        _CURRENCIES = {
            'myr': 'MYR', 'ringgit': 'MYR',
            'usd': 'USD', 'dollar': 'USD',
            'eur': 'EUR', 'euro': 'EUR',
            'gbp': 'GBP', 'pound': 'GBP',
            'sgd': 'SGD', 'jpy': 'JPY', 'yen': 'JPY',
            'cny': 'CNY', 'yuan': 'CNY', 'rmb': 'CNY',
            'aud': 'AUD', 'cad': 'CAD', 'chf': 'CHF',
            'thb': 'THB', 'idr': 'IDR', 'php': 'PHP', 'inr': 'INR',
        }
        _ql = question.strip().lower()
        _target_cur = None
        for kw, code in _CURRENCIES.items():
            if kw in _ql:
                _target_cur = code
                break

        def _live_rate(from_cur, to_cur):
            rate, source = _fetch_live_fx_rate(from_cur, to_cur)
            return rate

        def _detect_doc_currency(raw):
            import re as _rxc
            if _rxc.search(r'S\$', raw): return 'SGD'
            if _rxc.search(r'A\$', raw): return 'AUD'
            if _rxc.search(r'C\$', raw): return 'CAD'
            if '$' in raw: return 'USD'
            if '€' in raw: return 'EUR'
            if '£' in raw: return 'GBP'
            if '¥' in raw: return 'JPY'
            for code in ('USD','EUR','GBP','SGD','MYR','JPY','CNY','AUD','CAD','CHF'):
                if _rxc.search(r'\b' + code + r'\b', raw):
                    return code
            return 'USD'

        if _target_cur:
            # Strip action words, prepositions, currency keywords to get the base field name
            _ACTION = r'\b(add|tambah|tolong|please|baru|new|field|create|include|' \
                      r'in|to|as|dalam|untuk|ke|sebagai)\b'
            _base = _re3.sub(r'\b(' + '|'.join(_CURRENCIES.keys()) + r')\b', '', _ql)
            _base = _re3.sub(_ACTION, '', _base)
            _base = _re3.sub(r'\s+', ' ', _base).strip()
            import sys as _sys
            print(f"[CUR_DBG] target_cur={_target_cur} base_field={repr(_base)}", file=_sys.stderr, flush=True)
            # Use extract_fields_on_demand with the cleaned base field name
            _base_fields = extract_fields_on_demand(
                doc.raw_text, [_base] if _base else field_names,
                None, doc.document_type or ""
            )
            print(f"[CUR_DBG] extract returned: {_base_fields}", file=_sys.stderr, flush=True)
            # Pick the first numeric value we find
            _raw_val = next(iter(_base_fields.values()), "") if _base_fields else ""
            _num_m = _re3.search(r'[\d,]+\.?\d*', _raw_val)
            _amount = float(_num_m.group().replace(',', '')) if _num_m else None
            # Detect source currency from the raw value first, then from doc
            _src_cur = None
            for code in ('USD','EUR','GBP','SGD','MYR','JPY','CNY','AUD','CAD','CHF'):
                if _re3.search(r'\b' + code + r'\b', _raw_val.upper()):
                    _src_cur = code
                    break
            if not _src_cur:
                for sym, code in [('S$','SGD'),('A$','AUD'),('C$','CAD'),
                                   ('$','USD'),('€','EUR'),('£','GBP'),('¥','JPY'),('RM','MYR')]:
                    if sym in _raw_val:
                        _src_cur = code
                        break
            if not _src_cur:
                _src_cur = _detect_doc_currency(doc.raw_text or "")
            print(f"[CUR_DBG] amount={_amount} src_cur={_src_cur} raw_val={repr(_raw_val)}", file=_sys.stderr, flush=True)
            if _amount and _src_cur and _src_cur != _target_cur:
                _rate = _live_rate(_src_cur, _target_cur)
                print(f"[CUR_DBG] rate {_src_cur}->{_target_cur}: {_rate}", file=_sys.stderr, flush=True)
                if _rate:
                    _converted = round(_amount * _rate, 2)
                    _val = f"{_target_cur} {_converted:,.2f}"
                else:
                    _val = _raw_val  # API unreachable fallback
            elif _amount:
                print(f"[CUR_DBG] src==target or no src, skipping conversion", file=_sys.stderr, flush=True)
                _val = _raw_val  # same currency, keep as-is
            else:
                print(f"[CUR_DBG] no amount found, val=empty", file=_sys.stderr, flush=True)
                _val = ""
            print(f"[CUR_DBG] final val={repr(_val)}", file=_sys.stderr, flush=True)
            _key = _re3.sub(r'[^a-z0-9]+', '_', _ql).strip('_')
            new_fields = {_key: _val} if _val else {}
        elif len(field_names) == 1:
            # Single named field: same multi-tier search + confirm-before-write
            # flow as field corrections, instead of writing an AI guess straight
            # to the document.
            _new_fl2 = field_names[0].replace("_", " ").title()
            _new_fn2 = _re3.sub(r'[^a-z0-9]+', '_', field_names[0].lower()).strip('_')
            if _new_fn2:
                return _locate_new_field_or_ask(_new_fn2, _new_fl2)
            new_fields = {}
        else:
            new_fields = extract_fields_on_demand(doc.raw_text, field_names, None, doc.document_type or "")
        if new_fields:
            try:
                existing = doc.get_fields()
            except Exception:
                existing = {}
            merged = {**existing, **new_fields}
            doc.set_fields(merged)
            db.session.commit()
            _sf = bool(doc.document_type)
            return jsonify({"type": "extraction", "fields": merged, "new_fields": new_fields,
                            "new_count": len(new_fields),
                            "suggest_future": _sf, "doc_type": doc.document_type or ""})
        return jsonify({"type": "answer", "answer": f"I couldn't find \"{question.strip()}\" in this document. Check the Raw Text tab to confirm the field exists.", "source_quote": "", "page_number": None})

    # ?? Guided multi-turn: user tells us which field to remove ???????????????
    if guided_context.get("step") == "await_field_name_remove":
        try:
            existing = doc.get_fields()
        except Exception:
            existing = {}

        if not guided_context.get("confirmed") and _is_topic_change(question, "the name of the field to remove"):
            # User changed topic — drop guided state, handle as fresh QA
            from qwen_service import answer_question as _aqe
            _res = _aqe(doc.raw_text, question, history=history)
            _res["type"] = "answer"
            return jsonify(_res)
        q_norm = question.strip().lower().replace(" ", "_")
        if not guided_context.get("confirmed"):
            matched_key = None
            for key in existing.keys():
                if key.lower().replace(" ", "_") == q_norm or key.lower() == question.strip().lower():
                    matched_key = key
                    break
            if matched_key:
                return jsonify({"type": "guided_remove_confirm", "field_name": matched_key, "field_label": matched_key.replace("_", " ").title()})
            # No exact match - try to understand what the user meant
            import re as _rem
            _qw = set(_rem.sub(r'[^a-z0-9 ]', ' ', question.strip().lower()).split())
            # If user said "X in [currency]", they mean a converted field - don't fuzzy match base field
            _CUR_WORDS = {'myr','usd','eur','gbp','sgd','jpy','cny','aud','cad','chf','thb','idr','php','inr','ringgit','dollar','euro'}
            _has_currency = bool(_qw & _CUR_WORDS)
            # Fuzzy: find a field whose words all appear in the question (skip if currency present)
            _fuzzy = None
            if not _has_currency:
                for _fk in existing.keys():
                    if set(_fk.lower().replace("_", " ").split()) & _qw:
                        _fuzzy = _fk; break
            # Escape: correction keywords mean user wants to correct, not remove
            _COR = {"wrong","salah","incorrect","not right","tak betul","tidak betul","betul","fix","change","ubah"}
            if _COR & _qw:
                if _fuzzy:
                    return _guided_start_or_suggest(_fuzzy, _fuzzy.replace("_", " ").title())
                return jsonify({"type": "answer", "answer": "It looks like you want to correct a field value. Which field is wrong and what should it be?", "source_quote": "", "page_number": None})
            # Vague remove phrases: re-ask with field list (only when no currency mismatch)
            _VAG = {"want","remove","delete","buang","hapus","field","tolong","please","a","an","the"}
            if (_VAG & _qw) and not _fuzzy and not _has_currency:
                _hint = "Current fields: " + ", ".join(list(existing.keys())[:12])
                return jsonify({"type": "guided_remove", "message": "Which field would you like to remove? " + _hint,
                                "fields": list(existing.keys())[:12]})
            # Fuzzy match found - confirm with user
            if _fuzzy:
                return jsonify({"type": "guided_remove_confirm", "field_name": _fuzzy, "field_label": _fuzzy.replace("_", " ").title()})
            # Try reconstruction: "base_in_currency" before giving up
            _cur_kw2 = next(iter(_qw & _CUR_WORDS), None) if _has_currency else None
            if _cur_kw2:
                _ql_lower2 = question.strip().lower()
                _stop2 = {"remove","delete","buang","hapus","field","the","a","an","please","tolong","want","in","to"}
                _base_words2 = _qw - _CUR_WORDS - _stop2
                _base2 = "_".join(sorted(_base_words2, key=lambda w: _ql_lower2.find(w)))
                _recon2_key = _base2 + "_in_" + _cur_kw2
                _recon2 = None
                for _ek2 in existing.keys():
                    if _ek2.lower().replace(" ", "_") == _recon2_key:
                        _recon2 = _ek2; break
                if _recon2:
                    return jsonify({"type": "guided_remove_confirm", "field_name": _recon2,
                                    "field_label": _recon2.replace("_", " ").title()})
            _flist = ", ".join(list(existing.keys())[:10])
            return jsonify({"type": "answer", "answer": f"I couldn't find that field. Available fields: {_flist}.", "source_quote": "", "page_number": None})
        removed = []
        for key in list(existing.keys()):
            if key.lower().replace(" ", "_") == q_norm or key.lower() == question.strip().lower():
                del existing[key]
                removed.append(key)
                break
        if removed:
            doc.set_fields(existing)
            # "Remove for future docs too" ??? also drop the field definition from
            # the document type's schema, so process_document stops extracting it
            # on future uploads of this type. "Just this document" (default)
            # leaves the type definition untouched, matching the pre-existing
            # behavior of only affecting the current document.
            type_removed = False
            if guided_context.get("scope") == "future" and doc.document_type:
                dt_obj = DocumentType.query.filter_by(name=doc.document_type).first()
                if dt_obj:
                    df_obj = DocumentField.query.filter_by(document_type_id=dt_obj.id, field_name=removed[0]).first()
                    if df_obj:
                        db.session.delete(df_obj)
                        type_removed = True
            db.session.commit()
            return jsonify({"type": "remove", "fields": existing, "removed": removed, "type_removed": type_removed})
        return jsonify({"type": "answer", "answer": f"I couldn't find a field named \"{question.strip()}\" to remove. Check the field names in the panel.", "source_quote": "", "page_number": None})

    # -- One-shot: "Just ask a question" was clicked from a specific field's
    # quick-action panel, so the very next message should be answered with
    # that field in scope (e.g. clicking the "Alamat" field then typing
    # "translate" should translate Alamat's value, not summarise the whole
    # document). The field's current value is folded into the question sent
    # to the Q&A engine; the field is already known for certain here, so the
    # save-offer below is attached directly instead of going through
    # _match_question_to_field's fuzzy matching - guarded by
    # _is_transformation_request so "translate"/"summarize"/"explain" don't
    # trigger a "save this?" offer for what was never a proposed correction. --
    if guided_context.get("step") == "field_context_question":
        _fq2_fn = (guided_context.get("field_name") or "").strip()
        _fq2_fl = guided_context.get("field_label") or _fq2_fn.replace("_", " ").title()
        _fq2_numeric_only = _re.search(
            r"\b(?:numeric|numbers?|digits?|integer)\b(?:\s+\w+){0,3}\s+\b(?:only|value|values|detect)\b|"
            r"\b(?:numbers?|digits?)\s+only\b",
            question or "", _re.IGNORECASE,
        )
        if _fq2_fn and _fq2_numeric_only and doc.document_type:
            _fq2_dt = DocumentType.query.filter_by(name=doc.document_type).first()
            _fq2_schema_field = (DocumentField.query.filter_by(document_type_id=_fq2_dt.id, field_name=_fq2_fn).first()
                                 if _fq2_dt else None)
            if _fq2_schema_field and _fq2_schema_field.field_type != "number":
                return jsonify({
                    "type": "guided_field_type_confirm",
                    "field_name": _fq2_fn,
                    "field_label": _fq2_fl,
                    "field_type": "number",
                    "message": f"Yes. I can set \"{_fq2_fl}\" to accept numbers only. Apply this to future {doc.document_type} documents?",
                })
        _fq2_val = ""
        try:
            _fq2_fields = doc.get_fields()
            _fq2_val = str(_fq2_fields.get(_fq2_fn) or "").strip()
        except Exception:
            pass
        _fq2_q = (
            f'Regarding the "{_fq2_fl}" field (its current value on this document is: "{_fq2_val}"): {question}'
            if _fq2_val else f'Regarding the "{_fq2_fl}" field: {question}'
        )
        _fq2_result = answer_question(doc.raw_text, _fq2_q, history=history)
        _fq2_result["type"] = "answer"
        _fq2_quote = (_fq2_result.get("source_quote") or "").strip()
        if not _is_transformation_request(question):
            _fq2_offer_val = _best_field_offer_value(
                _fq2_result.get("answer", ""),
                _fq2_quote,
                _fq2_result.get("value", ""),
            )
            if _fq2_offer_val:
                _fq2_result["field_offer"] = {"field_name": _fq2_fn, "field_label": _fq2_fl, "value": _fq2_offer_val}
        return jsonify(_fq2_result)

    # -- Guided multi-turn: user asks the Q&A engine to find a field's value,
    # instead of typing it themselves - the practical fallback for scripts
    # (e.g. Jawi) that can't be typed on a normal keyboard. Deliberately a
    # separate tool from the multi-tier search behind "Correct the value"
    # (_smart_locate_field_value): that path targets structured extraction
    # and can still come up empty or wrong (as seen on this exact document -
    # it matched "Ruj. Kami" instead of the real subject), while the
    # conversational Q&A engine reasons about the question more broadly and
    # found the right excerpt when asked directly. Reuses guided_suggest_value
    # so it flows through the same confirm/duplicate-check pipeline as any
    # other suggested value. --
    if guided_context.get("step") == "ai_find_value":
        _af_fn = (guided_context.get("field_name") or "").strip()
        _af_fl = guided_context.get("field_label") or _af_fn.replace("_", " ").title()
        if _af_fn:
            # Lowercase the label in the question itself -- verified empirically
            # that this local model reads a capitalized label ("What is the
            # Subject?") as a literal document code/keyword to search for and
            # matches the wrong line, while the lowercase common-noun phrasing
            # ("what is the subject?") reliably reasons about the actual theme
            # of the document instead. Reproduced consistently on a real
            # document before relying on it here.
            #
            # Explicitly ask for a short, standalone value (not a full sentence):
            # answer_question's system prompt allows it to calculate/derive values
            # that are never literally printed in the document (e.g. a total page
            # count worked out by counting [PAGE N] markers) - those legitimately
            # have no "source_quote" to cite, but "answer" still has the real value,
            # just wrapped in prose by default without this steer.
            _af_result = answer_question(
                doc.raw_text,
                f'what is the {_af_fl.lower()}? Reply with just the value itself (e.g. a number or short '
                f'phrase), no full sentence or explanation - even if you have to count or calculate it '
                f'rather than quote it directly.',
                history=[],
            )
            _af_quote = (_af_result.get("source_quote") or "").strip()
            _af_answer = (_af_result.get("answer") or "").strip()
            _af_model_value = (_af_result.get("value") or "").strip()
            _af_field_type = _get_field_type(_af_fn) or _infer_field_type_from_name(_af_fn or _af_fl)
            # For numeric/currency fields, pull the first number from the answer
            # when the model didn't already return a clean standalone value.
            if _af_field_type in ("currency", "number") and _af_answer and not _af_model_value:
                _af_num = _re.search(r'\d[\d,]*(?:\.\d+)?', _af_answer)
                if _af_num:
                    _af_model_value = _af_num.group()
            # Use the same value-distillation logic as the field-offer path so
            # a raw source_quote sentence never becomes the stored candidate.
            _af_candidate = _best_field_offer_value(_af_answer, _af_quote, _af_model_value)
            if _af_candidate and not _is_plausible_value(_af_field_type, _af_candidate, field_name=_af_fn, field_label=_af_fl):
                _af_candidate = None
            if _af_candidate:
                return jsonify({"type": "guided_suggest_value", "field_name": _af_fn, "field_label": _af_fl,
                                 "suggested_value": _af_candidate, "source": "found via document Q&A"})
            return jsonify({"type": "answer",
                             "answer": f"I couldn't find a good candidate for \"{_af_fl}\" via Q&A either "
                                       "- you'll need to type or paste the value yourself.",
                             "source_quote": "", "page_number": None})
        return jsonify({"type": "answer", "answer": "Something went wrong.", "source_quote": "", "page_number": None})

    # ?? Guided multi-turn: user provides the corrected value ?????????????????
    if guided_context.get("step") == "await_value":
        fn = (guided_context.get("field_name") or "").strip()
        fl = guided_context.get("field_label") or fn.replace("_", " ").title()

        # No `asked_for` here (unlike some other guided steps): a typed-in
        # value can legitimately be a long, sentence-shaped answer (a Subject
        # line, an address, pasted text in a script the user can't type by
        # hand) that the AI-judgment fallback tends to misread as "this looks
        # like a new question" rather than a literal value - the same
        # false-positive pattern already fixed once for alias text. The
        # explicit "apply this?" confirm a few steps below still guards
        # against a genuine topic change slipping through.
        if fn and _is_topic_change(question):
            # User changed topic — drop guided state, handle as fresh QA
            return jsonify(_answer_with_field_offer(question))
        if fn and question:
            val = question.strip()
            for pat in [
                r'^(?:it (?:should|must) be|the (?:correct )?value (?:is|should be)|should be|'
                r'sepatutnya|ia(?:lah)?|nilai(?:nya)? (?:adalah|ialah))\s+',
            ]:
                m = _re.match(pat, val, _re.IGNORECASE)
                if m:
                    val = val[m.end():].strip()
                    break
            # If the "value" is just another vague complaint, ask again
            _VAGUE = {
                'wrong','still wrong',"that's wrong","it's wrong","value is wrong",
                'not right',"that's not right","isn't right","not correct","that's not correct",
                'incorrect','still incorrect','still not right','still not correct',
                'salah','masih salah','tak betul','tidak betul','masih tidak betul',
                'still','no','nope','tidak','bukan',"that's not it","that's incorrect",
            }
            _val_lc = val.lower().rstrip('.,!?')
            _is_vague = (
                _val_lc in _VAGUE or
                _val_lc.startswith('still ') or
                _val_lc.startswith('masih ') or
                _val_lc.startswith('bukan ') or
                ('wrong' in _val_lc and len(val.split()) <= 4)
            )
            if _is_vague:
                return jsonify({
                    "type": "guided_start",
                    "field_name": fn,
                    "field_label": fl,
                    "re_ask": True,
                })
            _dup_fn, _dup_fl = _find_duplicate_value_field(fn, val)
            if _dup_fn:
                return jsonify({
                    "type": "guided_duplicate_field",
                    "field_name": fn, "field_label": fl, "value": val,
                    "existing_field_name": _dup_fn, "existing_field_label": _dup_fl,
                    "message": f"This looks like the same thing as \"{_dup_fl}\", which is already set to "
                               f"\"{val}\". Want me to merge these into one field instead of keeping both?",
                })
            rule_text = _generate_guided_rule(fn, fl, val)
            return jsonify({
                "type":            "guided_rule",
                "field_name":      fn,
                "field_label":     fl,
                "corrected_value": val,
                "rule_text":       rule_text,
                "field_id":        _lookup_field_id(doc.document_type, fn),
            })

    # -- Guided multi-turn: user decides whether to merge a detected duplicate
    # field into the existing one, or keep both (from _find_duplicate_value_field,
    # triggered either while filling in a value, or while removing a field). --
    if guided_context.get("step") == "confirm_merge_duplicate":
        _md_fn = (guided_context.get("field_name") or "").strip()
        _md_fl = guided_context.get("field_label") or _md_fn.replace("_", " ").title()
        _md_val = guided_context.get("value") or ""
        _md_exist_fn = (guided_context.get("existing_field_name") or "").strip()
        _md_exist_fl = guided_context.get("existing_field_label") or _md_exist_fn.replace("_", " ").title()
        if guided_context.get("merge") and _md_fn and _md_exist_fn:
            try:
                _md_fields = doc.get_fields()
            except Exception:
                _md_fields = {}
            if _md_fn in _md_fields:
                del _md_fields[_md_fn]
                doc.set_fields(_md_fields)
            _alias_saved = _add_alias_to_field(_md_exist_fn, _md_fl) is not None
            db.session.commit()
            _md_msg = f"Done! I merged \"{_md_fl}\" into \"{_md_exist_fl}\" instead of keeping a duplicate field."
            if _alias_saved:
                _md_msg += (f" I'll also recognize \"{_md_fl}\" as another name for \"{_md_exist_fl}\" "
                            "on future documents of this type.")
            return jsonify({"type": "guided_merge_done", "message": _md_msg,
                             "fields": _md_fields, "field_sources": doc.get_field_sources()})
        # User chose to keep them separate -- continue with the normal correction
        # flow exactly as if no duplicate had been detected.
        if _md_fn and _md_val:
            rule_text = _generate_guided_rule(_md_fn, _md_fl, _md_val)
            return jsonify({
                "type":            "guided_rule",
                "field_name":      _md_fn,
                "field_label":     _md_fl,
                "corrected_value": _md_val,
                "rule_text":       rule_text,
                "field_id":        _lookup_field_id(doc.document_type, _md_fn),
            })
        return jsonify({"type": "answer", "answer": "Something went wrong.", "source_quote": "", "page_number": None})

    # ?? Guided multi-turn: user confirms or refines the rule ?????????????????
    if guided_context.get("step") == "await_confirmation":
        fn  = guided_context.get("field_name", "")
        fl  = guided_context.get("field_label") or fn.replace("_", " ").title()
        val = guided_context.get("corrected_value", "")
        existing_rule = guided_context.get("rule_text", "")
        q_norm = question.lower().strip()
        # Topic-escape: cancel phrases
        _CANCEL_CONF = {"never mind","nevermind","cancel","no","nope","skip","ignore","takpe","tak nak"}
        if q_norm in _CANCEL_CONF:
            return jsonify({"type": "answer", "answer": "No problem! Let me know if there's anything else I can help with.", "source_quote": "", "page_number": None})
        # Topic-escape: if user asks a new question, drop guided state and fall through
        _QW_CONF = ("what ","who ","how ","when ","where ","which ","does ","can ","show ",
                    "apa ","siapa ","berapa ","bila ","bagaimana ","add ","remove ","delete ","tambah ","buang ")
        _is_new_question = any(q_norm.startswith(w) for w in _QW_CONF) or (q_norm.startswith("is ") and "?" in question) or "?" in question
        if not _is_new_question:
            _YES = {"yes","y","ok","okay","yeah","apply","good","looks good","correct",
                    "betul","boleh","setuju","proceed","confirmed","yep","sure","go ahead","apply it"}
            is_yes = q_norm in _YES or any(q_norm.startswith(w+" ") for w in _YES)
            if is_yes:
                import json as _json_conf
                just_this_doc = guided_context.get("just_this_doc", False)
                rule_error = None
                try:
                    ver_label, learned_alias = _apply_rule(fn, fl, val, "" if just_this_doc else existing_rule)
                except Exception as _e_conf:
                    print(f"[await_confirmation] _apply_rule failed (doc_id={doc.id}, field={fn}, "
                          f"rule={existing_rule!r}): {_e_conf}")
                    ver_label, learned_alias = None, None
                    rule_error = str(_e_conf)
                try:
                    updated_fields = _json_conf.loads(doc.fields or "{}") if doc.fields else {}
                except Exception:
                    updated_fields = None
                updated_sources = doc.get_field_sources()
                return jsonify({"type": "guided_done", "rule_text": existing_rule,
                                "success": rule_error is None, "error": rule_error,
                                "version_label": ver_label, "field_name": fn, "field_label": fl,
                                "learned_alias": learned_alias,
                                "fields": updated_fields, "field_sources": updated_sources})
            else:
                # Treat message as refinement feedback — regenerate
                rule_text = _generate_guided_rule(fn, fl, val, hint=question.strip())
                return jsonify({
                    "type":            "guided_rule",
                    "field_name":      fn,
                    "field_label":     fl,
                    "corrected_value": val,
                    "rule_text":       rule_text,
                    "field_id":        guided_context.get("field_id"),
                })

    # -- "Add something else" was clicked from a specific field's quick-action
    # panel (qaGuidedState carries field_name/field_label, but no fixed step -
    # unlike field_context_question, this doesn't force Q&A-only handling).
    # The classifier below has no idea which field a bare "it"/"that" refers
    # to unless told - prepending the field name explicitly (same phrasing
    # field_context_question already uses) fixes that for every intent this
    # falls through to, not just teach_alias. No early return: this is a
    # context hint, not its own guided step. --
    _fsf_fn, _fsf_fl = "", ""
    if guided_context.get("step") == "field_scoped_freeform":
        _fsf_fn = (guided_context.get("field_name") or "").strip()
        _fsf_fl = guided_context.get("field_label") or _fsf_fn.replace("_", " ").title()
        if _fsf_fn:
            question = f'Regarding the "{_fsf_fl}" field: {question}'

    # ?? Unified intent detection: extract / add / remove / correct / qa ?????????
    current_fields = {}
    try:
        current_fields = doc.get_fields()
    except Exception:
        pass

    try:
        chat_intent = detect_chat_intent(question, current_fields, doc.raw_text, history)
    except Exception as e:
        print(f"[ASK] Intent detection error, falling back to QA: {e}")
        chat_intent = {"intent": "qa"}

    if chat_intent["intent"] == "chitchat":
        return jsonify({
            "type": "answer",
            "answer": "I'm here to help with this document — ask me about any of the extracted "
                      "fields, or just say if something looks wrong and I'll fix it.",
            "source_quote": "", "page_number": None,
        })

    # -- "Only numbers" / "make this a date" - a rule about what the field may
    # hold, applied straight away (see _apply_field_type_change). When the user
    # got here via "Add something else", the clicked field is what "this"/"it"
    # means for certain, so it wins over the classifier's guess; from the plain
    # chat box the classifier's pick is only trusted if it names a real field,
    # since nothing is confirmed before the change is made. --
    if chat_intent["intent"] == "set_field_type":
        _sft_fn, _sft_fl = _fsf_fn, _fsf_fl
        if not _sft_fn:
            _sft_known = set(current_fields.keys())
            _sft_dt = DocumentType.query.filter_by(name=doc.document_type).first() if doc.document_type else None
            if _sft_dt:
                _sft_known |= {f.field_name for f in _sft_dt.fields}
            _sft_want = (chat_intent.get("field_name") or "").lower().replace(" ", "_").strip()
            _sft_fn = next((k for k in _sft_known if k.lower().replace(" ", "_").strip() == _sft_want), "") if _sft_want else ""
            _sft_fl = _field_label_for(_sft_fn) if _sft_fn else ""
        if not _sft_fn:
            return jsonify({"type": "answer",
                             "answer": "Which field should that apply to? Click the field in the panel and choose "
                                       "\"Add something else\", or tell me its name.",
                             "source_quote": "", "page_number": None})
        if not chat_intent.get("field_type"):
            return jsonify({"type": "answer",
                             "answer": f"What kind of value should \"{_sft_fl}\" hold - a number, a date, a currency "
                                       "amount, or plain text?",
                             "source_quote": "", "page_number": None})
        return _apply_field_type_change(_sft_fn, _sft_fl, chat_intent["field_type"])

    if chat_intent["intent"] == "teach_alias":
        _ta_fn = chat_intent.get("field_name") or ""
        _ta_alias = (chat_intent.get("new_alias") or "").strip()

        def _norm_key(s):
            return (s or "").lower().replace(" ", "_").strip()

        _ta_resolved = next((k for k in current_fields.keys() if _norm_key(k) == _norm_key(_ta_fn)), None)
        if not _ta_resolved:
            # Same shape as guided_disambig_correct (candidates + candidate_labels)
            # so the frontend can offer clickable field options instead of a wall
            # of raw field_name text - carries new_alias through so picking one
            # goes straight to the confirm step via teach_alias_pick_field below.
            _ta_candidates = list(current_fields.keys())[:12]
            _ta_clabels = [c.replace("_", " ").title() for c in _ta_candidates]
            return jsonify({"type": "guided_disambig_alias",
                             "message": f"Which field should I use \"{_ta_alias or 'that label'}\" for?",
                             "candidates": _ta_candidates,
                             "candidate_labels": _ta_clabels,
                             "new_alias": _ta_alias})
        _ta_fn = _ta_resolved
        _ta_fl = _ta_fn.replace("_", " ").title()
        if not _ta_alias:
            return jsonify({"type": "guided_teach_alias_ask", "field_name": _ta_fn, "field_label": _ta_fl,
                             "message": f"Sure — what label should I also look for when detecting \"{_ta_fl}\"?"})
        return jsonify({"type": "guided_teach_alias", "field_name": _ta_fn, "field_label": _ta_fl,
                         "new_alias": _ta_alias,
                         "message": f"Got it — I'll also look for \"{_ta_alias}\" when detecting \"{_ta_fl}\" "
                                     "for future documents of this type. Apply this?"})

    if chat_intent["intent"] == "correct":
        fn  = chat_intent.get("field_name") or ""
        fl  = chat_intent.get("field_label") or fn.replace("_", " ").title()
        val = chat_intent.get("corrected_value")
        # If the model pulled a value from the document (not the user's message),
        # discard it -- the user hasn't stated the correct value yet.
        if val and val.lower().strip() not in question.lower():
            val = None
        if fn and not val:
            # Disambiguation logic:
            # - Single generic words ("name","amount","date",...) → always ask which field
            # - Multi-word terms ("invoice reference") → score by word overlap; only disambig if tied
            _HIGH_AMBIG = {"name","amount","date","number","address","total","price","company","tax"}
            # Stop words to filter out when extracting content words from user message
            _STOP_CORR = {"the","a","an","is","are","was","be","been","being","have","has","had",
                          "do","does","did","not","no","nor","please","i","want","need","but","and",
                          "wrong","incorrect","right","correct","fix","update","change","edit",
                          "salah","betul","tidak","tolong","sepatutnya","it","my","your","this","that"}
            _q_lower = question.strip().lower()
            _q_words = set(_q_lower.replace("_", " ").split())
            _q_content = {w for w in _q_words if w not in _STOP_CORR and len(w) > 1}
            _fn_all = set(fn.lower().replace("_", " ").split())
            # Match fn_all words to user words with prefix/stem tolerance
            _fn_words = set()
            for _fw in _fn_all:
                for _qw in _q_words:
                    if _fw == _qw or _fw.startswith(_qw) or _qw.startswith(_fw):
                        _fn_words.add(_fw); break
            if not _fn_words:
                _fn_words = _fn_all
            # Detect classifier mismatch: user has content words not found in fn
            # (e.g. "customer number" but fn="customer_reference" — "number" unmatched)
            _unmatched = {cw for cw in _q_content
                          if not any(cw in fw or fw in cw or cw.startswith(fw) or fw.startswith(cw)
                                     for fw in _fn_all)}
            _classifier_mismatch = len(_unmatched) > 0 and len(_q_content) > 1
            # When mismatch detected, score candidates by user content words, not fn
            _score_words = _q_content if _classifier_mismatch else _fn_words
            _is_high_ambig = bool(_fn_words & _HIGH_AMBIG) and len(_fn_words) == 1

            def _overlap_score(candidate_key):
                kw = set(candidate_key.lower().replace("_", " ").split())
                score = 0
                for fw in _score_words:
                    if any(fw in w or w in fw for w in kw):
                        score += 1
                return score

            _scored = [(k, _overlap_score(k)) for k in current_fields.keys()]
            _scored = [(k, s) for k, s in _scored if s > 0]
            _scored.sort(key=lambda x: -x[1])

            if not _scored:
                return _guided_start_or_suggest(fn, fl)

            _max_score = _scored[0][1]
            _top = [k for k, s in _scored if s == _max_score]

            if len(_top) == 1 and _max_score == len(_score_words) and not _is_high_ambig and not _classifier_mismatch:
                # Unambiguous full match — correct that field directly
                _best = _top[0]
                _best_label = _best.replace("_", " ").title()
                return _guided_start_or_suggest(_best, _best_label)

            # Ambiguous or mismatched — show disambiguation with allow_other
            _candidates = [k for k, s in _scored] if _is_high_ambig else _top
            _clabels = [c.replace("_", " ").title() for c in _candidates]
            _msg = (f"Which \"{fl}\" field do you mean? I found {len(_candidates)} possible field(s):"
                    if len(_candidates) > 1 else
                    f"Is this the \"{_clabels[0]}\" you mean, or a different one?")
            return jsonify({
                "type": "guided_disambig_correct",
                "message": _msg,
                "candidates": _candidates,
                "candidate_labels": _clabels,
                "allow_other": _is_high_ambig or _classifier_mismatch,
            })
        rule_text = _generate_guided_rule(fn, fl, val or "", chat_intent.get("rule_hint") or "")
        return jsonify({
            "type":            "guided_rule",
            "field_name":      fn,
            "field_label":     fl,
            "corrected_value": val,
            "rule_text":       rule_text,
            "field_id":        _lookup_field_id(doc.document_type, fn),
        })

    # ?? "add" with no specific field name ? resolve a recent field question ?
    # A request such as "create me the field" commonly follows "what is the
    # hospital name?".  The intent model is told to use history for this, but a
    # missed reference used to discard that context and make the user type the
    # name again.  Recover the unambiguous, immediately preceding question here.
    # We still use _locate_new_field_or_ask, so a value is never saved without
    # the existing suggestion/confirmation flow.
    if chat_intent["intent"] == "add" and not chat_intent.get("fields_to_add"):
        import re as _re_add
        _add_ref = question.strip().lower()
        _implicit_add = any(phrase in _add_ref for phrase in (
            "create me the field", "create the field", "add that", "add this",
            "add it", "save that", "save this", "save it", "that field",
            "this field", "the field",
        ))
        _prior_questions = [
            (turn.get("q") or "").strip()
            for turn in reversed(history or [])
            if (turn.get("q") or "").strip()
        ]
        if _implicit_add and _prior_questions:
            _last_question = _prior_questions[0]
            _field_match = _re_add.search(
                r"^\s*(?:what(?:'s|\s+is)|which)\s+(?:is\s+)?(?:the\s+)?(.+?)\?*\s*$",
                _last_question,
                _re_add.IGNORECASE,
            )
            if _field_match:
                _raw_name = _field_match.group(1).strip(" .?!")
                _generic_names = {"this", "that", "it", "the value", "the answer"}
                _new_fn = _re_add.sub(r"[^a-z0-9]+", "_", _raw_name.lower()).strip("_")
                if _new_fn and _raw_name.lower() not in _generic_names:
                    return _locate_new_field_or_ask(_new_fn, _raw_name.replace("_", " ").title())
        return jsonify({"type": "guided_add", "message": "What field would you like to add?"})

    # ?? "remove" with no specific field name ? ask which field ???????????????
    if chat_intent["intent"] == "remove" and not chat_intent.get("fields_to_remove"):
        # "remove all fields" — confirm before wiping
        _ql_ra = question.strip().lower()
        _ALL_KW = ("all fields","all field","semua field","all data","clear all","delete all","remove all","buang semua","hapus semua")
        if any(k in _ql_ra for k in _ALL_KW) and current_fields:
            _n = len(current_fields)
            return jsonify({"type": "remove_all_confirm",
                            "message": f"Are you sure you want to remove ALL {_n} field(s)? This can't be undone.",
                            "count": _n})
        field_list = list(current_fields.keys()) if current_fields else []
        hint = ""
        if field_list:
            hint = " Current fields: " + ", ".join(field_list) + "."
        return jsonify({"type": "guided_remove", "message": "Which field would you like to remove?" + hint,
                        "fields": field_list})

    if chat_intent["intent"] == "extract":
            count_hint = chat_intent.get("count_hint")
            _extract_field_hints = chat_intent.get("field_hints") or []
            new_fields = extract_fields_on_demand(
                doc.raw_text,
                _extract_field_hints,
                count_hint,
                doc.document_type or "",
            )
            if new_fields:
                if count_hint and len(new_fields) > count_hint:
                    new_fields = dict(list(new_fields.items())[:count_hint])
                # A "targeted" request (user named specific field(s)) only ever
                # touches those field(s) - the blast radius is that named subset,
                # not the whole document, so "replace" must not wipe unrelated
                # saved fields. An untargeted request ("extract all fields")
                # really does re-derive the full field set, so replace there
                # means a genuine full swap. Either way this always needs an
                # explicit confirm before writing, same as every other guided
                # flow that can overwrite saved data.
                return jsonify({
                    "type":       "guided_confirm_extract",
                    "new_fields": new_fields,
                    "count":      len(new_fields),
                    "had_existing_fields": bool(current_fields),
                    "targeted":   bool(_extract_field_hints),
                })
            else:
                result = answer_question(doc.raw_text, question, history=history)
                result["type"] = "answer"
                result["answer"] = (
                    "I couldn't extract any fields from this document. "
                    "Try specifying which fields you want, e.g. \"extract full name, NRIC, date of birth\"."
                )
                return jsonify(result)

    elif chat_intent["intent"] == "add" and chat_intent.get("fields_to_add"):
            # --- Currency conversion check ---
            import re as _re3a
            _CURRA = {
                'myr':'MYR','ringgit':'MYR','usd':'USD','dollar':'USD',
                'eur':'EUR','euro':'EUR','gbp':'GBP','pound':'GBP',
                'sgd':'SGD','jpy':'JPY','yen':'JPY','cny':'CNY','yuan':'CNY','rmb':'CNY',
                'aud':'AUD','cad':'CAD','chf':'CHF','thb':'THB','idr':'IDR','php':'PHP','inr':'INR',
            }
            _qlca = question.strip().lower()
            _tcura = None
            for _kwa, _codea in _CURRA.items():
                if _kwa in _qlca:
                    _tcura = _codea; break
            if _tcura:
                _ACTA = r'\b(add|tambah|tolong|please|baru|new|field|create|include|in|to|as|dalam|untuk|ke|sebagai)\b'
                _basea = _re3a.sub(r'\b(' + '|'.join(_CURRA.keys()) + r')\b', '', _qlca)
                _basea = _re3a.sub(_ACTA, '', _basea)
                _basea = _re3a.sub(r'\s+', ' ', _basea).strip()
                _bfa = extract_fields_on_demand(
                    doc.raw_text, [_basea] if _basea else list(chat_intent["fields_to_add"]),
                    None, doc.document_type or ""
                )
                _rvala = next(iter(_bfa.values()), "") if _bfa else ""
                _nma = _re3a.search(r'[\d,]+\.?\d*', _rvala)
                _amta = float(_nma.group().replace(',', '')) if _nma else None
                _scura = None
                for _ca in ('USD','EUR','GBP','SGD','MYR','JPY','CNY','AUD','CAD','CHF'):
                    if _re3a.search(r'\b' + _ca + r'\b', _rvala.upper()):
                        _scura = _ca; break
                if not _scura:
                    for _sym, _ca in [('S$','SGD'),('A$','AUD'),('C$','CAD'),
                                       ('$','USD'),('\u20ac','EUR'),('\u00a3','GBP'),('\u00a5','JPY'),('RM','MYR')]:
                        if _sym in _rvala:
                            _scura = _ca; break
                if not _scura:
                    import re as _rxca
                    _rt = doc.raw_text or ""
                    if _rxca.search(r'S\$', _rt): _scura = 'SGD'
                    elif _rxca.search(r'A\$', _rt): _scura = 'AUD'
                    elif _rxca.search(r'C\$', _rt): _scura = 'CAD'
                    elif '$' in _rt: _scura = 'USD'
                    elif '\u20ac' in _rt: _scura = 'EUR'
                    elif '\u00a3' in _rt: _scura = 'GBP'
                    elif '\u00a5' in _rt: _scura = 'JPY'
                    else:
                        for _cc in ('USD','EUR','GBP','SGD','MYR','JPY','CNY','AUD','CAD','CHF'):
                            if _rxca.search(r'\b' + _cc + r'\b', _rt):
                                _scura = _cc; break
                _vala = _rvala
                # Only True when the saved value is actually known to be in
                # the target currency -- either converted via a live rate, or
                # already detected as being in that currency to begin with.
                # Everything else (no amount found, no source currency
                # detected, rate lookup failed/timed out) leaves _vala as the
                # raw unconverted number, so the field must NOT be labelled
                # as if it were converted.
                _converted_ok = False
                _conv_reason  = None
                if _amta is None:
                    _conv_reason = "Could not detect a numeric amount to convert."
                elif not _scura:
                    _conv_reason = "Could not detect the source currency."
                elif _scura == _tcura:
                    _converted_ok = True  # already in the requested currency
                else:
                    _rate, _rate_source = _fetch_live_fx_rate(_scura, _tcura)
                    if _rate:
                        _vala = f"{_tcura} {round(_amta * _rate, 2):,.2f}"
                        _converted_ok = True
                    else:
                        _conv_reason = f"No exchange rate available for {_scura}->{_tcura}."
                if _vala:
                    _bkeya = _re3a.sub(r'[^a-z0-9]+', '_', _basea).strip('_')
                    if _converted_ok:
                        _keya = (f"{_bkeya}_in_{_tcura.lower()}" if _bkeya
                                 else _re3a.sub(r'[^a-z0-9]+', '_', _qlca).strip('_'))
                    else:
                        # No real conversion happened -- persist under the
                        # original (unconverted) key, not a "_in_<currency>"
                        # key that would misrepresent this as converted.
                        _keya = _bkeya if _bkeya else _re3a.sub(r'[^a-z0-9]+', '_', _qlca).strip('_')
                    try:
                        _exa = doc.get_fields()
                    except Exception:
                        _exa = {}
                    _mergeda = {**_exa, _keya: _vala}
                    doc.set_fields(_mergeda)
                    import sys as _sysa
                    from extensions import db as _dba
                    _dba.session.commit()
                    print(f"[ASK] Currency conversion: {_keya}={_vala} (converted={_converted_ok}"
                          + (f", reason={_conv_reason}" if _conv_reason else "") + ")", file=_sysa.stderr, flush=True)
                    resp = {"type": "extraction", "fields": _mergeda,
                            "new_fields": {_keya: _vala}, "new_count": 1,
                            "suggest_future": bool(doc.document_type), "doc_type": doc.document_type or ""}
                    if not _converted_ok:
                        resp["converted"] = False
                        resp["reason"] = _conv_reason
                    return jsonify(resp)
            # --- End currency check, fall through to normal add ---
            # Single named field: run the same multi-tier search + confirm-before-
            # write flow used for field corrections, instead of writing an AI
            # guess straight to the document.
            if len(chat_intent["fields_to_add"]) == 1:
                _raw_name = chat_intent["fields_to_add"][0].strip()
                _new_fl = _raw_name.replace("_", " ").title()
                _new_fn = _re3a.sub(r'[^a-z0-9]+', '_', _raw_name.lower()).strip('_')
                if _new_fn:
                    return _locate_new_field_or_ask(_new_fn, _new_fl)
            new_fields = extract_fields_on_demand(
                doc.raw_text,
                chat_intent["fields_to_add"],
                None,
                doc.document_type or "",
            )
            if new_fields:
                try:
                    existing = doc.get_fields()
                except Exception:
                    existing = {}
                merged = {**existing, **new_fields}
                doc.set_fields(merged)
                db.session.commit()
                print(f"[ASK] Added {len(new_fields)} field(s) for doc {doc.id}: {list(new_fields.keys())}")
                return jsonify({
                    "type":       "extraction",
                    "fields":     merged,
                    "new_fields": new_fields,
                    "new_count":  len(new_fields),
                    "suggest_future": bool(doc.document_type),
                    "doc_type":   doc.document_type or "",
                })
            else:
                result = answer_question(doc.raw_text, question, history=history)
                result["type"] = "answer"
                result["answer"] = (
                    f"I couldn't find a value for "
                    f"{', '.join(chat_intent['fields_to_add'])} in this document. "
                    "Check the Raw Text tab to confirm the field exists."
                )
                return jsonify(result)

    elif chat_intent["intent"] == "remove" and chat_intent.get("fields_to_remove"):
            try:
                existing = doc.get_fields()
            except Exception:
                existing = {}
            # Detect "remove all" via classifier returning ["all"] or similar
            _ql_ra2 = question.strip().lower()
            _ALL_KW2 = ("all fields","all field","semua field","all data","clear all","delete all","remove all","buang semua","hapus semua")
            _fn_ra = (chat_intent["fields_to_remove"][0] or "").lower().strip()
            if (any(k in _ql_ra2 for k in _ALL_KW2) or _fn_ra in ("all","all fields","all field")) and existing:
                _n2 = len(existing)
                return jsonify({"type": "remove_all_confirm",
                                "message": f"Are you sure you want to remove ALL {_n2} field(s)? This can't be undone.",
                                "count": _n2})
            # If user said "X in [currency]" but classifier stripped the currency keyword,
            # redirect to guided flow so the user can clarify which field they mean
            _CUR_R = {'myr','usd','eur','gbp','sgd','jpy','cny','aud','cad','chf',
                      'thb','idr','php','inr','ringgit','dollar','euro'}
            _ql_r = question.strip().lower()
            _q_words_r = set(_ql_r.split())
            _has_cur_r = bool(_q_words_r & _CUR_R)
            if _has_cur_r:
                _fn_r = (chat_intent["fields_to_remove"][0] or "").lower()
                _fn_words_r = set(_fn_r.replace("_", " ").split())
                if not (_fn_words_r & _CUR_R):  # currency was stripped by classifier
                    # Reconstruct: try "fn_in_currency" (e.g. subtotal -> subtotal_in_sgd)
                    _cur_kw = next(iter(_q_words_r & _CUR_R), None)
                    _recon = None
                    if _cur_kw:
                        _recon_key = _fn_r.replace(" ", "_") + "_in_" + _cur_kw
                        for _ek in existing.keys():
                            if _ek.lower().replace(" ", "_") == _recon_key:
                                _recon = _ek; break
                    if _recon:
                        # Override with the correct field name and fall through
                        chat_intent["fields_to_remove"] = [_recon]
                    else:
                        _flist_r = ", ".join(list(existing.keys())[:12])
                        return jsonify({"type": "answer",
                                        "answer": f"I couldn't find a field called that. "
                                                  f"Your current fields are: {_flist_r}.",
                                        "source_quote": "", "page_number": None})
            removed = []
            for fn in chat_intent["fields_to_remove"]:
                if fn in existing:
                    del existing[fn]
                    removed.append(fn)
                else:
                    fn_norm = fn.lower().replace(" ", "_")
                    for key in list(existing.keys()):
                        if key.lower().replace(" ", "_") == fn_norm:
                            del existing[key]
                            removed.append(key)
                            break
            if removed:
                doc.set_fields(existing)
                db.session.commit()
                print(f"[ASK] Removed field(s) {removed} from doc {doc.id}")
                return jsonify({
                    "type":    "remove",
                    "fields":  existing,
                    "removed": removed,
                })
            else:
                result = answer_question(doc.raw_text, question, history=history)
                result["type"] = "answer"
                result["answer"] = (
                    f"I couldn't find field(s) named '{', '.join(chat_intent['fields_to_remove'])}' "
                    "in this document's fields. Check the fields panel for the exact field names."
                )
                return jsonify(result)

    # -- Default: answer the question --------------------------------------
    return jsonify(_answer_with_field_offer(question))


# ?????? PATCH /api/documents/<id>/fields ???????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

@document_bp.route("/<int:doc_id>/fields", methods=["PATCH"])
def patch_document_field(doc_id):
    """Overwrite a single extracted field value on this document."""
    doc = Document.query.get_or_404(doc_id)
    data = request.get_json() or {}
    field_name = (data.get("field_name") or "").strip()
    if not field_name:
        return jsonify({"error": "field_name required"}), 400
    try:
        fields = doc.get_fields()
        fields[field_name] = data.get("value")
        doc.set_fields(fields)
        # An explicit value for this field resolves any pending "which value
        # did you mean" ambiguity for it - stale otherwise, since the field
        # no longer holds whichever candidate the badge was comparing against.
        _ambiguities = doc.get_field_ambiguities()
        if field_name in _ambiguities:
            del _ambiguities[field_name]
            doc.set_field_ambiguities(_ambiguities)
        db.session.commit()
        return jsonify({"ok": True, "fields": fields, "field_ambiguities": doc.get_field_ambiguities()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ?????? Chat history (Ask-tab transcript) ??????????????????????????????????

@document_bp.route("/<int:doc_id>/chat", methods=["GET"])
def get_chat_history(doc_id):
    """Full Ask-tab transcript for this document, oldest first."""
    Document.query.get_or_404(doc_id)
    msgs = (ChatMessage.query
            .filter_by(document_id=doc_id)
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
            .limit(500)
            .all())
    return jsonify({"messages": [m.to_dict() for m in msgs]})


@document_bp.route("/<int:doc_id>/chat", methods=["POST"])
def add_chat_message(doc_id):
    """Append one turn (user question or bot reply) to this document's transcript."""
    Document.query.get_or_404(doc_id)
    data = request.get_json() or {}
    role = (data.get("role") or "").strip()
    content = (data.get("content") or "").strip()
    if role not in ("user", "bot"):
        return jsonify({"error": "role must be 'user' or 'bot'"}), 400
    if not content:
        return jsonify({"error": "content required"}), 400
    msg = ChatMessage(document_id=doc_id, role=role, content=content)
    db.session.add(msg)
    db.session.commit()
    return jsonify({"ok": True, "message": msg.to_dict()})


@document_bp.route("/<int:doc_id>/chat", methods=["DELETE"])
def clear_chat_history(doc_id):
    """Clear this document's saved transcript (starts fresh next time it's opened)."""
    Document.query.get_or_404(doc_id)
    ChatMessage.query.filter_by(document_id=doc_id).delete()
    db.session.commit()
    return jsonify({"ok": True})


# ?? DELETE /api/documents/<id>/fields/<field_name> ????????????????????????????

@document_bp.route("/<int:doc_id>/fields/<field_name>", methods=["DELETE"])
def delete_document_field(doc_id, field_name):
    """Delete a single extracted field from this document."""
    doc = Document.query.get_or_404(doc_id)
    try:
        fields = doc.get_fields()
        if field_name not in fields:
            return jsonify({"error": f"Field '{field_name}' not found"}), 404
        del fields[field_name]
        doc.set_fields(fields)
        db.session.commit()
        return jsonify({"ok": True, "fields": fields, "removed": field_name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ?????? POST /api/documents/<id>/re-extract ???????????????????????????????????????????????????????????????????????????????????????????????????????????

@document_bp.route("/<int:doc_id>/re-extract", methods=["POST"])
def re_extract_fields(doc_id):
    """Re-extract all (or N) fields from the document's stored text and save them."""
    doc = Document.query.get_or_404(doc_id)
    if not doc.raw_text:
        return jsonify({"error": "No text available for this document"}), 400
    data = request.get_json() or {}
    count = data.get("count") or None
    if count:
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = None

    from qwen_service import extract_fields_on_demand
    new_fields = extract_fields_on_demand(
        doc.raw_text,
        [],
        count,
        doc.document_type or "",
    )
    if not new_fields:
        return jsonify({"error": "Could not extract any fields from this document."}), 422

    # Enforce count: trim to requested number (LLM treats count as a hint, not a limit)
    if count and len(new_fields) > count:
        new_fields = dict(list(new_fields.items())[:count])

    # Replace ? not merge ? so stale/blob fields from the original extraction are cleared
    doc.set_fields(new_fields)
    db.session.commit()
    print(f"[ReExtract] Extracted {len(new_fields)} fields for doc {doc_id}")
    return jsonify({
        "type":       "extraction",
        "fields":     new_fields,
        "new_fields": new_fields,
        "new_count":  len(new_fields),
    })


# ?????? POST /api/documents/<id>/fix-extraction-rule ???????????????????????????????????????????????????????????????????????????????????????

@document_bp.route("/<int:doc_id>/fix-extraction-rule", methods=["POST"])
def fix_extraction_rule(doc_id):
    """
    Generate a targeted extraction rule from a field correction and append it
    to the document type's active prompt version (structure_hint).
    """
    doc = Document.query.get_or_404(doc_id)
    data = request.get_json() or {}
    field_name     = (data.get("field_name") or "").strip()
    field_label    = (data.get("field_label") or field_name).strip()
    corrected_value = (data.get("corrected_value") or "").strip()
    rule_hint      = (data.get("rule_hint") or "").strip()
    if not field_name or not corrected_value:
        return jsonify({"error": "field_name and corrected_value required"}), 400
    if not doc.document_type:
        return jsonify({"error": "Document has no type assigned"}), 400

    try:
        from qwen_service import generate_field_extraction_rule
        rule = generate_field_extraction_rule(
            field_name, field_label, corrected_value,
            doc.document_type, doc.raw_text or "", rule_hint
        )

        # Load the current active prompt text and append the new rule
        from prompt_studio_service import get_active_prompt_version, create_prompt_version, activate_prompt_version, ensure_prompt_version_seed
        from document_model import DocumentType

        dt = DocumentType.query.filter_by(name=doc.document_type).first()
        if not dt:
            return jsonify({"error": f"Document type '{doc.document_type}' not found"}), 404

        active = get_active_prompt_version(doc.document_type)
        current_text = (active.prompt_text or "").strip() if active else ""

        # Append the new rule under a clear heading so it's visible in Prompt Studio
        separator = "\n\n" if current_text else ""
        new_text = current_text + separator + f"# Field correction rule ({field_label})\n{rule}"

        new_version = create_prompt_version(
            doc.document_type,
            prompt_text=new_text,
            change_summary=f"Auto-correction: {field_label} ??? {corrected_value}",
            created_by="ask-correction",
            bump="minor",
        )
        activate_prompt_version(new_version.id)

        return jsonify({
            "ok":   True,
            "rule": rule,
            "version_label": new_version.version_label,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ?????? POST /api/documents/<id>/reextract ?????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

@document_bp.route("/<int:doc_id>/reextract", methods=["POST"])
def reextract_document(doc_id):
    """Re-run field extraction on an already-processed document using current doc type settings."""
    doc = Document.query.get_or_404(doc_id)
    if not doc.raw_text:
        return jsonify({"error": "No raw text available ??? cannot re-extract"}), 400
    try:
        from qwen_service import extract_fields
        new_fields = extract_fields(doc.raw_text, doc.document_type or "other")
        doc.set_fields(new_fields)
        db.session.commit()
        return jsonify({"ok": True, "fields": new_fields})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ?????? Export filename convention for the Out-patient Prescription / Drug List family ??????????????????????????????????????????
# Scoped to exactly these 2 doc types per the 2026-09-10 finalized spec --
# every other doc type keeps today's vaultmind_{filename}.pdf naming and has
# no XML export at all.
_MRN_EXPORT_FIELD_BY_DOC_TYPE = {
    "out-patient prescription": "mrn_barcode",
    "out-patient drug list": "mrn_barcode_barcode",
}


def _mrn_export_basename(doc) -> str | None:
    """Returns "{doctype_slug}_{mrn_barcode}" for the 2 doc types above, or
    None for every other doc type (falls back to today's naming)."""
    from xml_export_service import doctype_slug
    mrn_field = _MRN_EXPORT_FIELD_BY_DOC_TYPE.get((doc.document_type or "").strip().lower())
    if not mrn_field:
        return None
    mrn_value = doc.get_fields().get(mrn_field)
    if not mrn_value:
        return None
    return f"{doctype_slug(doc.document_type)}_{mrn_value}"


# ?????? GET /api/documents/<id>/export ?????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

@document_bp.route("/<int:doc_id>/export", methods=["GET"])
def export_document_pdf(doc_id):
    from pdf_export_service import generate_export_pdf
    from flask import Response as FlaskResponse
    doc      = Document.query.get_or_404(doc_id)
    doc_dict = doc.to_dict()
    try:
        pdf_bytes    = generate_export_pdf(doc_dict)
        disposition  = request.args.get("download", "0")
        cd           = "attachment" if disposition == "1" else "inline"
        mrn_basename = _mrn_export_basename(doc)
        out_filename = f"{mrn_basename}.pdf" if mrn_basename else f"vaultmind_{doc.filename}.pdf"
        return FlaskResponse(
            pdf_bytes,
            mimetype="application/pdf",
            headers={
                "Content-Disposition": f'{cd}; filename="{out_filename}"',
                "Content-Length":      str(len(pdf_bytes)),
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ?????? GET /api/documents/<id>/export-xml ?????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

@document_bp.route("/<int:doc_id>/export-xml", methods=["GET"])
def export_document_xml(doc_id):
    from xml_export_service import generate_export_xml, doctype_slug
    from flask import Response as FlaskResponse
    doc      = Document.query.get_or_404(doc_id)
    doc_dict = doc.to_dict()
    try:
        xml_bytes    = generate_export_xml(doc_dict)
        disposition  = request.args.get("download", "0")
        cd           = "attachment" if disposition == "1" else "inline"
        mrn_basename = _mrn_export_basename(doc)
        out_filename = f"{mrn_basename}_DATA.xml" if mrn_basename else f"{doctype_slug(doc.document_type)}_DATA.xml"
        return FlaskResponse(
            xml_bytes,
            mimetype="application/xml",
            headers={
                "Content-Disposition": f'{cd}; filename="{out_filename}"',
                "Content-Length":      str(len(xml_bytes)),
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ?????? GET /api/documents/<id>/searchable ?????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

@document_bp.route("/<int:doc_id>/searchable", methods=["GET"])
def export_searchable_pdf(doc_id):
    import hashlib as _hashlib
    from pdf_searchable_service import generate_searchable_pdf
    from flask import Response as FlaskResponse
    doc      = Document.query.get_or_404(doc_id)
    doc_dict = doc.to_dict()
    try:
        pdf_bytes   = generate_searchable_pdf(doc_dict, UPLOAD_FOLDER)
        disposition = request.args.get("download", "0")
        cd          = "attachment" if disposition == "1" else "inline"
        # ETag from a short content hash so the browser can validate its cache
        # and skip the (potentially slow) OCR regeneration on repeat opens.
        etag = _hashlib.md5(pdf_bytes).hexdigest()[:16]
        if request.headers.get("If-None-Match") == etag:
            return FlaskResponse(status=304)
        resp = FlaskResponse(
            pdf_bytes,
            mimetype="application/pdf",
            headers={
                "Content-Disposition": f'{cd}; filename="searchable_{doc.filename}.pdf"',
                "Content-Length":      str(len(pdf_bytes)),
                "ETag":                etag,
                "Cache-Control":       "private, max-age=86400",
            }
        )
        return resp
    except BaseException as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ?????? GET /api/documents/<id>/highlighted ??????????????????????????????????????????????????????????????????????????????????????????????????????????????????

@document_bp.route("/<int:doc_id>/highlighted", methods=["GET"])
def export_highlighted_pdf(doc_id):
    from pdf_highlighted_service import generate_highlighted_pdf
    from flask import Response as FlaskResponse
    doc      = Document.query.get_or_404(doc_id)
    doc_dict = doc.to_dict()
    try:
        pdf_bytes   = generate_highlighted_pdf(doc_dict, UPLOAD_FOLDER)
        disposition = request.args.get("download", "0")
        cd          = "attachment" if disposition == "1" else "inline"
        return FlaskResponse(
            pdf_bytes,
            mimetype="application/pdf",
            headers={
                "Content-Disposition": f'{cd}; filename="highlighted_{doc.filename}.pdf"',
                "Content-Length":      str(len(pdf_bytes)),
            }
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ?????? Export Settings & Features ??????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

@document_bp.route("/export-settings", methods=["GET"])
def get_export_settings():
    """Get the current export folder configuration."""
    from export_service import get_export_folder
    return jsonify({"export_folder": get_export_folder()})


@document_bp.route("/export-settings", methods=["POST"])
def update_export_settings():
    """
    Update export folder configuration with validation.

    Request body:
        {"export_folder": "/path/to/folder"}

    Returns:
        {"success": true/false, "error": "..."}
    """
    from export_service import set_export_folder
    import os
    import getpass

    data = request.get_json() or {}
    path = data.get("export_folder", "").strip()

    # Detailed logging for debugging
    print("\n" + "="*60)
    print("[Export Settings] Validation Request")
    print("="*60)
    print(f"1. Raw JSON received: {data}")
    print(f"2. Raw path from JSON: {repr(data.get('export_folder', ''))}")
    print(f"3. After strip(): {repr(path)}")
    print(f"4. Length: {len(path)}")
    print(f"5. Flask process running as user: {getpass.getuser()}")
    print(f"6. Current working directory: {os.getcwd()}")

    if path:
        print(f"7. os.path.exists(path): {os.path.exists(path)}")
        print(f"8. os.path.isdir(path): {os.path.isdir(path)}")
        print(f"9. os.path.abspath(path): {os.path.abspath(path)}")
        try:
            print(f"10. os.listdir(path): {os.listdir(path)[:5] if os.path.exists(path) else 'N/A'}")
        except Exception as e:
            print(f"10. os.listdir(path) FAILED: {e}")
        try:
            print(f"11. os.access(path, os.W_OK): {os.access(path, os.W_OK)}")
            print(f"12. os.access(path, os.R_OK): {os.access(path, os.R_OK)}")
        except Exception as e:
            print(f"11-12. os.access() FAILED: {e}")
    print("="*60 + "\n")

    if not path:
        return jsonify({"success": False, "error": "Export folder path is required"}), 400

    result = set_export_folder(path)
    if result["success"]:
        return jsonify(result)
    else:
        return jsonify(result), 400


@document_bp.route("/<int:doc_id>/export-to-x2", methods=["POST"])
def export_document_to_x2(doc_id):
    """
    Export document (searchable PDF + JSON) to configured X2 folder.

    Returns:
        {
            "success": true/false,
            "pdf_filename": "...",
            "json_filename": "...",
            "export_folder": "...",
            "error": "..." (if failed)
        }
    """
    from pdf_searchable_service import generate_searchable_pdf
    from export_service import export_to_folder

    doc = Document.query.get_or_404(doc_id)
    doc_dict = doc.to_dict()

    try:
        # Generate searchable PDF
        pdf_bytes = generate_searchable_pdf(doc_dict, UPLOAD_FOLDER)

        # Export to configured folder
        result = export_to_folder(doc_dict, pdf_bytes)

        if result["success"]:
            return jsonify({
                "success": True,
                "pdf_path": result.get("pdf_path"),
                "json_path": result.get("json_path"),
                "pdf_filename": result["pdf_filename"],
                "json_filename": result["json_filename"],
            })
        else:
            return jsonify(result), 500

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@document_bp.route("/<int:doc_id>/export-download", methods=["GET"])
def export_document_download(doc_id):
    """
    Download document export as ZIP (searchable PDF + JSON).

    Returns:
        ZIP file containing {DocType}_{MRN}_{Date}.pdf and .json
    """
    from flask import Response as FlaskResponse
    from pdf_searchable_service import generate_searchable_pdf
    from export_service import create_download_zip

    doc = Document.query.get_or_404(doc_id)
    doc_dict = doc.to_dict()

    try:
        # Generate searchable PDF
        pdf_bytes = generate_searchable_pdf(doc_dict, UPLOAD_FOLDER)

        # Create ZIP
        zip_bytes, zip_filename = create_download_zip(doc_dict, pdf_bytes)

        return FlaskResponse(
            zip_bytes,
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{zip_filename}"',
                "Content-Length": str(len(zip_bytes)),
            }
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ?????? GET /api/documents/rag/stats ???????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

@document_bp.route("/rag/stats", methods=["GET"])
def rag_stats():
    try:
        from rag_service import get_stats
        return jsonify(get_stats())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@document_bp.route("/<int:doc_id>/translate-labels", methods=["POST"])
def translate_labels(doc_id):
    """
    Translate a document's field label keys between English and the document's
    original language (auto-detected from raw_text).

    Request body:
      { "target_lang": "english" }   ??? translate non-English labels ??? English
      { "target_lang": "thai" }      ??? translate English labels ??? Thai (or any language)

    Response:
      {
        "translations": { "original label": "translated label", ... },
        "source_lang":  "thai",
        "target_lang":  "english"
      }
    """
    from qwen_service import translate_field_labels, detect_language

    doc  = Document.query.get_or_404(doc_id)
    data = request.get_json() or {}
    target_lang = (data.get("target_lang") or "english").strip().lower()

    fields = doc.get_fields() or {}
    if not fields:
        return jsonify({"translations": {}, "value_translations": {}, "source_lang": "english", "target_lang": target_lang}), 200

    # Collect label strings (field keys + table column keys)
    label_set = set()
    # Collect string values that contain non-ASCII characters (Thai, Korean, etc.)
    value_set = set()

    def _has_foreign(s):
        return isinstance(s, str) and any(ord(c) > 127 for c in s)

    for key, val in fields.items():
        label_set.add(key)
        if isinstance(val, str) and val.strip() and _has_foreign(val):
            value_set.add(val)
        elif isinstance(val, list):
            for row in val:
                if isinstance(row, dict):
                    label_set.update(row.keys())
                    for cell_val in row.values():
                        if isinstance(cell_val, str) and cell_val.strip() and _has_foreign(cell_val):
                            value_set.add(cell_val)

    # Detect the language of the raw text so we know which direction to translate
    source_lang = detect_language(doc.raw_text or "") if doc.raw_text else "english"

    try:
        translations       = translate_field_labels(list(label_set), source_lang, target_lang)
        value_translations = translate_field_labels(list(value_set), source_lang, target_lang) if value_set else {}
        return jsonify({
            "translations":       translations,
            "value_translations": value_translations,
            "source_lang":        source_lang,
            "target_lang":        target_lang,
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@document_bp.route("/logs", methods=["GET"])
def get_logs():
    """Return the in-memory pipeline log buffer (last 500 lines)."""
    from qwen_service import get_log_buffer
    lines = get_log_buffer()
    n = request.args.get("n", 100, type=int)
    return jsonify({"logs": lines[-n:], "total": len(lines)}), 200


# ?????? iVault recognize job store (mirrors the main jobs store) ???????????????????????????????????????????????????
ivault_jobs      = {}
ivault_jobs_lock = threading.Lock()
IVAULT_JOB_TTL_SECONDS = 20 * 60  # safety net for clients that abandon polling before a terminal SSE event


def _sweep_expired_ivault_jobs():
    """Call with ivault_jobs_lock already held. ivault_progress()'s generate()
    already deletes a job's entry right after its terminal SSE event is sent -
    this is only a backstop for jobs whose client abandoned polling (or never
    started), which would otherwise leak for the process lifetime."""
    now = time.time()
    expired = [
        jid for jid, j in ivault_jobs.items()
        if (now - j.get("created_at", now)) > IVAULT_JOB_TTL_SECONDS
    ]
    for jid in expired:
        del ivault_jobs[jid]


def _update_ivault_step(job_id: str, label: str, frac: float):
    """Progress callback passed to qwen_service so Vision streaming updates SSE."""
    with ivault_jobs_lock:
        if job_id in ivault_jobs:
            ivault_jobs[job_id]["step"]     = label
            ivault_jobs[job_id]["step_num"] = frac


def _run_ivault_recognize(job_id: str, file_path: str, properties: list):
    """
    Background thread ??? mirrors process_file_task() for AI Doc Processing.

    Pipeline (same steps as AI Doc Processing):
      Step 1 ??? Image prep  : PIL resize + base64 encode (inside recognize_for_ivault)
      Step 2 ??? Vision call : Qwen reads image + extracts property values in ONE step
      Step 3 ??? Stream done : SSE event pushed with {propertyId: value} result
    """
    try:
        with ivault_jobs_lock:
            ivault_jobs[job_id]["status"]   = "processing"
            ivault_jobs[job_id]["step"]     = "Preparing image..."
            ivault_jobs[job_id]["step_num"] = 0.05

        _init_lm_progress(lambda label, frac: _update_ivault_step(job_id, label, frac))

        from qwen_service import recognize_for_ivault
        values = recognize_for_ivault(file_path, properties)

        # recognize_for_ivault() marks a partial result (OCR succeeded, final
        # extraction failed) with reserved non-UUID keys rather than raising -
        # log it distinctly from total failure, and don't ship the raw OCR
        # text blob to the Android client, just the null property values.
        partial_failure = values.pop("_extractionFailed", False)
        extraction_error = values.pop("_extractionError", None)
        values.pop("_ocrText", None)
        if partial_failure:
            print(f"[iVault Recognize] job {job_id}: partial result (OCR ok, extraction failed): {extraction_error}")

        with ivault_jobs_lock:
            ivault_jobs[job_id]["status"]   = "done"
            ivault_jobs[job_id]["values"]   = values
            ivault_jobs[job_id]["partial"]  = partial_failure
            ivault_jobs[job_id]["step"]     = "Done"
            ivault_jobs[job_id]["step_num"] = 1.0

    except Exception as e:
        import traceback; traceback.print_exc()
        with ivault_jobs_lock:
            ivault_jobs[job_id]["status"] = "error"
            ivault_jobs[job_id]["error"]  = str(e)
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


# ?????? POST /api/documents/ivault/recognize ???????????????????????????????????????????????????????????????????????????????????????????????????????????????

@document_bp.route("/ivault/recognize", methods=["POST"])
def ivault_recognize():
    """
    iVault Recognize ??? Step 1: receive file + properties, queue job, return job_id.

    Mirrors /upload: returns job_id immediately so Android can open the SSE stream.

    Accepts multipart/form-data:
      file        ??? scanned image or PDF from scanner
      properties  ??? JSON array of classPropertyModel objects from GET /api/Class/{classId}
                    [{propertyId, propertyName, propertyType, isRequired, defaultValue}, ...]
    """
    file            = request.files.get("file")
    properties_json = request.form.get("properties", "[]")

    if not file or file.filename == "":
        return jsonify({"error": "No file provided"}), 400

    try:
        properties = json.loads(properties_json)
    except (json.JSONDecodeError, ValueError):
        return jsonify({"error": "Invalid properties JSON"}), 400

    if not isinstance(properties, list) or not properties:
        return jsonify({"error": "properties must be a non-empty array"}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": "Unsupported file type"}), 400

    original_filename = secure_filename(file.filename)
    ext               = original_filename.rsplit(".", 1)[1].lower()
    unique_filename   = f"{uuid.uuid4().hex}.{ext}"
    file_path         = os.path.join(UPLOAD_FOLDER, unique_filename)
    file.save(file_path)

    job_id = uuid.uuid4().hex
    with ivault_jobs_lock:
        _sweep_expired_ivault_jobs()
        ivault_jobs[job_id] = {
            "status":     "pending",
            "step":       "Queued",
            "step_num":   0.0,
            "values":     None,
            "error":      None,
            "created_at": time.time(),
        }

    threading.Thread(
        target=_run_ivault_recognize,
        args=(job_id, file_path, properties),
        daemon=True,
    ).start()

    return jsonify({"success": True, "job_id": job_id}), 202


# ?????? GET /api/documents/ivault/progress/<job_id> ??? SSE stream ???????????????????????????????????????????????????

@document_bp.route("/ivault/progress/<job_id>")
def ivault_progress(job_id: str):
    """
    Shared SSE stream for all iVault background jobs.

    Handles two job types:
      recognize        ??? final event includes: values = {propertyId: value}
      detect_separators ??? final event includes: separators_found, documents

    Events always carry: status, step, step_num (0.0???1.0).
    """
    def generate():
        last_state = None
        start_time = time.time()
        _MAX_DURATION = 15 * 60  # 15 min - if the worker thread dies without updating status, don't poll forever

        while True:
            if time.time() - start_time > _MAX_DURATION:
                with ivault_jobs_lock:
                    if job_id in ivault_jobs and ivault_jobs[job_id]["status"] not in ("done", "error"):
                        ivault_jobs[job_id]["status"] = "error"
                        ivault_jobs[job_id]["error"]  = "Job timed out (no completion after 15 minutes)"
                yield f"data: {json.dumps({'status': 'error', 'error': 'Job timed out'})}\n\n"
                break

            with ivault_jobs_lock:
                job = ivault_jobs.get(job_id)

            if not job:
                yield f"data: {json.dumps({'error': 'Job not found'})}\n\n"
                break

            state = f"{job['status']}_{job.get('step_num', 0):.2f}_{job.get('step', '')}"
            if state != last_state:
                last_state = state
                payload = {
                    "status":   job["status"],
                    "step":     job.get("step", ""),
                    "step_num": job.get("step_num", 0.0),
                }
                if job["status"] == "done":
                    job_type = job.get("job_type", "recognize")
                    if job_type == "detect_separators":
                        payload["separators_found"] = job.get("separators_found", False)
                        payload["documents"]        = job.get("documents", [])
                    else:
                        payload["values"]  = job.get("values")
                        payload["partial"] = job.get("partial", False)
                elif job["status"] == "error":
                    payload["error"] = job.get("error", "Unknown error")
                yield f"data: {json.dumps(payload)}\n\n"

            if job["status"] in ("done", "error"):
                # Terminal event already flushed above -- the client has
                # everything it needs, so drop the entry now instead of
                # leaking it for the rest of the process lifetime.
                with ivault_jobs_lock:
                    ivault_jobs.pop(job_id, None)
                break

            time.sleep(0.4)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ?????? iVault class helpers ??????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

_IVAULT_FALLBACK_CLASSES = [
    {"classId": None, "className": "AP Invoice"},
    {"classId": None, "className": "AR Invoice"},
    {"classId": None, "className": "Credit Note"},
    {"classId": None, "className": "Receipt"},
    {"classId": None, "className": "Purchase Order"},
    {"classId": None, "className": "Contract"},
    {"classId": None, "className": "Bank Statement"},
    {"classId": None, "className": "ID Card"},
    {"classId": None, "className": "Passport"},
    {"classId": None, "className": "Resume"},
    {"classId": None, "className": "Medical Report"},
]


def _norm_class(s: str) -> str:
    """Normalize a class/type name for fuzzy matching."""
    import re as _re
    return _re.sub(r"[^a-z0-9]", "", str(s).lower())


def _fetch_ivault_classes(tenant: str, username: str, password: str) -> list:
    """
    Fetch iVault class list.  Returns list of {"classId": str, "className": str}.
    Falls back to _IVAULT_FALLBACK_CLASSES on any failure or missing credentials.
    """
    if not (tenant and username and password):
        return _IVAULT_FALLBACK_CLASSES

    import requests as _req
    import re as _re

    tenant = _re.sub(r'^https?://', '', tenant).rstrip('/')

    # Resolve API base (mirrors document_routes._resolve_ivault_api)
    api_base = None
    for scheme in ("https", "http"):
        try:
            cfg = _req.get(f"{scheme}://{tenant}/assets/config.json", timeout=5, verify=False)
            if cfg.ok:
                endpoint = cfg.json().get("endPoint", "").rstrip("/")
                if endpoint:
                    api_base = endpoint
                    break
        except Exception:
            pass
    if not api_base:
        api_base = "https://ivaultapi2.ivaultplus.com/api"

    try:
        lr = _req.post(
            f"{api_base}/login",
            data={"username": username, "password": password, "tenant": tenant},
            timeout=15, verify=False,
        )
        if not lr.ok:
            return _IVAULT_FALLBACK_CLASSES
        token = lr.json().get("token_type", "Bearer") + " " + lr.json()["access_token"]

        cr = _req.get(
            f"{api_base}/Class/list",
            headers={"Authorization": token, "Tenant": tenant},
            timeout=15, verify=False,
        )
        if cr.ok:
            raw = cr.json()
            items = raw if isinstance(raw, list) else raw.get("data", raw.get("classes", raw.get("items", [])))
            classes = []
            for item in items:
                cid = (item.get("classId") or item.get("ClassId") or
                       item.get("id")      or item.get("Id") or "")
                cname = (item.get("className") or item.get("ClassName") or
                         item.get("name")    or item.get("Name") or "")
                if cname:
                    classes.append({"classId": str(cid) if cid else None, "className": cname})
            if classes:
                return classes
    except Exception as e:
        print(f"[DetectSep] Failed to fetch iVault classes: {e}")

    return _IVAULT_FALLBACK_CLASSES


def _match_ivault_class(detected_type: str, classes: list) -> dict:
    """
    Fuzzy-match a snake_case detected_type to the closest iVault class.
    Returns {"classId": ..., "className": ...} or None if no reasonable match.
    """
    needle = _norm_class(detected_type)
    best, best_score = None, 0

    for cls in classes:
        hay = _norm_class(cls["className"])
        # Score: ratio of shared characters (simple overlap)
        score = sum(1 for c in needle if c in hay) / max(len(needle), 1)
        # Boost for full substring match in either direction
        if needle in hay or hay in needle:
            score += 0.5
        if score > best_score:
            best_score = score
            best = cls

    # Require at least 40 % overlap to avoid wild mismatches
    return best if best_score >= 0.4 else None


# ?????? Background worker ???????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

def _run_ivault_detect_separators(
    job_id: str,
    file_path: str,
    tenant: str,
    username: str,
    password: str,
):
    """
    Background thread for POST /api/documents/ivault/detect-separators.

    1. Calls detect_separator_groups() ??? barcode-first, AI fallback.
    2. Fetches iVault classes (live if credentials supplied, else fallback list).
    3. Maps each detected group to the closest iVault class.
    4. Stores result in ivault_jobs for the SSE stream to pick up.
    """
    try:
        with ivault_jobs_lock:
            ivault_jobs[job_id]["status"]   = "processing"
            ivault_jobs[job_id]["step"]     = "Starting..."
            ivault_jobs[job_id]["step_num"] = 0.02

        _init_lm_progress(lambda label, frac: _update_ivault_step(job_id, label, frac))

        from qwen_service import detect_separator_groups
        groups = detect_separator_groups(file_path)

        # Fetch iVault class list for mapping
        _update_ivault_step(job_id, "Matching document types...", 0.95)
        classes = _fetch_ivault_classes(tenant, username, password)

        documents = []
        for grp in groups:
            matched = _match_ivault_class(grp["detected_type"], classes)
            documents.append({
                "pages":     grp["pages"],
                "classId":   matched["classId"]   if matched else None,
                "className": matched["className"] if matched else grp["detected_type"],
            })

        separators_found = len(groups) > 1

        with ivault_jobs_lock:
            ivault_jobs[job_id]["status"]          = "done"
            ivault_jobs[job_id]["step"]            = "Done"
            ivault_jobs[job_id]["step_num"]        = 1.0
            ivault_jobs[job_id]["separators_found"] = separators_found
            ivault_jobs[job_id]["documents"]       = documents

        print(f"[DetectSep] Done ??? {len(documents)} group(s), separators_found={separators_found}")

    except Exception as e:
        import traceback; traceback.print_exc()
        with ivault_jobs_lock:
            ivault_jobs[job_id]["status"] = "error"
            ivault_jobs[job_id]["error"]  = str(e)
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


# ?????? POST /api/documents/ivault/detect-separators ??????????????????????????????????????????????????????????????????????????????????????????

@document_bp.route("/ivault/detect-separators", methods=["POST"])
def ivault_detect_separators():
    """
    Detect document boundaries inside a multi-record PDF.

    Accepts multipart/form-data:
      file       ??? the PDF to analyse (required)
      tenant     ??? iVault tenant host, e.g. "demo.ivaultplus.com" (optional)
      username   ??? iVault username (optional)
      password   ??? iVault password (optional)

    When tenant/username/password are supplied the backend fetches the live
    iVault class list to resolve classId values.  Without them it falls back
    to a built-in class name list (classId will be null).

    Returns:
      {"success": true, "job_id": "<hex>"}

    Progress + result via SSE:
      GET /api/documents/ivault/progress/<job_id>
    """
    file = request.files.get("file")
    if not file or file.filename == "":
        return jsonify({"error": "No file provided"}), 400
    if not allowed_file(file.filename):
        return jsonify({"error": "Unsupported file type ??? PDF required"}), 400

    ext = secure_filename(file.filename).rsplit(".", 1)[-1].lower()
    if ext != "pdf":
        return jsonify({"error": "Only PDF files are supported"}), 400

    unique_filename = f"{uuid.uuid4().hex}.pdf"
    file_path = os.path.join(UPLOAD_FOLDER, unique_filename)
    file.save(file_path)

    tenant   = request.form.get("tenant",   "").strip()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    job_id = uuid.uuid4().hex
    with ivault_jobs_lock:
        _sweep_expired_ivault_jobs()
        ivault_jobs[job_id] = {
            "job_type":        "detect_separators",
            "status":          "pending",
            "step":            "Queued",
            "step_num":        0.0,
            "separators_found": None,
            "documents":       None,
            "error":           None,
            "created_at":      time.time(),
        }

    threading.Thread(
        target=_run_ivault_detect_separators,
        args=(job_id, file_path, tenant, username, password),
        daemon=True,
    ).start()

    return jsonify({"success": True, "job_id": job_id}), 202


@document_bp.route("/learned-structures", methods=["GET"])
def get_learned_structures():
    """Fetch all learned document type structures."""
    try:
        from qwen_service import load_learned_structures
        structures = load_learned_structures()
        return jsonify({
            "learned_structures": structures,
            "count": len(structures)
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ?????? POST /api/documents/<id>/export-ivault ????????????????????????????????????????????????????????????????????????????????????????????????????????????

IVAULT_API_BASE_DEFAULT = "https://ivaultapi2.ivaultplus.com/api"


def _resolve_ivault_api(tenant: str) -> str:
    """
    Resolve the iVault API base URL for a given tenant.
    Tries fetching /assets/config.json from the tenant's web app first.
    Falls back to the cloud API with the tenant as subdomain context.
    """
    import requests as _r
    for scheme in ("https", "http"):
        try:
            cfg = _r.get(f"{scheme}://{tenant}/assets/config.json", timeout=5, verify=False)
            if cfg.ok:
                endpoint = cfg.json().get("endPoint", "").rstrip("/")
                if endpoint:
                    return endpoint
        except Exception:
            pass
    return IVAULT_API_BASE_DEFAULT


class IVaultAuthError(Exception):
    """Raised by _ivault_session() on login failure. Carries a user-facing message and HTTP status."""
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.message = message
        self.status = status


def _ivault_session(tenant: str, username: str, password: str):
    """
    Log into the iVault API for the given tenant.

    Returns (session, auth_header, api_base) on success.
    Raises IVaultAuthError(message, status) on failure.
    """
    import requests as req
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    api_base = _resolve_ivault_api(tenant)
    print(f"[iVault] resolved API base: {api_base} for tenant: {tenant}")

    session = req.Session()
    try:
        login_resp = session.post(
            f"{api_base}/login",
            data={"username": username, "password": password, "tenant": tenant},
            timeout=15,
            verify=False,
        )
        if login_resp.status_code == 400:
            login_resp = session.post(
                f"{api_base}/login",
                data={"username": username, "password": password},
                timeout=15,
                verify=False,
            )
        if not login_resp.ok:
            raise IVaultAuthError(login_resp.text or "Invalid iVault credentials", 401)
        token_data = login_resp.json()
        if not isinstance(token_data, dict) or "access_token" not in token_data:
            raise IVaultAuthError(f"Unexpected iVault login response (no access_token): {str(token_data)[:200]}", 502)
        auth_header = token_data.get("token_type", "Bearer") + " " + token_data["access_token"]
        print(f"[iVault] login OK, cookies: {dict(session.cookies)}")
    except IVaultAuthError:
        raise
    except (KeyError, ValueError, AttributeError):
        raise IVaultAuthError("Unexpected login response from iVault API", 502)
    except req.exceptions.ConnectionError:
        raise IVaultAuthError(f"Cannot connect to iVault API at {api_base}", 502)
    except req.exceptions.Timeout:
        raise IVaultAuthError("iVault login timed out", 504)
    except req.exceptions.HTTPError as e:
        raise IVaultAuthError(f"iVault login failed with HTTP {e.response.status_code}", 502)

    return session, auth_header, api_base


@document_bp.route("/<int:doc_id>/export-ivault", methods=["POST"])
def export_to_ivault(doc_id):
    """
    Upload a processed document to iVault under a user-selected record type.

    Body: {
      "record_type_id": "<iVault classId GUID>",
      "tenant":         "demo.ivaultplus.com",
      "username":       "sys.admin",
      "password":       "secret"
    }

    Flow:
      1. Resolve API base from tenant config.json
      2. POST /api/login  ??? Bearer token
      3. GET  /api/Class/{id} ??? class property definitions
      4. POST /api/document/upload ??? multipart with files[0] format
    """
    import requests as req
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    doc  = Document.query.get_or_404(doc_id)
    data = request.get_json() or {}

    record_type_id = data.get("record_type_id", "").strip()
    username       = data.get("username", "").strip()
    password       = data.get("password", "")
    import re as _re
    raw_tenant = data.get("tenant", "").strip()
    tenant = _re.sub(r'^https?://', '', raw_tenant).rstrip('/')

    if not record_type_id:
        return jsonify({"error": "record_type_id required"}), 400
    if not tenant or not username or not password:
        return jsonify({"error": "iVault credentials (tenant, username, password) required"}), 400

    # ?????? Step 1: Login ??? use a Session so cookies from login carry into upload ??????
    try:
        session, auth_header, api_base = _ivault_session(tenant, username, password)
    except IVaultAuthError as e:
        return jsonify({"error": e.message}), e.status

    # ?????? Step 2: Resolve file info ???????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????
    if not doc.unique_upload_filename:
        return jsonify({"error": "No source file stored for this document"}), 404

    file_path = os.path.join(UPLOAD_FOLDER, doc.unique_upload_filename)
    if not os.path.exists(file_path):
        return jsonify({"error": "Source file not found on disk"}), 404

    ext = doc.unique_upload_filename.rsplit(".", 1)[-1].lower()
    mime_map = {
        "pdf":  "application/pdf",
        "jpg":  "image/jpeg", "jpeg": "image/jpeg",
        "png":  "image/png",  "webp": "image/webp",
    }
    mime = mime_map.get(ext, "application/octet-stream")
    file_size = os.path.getsize(file_path)
    display_name = doc.filename or os.path.basename(file_path)

    # ?????? Step 2b: Searchable PDF (optional) ?????????????????????????????????????????????????????????????????????????????????????????????????????????
    # When "searchable": true is passed, generate a PDF with the raw_text
    # embedded as an invisible text layer so iVault can index/search its content.
    upload_bytes = None
    if data.get("searchable") and doc.raw_text:
        try:
            from pdf_searchable_service import generate_searchable_pdf
            upload_bytes = generate_searchable_pdf(doc.to_dict(), UPLOAD_FOLDER)
            base_name = display_name.rsplit(".", 1)[0]
            display_name = f"{base_name}.pdf"
            ext  = "pdf"
            mime = "application/pdf"
            file_size = len(upload_bytes)
            print(f"[iVault] Searchable PDF generated: {file_size} bytes")
        except Exception as _se:
            print(f"[iVault] Searchable PDF generation failed ({_se}), uploading original")

    # ?????? Step 3: Fetch class property definitions ??????????????????????????????????????????????????????????????????????????????????????????
    class_props = []
    class_name  = ""
    try:
        cp_resp = session.get(
            f"{api_base}/Class/{record_type_id}",
            headers={"Authorization": auth_header, "Tenant": tenant},
            timeout=15,
            verify=False,
        )
        if cp_resp.ok:
            cp_body = cp_resp.json()
            class_props = cp_body.get("classPropertyModels", [])
            class_name  = cp_body.get("className") or cp_body.get("name") or ""
    except Exception:
        pass

    # ?????? Step 3b: If the selected record type differs from what iProfiler
    # detected, sync it to a local DocumentType and re-run extraction with
    # forced_document_type so the fields match this record type's schema
    # (same mechanism used by /ivault/import) before mapping to iVault
    # properties below.
    reextracted_fields = None
    try:
        from settings_routes import _sync_ivault_document_type_with_session
        target_doc_type = _sync_ivault_document_type_with_session(
            record_type_id, class_name or "iVault Class", session, auth_header, api_base, tenant
        )
        db.session.commit()
        if target_doc_type.name != doc.document_type:
            print(f"[iVault] Re-extracting fields for record type '{target_doc_type.name}' (was '{doc.document_type}')")
            result = process_document(file_path, forced_document_type=target_doc_type.name)
            reextracted_fields = result.get("fields") or {}
            doc.document_type = target_doc_type.name
            doc.set_fields(reextracted_fields)
            if result.get("raw_text"):
                doc.raw_text = result["raw_text"]
            doc.confidence = result.get("confidence", doc.confidence)
            db.session.commit()
    except Exception as _sync_err:
        print(f"[iVault] Re-extraction for selected record type skipped: {_sync_err}")

    # ?????? Step 4: Map document fields ??? iVault property GUIDs ??????????????????????????????????????????????????????
    def _norm(s):
        return _re.sub(r'[^a-z0-9]', '', str(s).lower())

    doc_fields = reextracted_fields if reextracted_fields is not None else (doc.get_fields() or {})
    field_lookup = {
        _norm(k): str(v)
        for k, v in doc_fields.items()
        if v is not None and not isinstance(v, (dict, list))
    }
    # Allow iVault properties named "content", "raw_text", "ocr_text", or
    # "full_text" to receive the extracted raw text ??? useful if the iVault
    # class exposes a memo/long-text field for full-text search metadata.
    if doc.raw_text:
        for _alias in ("content", "rawtext", "ocrtext", "fulltext", "ocr", "text"):
            field_lookup.setdefault(_alias, doc.raw_text[:4000])

    # Allow iVault properties named "record_type", "class_id", "document_type"
    # etc. to receive the iVault classId (and class name) of the record type
    # the document was filed under for this export ??? lets iProfiler's
    # extraction surface which record type a document was matched to.
    for _alias in ("recordtype", "recordtypeid", "classid", "documenttype", "doctype"):
        field_lookup.setdefault(_alias, record_type_id)
    if class_name:
        for _alias in ("recordtypename", "documenttypename", "classname"):
            field_lookup.setdefault(_alias, class_name)

    def _match_value(prop_name):
        n = _norm(prop_name)
        if n in field_lookup:
            return field_lookup[n]
        for key, val in field_lookup.items():
            if n in key or key in n:
                return val
        return ""

    # ?????? Step 5: Upload with files[0] indexed multipart format ????????????????????????????????????????????????
    form_data = {
        "files[0][fileName]":  display_name,
        "files[0][extension]": mime,
        "files[0][size]":      str(file_size),
        "files[0][ext]":       ext,
        "files[0][classId]":   record_type_id,
    }
    for i, prop in enumerate(class_props):
        p = f"files[0][properties][{i}]"
        form_data[f"{p}[classId]"]      = record_type_id
        form_data[f"{p}[propertyId]"]   = prop.get("propertyId", "")
        form_data[f"{p}[propertyType]"] = str(prop.get("type", 0))
        form_data[f"{p}[value]"]        = _match_value(prop.get("propertyName", ""))

    print(f"[iVault] uploading to: {api_base}/document/upload")
    print(f"[iVault] file: {display_name} ({file_size} bytes, {mime})")
    print(f"[iVault] form_data fields:")
    for k, v in form_data.items():
        print(f"         {k} = {v}")

    _upload_headers = {
        "Authorization":   auth_header,
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin":          f"https://{tenant}",
        "Referer":         f"https://{tenant}/",
        "Tenant":          tenant,
    }

    try:
        # (connect_timeout, read_timeout): fail fast if iVault's API is
        # unreachable, but give the actual upload real headroom - a real file
        # over a real network (plus whatever server-side processing iVault
        # does on receipt) routinely takes longer than a flat 60s allowed for,
        # which was surfacing as a hard timeout on otherwise-working uploads.
        _upload_timeout = (15, 180)
        if upload_bytes:
            import io as _io
            upload_resp = session.post(
                f"{api_base}/document/upload",
                headers=_upload_headers,
                files={"files[0]": (display_name, _io.BytesIO(upload_bytes), mime)},
                data=form_data,
                timeout=_upload_timeout,
                verify=False,
            )
        else:
            with open(file_path, "rb") as f:
                upload_resp = session.post(
                    f"{api_base}/document/upload",
                    headers=_upload_headers,
                    files={"files[0]": (display_name, f, mime)},
                    data=form_data,
                    timeout=_upload_timeout,
                    verify=False,
                )

        print(f"[iVault] === Request headers sent ({len(upload_resp.request.headers)}) ===")
        for h, v in upload_resp.request.headers.items():
            if h.lower() == "authorization":
                print(f"         {h}: Bearer ***")
            else:
                print(f"         {h}: {v}")
        print(f"[iVault] upload status={upload_resp.status_code}")
        print(f"[iVault] response body: {upload_resp.text[:1000]}")

        if upload_resp.status_code not in (200, 201):
            try:
                detail = upload_resp.json()
            except ValueError:
                detail = upload_resp.text
            return jsonify({
                "error": f"iVault upload returned HTTP {upload_resp.status_code}",
                "detail": detail,
            }), 502

        try:
            upload_body = upload_resp.json()
        except ValueError:
            upload_body = {"raw": upload_resp.text}

    except req.exceptions.Timeout:
        return jsonify({"error": "iVault upload timed out (no response within 180 s)"}), 504
    except req.exceptions.ConnectionError:
        return jsonify({"error": f"Cannot connect to iVault API at {api_base}"}), 502
    except req.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 502

    return jsonify({"success": True, "ivault_response": upload_body})


# ?????? POST /api/documents/ivault/import ???????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

def _push_fields_to_ivault(doc, doc_type, record_type_id, tenant, session, auth_header, api_base):
    """
    Push a processed Document's extracted fields to iVault as a new document
    under record_type_id, mapping DocumentField.external_property_id directly
    to the matching extracted value ??? no fuzzy name matching needed since
    doc_type was synced from this exact iVault class.

    Returns (success: bool, response_dict).
    """
    if not doc.unique_upload_filename:
        return False, {"error": "No source file stored for this document"}

    file_path = os.path.join(UPLOAD_FOLDER, doc.unique_upload_filename)
    if not os.path.exists(file_path):
        return False, {"error": "Source file not found on disk"}

    ext = doc.unique_upload_filename.rsplit(".", 1)[-1].lower()
    mime_map = {
        "pdf":  "application/pdf",
        "jpg":  "image/jpeg", "jpeg": "image/jpeg",
        "png":  "image/png",  "webp": "image/webp",
    }
    mime = mime_map.get(ext, "application/octet-stream")
    file_size = os.path.getsize(file_path)
    display_name = doc.filename or os.path.basename(file_path)

    doc_fields = doc.get_fields() or {}

    form_data = {
        "files[0][fileName]":  display_name,
        "files[0][extension]": mime,
        "files[0][size]":      str(file_size),
        "files[0][ext]":       ext,
        "files[0][classId]":   record_type_id,
    }

    i = 0
    for field in doc_type.fields:
        if not field.external_property_id:
            continue
        value = doc_fields.get(field.field_name, "")
        if value is None:
            value = ""
        elif isinstance(value, (dict, list)):
            value = json.dumps(value)
        p = f"files[0][properties][{i}]"
        form_data[f"{p}[classId]"]      = record_type_id
        form_data[f"{p}[propertyId]"]   = field.external_property_id
        form_data[f"{p}[propertyType]"] = str(field.external_property_type or 0)
        form_data[f"{p}[value]"]        = str(value)
        i += 1

    print(f"[iVault Import] pushing to: {api_base}/document/upload")
    print(f"[iVault Import] file: {display_name} ({file_size} bytes, {mime})")
    for k, v in form_data.items():
        print(f"         {k} = {v}")

    upload_headers = {
        "Authorization":   auth_header,
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin":          f"https://{tenant}",
        "Referer":         f"https://{tenant}/",
        "Tenant":          tenant,
    }

    import requests as req
    try:
        with open(file_path, "rb") as f:
            upload_resp = session.post(
                f"{api_base}/document/upload",
                headers=upload_headers,
                files={"files[0]": (display_name, f, mime)},
                data=form_data,
                timeout=(15, 180),
                verify=False,
            )
        print(f"[iVault Import] upload status={upload_resp.status_code}")
        print(f"[iVault Import] response body: {upload_resp.text[:1000]}")

        if upload_resp.status_code not in (200, 201):
            try:
                detail = upload_resp.json()
            except ValueError:
                detail = upload_resp.text
            return False, {"error": f"iVault upload returned HTTP {upload_resp.status_code}", "detail": detail}

        try:
            return True, upload_resp.json()
        except ValueError:
            return True, {"raw": upload_resp.text}
    except req.exceptions.RequestException as e:
        return False, {"error": str(e)}


def _run_ivault_import_task(job_id, file_index, file_path, original_filename, ext, unique_filename,
                             record_type_id, class_name, tenant, username, password, do_process, sync_back):
    """Background thread for /ivault/import ??? sync type, process, save, optionally push back."""
    from app import app
    from settings_routes import _sync_ivault_document_type

    with app.app_context():
        try:
            with jobs_lock:
                jobs[job_id]["files"][file_index]["status"]     = "processing"
                jobs[job_id]["files"][file_index]["started_at"] = time.time()
                jobs[job_id]["files"][file_index]["step"]       = "Connecting to iVault..."
                jobs[job_id]["files"][file_index]["step_num"]   = 0

            doc_type = DocumentType.query.filter_by(external_source="ivault", external_id=record_type_id).first()
            if doc_type is None:
                update_job_step(job_id, file_index, "Syncing record type from iVault...", 0.1)
                doc_type = _sync_ivault_document_type(record_type_id, class_name, tenant, username, password)

            _init_lm_progress(lambda label=None, frac=None, pages_done=None, pages_total=None:
                              update_job_step(job_id, file_index, label, frac, pages_done, pages_total))

            if ext == "docx":
                update_job_step(job_id, file_index, "Converting document...", 0.05)
                from docx_service import docx_to_pdf
                pdf_unique_filename = os.path.splitext(unique_filename)[0] + ".pdf"
                pdf_path = os.path.join(UPLOAD_FOLDER, pdf_unique_filename)
                docx_to_pdf(file_path, pdf_path)
                os.remove(file_path)
                file_path       = pdf_path
                unique_filename = pdf_unique_filename
                ext             = "pdf"

            if do_process:
                update_job_step(job_id, file_index, "Processing with iProfiler...", 0.2)
                result = process_document(file_path, forced_document_type=doc_type.name)
            else:
                result = {
                    "document_type": doc_type.name,
                    "fields": {},
                    "raw_text": "",
                    "confidence": 0.0,
                    "text_blocks": None,
                }

            doc_dict = _save_document_to_db(result, original_filename, unique_filename, ext)

            ivault_push = None
            if sync_back and do_process:
                update_job_step(job_id, file_index, "Pushing results to iVault...", 0.95)
                try:
                    session, auth_header, api_base = _ivault_session(tenant, username, password)
                    doc = Document.query.get(doc_dict["id"])
                    ok, resp = _push_fields_to_ivault(doc, doc_type, record_type_id, tenant, session, auth_header, api_base)
                    ivault_push = {"success": ok, "response": resp}
                except IVaultAuthError as e:
                    ivault_push = {"success": False, "response": {"error": e.message}}

            doc_dict["ivault_push"] = ivault_push

            with jobs_lock:
                jobs[job_id]["files"][file_index]["status"] = "done"
                jobs[job_id]["files"][file_index]["result"] = doc_dict
                jobs[job_id]["status"] = "done"

        except Exception as e:
            with jobs_lock:
                jobs[job_id]["files"][file_index]["status"] = "error"
                jobs[job_id]["files"][file_index]["error"]  = str(e)
                jobs[job_id]["status"] = "done"


@document_bp.route("/ivault/import", methods=["POST"])
def ivault_import():
    """
    Import a document tied to a specific iVault record type.

    On first use of a given record_type_id, the iVault class is synced into
    a local DocumentType/DocumentField set (storing classId/propertyId) so
    extraction targets exactly those fields and results map back cleanly.

    Multipart form fields:
      file            ??? the document to import
      record_type_id  ??? iVault classId GUID
      class_name      ??? iVault class display name (used only if syncing for the first time)
      tenant          ??? iVault tenant host
      username        ??? iVault username
      password        ??? iVault password
      process         ??? "true"/"false" ??? run iProfiler extraction (default "true")
      sync_back       ??? "true"/"false" ??? push extracted fields back to iVault (default "false")

    Returns a job_id; track via the existing GET /api/documents/progress/<job_id> SSE stream.
    """
    import re as _re

    file = request.files.get("file")
    if not file or file.filename == "":
        return jsonify({"error": "No file provided"}), 400
    if not allowed_file(file.filename):
        return jsonify({"error": "Unsupported file type"}), 400

    record_type_id = (request.form.get("record_type_id") or "").strip()
    class_name      = (request.form.get("class_name") or "").strip()
    raw_tenant      = (request.form.get("tenant") or "").strip()
    tenant          = _re.sub(r'^https?://', '', raw_tenant).rstrip('/')
    username        = (request.form.get("username") or "").strip()
    password        = request.form.get("password") or ""
    do_process      = (request.form.get("process", "true").lower() in ("true", "1", "yes"))
    sync_back       = (request.form.get("sync_back", "false").lower() in ("true", "1", "yes"))

    if not record_type_id:
        return jsonify({"error": "record_type_id required"}), 400
    if not tenant or not username or not password:
        return jsonify({"error": "iVault credentials (tenant, username, password) required"}), 400

    original_filename = secure_filename(file.filename)
    ext               = original_filename.rsplit(".", 1)[1].lower()
    unique_filename   = f"{uuid.uuid4().hex}.{ext}"
    file_path         = os.path.join(UPLOAD_FOLDER, unique_filename)
    file.save(file_path)

    job_id = uuid.uuid4().hex
    job_files = [{
        "filename":        original_filename,
        "unique_filename": unique_filename,
        "file_path":       file_path,
        "ext":             ext,
        "status":      "pending",
        "step":        "Queued",
        "step_num":    0,
        "total_steps": 4,
        "started_at":  None,
        "result":      None,
        "sub_results": None,
        "error":       None,
    }]

    with jobs_lock:
        jobs[job_id] = {"status": "processing", "files": job_files}

    threading.Thread(
        target=_run_ivault_import_task,
        args=(job_id, 0, file_path, original_filename, ext, unique_filename,
              record_type_id, class_name, tenant, username, password, do_process, sync_back),
        daemon=True,
    ).start()

    return jsonify({"success": True, "job_id": job_id}), 202





