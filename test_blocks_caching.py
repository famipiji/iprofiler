"""
Test script to demonstrate blocks caching behavior.

Shows that:
1. First /blocks request triggers LM Studio call and caches result
2. Second /blocks request returns cached data immediately (no LM Studio call)
"""

import time
import requests
from app import app, db
from document_model import Document
import os

def test_blocks_caching():
    """Test that blocks are cached after first computation."""

    with app.app_context():
        # Find any existing document with a file
        doc = Document.query.filter(Document.unique_upload_filename.isnot(None)).first()

        if not doc:
            print("No documents found in database. Please upload a document first.")
            return

        doc_id = doc.id
        print(f"Testing with document #{doc_id}: {doc.filename}")
        print(f"File: {doc.unique_upload_filename}")
        print(f"Current text_blocks cached: {doc.text_blocks is not None}")

        if doc.text_blocks is not None:
            print(f"\n[WARNING] Document already has cached blocks ({len(doc.get_text_blocks())} blocks)")
            print("To test from scratch, clear the cache first:")
            print(f"  doc = Document.query.get({doc_id})")
            print(f"  doc.text_blocks = None")
            print(f"  db.session.commit()")

            # Clear cache for clean test
            print("\nClearing cache for clean test...")
            doc.text_blocks = None
            db.session.commit()
            print("[OK] Cache cleared")

        print("\n" + "="*70)
        print("TEST 1: First /blocks request (should trigger LM Studio)")
        print("="*70)

        start_time = time.time()
        response1 = requests.get(f"http://localhost:5000/api/documents/{doc_id}/blocks")
        elapsed1 = time.time() - start_time

        print(f"Response status: {response1.status_code}")
        print(f"Response time: {elapsed1:.2f}s")

        if response1.status_code == 200:
            data1 = response1.json()
            blocks1 = data1.get("blocks", [])
            print(f"Blocks returned: {len(blocks1)}")
            print(f"\n[OK] First request complete - check server logs for LM Studio call")
        else:
            print(f"✗ Request failed: {response1.text}")
            return

        # Wait a moment
        time.sleep(1)

        print("\n" + "="*70)
        print("TEST 2: Second /blocks request (should use cache)")
        print("="*70)

        start_time = time.time()
        response2 = requests.get(f"http://localhost:5000/api/documents/{doc_id}/blocks")
        elapsed2 = time.time() - start_time

        print(f"Response status: {response2.status_code}")
        print(f"Response time: {elapsed2:.2f}s")

        if response2.status_code == 200:
            data2 = response2.json()
            blocks2 = data2.get("blocks", [])
            print(f"Blocks returned: {len(blocks2)}")
            print(f"\n✓ Second request complete - check server logs for 'cached blocks' message")
        else:
            print(f"✗ Request failed: {response2.text}")
            return

        print("\n" + "="*70)
        print("RESULTS")
        print("="*70)

        print(f"First request:  {elapsed1:.2f}s, {len(blocks1)} blocks")
        print(f"Second request: {elapsed2:.2f}s, {len(blocks2)} blocks")

        if blocks1 == blocks2:
            print(f"✓ Same blocks returned (caching works)")
        else:
            print(f"✗ Different blocks returned (caching may be broken)")

        if elapsed2 < elapsed1 * 0.5:
            print(f"✓ Second request much faster ({elapsed2:.2f}s vs {elapsed1:.2f}s) - likely using cache")
        else:
            print(f"⚠ Second request not significantly faster - check server logs")

        print("\nCheck server logs above for:")
        print("  1st request: '[Blocks] Doc X: text_blocks not cached, computing via LM Studio'")
        print("  2nd request: '[Blocks] Doc X: returning N cached blocks (no LM Studio call)'")

if __name__ == "__main__":
    test_blocks_caching()
