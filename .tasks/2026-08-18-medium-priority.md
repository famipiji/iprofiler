# Medium-Priority Backlog — iProfiler

Source: iProfiler Backlog (Notion), live query as of 2026-08-18. All 74 items below are `Priority: Medium`, `Status: To Do` — the full remaining Medium-priority backlog (every High-priority item is already closed; see `2026-08-17-tasks.md`, `2026-08-17-remaining-high-priority.md`, and `2026-08-17-final-high-priority.md`).

Grouped by target file/module, heaviest first. Given the volume, each item folds scope/line-references into its Issue Summary rather than a separate Scope line — the file and line numbers are still called out explicitly wherever the original report named them.

## Module summary

| Module | Items |
|---|---|
| `templates/index.html` | 16 |
| `qwen_service.py` | 11 |
| `document_routes.py` | 11 |
| `settings_routes.py` | 7 |
| `pdf_highlighted_service.py` | 4 |
| `document_model.py` | 3 |
| `docx_service.py` | 1 |
| `pdf_searchable_service.py` | 2 |
| `retention_service.py` | 4 |
| `Dockerfile` | 1 |
| `test_rag_tenant_isolation.py` | 2 |
| `prompt_studio_service.py` | 1 |
| `pdf_export_service.py` | 1 |
| `docker-compose.yml` | 1 |
| `pdf_service.py` | 2 |
| `ocr_service.py` | 1 |
| `ollama-entrypoint.sh` | 2 |
| `app.py` | 1 |
| `test_connection.py` | 1 |
| `rag_service.py` | 1 |
| `requirements.txt` | 1 |
| **Total** | **74** |

---

## templates/index.html

### Task 1: Bulk 'reset' delete loop is sequential and stops silently on first failure

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81fcaec8c8f8e1b315e1

**Target File:** `templates/index.html`

**Issue Summary:**
The reset-btn handler (~6002-6013) fetches all documents then awaits a DELETE per document sequentially in a for loop; if any single DELETE fails, the surrounding try/catch aborts the whole loop with just a generic 'Failed to reset' alert and no indication of partial progress. Use Promise.all/allSettled (or a dedicated bulk-delete endpoint) and report partial failures explicitly.

**Step-by-step fix logic:**
1. In the reset-btn handler (~6002-6013), replace the sequential for-loop that awaits each DELETE one at a time with Promise.allSettled over all documents so every DELETE fires concurrently instead of stopping the loop on the first rejection.
2. Inspect the array of settled results: separate fulfilled vs rejected entries so you know exactly which document IDs failed to delete.
3. Replace the single generic 'Failed to reset' alert with a summary that reports counts (e.g. 'Deleted 42/45 documents, 3 failed') and lists which documents failed.
4. If a dedicated bulk-delete endpoint exists or is feasible on the backend, prefer calling that single endpoint over N individual DELETE requests to reduce request overhead and give the server a chance to return partial-failure info in one response.
5. Keep the try/catch around the whole operation, but scope it to catching setup/network-level errors (e.g. the initial fetch of all documents), not individual DELETE failures which are now handled via allSettled.
6. Verify: trigger reset with one document ID intentionally made to fail (e.g. already deleted server-side) mixed with valid ones, and confirm all valid documents are still deleted and the UI reports the specific partial failure instead of aborting with a generic message.

---

### Task 2: Card export dropdown never closes on outside click

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81ea9a82e1b4fd28a4d7

**Target File:** `templates/index.html`

**Issue Summary:**
The .btn-card-export click handler in renderHistory (~3353-3358) toggles the export dropdown open but no document-level click listener closes it when the user clicks elsewhere, leaving it visibly open and overlapping other cards until manually interacted with again. Add a document-level click listener that closes any open dropdown when the click target is outside it.

**Step-by-step fix logic:**
1. In renderHistory's .btn-card-export click handler (~3353-3358), keep the existing toggle-open behavior but stop the click event from bubbling to document (event.stopPropagation()) when opening the dropdown.
2. Add a single document-level click listener (registered once, not per-card) that checks if an export dropdown is currently open and whether the click target is outside that dropdown element (using .contains()).
3. If the click is outside the open dropdown, close it (hide/remove the open class) and clear any stored reference to which dropdown is open.
4. Make sure this listener also closes the dropdown when a different card's export button is clicked, so only one dropdown is ever open at a time.
5. Verify: open a card's export dropdown, click elsewhere on the page (not the dropdown or its trigger), and confirm the dropdown closes; also confirm clicking a different card's export button closes the first dropdown and opens the new one.

---

### Task 3: Custom clickable div elements lack keyboard/ARIA support (settings/ivault views)

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81a4a555d9be3603e021

**Target File:** `templates/index.html`

**Issue Summary:**
Plain divs with only onclick and no role/tabindex/keyboard handler appear in the folder cards in rcRenderList (~5904), record-type rows in ivaultRenderTypes (~7923-7941), and recent-doc rows in loadDashboard (~5668). These are unreachable via keyboard and not correctly announced to screen readers. Add role='button', tabindex='0', and a keydown handler for Enter/Space, or use real <button> elements.

**Step-by-step fix logic:**
1. Locate the three plain divs with only onclick handlers: folder cards in rcRenderList (~5904), record-type rows in ivaultRenderTypes (~7923-7941), and recent-doc rows in loadDashboard (~5668).
2. For each, add role='button' and tabindex='0' attributes to make the element focusable and correctly announced by screen readers, or better, swap the div for a real <button> element with appropriate styling reset if markup allows.
3. Add a keydown event listener on each element that checks for Enter or Space key and, on match, calls preventDefault() and invokes the same handler function used by the onclick.
4. If any of these elements contain nested interactive children (e.g. an inner delete icon), ensure the keydown handler doesn't fire when focus/click originated on the nested control.
5. Add appropriate aria-label text where the visible text alone doesn't clearly convey the action (e.g. a folder card with only an icon).
6. Verify: tab through the page using only the keyboard and confirm focus visibly lands on each folder card, record-type row, and recent-doc row, and that pressing Enter or Space activates the same action as a mouse click.

---

### Task 4: Document-type change overwrites manually edited fields with no confirmation

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81559282cb930892776e

**Target File:** `templates/index.html`

**Issue Summary:**
commitDocType (~3723-3772) always re-extracts every field against the newly selected type's schema and immediately overwrites currentDocFields, including on documents where the user just hand-corrected values, with no warning even though significant data can be lost at once. Add a confirmation prompt before committing when the document has unsaved/recently manually-edited field values.

**Step-by-step fix logic:**
1. In commitDocType (~3723-3772), before re-extracting fields and overwriting currentDocFields, add a check for whether the document has unsaved or recently manually-edited field values (e.g. a dirty/edited flag tracked elsewhere in the edit flow).
2. If such edits exist, use the existing showDeleteConfirm(title, message, onConfirm) modal pattern (repurposed as a generic confirm, or its underlying confirm-modal component) to warn the user that changing document type will overwrite their manual edits, and only proceed with the re-extraction/overwrite inside the onConfirm callback.
3. If no manual edits are pending, skip the confirmation and commit the type change immediately as before, to avoid adding friction to the common case.
4. Ensure the confirmation message clearly names which fields will be overwritten if that information is readily available, so the user understands the impact.
5. Verify: manually edit a field value, then change the document type and confirm a confirmation prompt appears; confirm cancelling leaves currentDocFields untouched, and confirming proceeds with the schema re-extraction as before. Also verify changing type on a document with no manual edits skips the prompt.

---

### Task 5: Field type hint deletion has no confirmation before destructive DELETE

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca815da77ad2a8eaad1e31

**Target File:** `templates/index.html`

**Issue Summary:**
stDeleteFieldTypeHint (~6977-6982) fires a DELETE immediately on click with no confirm() dialog, unlike equivalent destructive flows elsewhere (stConfirmDelete, studioDeleteVersion, studioRollback, dtwRevert) which all require confirmation. A misclick permanently removes a taught field-type hint with no undo. Add a confirm() prompt before calling the delete endpoint.

**Step-by-step fix logic:**
1. In stDeleteFieldTypeHint (~6977-6982), locate the click handler that currently fires the DELETE request immediately.
2. Wrap the DELETE call in a confirm() dialog (or route through the same showDeleteConfirm(title, message, onConfirm) modal already used by stConfirmDelete, studioDeleteVersion, studioRollback, and dtwRevert) so the delete only proceeds after explicit user confirmation.
3. Write a clear confirmation message naming the specific field-type hint being removed, matching the tone/format used in the other confirm flows in this file for consistency.
4. Only call the delete endpoint inside the confirm/onConfirm callback; ensure the click handler no longer calls it directly.
5. Verify: click the delete control for a field-type hint and confirm a confirmation dialog appears before any network request is sent; confirm cancelling leaves the hint intact, and confirming deletes it as before.

---

### Task 6: Missing try/catch around JSON.parse in SSE onmessage handler

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81059902e1c27324f817

**Target File:** `templates/index.html`

**Issue Summary:**
In listenProgress() (2284-2321), evtSource.onmessage parses e.data via JSON.parse with no try/catch (2291-2292), unlike every other network path in the file. A malformed SSE payload throws inside the handler, silently aborting that message and leaving the queue UI stuck mid-processing with no user feedback. Wrap the parse in try/catch and call setStatus(..., 'error') on failure.

**Step-by-step fix logic:**
1. In listenProgress() (2284-2321), locate evtSource.onmessage where e.data is parsed via JSON.parse at lines 2291-2292 with no surrounding try/catch.
2. Wrap the JSON.parse(e.data) call (and any immediately dependent processing of the parsed object) in a try/catch block, consistent with every other network path in the file.
3. In the catch block, call setStatus(..., 'error') with a user-facing message indicating the progress update could not be read, instead of letting the handler throw silently.
4. Ensure the catch block does not leave the queue UI permanently stuck: either allow the SSE connection to continue listening for subsequent valid messages, or trigger the same cleanup/close logic used elsewhere on unrecoverable errors, depending on what's appropriate for a single malformed message.
5. Verify: simulate a malformed SSE payload (e.g. temporarily emit invalid JSON from a test event) and confirm the UI shows an error status via setStatus instead of the queue silently freezing with no feedback, and that subsequent valid messages are still processed.

---

### Task 7: PDF find-in-page search runs expensive layout work on every keystroke with no debounce

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81cb9fade791afd6bbe6

**Target File:** `templates/index.html`

**Issue Summary:**
The pdf-find-input listener (~7742-7746) calls highlightInPdfDom on every keystroke, rebuilding a full per-character text index and calling getBoundingClientRect on every matching span across all pages (~7675-7739). For large PDFs this causes layout thrashing and noticeable input lag. Debounce the handler (150-250ms) before running the search.

**Step-by-step fix logic:**
1. In the pdf-find-input listener (~7742-7746), remove the direct call to highlightInPdfDom on every 'input' event.
2. Introduce a debounce: store a timer variable (e.g. let findDebounceTimer), clearTimeout(findDebounceTimer) at the start of the handler, then setTimeout(() => highlightInPdfDom(...), 200) (200ms is a reasonable midpoint of the 150-250ms range) to defer the actual search until typing pauses.
3. Make sure the debounce timer variable is scoped appropriately (e.g. module/closure level) so rapid keystrokes reset the same timer rather than creating multiple overlapping timers.
4. Leave highlightInPdfDom's internal logic (text index rebuild and getBoundingClientRect calls, ~7675-7739) unchanged, since the fix is purely about call frequency, not the underlying algorithm.
5. Verify: type quickly into the PDF find input on a large multi-page PDF and confirm highlightInPdfDom now runs once after typing pauses (~200ms) rather than on every keystroke, and that input no longer visibly lags.

---

### Task 8: PDF zoom/pan handlers await renderAllPages() with no error handling

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81dea8dce7c15b170177

**Target File:** `templates/index.html`

**Issue Summary:**
The zoom-in (1844), zoom-out (1852), double-click-zoom (1892), and highlightAndZoom (1761) handlers all await renderAllPages() with no surrounding try/catch. If rendering throws for any page, the promise rejects unhandled - the click silently fails, leaving the viewer blank/partial with no error shown. Wrap these calls in try/catch and surface a status message on failure.

**Step-by-step fix logic:**
1. Identify the four call sites that await renderAllPages() with no error handling: zoom-in (1844), zoom-out (1852), double-click-zoom (1892), and highlightAndZoom (1761).
2. Wrap each `await renderAllPages()` call in a try/catch block at its call site.
3. In each catch block, surface a user-facing status message (using the app's existing status/error display mechanism, e.g. setStatus(..., 'error')) indicating the page failed to render, rather than letting the rejection go unhandled.
4. Ensure the catch block leaves the viewer in a recoverable state where possible (e.g. don't leave zoom controls disabled or state flags stuck if the render call was tracking an in-progress flag).
5. Verify: force renderAllPages() to throw (e.g. temporarily throw inside it or pass invalid state) and click each of zoom-in, zoom-out, double-click-zoom, and trigger highlightAndZoom; confirm each shows an error status instead of silently leaving the viewer blank/partial.

---

### Task 9: qaThreads retains full DOM fragments per document with no eviction

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca815c8338d001db5a1198

**Target File:** `templates/index.html`

**Issue Summary:**
The qaThreads object (line 3781) stores a detached DOM subtree plus history/state for every document opened via qaSwitchToDoc, removed only when that specific document is reopened. Documents opened once and never revisited stay retained for the page's life, causing unbounded memory growth. Cap retained threads with LRU eviction, or store serializable history instead of live DOM fragments.

**Step-by-step fix logic:**
1. Locate the qaThreads object (line 3781) and the qaSwitchToDoc function that populates and clears its entries.
2. Decide on an eviction strategy: either cap qaThreads to a fixed number of most-recently-used documents (LRU) by tracking access order and deleting the least-recently-used entry when the cap is exceeded, or stop storing live DOM fragments entirely and instead persist serializable history/state (e.g. plain JS objects/arrays of messages) that can be re-rendered into DOM on demand.
3. If implementing LRU: on each qaSwitchToDoc call, move the accessed doc's entry to the 'most recent' end of the tracking structure (e.g. a Map preserves insertion order, so delete+re-set the key to bump it), and after insertion, if size exceeds the cap, delete the oldest entry (and any DOM nodes it references, to allow garbage collection).
4. If switching to serializable state: refactor qaThreads to store only the Q&A history/state per document, and change the rendering path in qaSwitchToDoc to rebuild the DOM fragment from that state each time a document is opened, removing direct DOM storage.
5. Ensure whichever approach is chosen properly detaches/nulls out references to removed DOM subtrees so they're eligible for garbage collection, not just removed from the object's keys.
6. Verify: open many distinct documents' Q&A threads in sequence (more than the chosen cap, e.g. 20+), then check memory usage (via browser dev tools heap snapshot) or simply confirm qaThreads never grows past the cap size, and that revisiting an evicted document still correctly reloads its history.

---

### Task 10: Race condition in openModal's ODL blocks fetch when switching documents

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca810086bddf5491a433c4

**Target File:** `templates/index.html`

**Issue Summary:**
openModal's /blocks fetch (~3453) has no doc-id staleness guard, unlike refreshDocDetail, so quickly switching between documents can apply the wrong document's bounding boxes if an earlier response resolves after a later one. Guard the callback with a currentDocId check before assigning odlBlocks.

**Step-by-step fix logic:**
1. In openModal's /blocks fetch (~3453), identify the callback that assigns the fetched response to odlBlocks.
2. Before initiating the fetch, capture the currently active document ID in a local variable (e.g. const requestedDocId = currentDocId).
3. In the fetch's .then callback, add a guard that compares requestedDocId to the current value of currentDocId; if they differ (meaning the user switched documents while the request was in flight), discard the response and return early without assigning odlBlocks.
4. Mirror the same staleness-guard pattern already used in refreshDocDetail for consistency, reusing any shared helper if one exists.
5. Verify: rapidly switch between two documents with differing load times (e.g. throttle network in dev tools) and confirm the bounding boxes (odlBlocks) shown always match the currently displayed document, never a stale one from an earlier request that resolved late.

---

### Task 11: renderAllPages() rebuilds every PDF page at full resolution on each zoom step

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81489f08e6da9b808140

**Target File:** `templates/index.html`

**Issue Summary:**
renderAllPages() (1611-1681) clears and rebuilds a DPR-scaled canvas plus text layer for every page on each zoom-in/out click, double-click zoom, and highlightAndZoom call - not just visible pages. For a large multi-page PDF, each zoom click re-renders 100+ full-res HiDPI canvases synchronously, causing multi-second UI freezes and heavy memory churn. Virtualize rendering to only redraw visible pages, or cache/reuse canvases across zoom levels.

**Step-by-step fix logic:**
1. In renderAllPages() (1611-1681), identify the loop that clears and rebuilds a DPR-scaled canvas plus text layer for every page on each zoom/pan trigger, regardless of visibility.
2. Add a visibility check (e.g. using getBoundingClientRect against the viewer's scroll container bounds, or an IntersectionObserver tracking which page containers are currently in or near the viewport) so only pages currently visible (plus a small buffer of adjacent pages) are re-rendered at the new zoom level.
3. For pages not currently visible, defer their re-render: either mark them dirty and re-render lazily when they scroll into view, or keep their existing canvas at the previous zoom level until then.
4. Alternatively/additionally, cache rendered canvases per zoom level (or per a coarse set of zoom buckets) so returning to a previously-used zoom level can reuse a cached canvas instead of re-rendering from scratch.
5. Ensure the text layer regeneration follows the same virtualization: only rebuild text layers for pages actually being redrawn.
6. Verify: open a large multi-page PDF (100+ pages), perform repeated zoom-in/zoom-out clicks, and confirm the UI no longer freezes for multiple seconds per click and only the visible pages' canvases are observed being rebuilt (check via performance profiling or by instrumenting a console log per page render).

---

### Task 12: renderResult() has no null check on doc.document_type before .replace()

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca816f9546d32fbbfbd4cb

**Target File:** `templates/index.html`

**Issue Summary:**
renderResult() (2344-2362) calls doc.document_type.replace(/_/g, ' ') at line 2353 with no guard, unlike other fields in the same function which are defensively handled. If the backend ever returns a document without document_type set, this throws a TypeError mid-render, leaving the result panel half-rendered with no user-facing error. Add (doc.document_type || 'other') before calling .replace().

**Step-by-step fix logic:**
1. In renderResult() (2344-2362), find line 2353 where doc.document_type.replace(/_/g, ' ') is called without a guard.
2. Add a fallback so the code reads (doc.document_type || 'other').replace(/_/g, ' '), matching the defensive handling already used for other fields in the same function.
3. Scan the rest of renderResult() for any other direct property access on doc that lacks a similar guard and apply the same defensive pattern if found, for consistency.
4. Confirm the fallback value 'other' is appropriate for how document_type is displayed/used downstream (e.g. check if it needs title-casing or matches an existing 'other' category used elsewhere in the UI).
5. Verify: render a result object with document_type set to undefined/null and confirm the result panel renders fully (showing 'other' in place of the missing type) instead of throwing a TypeError and leaving the panel half-rendered.

---

### Task 13: saveFields collects line-item data outside its try/catch block

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca8142a0f7c4017616389c

**Target File:** `templates/index.html`

**Issue Summary:**
In saveFields (~3056-3104), DOM-scraping and JSON.parse(sec.dataset.cols) logic runs before the try block begins (~3090). Malformed dataset.cols throws uncaught out of the async click handler, leaving the UI stuck in edit mode with no error message. Move the data-collection code inside the existing try block.

**Step-by-step fix logic:**
1. In saveFields (~3056-3104), locate the DOM-scraping and JSON.parse(sec.dataset.cols) logic that currently runs before the try block begins (~3090).
2. Move all of that data-collection code (including the JSON.parse(sec.dataset.cols) call) inside the existing try block, so any parse or DOM-access error is caught by the same handler that already covers the save/fetch logic.
3. Ensure the catch block's existing error handling (status message / UI reset) also correctly resets the 'edit mode' UI state if a data-collection error occurs, not just fetch failures, since previously edit mode could get stuck on this specific failure path.
4. Double check no code after the moved block depends on it running outside try (e.g. variable hoisting/scoping) and adjust variable declarations (let/const) as needed so they remain accessible after the try block if required.
5. Verify: set a malformed value on sec.dataset.cols so JSON.parse throws, click save, and confirm the error is now caught, a user-facing error message is shown, and the UI does not get stuck in edit mode with no feedback.

---

### Task 14: Sidebar nav, drop zone and card links unusable via keyboard

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca817e8607d7c54ffe7e03

**Target File:** `templates/index.html`

**Issue Summary:**
Sidebar nav items (881-905), the upload drop zone (929), and 'View all/log' links (1105, 1130) rely solely on click handlers with no role, tabindex, or keydown handling; an <a> without href is excluded from the tab order. Keyboard-only and screen-reader users cannot navigate primary nav, open the file picker, or reach dashboard quick links. Convert these to real <button> elements or add role='button' tabindex='0' plus keydown support.

**Step-by-step fix logic:**
1. Locate the sidebar nav items (881-905), the upload drop zone (929), and the 'View all/log' links (1105, 1130) that currently rely solely on click handlers.
2. Convert the sidebar nav items and drop zone to real <button> elements where feasible (preserving existing styling via CSS class reuse), or add role='button', tabindex='0', and a keydown handler for Enter/Space if converting to <button> isn't practical.
3. For the 'View all/log' links that are <a> tags without href, either add a valid href (if they represent real navigable state/URL) so they're naturally included in tab order, or add tabindex='0' plus role='button' and a matching keydown handler if they must remain JS-only triggers.
4. For the drop zone specifically, ensure keyboard activation (Enter/Space) opens the file picker the same way a click does, since drop zones are often mouse/drag-only by default.
5. Add descriptive aria-label attributes where the visible content alone may not be sufficiently descriptive for screen-reader users (e.g. icon-only nav items).
6. Verify: tab through the sidebar, drop zone, and dashboard quick links using only the keyboard, confirming each receives visible focus and that Enter/Space activates the same behavior as a mouse click, including opening the file picker from the drop zone.

---

### Task 15: Silent failure with no user feedback when refreshing iVault record types

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81569a98c6628453d09e

**Target File:** `templates/index.html`

**Issue Summary:**
In ivaultRefreshTypes (~7798-7835), the catch handler is empty (catch(e) {}), so a network failure leaves the status text stale while the button resets with no error message, unlike the explicit message used for the non-exception failure path. Add a status message in the catch block.

**Step-by-step fix logic:**
1. In ivaultRefreshTypes (~7798-7835), locate the empty catch(e) {} block.
2. Add a status message inside the catch block (using the same status-display mechanism the function already uses for its non-exception failure path) so a network/exception failure is reported to the user the same way as the explicit failure case.
3. Ensure the button's reset-to-idle state logic still runs after showing the error (e.g. in a finally block or explicitly in the catch), so the UI doesn't end up in a broken interim state.
4. Log the caught error (e) to the console or an existing error-logging utility in addition to the user-facing message, to aid future debugging, consistent with other catch blocks in the file if they do so.
5. Verify: simulate a network failure for the refresh-types request (e.g. via dev tools offline mode or blocking the endpoint) and confirm the status text now shows a clear error message instead of silently going stale while the button resets.

---

### Task 16: SSE onerror aborts progress tracking with no retry/poll fallback

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81c38423db0cadeace30

**Target File:** `templates/index.html`

**Issue Summary:**
evtSource.onerror (2314-2320) immediately closes the EventSource and shows a connection-lost error on any error, including transient blips, with no reconnect attempt or polling fallback. A brief network drop during a multi-file upload permanently kills the progress feed even though the backend keeps processing server-side. Add a retry/backoff or fall back to polling the job status endpoint.

**Step-by-step fix logic:**
1. In evtSource.onerror (2314-2320), remove the unconditional immediate close-and-show-error behavior for every error event.
2. Add retry logic: on the first error, instead of closing immediately, wait a short backoff period (e.g. 1-2s, increasing on repeated failures) and attempt to re-establish the EventSource connection to the same progress endpoint.
3. Track a retry/attempt counter so that only after a reasonable number of consecutive failed reconnect attempts (or a max total wait time) does the handler give up and show the 'connection lost' error to the user.
4. As a fallback (or alternative primary strategy), when SSE reconnection isn't reliable, switch to polling a job-status endpoint at a fixed interval to keep the progress UI updated even without a live SSE stream, since the backend continues processing server-side.
5. Ensure that once a real connection-lost state is reached (retries exhausted), the existing error UI and cleanup logic still runs as before, so users aren't left with a spinner that never resolves.
6. Verify: simulate a transient network blip during a multi-file upload (briefly block/unblock the SSE endpoint in dev tools) and confirm the progress feed automatically recovers without showing a permanent connection-lost error, while a genuinely prolonged outage still eventually surfaces the error message.

---

## qwen_service.py

### Task 17: Ad-hoc JSON parsing drops truncated model output instead of recovering it

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca8103a9bfebf94913c5ef

**Target File:** `qwen_service.py`

**Issue Summary:**
detect_chat_intent (max_tokens=300, ~4136), detect_extraction_request and detect_add_field_request (max_tokens=150, ~4248/4315) use a regex + raw json.loads() instead of the more robust safe_parse_json helper (line 751). If the model's JSON is cut off before the closing brace, the regex finds no match, json.loads raises, and the broad except silently falls back to a default, dropping a legitimate request. Replace the ad-hoc regex+json.loads with safe_parse_json in all of these functions.

**Step-by-step fix logic:**
1. In detect_chat_intent (~4136, max_tokens=300), remove the regex-match + raw json.loads() block and replace it with a call to the existing safe_parse_json helper (line 751), passing the raw model response text.
2. Do the same replacement in detect_extraction_request (~4248, max_tokens=150) and detect_add_field_request (~4315, max_tokens=150), routing both through safe_parse_json instead of their own regex/json.loads logic.
3. Confirm safe_parse_json's recovery behavior (e.g. brace-balancing/truncation repair) is sufficient for cut-off responses at these token limits; if not, extend safe_parse_json itself rather than re-adding ad-hoc handling in the three call sites.
4. Narrow the broad except around each call so only genuine unparseable-JSON cases fall back to the default intent/request, and log the fallback with the raw response snippet so silent drops are visible.
5. Remove the now-dead regex import/usage in these three functions if it isn't used elsewhere in the module.
6. Verify: feed each of the three functions a deliberately truncated JSON response (simulate max_tokens cutoff) and confirm safe_parse_json recovers the intent/request instead of falling through to the default, with a log line only on genuine parse failure.

---

### Task 18: Add file locking/atomic writes for learned_structures.json (qwen_service)

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81fbb8a4eeb7a65e5dc1

**Target File:** `qwen_service.py`

**Issue Summary:**
save_learned_structure (~1045) reads, mutates, and rewrites learned_structures.json with plain file I/O and no locking or atomic replace. Since document_routes.py spins up a background thread per uploaded file, concurrent uploads can interleave writes and corrupt the file. Use a file lock or write-to-temp-then-os.replace for atomicity.

**Step-by-step fix logic:**
1. In save_learned_structure (~1045), replace the direct read-mutate-write of learned_structures.json with a write-to-temp-then-os.replace pattern: read current contents under a lock, mutate in memory, write the full result to a temp file in the same directory, then os.replace() the temp file over the target path.
2. Introduce a module-level threading.Lock (or a file lock if cross-process safety is also needed) guarding the entire read-modify-write sequence in save_learned_structure so concurrent background threads from document_routes.py serialize on this file.
3. Ensure the temp file is created with a unique name (e.g. tempfile.NamedTemporaryFile or a suffix with the thread id/uuid) in the same directory as learned_structures.json so os.replace stays atomic on the same filesystem.
4. Add error handling so a failed write doesn't leave a stale temp file behind, and so the lock is released (use a try/finally or context manager) even on exception.
5. Since learn_type_correction (~4869-4892) has the identical race, factor the lock + atomic-write logic into a small shared helper both functions call, rather than duplicating it.
6. Verify: spin up several concurrent threads (mirroring document_routes.py's per-upload background thread) that each call save_learned_structure with different structures, then confirm learned_structures.json is valid JSON afterward and contains all expected entries with no corruption or lost writes.

---

### Task 19: Add safeguard against runaway large-token model generations

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81478692d2e286db3c56

**Target File:** `qwen_service.py`

**Issue Summary:**
extract_fields_generic (~1400) and extract_fields_vision (~1655) request max_tokens=16000, and call_qwen_vision/call_qwen_text buffer the entire streamed response in memory. A looping/pathological local model response can hold a very large string in memory and block the thread up to the 600s timeout. Add a sanity check that aborts/truncates generation without progress within a reasonable bound.

**Step-by-step fix logic:**
1. In call_qwen_vision and call_qwen_text, add tracking of streamed-chunk progress (e.g. last-chunk timestamp and/or a rolling token/byte count) while accumulating the response buffer.
2. Define a 'no progress' timeout shorter than the overall 600s request timeout (e.g. configurable, default tens of seconds) that aborts the stream if no new chunk arrives within that window, distinct from total-duration timeout.
3. Add an upper bound on accumulated response size/length independent of max_tokens, so a pathological looping generation that keeps emitting tokens without truncation is aborted once the buffer exceeds a sane cap.
4. When either the no-progress timeout or size cap trips, cancel/close the streaming call, raise a typed error (consistent with the retry/backoff error types already used at these call sites), and log the abort reason.
5. Since extract_fields_generic (~1400) and extract_fields_vision (~1655) both request max_tokens=16000, ensure their callers handle this new typed abort error gracefully (fall back or surface a clear failure) rather than letting it propagate as an unhandled exception.
6. Verify: simulate a streaming response that stalls mid-generation (no new chunks) and one that keeps emitting tokens indefinitely; confirm both are aborted well before the 600s timeout and raise the typed error instead of blocking the thread or consuming unbounded memory.

---

### Task 20: answer_question skips its char budget for single-blob documents

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81048201e66e8d5c6560

**Target File:** `qwen_service.py`

**Issue Summary:**
In answer_question (~3770-3774), raw_text is split on blank lines to build a page list; when that yields 0 or 1 chunks (common for a single scanned page), annotated = pages[0] if pages else raw_text with no truncation, unlike the multi-page branch which applies a 14000-char budget. A large single-page OCR transcript can be sent to the model completely unbounded. Apply the same char-budget truncation in the single-page fallback branch.

**Step-by-step fix logic:**
1. In answer_question (~3770-3774), locate the branch where `pages` (from splitting raw_text on blank lines) has 0 or 1 elements and `annotated = pages[0] if pages else raw_text` is assigned with no truncation.
2. Identify the 14000-char budget constant/logic used in the multi-page branch and extract it into a small shared helper (or reuse it directly) so both branches apply identical truncation rules.
3. Apply that same char-budget truncation to `annotated` in the single-page/empty fallback branch before it is used to build the prompt sent to the model.
4. Confirm the truncation preserves the most relevant content (e.g. same truncation strategy as the multi-page path — head, tail, or budget-aware chunking) rather than a naive hard cut that could drop the answer-bearing region.
5. Verify: run answer_question against a single large scanned-page OCR transcript exceeding 14000 chars and confirm the annotated text sent to the model is truncated to the same budget as the multi-page path, with no unbounded prompt size.

---

### Task 21: Cache custom-field/doc-type DB lookups within a single document's processing

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81f6bb70fa34b1a0a4fc

**Target File:** `qwen_service.py`

**Issue Summary:**
get_custom_doc_fields (~1760) and get_all_active_doc_type_names (~1802) each open a fresh app context and issue a fresh DB query every call; within one process_document run these are invoked several times each, adding 5+ redundant DB round-trips for identical rows. Cache/memoize the result per document-processing call.

**Step-by-step fix logic:**
1. In get_custom_doc_fields (~1760) and get_all_active_doc_type_names (~1802), identify all call sites within a single process_document run to confirm they're invoked multiple times with identical arguments/results.
2. Introduce a per-document-processing cache (e.g. a dict keyed by the query's identifying arguments, scoped via a parameter passed through the call chain, a thread-local like the existing _prompt_override_ctx pattern, or an lru_cache cleared per run) so repeated calls within one process_document invocation reuse the first DB result instead of re-querying.
3. Ensure the cache is scoped per document-processing call (not global/persistent across requests), since doc-type/custom-field data can change between uploads — e.g. clear or re-key it at the start of each process_document run.
4. Update the call sites for these two functions to read from the cache first and only fall back to opening a fresh app context and querying when no cached entry exists.
5. Verify: instrument or log DB query counts for get_custom_doc_fields and get_all_active_doc_type_names during a single process_document run and confirm the redundant round-trips drop from 5+ to one query each per document.

---

### Task 22: Guard FIELD_MAP lookup in extract_fields_structured against missing keys

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81439850f4a796e3c77c

**Target File:** `qwen_service.py`

**Issue Summary:**
extract_fields_structured (~972) does fields = FIELD_MAP[document_type] with direct dict indexing and no existence check. A document_type not present in FIELD_MAP raises an unhandled KeyError deep in the extraction pipeline. Use FIELD_MAP.get(document_type) and handle the None case (fall back to generic extraction).

**Step-by-step fix logic:**
1. In extract_fields_structured (~972), replace `fields = FIELD_MAP[document_type]` with `fields = FIELD_MAP.get(document_type)`.
2. Add an explicit None check immediately after: if `fields` is None, log a warning identifying the unrecognized document_type.
3. In the None case, fall back to generic extraction (reuse the existing generic extraction path, e.g. extract_fields_generic) instead of letting the pipeline continue with an undefined `fields` variable or raising KeyError.
4. Confirm downstream code that consumes `fields` handles the generic-extraction fallback's return shape consistently with the FIELD_MAP-driven path (same field-list contract).
5. Verify: call extract_fields_structured with a document_type not present in FIELD_MAP and confirm it falls back to generic extraction with a logged warning instead of raising an unhandled KeyError.

---

### Task 23: is_new_document_boundary bypasses the shared call_qwen_vision wrapper

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca8142bd58ef14e9558d29

**Target File:** `qwen_service.py`

**Issue Summary:**
is_new_document_boundary (~3057-3066) builds its own message list and calls the client directly instead of going through call_qwen_vision, duplicating logic and skipping the shared SSE progress-streaming path every other vision call uses. Extend call_qwen_vision to accept multiple images and route this call through it.

**Step-by-step fix logic:**
1. Review call_qwen_vision's current signature and confirm it accepts a single image; extend it to accept a list of images (or an optional multi-image parameter) while keeping single-image callers backward compatible.
2. Ensure the extended call_qwen_vision still applies the shared SSE progress-streaming path and the retry-with-backoff wrapper already in place around this low-level client call site for every invocation, including multi-image ones.
3. In is_new_document_boundary (~3057-3066), remove the ad-hoc message-list construction and direct client call, and instead call the extended call_qwen_vision with the multiple boundary-comparison images.
4. Update any prompt/message formatting specific to is_new_document_boundary's boundary-detection logic to be passed as parameters into call_qwen_vision rather than duplicated inline.
5. Verify: run is_new_document_boundary against a known page pair and confirm it returns the same boundary decision as before, now via call_qwen_vision, with SSE progress events emitted and retry/backoff active on transient failures — no direct client calls remaining in this function.

---

### Task 24: N+1 DB query in classify_single_page during PDF page classification

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca816382a3fa18b50bc902

**Target File:** `qwen_service.py`

**Issue Summary:**
classify_single_page (~2989) calls get_all_active_doc_type_names(), which opens a fresh app context and queries DocumentType, on every invocation - once per page inside process_split_pdf's loop (~3228-3236) and detect_separator_groups' classification path (~3457, 3481). An N-page PDF issues N identical DB round-trips for the same prompt list. Query the active document type names once per split job and pass the list into classify_single_page.

**Step-by-step fix logic:**
1. Identify the call sites feeding classify_single_page (~2989): process_split_pdf's per-page loop (~3228-3236) and detect_separator_groups' classification path (~3457, 3481).
2. Call get_all_active_doc_type_names() once at the start of the split job (e.g. before the page loop begins in process_split_pdf, and once before the classification path in detect_separator_groups), storing the result in a local variable.
3. Change classify_single_page's signature to accept the doc-type-names list as a parameter instead of calling get_all_active_doc_type_names() internally.
4. Update all call sites (the per-page loop and detect_separator_groups' classification path) to pass the pre-fetched list into classify_single_page on every invocation.
5. Remove the now-unused internal call to get_all_active_doc_type_names() from classify_single_page.
6. Verify: process a multi-page PDF through process_split_pdf and detect_separator_groups, and confirm (e.g. via query logging/count) that get_all_active_doc_type_names is invoked once per split job rather than once per page.

---

### Task 25: Narrow exception handling in vision resolution fallback loop

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca8172a632ff9de1067101

**Target File:** `qwen_service.py`

**Issue Summary:**
In process_document's image branch (~2602-2617), the resolution retry loop only re-raises when the exception string doesn't contain '400' or 'failed to process image', catching any exception type by string-matching, so a transport-level error is silently swallowed and retried, masking the true root cause. Catch specific exception types instead of string-matching on the message.

**Step-by-step fix logic:**
1. In process_document's image branch (~2602-2617), identify the resolution retry loop's except clause that string-matches on '400' or 'failed to process image' in the exception message.
2. Determine the actual exception types raised by the underlying client for (a) the image-too-large/invalid-resolution case that should trigger a retry at lower resolution, versus (b) transport-level errors (connection/timeout) that should not be silently retried under this logic.
3. Replace the string-matching condition with explicit except blocks (or isinstance checks) catching only the specific exception type(s) that correspond to the resolution-failure case, letting all other exception types propagate/re-raise immediately.
4. If the client library doesn't distinguish these cleanly via type, wrap the resolution-specific failure in a dedicated exception class at the point it's raised so this loop can catch it precisely instead of pattern-matching text.
5. Ensure the loop still logs the original exception before any retry so root-cause visibility isn't lost.
6. Verify: trigger both a genuine resolution/400-class failure (confirm it still retries at lower resolution) and a simulated transport-level error (confirm it now propagates/fails fast instead of being swallowed and retried) in this branch.

---

### Task 26: No page-count cap on split-PDF pipelines risks memory/latency blowup

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca811098b5f53bae914e7b

**Target File:** `qwen_service.py`

**Issue Summary:**
detect_separator_groups' barcode-scan path (~3429-3439) holds every rendered page as a PIL Image in memory before discarding any, and its AI-classification fallback buffers every page's base64 encoding; process_split_pdf/process_barcode_split_pdf then run one LLM call per page sequentially with no page-count bound. A large PDF can cause significant memory pressure and a request blocking for many minutes. Add a configurable max-page guard and stream/discard page images as processed.

**Step-by-step fix logic:**
1. In detect_separator_groups' barcode-scan path (~3429-3439), change the page-rendering loop so each page's PIL Image is processed (scanned/classified) and then explicitly discarded/closed before rendering the next page, instead of holding every rendered page in memory simultaneously.
2. Apply the same streaming approach to the AI-classification fallback's base64 encoding step so pages are encoded and sent one at a time rather than all buffered upfront.
3. Add a configurable max-page guard (e.g. an env var / config setting with a sane default) checked early in process_split_pdf and process_barcode_split_pdf; if the PDF's page count exceeds the cap, reject or truncate the job with a clear error/log message before starting the sequential per-page LLM calls.
4. Ensure the max-page check happens before any page rendering/LLM calls begin, so an oversized PDF fails fast rather than partway through a long-running loop.
5. Surface the cap and rejection reason to the caller (e.g. via the SSE progress/error channel already used elsewhere) so the user understands why an oversized upload was refused.
6. Verify: submit a PDF exceeding the configured max-page cap and confirm it's rejected immediately with a clear message before any page processing starts; submit a large-but-under-cap PDF and confirm memory usage stays bounded (pages discarded as processed) rather than holding all rendered images at once.

---

### Task 27: Unsynchronized read-modify-write race on learned_structures.json (learn_type_correction)

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81279100e554ea7767dc

**Target File:** `qwen_service.py`

**Issue Summary:**
learn_type_correction (~4869-4892) reads, merges, and writes learned_structures.json with no file lock, same as sibling save_learned_structure (~1054). Concurrent calls from different background threads can race, with the second writer's file overwriting the first's changes, silently losing a learned-type correction. Add a threading.Lock (or file-level lock) around the shared read-modify-write sequence.

**Step-by-step fix logic:**
1. In learn_type_correction (~4869-4892), locate the unsynchronized read-modify-write sequence on learned_structures.json.
2. Extract the lock-protected, temp-file-then-os.replace helper being introduced for save_learned_structure's identical race (~1054) into a shared function/module (if not already done as part of that fix), rather than writing a second, separate locking implementation.
3. Update learn_type_correction to acquire the same threading.Lock used by save_learned_structure around its full read-merge-write sequence, and to perform the write via the shared temp-file + os.replace atomic-write helper.
4. Confirm both learn_type_correction and save_learned_structure serialize on the exact same lock object (not two separate locks), since they operate on the same file and must not run concurrently with each other either.
5. Add a log statement noting when a correction is merged, to make it easier to confirm no writes are silently overwritten going forward.
6. Verify: fire concurrent calls to learn_type_correction and save_learned_structure from different threads targeting the same file, then confirm learned_structures.json contains all expected corrections/structures afterward with none lost to a race, and that the file remains valid JSON throughout.

---

## document_routes.py

### Task 28: Blocking outbound FX-rate HTTP call inside chat handler

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca8134a526c4ab49ae1e3d

**Target File:** `document_routes.py`

**Issue Summary:**
_live_rate (1771-1778), called from the guided currency-conversion step inside ask_document, makes a blocking urllib.request.urlopen call to a third-party API on every relevant chat message, with no caching and only a 5s timeout, degrading or failing the /ask endpoint under third-party slowness/outage. Cache FX rates for a short TTL and/or make the call asynchronous.

**Step-by-step fix logic:**
1. Locate _live_rate (document_routes.py, lines 1771-1778) and identify the blocking urllib.request.urlopen call made on every relevant chat message during the guided currency-conversion step inside ask_document.
2. Introduce a short-TTL in-memory (or Redis) cache keyed by currency pair, e.g. {(from, to): (rate, fetched_at)}, and have _live_rate return the cached rate if fetched_at is within the TTL window (e.g. 60-300s) instead of calling the API.
3. On a cache miss, keep the existing 5s-timeout urlopen call but wrap it so a failure doesn't crash ask_document -- fall back to the last cached rate if present, or a clear error if not.
4. Optionally move the actual network fetch off the request thread (background refresh thread or async task) so /ask never blocks on the third-party API even on cache miss.
5. Add a boolean/flag (mirroring the conversion_succeeded pattern used elsewhere) so ask_document can tell whether the returned rate came from a live fetch vs. cache vs. failure, and only label the conversion as live when appropriate.
6. Verify: simulate the FX API being slow/down (e.g. point at a non-responsive host) and confirm /ask still responds quickly using the cached rate or a graceful error, instead of hanging or timing out the whole endpoint; also confirm repeated calls within the TTL window don't re-hit the network.

---

### Task 29: Blocking synchronous exchange-rate HTTP call inside /ask request handler

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca817bb6a0fd3a9f883425

**Target File:** `document_routes.py`

**Issue Summary:**
Inside the /ask currency-conversion path (~2472), urllib.request.urlopen to open.er-api.com is called synchronously on every relevant chat message with no caching and a broad except Exception: pass that silently discards network failures, adding latency/availability coupling directly in the request path. Cache rates for a short TTL and/or run the lookup off the request thread.

**Step-by-step fix logic:**
1. Find the currency-conversion path inside the /ask handler (~line 2472) where urllib.request.urlopen to open.er-api.com is called synchronously on every relevant chat message.
2. Replace the bare 'except Exception: pass' with explicit exception handling that logs the failure and returns an honest error/status (per the explicit success/failure signaling pattern) rather than silently discarding the network error.
3. Add a short-TTL cache for the fetched rate (shared with or matching the cache added for _live_rate at 1771-1778) so repeated chat messages within the TTL window reuse the cached rate instead of re-calling the API.
4. Move the lookup off the synchronous request path where feasible -- e.g. background refresh with cache-first serving -- so /ask latency isn't coupled to open.er-api.com availability.
5. Ensure a failed/timed-out fetch surfaces as an explicit conversion failure in the response (not a silently degraded 'success') so the frontend can distinguish a real rate from a missing one.
6. Verify: block or delay requests to open.er-api.com in a test environment and confirm /ask returns a clear, fast error/fallback instead of hanging, and confirm cached rates are served without a network call within the TTL.

---

### Task 30: Commit failures silently swallowed; no rollback in document_routes.py

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81c29d76fbf11fdf348f

**Target File:** `document_routes.py`

**Issue Summary:**
Many db.session.commit() calls in ask_document's guided-flow handlers (e.g. _apply_rule 1074-1086, _add_alias_to_field 1021-1038) are wrapped in bare except Exception: pass, and db.session.rollback() is never called anywhere in the file. A failed commit is swallowed yet the endpoint still returns success, so nothing is persisted and the session can be left broken. Catch commit failures explicitly, call rollback(), and return an honest error response.

**Step-by-step fix logic:**
1. Enumerate every db.session.commit() call in ask_document's guided-flow handlers, starting with _apply_rule (1074-1086) and _add_alias_to_field (1021-1038), that is wrapped in a bare 'except Exception: pass'.
2. Replace each bare except with a specific except that catches the commit exception, logs it with context (handler name, doc id), and calls db.session.rollback() to reset the session state -- since rollback() is never called anywhere in the file today.
3. After the rollback, return an explicit failure response (e.g. {"success": false, "error": "..."}) instead of letting the endpoint fall through to its normal success response, matching the explicit success/failure signaling pattern already used elsewhere in the file.
4. Audit other guided-flow commit sites in the /ask section for the same bare except/commit pattern and apply the same fix consistently so no handler can swallow a failed persist.
5. Add a regression test or manual repro that forces a commit failure (e.g. a constraint violation) to confirm the session is left usable afterward rather than broken for subsequent requests.
6. Verify: force db.session.commit() to raise in _apply_rule and _add_alias_to_field (e.g. via a mock or invalid constraint), confirm rollback() is invoked, the endpoint returns success: false with an error message, and the DB session remains usable for the next request.

---

### Task 31: In-memory jobs dict grows unbounded for process lifetime

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca8134a061f2c11413b1c0

**Target File:** `document_routes.py`

**Issue Summary:**
The module-level jobs dict (23-24) backing /upload, /progress, /cancel is never pruned; every completed job's full extracted fields/text stays in memory forever. Under sustained upload traffic this causes unbounded memory growth until the process OOMs. Expire/remove job entries some time after they reach 'done' via a TTL sweep.

**Step-by-step fix logic:**
1. Locate the module-level jobs dict (lines 23-24) that backs /upload, /progress, and /cancel, and confirm entries are never removed after a job reaches 'done'.
2. Add a 'completed_at' (or 'updated_at') timestamp to each job entry when its status transitions to 'done' or 'error'.
3. Implement a TTL sweep (background thread with periodic interval, or lazy sweep triggered on each /upload call) that removes job entries whose completed_at is older than a defined TTL (e.g. 30-60 minutes), following the same expire/prune approach called out for ivault_jobs.
4. Ensure the sweep only removes terminal-state jobs (done/error) and never in-progress jobs, to avoid breaking active /progress polling.
5. Guard the sweep and dict mutation with the same lock/thread-safety mechanism already used to read/write jobs elsewhere in the file, to avoid race conditions during pruning.
6. Verify: run a batch of uploads to completion, wait past the TTL, and confirm their entries are removed from jobs (e.g. via a debug/introspection endpoint or memory inspection) while an in-progress job started just before the sweep is left untouched.

---

### Task 32: iVault SSE progress stream can poll forever if job never reaches terminal state

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81d7bfbeefc40e67cd3b

**Target File:** `document_routes.py`

**Issue Summary:**
ivault_progress (~3094-3143) loops with time.sleep(0.4) until job['status'] is done/error. If the background worker thread dies before updating status, the generator loops indefinitely, tying up a worker/connection. Add a max iteration/time budget after which the stream emits a timeout error and closes.

**Step-by-step fix logic:**
1. Open ivault_progress (~lines 3094-3143) and find the generator loop that sleeps 0.4s and repeats until job['status'] is 'done' or 'error', with no bound on iterations.
2. Introduce a max iteration count or wall-clock time budget (e.g. based on time.time() at loop start vs. a max duration like 60-120s) tracked alongside the existing sleep loop.
3. When the budget is exceeded without the job reaching a terminal state, emit an SSE timeout/error event to the client (matching the explicit failure-signaling pattern) and break out of the loop, closing the stream cleanly.
4. Ensure the underlying job entry is also marked as errored/timed-out in ivault_jobs when this happens, so subsequent polls or cleanup logic see a consistent terminal state rather than an orphaned 'in progress' job.
5. Cross-check this fix against the sibling /progress SSE stream (lines 488-536) which has the same unbounded-loop issue, and apply the same timeout budget there for consistency.
6. Verify: simulate a background worker thread dying before updating job status (e.g. kill/mock the worker), confirm the SSE stream emits a timeout error and terminates within the configured budget instead of polling indefinitely.

---

### Task 33: ivault_jobs dict grows unbounded with no eviction/TTL

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca812ea6fae72f44a49835

**Target File:** `document_routes.py`

**Issue Summary:**
ivault_jobs (~2988-2989) is written on every /ivault/recognize and /ivault/detect-separators call but entries are never removed after the client finishes polling /ivault/progress/<job_id>. Over the process lifetime this dict leaks memory. Delete the job entry after the SSE stream sends its terminal event, or add a TTL-based sweep.

**Step-by-step fix logic:**
1. Locate the ivault_jobs dict (~lines 2988-2989) and confirm it's written on every /ivault/recognize and /ivault/detect-separators call with no removal after clients finish polling.
2. Identify the point in the SSE stream (ivault_progress, ~3094-3143) where the terminal event (done/error) is sent to the client, and add cleanup logic there to delete the corresponding ivault_jobs[job_id] entry immediately after the terminal event is flushed.
3. As a safety net for clients that never finish polling (e.g. abandoned connections), add a TTL-based sweep similar to the one used for the jobs dict (23-24), removing entries older than a defined threshold regardless of whether the terminal event was delivered.
4. Ensure the deletion is thread-safe (use the same lock already protecting ivault_jobs reads/writes) so a delete doesn't race with an in-flight /ivault/progress poll.
5. Confirm /ivault/recognize and /ivault/detect-separators still function correctly after adding cleanup -- i.e. a new job_id for a new request isn't accidentally cleaned up mid-flight.
6. Verify: run an /ivault/recognize call to completion, poll /ivault/progress until the terminal SSE event, and confirm ivault_jobs no longer contains that job_id afterward; separately, start a job and abandon polling to confirm the TTL sweep still reclaims it.

---

### Task 34: Mid-loop save failure in upload leaves orphaned files with no job

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca8132ba6bc477796cce65

**Target File:** `document_routes.py`

**Issue Summary:**
In upload_documents' per-file loop (413-443), if file.save() raises for one file after earlier files already saved (e.g. disk full), the exception propagates before the jobs entry is created; the client gets a 500 with no job_id and already-saved files become permanently orphaned. Wrap the save loop in try/except that cleans up partially-saved files before returning an error.

**Step-by-step fix logic:**
1. Locate upload_documents' per-file loop (lines 413-443) and confirm that a file.save() failure on a later file (e.g. disk full) currently propagates as an unhandled exception before the jobs entry is created.
2. Wrap the per-file save loop in a try/except that catches save failures, tracking which files were already successfully saved to disk in this request.
3. On catching a save failure, iterate the list of already-saved files for this request and delete them (using the guarded-deletion pattern: check os.path.exists() before os.remove(), catch/log OSError) so no orphaned files remain.
4. After cleanup, return an explicit error response (e.g. {"success": false, "error": "..."}) including a clear message and, if a job was already created, its job_id -- rather than letting a raw 500 with no job_id reach the client.
5. Consider creating the jobs entry (in a 'failed'/'error' state) even on this early failure path, so the client has something to reference/poll instead of nothing.
6. Verify: simulate a mid-loop save failure (e.g. mock file.save() to raise on the 2nd of 3 files, or fill disk in a test environment), confirm the first saved file is removed from disk, the response is an explicit error (not a raw 500), and no orphaned files remain on disk afterward.

---

### Task 35: No pagination on document list endpoints; full raw_text returned

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81309f0fd13160f99e51

**Target File:** `document_routes.py`

**Issue Summary:**
get_all_documents (578-581) and get_by_type (752-755) load and serialize every matching row in one response, including full raw_text, with no limit/offset. As the table grows this returns megabytes of JSON per call. Add limit/offset or cursor-based pagination and exclude large text fields from the list view.

**Step-by-step fix logic:**
1. Locate get_all_documents (lines 578-581) and get_by_type (lines 752-755) and confirm both load and serialize every matching row in a single response with no limit/offset, including full raw_text.
2. Add limit/offset (or cursor-based) query parameters to both endpoints, with a sane default page size (e.g. 20-50) and a maximum cap to prevent callers from requesting unbounded pages, similar in spirit to the MAX_FILES_PER_UPLOAD pattern of enforcing explicit limits early.
3. Update the underlying query in both handlers to apply .limit()/.offset() (or a cursor filter) at the DB level rather than fetching all rows and slicing in Python, so the DB does the work of not loading unneeded rows.
4. Exclude the large raw_text field from the list-view serialization (only include lightweight summary fields), and provide a separate detail endpoint/param for callers that need the full raw_text.
5. Update response payloads to include pagination metadata (e.g. total count, next offset/cursor) so clients can page through results.
6. Verify: seed the documents table with a large number of rows containing sizable raw_text, call get_all_documents and get_by_type with and without limit/offset, and confirm response size stays bounded, raw_text is omitted from list responses, and pagination metadata correctly reflects the dataset.

---

### Task 36: PATCH /documents/<id> crashes on non-dict 'fields' payload

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca819fb0b3c2d76058ae47

**Target File:** `document_routes.py`

**Issue Summary:**
update_document (705-712) calls existing.update(data['fields']) without validating it's a dict. A PATCH body like {"fields": "oops"} raises an unhandled TypeError, producing a raw 500 instead of a clean validation error. Check isinstance(data['fields'], dict) before merging and return 400 otherwise.

**Step-by-step fix logic:**
1. Locate update_document (lines 705-712) and find the call existing.update(data['fields']) that assumes data['fields'] is always a dict.
2. Add a validation check isinstance(data.get('fields'), dict) immediately before the merge, guarding against payloads like {"fields": "oops"}.
3. When the check fails, return a clean 400 response with an explicit error message (e.g. {"success": false, "error": "'fields' must be an object"}) instead of letting the TypeError propagate into a raw 500.
4. Confirm data['fields'] being absent entirely is also handled gracefully (e.g. treated as no-op or its own 400), so the fix doesn't just move the crash to a KeyError.
5. Scan update_document and nearby handlers for similar unguarded .update()/merge calls on user-supplied JSON to ensure the same non-dict-payload crash isn't present elsewhere in the file.
6. Verify: send PATCH /documents/<id> with body {"fields": "oops"} and confirm a 400 with a clear error message is returned (no stack trace/500); then send a valid dict payload and confirm the update still succeeds as before.

---

### Task 37: Repeated manual doc.fields JSON parsing instead of get_fields() helper (ask endpoint)

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca815b8e28c3e0f0b0f1da

**Target File:** `document_routes.py`

**Issue Summary:**
Throughout the /ask section (2135, 2191, 2226-2230, 2487, 2516-2520, 2545-2548, 2631-2632, 2695-2696) the code repeats ad-hoc _json.loads(doc.fields or '{}') try/except blocks instead of using the existing doc.get_fields() helper, risking inconsistent error handling. Replace all inline blocks with calls to doc.get_fields().

**Step-by-step fix logic:**
1. Grep the /ask section of document_routes.py for every ad-hoc _json.loads(doc.fields or '{}') try/except block, using the listed locations (2135, 2191, 2226-2230, 2487, 2516-2520, 2545-2548, 2631-2632, 2695-2696) as the starting checklist.
2. Confirm the existing doc.get_fields() helper already encapsulates the same parsing plus consistent error handling, and note any behavioral differences (e.g. default value on parse failure) between it and each inline block before replacing.
3. Replace each inline _json.loads(doc.fields or '{}') try/except with a call to doc.get_fields(), removing the now-redundant local exception handling.
4. Re-run through each replaced call site to confirm downstream code still receives the same shape of dict it expected (some sites may have relied on a slightly different default/error behavior from their local try/except).
5. Remove now-unused local imports/aliases (e.g. _json) at call sites if get_fields() makes them unnecessary in that scope, and check that _json is still needed elsewhere before removing the import entirely.
6. Verify: exercise the /ask endpoint through each of the 8 replaced code paths (including cases with malformed/empty doc.fields) and confirm behavior matches pre-change output, with consistent error handling now coming from doc.get_fields() in all cases.

---

### Task 38: SSE progress stream has no idle timeout or disconnect detection

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81d6a60afc099756b052

**Target File:** `document_routes.py`

**Issue Summary:**
The generate() loop backing GET /progress/<job_id> (488-536) polls every 0.4s in an unbounded while True with no max duration and no client-disconnect detection. A client that stops reading can hold a worker thread/connection open indefinitely, risking WSGI worker pool exhaustion. Add an idle/hard timeout and handle client disconnects to break the loop.

**Step-by-step fix logic:**
1. Open the generate() loop backing GET /progress/<job_id> (lines 488-536) and confirm it's an unbounded 'while True' polling every 0.4s with no max duration and no client-disconnect check.
2. Add a max duration / max iteration budget (mirroring the fix for ivault_progress at ~3094-3143) so the loop tracks elapsed time and breaks once a threshold (e.g. 60-120s) is exceeded without reaching a terminal job status.
3. On budget exceeded, emit a final SSE event indicating timeout/error to the client before closing the generator, so the frontend can distinguish this from a normal completion.
4. Add client-disconnect detection inside the loop (e.g. checking request/response stream state or catching the exception Flask raises when the client has gone away) so the generator exits promptly instead of continuing to poll for a client that's no longer listening.
5. Ensure any resources tied to the loop (e.g. worker thread references, job status polling) are released cleanly on both the timeout and disconnect exit paths.
6. Verify: (1) start a job whose status is never set to done/error and confirm the stream emits a timeout event and closes after the configured budget; (2) start a normal job, disconnect the client mid-stream, and confirm the server-side generator exits promptly rather than holding the worker/connection open indefinitely.

---

## settings_routes.py

### Task 39: _snake_case can collapse iVault property names to empty string

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81c6ab11ddae9ecb65c4

**Target File:** `settings_routes.py`

**Issue Summary:**
_snake_case (399-407) strips all non-alphanumeric characters; a propertyName of only punctuation produces an empty string. _sync_ivault_document_type_with_session (496-508) only checks prop_name truthiness, not the _snake_case result, so two distinct properties can both map to field_name='' and the second is silently skipped as a duplicate, losing that property. Validate the _snake_case result is non-empty, falling back to a generated name otherwise.

**Step-by-step fix logic:**
1. In _snake_case (settings_routes.py, lines 399-407), after the regex strips all non-alphanumeric characters, check whether the resulting string is empty (e.g. `if not result:`).
2. If the result is empty, generate a fallback field name instead of returning '' - e.g. a deterministic slug derived from a hash/index of the original propertyName, or a prefix like 'field_' plus a counter, so punctuation-only names never collapse to the same empty key.
3. In _sync_ivault_document_type_with_session (lines 496-508), change the duplicate-check logic to test the _snake_case(prop_name) output for emptiness/collision, not just the raw prop_name truthiness, before deciding to skip a property as a duplicate.
4. Ensure each property that maps to a generated fallback name is still tracked as sync'd (not silently dropped) and log a warning noting the original propertyName that triggered the fallback.
5. Add/update a unit test that syncs two distinct iVault properties whose names are only punctuation (e.g. '---' and '***') and asserts both are preserved with distinct field_name values.
6. Verify: run the iVault sync against a mock payload containing two punctuation-only propertyName values and confirm both appear as separate fields in the resulting DocumentType schema, with no data loss and no empty-string field_name collisions.

---

### Task 40: delete_document_type has no protection against deleting built-in types

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81b5b76de405297fe47e

**Target File:** `settings_routes.py`

**Issue Summary:**
delete_document_type (115-120) unconditionally deletes any DocumentType including is_builtin=True ones (e.g. invoice, receipt), breaking extraction until restart re-seeds it, and any per-tenant customizations are permanently lost. Add a guard that blocks or requires extra confirmation for deleting is_builtin types.

**Step-by-step fix logic:**
1. In delete_document_type (settings_routes.py, lines 115-120), fetch the DocumentType record before deleting and check its is_builtin flag.
2. If is_builtin is True, return a 403/409 response with a clear error message (e.g. 'Built-in document types cannot be deleted') instead of proceeding to session.delete/commit.
3. Optionally support an explicit override path (e.g. a `force=true` query param or admin-only flag) that still requires additional confirmation before allowing deletion of a built-in type, if the product needs that escape hatch.
4. Ensure any per-tenant customizations tied to a built-in type are preserved by this guard, since the whole point is to avoid losing them on accidental delete.
5. Add a test that attempts to delete a built-in type (e.g. 'invoice') and asserts it is rejected, plus a test that a non-builtin type still deletes successfully.
6. Verify: call DELETE on a built-in document type via the route and confirm it returns a blocking error (not 200) and the record still exists in the DB afterward; confirm deleting a custom (is_builtin=False) type still works as before.

---

### Task 41: Multiple routes crash with AttributeError on non-dict JSON body

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca810b83b6fc11b2edc03e

**Target File:** `settings_routes.py`

**Issue Summary:**
Routes such as create_document_type (line 43), update_document_type (line 84), merge_prompt_version (line 216), create_prompt_version_route (line 258), update_prompt (line 343), update_retention_policy (line 176), and both iVault sync routes (575, 614) call request.get_json(force=True) and immediately call .get(...) on the result. Non-dict JSON (e.g. a list) causes AttributeError and an unhandled 500. Validate isinstance(data, dict) and return 400 on malformed payloads.

**Step-by-step fix logic:**
1. Identify every route calling request.get_json(force=True) and then calling .get(...) on the result: create_document_type (line 43), update_document_type (84), merge_prompt_version (216), create_prompt_version_route (258), update_prompt (343), update_retention_policy (176), and both iVault sync routes (575, 614).
2. Introduce a small shared helper (e.g. `_require_json_dict(request)`) that calls get_json(force=True), checks `isinstance(data, dict)`, and returns a 400 JSON error response early if it is not a dict.
3. Replace the raw `data = request.get_json(force=True)` line in each of the listed routes with a call to this helper, short-circuiting the route function on failure.
4. Make sure the 400 response body includes a descriptive message (e.g. 'Request body must be a JSON object') consistent across all affected routes for predictable client-side handling.
5. Add regression tests per route that POST a JSON list (e.g. `[]`) or a JSON string/number as the body and assert a 400 is returned instead of a 500/AttributeError.
6. Verify: send non-dict JSON bodies (list, string, number) to each of the 8 listed endpoints and confirm every one returns 400 with the validation message, with no AttributeError/500 in the logs.

---

### Task 42: sync_ivault_document_type doesn't catch network/JSON errors

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81f79642d79e3a20d824

**Target File:** `settings_routes.py`

**Issue Summary:**
In sync_ivault_document_type (lines 589-594) only IVaultAuthError and RuntimeError are caught, but _sync_ivault_document_type_with_session (lines 454-462) can raise ConnectionError/Timeout, or cp_resp.json() can raise on a non-JSON body with no try/except - these propagate as unhandled 500s with a full traceback exposed. Wrap the sync call in a broader try/except and return a clean 502.

**Step-by-step fix logic:**
1. In sync_ivault_document_type (settings_routes.py, lines 589-594), expand the except clause beyond IVaultAuthError and RuntimeError to also catch ConnectionError and Timeout (and any other requests-library exceptions raised by the underlying HTTP call).
2. In _sync_ivault_document_type_with_session (lines 454-462), wrap the cp_resp.json() call in its own try/except (e.g. catching ValueError/JSONDecodeError) so a non-JSON response body doesn't propagate as an unhandled exception.
3. On catching any of these new exception types, log the error (including the underlying exception detail server-side only) and return a clean 502 Bad Gateway JSON response instead of letting Flask's default handler expose a stack trace to the client.
4. Ensure the 502 response body is a generic, non-sensitive message (e.g. 'Unable to reach iVault service') so no internal traceback or connection details leak to the client.
5. Add tests that mock the iVault HTTP call to raise ConnectionError, raise Timeout, and to return a non-JSON body, asserting a 502 with the sanitized message in each case.
6. Verify: simulate a network timeout and a malformed (non-JSON) response from the iVault dependency and confirm sync_ivault_document_type returns 502 with no unhandled traceback in the response or logs.

---

### Task 43: TOCTOU race lets duplicate document type names bypass the 409 check

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81b392aff2db00dec0b5

**Target File:** `settings_routes.py`

**Issue Summary:**
create_document_type (48-56) and update_document_type (87-90) check for an existing name and only then insert/rename; a concurrent request can slip between check and commit, producing an unhandled IntegrityError (500) instead of a 409. Wrap the commit in try/except IntegrityError and return 409, or use a DB-level upsert.

**Step-by-step fix logic:**
1. In create_document_type (lines 48-56) and update_document_type (lines 87-90), keep the existing pre-check for an existing name (fast path for the common case, avoids unnecessary DB round trips on obvious conflicts).
2. Wrap the subsequent insert/rename and session.commit() call in a try/except that specifically catches sqlalchemy.exc.IntegrityError (the unique-constraint violation raised when two concurrent requests race past the pre-check).
3. On catching IntegrityError, call session.rollback() and return a 409 Conflict response with the same error shape/message used by the existing pre-check's 409 response, so callers see consistent behavior regardless of which path caught the duplicate.
4. Confirm the DocumentType.name column has (or add) a unique constraint/index at the DB level so the IntegrityError is actually guaranteed to fire on a race, rather than silently allowing duplicates.
5. Add a concurrency-style test (e.g. two near-simultaneous create/update calls with the same name, or a test that manually inserts a duplicate row between the check and commit to force the IntegrityError path) and assert a 409 is returned, not a 500.
6. Verify: simulate the race (mock the pre-check to pass, then insert a conflicting row before commit) and confirm the route returns 409 with rollback applied and no unhandled IntegrityError/500 in logs.

---

### Task 44: update_prompt crashes if content is a non-string JSON value

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca812e9a3be405a22db410

**Target File:** `settings_routes.py`

**Issue Summary:**
update_prompt (lines 345-347) does content = data.get('content'); if content is None or not content.strip(). A client sending {"content": 123} triggers AttributeError -> unhandled 500. Add an isinstance(content, str) check before calling .strip().

**Step-by-step fix logic:**
1. In update_prompt (settings_routes.py, lines 345-347), locate `content = data.get('content'); if content is None or not content.strip()`.
2. Add an `isinstance(content, str)` check before the `.strip()` call, e.g. change the condition to `if content is None or not isinstance(content, str) or not content.strip()`.
3. When content is present but not a string (e.g. `{"content": 123}`), return the same 400 validation error already used for the None/empty case, with a message clarifying content must be a string.
4. Double check no other downstream code path in update_prompt assumes content is a string without this same guard (e.g. before it's saved to the Prompt model).
5. Add a test posting `{"content": 123}` and one posting `{"content": [1,2]}` to update_prompt and assert both return 400 rather than 500.
6. Verify: send a request with a non-string content value (int, list, dict) to update_prompt and confirm a 400 validation response is returned with no AttributeError/500 in the logs; confirm valid string content still updates successfully.

---

### Task 45: update_retention_policy crashes on non-numeric ttl values

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca8123b4cec748c714583b

**Target File:** `settings_routes.py`

**Issue Summary:**
Lines 183-185 call int(data['sample_ttl_days']) / int(data['production_ttl_days']) with no try/except. A non-numeric string triggers an unhandled ValueError and 500 instead of a clean validation error. Wrap the int() conversions in try/except and return 400 with a descriptive message.

**Step-by-step fix logic:**
1. In update_retention_policy (settings_routes.py, lines 183-185), locate the `int(data['sample_ttl_days'])` and `int(data['production_ttl_days'])` conversions.
2. Wrap both conversions in a try/except block catching ValueError (and TypeError, in case the key is missing or None) around each conversion, or a shared helper that converts and validates both fields together.
3. On catching the exception, return a 400 response with a descriptive message identifying which field failed (e.g. 'sample_ttl_days must be a valid integer') rather than letting the ValueError propagate to a 500.
4. Consider also validating the parsed integers are within a sane range (e.g. non-negative) as part of the same validation step, consistent with how other numeric inputs are validated elsewhere in the module.
5. Add tests posting non-numeric strings (e.g. `{"sample_ttl_days": "abc"}`) and missing keys for both ttl fields, asserting a 400 with a clear message instead of a 500.
6. Verify: send non-numeric values for sample_ttl_days and production_ttl_days to update_retention_policy and confirm the route returns 400 with a descriptive error, with no unhandled ValueError/500 in the logs.

---

## pdf_highlighted_service.py

### Task 46: Corrupt uploaded PDF crashes highlighted export unhandled

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81f88c6bc4e30196d1ff

**Target File:** `pdf_highlighted_service.py`

**Issue Summary:**
generate_highlighted_pdf() (13-48) wraps the image path in try/except with a fallback, but the PDF branch (45-46) calls _highlighted_from_pdf() directly with no try/except. If fitz.open() or the per-page OCR/insert_text loop raises on a corrupt/malformed PDF, the exception propagates unhandled. Wrap the call in try/except and fall back to _fallback_pdf(doc_dict).

**Step-by-step fix logic:**
1. Open pdf_highlighted_service.py and locate generate_highlighted_pdf() (lines 13-48).
2. Note lines 45-46 call _highlighted_from_pdf() directly, unlike the image branch which is already wrapped in try/except with a fallback.
3. Wrap the _highlighted_from_pdf() call in a try/except matching the image branch's pattern.
4. On exception, fall back to _fallback_pdf(doc_dict) exactly as the image branch does, so callers always get a valid PDF back.
5. Log the caught exception (including the document id) before falling back, so corrupt uploads are still visible in logs.
6. Verify: feed a corrupt/malformed PDF into generate_highlighted_pdf() and confirm it returns a fallback PDF instead of raising, with the error logged.

---

### Task 47: fitz documents leak on exception paths in highlighting

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81229ba8cbf112509baf

**Target File:** `pdf_highlighted_service.py`

**Issue Summary:**
In _highlighted_from_image() (53-121) and _highlighted_from_pdf() (124-196), fitz documents are only closed after the happy path with no try/finally. An exception mid-processing leaves the open fitz document never closed, leaking the file handle/native memory. Wrap document lifetime in try/finally to guarantee .close() is called.

**Step-by-step fix logic:**
1. Open pdf_highlighted_service.py and inspect _highlighted_from_image() (53-121) and _highlighted_from_pdf() (124-196).
2. Identify where the fitz.Document object is opened/created and where .close() is currently called only on the happy path.
3. Refactor each function to open the fitz document, then wrap the processing body in try/finally with doc.close() in the finally block.
4. Ensure the finally block guards against the document not yet being assigned if the open itself fails (e.g. initialize the variable to None before the try).
5. Repeat the same try/finally structure in both functions for consistency.
6. Verify: force an exception mid-processing (e.g. mock insert_text to raise) and confirm the fitz document handle is closed via a mock/spy on .close(), with no leaked handles or unclosed docs after repeated calls.

---

### Task 48: Highlighted PDF fallback re-opens same corrupt image and fails too

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81679e1ff72861aadaa6

**Target File:** `pdf_highlighted_service.py`

**Issue Summary:**
When _highlighted_from_image() raises (e.g. Image.open() fails on a corrupt image), the caller falls back to _image_only_pdf(original_path), but that function also opens the same file at its start, raising the same exception again unhandled. Fall back to _fallback_pdf(doc_dict) instead, since it doesn't re-open the original file.

**Step-by-step fix logic:**
1. Locate the caller that catches exceptions from _highlighted_from_image() and currently falls back to _image_only_pdf(original_path).
2. Confirm _image_only_pdf() re-opens original_path via Image.open() at its start, which will raise the same error again for a corrupt image.
3. Change the fallback target from _image_only_pdf(original_path) to _fallback_pdf(doc_dict), which does not re-open the original file.
4. Confirm _fallback_pdf(doc_dict) has the parameters it needs (doc_dict) available at the call site.
5. Remove/adjust any now-unused fallback-chain code so _image_only_pdf isn't reached on this failure path.
6. Verify: pass a corrupt image through the export flow and confirm the response is the _fallback_pdf output (not a second unhandled exception from _image_only_pdf).

---

### Task 49: Repeated Tesseract OCR per export, no caching (pdf_highlighted_service)

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81748bbae738512aef7a

**Target File:** `pdf_highlighted_service.py`

**Issue Summary:**
_get_word_boxes() runs pytesseract.image_to_data() fresh on every export request for the same document; multi-page PDFs are re-rasterized and re-OCRed on every export, which is CPU/time-expensive and doesn't scale for repeated exports. Cache word-box results keyed by document id after first computation.

**Step-by-step fix logic:**
1. Open pdf_highlighted_service.py and locate _get_word_boxes(), which calls pytesseract.image_to_data() on every invocation.
2. Add a cache keyed by document id (and page number if applicable), backed by a dict, disk file, or existing cache/store used elsewhere in the app.
3. Before calling image_to_data(), check the cache for existing word-box results for that document id and return them if present.
4. After computing word boxes for a document, store the result in the cache before returning.
5. Add cache invalidation for when the source document/image changes (e.g. re-upload), so stale word boxes aren't served.
6. Verify: export the same document twice and confirm pytesseract.image_to_data() (via a mock/counter) is invoked only once, with the second export served from cache and producing identical output.

---

## document_model.py

### Task 50: Document.get_fields()/get_text_blocks() lack try/except used elsewhere

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca8146ba79f75398fab464

**Target File:** `document_model.py`

**Issue Summary:**
get_fields() (252-254) and get_text_blocks() (297-299) call json.loads directly with no exception handling, unlike get_field_sources()/get_field_ambiguities() (256-268) which catch errors and return {}. Malformed JSON in Document.fields/text_blocks raises an unhandled exception in to_dict(), called on virtually every document read. Make these methods consistent with the others by catching JSON errors.

**Step-by-step fix logic:**
1. Open document_model.py and locate get_fields() (252-254) and get_text_blocks() (297-299).
2. Compare them against get_field_sources()/get_field_ambiguities() (256-268), which already catch JSON errors and return {}.
3. Wrap the json.loads call in get_fields() in a try/except (e.g. except (TypeError, json.JSONDecodeError)) returning {} (or the appropriate empty default) on failure.
4. Apply the same try/except pattern to get_text_blocks().
5. Confirm to_dict() (which calls both methods on every document read) no longer propagates JSON decode errors from malformed fields/text_blocks data.
6. Verify: construct a Document with malformed JSON in the fields/text_blocks column, call to_dict(), and confirm it returns without raising, yielding an empty default instead.

---

### Task 51: DocumentType.to_dict()/PromptVersion.to_dict() cause N+1 queries

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81888938fa74f0641e38

**Target File:** `document_model.py`

**Issue Summary:**
DocumentType.to_dict() (28-45) issues a separate PromptVersion.query.get per call and iterates lazy-loaded self.fields; PromptVersion.to_dict() (400-416) similarly lazily touches document_type/validations. Listing routes call .to_dict() in a loop, causing O(N) extra DB round-trips instead of one joined/eager-loaded query. Use joinedload/selectinload and batch-fetch to avoid per-row queries.

**Step-by-step fix logic:**
1. Open document_model.py and review DocumentType.to_dict() (28-45), which does a per-call PromptVersion.query.get and iterates lazy-loaded self.fields.
2. Review PromptVersion.to_dict() (400-416), which similarly lazily touches document_type/validations.
3. Identify the listing routes that call .to_dict() in a loop over multiple rows.
4. Update those routes' base queries to use joinedload/selectinload for the relevant relationships (fields, document_type, validations, prompt_version) so related rows are eager-loaded in one query.
5. Refactor to_dict() to consume the already-loaded relationship data rather than issuing new .query.get calls internally.
6. Verify: enable SQL query logging (e.g. SQLAlchemy echo=True) around a listing endpoint before and after the change, and confirm the number of queries no longer scales linearly with the number of rows returned.

---

### Task 52: No unique constraint on (document_type_id, field_name)

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca8140b4a1d32749e9d63b

**Target File:** `document_model.py`

**Issue Summary:**
DocumentField (48-69) has no composite unique constraint on (document_type_id, field_name). Settings routes accept an arbitrary list of field dicts without checking for name collisions, so two DocumentField rows can share the same field_name under one type, which is ambiguous for extraction/alias matching downstream. Add a unique constraint or application-level dedupe check.

**Step-by-step fix logic:**
1. Open document_model.py and locate the DocumentField model definition (48-69).
2. Add a composite UniqueConstraint on (document_type_id, field_name) to the model's __table_args__.
3. Write a migration (Alembic or the app's existing migration mechanism) to add the constraint to the existing table, first checking/cleaning up any pre-existing duplicate rows that would violate it.
4. Update the settings routes that accept an arbitrary list of field dicts to validate for name collisions before insert/update, returning a clear application-level error instead of a raw DB integrity error.
5. Apply the migration to a staging/dev database and confirm existing data passes.
6. Verify: attempt to create two DocumentField rows with the same document_type_id and field_name and confirm the operation is rejected (either via the DB constraint or the application-level check) with a clear error message.

---

## docx_service.py

### Task 53: docx_to_pdf() crashes unhandled on a corrupt .docx file

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81479798ca9b65e1bae0

**Target File:** `docx_service.py`

**Issue Summary:**
docx_to_pdf() (32-91) calls DocxDocument(docx_path) (line 40) with no try/except. An invalid/corrupt .docx raises an exception that propagates straight out of the conversion pipeline instead of being handled gracefully. Catch the open failure and return/raise a clear, documented error so the upload pipeline can report a clean message.

**Step-by-step fix logic:**
1. Open docx_service.py and locate docx_to_pdf() (32-91), specifically the DocxDocument(docx_path) call on line 40.
2. Wrap that call in a try/except targeting the exception type(s) python-docx raises on invalid/corrupt files (e.g. PackageNotFoundError or a broader Exception).
3. On failure, raise a clear, documented custom exception (e.g. DocxConversionError) or return an error result that the upload pipeline understands.
4. Update the upload pipeline caller to catch this new error type and surface a clean, user-facing message rather than a stack trace.
5. Add/update any docstring or type hints for docx_to_pdf() documenting the new failure behavior.
6. Verify: feed a corrupt/truncated .docx file into docx_to_pdf() and confirm it returns/raises the documented clean error instead of an unhandled exception, and the upload pipeline surfaces a readable message.

---

## pdf_searchable_service.py

### Task 54: fitz src leaks on exception in searchable fallback

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca812ab0e9d3b799d2fd83

**Target File:** `pdf_searchable_service.py`

**Issue Summary:**
_searchable_from_pdf_fallback() (206-228) opens src = fitz.open(pdf_path) and only closes it at the end of the happy path, with no try/finally. If get_pixmap/insert_image/get_text raises mid-loop, src is never closed, leaking the file handle. Wrap the loop in try/finally.

**Step-by-step fix logic:**
1. Open pdf_searchable_service.py and locate _searchable_from_pdf_fallback() (206-228).
2. Identify where src = fitz.open(pdf_path) is assigned and where it's currently closed only at the end of the happy path.
3. Wrap the per-page loop (get_pixmap/insert_image/get_text calls) in a try/finally block.
4. Move src.close() into the finally block, guarding against src being unset if the open itself fails.
5. Confirm no other code path after the loop still assumes src is open/needs closing again.
6. Verify: force an exception mid-loop (e.g. mock get_pixmap to raise on a later page) and confirm src.close() is still called (via mock/spy), leaving no leaked file handle.

---

### Task 55: Repeated ocrmypdf OCR per export, no caching (pdf_searchable_service)

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81128d52e3d0850e1884

**Target File:** `pdf_searchable_service.py`

**Issue Summary:**
_searchable_from_image() (141) and _searchable_from_pdf() (189) both invoke ocrmypdf.ocr(...), an expensive full OCR pass, every time a searchable PDF is exported, with no result caching. Cache the generated searchable PDF on disk keyed by doc id plus a content hash, regenerating only when source changes.

**Step-by-step fix logic:**
1. Open pdf_searchable_service.py and locate _searchable_from_image() (line 141) and _searchable_from_pdf() (line 189), both calling ocrmypdf.ocr(...).
2. Design a cache key combining the document id and a content hash (e.g. SHA-256 of the source file) so cache entries invalidate automatically when the source changes.
3. Before invoking ocrmypdf.ocr(), check whether a cached searchable PDF exists on disk at the computed cache path and return it directly if present.
4. After generating a new searchable PDF, write it to the cache path keyed as above before returning it to the caller.
5. Add cleanup/retention logic for the cache directory so it doesn't grow unbounded (or integrate with the existing retention_service purge logic).
6. Verify: export the same document twice and confirm ocrmypdf.ocr() (via a mock/counter) runs only once, with the second export served from the on-disk cache and producing an equivalent searchable PDF.

---

## retention_service.py

### Task 56: In-process flag allows duplicate schedulers on multi-worker deploys

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca815bb999e1df81781b34

**Target File:** `retention_service.py`

**Issue Summary:**
_scheduler_started (line 13) is a plain module-level boolean that only prevents duplicate scheduler threads within a single process. Under a multi-worker deployment (e.g. gunicorn), each worker independently starts its own daily purge thread, causing redundant/concurrent purge runs against the same database. Use a distributed lock (DB row lock, Redis, or external cron) instead of an in-process flag.

**Step-by-step fix logic:**
1. Open retention_service.py and locate the module-level _scheduler_started boolean (line 13) and the code that checks/sets it before starting the daily purge thread.
2. Choose a distributed coordination mechanism appropriate to the deployment (a DB row-based lock/advisory lock, a Redis lock with TTL, or moving scheduling out of the app entirely to external cron).
3. Replace the _scheduler_started check with an attempt to acquire the distributed lock; only start the purge thread/timer if the lock is successfully acquired.
4. Ensure the lock is held/renewed for the duration of the scheduled purge run (or re-acquired each run) so a second worker can safely take over if the lock-holder dies.
5. Add logging when a worker fails to acquire the lock (expected in a multi-worker deploy) so it's clear only one worker is actually running purges.
6. Verify: start multiple worker processes locally (e.g. gunicorn -w 4) and confirm, via logs or a lock-status check, that only one worker's purge thread actually executes at the scheduled time.

---

### Task 57: Purge leaves derived page-image artifacts on disk

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca8161a1f3c6b9d3a84649

**Target File:** `retention_service.py`

**Issue Summary:**
_purge_document() (76-98) only deletes the raw upload file and vector-store chunks; PDF uploads are also rasterized into per-page PNGs written to a separate output folder, never referenced or cleaned up here. After a 'purge', residual PII-containing derived images can remain on disk indefinitely, undermining the retention/right-to-erasure guarantee. Audit all per-document derived-file locations and delete them in the purge path too.

**Step-by-step fix logic:**
1. Open retention_service.py and locate _purge_document() (76-98), which currently deletes the raw upload file and vector-store chunks only.
2. Audit the codebase (pdf_service.py / pdf rasterization code) to find where per-page PNGs are written and what folder/path convention they use (likely derived from the document id).
3. Add logic in _purge_document() to compute the derived-image output folder for the given document and delete all files within it.
4. Wrap each derived-artifact deletion in its own try/except so a missing/already-deleted file doesn't abort the rest of the purge for that document.
5. Extend the audit to any other per-document derived-file locations (thumbnails, cached OCR/searchable PDFs from the caching fixes above) and include them in the purge.
6. Verify: purge a document that has associated per-page PNGs on disk and confirm the output folder is empty/removed afterward, with no orphaned PII-containing images remaining.

---

### Task 58: Purge loads entire unpurged document table into memory

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca8198b4f7dcc83393a416

**Target File:** `retention_service.py`

**Issue Summary:**
Line 104 loads every non-purged document row across all tenants into memory, then loops in Python computing expiry per row. For a production-scale table this is an unnecessary full-table load and per-row computation run daily. Batch/paginate the query or precompute per-tenant expiry cutoffs and filter in SQL.

**Step-by-step fix logic:**
1. Open retention_service.py and locate the query at line 104 that loads every non-purged document row across all tenants into memory before looping in Python.
2. Determine the expiry logic currently computed per-row in Python (using _expiry_anchor()/_document_is_expired()) and translate it into a SQL WHERE clause (or precomputed per-tenant cutoff timestamps) where possible.
3. Replace the full-table load with either a paginated/batched query (e.g. yield_per() or LIMIT/OFFSET chunks) or a filtered query that only pulls candidate-expired rows.
4. If per-tenant cutoffs are needed, precompute them once per tenant and filter documents in SQL against the cutoff rather than iterating every row in Python.
5. Confirm the batched/filtered approach still purges every document that the original full-scan would have purged (no regressions in expiry logic).
6. Verify: run the purge job against a large seeded document table and confirm memory usage stays flat (via batching) and query time/row scans are reduced, while purge results match the pre-change behavior on a test dataset.

---

### Task 59: Unconfirmed documents (no confirmed_at) never expire

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81748dacc447da917bc9

**Target File:** `retention_service.py`

**Issue Summary:**
_expiry_anchor() (35-38) returns confirmed_at for production documents, and _document_is_expired() (41-51) returns False when the anchor is None. A production document uploaded but never 'confirmed' has no expiry anchor and never gets purged regardless of age - a compliance gap. Fall back to created_at when confirmed_at is None, or add a hard-cap TTL for unconfirmed documents.

**Step-by-step fix logic:**
1. Open retention_service.py and locate _expiry_anchor() (35-38), which returns confirmed_at for production documents, and _document_is_expired() (41-51), which returns False when the anchor is None.
2. Decide the compliance-appropriate fallback: use created_at when confirmed_at is None, and/or add a separate hard-cap TTL constant for unconfirmed documents.
3. Update _expiry_anchor() to fall back to created_at (or a dedicated 'unconfirmed max age' check) instead of returning None for unconfirmed documents.
4. Update _document_is_expired() so it no longer short-circuits to False purely because the anchor is None.
5. Add/adjust a config value for the unconfirmed-document TTL if a distinct (likely shorter) retention window is desired versus confirmed documents.
6. Verify: create a test document with confirmed_at unset and created_at older than the retention window, run the purge check, and confirm _document_is_expired() now returns True and the document is purged.

---

## Dockerfile

### Task 60: Dockerfile CMD runs Flask's dev server instead of the bundled production WSGI server

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81ca937ee7f621f8087d

**Target File:** `Dockerfile`

**Issue Summary:**
Line 28 runs flask run, using Werkzeug's development server, not intended for production traffic. waitress is already listed in requirements.txt but never actually invoked in the container. Change the CMD to launch waitress, e.g. CMD ['waitress-serve', '--host=0.0.0.0', '--port=5000', 'app:app'].

**Step-by-step fix logic:**
1. Open the Dockerfile and find the CMD on line 28 currently running flask run.
2. Confirm waitress is already present in requirements.txt.
3. Replace the CMD with a waitress invocation, e.g. CMD ["waitress-serve", "--host=0.0.0.0", "--port=5000", "app:app"].
4. Confirm the app module/callable name ("app:app") matches the actual Flask app object exposed in app.py.
5. Rebuild the Docker image and start the container to confirm it launches under waitress rather than the Werkzeug dev server.
6. Verify: start the container, check container logs for the waitress startup banner (not the 'WARNING: this is a development server' message), and confirm the app responds correctly to a request.

---

## test_rag_tenant_isolation.py

### Task 61: Near-total lack of automated test coverage relative to codebase size

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81b9892bcb2a03090ff0

**Target File:** `test_rag_tenant_isolation.py`

**Issue Summary:**
Of the three test scripts, only test_rag_tenant_isolation.py is a genuine assertion-based test; the other two are manual smoke scripts. Meanwhile the core pipeline (document_routes.py ~4000 lines, qwen_service.py ~4900 lines) has zero automated coverage - a significant quality gap for a system this size. Add unit tests around field-extraction parsing, ambiguity detection, and the /ask NLU logic, and wire a pytest-based CI job.

**Step-by-step fix logic:**
1. Inventory the three existing test scripts and confirm only test_rag_tenant_isolation.py is genuine assertion-based; treat the other two as smoke scripts to be replaced/supplemented, not relied on.
2. Identify the highest-risk, zero-coverage core logic: field-extraction parsing and ambiguity detection (likely in document_routes.py/qwen_service.py) and the /ask NLU logic.
3. Write pytest unit tests for field-extraction parsing covering typical and malformed extraction outputs.
4. Write pytest unit tests for ambiguity detection logic covering cases that should and shouldn't flag ambiguity.
5. Write pytest tests for the /ask NLU logic covering representative query intents and edge cases (empty/garbled input).
6. Verify: wire a pytest-based CI job (e.g. GitHub Actions) that runs on every push/PR, and confirm it fails on a deliberately broken change to one of the newly-tested functions and passes on the correct code.

---

### Task 62: test_rag_tenant_isolation.py leaves fake chromadb/sentence_transformers stubs in sys.modules

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81febd9de978e16726d6

**Target File:** `test_rag_tenant_isolation.py`

**Issue Summary:**
Lines 40-42 register fake chromadb/chromadb.config/sentence_transformers modules into sys.modules via setdefault at import time, with no teardown to remove them afterward. If combined with other tests in a single pytest session, later tests expecting the real packages silently get these no-op stubs, producing confusing failures. Use unittest.mock.patch.dict(sys.modules, ...) or explicit cleanup to scope the stubs to this module.

**Step-by-step fix logic:**
1. Open test_rag_tenant_isolation.py and locate lines 40-42 where fake chromadb/chromadb.config/sentence_transformers modules are registered into sys.modules via setdefault at import time.
2. Replace the raw sys.modules mutation with unittest.mock.patch.dict(sys.modules, {...}) used as a context manager or fixture, scoped to the tests that need the stubs.
3. If module-level stubbing is required before imports happen, move the stubbing into a pytest fixture (e.g. autouse, module-scoped) that patches sys.modules on entry and restores the original state on exit.
4. Remove the unconditional setdefault calls once the patch.dict-based fixture is in place.
5. Run the full test suite together (not just this file in isolation) to confirm no cross-test contamination remains.
6. Verify: run pytest with this test file alongside another test that imports the real chromadb/sentence_transformers, and confirm that other test sees the real modules, not leftover stubs from this file.

---

## prompt_studio_service.py

### Task 63: No existence check on sample path before test run (prompt_studio_service)

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca814a85a3d404d2202027

**Target File:** `prompt_studio_service.py`

**Issue Summary:**
test_prompt_version_against_sample() (309-311) builds a sample_path and passes it straight to process_document() with no existence check. If both filename fields are empty or the file was purged/moved, process_document() fails with a confusing low-level file-not-found error deep in the pipeline. Add an explicit os.path.isfile() check and raise a clear ValueError first.

**Step-by-step fix logic:**
1. Open prompt_studio_service.py and locate test_prompt_version_against_sample() (309-311), where sample_path is built and passed straight to process_document().
2. Add an explicit check (e.g. os.path.isfile(sample_path)) immediately after building sample_path and before calling process_document().
3. When the check fails, raise a ValueError with a clear message (e.g. naming the missing/empty filename fields or purged/moved sample).
4. Confirm callers of test_prompt_version_against_sample() catch ValueError (or let it propagate to an API layer that already converts it to a clean 4xx response).
5. Add a check for the case where both filename fields are empty, raising the same clear ValueError before even constructing sample_path if applicable.
6. Verify: call test_prompt_version_against_sample() with a sample_path pointing at a purged/nonexistent file and confirm it raises the clear ValueError immediately, rather than a low-level file-not-found error surfacing from deep inside process_document().

---

## pdf_export_service.py

### Task 64: No fallback if ReportLab doc.build() fails during export

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81809329d25459737954

**Target File:** `pdf_export_service.py`

**Issue Summary:**
generate_export_pdf() (65-262) has no try/except around doc.build(story) or the preceding style/table construction. Malformed content raises unhandled straight out of the export endpoint, with no fallback PDF unlike the sibling pdf_highlighted_service.py/pdf_searchable_service.py modules. Wrap the build call in try/except and return a minimal fallback PDF or clear error on failure.

**Step-by-step fix logic:**
1. Open pdf_export_service.py and locate generate_export_pdf() (65-262), including the style/table construction and the doc.build(story) call.
2. Wrap the style/table construction and doc.build(story) call in a try/except.
3. On exception, build and return a minimal fallback PDF (e.g. a simple ReportLab document stating export failed, consistent with the fallback pattern in pdf_highlighted_service.py/pdf_searchable_service.py) or raise a clear, catchable error for the endpoint to handle.
4. Log the caught exception (including document id/content details relevant for debugging malformed content) before falling back.
5. Update the export endpoint to handle the new error/fallback path consistently with how it already handles the sibling services' fallbacks.
6. Verify: pass malformed content designed to break story/table construction into generate_export_pdf() and confirm it returns a fallback PDF or a clean error instead of an unhandled exception.

---

## docker-compose.yml

### Task 65: No HEALTHCHECK defined in Dockerfile or docker-compose.yml

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca8192a827c60536b6c887

**Target File:** `docker-compose.yml`

**Issue Summary:**
Neither file defines a HEALTHCHECK/healthcheck: block for the service. With restart: unless-stopped set but no health probe, Docker can't detect a hung/unresponsive Flask process and won't restart it. Add a HEALTHCHECK hitting a lightweight endpoint on an interval.

**Step-by-step fix logic:**
1. Determine a lightweight health endpoint on the Flask app (e.g. a /health or /healthz route returning 200); add one if it doesn't already exist.
2. Open docker-compose.yml and locate the service definition that has restart: unless-stopped but no healthcheck: block.
3. Add a healthcheck: block specifying a test (e.g. curl -f http://localhost:5000/health or wget), interval, timeout, retries, and start_period.
4. If the Dockerfile is also expected to declare a HEALTHCHECK (e.g. for non-compose docker run usage), add a matching HEALTHCHECK instruction there too.
5. Rebuild/restart the stack with docker-compose up and confirm the container reports a health status.
6. Verify: run docker inspect or docker ps and confirm the service shows 'healthy' status, then simulate a hang (e.g. block the health endpoint) and confirm Docker marks it 'unhealthy' and restarts it per restart: unless-stopped.

---

## pdf_service.py

### Task 66: No page-count cap when rasterizing PDFs at 300 DPI

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81278e81e90fdf168458

**Target File:** `pdf_service.py`

**Issue Summary:**
The rasterization loop (21-27) renders every page of an uploaded PDF at 300 DPI with no limit on page count or output size. A large PDF (hundreds/thousands of pages) generates an equally large number of high-res PNGs synchronously, consuming significant memory/CPU/disk with no backpressure. Add a configurable max-page cap or limit rasterization for very large documents.

**Step-by-step fix logic:**
1. Open pdf_service.py and locate the rasterization loop (21-27) that renders every page at 300 DPI with no limit.
2. Add a configurable max-page constant/env var (e.g. MAX_RASTERIZE_PAGES).
3. Before or during the loop, check the PDF's total page count against the cap; if exceeded, either truncate processing at the cap or route the document to an async/background processing path instead of synchronous rasterization.
4. Surface a clear message/flag to the caller/UI when a document was capped or deferred, so users understand why not all pages were rasterized inline.
5. Consider also capping/lowering DPI for very large documents as a secondary mitigation if a hard page cap isn't acceptable for the product.
6. Verify: upload a PDF with a page count above the configured cap and confirm rasterization stops at (or defers past) the cap rather than synchronously processing all pages, while a normal-sized PDF still processes fully.

---

### Task 67: pdf_to_images() crashes unhandled on corrupt/malformed PDF

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81eb96a3f00d4b50b134

**Target File:** `pdf_service.py`

**Issue Summary:**
pdf_to_images() (7-29) calls fitz.open()/page.get_pixmap() with no try/except. A corrupt, password-protected, or truncated uploaded PDF causes an unhandled exception in the upload processing thread, likely surfacing as an unhandled worker-thread exception rather than a clean per-file error over SSE. Wrap the open/render logic in try/except and report a catchable per-file error.

**Step-by-step fix logic:**
1. Open pdf_service.py and locate pdf_to_images() (7-29), including the fitz.open() and page.get_pixmap() calls.
2. Wrap the fitz.open() call in try/except to catch corrupt, password-protected, or truncated PDFs specifically.
3. Wrap the per-page get_pixmap()/render loop similarly, so a failure on one page doesn't crash the whole worker thread.
4. On failure, return/raise a well-defined, catchable per-file error object (or reuse whatever error-reporting convention document_routes.py's SSE pipeline expects) instead of letting the exception propagate raw.
5. Update the upload processing thread/caller to catch this error and report it over SSE as a clean per-file failure rather than crashing the worker thread.
6. Verify: upload a corrupt and a password-protected PDF and confirm each surfaces as a clean per-file error over SSE, with the upload pipeline continuing to process other files rather than crashing.

---

## ocr_service.py

### Task 68: OCR errors returned as fake extracted text, not raised

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81f799d1fe63fd30cc21

**Target File:** `ocr_service.py`

**Issue Summary:**
In extract_with_tesseract(), the except block (28-30) returns f'OCR extraction failed: {str(e)}' through the same path as legitimate OCR text. Downstream consumers can't distinguish a real failure from actual content, and the error text could be stored as raw_text and shown to users as document content. Raise the exception or return a sentinel plus a separate status flag instead of embedding the error in the text payload.

**Step-by-step fix logic:**
1. Open ocr_service.py and locate extract_with_tesseract(), specifically the except block (28-30) that returns f'OCR extraction failed: {str(e)}' as if it were extracted text.
2. Decide the corrected contract: either raise the caught exception (wrapped in a domain-specific OCRError) or return a tuple/object with a distinct status flag (e.g. (text, success: bool)) instead of embedding the error string in the text payload.
3. Update extract_with_tesseract() to implement the chosen contract, removing the fake-text return.
4. Update all callers of extract_with_tesseract() to handle the new exception or status flag, ensuring the error is not stored as raw_text or shown to users as document content.
5. Check any place that stores/displays raw_text (e.g. Document.raw_text) to confirm it no longer receives OCR error strings once this fix lands.
6. Verify: force extract_with_tesseract() to fail (e.g. corrupt image) and confirm the error is raised/flagged distinctly rather than being persisted or displayed as document text.

---

## ollama-entrypoint.sh

### Task 69: ollama-entrypoint.sh pulls a model name that doesn't match configured OLLAMA_MODEL

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81dfa456e0f6c29eebef

**Target File:** `ollama-entrypoint.sh`

**Issue Summary:**
Line 13 pulls qwen2.5vl:7b, but docker-compose.yml sets OLLAMA_MODEL to a different value, and project docs reference yet other model names. If this script provisions the model server the app talks to, chat-completion calls request a model name that was never pulled, failing with a model-not-found error. Make the pulled model name match (or derive from) the OLLAMA_MODEL env var used by the app.

**Step-by-step fix logic:**
1. Open ollama-entrypoint.sh and locate line 13 which hardcodes 'ollama pull qwen2.5vl:7b'.
2. Open docker-compose.yml and check the OLLAMA_MODEL environment variable value set for the service.
3. Replace the hardcoded model name in ollama-entrypoint.sh with a reference to the OLLAMA_MODEL env var (e.g. ollama pull "${OLLAMA_MODEL}"), providing a sensible default if unset.
4. Audit project docs referencing other model names and update them to match the single source of truth (OLLAMA_MODEL).
5. Confirm the app's chat-completion calls request the same OLLAMA_MODEL value, so the pulled model always matches what's requested.
6. Verify: start the stack with OLLAMA_MODEL set to a specific value, confirm the entrypoint pulls exactly that model, and confirm a chat-completion request succeeds without a model-not-found error.

---

### Task 70: ollama-entrypoint.sh readiness loop has no timeout or failure handling

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca813f8abbc58abd2bdd51

**Target File:** `ollama-entrypoint.sh`

**Issue Summary:**
Lines 7-10 loop until ollama list succeeds with no retry cap; if ollama serve crashes or never becomes ready, the script hangs forever with no diagnostic signal. The exit status of ollama pull is also never checked, so a failed pull silently continues to wait for the server PID, leaving a model-less server running. Add a bounded retry count with a clear failure exit and check the pull command's exit code.

**Step-by-step fix logic:**
1. Open ollama-entrypoint.sh and locate the readiness loop (lines 7-10) that loops until 'ollama list' succeeds with no retry cap.
2. Add a bounded retry counter (e.g. max 30 attempts with a sleep interval) around the readiness check.
3. On exceeding the retry cap, print a clear diagnostic message and exit non-zero rather than hanging forever.
4. Capture the exit status of the 'ollama pull' command (currently unchecked) and, on failure, print a clear diagnostic message and exit non-zero instead of silently continuing to wait on the server PID.
5. Ensure the script only proceeds to wait on the server PID after both the readiness check and the pull have succeeded.
6. Verify: simulate a server that never becomes ready (or a pull that fails) and confirm the script exits with a non-zero status and clear diagnostic message within the bounded retry window, rather than hanging indefinitely.

---

## app.py

### Task 71: Startup schema migrations swallow all exceptions, hiding real DB errors

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca812db549d997241254b0

**Target File:** `app.py`

**Issue Summary:**
Nine migration blocks (176-271) each wrap ALTER TABLE in try/except Exception: pass, relying on the exception meaning 'column already exists' but also swallowing real failures like lost DB connection or insufficient privileges, letting the app start with a silently incomplete schema. Check information_schema.columns before altering (or use Alembic) instead of try/except-as-control-flow, and log exceptions instead of discarding them.

**Step-by-step fix logic:**
1. Open app.py and locate the nine migration blocks (176-271), each wrapping an ALTER TABLE in try/except Exception: pass.
2. For each block, replace the try/except-as-control-flow pattern with an explicit pre-check against information_schema.columns (or the equivalent for the DB in use) to determine whether the column already exists.
3. Only run the ALTER TABLE when the pre-check confirms the column is missing; skip it cleanly (with a log line) when it already exists.
4. For any exception that still occurs during the ALTER TABLE itself (e.g. lost connection, insufficient privileges), log it at error level with enough context to diagnose, rather than silently passing.
5. Consider migrating this ad hoc logic to Alembic (or the project's chosen migration tool) as a longer-term replacement for hand-rolled startup ALTER TABLE blocks.
6. Verify: simulate a real failure (e.g. run against a DB user lacking ALTER privileges) and confirm the app now logs a clear error instead of starting silently with an incomplete schema; also confirm a normal startup with already-migrated columns still starts cleanly with no repeated ALTER attempts.

---

## test_connection.py

### Task 72: test_connection.py and test_qwen.py swallow all exceptions and never fail/assert

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81c8aa07e5208ad89659

**Target File:** `test_connection.py`

**Issue Summary:**
Both test_connection.py (34-40) and test_qwen.py (18-20) wrap checks in a bare try/except that only prints, with no assert/raise/exit(1) on failure. Since both are named test_*.py (matching pytest discovery), they report as 'passed' even when the MySQL connection or LM Studio endpoint is completely unreachable, giving false confidence in CI. Replace the print-and-swallow pattern with real assertions or an explicit non-zero exit code.

**Step-by-step fix logic:**
1. Open test_connection.py (34-40) and test_qwen.py (18-20) and locate the bare try/except blocks that only print on failure.
2. Replace the print-and-swallow pattern with explicit assert statements (for pytest discovery) or sys.exit(1) on failure (for standalone script usage), matching how the file is actually invoked (pytest vs. direct run).
3. Ensure the exception message/details are still surfaced (e.g. via assert with a descriptive message, or printing before exiting non-zero) so failures remain diagnosable.
4. If these files are meant to be pytest-discovered, confirm the test function(s) now genuinely fail the pytest run when MySQL/LM Studio is unreachable.
5. Re-run these under both direct execution and pytest discovery to confirm consistent fail-loud behavior in both modes.
6. Verify: point the connection check at an unreachable MySQL host / LM Studio endpoint and confirm pytest reports a failure (non-zero exit, red test) instead of a false 'passed' result.

---

## rag_service.py

### Task 73: Two-key Chroma where filter may be invalid syntax

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca81f1a8a1d81a583f3c57

**Target File:** `rag_service.py`

**Issue Summary:**
retrieve_similar() (180-188) builds where as a flat dict and mutates it to add a second key. Some chromadb versions require multi-condition filters wrapped in {'$and': [...]} rather than a flat multi-key dict and would raise; the broad except swallows the error and returns [], silently breaking RAG retrieval whenever both filters combine. Use {'$and': [...]} explicitly and log query failures distinctly from 'no matches'.

**Step-by-step fix logic:**
1. Open rag_service.py and locate retrieve_similar() (180-188), where the where filter dict is built flat and then mutated to add a second key.
2. Confirm which chromadb version(s) require multi-condition filters wrapped in {'$and': [...]} versus a flat multi-key dict.
3. Rewrite the where-clause construction to explicitly build {'$and': [{key1: val1}, {key2: val2}]} whenever more than one condition is present, rather than merging keys into a flat dict.
4. Split the broad except around the query call so query-failure exceptions are logged/handled distinctly from the legitimate 'no matches' empty-result case, instead of both collapsing to [].
5. Add a regression check exercising the two-filter path against the pinned chromadb version to catch future incompatibilities.
6. Verify: call retrieve_similar() with both filters set simultaneously against the target chromadb version and confirm it returns actual matching results (not an empty list from a swallowed exception), and confirm a genuine query failure is now logged distinctly from a true no-matches case.

---

## requirements.txt

### Task 74: All requirements.txt dependencies are unpinned

**Notion Reference:** Medium priority — https://app.notion.com/3bcb8d467eca8136af50f80a259bdb94

**Target File:** `requirements.txt`

**Issue Summary:**
Every package in requirements.txt has no version constraint. Rebuilding the Docker image later can silently pull breaking API changes or an unreviewed CVE-affected release (notably risky for cryptography and Pillow). Pin exact or compatible-release versions and update deliberately via a dependency-review process.

**Step-by-step fix logic:**
1. Run pip freeze inside the current working container/venv to capture actually-installed versions for every package listed in requirements.txt.
2. Cross-reference each package (especially cryptography and Pillow) against its changelog/CVE database for known-breaking major version bumps since the currently installed version.
3. Pin each entry to an exact version (==) or a safe compatible-release specifier (~=), choosing versions that avoid known-breaking major bumps and unpatched CVEs.
4. Rebuild the Docker image from the pinned requirements.txt and confirm the app starts and imports succeed with no version-resolution changes.
5. Document a dependency-review process (e.g. dependabot/renovate with manual approval) so future upgrades are deliberate rather than automatic.
6. Verify: rebuild the Docker image twice in a row and confirm pip installs the identical resolved version set both times (no drift), and the app boots cleanly.

---

## Using this file

These are independent bugs across many files — there's no single suggested execution order the way the smaller High-priority batches had one; work through whichever module you're already in, or tackle a cluster (e.g. all of `templates/index.html`) in one pass since several share the same fix pattern (missing `r.ok` checks, missing delete confirmations, keyboard/ARIA gaps).

Once a fix lands, close it out in Notion with `/task <number>` (matching the numbering above, referencing this file) — the skill will verify the fix, write a System Benefit summary, and update Status/Completed Date automatically.
