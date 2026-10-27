import os
import json
import re
import hashlib
import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROMA_DIR = os.path.join(BASE_DIR, "chroma_db")
os.makedirs(CHROMA_DIR, exist_ok=True)

# ── Models (loaded once at import) ────────────────────────────────────────────
# all-MiniLM-L6-v2: tiny (80MB), fast on CPU, great for document similarity
_embedder = None

def get_embedder():
    global _embedder
    if _embedder is None:
        print("[RAG] Loading SentenceTransformer model...")
        _embedder = SentenceTransformer("all-MiniLM-L6-v2")
        print("[RAG] Model loaded.")
    return _embedder


# ── ChromaDB client + collection ──────────────────────────────────────────────
_chroma_client     = None
_chroma_collection = None

def get_collection():
    global _chroma_client, _chroma_collection
    if _chroma_collection is None:
        _chroma_client = chromadb.PersistentClient(
            path=CHROMA_DIR,
            settings=Settings(anonymized_telemetry=False),
        )
        _chroma_collection = _chroma_client.get_or_create_collection(
            name="vaultmind_documents",
            metadata={"hnsw:space": "cosine"},  # cosine similarity
        )
        print(f"[RAG] ChromaDB ready. Documents stored: {_chroma_collection.count()}")
    return _chroma_collection


_PII_PATTERNS = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED_SSN]"),
    (re.compile(r"\b\d{2}-\d{7}\b"), "[REDACTED_TAX_ID]"),
    (re.compile(r"\b\d{9}\b"), "[REDACTED_TAX_ID]"),
    (re.compile(r"\b(?:\d[ -]?){8,19}\b"), "[REDACTED_ACCOUNT_NUMBER]"),
]


def redact_sensitive_text(text: str) -> str:
    """Mask common PII patterns before the text is chunked and embedded."""
    if not text:
        return ""
    redacted = text
    for pattern, replacement in _PII_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def chunk_text(text: str, chunk_size: int = 1200, overlap: int = 180) -> list:
    """Split text into overlapping chunks for vector storage."""
    if not text:
        return []
    cleaned = text.strip()
    if not cleaned:
        return []
    if chunk_size <= 0:
        return [cleaned]
    overlap = max(0, min(overlap, chunk_size - 1))
    step = max(1, chunk_size - overlap)
    chunks = []
    start = 0
    while start < len(cleaned):
        end = min(len(cleaned), start + chunk_size)
        chunks.append(cleaned[start:end])
        if end >= len(cleaned):
            break
        start += step
    return chunks


def _embedding_to_list(vector) -> list:
    if hasattr(vector, "tolist"):
        return vector.tolist()
    return list(vector)


# ── Store a processed document ────────────────────────────────────────────────

def store_document(doc_id: int, raw_text: str, fields: dict, document_type: str, tenant_id: str = "default", document_class: str = "production"):
    """
    Convert document text to vectors and store them in ChromaDB.
    Called after every document is successfully processed.

    doc_id        → unique ID (matches SQLite id)
    raw_text      → full OCR text
    fields        → extracted fields dict
    document_type → invoice / receipt / etc.
    """
    if not raw_text or not raw_text.strip():
        print(f"[RAG] Skipping doc {doc_id} — no raw text")
        return

    try:
        collection = get_collection()
        embedder   = get_embedder()

        safe_text = redact_sensitive_text(raw_text)
        chunks = chunk_text(safe_text)
        if not chunks:
            print(f"[RAG] Skipping doc {doc_id} — no chunkable text")
            return

        embeddings = [_embedding_to_list(embedder.encode(chunk[:1500])) for chunk in chunks]
        chunk_ids = [f"{doc_id}:{i}" for i in range(len(chunks))]
        chunk_metadata = []
        for i, chunk in enumerate(chunks):
            chunk_metadata.append({
                "doc_id": doc_id,
                "tenant_id": tenant_id,
                "document_type": document_type,
                "document_class": document_class,
                "chunk_index": i,
                "chunk_hash": hashlib.sha256(chunk.encode("utf-8")).hexdigest(),
                "fields_json": json.dumps(fields, ensure_ascii=False)[:1000],
            })

        # Store each chunk with tenant-scoped metadata so queries can be isolated.
        collection.upsert(
            ids        = chunk_ids,
            embeddings = embeddings,
            documents  = [chunk[:2000] for chunk in chunks],
            metadatas  = chunk_metadata,
        )
        print(f"[RAG] Stored doc {doc_id} ({document_type}) as {len(chunks)} chunks for tenant '{tenant_id}'. Total: {collection.count()}")

    except Exception as e:
        # RAG failure should never break the main pipeline
        print(f"[RAG] Store error for doc {doc_id}: {e}")


# ── Retrieve similar documents ────────────────────────────────────────────────

def retrieve_similar(raw_text: str, document_type: str = None, top_k: int = 3, tenant_id: str = None) -> list:
    """
    Find top_k most similar past documents to the given raw_text.
    Optionally filter by document_type for tighter matches.

    Returns list of dicts:
    [
        {
            "doc_id":        1,
            "document_type": "invoice",
            "raw_text":      "...",
            "fields":        { ... },
            "similarity":    0.92,
        },
        ...
    ]
    """
    if not tenant_id:
        raise ValueError("tenant_id is required for vector-store reads")

    try:

        collection = get_collection()

        # Need at least 1 stored document
        if collection.count() == 0:
            return []

        embedder = get_embedder()
        vector   = _embedding_to_list(embedder.encode(redact_sensitive_text(raw_text)[:1500]))

        # Some chromadb versions require multi-condition filters wrapped in
        # {"$and": [...]} rather than a flat multi-key dict, which silently
        # raises (caught below) whenever both filters combine - build it
        # explicitly instead of merging keys into one dict.
        conditions = [{"tenant_id": tenant_id}]
        if document_type and document_type != "other":
            conditions.append({"document_type": document_type})
        where = conditions[0] if len(conditions) == 1 else {"$and": conditions}

        query_kwargs = {
            "query_embeddings": [vector],
            "n_results":        max(1, min(top_k, collection.count())),
            "include":          ["documents", "metadatas", "distances"],
            "where":            where,
        }

        try:
            results = collection.query(**query_kwargs)
        except Exception as e:
            # Distinct from "query succeeded, nothing matched" below - a
            # malformed/incompatible where-filter previously collapsed into
            # the exact same [] return as a genuine no-matches result,
            # silently breaking retrieval with no way to tell the two apart.
            print(f"[RAG] Query failed (where={where!r}): {e}")
            return []

        # Parse results
        similar = []
        ids        = results.get("ids",        [[]])[0]
        docs       = results.get("documents",  [[]])[0]
        metadatas  = results.get("metadatas",  [[]])[0]
        distances  = results.get("distances",  [[]])[0]

        for i, doc_id in enumerate(ids):
            meta     = metadatas[i] if i < len(metadatas) else {}
            distance = distances[i] if i < len(distances) else 1.0

            # Cosine distance → similarity (1 = identical, 0 = unrelated)
            similarity = round(1 - distance, 3)

            # Skip very low similarity (below 0.3 = basically unrelated)
            if similarity < 0.3:
                continue

            # Parse stored fields
            try:
                fields = json.loads(meta.get("fields_json", "{}"))
            except Exception:
                fields = {}

            similar.append({
                "doc_id":        meta.get("doc_id", doc_id),
                "document_type": meta.get("document_type", "other"),
                "tenant_id":     meta.get("tenant_id", tenant_id),
                "raw_text":      docs[i] if i < len(docs) else "",
                "fields":        fields,
                "similarity":    similarity,
            })

        print(f"[RAG] Retrieved {len(similar)} similar docs for query")
        return similar

    except Exception as e:
        print(f"[RAG] Retrieve error: {e}")
        return []


# ── Delete a document from ChromaDB ──────────────────────────────────────────

def delete_document(doc_id: int, tenant_id: str = None):
    """Remove all chunks for a document from ChromaDB when deleted from SQLite."""
    try:
        collection = get_collection()
        where = {"doc_id": doc_id}
        if tenant_id:
            where["tenant_id"] = tenant_id
        collection.delete(where=where)
        print(f"[RAG] Deleted doc {doc_id} from ChromaDB for tenant '{tenant_id or 'any'}'")
    except Exception as e:
        print(f"[RAG] Delete error for doc {doc_id}: {e}")


def delete_documents_for_tenant(tenant_id: str):
    """Delete all chunks for a tenant from ChromaDB."""
    try:
        collection = get_collection()
        collection.delete(where={"tenant_id": tenant_id})
        print(f"[RAG] Deleted all ChromaDB chunks for tenant '{tenant_id}'")
    except Exception as e:
        print(f"[RAG] Tenant delete error for '{tenant_id}': {e}")


# ── Stats ─────────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    """Return RAG database stats."""
    try:
        collection = get_collection()
        return {
            "total_documents": collection.count(),
            "chroma_dir":      CHROMA_DIR,
            "model":           "all-MiniLM-L6-v2",
        }
    except Exception as e:
        return {"error": str(e)}