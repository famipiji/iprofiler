# Phase 1 Batch Processing - Implementation Summary

**Date Completed:** 2026-09-14
**Status:** ✅ All tasks complete and verified

## Overview

Phase 1 introduces the batch processing foundation for iProfiler without changing user-visible behavior. The existing single-document upload now flows through the batch pipeline (as a batch of size 1), creating persistent records in the database that survive restarts.

## Files Created/Modified

### New Files (7)
1. **`batch_service.py`** (180 lines) - Service layer for batch operations
2. **`batch_worker.py`** (195 lines) - Standalone worker process with SKIP LOCKED
3. **`test_batch_service.py`** (238 lines) - Unit tests for batch_service
4. **`test_batch_worker.py`** (189 lines) - Integration tests for worker
5. **`migrations/versions/001_add_batch_processing_tables.py`** (89 lines) - DB migration
6. **`migrations/env.py`** - Flask-Migrate environment
7. **`migrations/script.py.mako`** - Flask-Migrate template

### Modified Files (4)
1. **`document_model.py`** - Added 3 new models (Batch, SourceFile, DocumentJob)
2. **`document_routes.py`** - Integrated batch creation + dual-write helper
3. **`app.py`** - Added Flask-Migrate integration
4. **`requirements.txt`** - Added flask-migrate==4.0.7

## Database Schema

### New Tables

#### `batches`
Tracks overall batch state and rollup counts.

```sql
CREATE TABLE batches (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    client_id VARCHAR(100) NOT NULL DEFAULT 'default',
    source VARCHAR(50) NOT NULL,  -- manual_upload | watched_folder | api
    status VARCHAR(50) NOT NULL DEFAULT 'draft',  -- draft | open | processing | completed | completed_with_errors
    total_jobs INTEGER NOT NULL DEFAULT 0,
    completed_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    split_pending_count INTEGER NOT NULL DEFAULT 0,
    notify_email VARCHAR(255),
    created_at DATETIME NOT NULL,
    completed_at DATETIME,
    INDEX ix_batches_status (status)
);
```

#### `source_files`
One row per uploaded file.

```sql
CREATE TABLE source_files (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    batch_id INTEGER NOT NULL,
    file_path VARCHAR(500) NOT NULL,
    page_count INTEGER,
    split_mode VARCHAR(50) NOT NULL DEFAULT 'none',  -- none | mrn_barcode | ...
    status VARCHAR(50) NOT NULL DEFAULT 'queued',  -- queued | splitting | split_done | failed
    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE,
    INDEX ix_source_files_batch_id (batch_id)
);
```

#### `document_jobs`
One extraction job per document (Phase 1: one per file, no splitting).

```sql
CREATE TABLE document_jobs (
    id INTEGER PRIMARY KEY AUTO_INCREMENT,
    batch_id INTEGER NOT NULL,
    source_file_id INTEGER NOT NULL,
    page_range VARCHAR(100),  -- e.g. "1-5" or NULL for whole file
    file_path VARCHAR(500) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'queued',  -- queued | claimed | processing | succeeded | failed | dead_letter
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_error TEXT,
    claimed_by VARCHAR(100),
    claimed_at DATETIME,
    priority INTEGER NOT NULL DEFAULT 0,
    result_ref INTEGER,  -- FK to documents.id
    created_at DATETIME NOT NULL,
    started_at DATETIME,
    finished_at DATETIME,
    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE,
    FOREIGN KEY (source_file_id) REFERENCES source_files(id) ON DELETE CASCADE,
    INDEX ix_document_jobs_batch_id (batch_id),
    INDEX ix_document_jobs_source_file_id (source_file_id),
    INDEX ix_document_jobs_claim_query (status, priority, created_at)  -- SKIP LOCKED composite
);
```

### Critical Index

The **composite index** `(status, priority, created_at)` on `document_jobs` is critical for the worker's SKIP LOCKED claim query performance. Without it, the claim query would do a full table scan.

## Key Components

### 1. batch_service.py

**Purpose:** Service layer for batch operations, modeled after `retention_service.py`.

**Functions:**
- `create_batch(files, source, client_id, split_mode, notify_email)` - Creates Batch + SourceFiles + DocumentJobs
- `get_batch_status(batch_id)` - Returns batch metadata and rollup counts
- `update_batch_status(batch_id)` - Recalculates rollup from actual job rows

**Key behavior:**
- Phase 1: `split_mode="none"` only (creates one job per file)
- Raises `NotImplementedError` for Phase 2 split modes
- `update_batch_status()` is idempotent - can be called repeatedly

### 2. batch_worker.py

**Purpose:** Standalone worker process that claims and processes jobs.

**Key features:**
- Worker identity: `worker-{hostname}-{pid}`
- Claim interval: 1 second when no jobs available
- SKIP LOCKED claim query (MySQL-specific syntax with subquery)
- Retry logic: exponential backoff (2^attempts, capped at 60s)
- Graceful shutdown: SIGINT/SIGTERM handlers

**Claim query:**
```python
claim_sql = """
    UPDATE document_jobs
    SET status = 'claimed', claimed_by = :worker_id, claimed_at = NOW()
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
```

**Why the subquery?** MySQL doesn't allow `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE)` directly - the subquery wraps it to satisfy the syntax requirement.

### 3. document_routes.py Integration

**Modified `upload_documents()` route:**
1. Saves files to disk (existing orphan-cleanup logic preserved)
2. Calls `batch_service.create_batch()` to create DB rows
3. Builds `job_id_map = {file_index: DocumentJob.id}`
4. Stores `batch_id` and `job_id_map` in in-memory `jobs[job_id]` dict
5. Launches background threads (existing async behavior unchanged)

**New `_update_document_job_status()` helper:**
- Dual-write: syncs DocumentJob table with in-memory job dict
- Sets `attempts=1` even on synchronous path (worker expects `attempts>0` on terminal jobs)
- Calls `batch_service.update_batch_status()` on terminal states

**Modified `process_file_task()`:**
- Three sync points:
  1. After status → "processing"
  2. After successful save → "succeeded" with `result_ref`
  3. On exception → "failed" with `last_error`

## Job State Transitions

### Normal Flow (Success)
```
queued → claimed → processing → succeeded
         ↑                      ↓
         └─ worker claims      result_ref set
```

### Retry Flow (Transient Failure)
```
queued → claimed → processing → failed (attempt 1)
                                ↓
         requeue after backoff  ↓
                                ↓
queued → claimed → processing → failed (attempt 2)
                                ↓
         requeue after backoff  ↓
                                ↓
queued → claimed → processing → failed (attempt 3)
                                ↓
                           dead_letter
```

### Batch Status Transitions
```
draft → open (when first job is created)
      ↓
processing (when first job succeeds/fails, but some still pending)
      ↓
completed (all jobs succeeded)
   OR
completed_with_errors (all jobs done, but some failed/dead_letter)
```

## Testing

### Unit Tests (test_batch_service.py)
8 tests covering `batch_service.py`:
- ✅ Create batch with 3 files
- ✅ Reject empty files list
- ✅ Reject unsupported split modes
- ✅ Get batch status (existing batch)
- ✅ Get batch status (nonexistent batch)
- ✅ Update batch status → completed
- ✅ Update batch status → completed_with_errors
- ✅ Update batch status → processing

### Integration Tests (test_batch_worker.py)
Commands:
- `python test_batch_worker.py insert` - Create 1 test job
- `python test_batch_worker.py insert_many N` - Create N test jobs
- `python test_batch_worker.py check` - View batch/job status
- `python test_batch_worker.py check_concurrency` - Verify SKIP LOCKED
- `python test_batch_worker.py cleanup` - Delete test batches

**SKIP LOCKED verification:**
1. Insert 10 jobs
2. Run 2 workers concurrently
3. Check that each job has exactly one `claimed_by` value
4. Result: ✅ No double-claims detected

## Known Limitations (Phase 1)

1. **Split modes not implemented** - Only `split_mode="none"` works
2. **No dashboard UI** - Batch status only visible in DB
3. **No notifications** - `notify_email` field exists but unused
4. **Worker not daemonized** - Run manually with `python batch_worker.py`
5. **No priority support** - All jobs have `priority=0`
6. **Synchronous path dominates** - Upload still processes files inline (worker is ready but not required)

## Running the Worker

### Manual (for testing)
```bash
python batch_worker.py
```

Worker will:
- Poll for jobs every 1 second
- Process jobs one at a time
- Log to stdout
- Exit gracefully on Ctrl+C

### Production (future - not in Phase 1)
Will be wired into:
- **systemd** (Linux) - auto-restart on crash
- **Docker Compose** - run as a service alongside Flask app
- **Multiple instances** - SKIP LOCKED enables horizontal scaling

## Verification Checklist

- ✅ All modules import successfully
- ✅ 8/8 unit tests pass
- ✅ Database tables and indexes created
- ✅ Batch creation works (3 files → 3 jobs)
- ✅ Worker claims jobs correctly
- ✅ SKIP LOCKED prevents double-claims
- ✅ Retry logic works (exponential backoff)
- ✅ Batch status updates on job completion
- ✅ `attempts=1` set on synchronous path
- ✅ Upload flow unchanged (SSE, response shape)

## Next Steps (Phase 2+)

### Phase 2: Split Step
- Implement `split_mode="mrn_barcode"`
- Add barcode detection logic
- Create multiple DocumentJobs per SourceFile

### Phase 3: Dashboard
- Batch list view (filter by status)
- Real-time progress via SSE
- Email notifications

### Phase 4: Barcode Decoder Fix
- Improve MRN barcode detection accuracy
- Handle rotated/skewed images

### Phase 5: Bulk Upload UI
- Multi-file drag-and-drop
- Preview before submission
- Batch configuration (split mode, priority)

## Migration Notes

**Upgrading:**
```bash
flask db upgrade
```

**Downgrading:**
```bash
flask db downgrade
```

**Migration is idempotent** - uses `CREATE TABLE IF NOT EXISTS` to handle race with `db.create_all()` in `app.py`.

## Code Reuse

The worker reuses existing pipeline code:
- `process_document()` from `qwen_service.py`
- `_save_document_to_db()` from `document_routes.py`
- `_init_lm_progress()` for progress callbacks (suppressed in worker)

**No duplication** - both synchronous path (Task 3) and async worker (Task 4) call the same extraction functions.

## Performance Characteristics

**Database:**
- Composite index on `(status, priority, created_at)` makes claim query O(1)
- SKIP LOCKED eliminates lock contention between workers
- Batch rollup uses aggregate queries (count by status)

**Worker:**
- 1-second poll interval when idle
- Exponential backoff prevents retry storms (2s, 4s, 8s, ...)
- Graceful shutdown prevents job loss

**Concurrency:**
- Multiple workers can run safely (SKIP LOCKED)
- Each worker processes one job at a time (sequential)
- Future: could parallelize within a worker (ThreadPoolExecutor)

## Troubleshooting

**Worker not claiming jobs:**
- Check job status: `SELECT * FROM document_jobs WHERE status='queued'`
- Check worker logs for errors
- Verify database connection (same DSN as Flask app)

**Jobs stuck in "claimed" status:**
- Worker crashed mid-processing
- Manual fix: `UPDATE document_jobs SET status='queued', claimed_by=NULL WHERE status='claimed'`

**Batch status not updating:**
- Call `batch_service.update_batch_status(batch_id)` manually
- Check job statuses: some may still be pending

## References

- Task file: `.tasks/2026-09-14-batch-processing-phase1.md`
- Architecture doc: `claude/2026-09-14-batch-processing-architecture.md`
- SKIP LOCKED pattern: `retention_service.py` (lines 138-220)
- Retry pattern: `qwen_service.py` (lines 50-100)
