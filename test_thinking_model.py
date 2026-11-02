"""Unit tests for the empty-answer guard in qwen_service.py (local-run fixes,
Task 1): a thinking model whose output only goes to the reasoning channel, or
a generation cut off at max_tokens with no content, must raise
QwenServiceUnavailableError instead of returning "".

Stream chunks and completions are faked - no LM Studio needed.

Run: pytest test_thinking_model.py -v
"""
import sys
from types import SimpleNamespace as NS

import pytest

import qwen_service as qs


def _chunk(content=None, reasoning=None, finish=None, model="fake-model"):
    delta = NS(content=content, reasoning_content=reasoning)
    return NS(model=model, choices=[NS(delta=delta, finish_reason=finish)])


def _completion(content, reasoning=None, finish="stop", tokens=10):
    msg = NS(content=content, reasoning_content=reasoning)
    return NS(model="fake-model", choices=[NS(message=msg, finish_reason=finish)],
              usage=NS(completion_tokens=tokens))


def test_stream_reasoning_only_hit_length_raises():
    stream = [_chunk(reasoning="thinking ") for _ in range(5)] + [_chunk(finish="length")]
    with pytest.raises(qs.QwenServiceUnavailableError, match="no answer"):
        qs._consume_stream(iter(stream), 100, "test")


def test_stream_length_without_reasoning_raises():
    with pytest.raises(qs.QwenServiceUnavailableError, match="max_tokens"):
        qs._consume_stream(iter([_chunk(finish="length")]), 100, "test")


def test_stream_reasoning_not_mixed_into_answer():
    stream = [_chunk(reasoning="let me think"), _chunk(content='{"a": 1}'), _chunk(finish="stop")]
    text, _ = qs._consume_stream(iter(stream), 100, "test")
    assert text == '{"a": 1}'


def test_stream_plain_empty_stop_returns_empty():
    # No reasoning, normal stop: genuinely empty answer - left to callers (Task 2).
    text, _ = qs._consume_stream(iter([_chunk(finish="stop")]), 100, "test")
    assert text == ""


def test_non_stream_reasoning_only_raises():
    with pytest.raises(qs.QwenServiceUnavailableError, match="reasoning"):
        qs._message_text(_completion(None, reasoning="hmm", finish="length"), "test")


def test_non_stream_normal_answer():
    assert qs._message_text(_completion("  invoice  "), "test") == "invoice"


# -- scanned-PDF path must not retry a "no answer" at lower resolutions -------

def _scanned_pipeline(monkeypatch, n_pages, vision):
    monkeypatch.setattr(qs, "is_scanned_pdf", lambda p: True)
    monkeypatch.setattr(qs, "pdf_pages_to_b64", lambda p, dpi=150, max_dim=1240: [
        (f"page{i}", "image/jpeg", 100, 100) for i in range(1, n_pages + 1)])
    monkeypatch.setattr(qs, "get_all_active_doc_type_names", lambda: ["invoice"])
    monkeypatch.setattr(qs, "_build_vision_extraction_hints", lambda t, raw_text=None: ([], "", ""))
    monkeypatch.setattr(qs, "_ocr_hints_for_pdf", lambda p: [])
    monkeypatch.setattr(qs, "call_qwen_vision", vision)


@pytest.mark.parametrize("n_pages", [1, 3])
def test_scanned_no_answer_is_not_retried_at_lower_dpi(monkeypatch, n_pages):
    calls = []

    def _no_answer(*a, **k):
        calls.append(1)
        raise qs.QwenServiceUnavailableError("call_qwen_vision: model returned no answer")

    _scanned_pipeline(monkeypatch, n_pages, _no_answer)
    with pytest.raises(qs.QwenServiceUnavailableError, match="no answer"):
        qs.process_document("scan.pdf")
    assert len(calls) == 1          # not once per DPI step (5)


def test_scanned_other_errors_still_retry_at_lower_dpi(monkeypatch):
    calls = []

    class _Stop(Exception):
        pass

    def _too_big_then_stop(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("image too large")    # generic error -> next DPI
        raise _Stop()

    _scanned_pipeline(monkeypatch, 3, _too_big_then_stop)
    try:
        qs.process_document("scan.pdf")
    except Exception:
        pass
    assert len(calls) >= 2          # retried at a lower resolution


# -- inline <think>...</think> in content ---------------------------------------

@pytest.mark.parametrize("raw, answer", [
    ('<think>hmm, an invoice</think>\n{"document_type": "invoice"}', '{"document_type": "invoice"}'),
    ('<THINK>a</THINK>x<think>b</think>y', "xy"),
    ('reasoning the template opened</think>\nANSWER', "ANSWER"),
    ('no tags here', "no tags here"),
])
def test_strip_think(raw, answer):
    assert qs._strip_think(raw)[0] == answer


def test_stream_think_block_stripped():
    stream = [_chunk(content="<think>let me "), _chunk(content="see</think>"),
              _chunk(content='{"a": 1}'), _chunk(finish="stop")]
    text, _ = qs._consume_stream(iter(stream), 100, "test")
    assert text == '{"a": 1}'


def test_stream_unclosed_think_raises():
    # Cut off at max_tokens while still "thinking" inline -> no answer.
    stream = [_chunk(content="<think>still thinking"), _chunk(finish="length")]
    with pytest.raises(qs.QwenServiceUnavailableError, match="reasoning"):
        qs._consume_stream(iter(stream), 100, "test")


def test_non_stream_think_only_raises():
    with pytest.raises(qs.QwenServiceUnavailableError, match="reasoning"):
        qs._message_text(_completion("<think>only reasoning</think>", finish="stop"), "test")


def test_non_stream_think_stripped():
    assert qs._message_text(_completion("<think>x</think> receipt "), "test") == "receipt"


# -- get_prompt_override: import failure in the structure_hint lookup -----------

def test_prompt_override_structure_hint_import_failure_no_nameerror(monkeypatch, capsys):
    monkeypatch.setattr(qs._prompt_override_ctx, "overrides", {}, raising=False)
    monkeypatch.setitem(sys.modules, "prompt_studio_service", None)   # import -> ImportError
    monkeypatch.setitem(sys.modules, "app", None)                     # no DB in tests
    assert qs.get_prompt_override("structure_hint:invoice") is None
    assert "DB lookup failed for 'invoice'" in capsys.readouterr().out


# -- direct client calls go through _message_text -------------------------------

@pytest.fixture
def fake_client(monkeypatch):
    state = {"content": ""}

    def _create(**kw):
        return _completion(state["content"])

    monkeypatch.setattr(qs.client.chat.completions, "create", _create)
    return state


def test_extraction_rule_think_stripped(fake_client):
    fake_client["content"] = "<think>what rule?</think>Take the value after 'Invoice No:'."
    rule = qs.generate_field_extraction_rule("invoice_no", "Invoice No", "123", "invoice")
    assert rule == "Take the value after 'Invoice No:'."


def test_guided_reply_think_stripped(fake_client):
    fake_client["content"] = "<think>is OTHER the answer? no</think>ANSWER"
    assert qs.is_reply_to_guided_prompt("RM 50.00", "the total") is True
    fake_client["content"] = "<think>it's an answer</think>OTHER"
    assert qs.is_reply_to_guided_prompt("try to rescan", "the total") is False
