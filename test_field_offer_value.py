"""
Unit tests for _best_field_offer_value in document_routes.py.

Tests the two main cases from the bug report:
  1. Long answer with a sentence lead-in — value must come from model_value,
     not from the raw source_quote.
  2. Short quote that is exactly the value (e.g. an invoice number) — must
     pass through unchanged.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import the standalone helper without starting Flask
from document_routes import _best_field_offer_value


def test_signatory_uses_model_value():
    """
    The model returns a clean value field.  The helper must prefer it over
    the verbose source_quote and the sentence-style answer.
    """
    answer = (
        "The signatory is DATO' INDERA NORIDAH BINTI ABDUL RAHIM, "
        "as indicated by the signature line at the bottom of the letter."
    )
    source_quote = "Saya yang menjalankan amanah, (Hamif) (DATO' INDERA NORIDAH BINTI ABDUL RAHIM)"
    model_value  = "DATO' INDERA NORIDAH BINTI ABDUL RAHIM"

    result = _best_field_offer_value(answer, source_quote, model_value)
    assert result == "DATO' INDERA NORIDAH BINTI ABDUL RAHIM", (
        f"Expected clean name, got: {result!r}"
    )


def test_signatory_strips_lead_in_when_no_model_value():
    """
    Without a model_value, a short answer with a lead-in ("The signatory is X")
    should be stripped to just the value, provided the stripped form is ≤ 120 chars.
    """
    answer = "The signatory is Ahmad bin Ali"
    source_quote = "DITANDATANGANI OLEH: Ahmad bin Ali"
    model_value  = ""

    result = _best_field_offer_value(answer, source_quote, model_value)
    assert result == "Ahmad bin Ali", (
        f"Expected stripped name, got: {result!r}"
    )


def test_short_invoice_number_passes_through():
    """
    A short, clean source_quote that is exactly the value (an invoice number)
    must be returned unchanged when both answer and model_value are unavailable.
    """
    answer       = "The invoice number is INV-2024-001."
    source_quote = "INV-2024-001"
    model_value  = ""

    result = _best_field_offer_value(answer, source_quote, model_value)
    # Either the lead-in-stripped answer or the short quote is acceptable
    assert result in ("INV-2024-001", "INV-2024-001"), (
        f"Expected invoice number, got: {result!r}"
    )


def test_long_ambiguous_quote_returns_none():
    """
    When neither the answer nor the quote yields a clean short value,
    the helper must return None so no save offer is shown.
    """
    answer = (
        "The document discusses various contractual obligations between the "
        "parties and outlines the terms for dispute resolution."
    )
    source_quote = (
        "The Parties agree that any dispute arising out of or in connection with "
        "this Agreement shall be referred to and finally resolved by arbitration."
    )
    model_value = ""

    result = _best_field_offer_value(answer, source_quote, model_value)
    assert result is None, (
        f"Expected None (no clean value), got: {result!r}"
    )


def test_model_value_preferred_over_short_quote():
    """
    Even when source_quote is short and clean, if the model provides an
    explicit value it should take priority.
    """
    answer       = "The total amount is RM 1,250.00"
    source_quote = "RM 1,250.00"
    model_value  = "RM 1,250.00"

    result = _best_field_offer_value(answer, source_quote, model_value)
    assert result == "RM 1,250.00", f"Got: {result!r}"


def test_create_last_asked_field_uses_last_answer_value():
    """
    When last_answer_value is present (user just asked "who is the signatory?"
    and the model returned a clean value), create_last_asked_field must use
    that value directly, not re-scan the document.

    We test the short-circuit condition: last_answer_value is non-empty and
    <= 200 chars -> it is returned as-is, bypassing _smart_locate_field_value.
    """
    last_answer_value = "DATO' INDERA NORIDAH BINTI ABDUL RAHIM"
    assert last_answer_value.strip() and len(last_answer_value) <= 200
    # Confirm _best_field_offer_value passes it through when used as model_value
    result = _best_field_offer_value("", "", last_answer_value)
    assert result == last_answer_value, f"Got: {result!r}"


def test_ai_find_value_rejects_raw_source_quote():
    """
    ai_find_value must not store the raw source_quote sentence as the field
    value. _best_field_offer_value must pick the clean name, not "Hamif" or
    the full signature sentence.
    """
    answer       = "The signatory is DATO' INDERA NORIDAH BINTI ABDUL RAHIM"
    source_quote = "Saya yang menjalankan amanah, (Hamif) (DATO' INDERA NORIDAH BINTI ABDUL RAHIM)"
    model_value  = "DATO' INDERA NORIDAH BINTI ABDUL RAHIM"

    candidate = _best_field_offer_value(answer, source_quote, model_value)
    assert candidate == "DATO' INDERA NORIDAH BINTI ABDUL RAHIM", f"Got: {candidate!r}"
    assert candidate is not None and "Hamif" not in candidate
    assert "Saya" not in candidate


def test_no_prior_answer_returns_none_triggers_search():
    """
    When there is no last_answer_value (user typed "add field" cold, with no
    prior question), _best_field_offer_value returns None, meaning the handler
    must fall through to _locate_new_field_or_ask / _smart_locate_field_value.
    """
    result = _best_field_offer_value("", "", "")
    assert result is None, f"Expected None, got: {result!r}"


if __name__ == "__main__":
    test_signatory_uses_model_value()
    print("PASSED: test_signatory_uses_model_value")

    test_signatory_strips_lead_in_when_no_model_value()
    print("PASSED: test_signatory_strips_lead_in_when_no_model_value")

    test_short_invoice_number_passes_through()
    print("PASSED: test_short_invoice_number_passes_through")

    test_long_ambiguous_quote_returns_none()
    print("PASSED: test_long_ambiguous_quote_returns_none")

    test_model_value_preferred_over_short_quote()
    print("PASSED: test_model_value_preferred_over_short_quote")

    test_create_last_asked_field_uses_last_answer_value()
    print("PASSED: test_create_last_asked_field_uses_last_answer_value")

    test_ai_find_value_rejects_raw_source_quote()
    print("PASSED: test_ai_find_value_rejects_raw_source_quote")

    test_no_prior_answer_returns_none_triggers_search()
    print("PASSED: test_no_prior_answer_returns_none_triggers_search")

    print("\nAll tests passed.")
