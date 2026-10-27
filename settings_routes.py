import json
import hashlib
from flask import Blueprint, request, jsonify
from sqlalchemy.exc import IntegrityError
from extensions import db
from document_model import DocumentType, DocumentField, PromptTemplate, TenantRetentionPolicy, Document, PromptVersion, PromptVersionValidation
from prompt_studio_service import (
    ensure_prompt_version_seed,
    list_prompt_versions,
    get_schema_context,
    generate_merged_prompt,
    create_prompt_version,
    activate_prompt_version,
    rollback_prompt_version,
    diff_prompt_versions,
    test_prompt_version_against_sample,
)

settings_bp = Blueprint("settings", __name__)


def _require_json_dict():
    """Parse the request body as JSON and require it to be an object.
    Returns (data, None) on success, or (None, error_response) if the body
    isn't a JSON object (e.g. a bare list/string/number) - callers should
    `data, err = _require_json_dict(); if err: return err` before calling
    .get(...) on the result, which otherwise raises AttributeError on
    non-dict JSON and surfaces as an unhandled 500."""
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        return None, (jsonify({"error": "Request body must be a JSON object"}), 400)
    return data, None


def _find_duplicate_field_name(fields: list) -> str | None:
    """Returns the first field_name that appears more than once in `fields`
    (after the same trim the DocumentField constructor applies), or None.
    Enforces the (document_type_id, field_name) uniqueness the DB schema
    declares (see DocumentField.__table_args__) at the application level too,
    since a fresh install is the only case the DB constraint actually covers."""
    seen = set()
    for f in fields:
        fn = (f.get("field_name") or "").strip()
        if not fn:
            continue
        if fn in seen:
            return fn
        seen.add(fn)
    return None


def _encode_field_label(label: str, field_type: str, sub_columns) -> str:
    """
    For table-type fields, encode sub_columns into field_label as
    "Label\tJSON_ARRAY" so it persists without a DB schema migration.
    """
    label = (label or "").strip()
    if field_type == "table" and isinstance(sub_columns, list) and sub_columns:
        cols = [c for c in sub_columns if c and str(c).strip()]
        if cols:
            return label + "\t" + json.dumps(cols)
    return label


# ?????? Document Types ???????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

@settings_bp.route("/document-types", methods=["GET"])
def list_document_types():
    # selectinload(fields) turns "one fields query per DocumentType row" into
    # one extra query total; batch-fetching active-version labels below turns
    # "one PromptVersion.query.get() per row" into one more - so this whole
    # listing endpoint stays at a fixed, small query count regardless of how
    # many document types exist.
    types = (
        DocumentType.query
        .options(db.selectinload(DocumentType.fields))
        .order_by(DocumentType.created_at.desc())
        .all()
    )
    version_ids = [t.active_prompt_version_id for t in types if t.active_prompt_version_id]
    labels = {}
    if version_ids:
        labels = {
            v.id: v.version_label
            for v in PromptVersion.query.filter(PromptVersion.id.in_(version_ids)).all()
        }
    return jsonify({"document_types": [t.to_dict(prompt_version_labels=labels) for t in types]})


@settings_bp.route("/document-types", methods=["POST"])
def create_document_type():
    data, err = _require_json_dict()
    if err: return err
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    if DocumentType.query.filter_by(name=name).first():
        return jsonify({"error": f"Document type '{name}' already exists"}), 409

    dup = _find_duplicate_field_name(data.get("fields", []))
    if dup:
        return jsonify({"error": f"Duplicate field name '{dup}' — field names must be unique within a document type"}), 400

    dt = DocumentType(
        name=name,
        description=(data.get("description") or "").strip() or None,
        is_active=bool(data.get("is_active", True)),
    )
    db.session.add(dt)
    db.session.flush()

    for i, f in enumerate(data.get("fields", [])):
        field = DocumentField(
            document_type_id=dt.id,
            field_label=_encode_field_label(f.get("field_label", ""), f.get("field_type", "text"), f.get("sub_columns")),
            field_name=f.get("field_name", "").strip(),
            field_type=f.get("field_type", "text"),
            is_required=bool(f.get("is_required", False)),
            sort_order=i,
            aliases=(f.get("aliases") or "").strip() or None,
        )
        db.session.add(field)

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": f"Document type '{name}' already exists"}), 409
    return jsonify(dt.to_dict()), 201


@settings_bp.route("/document-types/<int:type_id>", methods=["GET"])
def get_document_type(type_id):
    dt = DocumentType.query.get_or_404(type_id)
    return jsonify(dt.to_dict())


@settings_bp.route("/document-types/<int:type_id>", methods=["PUT"])
def update_document_type(type_id):
    dt = DocumentType.query.get_or_404(type_id)
    data, err = _require_json_dict()
    if err: return err

    name = (data.get("name") or "").strip()
    if name and name != dt.name:
        if DocumentType.query.filter_by(name=name).first():
            return jsonify({"error": f"Document type '{name}' already exists"}), 409
        dt.name = name

    if "description" in data:
        dt.description = (data["description"] or "").strip() or None
    if "is_active" in data:
        dt.is_active = bool(data["is_active"])

    if "fields" in data:
        dup = _find_duplicate_field_name(data["fields"])
        if dup:
            return jsonify({"error": f"Duplicate field name '{dup}' — field names must be unique within a document type"}), 400
        DocumentField.query.filter_by(document_type_id=dt.id).delete()
        for i, f in enumerate(data["fields"]):
            field = DocumentField(
                document_type_id=dt.id,
                field_label=_encode_field_label(f.get("field_label", ""), f.get("field_type", "text"), f.get("sub_columns")),
                field_name=f.get("field_name", "").strip(),
                field_type=f.get("field_type", "text"),
                is_required=bool(f.get("is_required", False)),
                sort_order=i,
                aliases=(f.get("aliases") or "").strip() or None,
            )
            db.session.add(field)

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": f"Document type '{name}' already exists"}), 409
    return jsonify(dt.to_dict())


@settings_bp.route("/document-types/<int:type_id>", methods=["DELETE"])
def delete_document_type(type_id):
    dt = DocumentType.query.get_or_404(type_id)
    if dt.is_builtin:
        return jsonify({"error": "Built-in document types cannot be deleted"}), 409
    db.session.delete(dt)
    db.session.commit()
    return jsonify({"deleted": type_id})


@settings_bp.route("/document-types/<int:type_id>/toggle", methods=["POST"])
def toggle_document_type(type_id):
    dt = DocumentType.query.get_or_404(type_id)
    dt.is_active = not dt.is_active
    db.session.commit()
    return jsonify({"id": dt.id, "is_active": dt.is_active})


@settings_bp.route("/document-types/fields/<int:field_id>/aliases", methods=["PATCH"])
def patch_field_aliases(field_id):
    """Update the aliases for a single DocumentField (used by the ASK correction card)."""
    from document_model import DocumentField
    df = DocumentField.query.get_or_404(field_id)
    data = request.get_json() or {}
    aliases = (data.get("aliases") or "").strip() or None
    df.aliases = aliases
    db.session.commit()
    return jsonify({"ok": True, "field_id": field_id, "aliases": df.aliases or ""})


# —— Field Type Hints ——————————————————————————————————————————————————————
# Keyword -> type mappings taught via the ASK chat's "Teach a type hint" flow
# (document_routes.py, _infer_field_type_from_name). Listed here for review
# and removal — the chat flow only ever adds/updates one, it never deletes.

@settings_bp.route("/field-type-hints", methods=["GET"])
def list_field_type_hints():
    from document_model import FieldTypeHint
    hints = FieldTypeHint.query.order_by(FieldTypeHint.keyword.asc()).all()
    return jsonify({"hints": [h.to_dict() for h in hints]})


@settings_bp.route("/field-type-hints/<int:hint_id>", methods=["DELETE"])
def delete_field_type_hint(hint_id):
    from document_model import FieldTypeHint
    hint = FieldTypeHint.query.get_or_404(hint_id)
    db.session.delete(hint)
    db.session.commit()
    return jsonify({"ok": True})


# ?????? Tenant Retention Policy ???????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

@settings_bp.route("/retention-policy/<string:tenant_id>", methods=["GET"])
def get_retention_policy(tenant_id):
    policy = TenantRetentionPolicy.query.filter_by(tenant_id=tenant_id).first()
    if policy is None:
        policy = TenantRetentionPolicy(tenant_id=tenant_id, sample_ttl_days=7, production_ttl_days=30)
    return jsonify(policy.to_dict())


@settings_bp.route("/retention-policy/<string:tenant_id>", methods=["PUT"])
def update_retention_policy(tenant_id):
    data, err = _require_json_dict()
    if err: return err
    policy = TenantRetentionPolicy.query.filter_by(tenant_id=tenant_id).first()
    if policy is None:
        policy = TenantRetentionPolicy(tenant_id=tenant_id)
        db.session.add(policy)

    if "sample_ttl_days" in data:
        try:
            policy.sample_ttl_days = max(0, int(data["sample_ttl_days"]))
        except (TypeError, ValueError):
            return jsonify({"error": "sample_ttl_days must be a valid integer"}), 400
    if "production_ttl_days" in data:
        try:
            policy.production_ttl_days = max(0, int(data["production_ttl_days"]))
        except (TypeError, ValueError):
            return jsonify({"error": "production_ttl_days must be a valid integer"}), 400

    db.session.commit()
    return jsonify(policy.to_dict())


# ?????? Prompt Versions ???????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

@settings_bp.route("/prompt-versions/<string:doc_type>", methods=["GET"])
def list_prompt_versions_route(doc_type):
    try:
        ensure_prompt_version_seed(doc_type)
    except Exception:
        pass

    versions = list_prompt_versions(doc_type)
    doc = DocumentType.query.filter(db.func.lower(DocumentType.name) == doc_type.lower()).first()
    sample_docs = []
    if doc:
        sample_docs = Document.query.filter_by(document_type=doc.name, document_class="sample").order_by(Document.created_at.desc()).all()

    return jsonify({
        "document_type": doc.to_dict() if doc else None,
        "versions": [v.to_dict() for v in versions],
        "sample_documents": [d.to_dict() for d in sample_docs],
        "schema_context": get_schema_context(doc_type),
    })


@settings_bp.route("/prompt-versions/<string:doc_type>/merge", methods=["POST"])
def merge_prompt_version(doc_type):
    data, err = _require_json_dict()
    if err: return err
    user_feedback = (data.get("user_feedback") or "").strip()
    if not user_feedback:
        return jsonify({"error": "user_feedback is required"}), 400

    doc = DocumentType.query.filter(db.func.lower(DocumentType.name) == doc_type.lower()).first()
    if doc is None:
        return jsonify({"error": "document type not found"}), 404

    base_version = None
    if data.get("base_version_id"):
        base_version = PromptVersion.query.get(data["base_version_id"])
    if base_version is None:
        ensure_prompt_version_seed(doc_type)
        base_version = PromptVersion.query.get(doc.active_prompt_version_id)

    sample_document = None
    if data.get("sample_document_id"):
        sample_document = Document.query.get(data["sample_document_id"])

    evidence_context = {
        "sample_document_id": sample_document.id if sample_document else None,
        "sample_document_filename": sample_document.filename if sample_document else None,
    }

    merged = generate_merged_prompt(
        base_version.prompt_text if base_version else "",
        user_feedback,
        get_schema_context(doc_type),
        evidence_context=evidence_context,
    )

    return jsonify({
        "base_version": base_version.to_dict() if base_version else None,
        "proposed_prompt": merged["merged_prompt"],
        "change_summary": merged["change_summary"],
        "notes": merged.get("notes"),
    })


@settings_bp.route("/prompt-versions/<string:doc_type>", methods=["POST"])
def create_prompt_version_route(doc_type):
    data, err = _require_json_dict()
    if err: return err
    prompt_text = (data.get("prompt_text") or "").strip()
    if not prompt_text:
        return jsonify({"error": "prompt_text is required"}), 400

    version = create_prompt_version(
        doc_type,
        prompt_text,
        change_summary=(data.get("change_summary") or "").strip() or None,
        created_by=(data.get("created_by") or "system").strip(),
        parent_version_id=data.get("parent_version_id"),
        bump=(data.get("bump") or "minor").strip().lower(),
    )

    if bool(data.get("activate", False)):
        version = activate_prompt_version(version.id)

    return jsonify(version.to_dict()), 201


@settings_bp.route("/prompt-versions/<int:version_id>/rollback", methods=["POST"])
def rollback_prompt_version_route(version_id):
    version = PromptVersion.query.get_or_404(version_id)
    result = rollback_prompt_version(version.document_type.name, version.id)
    return jsonify(result.to_dict())


@settings_bp.route("/prompt-versions/<int:version_id>", methods=["DELETE"])
def delete_prompt_version_route(version_id):
    version = PromptVersion.query.get_or_404(version_id)
    if version.is_active:
        return jsonify({"error": "Cannot delete the active version. Rollback to another version first."}), 400
    doc_type = version.document_type
    PromptVersionValidation.query.filter_by(prompt_version_id=version_id).delete()
    db.session.delete(version)
    db.session.commit()
    versions = list_prompt_versions(doc_type.name) if doc_type else []
    return jsonify({"success": True, "versions": [v.to_dict() for v in versions]})


@settings_bp.route("/prompt-versions/diff", methods=["GET"])
def diff_prompt_versions_route():
    left_id = request.args.get("left_id", type=int)
    right_id = request.args.get("right_id", type=int)
    if not left_id or not right_id:
        return jsonify({"error": "left_id and right_id are required"}), 400

    left = PromptVersion.query.get_or_404(left_id)
    right = PromptVersion.query.get_or_404(right_id)
    return jsonify({
        "left": left.to_dict(),
        "right": right.to_dict(),
        "diff": diff_prompt_versions(left, right),
    })


@settings_bp.route("/prompt-versions/<int:version_id>/test-extraction", methods=["POST"])
def test_prompt_version_route(version_id):
    data = request.get_json(force=True) or {}
    sample_document_id = data.get("sample_document_id")
    if not sample_document_id:
        return jsonify({"error": "sample_document_id is required"}), 400

    try:
        result = test_prompt_version_against_sample(
            version_id,
            int(sample_document_id),
            created_by=(data.get("created_by") or "system").strip(),
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ?????? Prompt Manager ????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

@settings_bp.route("/prompts", methods=["GET"])
def list_prompts():
    prompts = PromptTemplate.query.order_by(PromptTemplate.category, PromptTemplate.label).all()
    return jsonify({"prompts": [p.to_dict() for p in prompts]})


@settings_bp.route("/prompts/<int:prompt_id>", methods=["PUT"])
def update_prompt(prompt_id):
    prompt = PromptTemplate.query.get_or_404(prompt_id)
    data, err = _require_json_dict()
    if err: return err

    content = data.get("content")
    if content is None or not isinstance(content, str) or not content.strip():
        return jsonify({"error": "content is required and must be a string"}), 400

    prompt.content = content
    db.session.commit()
    return jsonify(prompt.to_dict())


@settings_bp.route("/prompts/<int:prompt_id>/reset", methods=["POST"])
def reset_prompt(prompt_id):
    prompt = PromptTemplate.query.get_or_404(prompt_id)

    from qwen_service import OCR_HINTS, DOCUMENT_STRUCTURE_HINTS, DEFAULT_CLASSIFY_PROMPT, EXTRACT_PROMPTS
    if prompt.category == "ocr_hint":
        default = OCR_HINTS.get(prompt.doc_type)
    elif prompt.category == "structure_hint":
        default = DOCUMENT_STRUCTURE_HINTS.get(prompt.doc_type)
    elif prompt.category == "classify":
        default = DEFAULT_CLASSIFY_PROMPT
    elif prompt.category == "extract":
        entry = EXTRACT_PROMPTS.get(prompt.key)
        default = entry[1] if entry else None
    else:
        default = None

    if default is None:
        return jsonify({"error": "no built-in default available for this prompt"}), 400

    prompt.content = default
    db.session.commit()
    return jsonify(prompt.to_dict())


# ?????? iVault sync ???????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????

import re as _re

_CURRENCY_HINTS = ("amount", "total", "price", "balance", "tax", "subtotal", "cost", "fee")
_DATE_HINTS = ("date", "_dob", "expiry", "expiration")

# iVault classPropertyModels "type" integer -> our local field_type. Only
# entries confirmed against a real iVault instance belong here (0 = Text,
# 1 = Number - verified 2026-08-13); everything else falls back to the
# field-name hints below rather than asserting an unverified guess. The
# previous version of this map (borrowed from qwen_service._IVAULT_TYPE_HINTS,
# itself never validated against real values) had 1 mapped to "text", which
# is exactly backwards - don't add entries here without confirming them.
_IVAULT_PROPERTY_TYPE_MAP = {
    0: "text",
    1: "number",
}


def _snake_case(s: str) -> str:
    orig = str(s or "")
    out = _re.sub(r'[^A-Za-z0-9]+', '_', orig).strip('_')
    out = _re.sub(r'(?<!^)(?=[A-Z])', '_', out)
    # A punctuation-triggered underscore (e.g. "Num. Pages" -> "Num_Pages")
    # landing right next to a capital-letter-triggered one produces "num__pages"
    # instead of "num_pages" - collapse so field names stay predictable and
    # match whatever a plain "Num Pages" would normalize to.
    out = _re.sub(r'_+', '_', out).strip('_')
    out = out.lower()
    if not out:
        # A punctuation-only propertyName (e.g. "---", "***") strips down to
        # "" - without a fallback, two distinct properties both collapse to
        # the same empty field_name and the second is silently dropped as a
        # "duplicate" by the caller's seen_field_names check. Hash the
        # original (untouched) input so distinct punctuation-only names stay
        # distinct instead of colliding.
        out = "field_" + hashlib.md5(orig.encode("utf-8")).hexdigest()[:8]
    return out


def _field_type_for_property(field_name: str, ivault_type=None) -> str:
    """
    Local field_type for a synced iVault property. iVault's own declared
    property type is trusted for the values confirmed in
    _IVAULT_PROPERTY_TYPE_MAP (currently Text and Number); anything else
    (unconfirmed type code, or none supplied) falls back to the field-name
    hints, same as before this mapping existed. A confirmed Number whose name
    looks like an amount (total, price, tax, ...) still comes through as
    "currency" - iVault doesn't appear to have a distinct currency type.
    """
    n = field_name.lower()
    mapped = _IVAULT_PROPERTY_TYPE_MAP.get(ivault_type)
    if mapped == "number" and any(h in n for h in _CURRENCY_HINTS):
        return "currency"
    if mapped:
        return mapped
    if any(h in n for h in _DATE_HINTS):
        return "date"
    if any(h in n for h in _CURRENCY_HINTS):
        return "currency"
    return "text"


def _sync_ivault_document_type(record_type_id: str, class_name: str, tenant: str, username: str, password: str) -> DocumentType:
    """
    Sync an iVault record type (Class + classPropertyModels) into a
    DocumentType/DocumentField pair, storing iVault's classId/propertyId
    so extraction + push-back can target this type directly. Idempotent.

    Raises document_routes.IVaultAuthError on login failure, or RuntimeError
    on other iVault API errors. Must be called within an app context.
    """
    from document_routes import _ivault_session

    session, auth_header, api_base = _ivault_session(tenant, username, password)
    return _sync_ivault_document_type_with_session(record_type_id, class_name, session, auth_header, api_base, tenant)


def _sync_ivault_document_type_with_session(record_type_id: str, class_name: str, session, auth_header: str, api_base: str, tenant: str) -> DocumentType:
    """
    Same as _sync_ivault_document_type(), but reuses an already-authenticated
    iVault session ??? used by sync-ivault-all to sync many record types with
    a single login. Raises RuntimeError on iVault API errors.
    """
    import requests as _requests
    try:
        cp_resp = session.get(
            f"{api_base}/Class/{record_type_id}",
            headers={"Authorization": auth_header, "Tenant": tenant},
            timeout=15,
            verify=False,
        )
    except (_requests.exceptions.ConnectionError, _requests.exceptions.Timeout) as e:
        raise RuntimeError(f"Could not reach iVault: {e}")
    if not cp_resp.ok:
        raise RuntimeError(f"Failed to fetch iVault class definition (HTTP {cp_resp.status_code})")
    try:
        class_props = cp_resp.json().get("classPropertyModels", [])
    except ValueError as e:
        raise RuntimeError(f"iVault returned a non-JSON response: {e}")

    # Find-or-create the DocumentType for this iVault class
    dt = DocumentType.query.filter_by(external_source="ivault", external_id=record_type_id).first()
    if dt is None:
        type_name = _snake_case(class_name) or f"ivault_{record_type_id[:8]}"
        # Avoid clashing with an unrelated existing type of the same name
        if DocumentType.query.filter_by(name=type_name).first():
            type_name = f"ivault_{type_name}"
        dt = DocumentType(
            name=type_name,
            description=f"Synced from iVault: {class_name}",
            is_active=True,
            is_builtin=False,
            external_source="ivault",
            external_id=record_type_id,
        )
        db.session.add(dt)
        db.session.flush()

    # Sync fields: find-or-create by external_property_id, drop stale ones.
    # iVault's own class definition can list two properties with the same
    # display name under different propertyIds (e.g. a mistaken duplicate, or
    # an old property left alongside its replacement) - only the first one
    # encountered is kept locally, since extraction can only ever produce one
    # JSON key per field name anyway; a second row would just sit unused.
    # Skipping it here (not adding its id to seen_property_ids) also means the
    # stale-removal pass below deletes any duplicate row a prior sync already
    # created for it, self-healing existing duplicates on the next sync.
    seen_property_ids = set()
    seen_field_names = {}
    for i, prop in enumerate(class_props):
        prop_id = str(prop.get("propertyId", "")).strip()
        prop_name = prop.get("propertyName", "")
        if not prop_id or not prop_name:
            continue

        field_name = _snake_case(prop_name)
        prop_type = prop.get("type", 0)

        if field_name in seen_field_names:
            print(f"[iVaultSync] Skipping duplicate property '{prop_name}' (id={prop_id}) on "
                  f"'{class_name}' ??? field '{field_name}' already mapped to property "
                  f"{seen_field_names[field_name]}")
            continue
        seen_field_names[field_name] = prop_id
        seen_property_ids.add(prop_id)

        field = DocumentField.query.filter_by(document_type_id=dt.id, external_property_id=prop_id).first()
        if field is None:
            # An ad-hoc field can already exist under this exact name with no
            # iVault link yet - e.g. auto-registered via the Ask chat's "apply
            # to future docs" flow before this property was ever synced.
            # Adopt it (keeps its id/aliases) instead of creating a second row.
            field = DocumentField.query.filter_by(
                document_type_id=dt.id, field_name=field_name, external_property_id=None
            ).first()
        if field is None:
            field = DocumentField(
                document_type_id=dt.id,
                field_label=prop_name,
                field_name=field_name,
                field_type=_field_type_for_property(field_name, prop_type),
                is_required=False,
                sort_order=i,
                external_property_id=prop_id,
                external_property_type=prop_type,
            )
            db.session.add(field)
        else:
            field.field_label = prop_name
            field.field_name = field_name
            field.sort_order = i
            field.external_property_id = prop_id
            field.external_property_type = prop_type
            # Pick up a type change made on iVault's side since the last sync too,
            # not just at first-creation time.
            field.field_type = _field_type_for_property(field_name, prop_type)

    # Remove fields whose iVault property no longer exists
    for field in list(dt.fields):
        if field.external_property_id and field.external_property_id not in seen_property_ids:
            db.session.delete(field)

    # Self-heal duplicates from before the adoption logic above existed: an
    # orphan ad-hoc field (no iVault link) whose name now matches a properly
    # linked field is redundant - the linked one already covers it going forward.
    linked_names = {f.field_name for f in dt.fields if f.external_property_id}
    for field in list(dt.fields):
        if not field.external_property_id and field.field_name in linked_names:
            db.session.delete(field)

    db.session.commit()
    return dt


@settings_bp.route("/document-types/sync-ivault", methods=["POST"])
def sync_ivault_document_type():
    """
    Sync an iVault record type (Class + classPropertyModels) into a
    DocumentType/DocumentField pair, storing iVault's classId/propertyId
    so extraction + push-back can target this type directly.

    Body: {
      "record_type_id": "<iVault classId GUID>",
      "class_name":     "AP Invoice",
      "tenant":         "demo.ivaultplus.com",
      "username":       "sys.admin",
      "password":       "secret"
    }
    """
    from document_routes import IVaultAuthError

    data, err = _require_json_dict()
    if err: return err
    record_type_id = (data.get("record_type_id") or "").strip()
    class_name = (data.get("class_name") or "").strip()
    tenant = _re.sub(r'^https?://', '', (data.get("tenant") or "").strip()).rstrip('/')
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not record_type_id:
        return jsonify({"error": "record_type_id required"}), 400
    if not class_name:
        return jsonify({"error": "class_name required"}), 400
    if not tenant or not username or not password:
        return jsonify({"error": "iVault credentials (tenant, username, password) required"}), 400

    try:
        dt = _sync_ivault_document_type(record_type_id, class_name, tenant, username, password)
    except IVaultAuthError as e:
        return jsonify({"error": e.message}), e.status
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502

    return jsonify({"document_type": dt.to_dict()})


@settings_bp.route("/document-types/sync-ivault-all", methods=["POST"])
def sync_ivault_all_document_types():
    """
    Fetch the full list of record types (Classes) from iVault and sync each
    one into a DocumentType/DocumentField set (same as sync-ivault, but for
    every class in one call using a single login).

    Body: {
      "tenant":   "demo.ivaultplus.com",
      "username": "sys.admin",
      "password": "secret"
    }
    """
    from document_routes import IVaultAuthError, _ivault_session

    data, err = _require_json_dict()
    if err: return err
    tenant = _re.sub(r'^https?://', '', (data.get("tenant") or "").strip()).rstrip('/')
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not tenant or not username or not password:
        return jsonify({"error": "iVault credentials (tenant, username, password) required"}), 400

    try:
        session, auth_header, api_base = _ivault_session(tenant, username, password)
    except IVaultAuthError as e:
        return jsonify({"error": e.message}), e.status

    try:
        list_resp = session.get(
            f"{api_base}/Class/list",
            headers={"Authorization": auth_header, "Tenant": tenant},
            timeout=15,
            verify=False,
        )
        if not list_resp.ok:
            return jsonify({"error": f"Failed to fetch iVault class list (HTTP {list_resp.status_code})"}), 502
        raw = list_resp.json()
        items = raw if isinstance(raw, list) else raw.get("data", raw.get("classes", raw.get("items", [])))
    except Exception as e:
        return jsonify({"error": f"Failed to fetch iVault class list: {e}"}), 502

    synced = []
    errors = []
    for item in items:
        cid = str(item.get("classId") or item.get("ClassId") or item.get("id") or item.get("Id") or "").strip()
        cname = (item.get("className") or item.get("ClassName") or item.get("name") or item.get("Name") or "").strip()
        if not cid or not cname:
            continue
        try:
            dt = _sync_ivault_document_type_with_session(cid, cname, session, auth_header, api_base, tenant)
            synced.append(dt.to_dict())
        except Exception as e:
            errors.append({"class_name": cname, "error": str(e)})

    return jsonify({"document_types": synced, "errors": errors})

