# Remaining High-Priority Backlog — iProfiler

Source: iProfiler Backlog (Notion). All items below were **Priority: High**, **Status: To Do**, as of 2026-08-17. (Tasks already closed earlier today — Hardcoded tesseract path, recognize_for_ivault, Qwen retry/backoff, currency-field mislabeling, DELETE file cleanup, upload limits, await_confirmation false success — are not repeated here; RAG deps / OCR deps / Dockerfile tesseract binary remain **In Progress**, also not repeated here.)

**Status update (2026-08-17, end of day):** All 8 tasks are now ✅ **Done** — verified in code and closed out in Notion with System Benefit notes and Completed Date stamps. **Task 5** (Delete/toggle fetch handlers ignoring non-2xx responses) was closed last, as Task 4 in [`2026-08-17-final-high-priority.md`](./2026-08-17-final-high-priority.md).

---

## Task 1: templates bind mount in docker-compose.yml can shadow baked-in templates — ✅ DONE

**Notion Reference:** High priority — https://app.notion.com/3bcb8d467eca8100aa30caf76265eef9

**Target File:** `docker-compose.yml`
**Scope:** Line 11 (the `./templates:/app/templates` bind mount).

**Issue Summary:**
Line 11 mounts `./templates:/app/templates` on top of the image's built-in templates directory. If the host's `./templates` doesn't exist or is empty at deploy time, Docker binds an empty directory over the baked-in templates, hiding `index.html` and breaking every `render_template()` call.

**Step-by-step fix logic:**
1. Open `docker-compose.yml` and locate the `./templates:/app/templates` bind mount (line 11).
2. Decide the intended behavior: either remove the bind mount entirely and rely on the image's baked-in copy of `templates/`, or keep it only for local development where the host directory is guaranteed populated.
3. If removing: delete the volume line and rebuild the image to confirm `templates/index.html` is present and served correctly with no bind mount involved.
4. If keeping (e.g. for hot-reloading templates in dev): add a startup check (in the Dockerfile `CMD`/entrypoint or a pre-flight script) that fails fast with a clear error if `templates/index.html` is missing from the mounted directory, instead of silently serving a blank/broken app.
5. Document the decision in `docker-compose.yml` with a comment so a future deploy doesn't reintroduce the same gap.
6. Verify: run `docker-compose up` with an empty or missing host `./templates` directory and confirm the app either serves correctly (image copy used) or fails loudly with a clear message — never silently breaks `render_template()`.

---

## Task 2: No delete confirmation before permanently destroying a document — ✅ DONE

**Notion Reference:** High priority — https://app.notion.com/3bcb8d467eca815891ade550eeba3746

**Target File:** `templates/index.html`
**Scope:** `.btn-delete` click handler, line 3332.

**Issue Summary:**
The `.btn-delete` click handler calls `deleteDoc` immediately with zero `confirm()` dialog, unlike the equivalent destructive flows elsewhere in the file which do require confirmation. A single mis-click permanently destroys a document and its extracted data with no undo.

**Step-by-step fix logic:**
1. Open `templates/index.html` and locate the `.btn-delete` click handler (~line 3332).
2. Find the confirmation-modal pattern already used elsewhere in the file for other destructive actions (e.g. the field type hint delete, or the guided-correction cancel flow) and reuse the same component/style rather than introducing a second confirmation UI.
3. Wrap the call to `deleteDoc` behind that confirmation step — only invoke it once the user explicitly confirms.
4. Make sure the confirmation copy names the specific document (title/filename) being deleted, not a generic "are you sure?", so a user scanning quickly still catches a wrong-target click.
5. Verify: click delete on a test document, confirm the modal appears and blocks the delete until confirmed, and confirm cancelling leaves the document untouched.

---

## Task 3: One failing document aborts entire retention purge batch — ✅ DONE

**Notion Reference:** High priority — https://app.notion.com/3bcb8d467eca819c8540e3174b9468aa

**Target File:** `retention_service.py`
**Scope:** `purge_expired_documents()` (101–122) and `purge_documents()` (125–140).

**Issue Summary:**
Both functions call `_purge_document()` per document with no `try/except` around the call. One document's delete/commit failure aborts processing of all remaining documents in that run, including right-to-erasure requests.

**Step-by-step fix logic:**
1. Open `retention_service.py` and locate `purge_expired_documents()` (~101–122) and `purge_documents()` (~125–140).
2. Wrap each per-document call to `_purge_document()` in its own `try/except` inside the loop, so one failure doesn't propagate out and stop the batch.
3. On a per-document failure, log it with enough context (document id, error) to follow up, and continue to the next document rather than returning/raising.
4. Track and return/report a summary at the end of the run — how many succeeded, how many failed and why — so a caller (or the person triggering purge) can see partial failures instead of assuming a clean run.
5. Pay particular attention to right-to-erasure requests specifically: make sure a failure on one such request doesn't silently block other pending erasure requests in the same batch, given the compliance angle.
6. Verify: force `_purge_document()` to fail for one document in a batch of several and confirm the rest still get purged, with the failure logged and reflected in the run summary.

---

## Task 4: Empty TYPE: line crashes process_document — ✅ DONE

**Notion Reference:** High priority — https://app.notion.com/3bcb8d467eca81c9ac50e5cddfd58cb4

**Target File:** `qwen_service.py`
**Scope:** Several `TYPE:` parsing sites (~2519, 2523, 2629, 2636).

**Issue Summary:**
Several places parse the model's `TYPE: ...` line via `type_part.replace('TYPE:','').strip().lower()...split()[0]`. If the model emits `TYPE:` with nothing after it, `split()` returns an empty list and indexing `[0]` throws an unhandled `IndexError`, aborting the entire document job.

**Step-by-step fix logic:**
1. Open `qwen_service.py` and locate each of the four `TYPE:` parsing sites (~2519, 2523, 2629, 2636).
2. At each site, check the length of the `split()` result before indexing `[0]`.
3. When the split result is empty, default to `'other'` (or whatever the established fallback classification is elsewhere in this file) instead of raising.
4. Since this pattern is duplicated four times, consider factoring it into one small helper (e.g. `_parse_type_line(text)`) so future changes to this parsing logic only need to happen once — optional, but worth it given the duplication.
5. Log when the fallback path is hit (model returned an empty `TYPE:`), so this is visible for tuning prompts later rather than silently defaulting forever.
6. Verify: feed a mocked model response with `TYPE:` followed by nothing (or only whitespace) into each of the four call sites and confirm none of them raises — all fall back to `'other'` and the document job completes.

---

## Task 5: Delete/toggle fetch handlers ignore non-2xx responses and report false success — ✅ DONE

**Notion Reference:** High priority — https://app.notion.com/3bcb8d467eca81d59adecdb70f869d66

**Target File:** `templates/index.html`
**Scope:** `st-confirm-ok` delete handler (~7654–7664), `stToggle` (~7634–7641), `stLoadTypes` (~6936–6944), `stDeleteFieldTypeHint` (~6977–6982).

**Issue Summary:**
All four handlers do `fetch(...).then(r => r.json())` without checking `r.ok`. A 4xx/5xx response with a JSON error body still resolves, so a failed delete shows a "deleted" success toast and closes the dialog — the user believes the action succeeded when it didn't.

**Step-by-step fix logic:**
1. Open `templates/index.html` and locate each of the four handlers listed above.
2. Find `stSave`'s fetch handling (referenced in the Notion description as the correct pattern already used elsewhere) and copy its `if (!r.ok) throw ...` check.
3. Add that same guard to each of the four handlers, before the `.json()` parse, so a non-2xx response is treated as a failure.
4. Make sure the resulting `catch`/error path in each handler surfaces a real failure message to the user (not a silent console-only log) — matching whatever error-toast pattern `stSave` uses.
5. Since this is the same fix repeated four times, this is a good candidate to extract into one small `fetchJsonOrThrow(url, opts)` helper used by all four (and by `stSave` too, if not already) — optional but reduces future drift.
6. Verify: force a 4xx/5xx response from the backend for each of the four actions (e.g. temporarily throw in the corresponding route, or hit a bad id) and confirm each now shows a failure state instead of a false "success" toast.

---

## Task 6: Unescaped field values crash PDF export on '&'/'<'/'>' characters — ✅ DONE

**Notion Reference:** High priority — https://app.notion.com/3bcb8d467eca81ef867ecf370ee76a21

**Target File:** `pdf_export_service.py`
**Scope:** `generate_export_pdf()`, `field_rows` construction (~181–189).

**Issue Summary:**
`generate_export_pdf()` builds `field_rows` with `Paragraph(f'<b>{val}</b>' ...)` directly from extracted field values with no escaping, unlike `raw_text` which is escaped at line 239. Any field value containing `&`, `<`, or `>` (e.g. "Johnson & Johnson") produces malformed ReportLab markup and raises an exception inside `doc.build()`.

**Step-by-step fix logic:**
1. Open `pdf_export_service.py` and locate `generate_export_pdf()`'s `field_rows` construction (~181–189).
2. Find the escaping function/pattern already used for `raw_text` at line 239 (likely something like `xml.sax.saxutils.escape` or a ReportLab-specific helper).
3. Apply that exact same escaping to every flattened field key and value before they're interpolated into the `Paragraph(f'<b>{val}</b>' ...)` markup — both the key and the value, since either could contain these characters.
4. Double-check nested/structured field values (lists, dicts flattened to strings) are escaped after flattening, not before, so the final string going into `Paragraph` is always safe.
5. Verify: export a document with a field value containing `&`, `<`, and `>` (e.g. "Johnson & Johnson", "Value < 100", "A > B") and confirm the PDF builds successfully with the characters rendered correctly rather than raising inside `doc.build()`.

---

## Task 7: Crash when Qwen returns empty classify response — ✅ DONE

**Notion Reference:** High priority — https://app.notion.com/3bcb8d467eca81f4bd58e4f7e12445f3

**Target File:** `qwen_service.py`
**Scope:** `quick_classify_from_image()` (~855).

**Issue Summary:**
`result = result.lower().strip().split()[0]` raises `IndexError` if the model returns an empty/whitespace-only string, crashing the file-processing thread with no fallback.

**Step-by-step fix logic:**
1. Open `qwen_service.py` and locate `quick_classify_from_image()` (~855).
2. Before indexing `split()[0]`, check `if not result.strip(): return 'other'` (or whichever default classification is used elsewhere in this file for "couldn't classify").
3. Log when this fallback triggers, the same way as suggested for Task 4, so empty-response cases are visible rather than silently swallowed forever.
4. Since this is effectively the same failure class as Task 4 (indexing into an empty `split()` result from a model response), consider whether the same helper function proposed there could cover this call site too — worth doing both fixes together if convenient.
5. Verify: mock the Qwen client to return an empty string from `quick_classify_from_image` and confirm it returns the fallback classification instead of raising.

---

## Task 8: Undefined _prompt_override_ctx causes NameError — ✅ DONE

**Notion Reference:** High priority — https://app.notion.com/3bcb8d467eca81f8886dd03738ba30b6

**Target File:** `prompt_studio_service.py`
**Scope:** `get_prompt_override_for_key()`, line 24.

**Issue Summary:**
`get_prompt_override_for_key()` does `getattr(_prompt_override_ctx, 'overrides', {})`, but `_prompt_override_ctx` is never imported or defined anywhere in the file. Any call to this function raises `NameError`.

**Step-by-step fix logic:**
1. Open `prompt_studio_service.py` and locate `get_prompt_override_for_key()` (line 24).
2. Check `qwen_service.py` for `set_prompt_overrides`/`clear_prompt_overrides` (lines 13–20 per the Notion description) — these almost certainly manage the real context object this function is meant to read from.
3. Import that real context object from `qwen_service.py` into `prompt_studio_service.py` and use it in place of the undefined `_prompt_override_ctx`.
4. If, on inspection, this function turns out to be dead code no longer called anywhere, removing it is a valid alternative to importing a context object — confirm which is true before choosing.
5. Verify: call `get_prompt_override_for_key()` directly (or trigger whatever flow invokes it) and confirm it returns the actual overrides dict instead of raising `NameError`.

---

## Suggested execution order

1. **Task 8** (`prompt_studio_service.py`) — guaranteed crash on every call, smallest fix, isolated.
2. **Task 4** and **Task 7** (`qwen_service.py`) — same failure class (unguarded `split()[0]` on possibly-empty model output), fix together; core pipeline crash risk.
3. **Task 6** (`pdf_export_service.py`) — isolated, crash-on-ordinary-data risk (e.g. company names with "&").
4. **Task 5** (`templates/index.html`, 4 handlers) — same fix pattern repeated four times; do as one pass.
5. **Task 2** (`templates/index.html`) — isolated UX/safety fix, no dependency on the others.
6. **Task 3** (`retention_service.py`) — isolated, but touches compliance-sensitive purge logic; test carefully.
7. **Task 1** (`docker-compose.yml`) — deployment-config fix; do last since it needs a rebuild/redeploy to verify, unlike the others which are pure code changes.

**Tasks 1, 2, 3, 4, 6, 7, 8 completed 2026-08-17.** Only **Task 5** (fetch handlers in `templates/index.html`) remains open.

---

## Claude Code execution commands

```
claude "Read remaining-high-priority.md and implement the fix for Task 8: Undefined _prompt_override_ctx causes NameError in prompt_studio_service.py. Follow the step-by-step fix logic exactly."
```

```
claude "Read remaining-high-priority.md and implement the fixes for Task 4 and Task 7 in qwen_service.py (Empty TYPE: line crash, and empty classify response crash). Follow the step-by-step fix logic for both exactly."
```

```
claude "Read remaining-high-priority.md and implement the fix for Task 6: Unescaped field values crash PDF export in pdf_export_service.py. Follow the step-by-step fix logic exactly."
```

```
claude "Read remaining-high-priority.md and implement the fix for Task 5: Delete/toggle fetch handlers ignore non-2xx responses in templates/index.html (all four handlers listed). Follow the step-by-step fix logic exactly."
```

```
claude "Read remaining-high-priority.md and implement the fix for Task 2: No delete confirmation before permanently destroying a document in templates/index.html. Follow the step-by-step fix logic exactly."
```

```
claude "Read remaining-high-priority.md and implement the fix for Task 3: One failing document aborts entire retention purge batch in retention_service.py. Follow the step-by-step fix logic exactly."
```

```
claude "Read remaining-high-priority.md and implement the fix for Task 1: templates bind mount in docker-compose.yml can shadow baked-in templates. Follow the step-by-step fix logic exactly."
```

Once each is implemented, close it out in Notion with `/task <number>` (matching the numbering above) — the skill will verify the fix, write a System Benefit summary, and update Status/Completed Date automatically.
