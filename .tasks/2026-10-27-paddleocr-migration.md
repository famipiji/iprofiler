# iProfiler — Tesseract → PaddleOCR Migration — 2026-10-27

## STATUS: In Progress — Tasks 1–6 done (UI checks + Tesseract comparison pending); Task 7 option A implemented (A/B pending); next: Task 7 A/B, then Task 8

Not from the Notion backlog — a scoped engine swap. Run tasks **in order, one at a time**;
each ends with a **Verify** step that must pass before starting the next. Commit after
each task (`Task N: <title>`), same as the batch-processing phase.

---

## Where OCR actually lives today (read this first)

The main extraction pipeline does **not** use Tesseract. Text for scanned PDFs and images
comes from **Qwen3-VL** (`qwen_service.py` ~3395–3500, `call_qwen_vision` with
`ocr_only_prompt`), and digital PDFs use **opendataloader-pdf (ODL)**. Tesseract only
shows up in these places:

| # | Location | What it does | Current engine |
|---|---|---|---|
| A | `ocr_service.py` → `extract_with_tesseract()` | Plain text from an image | Tesseract — **dead code, nothing imports it** |
| B | `pdf_highlighted_service.py` → `_get_word_boxes()` (~244) | Word boxes for the *Highlighted PDF* export | `pytesseract.image_to_data` |
| C | `pdf_searchable_service.py` → `_searchable_from_image/_pdf` (~190, ~238) | Text layer for the *Searchable PDF* export | `ocrmypdf` (= Tesseract inside). **`ocrmypdf` is not in requirements.txt**, so in practice it always falls through to the fallback, which spreads `raw_text` evenly over the page (Ctrl+F hits land in the wrong place) |
| D | `qwen_service.py` → `build_image_blocks()` (~2757), called by `compute_text_blocks_lazy()` (~2800) | Line bboxes for the in-app highlight overlay on scanned PDFs/images | **Qwen vision call** asking for JSON bboxes (10 s timeout, often inaccurate) |

So "switch to PaddleOCR" means: replace **B** and **C**, delete/replace **A**, and — the
biggest win — replace **D**, where an LLM is guessing coordinates that a real
detector does much better and faster. Qwen stays the extraction engine. Feeding PaddleOCR
text *into* Qwen is optional (Task 7).

### Key behaviour differences to design around
- PaddleOCR returns **text lines** (polygon + text + score 0–1), not words. Tesseract
  returned words with conf 0–100. Task 2 adds a line→word splitter so B/C keep working.
- `PaddleOCR` instances are **heavy (~300–600 MB RAM, a few seconds to load) and not
  thread-safe**. `document_routes.py` runs a thread per file, so use one lazily-loaded
  singleton guarded by a lock (same lazy pattern as `rag_service.get_embedder`).
- Models download on first use to `~/.paddlex/` (Linux) / `C:\Users\<you>\.paddlex\`
  (Windows). Docker needs a volume for this or it re-downloads on every container rebuild.
- Malay (Rumi) is plain Latin script → `lang="en"` is fine. **Jawi** would need the
  Arabic model (`lang="ar"`) and is out of scope here (see Task 7 note).

---

### Task 1: Install PaddleOCR + add config

**Target Files:** `requirements.txt`, `Dockerfile`, `docker-compose.yml`

**Goal:** Get PaddleOCR importable locally and in Docker without touching any app code yet.

**Step-by-step implementation:**
1. Locally (in the project venv), install and see what resolves:
   ```bash
   pip install paddlepaddle paddleocr
   ```
   If `paddlepaddle` has no wheel for your Python, use the official CPU index:
   `pip install paddlepaddle -i https://www.paddlepaddle.org.cn/packages/stable/cpu/`
2. Pin the **exact** versions that installed (`pip show paddleocr paddlepaddle`) in
   `requirements.txt` under a new `# OCR (PaddleOCR)` block. Leave `pytesseract` in place
   for now — it's the fallback until Task 8.
3. Smoke test the API you actually got (PaddleOCR 3.x):
   ```python
   from paddleocr import PaddleOCR
   ocr = PaddleOCR(lang="en",
                   use_doc_orientation_classify=False,
                   use_doc_unwarping=False,
                   use_textline_orientation=False)
   res = ocr.predict("uploads/<any>.jpeg")[0]
   print(res["rec_texts"][:5], res["rec_scores"][:5], res["rec_polys"][:1])
   ```
   If the key names differ in your installed version, note the real ones at the top of
   `ocr_service.py` in Task 2 — every later task relies on them.
4. `Dockerfile`: paddle needs `libgomp1` (and sometimes `libgl1`, `libglib2.0-0` for
   OpenCV) — add them to the existing `apt-get install` line.
5. `docker-compose.yml`: add a named volume `paddle_models:/root/.paddlex` to the app
   service, and env vars `OCR_ENGINE=paddle` and `PADDLE_OCR_LANG=en`.
6. **Verify:** smoke test prints real text from a sample upload locally; `docker-compose
   build` succeeds and `docker-compose run <app> python -c "import paddleocr"` works.

**Result (2026-10-27): ✅ Done locally — Docker verify still pending** (Docker isn't installed on
the dev machine; run `docker-compose build` + `docker-compose run vaultmind python -c "import paddleocr"`
where Docker is available).
- Pinned `paddlepaddle==3.2.2`, `paddleocr==3.7.0`, `paddlex==3.7.2`; `numpy` 2.4.2 → **2.3.5**
  (paddlex requires `numpy<2.4`). Full `requirements.txt` dry-run resolves with no conflicts.
- **Don't bump paddlepaddle to 3.3.x**: CPU `predict()` crashes with `NotImplementedError:
  ConvertPirAttribute2RuntimeAttribute not support [pir::ArrayAttribute<pir::DoubleAttribute>]`
  (oneDNN executor). `enable_mkldnn=False` avoids it but is ~5× slower (42–46 s/image).
- Result keys in 3.7.0 match the plan: `rec_texts`, `rec_scores` (0–1), `rec_polys` (4×2 int16).
  There is also **`rec_boxes`** (already axis-aligned `[x0,y0,x1,y1]`) — Task 2 can use it
  instead of computing min/max of the polygons.
- Warm timing on a 2236×1036 receipt JPEG (CPU, Windows dev machine): default server models **28 s** / 49 clean
  lines; `PP-OCRv5_mobile_det` + `en_PP-OCRv5_mobile_rec` **9.6 s** / 102 lines (also picks up
  the faint background watermark text). Task 2 should make the model choice configurable
  (e.g. `PADDLE_OCR_MODEL=mobile|server`) and probably downscale large images before predict.
- **Windows import-order trap:** `import paddle` *before* `torch` breaks torch with
  `OSError: [WinError 127] ... torch\lib\shm.dll` — both wheels ship `libiomp5md.dll` and
  paddle's older copy wins. `import paddleocr` alone is safe (paddlex → modelscope imports
  torch first), and `import torch; import paddle` is safe. Task 2: `ocr_service.py` must never
  `import paddle` directly; do `try: import torch` before `from paddleocr import PaddleOCR`
  with a comment explaining why. (Linux/Docker are unaffected.)
- Dockerfile: added `libgomp1`, `libgl1`, `libglib2.0-0` (paddlex pulls the full
  `opencv-contrib-python`, not headless). Compose: `paddle_models:/root/.paddlex` volume +
  `OCR_ENGINE`/`PADDLE_OCR_LANG` on both `vaultmind` and `batch-worker`.


**Windows re-check (2026-10-27): ✅ Passed.** `paddle.utils.run_check()` OK; `paddle_smoke_test.py`
on Windows (torch imported first — see import-order trap above):

| Models | Load | Time / image | Lines | Sample |
|---|---|---|---|---|
| mobile | 0.6 s | 9.0–9.3 s | 102 | `Maybank Islamic`, `14PAID`, `KUALA LUMPUR MAIN` |
| server | 1.4 s | 26.5–28.0 s | 49 | `Maybank Islamic`, `PIABG14STOOPDPTOPAID` (garbled), `KUALA LUMPUR MAIN` |

- Decision: **default `PADDLE_OCR_MODEL=mobile`** — 3× faster and read this receipt at least as well.
- Only one distinct sample so far (the 3 uploads were the same receipt) — Task 6 needs varied files.
- `ReduceMeanCheckIfOneDNNSupport` log spam is harmless paddle logging; Task 2 may silence it.
- Docker check still outstanding (only matters if you deploy with Docker).
- Models cache to `C:\Users\<you>\.paddlex\`. Set `OCR_ENGINE` / `PADDLE_OCR_LANG` as Windows env
  vars when not using compose. Tesseract fallback on Windows still needs `TESSERACT_CMD`.

---

### Task 2: Rewrite `ocr_service.py` as the single OCR entry point

**Target File:** `ocr_service.py`

**Goal:** One module that owns the engine, so no other file imports `pytesseract` or
`paddleocr` directly.

**Step-by-step implementation:**
1. Keep `OCRError` exactly as is (its docstring explains why it matters).
2. Add `OCR_ENGINE = os.environ.get("OCR_ENGINE", "paddle")` (`"paddle"` | `"tesseract"`)
   and `PADDLE_OCR_LANG = os.environ.get("PADDLE_OCR_LANG", "en")`.
   Also (from Task 1's timings): `PADDLE_OCR_MODEL` = `mobile` (default) | `server`
   — mobile = `text_detection_model_name="PP-OCRv5_mobile_det"`,
   `text_recognition_model_name="en_PP-OCRv5_mobile_rec"` (~3× faster on CPU) — and
   `PADDLE_OCR_MAX_SIDE` (default `1600`): downscale the image so its longest side is at
   most this before `predict()`, then scale boxes back up to the original pixel size.
3. Add a lazy singleton: `_get_paddle()` builds `PaddleOCR(...)` (args from Task 1) once,
   behind a `threading.Lock()`; plus a second lock `_paddle_infer_lock` held around every
   `.predict()` call (instances are not thread-safe).
4. Add the shared core:
   ```python
   def ocr_lines(img: PIL.Image) -> list[dict]:
       # -> [{"text", "box": (x0, y0, x1, y1) in pixels, "conf": 0..1}, ...]
   ```
   Convert to `np.array(img.convert("RGB"))`, run predict, and take boxes from
   `rec_boxes` (already axis-aligned `[x0,y0,x1,y1]` in 3.7.0 — no polygon math needed).
   Drop empty text and `conf < 0.5` (also filters most faint watermark text the mobile
   model picks up). Log the predict time per image: `[OCR] paddle 1.8s, 42 lines`.
   Sort top-to-bottom, then left-to-right.
5. Add derived helpers:
   - `ocr_words(img) -> list[dict]` — returns **Tesseract-shaped** dicts
     `{text, left, top, width, height, conf}` (conf scaled to 0–100) by splitting each
     line on spaces and dividing the line box horizontally in proportion to each word's
     character count (+1 for the space). This keeps Task 3 a near drop-in.
   - `extract_text(image_path) -> str` — lines joined with `\n`; raises `OCRError` on any
     failure.
   - `ocr_line_blocks_normalized(img, page=1)` — `{"page", "text", "bbox": {x1,y1,x2,y2}}`
     with coords divided by image width/height (0–1, top-left origin) — the exact shape
     `build_image_blocks()` returns today (Task 4).
6. Tesseract path: if `OCR_ENGINE == "tesseract"` **or** paddle fails to import/load, the
   same functions fall back to the current pytesseract code (move the existing
   `image_to_data` logic from `pdf_highlighted_service.py` here). Log once which engine is
   active: `[OCR] Engine: paddle (lang=en)`.
7. Rename `extract_with_tesseract` → keep it as a thin alias to `extract_text` so nothing
   breaks if something external called it.
8. **Verify:** quick script against 3 samples (one JPEG, one PNG, one page rasterized from a
   scanned PDF in `uploads/`): print `len(ocr_lines)`, the predict time, first 5 lines, and confirm
   `ocr_words` boxes are inside the image bounds. Run once with `OCR_ENGINE=tesseract` to
   confirm the fallback still works.

**Result (2026-10-27): ✅ Paddle path verified — Tesseract path verified only up to the binary**
(Tesseract isn't installed on the Windows dev machine).

| Sample | Size | `ocr_lines` | Time | Words out of bounds | Blocks in 0–1 |
|---|---|---|---|---|---|
| `86185fb3….jpg` (MyKad) | 800×524 | 12 | 0.7–1.1 s | 0 / 22 | ✅ |
| `0c92734b….png` (invoice) | 1536×1024 | 10 | 1.0–1.3 s | 0 / 16 | ✅ |
| `0503805e….pdf` p1, scanned, 200 DPI | 1654×2339 | 65 | 4.4–4.9 s | 0 / 209 | ✅ |

- Text is accurate (`KAD PENGENALAN`, `840403-12-5089`, `INV-2023-1056`, `SIMEN UTARA SDN. BHD. (Company No: 117752-D)`);
  one OCR slip seen: `ABC Suppiies Ltd.` (should be `Supplies`).
- BGR assumption checked: `predict(ndarray BGR)` == `predict(path)` (identical texts, score diff 0.0).
- Fallback routing: `OCR_ENGINE=tesseract` and a forced `paddleocr` import failure both switch to
  Tesseract with a log line; with no binary they raise `OCRError` (`TesseractNotFoundError` cause),
  never return error text. Tesseract parsing/line-grouping checked with a mocked `image_to_data`.
  **Still to do where Tesseract is installed:** one real `OCR_ENGINE=tesseract` run.
- Behaviour change: Tesseract confs like `"91.5"` (newer builds) are now parsed; the old
  `isdigit()` check silently dropped those words.
- `ReduceMeanCheckIfOneDNNSupport` spam not silenced (it's printed from paddle's C++ side).
- `extract_with_tesseract` is now an alias of `extract_text` (uses the configured engine).

---

### Task 3: Highlighted PDF export → PaddleOCR

**Target File:** `pdf_highlighted_service.py`

**Goal:** Replace the direct `pytesseract.image_to_data` call (B).

**Step-by-step implementation:**
1. Remove `import pytesseract`; `from ocr_service import ocr_words`.
2. Make `_get_word_boxes(img)` just `return ocr_words(img)` inside the existing
   `try/except → []`. The returned dict shape is unchanged, so `_highlighted_from_image`
   (~66) and `_highlighted_from_pdf` (~99) need no changes.
3. Keep `_get_word_boxes_cached` as is, but add the engine name to the cache key
   (`f"{OCR_ENGINE}:{doc_id}:..."`) so switching engines doesn't serve stale Tesseract boxes.
4. Update the "Tesseract" comments/docstrings to say OCR engine.
5. **Verify:** export a Highlighted PDF for one image doc and one scanned-PDF doc from the
   UI (`GET /api/documents/<id>/highlighted` — route at `document_routes.py` ~4036).
   Yellow boxes should sit on the words; Ctrl+F in the PDF finds and lands on them.
   Compare side-by-side with a `OCR_ENGINE=tesseract` export of the same doc.

**Result (2026-10-27): ✅ Paddle export verified — Tesseract side-by-side not possible here**
(no Tesseract binary on the dev machine).
- Verified by calling `generate_highlighted_pdf(doc_dict, "uploads")` directly (what the
  `/highlighted` route does after `Document.query.get_or_404`) — not through the running app,
  because importing `app.py` starts the retention scheduler, which purges expired documents.

| Doc | Highlights p1 | All inside page | Ctrl+F hits landing on a highlight |
|---|---|---|---|
| `86185fb3….jpg` (MyKad image) | 22 | ✅ | `PENGENALAN`, `840403-12-5089`, `KINABALU` — 3/3 |
| `0503805e….pdf` (scanned invoice) | 209 | ✅ | `SIMEN` ×3, `Penang` ×2, `117752-D` — 6/6 |

- Rendered pages checked visually: boxes sit on the words. Minor false positives on the scan:
  the "SD" logo (conf 0.55) and a few specks along the top edge get highlighted.
- Re-export served from cache in 0.14 s; cache keys now start with the engine (`paddle:…`).
- Deviation from step 3: `_get_word_boxes_cached` no longer caches `[]` — an OCR failure also
  returns `[]`, and now that paddle loading is retried that would otherwise pin a page to
  "no highlights" until restart.
- Also changed in `ocr_service.py` (requested with this task): only `ImportError` marks paddle as
  permanently unavailable; any other load failure falls back to Tesseract and retries after
  `PADDLE_RETRY_SECONDS = 60`. The `[OCR] Engine:` line now logs on every engine change.
  Checked with a mocked loader + fake clock: fail → no retry at +30 s → reload at +61 s →
  singleton reused; ImportError stays on Tesseract permanently.
- **Still to do where Tesseract is installed:** `OCR_ENGINE=tesseract` export of the same two docs
  for the side-by-side comparison.

---

### Task 4: In-app highlight overlay → PaddleOCR instead of a Qwen bbox call

**Target File:** `qwen_service.py` (`build_image_blocks` ~2757, `compute_text_blocks_lazy` ~2800)

**Goal:** Replace (D). Removes one vision-model call per page and gives real coordinates.

**Step-by-step implementation:**
1. In `compute_text_blocks_lazy()`:
   - **Plain image branch:** open the original file with PIL (full resolution, not the
     1024-px `encode_image` copy) and `return ocr_line_blocks_normalized(img, page=1)`.
   - **Scanned PDF branch:** rasterize with `fitz` directly (`page.get_pixmap(dpi=200)` →
     PIL) instead of `pdf_pages_to_b64`, and call `ocr_line_blocks_normalized(img, page=n)`
     per page.
   - Digital-PDF/ODL branch: unchanged.
2. If PaddleOCR returns `[]` or raises, fall back to the existing `build_image_blocks()`
   Qwen path so the overlay never gets *worse*. Keep `build_image_blocks` — don't delete it.
3. Note that cached blocks live on the `Document` row (see `document_routes.get_text_blocks`).
   Old documents keep their old Qwen boxes until re-processed — that's fine; optionally add
   a one-off script to clear the cached blocks column so they recompute.
4. **Verify:** upload a new JPEG and a scanned PDF, open the highlight overlay in the UI,
   click a few extracted fields — the highlight should land on the right line. Check the
   logs show no `[ImageBlocks]` Qwen call for these. Time the first `/blocks` request
   before vs after.

**Result (2026-10-27): ✅ `compute_text_blocks_lazy()` verified directly — UI check pending (user)**
(not via `app.py`: importing it starts the retention scheduler, which purges documents).

| File | Blocks | Time | Qwen calls |
|---|---|---|---|
| `86185fb3….jpg` (MyKad) | 12 | 9.3 s (includes one-time model load; ~1 s warm) | 0 |
| `0c92734b….png` (invoice) | 10 | 1.2 s | 0 |
| `0503805e….pdf` (scanned, 1 page) | 65 | 5.0 s | 0 |
| `17a177f8….pdf` (scanned, 3 pages) | 9 (pages 1–3) | 1.8 s | 0 |

- No `[ImageBlocks]` log lines; block shape `{page, text, bbox{x1,y1,x2,y2}}` and 0–1 coords
  match `build_image_blocks()`. Boxes drawn on the images sit tightly on each line.
- Vision fallback (Qwen stubbed): OCR raising → 1 Vision call; OCR returning `[]` → 1 Vision call;
  3-page scan with page 2 failing → only page 2 goes to Vision (with `page=2`), pages 1 and 3 keep OCR.
  Fallback is per page; the 150-DPI Vision rasterization only runs if some page needs it.
- **No before/after timing:** LM Studio (`192.168.1.18:1234`) was unreachable from the dev machine.
- Digital-PDF/ODL branch unchanged; couldn't exercise it here (no Java → ODL `WinError 2`).
- `build_image_blocks()` kept as is. Images are not EXIF-transposed, matching the frame the old
  Vision path used. Also updated the `[Blocks] … computing via LM Studio (10s timeout)` log line in
  `document_routes.get_text_blocks` to `computing (OCR, Vision fallback)`.
- Step 3: old documents keep their cached Qwen boxes (`documents.text_blocks`) — no clearing script added.
- Known UI limit (pre-existing): the frontend aborts `/blocks` after 10 s. The server still finishes
  and caches, so a first open that is slow (model load, or a scan > ~2 pages at ~5 s/page) shows no
  highlights until the document is reopened.

---

### Task 5: Searchable PDF export → PaddleOCR word positions

**Target File:** `pdf_searchable_service.py`

**Goal:** Fix (C). Today `ocrmypdf` isn't installed, so scanned pages get evenly-spread
text and Ctrl+F lands in the wrong spot.

**Step-by-step implementation:**
1. Drop the `ocrmypdf` attempts in `_searchable_from_image` (~190) and
   `_searchable_from_pdf` (~238) — go straight to the manual-embed path (keep the
   existing functions; just stop trying `ocrmypdf` first).
2. `_searchable_from_image_fallback`: run `ocr_words(PIL image)`, scale pixel boxes to the
   page size (`w / img_w`, `h / img_h`), convert to `(x0, y0, x1, y1, text)` tuples and
   pass to the existing `_insert_words_at_positions()`. Only use `_insert_invisible_text`
   if OCR returns nothing.
3. `_searchable_from_pdf_fallback` (~270): digital pages already use
   `src_page.get_text("words")` — keep that. For pages where that's empty (scanned), the
   `pix` is already rendered at 2× — convert to PIL, `ocr_words`, scale by 0.5 to points,
   `_insert_words_at_positions`.
4. Searchable PDFs are cached in `searchable_pdf_cache/` keyed by content hash — clear
   that folder after this change (or add the engine name to `_searchable_cache_path`).
5. **Verify:** export a Searchable PDF for a scanned doc, open in a PDF viewer and in the
   app's PDF.js viewer, Ctrl+F three words from different parts of the page — each hit
   should highlight the right spot. Also export one digital PDF and confirm it's unchanged.

**Result (2026-10-27): ✅ Verified by calling `generate_searchable_pdf()` directly — PDF.js viewer check pending (user)**
(not via `app.py`; cache dir redirected to a scratch folder during tests).
- `ocrmypdf` attempts, `_get_image_dpi` and the `tempfile` import removed; `_searchable_from_image/_pdf`
  now go straight to the manual-embed builders, which take an `ocr_status` dict.
- Scanned page / image: OCR words placed via `_insert_words_at_positions(..., fit_width=True)` —
  **addition beyond the plan:** each OCR word is stretched/squeezed (`morph`) to exactly span its box.
  Without it, Helvetica's natural width covered only part of a word or ran into the next one,
  merging words (text layer had 141 words instead of 209; phrase search like `SIMEN UTARA` failed).
  Digital pages don't use `fit_width`.
- Scanned invoice `0503805e….pdf`: 209 words in the text layer; Ctrl+F `SEMENYIH` ×2, `JALAN BANGI`,
  `INVOICE`, `BK1141557`, `11,494.50`, `SIMEN UTARA` (footer) all cover exactly the right word.
  **Off:** the header line — big `SIMEN UTARA SDN. BHD.` + small `(Company No: …)` is one OCR line, and
  `ocr_words` splits a line's box by character count, so `Company No` lands on `BHD.` and `SIMEN` is
  partly covered. **Fix (follow-up, `ocr_service.ocr_words`):** use `predict(..., return_word_box=True)` —
  `text_word` / `text_word_boxes` give real word boxes (same speed, 4.3 s vs 4.4 s); tokens split
  punctuation (`". ("`), so they must be merged back into whitespace-delimited words.
- Image `86185fb3….jpg`: Ctrl+F `PENGENALAN`, `840403-12-5089`, `KINABALU` found.
- Digital PDFs (3 tried — identical files): text layer **identical to the old code's output** (verbatim
  copy of the pre-change function), 0 OCR calls.
- Cache: filename now `{doc_id}_{OCR_ENGINE}_{hash}.pdf` (configured engine, so the document-list
  `is_searchable_cached()` check never loads PaddleOCR). Re-export served from cache (0.01–0.03 s).
  Exports where OCR failed, or a different engine than configured answered, are returned but **not
  cached** (checked with a simulated OCR failure: evenly-spread text returned, `cached: False`).
  The one old cache file (`searchable_pdf_cache/13_5a13254c934d50f2df8d.pdf`, 4 KB) is now orphaned.
- Also done with this task: `templates/index.html` `/blocks` fetch timeout 10 s → 60 s, comment and
  timeout console message updated (blocks come from local OCR, not the vision model).

---

### Task 6: Tests + benchmark

**Target Files:** new `test_ocr_service.py`, new `benchmark_ocr.py`, `.github/workflows/tests.yml`

**Step-by-step implementation:**
1. `test_ocr_service.py` (pytest, **mocks** `PaddleOCR` so CI doesn't download models):
   - polygon → box conversion; `conf` filter; sort order
   - `ocr_words` splitting: boxes are contiguous, inside the line box, word count matches
   - `ocr_line_blocks_normalized` values are within 0–1
   - paddle import failure → falls back to tesseract path; any failure → `OCRError`
2. Add it to the `pytest` line in `tests.yml`. If CI install time explodes because of
   paddle, move paddle into a `requirements-ocr.txt` and keep CI on mocks.
3. `benchmark_ocr.py` (manual, like `test_barcode_benchmark.py`): run both engines on ~20
   files from `uploads/`, write `ocr_benchmark_results.json` with per-file time, line count,
   and mean confidence. Optional: if you have a few hand-checked texts, add a CER score.
4. **Verify:** `pytest test_core_logic.py test_rag_tenant_isolation.py test_ocr_service.py -v`
   passes; benchmark runs and PaddleOCR is at least as good as Tesseract on your samples.

**Task 5 follow-up (2026-10-27): ✅ real word boxes.** `ocr_service._paddle_lines` now calls
`predict(..., return_word_box=True)`; `_merge_word_tokens` turns each line's `text_word` /
`text_word_boxes` tokens into whitespace-delimited words (per character: whitespace ends a word,
punctuation like `". ("` attaches to its neighbours) and is only used when the merged words equal the
line's `text.split()` — otherwise that line falls back to the character-count split. Coverage: real word
boxes on 87/87 lines across the scan, MyKad and invoice samples (fallback never hit).
Header line `SIMEN UTARA SDN. BHD. (Company No: 117752-D)` re-checked in **both** exports: Ctrl+F
`SIMEN`, `UTARA`, `BHD`, `Company`, `117752-D` each cover their own word; phrases `SIMEN UTARA` and
`Company No` found. The highlighted export's invisible text was still natural-width Helvetica (phrase
`SIMEN UTARA` not found, `Company` hit spilling over), so `pdf_highlighted_service` got the same
width-fit as Task 5 (`_fit_width_morph`, both `insert_text` calls); after that both exports give
identical hits.

**Result (2026-10-27): ✅ Tests pass — Paddle-vs-Tesseract comparison not possible here** (no Tesseract binary).
- `test_ocr_service.py`: 15 pytest tests, PaddleOCR mocked with the real 3.7.0 result layout
  (incl. `text_word`/`text_word_boxes`), `pytesseract.image_to_data` faked — no models or binaries needed.
  Covers conf/empty filter, reading order (incl. same-row), downscale + BGR + boxes scaled back, clamping,
  word-box merging (real `". ("` tokenization), char-count fallback (no tokens / mismatched tokens),
  normalized blocks in 0–1, ImportError → permanent Tesseract, load error → retry after 60 s, forced
  Tesseract, every failure → `OCRError`, `extract_text` + alias. Mutation-checked: breaking the
  punctuation merge or the conf filter each fails a test.
- `tests.yml`: `test_ocr_service.py` added to the pytest line. Paddle stays in `requirements.txt`
  (CI doesn't need it for these mocked tests; split into `requirements-ocr.txt` only if CI install
  time becomes a problem).
- Verify command: **49 passed** (`test_core_logic.py` 32, `test_rag_tenant_isolation.py` 2, `test_ocr_service.py` 15).
- `benchmark_ocr.py` (manual): byte-identical uploads deduplicated; PDFs page 1 at 200 DPI; digital PDFs
  are ground truth (word precision/recall/F1 vs the page's own text layer — order-insensitive, since OCR
  reading order ≠ PDF text order). Unreadable files are skipped and listed. Writes `ocr_benchmark_results.json`.
- Run (paddle mobile, Windows CPU): **20 distinct files OK** (7 images, 7 scanned PDFs, 6 digital PDFs),
  2 corrupt uploads skipped (`a3677a83….jpg` broken data stream, `e3850906….png` not an image).
  Mean 3.7 s/file (0.8–7.4 s), mean conf 0.975. **Ground truth (6 digital PDFs): word precision 0.99,
  recall 0.98, F1 0.985** (per file 0.95–1.00). Caveat: rendered digital pages are clean, so this is a
  best case; scanned files have no ground truth (conf only).
- The Maybank receipt now takes 3.4 s / 53 lines vs 9.6 s / 102 lines in Task 1 — the 1600 px
  downscale (`PADDLE_OCR_MAX_SIDE`), with most dropped lines being faint watermark text.
- **Still to do where Tesseract is installed:** re-run `python benchmark_ocr.py` — it picks up Tesseract
  automatically and adds its columns for the side-by-side.

---

### Task 7 (optional): Feed PaddleOCR text into Qwen for scanned docs

**Target File:** `qwen_service.py` (scanned-PDF OCR loop ~3395–3500, image path ~1110)

Only do this if Task 6's benchmark shows PaddleOCR text is clean on your documents.
1. Behind a new env flag `OCR_PRETEXT_HINT=1` (default off): run `extract_text` on each
   page and prepend it to the vision prompt as *"Reference OCR text (may contain errors):
   ..."* — Qwen still sees the image, but gets the text as a crutch for small fonts.
2. Alternatively, an `OCR_TEXT_SOURCE=paddle` mode that uses PaddleOCR for `raw_text`
   entirely and only calls Qwen for classification + field extraction — fewer vision calls,
   much faster on multi-page scans, but loses Qwen's layout understanding in `raw_text`.
3. **Jawi note:** Jawi pages would need `lang="ar"` plus the RTL handling already in
   `_fix_odl_rtl_line_order`. Keep Qwen as the text source for Jawi docs.
4. **Verify:** A/B on 10 scanned docs — compare extracted fields and total processing time
   with the flag on vs off.

**Result (2026-10-30): ✅ Option A implemented + unit-tested — A/B pending (needs LM Studio)**. Option B not done.
- `qwen_service.py`: new `OCR_PRETEXT_HINT` (default **off**; `1/true/yes/on` enables), `OCR_PRETEXT_MAX_CHARS`
  (default 6000, cut on a line boundary). Helpers `_ocr_hint_for_image`, `_ocr_hint_for_path`, `_ocr_hints_for_pdf`,
  `_with_ocr_hint`. The hint is wrapped in `<<<OCR … OCR>>>` and placed before the prompt, with "the image is
  the authority" + "keep the output format" instructions.
- Where it's applied (every Qwen *transcription* call, never the tiny classify-only call):
  - scanned PDF, multi-page: page 1 `combined_prompt` and each page 2+ `ocr_only_prompt` — **each page gets its
    own hint**;
  - scanned PDF, single page + every image upload: `classify_and_extract_combined_vision(..., ocr_hint=)` — all
    three of its transcription prompts (text-only, table-type, combined OCR+fields).
  - `extract_text_from_image()` (~1110, named in the plan) is not called anywhere any more, so it was left alone.
- Scanned pages are rendered for OCR once at **200 DPI** (`OCR_PRETEXT_PDF_DPI`), not at the 150→72 DPI the
  Vision retry loop sends — hints are computed once, outside that loop.
- **Weak-OCR guard (covers Jawi):** a page gets no hint if its OCR text is < 20 chars or mean line conf < 0.80
  (the English model on Jawi/bad scans). OCR errors never fail processing — they just mean no hint. Logs:
  `[OCR-Hint] page 2: 41 lines, mean conf 0.97 (3.9s)` / `… skipped - …`.
- Cost when on: one extra local OCR pass per page (~1–5 s/page on CPU, from Task 6), run before the Vision calls;
  the prompt grows by up to ~6000 chars. Flag off = byte-identical prompts to before (tested).
- `test_ocr_hint.py` (new, 20 tests, in `tests.yml`): flag parsing, conf/length guard, OCR failure, truncation,
  per-page PDF hints (fake PyMuPDF), prompt wrapping, all 3 combined-call paths, and `process_document()` run up to
  the end of OCR for a 3-page scan (page 2 hint-less), a 1-page scan, an image, and flag off. Mutation-checked
  (wrong page's hint, missing image hint, removed conf guard, removed combined-prompt hint each fail a test).
  `test_ocr_hint.py` + `test_ocr_service.py`: 35 passed. `test_core_logic.py` not re-run (no sqlalchemy where tested).
- `docker-compose.yml`: `OCR_PRETEXT_HINT=0` on both services. Windows: `set OCR_PRETEXT_HINT=1` before `python app.py`.
- **A/B to do (needs LM Studio reachable):** run the app with `OCR_PRETEXT_HINT=0`, upload ~10 scanned docs (include a
  multi-page one, one with small print, one Jawi if you have it), note fields + total time from the `[Pipeline]` logs;
  restart with `OCR_PRETEXT_HINT=1`, upload the same files, compare. Keep it on only if fields are as good or better.

---

### Task 8: Remove Tesseract + update docs

**Target Files:** `requirements.txt`, `Dockerfile`, `docker-compose.yml`, `ocr_service.py`, `CLAUDE.md`, `.tasks/INDEX.md`

Do this only after Tasks 1–6 have run in normal use for a while.
1. Remove `pytesseract` from `requirements.txt` and `tesseract-ocr tesseract-ocr-eng`
   from the `Dockerfile`; drop the `TESSERACT_CMD` comment in `docker-compose.yml`.
2. Remove the tesseract fallback branch from `ocr_service.py` (keep the Qwen fallback in
   Task 4).
3. `grep -rn -i tesseract --include=*.py .` should return nothing.
4. Update `CLAUDE.md`: OCR section (engine, env vars `OCR_ENGINE`, `PADDLE_OCR_LANG`,
   model cache location), and mark this file done in `INDEX.md`.
5. **Verify:** clean `docker-compose build`, image boots, health check passes, and one
   upload + both PDF exports work end-to-end.
