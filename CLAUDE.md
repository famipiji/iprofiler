# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

**iProfiler** (repo name "Vaultmind", Docker image `dcsadmin01/vaultmind`) is a Flask app that extracts structured data from uploaded documents (images/PDFs) using a locally-hosted vision-language model (Qwen3-VL via LM Studio or Ollama, OpenAI-compatible API). It also syncs document-type schemas with an external system called **iVault** and pushes extracted data back to it.

## Commands

Run the app locally:
```bash
python app.py
```
Flask listens on port 5000. `FLASK_APP=app.py`, `FLASK_ENV=production` per the Dockerfile, but the code calls `app.run` directly for local dev.

Run via Docker:
```bash
docker-compose up
```
This expects an LM Studio/Ollama server reachable at `host.docker.internal:1234` and a MySQL instance at `host.docker.internal:3306` (see Environment/config below).

Install dependencies:
```bash
pip install -r requirements.txt
```
PDF support additionally requires poppler on PATH (Windows: poppler-windows release; Mac: `brew install poppler`; Linux: `apt install poppler-utils`). Docker image installs `libmupdf-dev`, `mupdf-tools`, and a headless JRE (needed by `opendataloader-pdf`).

Tests are plain scripts, not a pytest suite — run individually:
```bash
python test_connection.py            # verifies MySQL connectivity + table row counts
python test_qwen.py                  # sanity-checks the LM Studio /v1 endpoint is reachable
python test_rag_tenant_isolation.py  # unittest-based; mocks chromadb/sentence_transformers
```

There is no lint/build step configured (no `pyproject.toml`, `.eslintrc`, or CI config in the repo).

## Architecture

**Flask app (`app.py`)** registers two blueprints and owns DB config/seeding:
- `document_bp` → `/api/documents/*` (`document_routes.py`)
- `settings_bp` → `/api/settings/*` (`settings_routes.py`)

On startup it seeds `DocumentType`/`DocumentField` rows from `qwen_service.FIELD_MAP` for any built-in types not already in the DB (`seed_builtin_document_types`), so the DB is the source of truth for field schemas at runtime — `FIELD_MAP` is only the bootstrap default.

**Database**: SQLAlchemy against MySQL (`dms_iprofiler` DB, user `iprofiler_user`) by default, configurable via `SQLALCHEMY_DATABASE_URI` env var. A stray empty `vaultmind.db`/`instance/documents.db` exist from an earlier SQLite setup but are not the active store.

**Document processing pipeline** (`document_routes.py`, the largest file in the repo):
1. `POST /api/documents/upload` accepts one or more files, spins up a background thread per file (`process_file_task`) and streams progress over SSE (`GET /api/documents/progress/<job_id>`).
2. PDFs are rasterized to page images via `pdf_service.pdf_to_images` (PyMuPDF, 300 DPI default) before being sent to the vision model.
3. `qwen_service.py` (the largest file — very large, treat as a single monolith) drives the LM Studio/Ollama client: classifies the document type, then extracts fields. Known types use the fixed field list in `FIELD_MAP`; per-type free-text structure hints live in `DOCUMENT_STRUCTURE_HINTS` (e.g. resumes need nested arrays for work experience/education, bank statements need a transaction array capped at 10 rows).
4. Extraction prompts are versionable at runtime: `PromptTemplate`/`PromptVersion` rows (editable via `/api/settings/prompts*` and `/api/settings/prompt-versions/*`) let a user override the built-in prompts per document type without a code change — `prompt_studio_service.py` and `qwen_service.set_prompt_overrides` wire runtime overrides into the extraction call.
5. Results are saved via `_save_document_to_db`, including field-level ambiguity detection (`_detect_field_ambiguities`) and a `field_sources` map recording whether each value came from the model, an alias match, a date heuristic, or was not detected.
6. `document_routes.py` also implements a conversational "ask this document" endpoint (`/ask`, and `/chat` history) with a large amount of custom logic for matching free-text questions to fields, inferring new fields from answers, and applying user-defined extraction rules (`_apply_rule`, `_generate_guided_rule`) — this is bespoke NLU, not a call-through to the LLM for everything.

**iVault integration**: `settings_routes.py` has `/document-types/sync-ivault` and `sync-ivault-all`, which log into an external iVault tenant and import its record-type/property schema as local `DocumentType`/`DocumentField` rows (storing `external_property_id`/`external_property_type` for round-tripping). `IVaultAuthError` (raised from `document_routes.py`) is the shared auth-failure exception across both route modules.

**RAG** (`rag_service.py`): a separate retrieval layer over document text using ChromaDB (persisted to `./chroma_db`) and `sentence-transformers` (`all-MiniLM-L6-v2`), chunked into the `DocumentChunk` model. This is independent of the field-extraction pipeline — it's for semantic search/QA over document contents, not structured extraction. Loaded lazily (`get_embedder`, `get_collection`) since the embedding model is expensive to load.

**Retention** (`retention_service.py`): background scheduler that purges documents past their tenant's TTL. `document_class` on a `Document` (`"sample"` vs `"production"`) selects which of the two TTLs (`sample_ttl_days` / `production_ttl_days` on `TenantRetentionPolicy`) applies; a per-document `ttl_days_override` can bypass the policy.

**Export services**: `docx_service.py` (Word doc read/generate via python-docx + reportlab), `pdf_export_service.py`, `pdf_highlighted_service.py` (overlays extraction bounding boxes), `pdf_searchable_service.py` (adds an OCR text layer to make scanned PDFs searchable) — these are separate one-purpose modules, not a shared export interface.

**Frontend**: a single server-rendered page, `templates/index.html`, plus `static/` assets (bundled `pdf.min.js`/`pdf.worker.min.js` for in-browser PDF rendering). There is no separate SPA build step — `package.json` only pins `pdfjs-dist`, and `node_modules` is vendored for that one dependency.

## Configuration / environment

- `OLLAMA_BASE_URL` — OpenAI-compatible endpoint for the vision model (LM Studio default: `http://192.168.1.18:1234/v1`; Docker Compose points at `http://host.docker.internal:1234/v1`).
- `OLLAMA_MODEL` — e.g. `qwen/qwen3-vl-8b` (Compose) vs `qwen3-vl-2b-instruct`/`qwen3-vl-7b-instruct` referenced elsewhere — model name must match what's loaded in LM Studio/Ollama.
- `LLM_API_KEY` — dummy value is fine for a local LM Studio server (`lm-studio`).
- `SQLALCHEMY_DATABASE_URI` — overrides the hardcoded MySQL DSN in `app.py`.
- iVault credentials (`tenant`, `username`, `password`) are passed per-request in the sync endpoints, not stored as env vars.

Switching model size is a one-line change in `qwen_service.py` (the `MODEL` constant) per the README; nothing else needs to change.

## Supported document types

`invoice`, `receipt`, `id_card`, `passport`, `contract`, `bond_purchase_agreement`, `medical_report`, `bank_statement`, `resume`, `form`, `letter`, `other` — defined in `qwen_service.FIELD_MAP`, but customizable per-tenant at runtime through `/api/settings/document-types`.
