# Phase 3 Batch Actions - Implementation Summary

**Date Completed:** 2026-09-15
**Status:** ✅ All 4 batch management actions complete

## Overview

Completed Phase 3 of the batch processing system by implementing all interactive batch management actions. Users can now retry failed jobs, cancel in-progress batches, view extracted documents, and add more files to open batches.

This completes the full batch processing feature set:
- **Phase 1** (2026-09-14): Database schema, batch_service, batch_worker with SKIP LOCKED
- **Phase 2** (2026-09-15 AM): Batches list UI + real-time dashboard with SSE
- **Phase 3** (2026-09-15 PM): Interactive batch management actions (this document)

## Features Implemented

### 1. Retry Failed Jobs ✅
**Commit:** `cb97434`

Resets all failed and dead-letter jobs in a batch back to queued status, allowing the worker to retry them.

**Backend (batch_service.py):**
```python
def retry_failed_jobs(batch_id: int, client_id: str = "default") -> Optional[dict]:
    # Find all failed/dead_letter jobs
    failed_jobs = DocumentJob.query.filter_by(batch_id=batch_id)\
                                   .filter(DocumentJob.status.in_(['failed', 'dead_letter']))\
                                   .all()

    # Reset each job
    for job in failed_jobs:
        job.status = 'queued'
        job.attempts = 0
        job.last_error = None
        job.claimed_by = None
        # ... reset timestamps

    # Transition batch back to processing
    if batch.status == 'completed_with_errors':
        batch.status = 'processing'

    update_batch_status(batch_id)
```

**API Endpoint:**
- `POST /api/documents/batches/<id>/retry`
- Returns: `{success, batch, jobs_reset, message}`

**Frontend:**
- "Retry failed jobs" button appears when:
  - `batch.status == 'completed_with_errors'`
  - `batch.failed_count > 0`
- Confirmation dialog before retry
- Button shows "Retrying..." loading state
- Dashboard updates via SSE after retry
- Success/error message via alert

### 2. Cancel Batch ✅
**Commit:** `cb97434`

Cancels all queued jobs in a batch. Jobs already being processed complete naturally.

**Backend (batch_service.py):**
```python
def cancel_batch(batch_id: int, client_id: str = "default") -> Optional[dict]:
    # Only allow cancelling open/processing batches
    if batch.status not in ('open', 'processing'):
        return error message

    # Find all queued jobs (not yet claimed)
    queued_jobs = DocumentJob.query.filter_by(batch_id=batch_id, status='queued').all()

    # Mark as cancelled
    for job in queued_jobs:
        job.status = 'cancelled'
        job.finished_at = datetime.utcnow()

    # Recalculate batch status
    update_batch_status(batch_id)
```

**API Endpoint:**
- `POST /api/documents/batches/<id>/cancel`
- Returns: `{success, batch, jobs_cancelled, message}`

**Frontend:**
- "Cancel batch" button appears when:
  - `batch.status == 'processing'`
- Confirmation dialog: "Jobs in progress will complete, but queued jobs will be cancelled."
- Button shows "Cancelling..." loading state
- Dashboard updates via SSE after cancel
- Success/error message via alert

**CSS:**
- Added `.job-status-cancelled` pill (gray: #e5e7eb bg, #6b7280 text)
- Updated `renderJobStatus()` to handle 'cancelled' status

### 3. View Batch Results ✅
**Commit:** `97d2027`

Displays all documents extracted from a batch's jobs. Navigates to documents view filtered to this batch's output.

**Backend (batch_service.py):**
```python
def get_batch_results(batch_id: int, client_id: str = "default") -> Optional[dict]:
    # Find all job results
    job_results = DocumentJob.query.filter_by(batch_id=batch_id)\
                                   .filter(DocumentJob.result_ref.isnot(None))\
                                   .all()

    document_ids = [job.result_ref for job in job_results]

    # Fetch documents
    if document_ids:
        documents = Document.query.filter(Document.id.in_(document_ids)).all()

    return {
        "batch": batch.to_dict(),
        "documents": [d.to_dict() for d in documents],
        "total_documents": len(documents)
    }
```

**API Endpoint:**
- `GET /api/documents/batches/<id>/results`
- Returns: `{success, batch, documents[], total_documents}`

**Frontend:**
- "View results" link appears for completed batches
- Fetches documents via `/batches/<id>/results`
- Loads documents into review view (`rcAllDocs`)
- Updates breadcrumb: "All folders → Batch #X Results (N documents)"
- Breadcrumb link returns to full document list
- Works with existing document view features (search, detail modal, etc.)

### 4. Add Files to Batch ✅
**Commit:** `fa7149e`

Uploads additional files to an existing open batch, creating new jobs for the worker to process.

**Backend (batch_service.py):**
```python
def add_files_to_batch(batch_id: int, files: List[dict], client_id: str = "default") -> Optional[dict]:
    # Validate batch is open
    if batch.status != 'open':
        return error message

    # Create SourceFile + DocumentJob for each file
    for file_info in files:
        source_file = SourceFile(batch_id=batch_id, file_path=file_info['file_path'], ...)
        db.session.add(source_file)
        db.session.flush()

        job = DocumentJob(batch_id=batch_id, source_file_id=source_file.id, ...)
        db.session.add(job)
        jobs_added += 1

    # Update batch total_jobs count
    batch.total_jobs += jobs_added

    return {
        "batch": batch.to_dict(),
        "jobs_added": jobs_added,
        "message": f"Added {jobs_added} file(s) to batch"
    }
```

**API Endpoint:**
- `POST /api/documents/batches/<id>/files`
- Accepts: `multipart/form-data` with file uploads
- Saves files to `uploads/` directory with timestamp prefix
- Detects PDF page count for uploaded PDFs
- Returns: `{success, batch, jobs_added, message}`

**Frontend:**
- "Add files" link appears for batches with `status='open'`
- Creates hidden file input element
- Multiple file selection: `accept="image/*,application/pdf"`
- Confirmation dialog: "Add N file(s) to batch #X?"
- FormData upload via fetch POST
- Loading state: "Uploading N file(s)..." in table
- Success message via alert
- Auto-reloads batches list (shows updated total_jobs)

## Files Modified

### batch_service.py (+244 lines)
- Added `retry_failed_jobs()` - Reset failed jobs to queued
- Added `cancel_batch()` - Mark queued jobs as cancelled
- Added `get_batch_results()` - Fetch documents created by batch
- Added `add_files_to_batch()` - Add files to existing batch

### document_routes.py (+134 lines)
- Added `POST /batches/<id>/retry` - Retry endpoint
- Added `POST /batches/<id>/cancel` - Cancel endpoint
- Added `GET /batches/<id>/results` - Results endpoint
- Added `POST /batches/<id>/files` - File upload endpoint

### templates/index.html (+177 lines)
- Implemented `retryFailedJobs()` - Confirmation + API call + loading state
- Implemented `cancelBatch()` - Confirmation + API call + loading state
- Implemented `viewBatchResults()` - Fetch + navigate to review view
- Implemented `addFilesToBatch()` - File picker + FormData upload
- Added CSS for `.job-status-cancelled` pill
- Updated `renderJobStatus()` to handle cancelled status

## API Summary

### POST /api/documents/batches/<batch_id>/retry
**Purpose:** Reset all failed/dead_letter jobs to queued

**Request:** Empty POST (no body)

**Response:**
```json
{
  "success": true,
  "batch": {...},
  "jobs_reset": 5,
  "message": "Reset 5 failed job(s) to queued"
}
```

### POST /api/documents/batches/<batch_id>/cancel
**Purpose:** Cancel all queued jobs

**Request:** Empty POST (no body)

**Response:**
```json
{
  "success": true,
  "batch": {...},
  "jobs_cancelled": 10,
  "message": "Cancelled 10 queued job(s)"
}
```

### GET /api/documents/batches/<batch_id>/results
**Purpose:** Get all documents created by batch

**Request:** GET (no params)

**Response:**
```json
{
  "success": true,
  "batch": {...},
  "documents": [{...}, {...}],
  "total_documents": 15
}
```

### POST /api/documents/batches/<batch_id>/files
**Purpose:** Upload files to existing batch

**Request:** `multipart/form-data`
- `files`: File upload(s)
- `tenant_id`: Optional tenant ID (default: "default")

**Response:**
```json
{
  "success": true,
  "batch": {...},
  "jobs_added": 3,
  "message": "Added 3 file(s) to batch"
}
```

## UI Interactions

### Retry Failed Jobs Flow
1. User views batch dashboard with failed jobs
2. "Retry failed jobs" button visible (if `completed_with_errors` + failed_count > 0)
3. User clicks button
4. Confirmation dialog: "Retry all failed jobs? This will reset them to queued status..."
5. Button disabled, shows "Retrying..."
6. POST request to `/batches/<id>/retry`
7. Success alert with message
8. Dashboard updates via SSE (jobs change: failed → queued)
9. Worker picks up jobs and processes them

### Cancel Batch Flow
1. User views batch dashboard while processing
2. "Cancel batch" button visible (if `status=='processing'`)
3. User clicks button
4. Confirmation dialog: "Cancel this batch? Jobs in progress will complete..."
5. Button disabled, shows "Cancelling..."
6. POST request to `/batches/<id>/cancel`
7. Success alert with message
8. Dashboard updates via SSE (queued jobs change to cancelled)
9. In-progress jobs finish naturally, batch transitions to completed

### View Results Flow
1. User views batches list
2. Clicks "View results" on completed batch
3. Fetch documents from `/batches/<id>/results`
4. Navigate to review (documents) view
5. Documents loaded into `rcAllDocs`
6. Breadcrumb shows: "All folders → Batch #X Results (N documents)"
7. User can search, view details, etc. using existing features
8. Click breadcrumb link to return to all documents

### Add Files Flow
1. User views batches list
2. Clicks "Add files" on batch with `status='open'`
3. File picker opens (multiple selection, images/PDFs)
4. User selects files
5. Confirmation dialog: "Add N file(s) to batch #X?"
6. Files upload via FormData POST to `/batches/<id>/files`
7. Loading message: "Uploading N file(s)..."
8. Success alert: "Added N file(s) to batch"
9. Batches list reloads, showing updated total_jobs count
10. Worker can claim new jobs immediately

## Batch Status Transitions

**Retry affects:**
- `completed_with_errors` → `processing` (if failed jobs exist)
- Failed/dead_letter jobs → queued

**Cancel affects:**
- `open` or `processing` → recalculated (may stay processing if jobs still running)
- Queued jobs → cancelled
- Eventually → `completed` (when all claimed/processing jobs finish)

**Add files affects:**
- `draft` → `open` (if first files added)
- `open` → `open` (total_jobs increased)
- Batch must be `open` to accept files

## Testing Checklist

### Retry Failed Jobs
- ✅ Button appears only when batch has failed jobs
- ✅ Confirmation dialog before retry
- ✅ Loading state during API call
- ✅ Failed jobs reset to queued (attempts=0, error cleared)
- ✅ Batch status changes to processing
- ✅ Dashboard updates via SSE
- ✅ Worker can claim retried jobs
- ✅ Error handling if batch not found

### Cancel Batch
- ✅ Button appears only when batch is processing
- ✅ Confirmation dialog before cancel
- ✅ Loading state during API call
- ✅ Queued jobs marked as cancelled
- ✅ In-progress jobs complete normally
- ✅ Dashboard updates via SSE
- ✅ Cancelled jobs show gray pill
- ✅ Cannot cancel completed batches

### View Results
- ✅ Link appears for completed batches
- ✅ Fetches correct documents for batch
- ✅ Navigates to review view
- ✅ Breadcrumb shows batch filter
- ✅ Breadcrumb link returns to all documents
- ✅ Empty state if no documents
- ✅ Works with existing document features

### Add Files
- ✅ Link appears only for open batches
- ✅ File picker allows multiple selection
- ✅ Confirmation dialog before upload
- ✅ Files saved to uploads directory
- ✅ PDF page count detected correctly
- ✅ Jobs created in database
- ✅ Batch total_jobs updated
- ✅ Batches list reloads after upload
- ✅ Error if batch not open
- ✅ Error if no files selected

## Security Notes

All four new endpoints inherit the same tenant isolation gap as documented in previous phases:

```python
# Security note / TODO:
#     Same tenant isolation gap as list_batches() - the "default" tenant_id
#     is a shared literal string, not session-scoped. Before multi-client
#     production deployment, proper authentication must be added across ALL
#     batch endpoints.
```

This is consistent with existing endpoints and not a regression. Real tenant isolation requires:
- Session-based authentication
- Tenant ID from authenticated session (not request params)
- Row-level security enforcement across all endpoints

## Performance Notes

**Retry:**
- Single UPDATE query per job (typically <100 jobs per batch)
- Batch status recalculation (2 COUNT queries)
- Fast operation (<100ms for typical batch)

**Cancel:**
- Single UPDATE query per queued job
- Batch status recalculation
- Fast operation (<100ms for typical batch)

**View Results:**
- Single SELECT with IN clause (limited by batch size)
- No pagination yet (relies on reasonable result counts)
- Fast for <1000 documents

**Add Files:**
- File I/O: save N files to disk
- PDF page count detection (can be slow for large PDFs)
- N INSERT queries (SourceFile + DocumentJob per file)
- Consider async/background processing for large uploads

## Code Reuse

All four features follow established patterns:

- **API endpoints:** Same structure as existing batch endpoints
- **Security warnings:** Consistent TODO comments
- **Error handling:** Standard try/catch + alert pattern
- **Loading states:** Disabled buttons with "...ing" text
- **Success messages:** Alert with server message
- **SSE integration:** Dashboard auto-updates for all actions
- **Confirmation dialogs:** Consistent UX before destructive actions

## Next Steps (Future Enhancements)

### Email Notifications
- Send email when batch completes (if `notify_email` set)
- Include: success count, failed count, link to results
- Requires: email service integration (SendGrid, SES, etc.)

### Batch Statistics
- Processing time per document
- Success rate trends over time
- Document type breakdown chart
- Export batch metadata as CSV

### Advanced Retry Options
- Retry specific jobs (not all failed)
- Click job row in dashboard to retry individual job
- Increase max_attempts for specific jobs

### Batch Priority
- High/normal/low priority levels
- Workers claim high-priority batches first
- Priority queue visualization in dashboard

### Batch Scheduling
- Schedule batch to start at specific time
- Recurring batches (daily/weekly document ingestion)
- Cron-like syntax for scheduling

### Partial Cancel
- Cancel specific jobs (not all queued)
- Pause/resume batch processing
- Stop new claims without cancelling queued jobs

### Export Options
- Download batch results as ZIP
- Export extracted data as CSV/Excel
- Batch-level PDF merge (all docs → one PDF)

## Troubleshooting

**Retry button not showing:**
- Check batch.status === 'completed_with_errors'
- Check batch.failed_count > 0
- Verify renderBatchDashboard() conditional logic

**Cancel doesn't work:**
- Verify batch.status === 'processing' or 'open'
- Check for queued jobs in database
- Verify worker isn't immediately re-claiming jobs

**View results shows no documents:**
- Verify jobs have result_ref set (not NULL)
- Check Document table for matching IDs
- Verify DocumentJob.result_ref points to valid Document.id

**Add files fails:**
- Verify batch.status === 'open'
- Check uploads/ directory exists and is writable
- Verify file size within server limits
- Check browser console for FormData errors

**SSE not updating after action:**
- Verify SSE connection still active (Network tab)
- Check batch_progress endpoint for errors
- Verify update_batch_status() called after action

## References

- **Phase 1 Summary:** `.tasks/PHASE1-IMPLEMENTATION-SUMMARY.md`
- **Batches List UI:** `.tasks/BATCHES-UI-IMPLEMENTATION.md`
- **Batch Dashboard:** `.tasks/BATCH-DASHBOARD-IMPLEMENTATION.md`
- **Existing Upload Flow:** `document_routes.py:538-690` (reference pattern)
- **SSE Pattern:** `document_routes.py:695-775` (upload progress)
- **DocumentJob Model:** `document_model.py:551-592` (schema reference)

---

**Total Implementation Time:** ~3 hours (4 features: retry, cancel, view results, add files)
**Lines Added:** ~555 (batch_service.py: 244, document_routes.py: 134, templates/index.html: 177)
**Files Modified:** 3 (batch_service.py, document_routes.py, templates/index.html)
**Commits:** 3 (cb97434 retry+cancel, 97d2027 view results, fa7149e add files)
**New Dependencies:** None (reused existing Flask + SQLAlchemy)

**Phase 3 Status:** ✅ COMPLETE - All batch management actions functional!
