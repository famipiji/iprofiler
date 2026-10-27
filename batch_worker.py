"""
Batch processing worker - claims and processes DocumentJob rows using SKIP LOCKED.

Standalone worker process that can run as a daemon/service. Multiple workers can
run concurrently without double-claiming jobs, using the same SKIP LOCKED pattern
as retention_service.py's distributed purge lock.

Usage:
    python batch_worker.py

To run multiple workers concurrently (for testing SKIP LOCKED coordination):
    python batch_worker.py &
    python batch_worker.py &
"""

import os
import sys
import time
import signal
import socket
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

from app import app
from extensions import db
from document_model import DocumentJob, Batch
import batch_service
from qwen_service import process_document, _init_lm_progress
from document_routes import _save_document_to_db


# Worker identity for claim tracking
WORKER_ID = f"worker-{socket.gethostname()}-{os.getpid()}"

# How long to sleep when no jobs are available (seconds)
CLAIM_POLL_INTERVAL = 1.0

# Maximum time to allow a single job to run before timing out (seconds)
# This is a hard limit on the entire process_document() call to prevent
# jobs from hanging indefinitely on unresponsive LLM servers or infinite loops.
# 15 minutes = 900 seconds (covers multiple API calls + retries)
JOB_TIMEOUT_SECONDS = 900

# Whether the worker should keep running (set to False on SIGINT/SIGTERM)
_running = True


def _signal_handler(signum, frame):
    """Graceful shutdown on SIGINT/SIGTERM."""
    global _running
    print(f"[{WORKER_ID}] Received signal {signum}, shutting down gracefully...")
    _running = False


def claim_job() -> DocumentJob | None:
    """
    Claim one queued job using SKIP LOCKED for distributed coordination.

    Returns the claimed DocumentJob, or None if no jobs are available.

    MySQL SKIP LOCKED prevents multiple workers from blocking on the same row -
    if worker A is locking job 1, worker B's query skips it and claims job 2
    instead of waiting for A to release the lock.
    """
    # MySQL requires a subquery to reference the same table in UPDATE...WHERE id IN (SELECT...)
    # https://dev.mysql.com/doc/refman/8.0/en/update.html
    claim_sql = """
        UPDATE document_jobs
        SET status = 'claimed',
            claimed_by = :worker_id,
            claimed_at = NOW()
        WHERE id = (
            SELECT id FROM (
                SELECT id FROM document_jobs
                WHERE status = 'queued'
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            ) AS subquery
        )
    """

    try:
        result = db.session.execute(db.text(claim_sql), {"worker_id": WORKER_ID})
        db.session.commit()

        # If UPDATE affected 1 row, we claimed a job - fetch it
        if result.rowcount == 1:
            # The UPDATE changed status to 'claimed' but we need the full row.
            # Query again (status='claimed', claimed_by=WORKER_ID) to get it.
            job = DocumentJob.query.filter_by(
                status="claimed",
                claimed_by=WORKER_ID
            ).order_by(DocumentJob.claimed_at.desc()).first()
            return job
        else:
            return None
    except Exception as e:
        print(f"[{WORKER_ID}] Claim failed: {e}")
        db.session.rollback()
        return None


def process_job(job: DocumentJob) -> None:
    """
    Process a claimed job: run OCR + extraction, save result, update job status.

    On success: mark 'succeeded', store result_ref, update batch.
    On failure: increment attempts, requeue if under max_attempts, else dead_letter.
    """
    job_id = job.id
    file_path = job.file_path
    batch_id = job.batch_id

    print(f"[{WORKER_ID}] Processing job {job_id}: {file_path}")

    # Update status to 'processing'
    job.status = "processing"
    job.started_at = datetime.utcnow()
    job.attempts += 1
    db.session.commit()

    try:
        # Run extraction pipeline with hard timeout to prevent indefinite hangs
        # Suppress progress callbacks - worker doesn't have an SSE stream
        _init_lm_progress(lambda *args, **kwargs: None)

        # Wrap process_document in a timeout executor
        # This prevents jobs from hanging forever on unresponsive LLM servers,
        # infinite loops, or other deadlocks that the per-API-call timeout doesn't catch
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(process_document, file_path)
            try:
                result = future.result(timeout=JOB_TIMEOUT_SECONDS)
            except FuturesTimeoutError:
                raise Exception(
                    f"Job timed out after {JOB_TIMEOUT_SECONDS}s "
                    f"(LLM server unresponsive or processing too slow)"
                )

        # Determine tenant_id from batch (batch.client_id is the tenant)
        batch = Batch.query.get(batch_id)
        tenant_id = batch.client_id if batch else "default"

        # Extract original filename from file_path
        original_filename = os.path.basename(file_path)
        unique_filename = original_filename
        ext = original_filename.rsplit(".", 1)[1].lower() if "." in original_filename else "pdf"

        # Save to documents table (reusing document_routes helper)
        doc_dict = _save_document_to_db(
            result,
            original_filename,
            unique_filename,
            ext,
            tenant_id=tenant_id,
            document_class="production",
            force_document_type=None
        )

        # Mark job as succeeded
        job.status = "succeeded"
        job.result_ref = doc_dict.get("id")
        job.finished_at = datetime.utcnow()
        job.last_error = None
        db.session.commit()

        print(f"[{WORKER_ID}] Job {job_id} succeeded -> document {doc_dict.get('id')}")

        # Update batch status
        batch_service.update_batch_status(batch_id)

    except Exception as e:
        error_msg = str(e)
        print(f"[{WORKER_ID}] Job {job_id} failed (attempt {job.attempts}/{job.max_attempts}): {error_msg}")

        # Retry logic with exponential backoff
        if job.attempts < job.max_attempts:
            # Requeue for retry
            job.status = "queued"
            job.last_error = error_msg
            job.claimed_by = None
            job.claimed_at = None
            db.session.commit()

            # Exponential backoff: wait before claiming next job
            backoff_seconds = min(2 ** job.attempts, 60)  # Cap at 60 seconds
            print(f"[{WORKER_ID}] Job {job_id} requeued, backing off {backoff_seconds}s")
            time.sleep(backoff_seconds)
        else:
            # Max attempts reached - dead letter
            job.status = "dead_letter"
            job.last_error = error_msg
            job.finished_at = datetime.utcnow()
            db.session.commit()

            print(f"[{WORKER_ID}] Job {job_id} moved to dead_letter after {job.attempts} attempts")

            # Update batch status (dead_letter counts as failed)
            batch_service.update_batch_status(batch_id)


def worker_loop():
    """Main worker loop: claim jobs and process them until shutdown signal."""
    print(f"[{WORKER_ID}] Worker started, polling for jobs every {CLAIM_POLL_INTERVAL}s")

    while _running:
        try:
            job = claim_job()

            if job:
                process_job(job)
            else:
                # No jobs available - sleep before next poll
                time.sleep(CLAIM_POLL_INTERVAL)

        except KeyboardInterrupt:
            # Graceful shutdown on Ctrl+C
            print(f"[{WORKER_ID}] KeyboardInterrupt received, shutting down...")
            break
        except Exception as e:
            print(f"[{WORKER_ID}] Worker loop error: {e}")
            db.session.rollback()
            time.sleep(CLAIM_POLL_INTERVAL)

    print(f"[{WORKER_ID}] Worker stopped")


def main():
    """Entry point: set up signal handlers and start worker loop."""
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Run worker loop inside Flask app context (for db.session access)
    with app.app_context():
        worker_loop()


if __name__ == "__main__":
    main()
