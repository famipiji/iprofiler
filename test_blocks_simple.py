"""
Simple test: clear cache, make two requests, show timing.
"""
import time
from app import app, db
from document_model import Document

with app.app_context():
    # Find first document
    doc = Document.query.filter(Document.unique_upload_filename.isnot(None)).first()

    if not doc:
        print("No documents found")
        exit(1)

    print(f"Document #{doc.id}: {doc.filename}")

    # Clear cache
    if doc.text_blocks is not None:
        print(f"Clearing existing cache ({len(doc.get_text_blocks())} blocks)...")
        doc.text_blocks = None
        db.session.commit()
        print("Cache cleared.")
    else:
        print("No cache to clear.")

    print(f"\nTest document #{doc.id}")
    print("Now make two requests to /api/documents/{doc.id}/blocks")
    print("Check server logs for:")
    print("  1st: 'computing via LM Studio'")
    print("  2nd: 'returning N cached blocks (no LM Studio call)'")
