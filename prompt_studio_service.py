import difflib
import json
import os

from extensions import db
from document_model import Document, DocumentType, PromptTemplate, PromptVersion, PromptVersionValidation


def _normalize_doc_type_name(name: str) -> str:
    return (name or "").strip().lower().replace(" ", "_").replace("-", "_")


def set_prompt_overrides(overrides: dict | None):
    from qwen_service import set_prompt_overrides as _set
    _set(overrides)


def clear_prompt_overrides():
    from qwen_service import clear_prompt_overrides as _clear
    _clear()


def get_prompt_override_for_key(key: str):
    from qwen_service import _prompt_override_ctx
    overrides = getattr(_prompt_override_ctx, "overrides", {}) or {}
    return overrides.get(key)


def get_active_prompt_version(document_type_name: str) -> PromptVersion | None:
    # Match by normalized name so the classifier's snake_case form ("tax_invoice")
    # resolves to a type named with spaces ("Tax Invoice"). Without this the
    # active prompt is never found during extraction and tuning has no effect.
    def _norm(s):
        return (s or "").lower().replace("_", " ").replace("-", " ").strip()
    target = _norm(document_type_name)
    doc_type = next((t for t in DocumentType.query.all() if _norm(t.name) == target), None)
    if not doc_type or not doc_type.active_prompt_version_id:
        return None
    return PromptVersion.query.get(doc_type.active_prompt_version_id)


def _generate_default_structure_prompt(doc_type: DocumentType) -> str:
    if doc_type and doc_type.fields:
        lines = [
            f"You are a document data extractor. Extract ONLY the fields listed below from this {doc_type.name}.",
            "",
            "Fields to extract:",
        ]
        for field in doc_type.fields:
            label = field.field_label or field.field_name
            if field.field_type == "table":
                lines.append(f'- "{field.field_name}": array of objects — {label}')
            elif field.field_type == "list":
                lines.append(f'- "{field.field_name}": array of strings — {label}')
            else:
                lines.append(f'- "{field.field_name}": {label}')
        lines.extend([
            "",
            "Rules:",
            "- Output ONLY a single JSON object, no markdown, no explanation",
            "- Use EXACTLY these field names (keys)",
            "- Use null for any field not found in the document",
            "- Copy values exactly as they appear",
            "- For table fields, return an array of row objects",
        ])
        return "\n".join(lines)
    return "Extract ONLY the fields defined for this document type. Output valid JSON only."


def _current_base_prompt_text(doc_type: DocumentType) -> str:
    key = f"structure_hint:{doc_type.name}"
    from qwen_service import OCR_HINTS, DOCUMENT_STRUCTURE_HINTS
    prompt = PromptTemplate.query.filter_by(key=key).first()
    if prompt and prompt.content:
        return prompt.content
    default = DOCUMENT_STRUCTURE_HINTS.get(doc_type.name) or _generate_default_structure_prompt(doc_type)
    return default


def ensure_prompt_version_seed(document_type_name: str, created_by: str = "system") -> PromptVersion:
    doc_type = DocumentType.query.filter(db.func.lower(DocumentType.name) == document_type_name.lower()).first()
    if doc_type is None:
        raise ValueError(f"Unknown document type: {document_type_name}")

    active = PromptVersion.query.filter_by(document_type_id=doc_type.id, is_active=True).order_by(PromptVersion.created_at.desc()).first()
    if active:
        if doc_type.active_prompt_version_id != active.id:
            doc_type.active_prompt_version_id = active.id
            db.session.commit()
        return active

    latest = PromptVersion.query.filter_by(document_type_id=doc_type.id).order_by(PromptVersion.version_major.desc(), PromptVersion.version_minor.desc(), PromptVersion.created_at.desc()).first()
    if latest:
        latest.is_active = True
        doc_type.active_prompt_version_id = latest.id
        db.session.commit()
        return latest

    base_prompt = _current_base_prompt_text(doc_type)
    version = PromptVersion(
        document_type_id=doc_type.id,
        prompt_kind="structure_hint",
        version_major=1,
        version_minor=0,
        version_label="v1.0",
        prompt_text=base_prompt,
        parent_version_id=None,
        change_summary="Initial prompt seeded from document type schema",
        created_by=created_by,
        is_active=True,
    )
    db.session.add(version)
    db.session.flush()
    doc_type.active_prompt_version_id = version.id
    db.session.commit()
    return version


def list_prompt_versions(document_type_name: str) -> list[PromptVersion]:
    doc_type = DocumentType.query.filter(db.func.lower(DocumentType.name) == document_type_name.lower()).first()
    if doc_type is None:
        return []
    # Eager-load what to_dict() touches per row (document_type.name,
    # validations) in this one query, instead of a separate query per
    # PromptVersion when the caller iterates and calls .to_dict() on each.
    return (
        PromptVersion.query
        .filter_by(document_type_id=doc_type.id)
        .options(db.joinedload(PromptVersion.document_type), db.selectinload(PromptVersion.validations))
        .order_by(PromptVersion.version_major.desc(), PromptVersion.version_minor.desc(), PromptVersion.created_at.desc())
        .all()
    )


def _next_version_numbers(parent: PromptVersion | None, bump: str = "minor") -> tuple[int, int, str]:
    if parent is None:
        return 1, 0, "v1.0"
    if bump == "major":
        major = parent.version_major + 1
        minor = 0
    else:
        major = parent.version_major
        minor = parent.version_minor + 1
    return major, minor, f"v{major}.{minor}"


def build_prompt_merge_system_prompt(current_prompt_text: str, user_feedback: str, schema_context: dict, evidence_context: dict | None = None) -> str:
    schema_json = json.dumps(schema_context or {}, indent=2, ensure_ascii=False)
    evidence_json = json.dumps(evidence_context or {}, indent=2, ensure_ascii=False)
    return (
        "You are a senior prompt engineer for a document extraction system.\n"
        "The user describes extraction problems in plain, casual language. They are NOT prompt experts.\n"
        "YOUR JOB is to do the prompt engineering for them: turn their plain-language symptom into a PRECISE,\n"
        "deterministic extraction rule, and MERGE it into the current saved prompt.\n\n"
        "RULES FOR THE MERGE:\n"
        "1. Do not rewrite from scratch. Preserve every part of the current prompt that still works "
        "(field names, JSON shape, ordering, language constraints, working heuristics).\n"
        "2. Even if the user's feedback is short or vague (e.g. 'invoice date is wrong', 'focus on the date only'), "
        "infer their intent and write a SPECIFIC rule. Do NOT just restate their words — engineer the actual instruction.\n"
        "3. Use SCHEMA CONTEXT to look up each field's TYPE. When a rule touches a typed field, add an explicit format constraint:\n"
        "   - date fields: extract a SINGLE date token in the document's format (e.g. DD-MM-YYYY); the value must match a date pattern and contain nothing after it.\n"
        "   - number / currency fields: digits (and decimal/sign/currency symbol) only; no surrounding words.\n"
        "4. Add a VALUE-BOUNDARY rule whenever the symptom is 'field X is also capturing Y': each field's value must be read ONLY from its own label/line/cell. "
        "Never concatenate or merge values from an adjacent field, row, or column. If the user names the wrong value being captured, list it as an explicit exclusion.\n"
        "5. Make the new rule robust to messy OCR and column layouts, but keep the change focused on what the user reported.\n"
        "6. DEDUPLICATION (mandatory): Before returning the merged prompt, scan ALL existing rules. "
        "If the new rule overlaps with or restates an existing rule for the same field, CONSOLIDATE them into one precise rule — do NOT keep both. "
        "Remove any rule that is made redundant by the new addition. The final prompt must have zero duplicate or overlapping rules for any field.\n"
        "7. BREVITY: After deduplication, tighten any verbose rules. One clear sentence per field constraint is enough. "
        "A shorter, precise prompt outperforms a long, redundant one.\n\n"
        "Return valid JSON only with these keys:\n"
        '{"merged_prompt":"<full revised prompt text>","change_summary":"<short, plain-language summary of what you tightened>","notes":"<optional: the specific rule you added and any assumption>"}\n\n'
        f"CURRENT PROMPT TEXT:\n{current_prompt_text}\n\n"
        f"USER FEEDBACK (plain language — engineer it into a precise rule):\n{user_feedback}\n\n"
        f"SCHEMA CONTEXT (field names + types — use these to add format constraints):\n{schema_json}\n\n"
        f"VALIDATION / EVIDENCE CONTEXT:\n{evidence_json}\n"
    )


def _is_usable_prompt(text: str) -> bool:
    """A real prompt is instructions, not an empty extraction result. Reject text
    that is too short or that parses as a JSON object/array."""
    t = (text or "").strip()
    if len(t) < 40:
        return False
    if t[:1] in ("{", "["):
        try:
            json.loads(t)
            return False  # it's a JSON result, not a prompt
        except Exception:
            pass
    return True


def _clean_base_prompt(current_prompt_text: str, schema_context: dict) -> str:
    """Return a usable base prompt to merge onto. If the current prompt is a real
    prompt, use it. If it's corrupted (e.g. a previous bad merge that saved a JSON
    template), rebuild the clean default prompt from the type's schema instead."""
    if _is_usable_prompt(current_prompt_text):
        return current_prompt_text.rstrip()
    try:
        name = (schema_context or {}).get("document_type")
        if name:
            dt = DocumentType.query.filter(db.func.lower(DocumentType.name) == name.lower()).first()
            if dt:
                return _generate_default_structure_prompt(dt).rstrip()
    except Exception:
        pass
    return (current_prompt_text or "").rstrip()


def generate_merged_prompt(current_prompt_text: str, user_feedback: str, schema_context: dict, evidence_context: dict | None = None) -> dict:
    from qwen_service import call_qwen_messages, safe_parse_json

    system_prompt = build_prompt_merge_system_prompt(current_prompt_text, user_feedback, schema_context, evidence_context)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Return the merged prompt now."},
    ]
    result = safe_parse_json(call_qwen_messages(messages, max_tokens=1200, temperature=0.1))
    merged_prompt = (result.get("merged_prompt") or "").strip()
    change_summary = (result.get("change_summary") or "").strip()

    # Small local models often return an empty result object (e.g.
    # {"invoice_date": null}) instead of a real prompt. When the model's output
    # isn't a usable prompt, fall back to a DETERMINISTIC merge: append the
    # user's instruction as a new rule on a clean base. If the current prompt is
    # itself corrupted (a previous bad merge), rebuild the clean default prompt
    # from the type's schema so we never stack rules onto junk.
    if not _is_usable_prompt(merged_prompt):
        base = _clean_base_prompt(current_prompt_text, schema_context)
        feedback = (user_feedback or "").strip()
        merged_prompt = (base + "\n- " + feedback) if base else feedback
        change_summary = change_summary or "Added your rule to the prompt."

    return {
        "merged_prompt": merged_prompt,
        "change_summary": change_summary or "Merged prompt with user feedback",
        "notes": (result.get("notes") or "").strip() or None,
    }


def create_prompt_version(document_type_name: str, prompt_text: str, change_summary: str | None = None, created_by: str = "system", parent_version_id: int | None = None, bump: str = "minor") -> PromptVersion:
    doc_type = DocumentType.query.filter(db.func.lower(DocumentType.name) == document_type_name.lower()).first()
    if doc_type is None:
        raise ValueError(f"Unknown document type: {document_type_name}")

    parent = PromptVersion.query.get(parent_version_id) if parent_version_id else PromptVersion.query.filter_by(document_type_id=doc_type.id, is_active=True).order_by(PromptVersion.created_at.desc()).first()
    major, minor, label = _next_version_numbers(parent, bump=bump)

    version = PromptVersion(
        document_type_id=doc_type.id,
        prompt_kind="structure_hint",
        version_major=major,
        version_minor=minor,
        version_label=label,
        prompt_text=prompt_text,
        parent_version_id=parent.id if parent else None,
        change_summary=change_summary or "Updated prompt version",
        created_by=created_by,
        is_active=False,
    )
    db.session.add(version)
    db.session.commit()
    return version


def activate_prompt_version(version_id: int) -> PromptVersion:
    version = PromptVersion.query.get_or_404(version_id)
    doc_type = version.document_type
    if doc_type is None:
        raise ValueError("Prompt version is not linked to a document type")

    for other in PromptVersion.query.filter_by(document_type_id=doc_type.id).all():
        other.is_active = other.id == version.id
    doc_type.active_prompt_version_id = version.id
    db.session.commit()
    return version


def rollback_prompt_version(document_type_name: str, version_id: int) -> PromptVersion:
    version = PromptVersion.query.get_or_404(version_id)
    if not version.document_type or version.document_type.name.lower() != document_type_name.lower():
        raise ValueError("Version does not belong to the requested document type")
    return activate_prompt_version(version.id)


def diff_prompt_versions(left: PromptVersion, right: PromptVersion) -> str:
    left_lines = (left.prompt_text or "").splitlines()
    right_lines = (right.prompt_text or "").splitlines()
    return "\n".join(difflib.unified_diff(left_lines, right_lines, fromfile=left.version_label, tofile=right.version_label, lineterm=""))


def get_schema_context(document_type_name: str) -> dict:
    doc_type = DocumentType.query.filter(db.func.lower(DocumentType.name) == document_type_name.lower()).first()
    if doc_type is None:
        return {"document_type": document_type_name, "fields": []}
    return {
        "document_type": doc_type.name,
        "fields": [f.to_dict() for f in doc_type.fields],
        "active_prompt_version_id": doc_type.active_prompt_version_id,
    }


def test_prompt_version_against_sample(version_id: int, sample_document_id: int, created_by: str = "system") -> dict:
    from qwen_service import process_document
    version = PromptVersion.query.get_or_404(version_id)
    sample_doc = Document.query.get_or_404(sample_document_id)

    if sample_doc.document_class != "sample":
        raise ValueError("sample_document_id must reference a sample document")

    doc_type_name = version.document_type.name if version.document_type else sample_doc.document_type
    if not doc_type_name:
        raise ValueError("Unable to determine document type for test extraction")

    stored_filename = sample_doc.unique_upload_filename or sample_doc.storage_key
    if not stored_filename:
        raise ValueError(
            f"Sample document {sample_doc.id} has no stored file (unique_upload_filename/storage_key are both empty)"
        )
    base_dir = os.path.dirname(os.path.abspath(__file__))
    sample_path = os.path.join(base_dir, "uploads", stored_filename)
    if not os.path.isfile(sample_path):
        raise ValueError(f"Sample document {sample_doc.id}'s file was not found at {sample_path} (purged or moved?)")

    set_prompt_overrides({f"structure_hint:{doc_type_name}": version.prompt_text})
    try:
        result = process_document(sample_path)
    finally:
        clear_prompt_overrides()

    validation = PromptVersionValidation(
        prompt_version_id=version.id,
        sample_document_id=sample_doc.id,
        tenant_id=sample_doc.tenant_id,
        extracted_result_json=json.dumps(result, ensure_ascii=False),
        expected_result_json=None,
        success=True,
        notes="Test extraction completed",
        created_by=created_by,
    )
    db.session.add(validation)
    db.session.commit()
    return {"validation": validation.to_dict(), "result": result}
