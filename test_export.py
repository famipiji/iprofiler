"""
Test export functionality - settings and document export.
"""
import requests
import json
import os

BASE_URL = "http://127.0.0.1:5000/api/documents"

def test_export_settings():
    """Test getting and setting export folder path."""
    print("\n=== Testing Export Settings ===")

    # Get current settings
    print("\n1. Getting current export settings...")
    resp = requests.get(f"{BASE_URL}/export-settings")
    if resp.status_code == 200:
        data = resp.json()
        print(f"   [OK] Current export folder: {data.get('export_folder')}")
    else:
        print(f"   [FAIL] Status {resp.status_code}: {resp.text}")
        return False

    # Set new export path
    export_path = r"C:\Users\batri\Vaultmind\exports"
    print(f"\n2. Setting export folder to: {export_path}")
    resp = requests.post(
        f"{BASE_URL}/export-settings",
        json={"export_folder": export_path},
        headers={"Content-Type": "application/json"}
    )
    if resp.status_code == 200:
        data = resp.json()
        print(f"   [OK] Export folder set: {data.get('path')}")
    else:
        print(f"   [FAIL] Status {resp.status_code}: {resp.text}")
        return False

    # Verify it persisted
    print("\n3. Verifying settings persisted...")
    resp = requests.get(f"{BASE_URL}/export-settings")
    if resp.status_code == 200:
        data = resp.json()
        if data.get('export_folder') == export_path:
            print(f"   [OK] Settings persisted correctly")
        else:
            print(f"   [FAIL] Expected {export_path}, got {data.get('export_folder')}")
            return False
    else:
        print(f"   [FAIL] Status {resp.status_code}: {resp.text}")
        return False

    return True


def test_document_export():
    """Test exporting a document to folder and as ZIP."""
    print("\n=== Testing Document Export ===")

    # Get a processed document
    print("\n1. Finding a processed document...")
    resp = requests.get("http://127.0.0.1:5000/api/documents/")
    if resp.status_code != 200:
        print(f"   [FAIL] Could not fetch documents: {resp.status_code}")
        return False

    data = resp.json()
    docs = data.get('documents', [])

    if not docs:
        print("   [FAIL] No documents found in database")
        return False

    # Find first completed document with extracted fields
    test_doc = None
    for doc in docs:
        if doc.get('status') == 'completed' and doc.get('fields'):
            test_doc = doc
            break

    if not test_doc:
        # Just use the first document
        test_doc = docs[0]

    doc_id = test_doc['id']
    print(f"   [OK] Using document ID {doc_id}: {test_doc.get('filename')}")
    print(f"        Type: {test_doc.get('document_type')}")
    print(f"        Fields: {len(test_doc.get('fields', {}))}")

    # Test Export to X2
    print(f"\n2. Testing 'Export to X2' for document {doc_id}...")
    resp = requests.post(f"{BASE_URL}/{doc_id}/export-to-x2")
    if resp.status_code == 200:
        data = resp.json()
        print(f"   [OK] Export successful!")
        print(f"        PDF: {data.get('pdf_filename')}")
        print(f"        JSON: {data.get('json_filename')}")
        print(f"        Full paths:")
        print(f"          {data.get('pdf_path')}")
        print(f"          {data.get('json_path')}")

        # Verify files exist
        if os.path.exists(data.get('pdf_path')) and os.path.exists(data.get('json_path')):
            print(f"   [OK] Both files verified on disk")
        else:
            print(f"   [FAIL] Files not found on disk")
            return False
    else:
        print(f"   [FAIL] Status {resp.status_code}: {resp.text}")
        return False

    # Test Download as ZIP
    print(f"\n3. Testing 'Download' (ZIP) for document {doc_id}...")
    resp = requests.get(f"{BASE_URL}/{doc_id}/export-download")
    if resp.status_code == 200:
        zip_filename = resp.headers.get('Content-Disposition', '').split('filename=')[-1].strip('"')
        zip_size = len(resp.content)
        print(f"   [OK] ZIP download successful!")
        print(f"        Filename: {zip_filename}")
        print(f"        Size: {zip_size} bytes")

        # Save ZIP to verify contents
        test_zip_path = os.path.join(r"C:\Users\batri\Vaultmind", "test_download.zip")
        with open(test_zip_path, 'wb') as f:
            f.write(resp.content)
        print(f"        Saved to: {test_zip_path}")

        # Verify ZIP contains both files
        import zipfile
        with zipfile.ZipFile(test_zip_path, 'r') as zf:
            files = zf.namelist()
            print(f"        ZIP contents: {files}")
            if len(files) == 2:
                pdf_in_zip = any(f.endswith('.pdf') for f in files)
                json_in_zip = any(f.endswith('.json') for f in files)
                if pdf_in_zip and json_in_zip:
                    print(f"   [OK] ZIP contains both PDF and JSON")
                else:
                    print(f"   [FAIL] ZIP missing files (PDF: {pdf_in_zip}, JSON: {json_in_zip})")
                    return False
            else:
                print(f"   [FAIL] ZIP should contain 2 files, found {len(files)}")
                return False
    else:
        print(f"   [FAIL] Status {resp.status_code}: {resp.text}")
        return False

    return True


def show_exports_folder():
    """Show contents of exports folder."""
    print("\n=== Exports Folder Contents ===")
    exports_path = r"C:\Users\batri\Vaultmind\exports"

    if not os.path.exists(exports_path):
        print(f"[FAIL] Exports folder does not exist: {exports_path}")
        return

    files = os.listdir(exports_path)
    if not files:
        print("(empty)")
    else:
        print(f"\nFound {len(files)} files:")
        for f in sorted(files):
            full_path = os.path.join(exports_path, f)
            size = os.path.getsize(full_path)
            print(f"  - {f} ({size:,} bytes)")


if __name__ == "__main__":
    print("=" * 60)
    print("Export Functionality Test")
    print("=" * 60)

    # Test settings
    if not test_export_settings():
        print("\n[FAIL] Export settings test failed")
        exit(1)

    # Test document export
    if not test_document_export():
        print("\n[FAIL] Document export test failed")
        exit(1)

    # Show final state
    show_exports_folder()

    print("\n" + "=" * 60)
    print("[PASS] All export tests passed!")
    print("=" * 60)
