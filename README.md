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
2. Search and download `qwen3-vl-2b-instruct` (or 7b if hardware allows)
3. Load the model
4. Go to **Local Server** tab → click **Start Server**
5. Server runs at `http://localhost:1234`

### 3. Run Flask
```bash
python app.py
```

---

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
