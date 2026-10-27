import sys
import types
import unittest
from unittest.mock import patch


class _FakeSettings:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakePersistentClient:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def get_or_create_collection(self, *args, **kwargs):
        raise AssertionError("test should not create a real Chroma collection")


fake_chromadb = types.ModuleType("chromadb")
fake_chromadb.PersistentClient = _FakePersistentClient

fake_config = types.ModuleType("chromadb.config")
fake_config.Settings = _FakeSettings

fake_sentence_transformers = types.ModuleType("sentence_transformers")


class _PlaceholderSentenceTransformer:
    def __init__(self, *args, **kwargs):
        pass

    def encode(self, text):
        return [0.1, 0.2, 0.3]


fake_sentence_transformers.SentenceTransformer = _PlaceholderSentenceTransformer

# rag_service imports chromadb/sentence_transformers at its own module level,
# so these stubs must already be in sys.modules by the time `import
# rag_service` below runs - too early for a normal pytest fixture (fixtures
# only run once this module has already finished importing). Use
# patch.dict's start()/stop() instead of a raw sys.modules mutation, so the
# stubs are properly removed in tearDownModule() rather than leaking into
# every other test that happens to run afterward in the same pytest session.
_sys_modules_patcher = patch.dict(sys.modules, {
    "chromadb": fake_chromadb,
    "chromadb.config": fake_config,
    "sentence_transformers": fake_sentence_transformers,
})
_sys_modules_patcher.start()

import rag_service


def tearDownModule():
    """Restores sys.modules to what it was before this file's stubs were
    installed, so a later test module that imports the real chromadb/
    sentence_transformers doesn't silently get these no-op fakes instead."""
    _sys_modules_patcher.stop()


class FakeEmbedder:
    def encode(self, text):
        return [0.1, 0.2, 0.3]


class FakeCollection:
    def __init__(self):
        self.last_query_kwargs = None

    def count(self):
        return 2

    def query(self, **kwargs):
        self.last_query_kwargs = kwargs
        where = kwargs.get("where") or {}
        tenant_id = where.get("tenant_id")

        if tenant_id != "tenant_a":
            raise AssertionError(f"tenant filter missing or incorrect: {where}")

        return {
            "ids": [["tenant_a:0"]],
            "documents": [["tenant_a chunk"]],
            "metadatas": [[{"doc_id": 10, "tenant_id": "tenant_a", "document_type": "invoice", "fields_json": "{}"}]],
            "distances": [[0.1]],
        }

    def upsert(self, **kwargs):
        raise NotImplementedError

    def delete(self, **kwargs):
        raise NotImplementedError


class TenantIsolationTests(unittest.TestCase):
    def test_retrieve_similar_requires_tenant_id(self):
        with self.assertRaises(ValueError):
            rag_service.retrieve_similar("hello world")

    def test_retrieve_similar_includes_tenant_filter(self):
        fake_collection = FakeCollection()

        with patch.object(rag_service, "get_collection", return_value=fake_collection), \
             patch.object(rag_service, "get_embedder", return_value=FakeEmbedder()):
            results = rag_service.retrieve_similar("invoice total 123", tenant_id="tenant_a")

        self.assertEqual(fake_collection.last_query_kwargs["where"], {"tenant_id": "tenant_a"})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["tenant_id"], "tenant_a")
        self.assertEqual(results[0]["doc_id"], 10)


if __name__ == "__main__":
    unittest.main()
