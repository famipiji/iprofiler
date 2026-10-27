import os
import time
import threading
from datetime import datetime, timedelta

from extensions import db
from document_model import Document, DocumentChunk, DocumentDeletionAuditLog, TenantRetentionPolicy


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")

_scheduler_started = False

# A production document that's never confirmed has no _expiry_anchor() (its
# confirmed_at stays None) and previously never expired regardless of age -
# a compliance gap for a retention system. Hard-cap it from created_at
# instead, independent of (and typically longer than) the normal
# confirmed-document TTL, so it eventually purges either way.
UNCONFIRMED_MAX_AGE_DAYS = int(os.environ.get("UNCONFIRMED_MAX_AGE_DAYS", "90"))


def get_retention_policy(tenant_id: str) -> TenantRetentionPolicy:
    policy = TenantRetentionPolicy.query.filter_by(tenant_id=tenant_id).first()
    if policy is None:
        policy = TenantRetentionPolicy(
            tenant_id=tenant_id,
            sample_ttl_days=7,
            production_ttl_days=30,
        )
    return policy


def _ttl_for_document(document: Document, policy: TenantRetentionPolicy) -> int:
    if document.ttl_days_override is not None:
        return max(0, int(document.ttl_days_override))
    if (document.document_class or "production").lower() == "sample":
        return max(0, int(policy.sample_ttl_days or 7))
    return max(0, int(policy.production_ttl_days or 30))


def _expiry_anchor(document: Document) -> datetime | None:
    if (document.document_class or "production").lower() == "sample":
        return document.created_at
    return document.confirmed_at


def _document_is_expired(document: Document, now: datetime) -> bool:
    if document.pinned or document.purged_at is not None:
        return False

    policy = get_retention_policy(document.tenant_id)
    ttl_days = _ttl_for_document(document, policy)
    anchor = _expiry_anchor(document)
    if anchor is not None:
        return now >= anchor + timedelta(days=ttl_days)

    # No anchor (an unconfirmed production document - see _expiry_anchor)
    # would otherwise never expire. Fall back to a hard-cap TTL from
    # created_at instead of treating "never confirmed" as "never expires".
    if document.created_at is None:
        return False
    return now >= document.created_at + timedelta(days=UNCONFIRMED_MAX_AGE_DAYS)


def _delete_raw_file(document: Document) -> None:
    filename = document.unique_upload_filename or document.storage_key
    if not filename:
        return
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(file_path):
        os.remove(file_path)


def _delete_vector_chunks(document: Document) -> None:
    try:
        from rag_service import delete_document
        delete_document(document.id, tenant_id=document.tenant_id)
    except Exception as exc:
        print(f"[Retention] Vector delete failed for doc {document.id}: {exc}")


def _delete_derived_artifacts(document: Document) -> None:
    """Per-document derived files that live outside the raw upload itself -
    e.g. pdf_searchable_service's on-disk searchable-PDF cache, keyed
    '{doc_id}_{hash}.pdf'. Without this, a 'purged' document's cached
    searchable export (still containing the document's OCR'd text/content)
    can remain on disk indefinitely, undermining the retention guarantee.
    Each artifact location is wrapped in its own try/except so a missing or
    already-cleaned-up file doesn't abort the rest of this document's purge."""
    try:
        import glob
        from pdf_searchable_service import _SEARCHABLE_CACHE_DIR
        for path in glob.glob(os.path.join(_SEARCHABLE_CACHE_DIR, f"{document.id}_*.pdf")):
            try:
                os.remove(path)
            except OSError as exc:
                print(f"[Retention] Failed to remove cached searchable PDF {path}: {exc}")
    except Exception as exc:
        print(f"[Retention] Derived-artifact cleanup failed for doc {document.id}: {exc}")


def _delete_chunk_rows(document: Document) -> int:
    deleted = DocumentChunk.query.filter_by(document_id=document.id, tenant_id=document.tenant_id).delete(synchronize_session=False)
    return int(deleted or 0)


def _purge_document(document: Document, reason: str, now: datetime, respect_pin: bool = True) -> bool:
    if (respect_pin and document.pinned) or document.purged_at is not None:
        return False

    try:
        _delete_raw_file(document)
    except Exception as exc:
        print(f"[Retention] Raw file delete failed for doc {document.id}: {exc}")

    _delete_derived_artifacts(document)
    _delete_vector_chunks(document)
    deleted_chunks = _delete_chunk_rows(document)

    db.session.add(DocumentDeletionAuditLog(
        tenant_id=document.tenant_id,
        document_id=document.id,
        reason=reason,
        actor="system",
        details=f"purge deleted {deleted_chunks} chunk rows",
    ))
    document.purged_at = now
    document.purge_reason = reason
    db.session.commit()
    return True


PURGE_SCAN_BATCH_SIZE = 200


def purge_expired_documents(app, now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    with app.app_context():
        purged = []
        skipped = []
        failed = []

        # Keyset-paginate (id > last_id) instead of loading every non-purged
        # document across every tenant into memory at once - a production-
        # scale table previously meant one full-table load + per-row Python
        # object materialization every time this ran (daily). Keyset (not
        # OFFSET) pagination so rows already purged in an earlier batch of
        # this same run don't shift later pages.
        last_id = 0
        while True:
            batch = (
                Document.query
                .filter(Document.purged_at.is_(None), Document.id > last_id)
                .order_by(Document.id)
                .limit(PURGE_SCAN_BATCH_SIZE)
                .all()
            )
            if not batch:
                break
            last_id = batch[-1].id

            for document in batch:
                if document.pinned:
                    skipped.append({"doc_id": document.id, "tenant_id": document.tenant_id, "reason": "pinned"})
                    continue
                if not _document_is_expired(document, now):
                    continue
                try:
                    if _purge_document(document, "ttl_expired", now, respect_pin=True):
                        purged.append({"doc_id": document.id, "tenant_id": document.tenant_id})
                except Exception as exc:
                    # A failed commit leaves the session unusable for the rest of the
                    # batch until rolled back -- without this, one bad document would
                    # still silently stop every document after it even inside try/except.
                    db.session.rollback()
                    print(f"[Retention] Purge failed for doc {document.id} (tenant={document.tenant_id}): {exc}")
                    failed.append({"doc_id": document.id, "tenant_id": document.tenant_id, "error": str(exc)})

        return {
            "purged_count": len(purged),
            "skipped_count": len(skipped),
            "failed_count": len(failed),
            "purged": purged,
            "skipped": skipped,
            "failed": failed,
        }


def purge_documents(app, *, doc_id: int | None = None, tenant_id: str | None = None, reason: str = "right_to_erasure") -> dict:
    now = datetime.utcnow()
    with app.app_context():
        query = Document.query.filter(Document.purged_at.is_(None))
        if doc_id is not None:
            query = query.filter(Document.id == doc_id)
        if tenant_id is not None:
            query = query.filter(Document.tenant_id == tenant_id)

        documents = query.all()
        purged = []
        failed = []
        for document in documents:
            # Each document (in particular each right-to-erasure request) is
            # purged independently -- one failure must not block the rest of
            # this batch's pending erasure requests.
            try:
                if _purge_document(document, reason, now, respect_pin=False):
                    purged.append({"doc_id": document.id, "tenant_id": document.tenant_id})
            except Exception as exc:
                db.session.rollback()
                print(f"[Retention] Purge failed for doc {document.id} (tenant={document.tenant_id}, reason={reason}): {exc}")
                failed.append({"doc_id": document.id, "tenant_id": document.tenant_id, "error": str(exc)})

        return {"purged_count": len(purged), "failed_count": len(failed), "purged": purged, "failed": failed}


def _seconds_until_next_midnight() -> float:
    now = datetime.utcnow()
    next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(60.0, (next_midnight - now).total_seconds())


_RETENTION_LOCK_NAME = "vaultmind_retention_purge"


def _run_purge_with_distributed_lock(app) -> None:
    """_scheduler_started only prevents duplicate scheduler threads within a
    single process - under a multi-worker deployment (e.g. gunicorn -w N),
    each worker is its own process with its own copy of that flag, so every
    worker would still start a thread and every one of them would run the
    purge at the same scheduled time. Guard the actual run with a MySQL
    named lock so only one worker's scheduled run executes; the rest skip
    that cycle rather than purge concurrently against the same rows."""
    with app.app_context():
        try:
            conn = db.engine.connect()
        except Exception as exc:
            print(f"[Retention] Could not open a connection for the scheduler lock: {exc}")
            purge_expired_documents(app)
            return
        try:
            try:
                got_lock = conn.execute(db.text(f"SELECT GET_LOCK('{_RETENTION_LOCK_NAME}', 0)")).scalar()
            except Exception as exc:
                # Non-MySQL backend (e.g. sqlite in tests) doesn't support
                # GET_LOCK - fail open rather than never purging at all.
                print(f"[Retention] Scheduler lock unavailable ({exc}), running without it")
                purge_expired_documents(app)
                return
            if got_lock != 1:
                print("[Retention] Another worker holds the purge lock, skipping this run")
                return
            try:
                purge_expired_documents(app)
            finally:
                try:
                    conn.execute(db.text(f"SELECT RELEASE_LOCK('{_RETENTION_LOCK_NAME}')"))
                except Exception as exc:
                    print(f"[Retention] Failed to release scheduler lock: {exc}")
        finally:
            conn.close()


def start_retention_scheduler(app) -> None:
    global _scheduler_started
    if _scheduler_started:
        return
    if os.environ.get("DISABLE_RETENTION_SCHEDULER", "0") == "1":
        return

    _scheduler_started = True

    def _loop():
        while True:
            try:
                _run_purge_with_distributed_lock(app)
            except Exception as exc:
                print(f"[Retention] Scheduled purge failed: {exc}")
            time.sleep(_seconds_until_next_midnight())

    thread = threading.Thread(target=_loop, daemon=True, name="retention-purge-scheduler")
    thread.start()