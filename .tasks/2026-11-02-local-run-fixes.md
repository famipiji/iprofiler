# iProfiler — Local-run fixes — 2026-11-02

## STATUS: In Progress — Tasks 0–5, 7 done (live checks 2026-11-04); 6 To Do

| Task | Status | Commit | Still pending |
|---|---|---|---|
| 0 | ✅ Done | `d0d1dc1` | — (CI green) |
| 1 | ✅ Done | `924e4ff`, `edb74d3`, model switch | Thinking can't be turned off for `qwen/qwen3.5-9b` in LM Studio ([bug #1990](https://github.com/lmstudio-ai/lmstudio-bug-tracker/issues/1990)) → switched to **`qwen3-vl-8b-instruct`** (context 32k). Live 2026-11-02: 2-page scanned bank statement → `bank_statement` 90% + 7 fields, no reasoning errors. Still: upload one digital PDF |
| 2 | ✅ Done | `073cf51` | Live 2026-11-04: re-extract of doc 755 (`dummy_statement.pdf`, bank_statement) returned real values, no silent fallback. Follow-up `Prompts: never invent values`: every field-extraction prompt ends with a hard "only printed values, copy dates/periods as printed, else null" rule (after any Prompt Studio / template override); `statement_period` went from invented `2024-01-15` to the printed period |
| 3 | ✅ Done | Task 3 commit | Live 2026-11-04 type change round trip on doc 755 via PATCH: bank_statement → invoice (fields in 7 s) → bank_statement (16 s); PATCH answers with the new type immediately, `reextract-status` running → done, fields swapped each way |
| 4 | ✅ Done | Task 4 commit | Java = portable Temurin 17 in `jre\` (git-ignored, on PATH via the .bat); no Java → one `[Startup]` warning, ODL skipped. `parse_odl_blocks()` walks ODL's `kids` tree. Image date highlight (`00f2601`, `static/image_highlight.js`): `test_image_highlight.py` green; browser check listed for 2026-11-04 |
| 5 | ✅ Done | `6219886` | `paddle` on in `start_iprofiler.bat` (Compose still `vision`). First live run 47.0 s vs 306.4 s. Live 2026-11-04 not-in-document flag: doc 755 `statement_period` no longer flagged after re-extract (value_check now ignores range connectors like "to"/"until"/"sampai" the model adds between two printed parts). Optional: A/B on ~10 scanned docs incl. messy photos |
| 6 | To Do | — | — |
| 7 | ✅ Done | Task 7 commit | `app.py` reads `FLASK_DEBUG` (default off) and `HOST` (default `127.0.0.1`); `start_iprofiler.bat` leaves both unset with commented `REM set` examples. Verified 2026-11-04 on a spare port: default → bound `127.0.0.1`, LAN request fails, `/console` off; `HOST=0.0.0.0 FLASK_DEBUG=1` → bound `0.0.0.0`, LAN 200, debugger on. Docker unaffected (waitress in `entrypoint.sh`). Note: with the debugger off, saving a `.py` no longer auto-reloads - restart the .bat |

Not from the Notion backlog. These came out of getting iProfiler running directly on the Windows
dev laptop (no Docker) on 2026-10-30 / 2026-11-02 and reading its log. Run tasks **in order, one at
a time**; each ends with a **Verify** step that must pass before the next. Commit after each task
(`Task N: <title>`).

---

## Context (read this first)

**How the app runs locally now**
- Start: double-click `start_iprofiler.bat` (project root). It sets `DB_HOST=192.168.1.18`, runs
  `python app.py`, and tees all output to `iprofiler.log`.
- `iprofiler.log` is written by PowerShell `Tee-Object`, so it is **UTF-16**. Read it with
  `iconv -f utf-16 -t utf-8 iprofiler.log` (or open it in an editor that detects UTF-16).
- Since Task 7 the debugger/auto-reload is off by default (`set FLASK_DEBUG=1` in the .bat to get reload-on-save back).
- **X2 server (192.168.1.18)** hosts both MySQL (`dms_iprofiler`, port 3306, user
  `iprofiler_user@%`) and LM Studio (port 1234).

**Uncommitted local changes (as of 2026-11-02 11:15)**: `app.py` (DB_HOST from env),
`start_iprofiler.bat` (new), `.gitignore` (+ iprofiler.log), `document_routes.py` (type saved before
re-extraction), `learned_structures.json` (changed by the app itself). Task 0 commits them.

**What the log showed**
| Symptom | Log evidence | Real cause |
|---|---|---|
| Digital PDFs come back "unclassified", 65% | `Text stream (51 tok)` → `Classified as: unknown (confidence: 65%)` | Model's classify answer was empty/unparseable; app silently fell back to "unknown" + the 0.65 text-path default |
| Record type set, still "No fields extracted" | `Text call (2000 tok): 41.9s` → `Parsing failed` → generic `Text call (16000 tok): 329.7s` → `Response length: 0 chars` → `Re-extracted 0 fields` | Model generated up to its token limit (~48 tok/s) but **`content` was empty** — almost certainly a *thinking* model whose output goes to the reasoning channel, which `_consume_stream()` (`delta.content` only) never reads |
| Changing record type "doesn't stick" | PATCH runs re-extraction (minutes) before `db.session.commit()` | Fixed locally 2026-11-02 (commit before re-extract) — still need background re-extract + don't wipe fields (Task 3) |
| `'java' command not found` on every digital PDF | `[Pipeline] ODL failed: [WinError 2]` → pdfplumber fallback | No Java on the laptop; opendataloader-pdf needs a JRE (Docker image has one) |
| Percent badge (65% / 90%) | `normalize_confidence()` `SOURCE_DEFAULTS` | It is a fixed per-route default, not a measured accuracy |

**This is not PaddleOCR.** Field extraction and type classification are 100% the LLM. PaddleOCR
only feeds the highlight overlay and the PDF exports (Tasks 1–6 of the PaddleOCR migration).

---

### Task 0: Commit the pending local-run changes

**Target Files:** `app.py`, `start_iprofiler.bat`, `.gitignore`, `document_routes.py`, `learned_structures.json`

1. `git diff` each file and confirm the change is intended (the list above).
2. Commit as `Local run: DB_HOST env, start script, save type before re-extract`.
3. Push (`git push origin main`) from a terminal that has GitHub credentials.
4. **Verify:** `git status` clean; GitHub Actions run for the commit is green (the last known red run
   was on `7faff62`; `7570af7` fixed the test isolation — confirm it actually went green).

---

### Task 1: Make the LLM model explicit + handle "thinking" models

**Target Files:** `start_iprofiler.bat`, `qwen_service.py` (`_consume_stream` ~839, `call_qwen_text` ~919, `call_qwen_vision` ~881)

**Goal:** The app always asks LM Studio for a known model, and a reasoning/thinking model can never
again produce a silent empty answer.

1. In LM Studio on X2, load an **Instruct (non-thinking) vision model** (e.g. `qwen/qwen3-vl-8b`,
   which is what `docker-compose.yml` uses), or turn thinking off for the loaded model. Open
   `http://192.168.1.18:1234/v1/models` and note the exact `id`.
2. `start_iprofiler.bat`: add `if not defined OLLAMA_MODEL set OLLAMA_MODEL=<that id>` (and
   `OLLAMA_BASE_URL=http://192.168.1.18:1234/v1` for clarity).
3. `qwen_service._consume_stream()`: also accumulate `delta.reasoning_content` / `delta.reasoning`
   if present (separately — never mix it into the returned text). Track `finish_reason`.
4. If the returned `content` is empty **and** (reasoning text was received **or**
   `finish_reason == "length"`), raise `QwenServiceUnavailableError` with a clear message, e.g.
   `"<label>: model returned no answer (used N tokens on reasoning / hit max_tokens). Load a
   non-thinking Instruct model or disable thinking in LM Studio."` Same check on the non-stream
   branch of `call_qwen_text` (`response.choices[0].finish_reason`, `message.reasoning_content`).
5. Log the model id LM Studio actually answered with (`response.model` / first chunk `.model`)
   once per process: `[Qwen] Serving model: <id>`.
6. **Verify:** with a thinking model loaded, uploading a digital PDF shows a clear error (not
   "unclassified 65%"); with the Instruct model loaded, the same PDF gets a real type and fields.
   Unit test: fake stream chunks with only `reasoning_content` + `finish_reason="length"` → raises.

---

### Task 2: Stop silent fallbacks when the AI answer is unusable

**Target Files:** `qwen_service.py` (`classify_document` ~1270, `extract_fields_custom` ~2411/2659, `extract_fields_generic` ~1671/1768)

**Goal:** A failed AI answer is visible (log + UI), never disguised as a normal low-confidence result.

1. `classify_document`: if `safe_parse_json(...)` returns `{}` or has no `document_type`, log the
   first 300 chars of the raw response (`[Classify] Unparseable answer: ...`) and return confidence
   from `source="fallback"` (0.35), not the 0.65 text default — or raise if the answer was empty.
2. `extract_fields_custom`: on "Parsing failed", log the first 500 chars of the raw response before
   falling back to generic extraction.
3. `extract_fields_generic`: if the response is empty (`Response length: 0`), raise
   `QwenServiceUnavailableError` instead of returning `{}` — an empty answer is a model problem, not
   "this document has no fields".
4. Make sure the upload flow (`document_routes.process_file_task`) and the PATCH re-extract show
   that error to the user (✗ Error in the progress UI) instead of saving 0 fields.
5. **Verify:** stub `call_qwen_text` to return `""` → upload shows an error, nothing saved as
   "unclassified 65%"; stub it to return junk → log shows the raw snippet. Existing tests still pass.

---

### Task 3: Record-type change — background re-extract, never wipe fields on failure

**Target Files:** `document_routes.py` (`update_document` ~1428), `templates/index.html` (type-change PATCH ~4445–4460)

**Already done locally (2026-11-02):** the new type is committed *before* re-extraction, so it shows
immediately. Remaining:

1. Only replace `doc.fields` when re-extraction returned a non-empty dict. On failure/empty, keep the
   existing fields and return `"reextract": "failed"` + the error in the JSON.
2. Move re-extraction off the request thread (same pattern as uploads: background thread + progress
   via SSE, or reuse the batch `DocumentJob` queue). The PATCH returns immediately with
   `"reextract": "started"`.
3. Frontend: after changing the type, show "Re-extracting fields…" on the document and refresh the
   fields when the job finishes; show the error if it failed. Disable the type selector while a
   re-extract for that document is running (prevents the stacked PATCHes seen in the log).
4. **Verify:** change type → badge updates instantly, page reload keeps it; fields appear when done;
   with the model stopped, old fields stay and an error is shown.

---

### Task 4: Java for opendataloader-pdf on Windows

**Target Files:** `app.py` (startup), `README.md`, `CLAUDE.md`

1. Install a JRE 17+ on the laptop (e.g. Eclipse Temurin), make sure `java -version` works in a new
   terminal, restart via `start_iprofiler.bat`.
2. Add a startup check in `app.py`: `shutil.which("java")` — if missing, print one clear warning
   (`[Startup] Java not found - digital PDFs will use the pdfplumber fallback (worse tables)`)
   instead of two `'java' command not found` lines per PDF.
3. Document the Windows prerequisites (Python 3.12, poppler, JRE 17+, `start_iprofiler.bat`,
   X2 addresses) in `README.md` and the "Commands" section of `CLAUDE.md`.
4. **Verify:** upload a digital PDF → log shows ODL extraction, no `WinError 2`.

---

### Task 5 (after Task 1–2): PaddleOCR text for scanned docs, AI for type + fields (Option B)

**Target Files:** `qwen_service.py` (scanned-PDF path ~3540–3640, image path), `docker-compose.yml`, `test_ocr_hint.py` or a new test file

This is the PaddleOCR migration's **Task 7, option B** (option A — `OCR_PRETEXT_HINT` — is done).

1. New env flag `OCR_TEXT_SOURCE` = `vision` (default, today's behaviour) | `paddle`.
2. With `paddle`: for scanned PDFs and images, build `raw_text` from `ocr_service.ocr_lines()` per
   page (200 DPI render, reuse `_ocr_hints_for_pdf`-style rendering), then run the **text** path:
   `classify_document(raw_text)` + `extract_fields(raw_text, type)`. No Vision transcription calls.
3. Fall back to the Vision path per document when OCR is weak (same guard as the hint:
   < 20 chars or mean conf < 0.80 — covers Jawi) or OCR fails.
4. Log timings so the A/B is easy: `[Pipeline] OCR text source: paddle (N pages, X s)`.
5. **Verify:** A/B on ~10 scanned docs (flag off vs on): document type, fields, total time.
   Keep `paddle` only if fields are as good on your record types; tables/forms are the risk.

---

### Task 6 (low priority): Honest confidence badge

**Target Files:** `qwen_service.py` (`normalize_confidence` ~322), `templates/index.html` (~2917)

1. Store *where* the number came from (`model` vs `default:<route>`), e.g. a `confidence_source`
   value next to `confidence`.
2. UI: when it's a default, show it differently (grey dot / tooltip "Not measured — default for this
   processing route") so 65%/90% isn't read as accuracy.
3. **Verify:** a document classified with a model-supplied confidence shows it normally; a default
   one shows the tooltip.

---

### Task 7 (low priority): Don't expose the debug server on the network by default

**Target Files:** `app.py` (`app.run` ~459), `start_iprofiler.bat`

1. Read `FLASK_DEBUG` (default off) and `HOST` (default `127.0.0.1`) from env; keep the current
   behaviour available via `set FLASK_DEBUG=1` / `set HOST=0.0.0.0`.
2. **Verify:** default start is reachable at `http://localhost:5000` only; Werkzeug debugger off.

---

### Still open from earlier task files
- PaddleOCR migration (`2026-10-27-paddleocr-migration.md`): Task 4/5 UI checks, Docker build check,
  Paddle-vs-Tesseract benchmark, Task 7 option A A/B (`OCR_PRETEXT_HINT`), Task 8 (remove Tesseract).
- `2026-08-19-tasks.md`: 3 backlog tasks still To Do.
