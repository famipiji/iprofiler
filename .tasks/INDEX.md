# iProfiler Backlog — Daily Task Index

Tracks daily task batches pulled from the 'iProfiler Backlog' Notion database and staged into `.tasks/` for Claude Code execution.

| Date | Task Count | Primary Modules Affected | File | Status |
|---|---|---|---|---|
| 2026-08-17 | 7 | `ocr_service.py`, `qwen_service.py`, `document_routes.py`, `app.py` | [2026-08-17-tasks.md](./2026-08-17-tasks.md) | ✅ All 7 Done |
| 2026-08-17 | 8 | `docker-compose.yml`, `templates/index.html`, `retention_service.py`, `qwen_service.py`, `pdf_export_service.py`, `prompt_studio_service.py` | [2026-08-17-remaining-high-priority.md](./2026-08-17-remaining-high-priority.md) | ✅ 8/8 Done (Task 5 closed via final-high-priority.md Task 4) |
| 2026-08-17 | 4 | `requirements.txt`, `Dockerfile`, `templates/index.html` | [2026-08-17-final-high-priority.md](./2026-08-17-final-high-priority.md) | ✅ All 4 Done — backlog has 0 open High-priority items |
| 2026-08-18 | 74 | `templates/index.html` (16), `qwen_service.py` (11), `document_routes.py` (11), `settings_routes.py` (7), + 17 smaller modules | [2026-08-18-medium-priority.md](./2026-08-18-medium-priority.md) | ✅ All 74 Done — Notion synced 2026-08-19 (Tasks 14, 37, 61 were fixed in code but not yet closed in Notion; verified and closed) |
| 2026-08-19 | 3 | `document_routes.py`, `retention_service.py`, `document_model.py` | [2026-08-19-tasks.md](./2026-08-19-tasks.md) | To Do |
| 2026-09-14 | 4 | `document_model.py`, `batch_service.py` (new), `document_routes.py`, `batch_worker.py` (new) | [2026-09-14-batch-processing-phase1.md](./2026-09-14-batch-processing-phase1.md) | ✅ Complete (2026-09-14, per the task file) — *not* from Notion backlog; Phase 1/5 of the batch-processing architecture scoped this session (see project doc `claude/2026-09-14-batch-processing-architecture.md`) |
| 2026-10-27 | 8 (Task 7 optional) | `ocr_service.py`, `pdf_highlighted_service.py`, `pdf_searchable_service.py`, `qwen_service.py`, `requirements.txt`, `Dockerfile` | [2026-10-27-paddleocr-migration.md](./2026-10-27-paddleocr-migration.md) | In Progress — Tasks 1–6/8 done (Docker verify, Tesseract runs/benchmark, Task 4/5 UI checks pending); Task 7 option A implemented 2026-10-30 behind `OCR_PRETEXT_HINT` (off), A/B pending; *not* from Notion backlog; Tesseract → PaddleOCR engine swap |
