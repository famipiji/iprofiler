# Batches UI - Implementation Summary

**Date Completed:** 2026-09-14
**Status:** ✅ Core UI complete, placeholder actions for Phase 3

## Overview

Built the Batches list screen as a new view in the iProfiler UI, positioned between "Documents" and "Doc Type Manager" in the sidebar. The view displays all batches in a table format with status pills, progress tracking, and contextual actions.

This is the **UI layer** for the batch processing foundation built in Phase 1. It provides visibility into batch status without changing the underlying pipeline.

## Files Modified

### 1. **batch_service.py** (NEW function)
- **Lines 183-210**: Added `list_batches()` function
- **Purpose**: Query batches for a tenant, ordered by newest first
- **Security note**: Includes TODO comment about tenant isolation gap (matches existing pattern)

### 2. **document_routes.py** (NEW endpoint)
- **Lines 843-862**: Added `GET /api/documents/batches` endpoint
- **Purpose**: REST API to list batches for the batches view
- **Security note**: Includes TODO comment referencing tenant isolation gap at lines 542 and 836

### 3. **templates/index.html** (4 sections modified)

#### a) Sidebar Navigation
- **Lines 893-896**: Added "Batches" nav item with grid icon SVG
- **Position**: Between "Documents" and "Doc Type Manager" as specified

```html
<div class="nav-item" id="nav-batches" onclick="switchView('batches')" role="button" tabindex="0">
  <svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/></svg>
  Batches
</div>
```

#### b) CSS Styles
- **Lines 660-676**: Added complete batches view styling

```css
#view-batches { display: none; background: var(--bg); overflow-y: auto; min-height: 100vh; flex: 1; }
.batches-table-wrap { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; overflow: hidden; }
.batches-table { width: 100%; border-collapse: collapse; }
.batches-table thead { background: var(--bg-elevated); border-bottom: 1px solid var(--border); }
.batches-table th { padding: 14px 18px; text-align: left; font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-muted); }
.batches-table tbody tr { border-bottom: 1px solid var(--border); transition: background 0.15s; }
.batches-table tbody tr:hover { background: var(--bg); }
.batches-table td { padding: 16px 18px; font-size: 14px; color: var(--text); }
.batch-status-pill { display: inline-block; padding: 5px 11px; border-radius: 12px; font-size: 11px; font-weight: 700; letter-spacing: 0.5px; text-transform: uppercase; }
.batch-status-open { background: #d1fae5; color: #065f46; }
.batch-status-processing { background: #fed7aa; color: #92400e; }
.batch-status-completed { background: #e5e7eb; color: #374151; }
.batch-status-errors { background: #fee2e2; color: #991b1b; }
.batch-action-link { color: var(--accent); text-decoration: none; font-weight: 500; transition: opacity 0.15s; }
.batch-action-link:hover { opacity: 0.7; text-decoration: underline; }
.batches-empty { padding: 40px 20px; text-align: center; color: var(--text-muted); font-size: 14px; }
```

**Status pill colors (exact as specified):**
- **Green** (`batch-status-open`): #d1fae5 background, #065f46 text
- **Orange** (`batch-status-processing`): #fed7aa background, #92400e text
- **Gray** (`batch-status-completed`): #e5e7eb background, #374151 text
- **Red** (`batch-status-errors`): #fee2e2 background, #991b1b text

#### c) HTML Structure
- **Lines 1163-1190**: Added `view-batches` div with table

```html
<div id="view-batches">
  <div class="db-wrap">
    <div class="db-header">
      <div>
        <div class="db-header-title">Batches</div>
        <div class="db-header-date">Batch upload status</div>
      </div>
    </div>
    <div class="batches-table-wrap">
      <table class="batches-table">
        <thead>
          <tr>
            <th>Batch</th>
            <th>Status</th>
            <th>Documents</th>
            <th>Created</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody id="batches-tbody"></tbody>
      </table>
      <div class="batches-empty" id="batches-empty" style="display:none;">
        No batches yet. Upload documents to create a batch.
      </div>
    </div>
  </div>
</div>
```

#### d) JavaScript Functions
- **Lines 6133-6212**: Added complete batches view logic

**Main functions:**

1. **`loadBatches()`** - Fetches and renders batches
   - Calls `GET /api/documents/batches`
   - Handles empty state and errors
   - Populates table with batch data

2. **`renderBatchStatus(status, failedCount)`** - Renders status pills
   - Maps batch status to pill class and text
   - Shows "COMPLETE, N FAILED" for `completed_with_errors`

3. **`renderDocProgress(completed, total, status)`** - Renders document progress
   - Shows "340 / 340" for completed batches
   - Shows "212 / 500+" for processing/open batches

4. **`renderBatchAction(status, batchId)`** - Renders contextual actions
   - "Add files" for `open` status
   - "View dashboard" for `processing` status
   - "View results" for `completed` / `completed_with_errors` status

5. **`formatBatchDate(isoStr)`** - Formats timestamp
   - Example: "14 Sep 2026 15:23"
   - Uses `en-GB` locale for consistency with dashboard

6. **Placeholder action handlers** (Phase 3):
   - `addFilesToBatch(batchId)` - Shows alert (not yet implemented)
   - `viewBatchDashboard(batchId)` - Shows alert (not yet implemented)
   - `viewBatchResults(batchId)` - Shows alert (not yet implemented)

#### e) View Switcher Integration
- **Lines 5868, 5876, 5887-5889**: Updated `switchView()` function

```javascript
var batchesEl   = document.getElementById('view-batches');
// ...
batchesEl.style.display   = 'none';
// ...
} else if (view === 'batches') {
  batchesEl.style.display = 'block';
  loadBatches();
```

## API Contract

### `GET /api/documents/batches`

**Query Parameters:**
- `tenant_id` (optional, default: "default")

**Response:**
```json
{
  "success": true,
  "batches": [
    {
      "id": 123,
      "client_id": "default",
      "source": "manual_upload",
      "status": "processing",
      "total_jobs": 500,
      "completed_count": 212,
      "failed_count": 3,
      "split_pending_count": 0,
      "notify_email": null,
      "created_at": "2026-09-14T15:23:45Z",
      "completed_at": null
    }
  ]
}
```

## Table Columns

| Column | Description | Example |
|--------|-------------|---------|
| **Batch** | Batch ID (prefixed with #) | #123 |
| **Status** | Status pill with color and text | 🟢 OPEN, 🟠 PROCESSING, ⚪ COMPLETE, 🔴 COMPLETE, 3 FAILED |
| **Documents** | Progress: completed / total (+ if still processing) | 212 / 500+ or 340 / 340 |
| **Created** | Formatted timestamp | 14 Sep 2026 15:23 |
| **Action** | Contextual link based on status | "Add files", "View dashboard", "View results" |

## Status Transitions and UI Mapping

| Batch Status | Pill Color | Pill Text | Documents Format | Action Link |
|--------------|------------|-----------|------------------|-------------|
| `open` | Green | OPEN | N / M+ | Add files |
| `processing` | Orange | PROCESSING | N / M+ | View dashboard |
| `completed` | Gray | COMPLETE | N / N | View results |
| `completed_with_errors` | Red | COMPLETE, X FAILED | N / N | View results |

## What Works Now

✅ Batches nav item in sidebar (positioned between Documents and Doc Type Manager)
✅ Clicking nav item switches to batches view
✅ Fetches real batch data from backend via `/api/documents/batches`
✅ Displays all batches in table format
✅ Status pills with correct colors and text
✅ Document progress shows "N / M+" for in-progress, "N / N" for complete
✅ Action links show contextually based on batch status
✅ Empty state when no batches exist
✅ Error handling if API fetch fails
✅ Date formatting consistent with dashboard (en-GB locale)
✅ Hover states on table rows

## Not Yet Implemented (Phase 3)

The three action handlers currently show placeholder alerts:

1. **"Add files"** (`addFilesToBatch(batchId)`)
   - Future: Show file upload dialog
   - On upload, add files to existing batch (requires backend support)

2. **"View dashboard"** (`viewBatchDashboard(batchId)`)
   - Future: Navigate to batch-specific dashboard
   - Show real-time progress via SSE
   - Display per-document status, errors, retry attempts

3. **"View results"** (`viewBatchResults(batchId)`)
   - Future: Navigate to results view
   - Show all extracted documents
   - Filter/export completed batch results

## Testing the UI

### Prerequisites
- Phase 1 must be complete (batch_service.py, migration applied)
- At least one batch must exist in the database

### Create Test Batch
```bash
# Option 1: Upload a document via existing UI (creates batch of size 1)
# Navigate to http://localhost:5000 → Upload → Select file

# Option 2: Use test_batch_worker.py to create test batches
python test_batch_worker.py insert_many 5
```

### Verify UI
1. Navigate to http://localhost:5000
2. Click "Batches" in sidebar (between Documents and Doc Type Manager)
3. Verify:
   - Table shows batches ordered by newest first
   - Status pills have correct colors
   - Documents column shows progress
   - Action links appear based on status
   - Clicking action shows placeholder alert

### Test Empty State
```sql
-- Temporarily hide batches
DELETE FROM batches WHERE client_id = 'default';
```
- Refresh batches view
- Should show: "No batches yet. Upload documents to create a batch."

## Security Notes

Both `list_batches()` in batch_service.py and the `/batches` endpoint include security warnings:

```python
# Security note / TODO:
#     This uses the same "default" tenant_id fallback as the upload endpoint
#     (document_routes.py:542) and get_all_documents (document_routes.py:836).
#     This is NOT real tenant isolation - it's a shared literal string.
#     Before any multi-client production deployment, proper authentication and
#     session-based tenant scoping must be added across ALL endpoints:
#     - /api/documents (currently returns all documents, no tenant filter)
#     - /api/documents/upload (accepts tenant_id from untrusted form data)
#     - /api/documents/batches (this endpoint)
```

This matches the existing pattern in the codebase (not a regression), but must be addressed before multi-tenant production use.

## Code Reuse

The batches view follows existing iProfiler UI patterns:

- **Layout**: Uses `.db-wrap` pattern from dashboard (view-dashboard)
- **Table styles**: Based on existing table conventions (view-review)
- **Status pills**: Similar to document type badges in dashboard
- **Date formatting**: Same `en-GB` locale as dashboard activity feed
- **View switching**: Same `switchView()` pattern as other views
- **Fetch pattern**: Same `async/await` + error handling as `loadDashboard()`

## Performance Characteristics

**Frontend:**
- Single fetch on view load (no polling)
- Table renders via `innerHTML` map (fast for <1000 batches)
- No pagination yet (relies on backend limit=100)

**Backend:**
- `list_batches()` uses `Batch.query.filter_by().order_by().limit()`
- Indexed query on `client_id` (if index exists on batches table)
- Returns JSON via `Batch.to_dict()` (no N+1 queries)

**Future optimization (if >100 batches):**
- Add pagination (offset/limit query params)
- Add filtering by status
- Add search by batch ID

## Next Steps (Phase 3 - Dashboard)

### 1. Batch Dashboard View
- Real-time progress via SSE (like upload progress)
- Per-document status grid
- Error log for failed jobs
- Retry controls for dead-letter jobs

### 2. "Add Files" Flow
- File upload dialog when clicking "Add files"
- Append to existing batch (requires `add_files_to_batch()` in batch_service.py)
- Transition batch from `open` → `processing` when done

### 3. "View Results" Flow
- Navigate to filtered document list (all docs from batch)
- Export options (CSV, ZIP download)
- Batch statistics (avg confidence, doc type breakdown)

### 4. Email Notifications
- Implement `notify_email` handling when batch completes
- Send summary: N succeeded, M failed, link to results

### 5. Batch Actions
- Cancel batch (mark all pending jobs as cancelled)
- Retry all failed jobs
- Delete batch (cascade delete to jobs and source files)

## Troubleshooting

**Batches view is blank:**
- Check browser console for fetch errors
- Verify Flask app is running on port 5000
- Check `/api/documents/batches` returns JSON: `curl http://localhost:5000/api/documents/batches`

**No batches showing despite database records:**
- Verify `client_id='default'` in database: `SELECT * FROM batches WHERE client_id='default'`
- Check `list_batches()` is filtering correctly
- Verify `Batch.to_dict()` is serializing correctly

**Status pills not colored:**
- Check CSS loaded (view page source, search for `.batch-status-open`)
- Check status value in database matches enum: `open`, `processing`, `completed`, `completed_with_errors`

**Action links not working:**
- Expected behavior: placeholder alerts for Phase 3 actions
- If no alert shows, check browser console for JS errors

## References

- **Phase 1 Summary**: `.tasks/PHASE1-IMPLEMENTATION-SUMMARY.md`
- **Phase 1 Tasks**: `.tasks/2026-09-14-batch-processing-phase1.md`
- **Architecture Doc**: `claude/2026-09-14-batch-processing-architecture.md` (if exists)
- **Existing UI Patterns**: `templates/index.html` (dashboard at lines 5949-6131)
- **Tenant Isolation Gap**: `document_routes.py:542`, `document_routes.py:836`

## Verification Checklist

- ✅ `list_batches()` added to batch_service.py with security warning
- ✅ `/api/documents/batches` endpoint added to document_routes.py
- ✅ "Batches" nav item added to sidebar (between Documents and Doc Type Manager)
- ✅ CSS styles added for table, status pills, and empty state
- ✅ HTML structure added for view-batches div
- ✅ JavaScript functions added: loadBatches, renderBatchStatus, renderDocProgress, renderBatchAction, formatBatchDate
- ✅ switchView() updated to handle 'batches' case
- ✅ Status pills use exact specified colors (green, orange, gray, red)
- ✅ Documents column shows "N / M+" for processing, "N / N" for complete
- ✅ Action links contextual based on status
- ✅ Empty state displays when no batches exist
- ✅ Error handling for failed fetch

---

**Total Implementation Time:** ~1 hour (backend endpoint + frontend UI)
**Lines Added:** ~150 (batch_service.py: 28, document_routes.py: 20, templates/index.html: 100+)
**Files Modified:** 3 (batch_service.py, document_routes.py, templates/index.html)
**New Dependencies:** None (uses existing fetch API, no new libraries)
