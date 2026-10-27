"""
Batch processing service - the central entry point for all document batch operations.

Mirrors retention_service.py's file-per-concern pattern: one service module per major
workflow, with helper functions that can be called independently for testing or from
other services.
"""

import os
import secrets
from datetime import datetime
from typing import List, Optional

from extensions import db
from document_model import Batch, SourceFile, DocumentJob


def _generate_display_code() -> str:
    """
    Generate a unique 6-character alphanumeric display code for a batch.

    Returns:
        Uppercase alphanumeric string (e.g. 'A3F7B2')
    """
    max_attempts = 10
    for _ in range(max_attempts):
        # secrets.token_hex(3) generates 6 hex chars
        code = secrets.token_hex(3).upper()

        # Check if this code is already in use
        existing = Batch.query.filter_by(display_code=code).first()
        if not existing:
            return code

    # Fallback: if we somehow can't find a unique code in 10 tries,
    # use a longer code
    return secrets.token_hex(4).upper()[:8]


def create_batch(
    files: List[dict],
    source: str,
    client_id: str = "default",
    split_mode: str = "none",
    notify_email: Optional[str] = None
) -> Batch:
    """
    Create a new batch with one SourceFile per file and (for Phase 1) one DocumentJob per file.

    Parameters:
        files: List of file dicts, each with 'file_path' (absolute path to the uploaded file)
               and optionally 'page_count' (for PDFs)
        source: 'manual_upload' | 'watched_folder' | 'api'
        client_id: Tenant/client identifier (default: "default")
        split_mode: 'none' (Phase 1 only) | 'mrn_barcode' | ... (Phase 2+)
        notify_email: Optional email to notify when batch completes

    Returns:
        The created Batch object (with id populated)

    Example:
        batch = create_batch(
            files=[{'file_path': '/path/to/file.pdf', 'page_count': 5}],
            source='manual_upload',
            client_id='tenant_123'
        )
    """
    if not files:
        raise ValueError("Cannot create a batch with zero files")

    if split_mode != "none":
        raise NotImplementedError(
            f"split_mode='{split_mode}' is not implemented yet (Phase 2). "
            "Use 'none' for Phase 1."
        )

    # Create the Batch record
    batch = Batch(
        client_id=client_id,
        source=source,
        status="open",
        total_jobs=0,  # Will be set after we create jobs
        completed_count=0,
        failed_count=0,
        split_pending_count=0,
        notify_email=notify_email,
        display_code=_generate_display_code(),
        created_at=datetime.utcnow(),
    )
    db.session.add(batch)
    db.session.flush()  # Get batch.id before creating children

    job_count = 0

    for file_info in files:
        file_path = file_info.get('file_path')
        if not file_path:
            raise ValueError("Each file must have a 'file_path' key")

        page_count = file_info.get('page_count')

        # Create SourceFile
        source_file = SourceFile(
            batch_id=batch.id,
            file_path=file_path,
            page_count=page_count,
            split_mode=split_mode,
            status="queued",
        )
        db.session.add(source_file)
        db.session.flush()  # Get source_file.id

        # Phase 1: split_mode='none' means one job per file (no splitting)
        if split_mode == "none":
            job = DocumentJob(
                batch_id=batch.id,
                source_file_id=source_file.id,
                page_range=None,  # Whole file
                file_path=file_path,
                status="held",  # Jobs start as 'held' until user explicitly starts processing
                attempts=0,
                max_attempts=3,
                priority=0,
                created_at=datetime.utcnow(),
            )
            db.session.add(job)
            job_count += 1

    batch.total_jobs = job_count
    db.session.commit()

    return batch


def get_batch_status(batch_id: int) -> dict:
    """
    Retrieve the current status of a batch (for dashboard/polling).

    Returns a dict with batch metadata and rollup counts. Returns None if the batch doesn't exist.
    """
    batch = Batch.query.get(batch_id)
    if not batch:
        return None

    return {
        "id": batch.id,
        "client_id": batch.client_id,
        "source": batch.source,
        "status": batch.status,
        "total_jobs": batch.total_jobs,
        "completed_count": batch.completed_count,
        "failed_count": batch.failed_count,
        "split_pending_count": batch.split_pending_count,
        "notify_email": batch.notify_email,
        "created_at": batch.created_at.isoformat() + "Z" if batch.created_at else None,
        "completed_at": batch.completed_at.isoformat() + "Z" if batch.completed_at else None,
    }


def update_batch_status(batch_id: int) -> None:
    """
    Recalculate batch rollup counts from actual DocumentJob rows and update batch.status.

    Called by the worker after each job completes/fails, so the batch status reflects
    current reality even if a worker crashed mid-batch (leaving completed_count stale).

    Updates:
      - completed_count: count of jobs with status='succeeded'
      - failed_count: count of jobs with status in ('failed', 'dead_letter')
      - status: 'completed' if all jobs done with no failures, 'completed_with_errors'
                if all done but some failed, otherwise remains 'processing'/'open'
    """
    batch = Batch.query.get(batch_id)
    if not batch:
        return

    # Count jobs by status
    succeeded = DocumentJob.query.filter_by(batch_id=batch_id, status="succeeded").count()
    failed = DocumentJob.query.filter(
        DocumentJob.batch_id == batch_id,
        DocumentJob.status.in_(["failed", "dead_letter"])
    ).count()

    # Held, queued, claimed, processing
    pending = DocumentJob.query.filter(
        DocumentJob.batch_id == batch_id,
        DocumentJob.status.in_(["held", "queued", "claimed", "processing"])
    ).count()

    batch.completed_count = succeeded
    batch.failed_count = failed

    # Update batch status if all jobs are done
    if pending == 0:
        if failed > 0:
            batch.status = "completed_with_errors"
        else:
            batch.status = "completed"
        batch.completed_at = datetime.utcnow()
    elif succeeded > 0 or failed > 0:
        # At least one job has finished (but some are still pending)
        batch.status = "processing"

    db.session.commit()


def list_batches(client_id: str = "default", limit: int = 100) -> list:
    """
    List batches for a given tenant/client, ordered by newest first.

    Parameters:
        client_id: Tenant identifier (default: "default")
        limit: Maximum number of batches to return (default: 100)

    Returns:
        List of batch dicts via Batch.to_dict()

    Security note / TODO:
        This uses the same "default" tenant_id fallback as the upload endpoint
        (document_routes.py:542) and get_all_documents (document_routes.py:836).
        This is NOT real tenant isolation - it's a shared literal string.
        Before any multi-client production deployment, proper authentication and
        session-based tenant scoping must be added across ALL endpoints:
        - /api/documents (currently returns all documents, no tenant filter)
        - /api/documents/upload (accepts tenant_id from untrusted form data)
        - /api/documents/batches (this endpoint)
    """
    batches = Batch.query.filter_by(client_id=client_id)\
                         .order_by(Batch.created_at.desc())\
                         .limit(limit)\
                         .all()

    return [b.to_dict() for b in batches]


def get_batch_details(batch_id: int, client_id: str = "default") -> Optional[dict]:
    """
    Get detailed batch information including all document jobs.

    Parameters:
        batch_id: Batch ID
        client_id: Tenant identifier (default: "default")

    Returns:
        Dict with batch info and jobs list, or None if not found

    Security note / TODO:
        Same tenant isolation gap as list_batches() - see comment above.
    """
    batch = Batch.query.filter_by(id=batch_id, client_id=client_id).first()
    if not batch:
        return None

    # Fetch all jobs for this batch with eager-loading of source_file relationship
    # to avoid N+1 queries when accessing job.source_file.page_count in to_dict()
    jobs = DocumentJob.query.filter_by(batch_id=batch_id)\
                            .options(db.joinedload(DocumentJob.source_file))\
                            .order_by(DocumentJob.created_at.asc())\
                            .all()

    return {
        "batch": batch.to_dict(),
        "jobs": [j.to_dict() for j in jobs]
    }


def retry_failed_jobs(batch_id: int, client_id: str = "default") -> Optional[dict]:
    """
    Retry all failed and dead-letter jobs in a batch.

    Resets failed/dead_letter jobs to queued status, clears error state,
    and transitions batch back to processing.

    Parameters:
        batch_id: Batch ID
        client_id: Tenant identifier (default: "default")

    Returns:
        Dict with updated batch info and count of jobs reset, or None if not found

    Security note / TODO:
        Same tenant isolation gap as list_batches() - see comment above.
    """
    batch = Batch.query.filter_by(id=batch_id, client_id=client_id).first()
    if not batch:
        return None

    # Find all failed/dead_letter jobs
    failed_jobs = DocumentJob.query.filter_by(batch_id=batch_id)\
                                   .filter(DocumentJob.status.in_(['failed', 'dead_letter']))\
                                   .all()

    if not failed_jobs:
        return {
            "batch": batch.to_dict(),
            "jobs_reset": 0,
            "message": "No failed jobs to retry"
        }

    # Reset each job to queued state
    for job in failed_jobs:
        job.status = 'queued'
        job.attempts = 0
        job.last_error = None
        job.claimed_by = None
        job.claimed_at = None
        job.started_at = None
        job.finished_at = None

    # Update batch status and counts
    if batch.status == 'completed_with_errors':
        batch.status = 'processing'

    # Recalculate batch counts from actual job states
    update_batch_status(batch_id)

    db.session.commit()

    return {
        "batch": batch.to_dict(),
        "jobs_reset": len(failed_jobs),
        "message": f"Reset {len(failed_jobs)} failed job(s) to queued"
    }


def cancel_batch(batch_id: int, client_id: str = "default") -> Optional[dict]:
    """
    Cancel a batch by marking all non-terminal jobs as cancelled.

    Force-cancels jobs in any non-terminal state (held, queued, claimed, processing).
    Jobs stuck in 'processing' for extended periods (e.g. lost worker, hung LLM call)
    are forcibly terminated since there's no interrupt mechanism for in-flight calls.
    Batch status is updated based on final job states.

    Parameters:
        batch_id: Batch ID
        client_id: Tenant identifier (default: "default")

    Returns:
        Dict with updated batch info and count of jobs cancelled, or None if not found

    Security note / TODO:
        Same tenant isolation gap as list_batches() - see comment above.
    """
    batch = Batch.query.filter_by(id=batch_id, client_id=client_id).first()
    if not batch:
        return None

    # Only allow cancelling if batch is still processing
    if batch.status not in ('open', 'processing'):
        return {
            "batch": batch.to_dict(),
            "jobs_cancelled": 0,
            "message": f"Cannot cancel batch with status '{batch.status}'"
        }

    # Find all non-terminal jobs (held, queued, claimed, processing)
    # Terminal states (succeeded, failed, dead_letter, cancelled) are left unchanged
    cancellable_jobs = DocumentJob.query.filter_by(batch_id=batch_id)\
        .filter(DocumentJob.status.in_(['held', 'queued', 'claimed', 'processing']))\
        .all()

    if not cancellable_jobs:
        return {
            "batch": batch.to_dict(),
            "jobs_cancelled": 0,
            "message": "No jobs to cancel (all are already in a terminal state)"
        }

    # Mark all non-terminal jobs as cancelled
    for job in cancellable_jobs:
        job.status = 'cancelled'
        job.finished_at = datetime.utcnow()

    # Recalculate batch status - will transition to cancelled/completed once all jobs are terminal
    update_batch_status(batch_id)

    db.session.commit()

    return {
        "batch": batch.to_dict(),
        "jobs_cancelled": len(cancellable_jobs),
        "message": f"Cancelled {len(cancellable_jobs)} job(s)"
    }


def get_batch_results(batch_id: int, client_id: str = "default") -> Optional[dict]:
    """
    Get all documents created by jobs in this batch.

    Parameters:
        batch_id: Batch ID
        client_id: Tenant identifier (default: "default")

    Returns:
        Dict with batch info and list of documents, or None if not found

    Security note / TODO:
        Same tenant isolation gap as list_batches() - see comment above.
    """
    from document_model import Document

    batch = Batch.query.filter_by(id=batch_id, client_id=client_id).first()
    if not batch:
        return None

    # Find all document IDs created by this batch's jobs
    job_results = DocumentJob.query.filter_by(batch_id=batch_id)\
                                   .filter(DocumentJob.result_ref.isnot(None))\
                                   .all()

    document_ids = [job.result_ref for job in job_results]

    # Fetch all documents
    if document_ids:
        documents = Document.query.filter(Document.id.in_(document_ids)).all()
    else:
        documents = []

    return {
        "batch": batch.to_dict(),
        "documents": [d.to_dict() for d in documents],
        "total_documents": len(documents)
    }


def get_all_batch_documents(client_id: str = "default") -> dict:
    """
    Get all documents produced by ALL batches for this client.

    Returns a flat list of documents ordered by creation date (newest first),
    regardless of which batch they came from. This is for the "View all
    processed documents" link on the Batches list page.

    Parameters:
        client_id: Tenant identifier (default: "default")

    Returns:
        Dict with documents list and total count

    Security note / TODO:
        Same tenant isolation gap as list_batches() - see comment above.
    """
    from document_model import Document

    # Find all DocumentJobs for this client's batches that have produced documents
    job_results = db.session.query(DocumentJob)\
                            .join(Batch, DocumentJob.batch_id == Batch.id)\
                            .filter(Batch.client_id == client_id)\
                            .filter(DocumentJob.result_ref.isnot(None))\
                            .all()

    document_ids = [job.result_ref for job in job_results]

    # Fetch all documents, ordered newest first
    if document_ids:
        documents = Document.query.filter(Document.id.in_(document_ids))\
                                  .order_by(Document.created_at.desc())\
                                  .all()
    else:
        documents = []

    return {
        "documents": [d.to_dict() for d in documents],
        "total_documents": len(documents)
    }


def update_batch_label(
    batch_id: int,
    label: Optional[str],
    client_id: str = "default"
) -> Optional[dict]:
    """
    Update the label for a batch.

    Trims the label and treats empty strings as None. Label is purely cosmetic
    and doesn't affect batch processing logic.

    Security note / TODO:
        Same tenant isolation gap as list_batches() - uses filter_by(client_id=...)
        where client_id comes from request params, not session auth.
    """
    batch = Batch.query.filter_by(id=batch_id, client_id=client_id).first()
    if not batch:
        return None

    # Trim label, treat empty string as None
    batch.label = (label or "").strip() or None
    db.session.commit()

    return {"batch": batch.to_dict()}


def delete_batch(
    batch_id: int,
    client_id: str = "default"
) -> dict:
    """
    Delete a batch and all associated SourceFile/DocumentJob records.

    Document records (DocumentJob.result_ref) are NOT deleted - they remain in
    the library. This is safe because result_ref is a plain integer column, not
    a ForeignKey with cascading delete.

    Deleting a batch that is currently processing is blocked to prevent worker
    crashes when they try to update a DocumentJob row that no longer exists.

    Returns error if:
    - Batch not found (or belongs to different tenant)
    - Batch status is 'processing' (must cancel first)

    Security note / TODO:
        Same tenant isolation gap as list_batches() - uses filter_by(client_id=...)
        where client_id comes from request params, not session auth.
    """
    batch = Batch.query.filter_by(id=batch_id, client_id=client_id).first()
    if not batch:
        return {"success": False, "error": "Batch not found"}

    # Block deletion of processing batches to avoid worker crashes
    if batch.status == "processing":
        return {
            "success": False,
            "error": "Cannot delete a batch that is currently processing. Please cancel it first."
        }

    # Delete batch - cascades to SourceFile and DocumentJob automatically
    # (Document records with result_ref remain untouched)
    db.session.delete(batch)
    db.session.commit()

    return {
        "success": True,
        "message": f"Deleted batch #{batch_id} and associated jobs"
    }


def clear_all_batches(client_id: str = "default") -> dict:
    """
    Delete all batches that are NOT currently processing.

    Processing batches are skipped to avoid worker crashes.
    If the table is completely empty after deletion, the auto-increment counter
    is reset to 1.

    Parameters:
        client_id: Tenant identifier (default: "default")

    Returns:
        Dict with success status, counts, and whether auto-increment was reset
    """
    # Find all batches for this tenant that are not processing
    deletable_batches = Batch.query.filter_by(client_id=client_id)\
        .filter(Batch.status != 'processing')\
        .all()

    # Count processing batches (skipped)
    processing_count = Batch.query.filter_by(client_id=client_id, status='processing').count()

    deleted_count = len(deletable_batches)

    # Delete all non-processing batches
    for batch in deletable_batches:
        db.session.delete(batch)

    db.session.commit()

    # Check if table is completely empty (across all tenants)
    remaining_count = Batch.query.count()
    auto_increment_reset = False

    if remaining_count == 0:
        # Table is completely empty - reset auto-increment
        try:
            db.session.execute(db.text("ALTER TABLE batches AUTO_INCREMENT = 1"))
            db.session.commit()
            auto_increment_reset = True
        except Exception as e:
            print(f"[Clear All] Failed to reset auto-increment: {e}")
            # Not a critical error - continue

    return {
        "success": True,
        "deleted_count": deleted_count,
        "skipped_count": processing_count,
        "auto_increment_reset": auto_increment_reset,
        "remaining_total": remaining_count
    }


def add_files_to_batch(
    batch_id: int,
    files: List[dict],
    client_id: str = "default"
) -> Optional[dict]:
    """
    Add files to an existing batch.

    Creates new SourceFile and DocumentJob rows for each file.
    Batch must be in 'open' status to add files.

    Parameters:
        batch_id: Batch ID
        files: List of file dicts, each with 'file_path' and optionally 'page_count'
        client_id: Tenant identifier (default: "default")

    Returns:
        Dict with updated batch info and count of jobs added, or None if not found

    Security note / TODO:
        Same tenant isolation gap as list_batches() - see comment above.
    """
    batch = Batch.query.filter_by(id=batch_id, client_id=client_id).first()
    if not batch:
        return None

    # Only allow adding files to open batches
    if batch.status != 'open':
        return {
            "batch": batch.to_dict(),
            "jobs_added": 0,
            "message": f"Cannot add files to batch with status '{batch.status}'. Only 'open' batches accept new files."
        }

    if not files:
        return {
            "batch": batch.to_dict(),
            "jobs_added": 0,
            "message": "No files provided"
        }

    jobs_added = 0

    # Create SourceFile and DocumentJob for each file
    for file_info in files:
        file_path = file_info.get('file_path')
        page_count = file_info.get('page_count')

        if not file_path:
            continue

        # Create SourceFile
        source_file = SourceFile(
            batch_id=batch_id,
            file_path=file_path,
            page_count=page_count,
            split_mode='none',
            status='queued'
        )
        db.session.add(source_file)
        db.session.flush()  # Get source_file.id

        # Create DocumentJob (Phase 1: one job per file, no splitting)
        job = DocumentJob(
            batch_id=batch_id,
            source_file_id=source_file.id,
            file_path=file_path,
            status='held',  # Jobs start as 'held' until user starts processing
            priority=0
        )
        db.session.add(job)
        jobs_added += 1

    # Update batch total_jobs count
    batch.total_jobs += jobs_added

    # Transition batch to processing if it was in draft
    if batch.status == 'draft':
        batch.status = 'open'

    db.session.commit()

    return {
        "batch": batch.to_dict(),
        "jobs_added": jobs_added,
        "message": f"Added {jobs_added} file(s) to batch"
    }


def start_batch_processing(batch_id: int, client_id: str = "default") -> Optional[dict]:
    """
    Start processing a batch by transitioning all held jobs to queued.

    This makes jobs visible to the worker claim query (WHERE status='queued')
    and marks the batch as actively processing.

    Parameters:
        batch_id: Batch ID
        client_id: Tenant identifier (default: "default")

    Returns:
        Dict with batch info and count of jobs started, or None if not found

    Security note / TODO:
        Same tenant isolation gap as list_batches() - see comment above.
    """
    batch = Batch.query.filter_by(id=batch_id, client_id=client_id).first()
    if not batch:
        return None

    # Only allow starting batches that are open
    if batch.status != 'open':
        return {
            "batch": batch.to_dict(),
            "jobs_started": 0,
            "message": f"Cannot start batch with status '{batch.status}'. Only 'open' batches can be started."
        }

    # Find all held jobs
    held_jobs = DocumentJob.query.filter_by(batch_id=batch_id, status='held').all()

    if not held_jobs:
        return {
            "batch": batch.to_dict(),
            "jobs_started": 0,
            "message": "No held jobs to start. Batch may already be processing."
        }

    # Transition all held jobs to queued (atomic transaction)
    for job in held_jobs:
        job.status = 'queued'

    # Transition batch to processing
    batch.status = 'processing'

    db.session.commit()

    return {
        "batch": batch.to_dict(),
        "jobs_started": len(held_jobs),
        "message": f"Started processing {len(held_jobs)} job(s)"
    }
