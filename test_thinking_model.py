"""Unit tests for the empty-answer guard in qwen_service.py (local-run fixes,
Task 1): a thinking model whose output only goes to the reasoning channel, or
a generation cut off at max_tokens with no content, must raise
QwenServiceUnavailableError instead of returning "".

Stream chunks and completions are faked - no LM Studio needed.

Run: pytest test_thinking_model.py -v
"""
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
