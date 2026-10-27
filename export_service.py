"""
Export service for processed documents.

Generates searchable PDF + JSON exports with proper naming conventions.
Supports both local file writes (Export to X2) and browser downloads (ZIP).
"""
import os
import re
import json
import zipfile
import io
from datetime import datetime
from typing import Optional, Tuple
from document_model import AppSettings
from extensions import db

# Default export folder (configurable via settings)
# Relative to app directory, created automatically on startup
DEFAULT_EXPORT_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exports")


def get_export_folder() -> str:
    """
    Get the currently configured export folder path.

    Returns the path from database settings, or the default if not configured.
    """
    setting = AppSettings.query.filter_by(key="export_folder_path").first()
    if setting and setting.value:
        return setting.value
    return DEFAULT_EXPORT_FOLDER


def set_export_folder(path: str) -> dict:
    """
    Set the export folder path after validation.

    Args:
        path: Absolute path to the export folder

    Returns:
        dict with success status and error message if validation fails
    """
    print("\n[set_export_folder] ENTER")
    print(f"  Input path (repr): {repr(path)}")
    print(f"  Input path (str): {path}")
    print(f"  Type: {type(path)}")

    # Validate path
    if not path or not path.strip():
        print("  VALIDATION FAILED: Empty path")
        return {"success": False, "error": "Export path cannot be empty"}

    path = path.strip()
    print(f"  After strip (repr): {repr(path)}")

    # Check if folder exists
    exists_check = os.path.exists(path)
    print(f"  os.path.exists(path) = {exists_check}")
    if not exists_check:
        print(f"  VALIDATION FAILED: Folder does not exist")
        return {"success": False, "error": f"Folder does not exist: {path}"}

    # Check if it's a directory
    isdir_check = os.path.isdir(path)
    print(f"  os.path.isdir(path) = {isdir_check}")
    if not isdir_check:
        print(f"  VALIDATION FAILED: Not a directory")
        return {"success": False, "error": f"Path is not a directory: {path}"}

    # Check if writable
    test_file = os.path.join(path, ".vaultmind_write_test")
    print(f"  Test file path: {test_file}")
    try:
        with open(test_file, 'w') as f:
            f.write("test")
        print(f"  Write test: SUCCESS")
        os.remove(test_file)
        print(f"  Remove test file: SUCCESS")
    except Exception as e:
        print(f"  VALIDATION FAILED: Not writable - {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "error": f"Folder is not writable: {str(e)}"}

    # Save to database
    print(f"  All validation passed, saving to database...")
    setting = AppSettings.query.filter_by(key="export_folder_path").first()
    if not setting:
        setting = AppSettings(key="export_folder_path")
        db.session.add(setting)
        print(f"  Created new AppSettings record")
    else:
        print(f"  Updating existing AppSettings record")

    setting.value = path
    setting.updated_at = datetime.utcnow()
    db.session.commit()
    print(f"  Database commit: SUCCESS")

    print(f"[set_export_folder] EXIT - Success\n")
    return {"success": True, "path": path}


def sanitize_filename_component(text: str) -> str:
    """
    Sanitize a text component for use in filenames.

    Converts to title case, removes special characters, replaces spaces/slashes
    with underscores.
    """
    if not text:
        return ""

    # Replace underscores and hyphens with spaces first for better title casing
    text = text.replace("_", " ").replace("-", " ")

    # Title case (each word capitalized)
    text = text.title()

    # Remove spaces
    text = text.replace(" ", "")

    # Remove any remaining unsafe characters (keep only alphanumeric)
    text = re.sub(r'[^A-Za-z0-9]', '', text)

    return text


def generate_export_filename(doc_dict: dict, doc_id: int) -> str:
    """
    Generate export filename base (without extension) following the pattern:
    {DocType}_{MRN}_{Date}

    Args:
        doc_dict: Document dictionary with extracted fields
        doc_id: Document ID (used as fallback for collision resolution)

    Returns:
        Base filename (without extension)
    """
    # Get document type
    doc_type = doc_dict.get("document_type", "Document")
    doc_type_clean = sanitize_filename_component(doc_type) or "Document"

    # Get MRN from extracted fields
    fields = doc_dict.get("fields", {})
    mrn = None

    # Try common MRN field names
    for mrn_field in ["mrn_barcode", "mrn", "patient_id", "medical_record_number"]:
        if mrn_field in fields and fields[mrn_field]:
            mrn = str(fields[mrn_field]).strip()
            break

    mrn_clean = sanitize_filename_component(mrn) if mrn else "NOMRN"

    # Get date (from created_at or current date)
    created_at = doc_dict.get("created_at")
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            date_str = dt.strftime("%Y-%m-%d")
        except:
            date_str = datetime.utcnow().strftime("%Y-%m-%d")
    else:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")

    # Combine components
    base_name = f"{doc_type_clean}_{mrn_clean}_{date_str}"

    return base_name


def get_unique_filename(base_path: str, base_name: str, extension: str, doc_id: int) -> str:
    """
    Get a unique filename by appending _doc{id} suffix if file already exists.

    Args:
        base_path: Directory path
        base_name: Base filename (without extension)
        extension: File extension (including dot, e.g., ".pdf")
        doc_id: Document ID to use in conflict suffix

    Returns:
        Full file path that doesn't conflict with existing files
    """
    # Try base name first
    file_path = os.path.join(base_path, f"{base_name}{extension}")
    if not os.path.exists(file_path):
        return file_path

    # Conflict - append doc ID
    unique_name = f"{base_name}_doc{doc_id}{extension}"
    return os.path.join(base_path, unique_name)


def generate_json_export(doc_dict: dict) -> str:
    """
    Generate JSON export containing extracted fields and metadata.

    Args:
        doc_dict: Document dictionary from Document.to_dict()

    Returns:
        JSON string (formatted, indented)
    """
    # Build export structure
    export_data = {
        "document_id": doc_dict.get("id"),
        "filename": doc_dict.get("filename"),
        "document_type": doc_dict.get("document_type"),
        "confidence": doc_dict.get("confidence"),
        "created_at": doc_dict.get("created_at"),
        "fields": doc_dict.get("fields", {}),
        "field_sources": doc_dict.get("field_sources", {}),
        "ambiguous_fields": doc_dict.get("ambiguous_fields", []),
        "raw_text": doc_dict.get("raw_text"),
    }

    return json.dumps(export_data, indent=2, ensure_ascii=False)


def export_to_folder(doc_dict: dict, pdf_bytes: bytes, export_folder: Optional[str] = None) -> dict:
    """
    Export document PDF + JSON to the configured export folder.

    Args:
        doc_dict: Document dictionary
        pdf_bytes: Searchable PDF content
        export_folder: Optional override for export folder path

    Returns:
        dict with success status, file paths, or error message
    """
    doc_id = doc_dict.get("id")
    if not doc_id:
        return {"success": False, "error": "Document ID missing"}

    # Get export folder
    if not export_folder:
        export_folder = get_export_folder()

    # Validate folder at export time (may have been deleted/unmounted)
    if not os.path.exists(export_folder):
        return {"success": False, "error": f"Export folder does not exist: {export_folder}"}

    if not os.path.isdir(export_folder):
        return {"success": False, "error": f"Export path is not a directory: {export_folder}"}

    if not os.access(export_folder, os.W_OK):
        return {"success": False, "error": f"Export folder is not writable: {export_folder}"}

    # Generate base filename
    base_name = generate_export_filename(doc_dict, doc_id)

    # Get unique filenames (handle conflicts)
    pdf_path = get_unique_filename(export_folder, base_name, ".pdf", doc_id)
    json_path = get_unique_filename(export_folder, base_name, ".json", doc_id)

    try:
        # Write PDF
        with open(pdf_path, 'wb') as f:
            f.write(pdf_bytes)

        # Generate and write JSON
        json_content = generate_json_export(doc_dict)
        with open(json_path, 'w', encoding='utf-8') as f:
            f.write(json_content)

        return {
            "success": True,
            "pdf_path": pdf_path,
            "json_path": json_path,
            "pdf_filename": os.path.basename(pdf_path),
            "json_filename": os.path.basename(json_path),
        }

    except Exception as e:
        # Clean up partial writes
        for path in [pdf_path, json_path]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except:
                    pass

        return {"success": False, "error": f"Failed to write files: {str(e)}"}


def create_download_zip(doc_dict: dict, pdf_bytes: bytes) -> Tuple[bytes, str]:
    """
    Create a ZIP file containing PDF + JSON for browser download.

    Args:
        doc_dict: Document dictionary
        pdf_bytes: Searchable PDF content

    Returns:
        Tuple of (zip_bytes, zip_filename)
    """
    doc_id = doc_dict.get("id", 0)

    # Generate base filename
    base_name = generate_export_filename(doc_dict, doc_id)

    # Create ZIP in memory
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        # Add PDF
        pdf_filename = f"{base_name}.pdf"
        zip_file.writestr(pdf_filename, pdf_bytes)

        # Add JSON
        json_content = generate_json_export(doc_dict)
        json_filename = f"{base_name}.json"
        zip_file.writestr(json_filename, json_content.encode('utf-8'))

    zip_bytes = zip_buffer.getvalue()
    zip_filename = f"{base_name}.zip"

    return zip_bytes, zip_filename
