"""The "never invent values" hard rule (qwen_service.NO_INVENTION_RULE) must
reach the final prompt of every field-extraction call - with the built-in
prompts AND when a Prompt Studio version / Prompt Manager template overrides
the type prompt (the rule is appended after the override text, so it can't be
dropped by an override that doesn't include it).

LLM calls are stubbed - no LM Studio, no DB needed.

Run: pytest test_no_invention_rule.py -v
"""
import json
from types import SimpleNamespace

import pytest

import qwen_service as qs

RAW = ("Statement of Account\nJAMES C. MORRISON\nBeginning balance on October 10 $69.96\n"
       "Ending balance on November 9 $586.71\n") * 3

FIELDS = [
    {"field_name": "account_holder", "field_label": "Account Holder", "field_type": "text"},
    {"field_name": "statement_period", "field_label": "Statement Period", "field_type": "date"},
]

STUDIO_TEXT = "PROMPT STUDIO VERSION: statement_period is the 'Beginning balance on' date. " * 2
TEMPLATE_OVERRIDE = "Custom template without any rule placeholders. Text: {{TEXT_BLOCK}}"


@pytest.fixture
def captured(monkeypatch):
    """Records every prompt sent to the model; overrides set via state["overrides"]."""
    state = {"prompts": [], "overrides": {}}

    def text_call(prompt, max_tokens=1000):
        state["prompts"].append(prompt)
        return json.dumps({"account_holder": "JAMES C. MORRISON", "statement_period": None})

    def vision_call(b64, media_type, prompt, max_tokens=1000):
        state["prompts"].append(prompt)
        return 'TEXT:\nx\nFIELDS:\n{"account_holder": "JAMES C. MORRISON"}'

    monkeypatch.setattr(qs, "call_qwen_text", text_call)
    monkeypatch.setattr(qs, "call_qwen_vision", vision_call)
    monkeypatch.setattr(qs, "get_prompt_override", lambda key: state["overrides"].get(key))
    monkeypatch.setattr(qs, "get_custom_doc_fields", lambda doc_type: FIELDS)
    monkeypatch.setattr(qs, "save_learned_structure", lambda *a, **k: None)
    return state


def _assert_rule_last(prompt, *must_contain):
    assert qs.NO_INVENTION_RULE in prompt
    # The rule comes after everything else, override text included.
    for text in must_contain:
        assert text in prompt
        assert prompt.index(text) < prompt.index(qs.NO_INVENTION_RULE)
    assert prompt.rstrip().endswith(qs.NO_INVENTION_RULE)


def test_rule_wording():
    assert "never convert a written period" in qs.NO_INVENTION_RULE
    assert "return null" in qs.NO_INVENTION_RULE


# -- extract_fields_custom ---------------------------------------------------

def test_custom_default_prompt(captured):
    qs.extract_fields_custom(RAW, "bank_statement", FIELDS)
    _assert_rule_last(captured["prompts"][-1], "Fields to extract:")


def test_custom_no_example_date_to_copy(captured):
    qs.extract_fields_custom(RAW, "bank_statement", FIELDS)
    assert "2024-01-15" not in captured["prompts"][-1]


def test_custom_prompt_studio_override(captured):
    captured["overrides"]["structure_hint:bank_statement"] = STUDIO_TEXT
    qs.extract_fields_custom(RAW, "bank_statement", FIELDS)
    _assert_rule_last(captured["prompts"][-1], STUDIO_TEXT.strip())


def test_custom_template_override_without_rule(captured):
    captured["overrides"]["extract_custom_fields_prompt"] = TEMPLATE_OVERRIDE
    qs.extract_fields_custom(RAW, "bank_statement", FIELDS)
    _assert_rule_last(captured["prompts"][-1], "Custom template without any rule placeholders")


# -- extract_fields_generic --------------------------------------------------

@pytest.mark.parametrize("doc_type", ["bank_statement", "some_new_type"])  # directive / open prompt
def test_generic_default_prompt(captured, doc_type):
    qs.extract_fields_generic(RAW, doc_type)
    _assert_rule_last(captured["prompts"][-1])


def test_generic_prompt_studio_override(captured):
    captured["overrides"]["structure_hint:bank_statement"] = STUDIO_TEXT
    qs.extract_fields_generic(RAW, "bank_statement")
    _assert_rule_last(captured["prompts"][-1], STUDIO_TEXT.strip())


@pytest.mark.parametrize("key,doc_type", [
    ("extract_generic_directive_prompt", "bank_statement"),
    ("extract_generic_open_prompt", "some_new_type"),
])
def test_generic_template_override_without_rule(captured, key, doc_type):
    captured["overrides"][key] = TEMPLATE_OVERRIDE
    qs.extract_fields_generic(RAW, doc_type)
    _assert_rule_last(captured["prompts"][-1], "Custom template without any rule placeholders")


# -- extract_fields_vision / combined vision ---------------------------------

def test_vision_default_prompt(captured):
    qs.extract_fields_vision("b64", "image/png", "bank_statement")
    _assert_rule_last(captured["prompts"][-1])


def test_vision_prompt_studio_and_template_override(captured):
    captured["overrides"]["structure_hint:bank_statement"] = STUDIO_TEXT
    captured["overrides"]["extract_vision_prompt"] = "Vision override {{STRUCTURE_HINT_BLOCK}}"
    qs.extract_fields_vision("b64", "image/png", "bank_statement")
    _assert_rule_last(captured["prompts"][-1], "Vision override", STUDIO_TEXT.strip())


def test_combined_vision_default_prompt(captured):
    qs.classify_and_extract_combined_vision("b64", "image/png", forced_document_type="bank_statement")
    _assert_rule_last(captured["prompts"][-1], "NOW process the document image above.")


def test_combined_vision_prompt_studio_override(captured):
    captured["overrides"]["structure_hint:bank_statement"] = STUDIO_TEXT
    qs.classify_and_extract_combined_vision("b64", "image/png", forced_document_type="bank_statement")
    _assert_rule_last(captured["prompts"][-1], STUDIO_TEXT.strip())


# -- extract_fields_on_demand (Ask chat) ---------------------------------------

def test_on_demand_prompt(monkeypatch):
    sent = {}

    def create(**kw):
        sent["messages"] = kw["messages"]
        msg = SimpleNamespace(content='{"account_holder": "JAMES C. MORRISON"}')
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")])

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(qs, "client", fake)
    monkeypatch.setattr(qs, "_message_text", lambda resp, label: resp.choices[0].message.content)
    qs.extract_fields_on_demand(RAW, ["statement period"], None, "bank_statement")
    _assert_rule_last(sent["messages"][0]["content"])
