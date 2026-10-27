from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
from flask_migrate import Migrate
from extensions import db
from document_routes import document_bp
from settings_routes import settings_bp
from urllib.parse import quote_plus
import requests
import os
import json

IVAULT_BASE     = "http://192.168.1.18"
IVAULT_API_BASE = "https://ivaultapi2.ivaultplus.com/api"
DB_USER = 'iprofiler_user'
DB_PASS = quote_plus('P@ssw0rd')   # safely encodes @ and other special chars
DB_HOST = 'host.docker.internal'
DB_PORT = '3306'
DB_NAME = 'dms_iprofiler'

import logging
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
CORS(app)

@app.before_request
def log_request_headers():
    app.logger.debug(
        "\n── Incoming Request ──────────────────────────────\n"
        "%s %s\n"
        "Headers:\n%s"
        "─────────────────────────────────────────────────",
        request.method,
        request.path,
        "".join(f"  {k}: {v}\n" for k, v in request.headers),
    )

@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "File too large. Maximum upload size is 100MB."}), 413

# Config
# Without this, Jinja2's template cache never rechecks the file's mtime once
# Flask isn't in debug mode (true here — see FLASK_ENV=production in the
# Dockerfile) - templates/index.html is bind-mounted specifically so edits
# show up without a rebuild (see docker-compose.yml's comment on that
# mount), but that only actually works if the running process re-reads the
# file; otherwise a long-lived container keeps serving whatever it first
# cached, silently ignoring every subsequent edit until it's restarted.
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB — enforces the 413 handler above
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'SQLALCHEMY_DATABASE_URI',
    f'mysql+pymysql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}'
)

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_recycle': 280,      # recycle connections before MySQL's wait_timeout (300s default)
    'pool_pre_ping': True,    # test connection before using it
    'pool_size': 5,
    'max_overflow': 10,
}

# Init extensions
db.init_app(app)
migrate = Migrate(app, db)

# Register blueprints
app.register_blueprint(document_bp, url_prefix="/api/documents")
app.register_blueprint(settings_bp, url_prefix="/api/settings")

# ── Field type hints for seeding ──────────────────────────────────────────────
_DATE_FIELDS     = {"date", "due_date", "issue_date", "expiry_date", "effective_date",
                    "maturity_date", "date_of_birth", "statement_period"}
_CURRENCY_FIELDS = {"total_amount", "subtotal", "tax", "purchase_price", "principal_amount",
                    "purchase_price", "opening_balance", "closing_balance", "total_interest_paid"}

def _field_type_for(name: str) -> str:
    n = name.strip()
    if n in _DATE_FIELDS:
        return "date"
    if n in _CURRENCY_FIELDS:
        return "currency"
    return "text"

def _label_for(name: str) -> str:
    return name.strip().replace("_", " ").title()


# FIELD_MAP entries for these types are a free-text description of the extraction
# GOAL ("all key information as field-value pairs"), not real comma-separated field
# names — splitting them on "," produces one garbled field instead of a real schema.
# They're the unclassified/catch-all types, so they get zero seeded fields on
# purpose: no defined schema means no auto-extraction (see extract_fields /
# extract_fields_vision in qwen_service.py), and the user defines fields themselves.
_ZERO_TEMPLATE_TYPES = {"other", "form"}


def seed_builtin_document_types():
    """Populate DocumentType table with all built-in types from FIELD_MAP."""
    from qwen_service import FIELD_MAP
    from document_model import DocumentType, DocumentField

    for type_name, fields_csv in FIELD_MAP.items():
        dt = DocumentType.query.filter_by(name=type_name).first()
        if dt:
            # Already exists — don't overwrite user edits, but self-heal the one
            # known bug artifact: a single garbled field auto-seeded from the
            # free-text FIELD_MAP description before _ZERO_TEMPLATE_TYPES existed.
            if type_name in _ZERO_TEMPLATE_TYPES and dt.is_builtin and len(dt.fields) == 1:
                bad_fn = fields_csv.strip().replace(" ", "_").lower()
                if dt.fields[0].field_name == bad_fn:
                    print(f"[Seed] Removing garbled auto-seeded field on '{type_name}': {bad_fn}")
                    db.session.delete(dt.fields[0])
            continue

        dt = DocumentType(
            name=type_name,
            description=f"Built-in {_label_for(type_name)} type",
            is_active=True,
            is_builtin=True,
        )
        db.session.add(dt)
        db.session.flush()

        if type_name in _ZERO_TEMPLATE_TYPES:
            continue  # zero template — no fields seeded, user defines them

        for i, raw_field in enumerate(fields_csv.split(",")):
            fn = raw_field.strip().replace(" ", "_").lower()
            if not fn:
                continue
            db.session.add(DocumentField(
                document_type_id=dt.id,
                field_label=_label_for(fn),
                field_name=fn,
                field_type=_field_type_for(fn),
                is_required=False,
                sort_order=i,
            ))

    db.session.commit()
    print("[Seed] Built-in document types seeded into DocumentType table")


def seed_prompt_templates():
    """Populate PromptTemplate table with editable copies of the built-in OCR/structure/classify prompts."""
    from qwen_service import OCR_HINTS, DOCUMENT_STRUCTURE_HINTS, DEFAULT_CLASSIFY_PROMPT, EXTRACT_PROMPTS
    from document_model import PromptTemplate

    def _add(key, category, doc_type, label, content):
        if PromptTemplate.query.filter_by(key=key).first():
            return  # already exists — don't overwrite user edits
        db.session.add(PromptTemplate(key=key, category=category, doc_type=doc_type, label=label, content=content))

    for type_name, hint in OCR_HINTS.items():
        _add(f"ocr_hint:{type_name}", "ocr_hint", type_name, f"OCR Focus — {_label_for(type_name)}", hint)

    for type_name, hint in DOCUMENT_STRUCTURE_HINTS.items():
        _add(f"structure_hint:{type_name}", "structure_hint", type_name, f"Extraction Structure — {_label_for(type_name)}", hint)

    _add("classify_prompt", "classify", None, "Document Classification Prompt", DEFAULT_CLASSIFY_PROMPT)

    for key, (label, content) in EXTRACT_PROMPTS.items():
        _add(key, "extract", None, label, content)

    db.session.commit()
    print("[Seed] Prompt templates seeded into PromptTemplate table")


def seed_prompt_versions():
    """Seed an initial prompt version for each document type."""
    from document_model import DocumentType
    from prompt_studio_service import ensure_prompt_version_seed

    types = DocumentType.query.order_by(DocumentType.name).all()
    for doc_type in types:
        try:
            ensure_prompt_version_seed(doc_type.name)
        except Exception as e:
            print(f"[Seed] Prompt version seed skipped for {doc_type.name}: {e}")

    print("[Seed] Prompt versions seeded for document types")


def _column_exists(conn, table: str, column: str) -> bool:
    result = conn.execute(db.text(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = :table AND column_name = :column"
    ), {"table": table, "column": column})
    return result.scalar() > 0


def _index_exists(conn, table: str, index_name: str) -> bool:
    result = conn.execute(db.text(
        "SELECT COUNT(*) FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = :table AND index_name = :index_name"
    ), {"table": table, "index_name": index_name})
    return result.scalar() > 0


def _add_column_if_missing(conn, table: str, column: str, ddl_suffix: str) -> None:
    """Explicit existence check instead of try/ALTER/except-pass - the old
    pattern relied on the exception meaning 'column already exists', but that
    same except also silently swallowed real failures (lost connection,
    insufficient privileges), letting the app start with a silently
    incomplete schema. Any exception here now propagates and is logged."""
    if _column_exists(conn, table, column):
        return
    conn.execute(db.text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_suffix}"))
    conn.commit()
    print(f"[Migration] Added {table}.{column}")


def _add_index_if_missing(conn, table: str, index_name: str, columns_sql: str) -> None:
    if _index_exists(conn, table, index_name):
        return
    conn.execute(db.text(f"CREATE INDEX {index_name} ON {table} ({columns_sql})"))
    conn.commit()
    print(f"[Migration] Added index {index_name} on {table}")


# Create tables + migrate existing schema
with app.app_context():
    db.create_all()
    try:
        with db.engine.connect() as conn:
            _add_column_if_missing(conn, "documents", "tenant_id", "VARCHAR(100) NOT NULL DEFAULT 'default'")
            _add_column_if_missing(conn, "documents", "document_class", "VARCHAR(20) NOT NULL DEFAULT 'production'")
            _add_column_if_missing(conn, "documents", "storage_key", "VARCHAR(255)")
            _add_column_if_missing(conn, "documents", "confirmed_at", "DATETIME")
            _add_column_if_missing(conn, "documents", "pinned", "BOOLEAN NOT NULL DEFAULT 0")
            _add_column_if_missing(conn, "documents", "pinned_at", "DATETIME")
            _add_column_if_missing(conn, "documents", "ttl_days_override", "INT")
            _add_column_if_missing(conn, "documents", "purged_at", "DATETIME")
            _add_column_if_missing(conn, "documents", "purge_reason", "TEXT")
            _add_index_if_missing(conn, "documents", "ix_documents_tenant_id", "tenant_id")
            _add_index_if_missing(conn, "documents", "ix_documents_document_class", "document_class")
            _add_index_if_missing(conn, "documents", "ix_documents_pinned", "pinned")
            _add_index_if_missing(conn, "documents", "ix_documents_purged_at", "purged_at")
    except Exception as e:
        print(f"[Migration] ERROR adding retention columns to documents: {e}")
    try:
        with db.engine.connect() as conn:
            _add_column_if_missing(conn, "documents", "text_blocks", "TEXT")
    except Exception as e:
        print(f"[Migration] ERROR adding text_blocks column: {e}")
    try:
        with db.engine.connect() as conn:
            _add_column_if_missing(conn, "documents", "field_sources", "TEXT")
    except Exception as e:
        print(f"[Migration] ERROR adding field_sources column to documents: {e}")
    try:
        with db.engine.connect() as conn:
            _add_column_if_missing(conn, "document_types", "is_builtin", "BOOLEAN NOT NULL DEFAULT 0")
    except Exception as e:
        print(f"[Migration] ERROR adding is_builtin column: {e}")
    try:
        with db.engine.connect() as conn:
            _add_column_if_missing(conn, "document_fields", "sub_columns", "TEXT")
    except Exception as e:
        print(f"[Migration] ERROR adding sub_columns column: {e}")
    try:
        with db.engine.connect() as conn:
            _add_column_if_missing(conn, "document_fields", "aliases", "TEXT")
    except Exception as e:
        print(f"[Migration] ERROR adding aliases column to document_fields: {e}")
    try:
        with db.engine.connect() as conn:
            _add_column_if_missing(conn, "document_types", "external_source", "VARCHAR(50)")
            _add_column_if_missing(conn, "document_types", "external_id", "VARCHAR(100)")
            _add_index_if_missing(conn, "document_types", "ix_document_types_external_id", "external_id")
    except Exception as e:
        print(f"[Migration] ERROR adding external_source/external_id columns to document_types: {e}")
    try:
        with db.engine.connect() as conn:
            _add_column_if_missing(conn, "document_fields", "external_property_id", "VARCHAR(100)")
            _add_column_if_missing(conn, "document_fields", "external_property_type", "INT")
    except Exception as e:
        print(f"[Migration] ERROR adding external_property_id/external_property_type columns to document_fields: {e}")
    try:
        with db.engine.connect() as conn:
            _add_column_if_missing(conn, "document_types", "active_prompt_version_id", "INT")
            _add_index_if_missing(conn, "document_types", "ix_document_types_active_prompt_version_id", "active_prompt_version_id")
    except Exception as e:
        print(f"[Migration] ERROR adding active_prompt_version_id column to document_types: {e}")
    try:
        with db.engine.connect() as conn:
            _add_column_if_missing(conn, "document_types", "excluded_fields", "TEXT")
    except Exception as e:
        print(f"[Migration] ERROR adding excluded_fields column to document_types: {e}")
    try:
        with db.engine.connect() as conn:
            _add_column_if_missing(conn, "documents", "field_ambiguities", "TEXT")
    except Exception as e:
        print(f"[Migration] ERROR adding field_ambiguities column to documents: {e}")
    try:
        with db.engine.connect() as conn:
            _add_column_if_missing(conn, "documents", "ad_hoc_fields_dismissed", "BOOLEAN NOT NULL DEFAULT 0")
    except Exception as e:
        print(f"[Migration] ERROR adding ad_hoc_fields_dismissed column to documents: {e}")
    try:
        with db.engine.connect() as conn:
            _add_column_if_missing(conn, "batches", "label", "VARCHAR(200)")
    except Exception as e:
        print(f"[Migration] ERROR adding label column to batches: {e}")

    try:
        with db.engine.connect() as conn:
            _add_column_if_missing(conn, "batches", "display_code", "VARCHAR(20)")
    except Exception as e:
        print(f"[Migration] ERROR adding display_code column to batches: {e}")

    # Create app_settings table if it doesn't exist
    try:
        from sqlalchemy import text
        with db.engine.connect() as conn:
            # Check if table exists
            result = conn.execute(text(
                "SELECT COUNT(*) FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'app_settings'"
            ))
            table_exists = result.fetchone()[0] > 0

            if not table_exists:
                conn.execute(text(
                    "CREATE TABLE app_settings ("
                    "  `key` VARCHAR(100) PRIMARY KEY,"
                    "  value TEXT,"
                    "  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"
                    ")"
                ))
                conn.commit()
                print("[Migration] Created app_settings table")
    except Exception as e:
        print(f"[Migration] ERROR creating app_settings table: {e}")

    # Create exports folder automatically
    try:
        from export_service import DEFAULT_EXPORT_FOLDER
        os.makedirs(DEFAULT_EXPORT_FOLDER, exist_ok=True)
        print(f"[Exports] Folder ready: {DEFAULT_EXPORT_FOLDER}")
    except Exception as e:
        print(f"[Exports] ERROR creating exports folder: {e}")

    seed_builtin_document_types()
    seed_prompt_templates()
    seed_prompt_versions()

    try:
        from retention_service import start_retention_scheduler
        start_retention_scheduler(app)
        print("[Retention] Scheduler started")
    except Exception as e:
        print(f"[Retention] Scheduler not started: {e}")


# Serve frontend UI
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/health')
def health():
    """Lightweight liveness probe for Docker/orchestrator health checks -
    deliberately does not touch the DB or any external service, just
    confirms the Flask process itself is up and responsive."""
    return jsonify({"status": "ok"}), 200


# ── iVault: fetch available record types ─────────────────────────────────────
# Tries the live iVault API first; falls back to a hardcoded list if unreachable.
IVAULT_FALLBACK_TYPES = [
    {'id': 'ap_invoice',     'name': 'AP Invoice',     'category': 'Finance'},
    {'id': 'ar_invoice',     'name': 'AR Invoice',     'category': 'Finance'},
    {'id': 'credit_note',    'name': 'Credit Note',    'category': 'Finance'},
    {'id': 'receipt',        'name': 'Receipt',        'category': 'Finance'},
    {'id': 'purchase_order', 'name': 'Purchase Order', 'category': 'Procurement'},
    {'id': 'contract',       'name': 'Contract',       'category': 'Legal'},
    {'id': 'bank_statement', 'name': 'Bank Statement', 'category': 'Finance'},
    {'id': 'id_card',        'name': 'ID Card',        'category': 'Identity'},
    {'id': 'passport',       'name': 'Passport',       'category': 'Identity'},
    {'id': 'resume',         'name': 'Resume / CV',    'category': 'HR'},
    {'id': 'medical_report', 'name': 'Medical Report', 'category': 'Healthcare'},
]


def _resolve_ivault_api(tenant: str) -> str:
    """Resolve iVault API base from tenant's config.json, fallback to cloud API."""
    import re as _r
    for scheme in ("https", "http"):
        try:
            cfg = requests.get(f"{scheme}://{tenant}/assets/config.json", timeout=5, verify=False)
            if cfg.ok:
                endpoint = cfg.json().get("endPoint", "").rstrip("/")
                if endpoint:
                    return endpoint
        except Exception:
            pass
    return IVAULT_API_BASE


@app.route('/api/ivault/record-types', methods=['GET'])
def get_ivault_record_types():
    """
    Fetch iVault class/record types.
    If tenant + credentials are supplied as query params, fetches live from the
    iVault API (login → GET /Class).  Otherwise returns the fallback list.
    """
    import re as _re
    raw_tenant = request.args.get('tenant', '').strip()
    tenant     = _re.sub(r'^https?://', '', raw_tenant).rstrip('/')
    username   = request.args.get('username', '').strip()
    password   = request.args.get('password', '').strip()

    if tenant and username and password:
        try:
            api_base   = _resolve_ivault_api(tenant)
            login_resp = requests.post(
                f"{api_base}/login",
                data={"username": username, "password": password, "tenant": tenant},
                timeout=10,
            )
            if login_resp.ok:
                token_data  = login_resp.json()
                auth_header = token_data.get("token_type", "Bearer") + " " + token_data["access_token"]
                class_resp  = requests.get(
                    f"{api_base}/Class/list",
                    headers={"Authorization": auth_header},
                    timeout=10,
                )
                if class_resp.ok:
                    raw = class_resp.json()
                    items = raw if isinstance(raw, list) else raw.get("data", raw.get("classes", raw.get("items", [])))
                    types = []
                    for item in items:
                        cid   = (item.get("classId") or item.get("ClassId") or
                                 item.get("id")      or item.get("Id") or "")
                        cname = (item.get("className") or item.get("ClassName") or
                                 item.get("name")    or item.get("Name") or "")
                        cat   = (item.get("category") or item.get("Category") or
                                 item.get("classType") or "")
                        if cid and cname:
                            types.append({"id": str(cid), "name": cname, "category": cat})
                    if types:
                        return jsonify({"record_types": types, "source": "ivault"})
        except Exception:
            pass  # fall through to hardcoded list

    return jsonify({"record_types": IVAULT_FALLBACK_TYPES, "source": "fallback"})


if __name__ == '__main__':
    # threaded=True allows concurrent requests (e.g. PDF viewer can load /original
    # while /blocks is still computing lazy highlights in a separate thread)
    app.run(debug=True, host='0.0.0.0', port=5000, threaded=True)
