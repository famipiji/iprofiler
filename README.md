# Document AI System
Flask + Qwen Vision (via LM Studio) — extract text, classify documents, and generate structured fields from images and PDFs.

---

## Project Structure

```
document_ai/
├── app.py                        # Flask entry point
├── extensions.py                 # SQLAlchemy instance
├── requirements.txt
├── uploads/                      # Uploaded files stored here (auto-created)
├── models/
│   └── document_model.py         # Document DB model
├── routes/
│   └── document_routes.py        # API endpoints
└── services/
    ├── qwen_service.py           # Qwen Vision OCR + classification + extraction
    └── pdf_service.py            # PDF → image conversion
```

---

## Setup Steps

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

For PDF support, also install poppler:
- **Windows**: Download from https://github.com/oschwartz10612/poppler-windows/releases → add `bin/` to PATH
- **Mac**: `brew install poppler`
- **Linux**: `sudo apt install poppler-utils`

### 2. LM Studio Setup
1. Open LM Studio
2. Search and download `qwen3-vl-8b-instruct`
3. Load the model. It must be a **non-thinking Instruct** vision model: thinking models (e.g. `qwen/qwen3.5-9b`) put their output in the reasoning channel and iProfiler gets empty answers (it now stops with a clear "model returned no answer" error)
4. Go to **Local Server** tab → click **Start Server**
5. Server runs at `http://localhost:1234`; `/v1/models` shows the exact model id to put in `OLLAMA_MODEL`

### 3. Run Flask
```bash
python app.py
```

---

## Running locally on Windows (no Docker)

The dev laptop runs `app.py` directly against the **X2 server (192.168.1.18)**, which hosts both
MySQL (`dms_iprofiler`, port 3306, user `iprofiler_user`) and LM Studio (port 1234).

**Prerequisites**
- **Python 3.12**, then `pip install -r requirements.txt`
- **poppler** on PATH: [poppler-windows releases](https://github.com/oschwartz10612/poppler-windows/releases) → add its `bin\` to PATH
- **Java** for `opendataloader-pdf` (digital PDF text + highlight boxes). Either unpack a portable
  JDK/JRE 17+ (e.g. Eclipse Temurin) into `jre\` in the project folder, so `jre\bin\java.exe` exists
  (`start_iprofiler.bat` puts it on PATH; `jre\` is git-ignored), or install a JRE 17+ system-wide so
  `java -version` works in a new terminal. Without Java the app prints one
  `[Startup] Java not found` warning and digital PDFs use the pdfplumber fallback (worse tables, no
  highlight boxes).
- **LM Studio on X2** serving `qwen3-vl-8b-instruct` with thinking off (see step 2)

**Start:** double-click `start_iprofiler.bat`. It sets, unless already set:

| Variable | Value | Purpose |
|---|---|---|
| `DB_HOST` | `192.168.1.18` | MySQL on X2 (`app.py` defaults to `host.docker.internal`) |
| `OLLAMA_BASE_URL` | `http://192.168.1.18:1234/v1` | LM Studio on X2 |
| `OLLAMA_MODEL` | `qwen3-vl-8b-instruct` | must match the id in LM Studio's `/v1/models` |
| `OCR_TEXT_SOURCE` | `paddle` | scanned PDFs/images: `paddle` = PaddleOCR reads the text, then the text model classifies + extracts (faster, falls back to Vision when OCR is weak); `vision` = Qwen Vision reads the page (Docker default) |
| `PATH` | `.\jre\bin` first, if present | portable Java |

Two more variables are read by `python app.py` and left **unset** by `start_iprofiler.bat` (a
commented `REM set ...` example for each is in the file):

| Variable | Default | Purpose |
|---|---|---|
| `FLASK_DEBUG` | off | `1` = Werkzeug debugger + auto-reload when a `.py` file is saved |
| `HOST` | `127.0.0.1` | `0.0.0.0` = reachable from other machines on the LAN at `http://<laptop-ip>:5000` |

Never set both `FLASK_DEBUG=1` and `HOST=0.0.0.0` on a network you don't trust: the debugger lets
anyone who can open the page run Python on the laptop. Docker is unaffected (it serves with waitress
on `0.0.0.0`, see `entrypoint.sh`).

The app is then at `http://localhost:5000`. All output is also written to `iprofiler.log`, which is
**UTF-16** (PowerShell `Tee-Object`): read it with `iconv -f utf-16 -t utf-8 iprofiler.log` or an
editor that detects the encoding.

---

## iVault push: date values

When fields are pushed to iVault, values of **Date** properties (iVault `propertyType` 3) are
re-formatted; the field value stored in iProfiler stays as printed on the document.

| Variable | Default | Purpose |
|---|---|---|
| `IVAULT_DATE_FORMAT` | `%Y-%m-%d` | strftime format sent to iVault (e.g. `%d/%m/%Y`) |
| `DATE_ORDER` | `DMY` | `DMY` or `MDY`: how to read an ambiguous date like `5/6/2026` |

A value that isn't a single date (e.g. a period "October 10 to November 9", or a date without a
year) is sent unchanged and logged as `[iVault] WARNING: date property value ... could not be parsed`.

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/documents/upload` | Upload image or PDF |
| GET | `/api/documents/` | Get all documents |
| GET | `/api/documents/<id>` | Get document by ID |
| GET | `/api/documents/type/<type>` | Filter by document type |
| DELETE | `/api/documents/<id>` | Delete document |

---

## Example Usage

### Upload a document
```bash
curl -X POST http://localhost:5000/api/documents/upload \
  -F "file=@invoice.jpg"
```

### Response
```json
{
  "success": true,
  "document": {
    "id": 1,
    "filename": "invoice.jpg",
    "file_type": "image",
    "document_type": "invoice",
    "confidence": "high",
    "raw_text": "INVOICE #1234\nDate: 2024-01-15\n...",
    "fields": {
      "invoice_number": "1234",
      "date": "2024-01-15",
      "vendor_name": "Acme Corp",
      "total_amount": "1500.00",
      "currency": "USD"
    },
    "created_at": "2024-01-15T10:30:00"
  }
}
```

---

## Switching to Qwen 7B
Edit `services/qwen_service.py` line 12:
```python
MODEL = "qwen3-vl-7b-instruct"  # was: qwen3-vl-2b-instruct
```
Nothing else needs to change.

---

## Supported Document Types
`invoice` · `receipt` · `id_card` · `passport` · `contract` · `medical_report` · `bank_statement` · `form` · `letter` · `other`
