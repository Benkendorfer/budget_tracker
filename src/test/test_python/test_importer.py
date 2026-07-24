"""Unit tests for the CSV importer's pure helpers."""

from budget_tracker.importer import _parse_amount_minor, _row_hash


def test_debit_is_negative_outflow():
    assert _parse_amount_minor("3.28", "", 2) == -328


def test_credit_is_positive_inflow():
    assert _parse_amount_minor("", "10.00", 2) == 1000


def test_blank_amounts_are_zero():
    assert _parse_amount_minor("", "", 2) == 0


def test_decimal_places_zero_currency():
    # A 0-decimal currency (e.g. JPY): minor unit == major unit.
    assert _parse_amount_minor("500", "", 0) == -500


def test_hash_is_stable():
    args = ("8207", "2025-07-23", "2025-07-24", "CERN Snacking", "3.28", "", 0)
    assert _row_hash(*args) == _row_hash(*args)


def test_hash_varies_with_occurrence():
    base = ("8207", "2025-07-23", "2025-07-24", "CERN Snacking", "3.28", "")
    assert _row_hash(*base, 0) != _row_hash(*base, 1)
