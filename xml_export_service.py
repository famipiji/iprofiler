import re
from xml.sax.saxutils import escape


def doctype_slug(name: str) -> str:
    """
    Mirrors the exact slugging templates/index.html's Doc Type Manager
    already uses for its live XML-tag preview (stUpdatePreview()): lowercase,
    whitespace -> "_", then strip everything that isn't a-z/0-9/"_" (so a
    hyphen is dropped entirely, not replaced -- "Out-patient Prescription"
    becomes "outpatient_prescription", matching what users already see in
    that preview).
    """
    slug = (name or "").strip().lower()
    slug = re.sub(r"\s+", "_", slug)
    slug = re.sub(r"[^a-z0-9_]", "", slug)
    return slug or "document"


def generate_export_xml(doc_dict: dict) -> bytes:
    """
    Serializes one document's extracted fields as XML:
      <{doctype_slug}>
        <field_name>value</field_name>
        ...
      </{doctype_slug}>

    Table-type fields (a list of row dicts) become repeated <field_name> row
    elements, each with its columns as children. Anything else is written as
    plain escaped text.
    """
    root_tag = doctype_slug(doc_dict.get("document_type", ""))
    fields = doc_dict.get("fields") or {}

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', f"<{root_tag}>"]
    for field_name, value in fields.items():
        tag = re.sub(r"[^a-zA-Z0-9_]", "_", field_name) or "field"
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            for row in value:
                lines.append(f"  <{tag}>")
                for col_name, col_value in row.items():
                    col_tag = re.sub(r"[^a-zA-Z0-9_]", "_", col_name) or "column"
                    lines.append(f"    <{col_tag}>{escape(str(col_value))}</{col_tag}>")
                lines.append(f"  </{tag}>")
        elif value not in (None, "", []):
            lines.append(f"  <{tag}>{escape(str(value))}</{tag}>")
        else:
            lines.append(f"  <{tag}/>")
    lines.append(f"</{root_tag}>")

    return ("\n".join(lines) + "\n").encode("utf-8")
