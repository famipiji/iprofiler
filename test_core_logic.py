"""
Unit tests for pure, dependency-free parsing/matching logic in the core
extraction pipeline. Deliberately scoped to functions that don't need a live
LLM, DB, or file I/O to exercise meaningfully - qwen_service.py (~4900
lines) and document_routes.py (~4000 lines) otherwise have zero automated
coverage for a codebase this size. Not exhaustive; a starting point for the
highest-risk parsing logic (field-extraction, JSON recovery from a model
response, document-type-line parsing, field-name normalization).
"""
import pytest

import qwen_service
import settings_routes


# ── qwen_service.safe_parse_json ──────────────────────────────────────────────

class TestSafeParseJson:
    def test_clean_json(self):
        assert qwen_service.safe_parse_json('{"a": 1, "b": "two"}') == {"a": 1, "b": "two"}

    def test_markdown_fenced_json(self):
        text = '```json\n{"intent": "qa", "field_hints": []}\n```'
        assert qwen_service.safe_parse_json(text) == {"intent": "qa", "field_hints": []}

    def test_json_with_surrounding_prose(self):
        text = 'Sure, here is the result:\n{"is_extraction_request": true, "count_hint": 5}\nHope that helps!'
        result = qwen_service.safe_parse_json(text)
        assert result["is_extraction_request"] is True
        assert result["count_hint"] == 5

    def test_truncated_json_is_recovered(self):
        # Simulates a response cut off by max_tokens before the closing
        # brace, mid-value rather than mid-array (safe_parse_json's brace/
        # quote-closing recovery handles an unclosed object+string, not an
        # unclosed array/list inside it).
        text = '{"intent": "qa", "field_hints": [], "note": "cut off here'
        result = qwen_service.safe_parse_json(text)
        assert result.get("intent") == "qa"

    def test_bare_array_is_rejected_not_a_dict(self):
        assert qwen_service.safe_parse_json('["a", "b", "c"]') == {}

    def test_garbage_returns_empty_dict(self):
        assert qwen_service.safe_parse_json("not json at all, sorry") == {}

    def test_empty_string(self):
        assert qwen_service.safe_parse_json("") == {}


# ── qwen_service._parse_type_line ─────────────────────────────────────────────

class TestParseTypeLine:
    def test_normal_type(self):
        assert qwen_service._parse_type_line("TYPE: Invoice") == "invoice"

    def test_multi_word_type_becomes_snake_case(self):
        assert qwen_service._parse_type_line("TYPE: Bank Statement") == "bank_statement"

    def test_empty_type_falls_back_to_default(self):
        assert qwen_service._parse_type_line("TYPE:", default="other") == "other"

    def test_whitespace_only_type_falls_back(self):
        assert qwen_service._parse_type_line("TYPE:    ", default="unknown") == "unknown"

    def test_hyphenated_type_normalized(self):
        assert qwen_service._parse_type_line("TYPE: bond-purchase-agreement") == "bond_purchase_agreement"


# ── qwen_service.find_field_value_in_text ─────────────────────────────────────

class TestFindFieldValueInText:
    def test_colon_separated_label(self):
        text = "Invoice Number: INV-2024-001\nDate: 2024-01-15"
        val, label = qwen_service.find_field_value_in_text(text, ["Invoice Number"])
        assert val == "INV-2024-001"
        assert label == "Invoice Number"

    def test_tab_separated_label(self):
        text = "Account Holder\t\tJohn Smith"
        val, label = qwen_service.find_field_value_in_text(text, ["Account Holder"])
        assert val == "John"  # value pattern stops at whitespace

    def test_tries_multiple_labels_in_order(self):
        text = "NRIC: 900101-14-1234"
        val, label = qwen_service.find_field_value_in_text(text, ["Passport No", "NRIC"])
        # value pattern includes hyphens (real-world IDs commonly have them),
        # so the full hyphenated value is captured, not just the first segment
        assert val == "900101-14-1234"
        assert label == "NRIC"

    def test_no_match_returns_none_none(self):
        val, label = qwen_service.find_field_value_in_text("Some unrelated text", ["Invoice Number"])
        assert (val, label) == (None, None)

    def test_empty_raw_text(self):
        assert qwen_service.find_field_value_in_text("", ["Invoice Number"]) == (None, None)

    def test_empty_labels(self):
        assert qwen_service.find_field_value_in_text("Invoice Number: 123", []) == (None, None)

    def test_case_insensitive_label_match(self):
        text = "invoice number: ABC123"
        val, label = qwen_service.find_field_value_in_text(text, ["Invoice Number"])
        assert val == "ABC123"


# ── settings_routes._snake_case ────────────────────────────────────────────────

class TestSnakeCase:
    def test_simple_words(self):
        assert settings_routes._snake_case("Invoice Number") == "invoice_number"

    def test_camel_case_input(self):
        assert settings_routes._snake_case("InvoiceNumber") == "invoice_number"

    def test_punctuation_stripped(self):
        assert settings_routes._snake_case("Num. Pages") == "num_pages"

    def test_punctuation_only_gets_deterministic_fallback(self):
        result = settings_routes._snake_case("---")
        assert result != ""
        assert result.startswith("field_")

    def test_distinct_punctuation_only_inputs_dont_collide(self):
        a = settings_routes._snake_case("---")
        b = settings_routes._snake_case("***")
        assert a != b

    def test_same_input_is_deterministic(self):
        assert settings_routes._snake_case("###") == settings_routes._snake_case("###")

    def test_empty_input(self):
        result = settings_routes._snake_case("")
        assert result.startswith("field_")

    def test_none_input_does_not_raise(self):
        result = settings_routes._snake_case(None)
        assert result.startswith("field_")


# ── settings_routes._find_duplicate_field_name ─────────────────────────────────

class TestFindDuplicateFieldName:
    def test_no_duplicates(self):
        fields = [{"field_name": "a"}, {"field_name": "b"}, {"field_name": "c"}]
        assert settings_routes._find_duplicate_field_name(fields) is None

    def test_finds_duplicate(self):
        fields = [{"field_name": "invoice_number"}, {"field_name": "date"}, {"field_name": "invoice_number"}]
        assert settings_routes._find_duplicate_field_name(fields) == "invoice_number"

    def test_ignores_blank_field_names(self):
        fields = [{"field_name": ""}, {"field_name": "  "}, {"field_name": "a"}]
        assert settings_routes._find_duplicate_field_name(fields) is None

    def test_trims_whitespace_before_comparing(self):
        fields = [{"field_name": "amount"}, {"field_name": " amount "}]
        assert settings_routes._find_duplicate_field_name(fields) == "amount"

    def test_empty_list(self):
        assert settings_routes._find_duplicate_field_name([]) is None


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
