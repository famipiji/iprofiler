"""Unit tests for local-run fixes Task 2: an unusable AI answer must be
visible (log + error), never disguised as a normal low-confidence result.

call_qwen_text is stubbed - no LM Studio, no DB needed.

Run: pytest test_silent_fallbacks.py -v
"""
import pytest

import qwen_service as qs


@pytest.fixture
def llm(monkeypatch):
    """Stub call_qwen_text; set state["answer"] to what the model returns."""
    state = {"answer": ""}
    monkeypatch.setattr(qs, "call_qwen_text", lambda prompt, max_tokens=1000: state["answer"])
    monkeypatch.setattr(qs, "get_all_active_doc_type_names", lambda: ["invoice", "receipt"])
    monkeypatch.setattr(qs, "get_prompt_override", lambda key: None)
    return state


# -- classify_document -------------------------------------------------------

def test_classify_empty_answer_raises(llm):
    llm["answer"] = "   "
    with pytest.raises(qs.QwenServiceUnavailableError, match="empty answer"):
        qs.classify_document("INVOICE No. 123")


def test_classify_junk_answer_logs_and_uses_fallback_confidence(llm, capsys):
    llm["answer"] = "I think this might be some kind of document"
    result = qs.classify_document("INVOICE No. 123")
    assert result["document_type"] == "unknown"
    assert result["confidence"] == pytest.approx(0.35)
    assert "[Classify] Unparseable answer: I think this might be" in capsys.readouterr().out


def test_classify_json_without_type_uses_hint(llm):
    llm["answer"] = '{"confidence": "high"}'
    result = qs.classify_document("Total 10.00", hint_type="receipt")
    assert result["document_type"] == "receipt"
    assert result["confidence"] == pytest.approx(0.35)


def test_classify_good_answer_unchanged(llm):
    llm["answer"] = '{"document_type": "Invoice", "confidence": "high"}'
    result = qs.classify_document("INVOICE No. 123")
    assert result["document_type"] == "invoice"
    assert result["confidence"] != pytest.approx(0.35)


# -- extract_fields_generic --------------------------------------------------

def test_generic_empty_answer_raises(llm, monkeypatch):
    monkeypatch.setattr(qs, "save_learned_structure", lambda *a, **k: None)
    llm["answer"] = ""
    with pytest.raises(qs.QwenServiceUnavailableError, match="empty answer"):
        qs.extract_fields_generic("Name: Ali\nPhone: 012-3456789", document_type="resume")


# -- extract_fields_custom ---------------------------------------------------

def test_custom_junk_answer_logs_raw_response(llm, capsys):
    llm["answer"] = "Sorry, I cannot find those fields."
    fields = [{"field_name": "invoice_no", "field_label": "Invoice No", "field_type": "text", "sub_columns": None}]
    assert qs.extract_fields_custom("INVOICE No. 123", "my_invoice", fields) == {}
    assert "[CustomExtraction] Raw response: Sorry, I cannot find" in capsys.readouterr().out
