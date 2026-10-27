from extensions import db
from datetime import datetime
import json


class DocumentType(db.Model):
    __tablename__ = "document_types"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    description = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    is_builtin = db.Column(db.Boolean, default=False, nullable=False)
    # Set when this type was synced from an external system (e.g. "ivault"),
    # with external_id holding that system's identifier (iVault classId).
    external_source = db.Column(db.String(50), nullable=True)
    external_id = db.Column(db.String(100), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    active_prompt_version_id = db.Column(db.Integer, nullable=True, index=True)
    # JSON list of field names to permanently exclude from "new field detected" prompts
    excluded_fields = db.Column(db.Text, nullable=True)
    fields = db.relationship(
        "DocumentField",
        backref="document_type",
        lazy=True,
        cascade="all, delete-orphan",
        order_by="DocumentField.sort_order",
    )

    def get_excluded_fields(self) -> list:
        """Parse excluded_fields JSON back to list of field names."""
        try:
            return json.loads(self.excluded_fields) if self.excluded_fields else []
        except Exception:
            return []

    def set_excluded_fields(self, field_names: list):
        """Store excluded field names as JSON string."""
        self.excluded_fields = json.dumps(field_names) if field_names else None

    def add_excluded_field(self, field_name: str):
        """Add a field name to the exclude list (if not already present)."""
        excluded = self.get_excluded_fields()
        if field_name not in excluded:
            excluded.append(field_name)
            self.set_excluded_fields(excluded)

    def remove_excluded_field(self, field_name: str):
        """Remove a field name from the exclude list."""
        excluded = self.get_excluded_fields()
        if field_name in excluded:
            excluded.remove(field_name)
            self.set_excluded_fields(excluded)

    def to_dict(self, prompt_version_labels: dict | None = None) -> dict:
        """`prompt_version_labels`, if given, is a pre-fetched
        {version_id: label} map (see settings_routes.list_document_types) -
        lets a listing endpoint batch-resolve every row's active version
        label in one query instead of a separate PromptVersion.query.get()
        per row. Falls back to the single-row query when not supplied, so
        single-record callers (get_document_type) are unaffected."""
        active_label = None
        if self.active_prompt_version_id:
            if prompt_version_labels is not None:
                active_label = prompt_version_labels.get(self.active_prompt_version_id)
            else:
                apv = PromptVersion.query.get(self.active_prompt_version_id)
                active_label = apv.version_label if apv else None
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "is_active": self.is_active,
            "is_builtin": self.is_builtin,
            "external_source": self.external_source,
            "external_id": self.external_id,
            "active_prompt_version_id": self.active_prompt_version_id,
            "active_prompt_version_label": active_label,
            "created_at": self.created_at.isoformat() + "Z",
            "fields": [f.to_dict() for f in self.fields],
            "excluded_fields": self.get_excluded_fields(),
        }


class DocumentField(db.Model):
    __tablename__ = "document_fields"
    # Declared for fresh installs (db.create_all() only creates missing
    # tables, so this doesn't retroactively alter an already-deployed table -
    # existing duplicates, if any, still need a manual migration + cleanup).
    # The settings routes that accept a field list (create/update document
    # type) additionally reject duplicate field_names at the application
    # level, which is what actually protects the already-running system.
    __table_args__ = (db.UniqueConstraint("document_type_id", "field_name", name="uq_document_type_field_name"),)

    id = db.Column(db.Integer, primary_key=True)
    document_type_id = db.Column(db.Integer, db.ForeignKey("document_types.id"), nullable=False)
    field_label = db.Column(db.String(500), nullable=False)
    field_name = db.Column(db.String(500), nullable=False)
    field_type = db.Column(db.String(20), nullable=False, default="text")
    is_required = db.Column(db.Boolean, default=False, nullable=False)
    sort_order = db.Column(db.Integer, default=0, nullable=False)
    # NOTE: sub_columns are encoded inside field_label as "Label\tJSON_ARRAY"
    # for table-type fields — avoids needing a DB migration.
    # The sub_columns DB column is kept for compatibility but not relied on.
    sub_columns = db.Column(db.Text, nullable=True)
    # Set when this field was synced from an external system's property
    # (iVault propertyId / propertyType) — enables direct round-trip mapping.
    external_property_id = db.Column(db.String(100), nullable=True)
    external_property_type = db.Column(db.Integer, nullable=True)
    # Comma-separated label synonyms the deterministic extractor uses to locate
    # this field's value in a document (e.g. "Invoice Number, Number, Inv No").
    # User-editable in the Doc Type Manager — no code change needed per format.
    aliases = db.Column(db.Text, nullable=True)

    def to_dict(self) -> dict:
        import json as _json
        label = self.field_label or ""
        cols = None
        if self.field_type == "table":
            if "\t" in label:
                # Columns encoded inside field_label
                parts = label.split("\t", 1)
                label = parts[0]
                try:
                    cols = _json.loads(parts[1])
                except Exception:
                    cols = []
            elif self.sub_columns:
                # Fallback: legacy sub_columns DB column (if migration ran)
                try:
                    cols = _json.loads(self.sub_columns)
                except Exception:
                    cols = []
        return {
            "id": self.id,
            "document_type_id": self.document_type_id,
            "field_label": label,
            "field_name": self.field_name,
            "field_type": self.field_type,
            "is_required": self.is_required,
            "sort_order": self.sort_order,
            "sub_columns": cols,
            "aliases": self.aliases or "",
            "external_property_id": self.external_property_id,
            "external_property_type": self.external_property_type,
        }


class FieldTypeHint(db.Model):
    """
    User-taught keyword -> field type mapping. Used by _infer_field_type_from_name
    (document_routes.py) to guess a sensible type for a field that isn't
    registered in any Document Type's schema yet (e.g. an ad-hoc field the AI
    named on its own) instead of falling back to a hardcoded keyword list.
    Only ever consulted for such unregistered fields - a field with its own
    saved DocumentField.field_type is read directly and never affected.
    """
    __tablename__ = "field_type_hints"

    id = db.Column(db.Integer, primary_key=True)
    keyword = db.Column(db.String(50), nullable=False, unique=True)
    field_type = db.Column(db.String(20), nullable=False)  # date | currency | number | text
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "keyword": self.keyword,
            "field_type": self.field_type,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
        }


class ChatMessage(db.Model):
    """
    Persisted transcript of the Ask-tab conversation for a document, so
    reopening it later shows what was previously asked instead of starting
    blank every time. Deliberately just a flat (role, content) log, not a
    replay of the guided multi-turn flow's interactive state (buttons like
    "Yes, apply" reference in-flight JS state that's meaningless once the
    page has reloaded) - history is for looking back, not resuming.
    """
    __tablename__ = "chat_messages"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    role = db.Column(db.String(10), nullable=False)  # "user" | "bot"
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "document_id": self.document_id,
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
        }


class PromptTemplate(db.Model):
    __tablename__ = "prompt_templates"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), nullable=False, unique=True)
    category = db.Column(db.String(50), nullable=False)   # "ocr_hint" | "structure_hint" | "classify"
    doc_type = db.Column(db.String(100), nullable=True)   # e.g. "invoice" — null for non-doc-type prompts
    label = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "key": self.key,
            "category": self.category,
            "doc_type": self.doc_type,
            "label": self.label,
            "content": self.content,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
        }


class TenantRetentionPolicy(db.Model):
    __tablename__ = "tenant_retention_policies"

    tenant_id = db.Column(db.String(100), primary_key=True)
    sample_ttl_days = db.Column(db.Integer, nullable=False, default=7)
    production_ttl_days = db.Column(db.Integer, nullable=False, default=30)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "sample_ttl_days": self.sample_ttl_days,
            "production_ttl_days": self.production_ttl_days,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
        }


class Document(db.Model):
    __tablename__ = "documents"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.String(100), nullable=False, default="default", index=True)
    document_class = db.Column(db.String(20), nullable=False, default="production", index=True)
    filename = db.Column(db.String(255), nullable=False)
    unique_upload_filename = db.Column(db.String(255), nullable=True)  # UUID filename for upload
    storage_key = db.Column(db.String(255), nullable=True, index=True)
    file_type = db.Column(db.String(50))                  # image or pdf
    document_type = db.Column(db.String(100))             # invoice, receipt, etc.
    extractedText = db.Column("raw_text", db.Text(length=4294967295))
    raw_text = db.synonym("extractedText")
    fields = db.Column(db.Text)                           # JSON string of extracted fields
    field_sources = db.Column(db.Text)                    # JSON: per-field detection source (model/alias/date/not detected)
    # JSON: {field_name: [{"value":..., "source":...}, ...]} - set when a
    # field's primary label and one of its taught aliases resolved to two
    # different values on THIS document, so the user can pick the more
    # specific one instead of the extractor silently choosing for them.
    # Cleared as soon as the field is explicitly set (see patch_document_field).
    field_ambiguities = db.Column(db.Text, nullable=True)
    # Set once the user has answered the "apply these fields to future
    # documents of this type too?" prompt for THIS document (either way -
    # "just this document" is a real answer, not merely "not asked yet") -
    # see get_ad_hoc_fields() and the save_add_rule / dismiss_adhoc_fields
    # guided_context steps in document_routes.py.
    ad_hoc_fields_dismissed = db.Column(db.Boolean, default=False, nullable=False)
    confidence = db.Column(db.Float)
    text_blocks = db.Column(db.Text)                      # JSON list of ODL bounding boxes
    confirmed_at = db.Column(db.DateTime, nullable=True)
    pinned = db.Column(db.Boolean, default=False, nullable=False, index=True)
    pinned_at = db.Column(db.DateTime, nullable=True)
    ttl_days_override = db.Column(db.Integer, nullable=True)
    purged_at = db.Column(db.DateTime, nullable=True, index=True)
    purge_reason = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    chunks = db.relationship(
        "DocumentChunk",
        backref="document",
        lazy=True,
        cascade="all, delete-orphan",
    )
    chat_messages = db.relationship(
        "ChatMessage",
        backref="document",
        lazy=True,
        cascade="all, delete-orphan",
    )

    

    def set_fields(self, fields_dict: dict):
        """Store fields dict as JSON string."""
        self.fields = json.dumps(fields_dict)

    def get_fields(self) -> dict:
        """Parse fields JSON string back to dict."""
        try:
            return json.loads(self.fields) if self.fields else {}
        except Exception:
            return {}

    def get_field_sources(self) -> dict:
        """Parse per-field detection source JSON back to dict."""
        try:
            return json.loads(self.field_sources) if self.field_sources else {}
        except Exception:
            return {}

    def get_field_ambiguities(self) -> dict:
        """Parse pending multi-value-candidate JSON back to dict."""
        try:
            return json.loads(self.field_ambiguities) if self.field_ambiguities else {}
        except Exception:
            return {}

    def set_field_ambiguities(self, ambiguities: dict):
        self.field_ambiguities = json.dumps(ambiguities) if ambiguities else None

    def get_ad_hoc_fields(self) -> list:
        """Extracted field keys that aren't part of this document type's saved
        schema yet - e.g. everything on the first document ever processed
        under a brand-new type, since there's no DocumentField list to follow
        yet. Computed fresh each call (not cached) so it stays accurate if
        the schema changes later; empty once ad_hoc_fields_dismissed is set,
        even if some fields are still unregistered - "just this document" is
        a real answer, not just not-yet-asked.

        Also filters out any fields in the document type's excluded_fields list -
        fields the user has chosen to permanently suppress from "new field detected"
        prompts for this document type."""
        if self.ad_hoc_fields_dismissed or not self.document_type:
            return []
        dt = DocumentType.query.filter_by(name=self.document_type).first()
        if not dt:
            return []
        registered = {f.field_name for f in dt.fields}
        excluded = set(dt.get_excluded_fields())
        try:
            fields_dict = self.get_fields()
        except Exception:
            return []
        return [k for k in fields_dict.keys() if k not in registered and k not in excluded]

    def set_text_blocks(self, blocks: list):
        """Store ODL text blocks (with bboxes) as JSON string."""
        self.text_blocks = json.dumps(blocks)

    def get_text_blocks(self) -> list:
        """Parse text_blocks JSON string back to list."""
        try:
            return json.loads(self.text_blocks) if self.text_blocks else []
        except Exception:
            return []

    def to_dict(self, include_raw_text: bool = True) -> dict:
        """`include_raw_text=False` omits extractedText/raw_text (the largest
        field, often many KB of OCR output) - used by list-view endpoints
        that serialize many rows at once, where the full text isn't needed
        and previously made those responses balloon in size for no reason."""
        d = {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "document_class": self.document_class,
            "filename": self.filename,
            "file_type": self.file_type,
            "document_type": self.document_type,
            "fields": self.get_fields(),
            "field_sources": self.get_field_sources(),
            "field_ambiguities": self.get_field_ambiguities(),
            "confidence": self.confidence,
            "created_at": self.created_at.isoformat() + 'Z',
            "unique_upload_filename": self.unique_upload_filename,
            "storage_key": self.storage_key,
            "confirmed_at": self.confirmed_at.isoformat() + "Z" if self.confirmed_at else None,
            "pinned": self.pinned,
            "pinned_at": self.pinned_at.isoformat() + "Z" if self.pinned_at else None,
            "ttl_days_override": self.ttl_days_override,
            "purged_at": self.purged_at.isoformat() + "Z" if self.purged_at else None,
            "purge_reason": self.purge_reason,
        }
        if include_raw_text:
            d["extractedText"] = self.extractedText
            d["raw_text"] = self.extractedText
        return d


class DocumentChunk(db.Model):
    __tablename__ = "document_chunks"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id = db.Column(db.String(100), nullable=False, index=True)
    chunk_index = db.Column(db.Integer, nullable=False)
    chunk_text = db.Column(db.Text, nullable=False)
    vector_id = db.Column(db.String(255), nullable=True, unique=True, index=True)
    chunk_hash = db.Column(db.String(64), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "document_id": self.document_id,
            "tenant_id": self.tenant_id,
            "chunk_index": self.chunk_index,
            "chunk_text": self.chunk_text,
            "vector_id": self.vector_id,
            "chunk_hash": self.chunk_hash,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
        }


class DocumentDeletionAuditLog(db.Model):
    __tablename__ = "document_deletion_audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.String(100), nullable=False, index=True)
    document_id = db.Column(db.Integer, nullable=True, index=True)
    reason = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    actor = db.Column(db.String(100), nullable=True)
    details = db.Column(db.Text, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "document_id": self.document_id,
            "reason": self.reason,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "actor": self.actor,
            "details": self.details,
        }


class PromptVersion(db.Model):
    __tablename__ = "prompt_versions"

    id = db.Column(db.Integer, primary_key=True)
    document_type_id = db.Column(db.Integer, db.ForeignKey("document_types.id", ondelete="CASCADE"), nullable=False, index=True)
    prompt_kind = db.Column(db.String(50), nullable=False, default="structure_hint")
    version_major = db.Column(db.Integer, nullable=False, default=1)
    version_minor = db.Column(db.Integer, nullable=False, default=0)
    version_label = db.Column(db.String(20), nullable=False, index=True)
    prompt_text = db.Column(db.Text, nullable=False)
    parent_version_id = db.Column(db.Integer, db.ForeignKey("prompt_versions.id", ondelete="SET NULL"), nullable=True, index=True)
    change_summary = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    created_by = db.Column(db.String(100), nullable=True)
    is_active = db.Column(db.Boolean, default=False, nullable=False, index=True)

    document_type = db.relationship("DocumentType", backref=db.backref("prompt_versions", lazy=True, cascade="all, delete-orphan"))
    parent_version = db.relationship("PromptVersion", remote_side=[id], uselist=False)
    validations = db.relationship(
        "PromptVersionValidation",
        backref="prompt_version",
        lazy=True,
        cascade="all, delete-orphan",
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "document_type_id": self.document_type_id,
            "prompt_kind": self.prompt_kind,
            "version_major": self.version_major,
            "version_minor": self.version_minor,
            "version_label": self.version_label,
            "prompt_text": self.prompt_text,
            "parent_version_id": self.parent_version_id,
            "change_summary": self.change_summary,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "created_by": self.created_by,
            "is_active": self.is_active,
            "document_type_name": self.document_type.name if self.document_type else None,
            "validations": [v.to_dict() for v in self.validations],
        }


class PromptVersionValidation(db.Model):
    __tablename__ = "prompt_version_validations"

    id = db.Column(db.Integer, primary_key=True)
    prompt_version_id = db.Column(db.Integer, db.ForeignKey("prompt_versions.id", ondelete="CASCADE"), nullable=False, index=True)
    sample_document_id = db.Column(db.Integer, db.ForeignKey("documents.id", ondelete="SET NULL"), nullable=True, index=True)
    tenant_id = db.Column(db.String(100), nullable=False, index=True)
    extracted_result_json = db.Column(db.Text, nullable=False)
    expected_result_json = db.Column(db.Text, nullable=True)
    success = db.Column(db.Boolean, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    created_by = db.Column(db.String(100), nullable=True)

    sample_document = db.relationship("Document", foreign_keys=[sample_document_id])

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "prompt_version_id": self.prompt_version_id,
            "sample_document_id": self.sample_document_id,
            "tenant_id": self.tenant_id,
            "extracted_result_json": self.extracted_result_json,
            "expected_result_json": self.expected_result_json,
            "success": self.success,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "created_by": self.created_by,
            "sample_document_filename": self.sample_document.filename if self.sample_document else None,
        }


class Batch(db.Model):
    __tablename__ = "batches"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.String(100), nullable=False, default="default")
    source = db.Column(db.String(50), nullable=False)  # manual_upload | watched_folder | api
    status = db.Column(db.String(50), nullable=False, default="draft", index=True)  # draft | open | processing | completed | completed_with_errors
    total_jobs = db.Column(db.Integer, nullable=False, default=0)
    completed_count = db.Column(db.Integer, nullable=False, default=0)
    failed_count = db.Column(db.Integer, nullable=False, default=0)
    split_pending_count = db.Column(db.Integer, nullable=False, default=0)
    notify_email = db.Column(db.String(255), nullable=True)
    label = db.Column(db.String(200), nullable=True)
    display_code = db.Column(db.String(20), nullable=True, unique=True)  # Short random code for display
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)

    source_files = db.relationship(
        "SourceFile",
        backref="batch",
        lazy=True,
        cascade="all, delete-orphan",
    )
    jobs = db.relationship(
        "DocumentJob",
        backref="batch",
        lazy=True,
        cascade="all, delete-orphan",
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "client_id": self.client_id,
            "source": self.source,
            "status": self.status,
            "total_jobs": self.total_jobs,
            "completed_count": self.completed_count,
            "failed_count": self.failed_count,
            "split_pending_count": self.split_pending_count,
            "notify_email": self.notify_email,
            "label": self.label,
            "display_code": self.display_code,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "completed_at": self.completed_at.isoformat() + "Z" if self.completed_at else None,
        }


class SourceFile(db.Model):
    __tablename__ = "source_files"

    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("batches.id", ondelete="CASCADE"), nullable=False, index=True)
    file_path = db.Column(db.String(500), nullable=False)
    page_count = db.Column(db.Integer, nullable=True)
    split_mode = db.Column(db.String(50), nullable=False, default="none")  # none | mrn_barcode | ...
    status = db.Column(db.String(50), nullable=False, default="queued")  # queued | splitting | split_done | failed

    jobs = db.relationship(
        "DocumentJob",
        backref="source_file",
        lazy=True,
        cascade="all, delete-orphan",
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "file_path": self.file_path,
            "page_count": self.page_count,
            "split_mode": self.split_mode,
            "status": self.status,
        }


class DocumentJob(db.Model):
    __tablename__ = "document_jobs"
    __table_args__ = (
        db.Index("ix_document_jobs_claim_query", "status", "priority", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("batches.id", ondelete="CASCADE"), nullable=False, index=True)
    source_file_id = db.Column(db.Integer, db.ForeignKey("source_files.id", ondelete="CASCADE"), nullable=False, index=True)
    page_range = db.Column(db.String(100), nullable=True)  # e.g. "1-5" or None for whole file
    file_path = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(50), nullable=False, default="queued")  # held | queued | claimed | processing | succeeded | failed | dead_letter | cancelled
    attempts = db.Column(db.Integer, nullable=False, default=0)
    max_attempts = db.Column(db.Integer, nullable=False, default=3)
    last_error = db.Column(db.Text, nullable=True)
    claimed_by = db.Column(db.String(100), nullable=True)
    claimed_at = db.Column(db.DateTime, nullable=True)
    priority = db.Column(db.Integer, nullable=False, default=0)
    result_ref = db.Column(db.Integer, nullable=True)  # FK to documents.id after successful processing
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self) -> dict:
        # Fetch page_count from related SourceFile if available
        # Guard against source_file being None to prevent AttributeError
        page_count = self.source_file.page_count if self.source_file else None

        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "source_file_id": self.source_file_id,
            "page_range": self.page_range,
            "file_path": self.file_path,
            "page_count": page_count,
            "status": self.status,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "last_error": self.last_error,
            "claimed_by": self.claimed_by,
            "claimed_at": self.claimed_at.isoformat() + "Z" if self.claimed_at else None,
            "priority": self.priority,
            "result_ref": self.result_ref,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "started_at": self.started_at.isoformat() + "Z" if self.started_at else None,
            "finished_at": self.finished_at.isoformat() + "Z" if self.finished_at else None,
        }


class AppSettings(db.Model):
    """
    Application-wide settings (key-value store).

    Used for configuration that needs to persist across server restarts
    and be modifiable from the UI without code changes.
    """
    __tablename__ = "app_settings"

    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "value": self.value,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
        }
