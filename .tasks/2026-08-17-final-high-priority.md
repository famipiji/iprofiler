# Final High-Priority Backlog — iProfiler

Source: iProfiler Backlog (Notion), live query as of 2026-08-17 end of day. These are the **only 4 items left** with `Priority: High` and `Status != Done` — everything else in today's two earlier batches (`2026-08-17-tasks.md`, `2026-08-17-remaining-high-priority.md`) is closed. Three of these (Tasks 1-3 below) have been sitting at **In Progress** since yesterday's backlog pass; Task 4 is the one holdover from the previous batch.

**Status update (2026-08-17, end of day):** All 4 tasks are ✅ **Done** — verified in code and closed out in Notion with System Benefit notes and Completed Date stamps. **This clears every remaining High-priority item in the backlog** — nothing High-priority is left at To Do or In Progress.

---

## Task 1: OCR dependencies (pytesseract, numpy) missing from requirements.txt — ✅ DONE

**Notion Reference:** High priority, In Progress — https://app.notion.com/3bcb8d467eca8141a629dbf42cec55c4

**Target File:** `requirements.txt`
**Scope:** Add two pinned entries.

**Issue Summary:**
`ocr_service.py` and `pdf_highlighted_service.py` both `import pytesseract` (and `numpy`), but neither package is listed in `requirements.txt`. Any call into the OCR or highlighted-PDF export path fails with `ModuleNotFoundError` after a fresh install — including every Docker build, since the image installs strictly from this file.

**Step-by-step fix logic:**
1. Open `requirements.txt`.
2. Add `pytesseract` and `numpy`, pinned to specific versions compatible with the Python 3.12 base image used in the `Dockerfile`.
3. Check `ocr_service.py` and `pdf_highlighted_service.py` for any version-specific API usage (e.g. numpy 2.x breaking changes) before picking pins — don't just take the latest.
4. Run `pip install -r requirements.txt` locally (or as part of the Docker build) and confirm it resolves cleanly against the other pinned packages already in the file.
5. Verify: `import pytesseract` and `import numpy` succeed with no `ModuleNotFoundError`, and a basic OCR call in `ocr_service.py` gets past the import stage.

---

## Task 2: Dockerfile never installs the tesseract-ocr binary pytesseract depends on — ✅ DONE

**Notion Reference:** High priority, In Progress — https://app.notion.com/3bcb8d467eca81a2bd46eef2d6dc6713

**Target File:** `Dockerfile`
**Scope:** `apt-get install` list, lines 4-11.

**Issue Summary:**
The `apt-get install` list only covers `libmupdf-dev`, `mupdf-tools`, `libfreetype6`, `libjpeg62-turbo`, `libopenjp2-7`, and `default-jre-headless` — it never includes `tesseract-ocr`, the system binary `pytesseract` wraps as a subprocess. Even once Task 1 lands, OCR/highlighted-PDF calls in the container will still fail with `TesseractNotFoundError` because the binary isn't on `PATH`.

**Step-by-step fix logic:**
1. Open `Dockerfile` and locate the `apt-get install -y --no-install-recommends` block (lines 4-11) — the same block already carrying the `entrypoint.sh` / templates-bind-mount fix from earlier today.
2. Add `tesseract-ocr` to the package list, plus any language packs (e.g. `tesseract-ocr-eng`) that `ocr_service.py` actually configures — check for a `lang=` parameter or similar before assuming English-only.
3. This depends on Task 1 for full effect (the binary alone doesn't help without the Python wrapper) — land them together and rebuild once.
4. Rebuild the image and confirm the binary resolves on `PATH` inside the container (e.g. `tesseract --version`).
5. Verify: run `extract_with_tesseract()` against a sample image inside a freshly built container and confirm no `TesseractNotFoundError`.

---

## Task 3: RAG dependencies (chromadb, sentence-transformers) missing from requirements.txt — ✅ DONE

**Notion Reference:** High priority, In Progress — https://app.notion.com/3bcb8d467eca81c4a529c62d7ace52c1

**Target File:** `requirements.txt`
**Scope:** Add two pinned entries.

**Issue Summary:**
`rag_service.py` imports `chromadb` and `sentence_transformers`, but neither appears in `requirements.txt`. A clean `pip install` followed by any RAG code path (embed, retrieve, delete) raises `ModuleNotFoundError` at runtime, in both local and Docker deployments.

**Step-by-step fix logic:**
1. Open `requirements.txt` and add pinned `chromadb` and `sentence-transformers` entries.
2. Note that `sentence-transformers` pulls in `torch` as a transitive dependency — this meaningfully increases image size and build time. Consider whether the Docker build needs a CPU-only torch wheel index (`--extra-index-url` pointing at the CPU build) to avoid pulling CUDA wheels the container will never use.
3. Run `pip install -r requirements.txt` and confirm it resolves without version conflicts against the `numpy` pin added in Task 1 (both `sentence-transformers` and `chromadb` have their own numpy constraints).
4. Verify: `import chromadb` and `import sentence_transformers` succeed, and a smoke embed/retrieve call through `rag_service.py` completes without `ModuleNotFoundError`.

---

## Task 4: Delete/toggle fetch handlers ignore non-2xx responses and report false success — ✅ DONE

**Notion Reference:** High priority, To Do — https://app.notion.com/3bcb8d467eca81d59adecdb70f869d66

**Target File:** `templates/index.html`
**Scope:** `st-confirm-ok` delete handler (~7654-7664), `stToggle` (~7634-7641), `stLoadTypes` (~6936-6944), `stDeleteFieldTypeHint` (~6977-6982).

**Issue Summary:**
All four handlers do `fetch(...).then(r => r.json())` without checking `r.ok`. A 4xx/5xx response with a JSON error body still resolves, so a failed delete/toggle shows a "success" toast and closes the dialog — the user believes the action succeeded when it didn't. (Carried over from the previous batch — not yet fixed.)

**Step-by-step fix logic:**
1. Open `templates/index.html` and locate each of the four handlers listed above.
2. Find `stSave`'s fetch handling — it already does `if (!r.ok) throw ...` — and copy that guard into each of the four handlers, before the `.json()` parse.
3. Make sure the resulting `catch`/error path in each handler surfaces a real failure message to the user, matching whatever error-toast pattern `stSave` uses — not a silent console-only log.
4. Since this is the same fix repeated four times, consider extracting one shared `fetchJsonOrThrow(url, opts)` helper used by all four (and by `stSave` too, if convenient).
5. Verify: force a 4xx/5xx response for each of the four actions and confirm each now shows a failure state instead of a false "success" toast.

---

## Suggested execution order

1. **Task 1** (`requirements.txt` — pytesseract/numpy) — foundational, small, unblocks Task 2.
2. **Task 2** (`Dockerfile` — tesseract-ocr binary) — land right after Task 1 and rebuild once; OCR only fully works once both are in.
3. **Task 3** (`requirements.txt` — chromadb/sentence-transformers) — independent of 1/2, but check for the torch dependency-bloat question before committing.
4. **Task 4** (`templates/index.html` — fetch handlers) — isolated frontend fix, no dependency on the others.

---

## Claude Code execution commands

```
claude "Read final-high-priority.md and implement the fixes for Task 1 and Task 2: add pytesseract/numpy to requirements.txt and add the tesseract-ocr system package to the Dockerfile's apt-get install list. Follow the step-by-step fix logic for both exactly, and land them together since OCR needs both to work."
```

```
claude "Read final-high-priority.md and implement the fix for Task 3: add chromadb and sentence-transformers to requirements.txt, watching for the torch transitive-dependency size impact noted in the fix logic. Follow the step-by-step fix logic exactly."
```

```
claude "Read final-high-priority.md and implement the fix for Task 4: Delete/toggle fetch handlers ignoring non-2xx responses in templates/index.html (all four handlers listed). Follow the step-by-step fix logic exactly."
```

Once each is implemented, close it out in Notion with `/task <number>` (matching the numbering above) — the skill will verify the fix, write a System Benefit summary, and update Status/Completed Date automatically.
