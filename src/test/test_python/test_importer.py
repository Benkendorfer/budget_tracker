"""Tests for the CSV importer: pure helpers plus invert_amount and delete_import.

Fixtures use a single signed-amount layout throughout (a stand-in for a card provider
whose export uses the opposite sign convention from ours), since that is the only
amount_style invert_amount applies to.
"""

import pytest
from sqlalchemy import select

from budget_tracker import formats, importer, queries
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.importer import (
    UnknownImport,
    _parse_amount_minor,
    _parse_signed_minor,
    _row_hash,
    delete_import,
    import_csv,
    read_header_and_rows,
)
from budget_tracker.models import Import, Transaction, Vendor


def test_debit_is_negative_outflow():
    assert _parse_amount_minor("3.28", "", 2) == -328


def test_credit_is_positive_inflow():
    assert _parse_amount_minor("", "10.00", 2) == 1000


def test_blank_amounts_are_zero():
    assert _parse_amount_minor("", "", 2) == 0


def test_decimal_places_zero_currency():
    # A 0-decimal currency (e.g. JPY): minor unit == major unit.
    assert _parse_amount_minor("500", "", 0) == -500


# ------------------------------------------------------------- currency-symbol amounts
#
# A real provider's export writes amounts like "$9.99" and "-$75.00" rather than bare
# decimals, which used to crash Decimal() with decimal.InvalidOperation. These pin the
# normalization that both parse paths now share.

def test_signed_minor_plain_negative():
    assert _parse_signed_minor("-75.00", 2) == -7500


def test_signed_minor_symbol_before_sign():
    assert _parse_signed_minor("$-75.00", 2) == -7500


def test_signed_minor_symbol_after_sign():
    assert _parse_signed_minor("-$75.00", 2) == -7500


def test_signed_minor_thousands_separator():
    assert _parse_signed_minor("$1,234.56", 2) == 123456


def test_signed_minor_separator_and_sign_together():
    assert _parse_signed_minor("-$9,999.99", 2) == -999999


def test_signed_minor_every_shape_seen_in_the_wild():
    cases = {
        "$9.99": 999,
        "$999.99": 99999,
        "$9,999.99": 999999,
        "$99,999.99": 9999999,
        "-$9.99": -999,
        "-$99.99": -9999,
        "-$9,999.99": -999999,
    }
    for raw, expected in cases.items():
        assert _parse_signed_minor(raw, 2) == expected, raw


def test_debit_credit_amounts_also_tolerate_currency_symbols():
    """The debit/credit path shares the same normalizer as the signed path — fixing
    one and not the other would leave half the crash in place."""
    assert _parse_amount_minor("$1,234.56", "", 2) == -123456
    assert _parse_amount_minor("", "$1,234.56", 2) == 123456


def test_unparseable_amount_raises_naming_the_raw_text():
    with pytest.raises(ValueError) as excinfo:
        _parse_signed_minor("N/A", 2)
    assert "N/A" in str(excinfo.value)


def test_european_grouping_is_not_silently_accepted():
    # "1.234,56" is ambiguous without knowing the file's locale (thousands separator or
    # decimal point?), so it is deliberately not supported. It must raise rather than
    # be coerced into a plausible-looking wrong number.
    with pytest.raises(ValueError):
        _parse_signed_minor("1.234,56", 2)


def test_parenthesised_negative_is_not_supported():
    # No format seen here uses this shape; raising rather than guessing at it.
    with pytest.raises(ValueError):
        _parse_signed_minor("(75.00)", 2)


def test_hash_is_stable():
    args = ("8207", "2025-07-23", "2025-07-24", "CERN Snacking", "3.28", "", 0)
    assert _row_hash(*args) == _row_hash(*args)


def test_hash_varies_with_occurrence():
    base = ("8207", "2025-07-23", "2025-07-24", "CERN Snacking", "3.28", "")
    assert _row_hash(*base, 0) != _row_hash(*base, 1)


# ------------------------------------------------------------------------ integration

CARD_CSV = """Transaction Date,Posted Date,Card No.,Description,Amount
2026-07-01,2026-07-02,1234,COFFEE SHOP,617.66
2026-07-03,2026-07-04,1234,PAYMENT RECEIVED,-1000.00
"""

# Same layout, but amounts as the real crash shapes: a currency symbol and a thousands
# separator, sign before the symbol on one row. The amount with an embedded comma has
# to be quoted, same as a real export would quote it, or the CSV reader would split it
# into an extra column.
CURRENCY_CARD_CSV = """Transaction Date,Posted Date,Card No.,Description,Amount
2026-07-01,2026-07-02,1234,COFFEE SHOP,$617.66
2026-07-03,2026-07-04,1234,PAYMENT RECEIVED,"-$1,000.00"
"""

# The same layout, but with the header pushed down by blank leading rows, as an export
# saved out of a spreadsheet arrives. csv would name every column "" from the first line.
PREAMBLE_LEDGER_CSV = ",,,,,\n,,,,,\n,,,,,\nPosting Date,Description,Debit,Credit,,\n" \
    "20260722,TRANSFER IN,,200,,\n20260727,TRANSFER OUT,-100,,,\n"

# Compact dates, a debit column that is already negative, a blank row left behind by a
# spreadsheet, and the trailing empty columns such an edit pads every line with.
COMPACT_LEDGER_CSV = """Posting Date,Description,Debit,Credit,,
,,,,,
20260722,TRANSFER IN,,200,,
20260727,TRANSFER OUT,-100,,,
20260804,FEE,25,,,
"""

# A semicolon-delimited export with a metadata preamble (narrower than the real data)
# and a blank separator line before the real header -- the shape a European bank
# export takes. Modal field count alone is enough to find the header once delimiter
# sniffing gets ';' right: the preamble rows are 3 fields wide, the header and the six
# data rows are 4, so width already outvotes them. The one quoted description
# containing a literal ';' is not decoration -- csv.Sniffer's reliable path looks for a
# quoted field with a consistent character just outside it, same as a real address or
# memo field quoted for holding the delimiter would give it.
SEMICOLON_PREAMBLE_CSV = (
    "Account number:;0215 00194150.40;\n"
    "Account holder:;Jane Doe;\n"
    "Currency:;CHF;\n"
    "Opening balance:;1000.00;\n"
    "\n"
    "Date;Description;Amount;Currency\n"
    '2026-07-01;"Coffee Shop; Main St";-6.50;CHF\n'
    "2026-07-02;GROCERY STORE;-42.10;CHF\n"
    "2026-07-03;PAYMENT RECEIVED;100.00;CHF\n"
    "2026-07-04;BOOKSTORE;-18.25;CHF\n"
    "2026-07-05;PHARMACY;-9.90;CHF\n"
    "2026-07-06;REFUND;25.00;CHF\n"
)

# A comma export padded to one width throughout, title rows included -- the shape a
# payments app takes. Modal count alone would pick line 0: every row, titles included,
# is 7 fields wide. Only the "looks like column names" test tells the title rows (one
# non-empty cell) apart from the real header (six, all distinct, none numeric).
PADDED_TITLE_CSV = (
    "Account Statement,,,,,,\n"
    "Account Activity,,,,,,\n"
    ",ID,Date,Description,Amount,Currency,Status\n"
    "1,T001,2026-07-01,COFFEE SHOP,-6.50,USD,Complete\n"
    "2,T002,2026-07-02,GROCERY STORE,-42.10,USD,Complete\n"
    "3,T003,2026-07-03,PAYMENT RECEIVED,100.00,USD,Complete\n"
)

# Nothing here looks like column names: every row is bare numbers, so every row fails
# the numeric-majority test. Detection has to give up gracefully rather than raise.
NO_HEADER_CSV = "1,2,3\n4,5,6\n7,8,9\n"

# Two legs of one transfer, in separate files as separate accounts would export them.
XFER_OUT_CSV = """Transaction Date,Posted Date,Card No.,Description,Amount
2026-07-01,2026-07-02,CHK,TRANSFER TO CARD,-100.00
"""
XFER_IN_CSV = """Transaction Date,Posted Date,Card No.,Description,Amount
2026-07-01,2026-07-02,CRD,PAYMENT RECEIVED,100.00
"""


def _session_factory(tmp_path, name="t.db"):
    engine = get_engine(tmp_path / name)
    init_db(engine)
    return get_sessionmaker(engine)


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _learn(session, path, name, invert_amount=False):
    """Infer a format and save it, answering the polarity question explicitly.

    Every fixture here is a single signed amount column, so inference always leaves
    exactly one question of substance (invert_amount); this settles it deliberately
    rather than by relying on its default, since these tests need to exercise both
    settings. Any other question left over (currency, always asked with a "USD"
    default) is resolved by accepting its default, the same as a real walkthrough
    pressing enter would.
    """
    fieldnames, rows = read_header_and_rows(path)
    inference = formats.infer(name, fieldnames, rows)
    values = formats.apply_answers(
        inference.values,
        {"invert_amount": "yes" if invert_amount else "no"},
        fieldnames,
        rows,
    )
    questions = formats.remaining_questions(values, rows, fieldnames)
    while questions:
        answers = {q.field: q.default for q in questions if q.default is not None}
        assert answers, f"_learn cannot answer {[q.field for q in questions]}"
        values = formats.apply_answers(values, answers, fieldnames, rows)
        questions = formats.remaining_questions(values, rows, fieldnames)
    spec = formats.spec_from_values(values)
    formats.save_format(session, spec)
    session.commit()
    return spec


# -------------------------------------------------------------- invert_amount x hash

def test_invert_amount_changes_value_not_hash(tmp_path):
    """Flipping invert_amount must never change import_hash — only value_minor.

    The hash is built from raw dedup-column text (see _row_hash), which invert_amount
    never touches. This pins that down at the level of a real import, not just the
    hash formula, since that is exactly what protects dedup across the flag change.
    """
    path = _write(tmp_path, "card.csv", CARD_CSV)

    def _import(invert, db_name):
        session_factory = _session_factory(tmp_path, db_name)
        with session_factory() as session:
            _learn(session, path, "card", invert_amount=invert)
            import_csv(session, path)
        with session_factory() as session:
            return {
                (t.import_hash, t.value_minor)
                for t in session.scalars(select(Transaction))
            }

    normal = _import(False, "a.db")
    inverted = _import(True, "b.db")

    assert {h for h, _ in normal} == {h for h, _ in inverted}  # same dedup identity
    normal_minors = sorted(v for _, v in normal)
    inverted_minors = sorted(v for _, v in inverted)
    assert inverted_minors == sorted(-v for v in normal_minors)


def test_import_handles_currency_formatted_amounts(tmp_path):
    """The crash this fixes, end to end: a real export writes "$617.66" and
    "-$1,000.00", not bare decimals. Also pins that import_hash is built from the raw
    cell text ("$617.66", comma and all) rather than the normalized amount — if
    normalization ever leaked into the hash, every existing import would stop
    de-duplicating.
    """
    session_factory = _session_factory(tmp_path)
    path = _write(tmp_path, "card.csv", CURRENCY_CARD_CSV)
    with session_factory() as session:
        _learn(session, path, "card")
        result = import_csv(session, path)
    assert result.inserted == 2
    assert result.skipped_duplicates == 0

    with session_factory() as session:
        txns = {t.description: t for t in queries.get_transactions(session)}
        hashes = {t.import_hash for t in session.scalars(select(Transaction))}
    assert txns["COFFEE SHOP"].amount_minor == 61766
    assert txns["PAYMENT RECEIVED"].amount_minor == -100000

    expected = _row_hash(
        "1234", "2026-07-01", "2026-07-02", "COFFEE SHOP", "$617.66", 0
    )
    assert expected in hashes  # the literal "$" is in the hash, not the parsed amount


def test_header_is_found_below_blank_leading_rows(tmp_path):
    """A spreadsheet-saved export can begin with rows of bare commas. csv takes the
    first line as the header, so every column comes out named "" and the setup wizard
    asks which of several empty strings holds the amount — unanswerable.

    Inference and import must agree on which line is the header, or a format learned
    from a file would fail to match that same file on import.
    """
    session_factory = _session_factory(tmp_path)
    path = _write(tmp_path, "preamble.csv", PREAMBLE_LEDGER_CSV)

    fieldnames, rows = read_header_and_rows(path)
    assert fieldnames[:4] == ["Posting Date", "Description", "Debit", "Credit"]
    assert len(rows) == 2

    inference = formats.infer("preamble", fieldnames, rows)
    with session_factory() as session:
        formats.save_format(
            session,
            formats.from_dict({
                **inference.values,
                "signature": list(fieldnames),
                "dedup_columns": ["Posting Date", "Description", "Debit", "Credit"],
            }),
        )
        session.commit()
        # No fmt= : this also proves detect() sees the same header inference did.
        result = import_csv(session, path, account_name="Brokerage")
    assert result.inserted == 2

    with session_factory() as session:
        amounts = {t.description: t.amount_minor for t in queries.get_transactions(session)}
    assert amounts == {"TRANSFER IN": 20000, "TRANSFER OUT": -10000}


# ---------------------------------------------------------- delimiter + header sniffing

def test_ordinary_header_on_line_one_is_unaffected(tmp_path):
    """A plain comma file with the header already on line 1 -- the common case -- must
    resolve exactly as before: sniffing must not go looking for trouble that isn't
    there."""
    path = _write(tmp_path, "card.csv", CARD_CSV)
    fieldnames, rows = read_header_and_rows(path)
    assert fieldnames == ["Transaction Date", "Posted Date", "Card No.", "Description", "Amount"]
    assert len(rows) == 2


def test_semicolon_delimiter_and_header_are_found_past_a_metadata_preamble(tmp_path):
    """A European-style export: semicolon-delimited, an 8-ish-line metadata preamble
    three fields wide, a blank line, then the real header. _dict_reader assumed commas,
    so the whole preamble used to collapse into one bogus column."""
    path = _write(tmp_path, "preamble.csv", SEMICOLON_PREAMBLE_CSV)
    fieldnames, rows = read_header_and_rows(path)
    assert fieldnames == ["Date", "Description", "Amount", "Currency"]
    assert len(rows) == 6
    # The delimiter inside the quoted description survived, not just the one outside it.
    assert rows[0]["Description"] == "Coffee Shop; Main St"


def test_header_is_found_among_uniformly_padded_title_rows(tmp_path):
    """A payments-app-style export: every line, title rows included, padded to the same
    field count. Modal count alone picks line 0 (a title); only the plausibility check
    -- more than one non-empty cell, all distinct, not mostly numeric -- rejects it."""
    path = _write(tmp_path, "padded.csv", PADDED_TITLE_CSV)
    fieldnames, rows = read_header_and_rows(path)
    assert fieldnames == ["", "ID", "Date", "Description", "Amount", "Currency", "Status"]
    assert len(rows) == 3
    assert rows[0]["Description"] == "COFFEE SHOP"


def test_no_plausible_header_falls_back_to_the_first_non_blank_line(tmp_path):
    """Nothing in this file reads as column names -- every row is bare numbers -- so
    detection has to degrade to the old behavior (first non-blank line as header)
    instead of raising or picking a data row at random."""
    path = _write(tmp_path, "numbers.csv", NO_HEADER_CSV)
    fieldnames, rows = read_header_and_rows(path)
    assert fieldnames == ["1", "2", "3"]
    assert len(rows) == 2


def test_inference_and_import_agree_on_a_semicolon_header(tmp_path):
    """Inference and import both go through the same reader, so a format learned from
    this file must describe columns import_csv can actually find in it."""
    session_factory = _session_factory(tmp_path)
    path = _write(tmp_path, "preamble.csv", SEMICOLON_PREAMBLE_CSV)
    with session_factory() as session:
        _learn(session, path, "semicolon", invert_amount=False)
        # No fmt= : detect() must see the same fieldnames inference was shown.
        result = import_csv(session, path, account_name="Checking")
    assert result.inserted == 6
    assert result.skipped_duplicates == 0

    with session_factory() as session:
        amounts = {t.description: t.amount_minor for t in queries.get_transactions(session)}
    assert amounts["Coffee Shop; Main St"] == -650
    assert amounts["PAYMENT RECEIVED"] == 10000


def test_reimporting_a_semicolon_file_still_deduplicates(tmp_path):
    session_factory = _session_factory(tmp_path)
    path = _write(tmp_path, "preamble.csv", SEMICOLON_PREAMBLE_CSV)
    with session_factory() as session:
        _learn(session, path, "semicolon")
        first = import_csv(session, path, account_name="Checking")
        second = import_csv(session, path, account_name="Checking")
    assert first.inserted == 6
    assert second.inserted == 0
    assert second.skipped_duplicates == 6


def test_inference_and_import_agree_on_a_padded_title_header(tmp_path):
    """Same guarantee, for the other shape: uniform padding rather than a preamble."""
    session_factory = _session_factory(tmp_path)
    path = _write(tmp_path, "padded.csv", PADDED_TITLE_CSV)
    with session_factory() as session:
        _learn(session, path, "padded", invert_amount=False)
        result = import_csv(session, path, account_name="Card1")
    assert result.inserted == 3
    assert result.skipped_duplicates == 0

    with session_factory() as session:
        amounts = {t.description: t.amount_minor for t in queries.get_transactions(session)}
    assert amounts == {
        "COFFEE SHOP": -650,
        "GROCERY STORE": -4210,
        "PAYMENT RECEIVED": 10000,
    }


def test_reimporting_a_padded_title_file_still_deduplicates(tmp_path):
    session_factory = _session_factory(tmp_path)
    path = _write(tmp_path, "padded.csv", PADDED_TITLE_CSV)
    with session_factory() as session:
        _learn(session, path, "padded")
        first = import_csv(session, path, account_name="Card1")
        second = import_csv(session, path, account_name="Card1")
    assert first.inserted == 3
    assert second.inserted == 0
    assert second.skipped_duplicates == 3


def test_import_skips_blank_rows_and_reads_a_signed_debit_column(tmp_path):
    """Three things a real broker export brought at once.

    A row of bare commas survives a spreadsheet round-trip and csv reads it as a dict of
    empty strings, so it used to reach the date parser and fail there — blaming the date
    format for a row holding no data. Dates come as eight bare digits. And the debit
    column is *already* negative, which the old code negated again, turning a 100.00
    outflow into 100.00 of income: plausible on screen rather than obviously broken.
    """
    session_factory = _session_factory(tmp_path)
    path = _write(tmp_path, "ledger.csv", COMPACT_LEDGER_CSV)
    fieldnames, rows = read_header_and_rows(path)
    inference = formats.infer("ledger", fieldnames, rows)
    # Nothing to ask about polarity or dates: a debit/credit pair settles polarity, and
    # %Y%m%d now infers. currency is always left over -- nothing in a CSV states it --
    # and its "USD" default is what from_dict below relies on to build a usable spec
    # without answering it.
    assert inference.values["date_formats"] == ["%Y%m%d"]
    assert [q.field for q in formats.remaining_questions(inference.values, rows, fieldnames)] == [
        "currency"
    ]

    with session_factory() as session:
        formats.save_format(
            session,
            formats.from_dict({
                **inference.values,
                "signature": list(fieldnames),
                "dedup_columns": ["Posting Date", "Description", "Debit", "Credit"],
            }),
        )
        session.commit()
        result = import_csv(session, path, account_name="Brokerage")

    assert result.inserted == 3  # the blank row is not a transaction
    with session_factory() as session:
        amounts = {t.description: t.amount_minor for t in queries.get_transactions(session)}
    assert amounts == {"TRANSFER IN": 20000, "TRANSFER OUT": -10000, "FEE": -2500}
    # The column decides the direction: a debit is an outflow whether the provider
    # writes "-100" or "25".
    assert sum(amounts.values()) == 7500


# ------------------------------------------------------------------------ delete_import

def test_delete_import_reports_an_unknown_id(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        with pytest.raises(UnknownImport) as excinfo:
            delete_import(session, 999)
    assert "999" in str(excinfo.value)


def test_delete_import_removes_transactions_but_keeps_reference_data(tmp_path):
    session_factory = _session_factory(tmp_path)
    path = _write(tmp_path, "card.csv", CARD_CSV)
    with session_factory() as session:
        _learn(session, path, "card")
        import_csv(session, path)

    with session_factory() as session:
        import_id = session.scalar(select(Import.id))
        result = delete_import(session, import_id)
        session.commit()
    assert result.transactions_deleted == 2
    assert result.transfers_broken == 0

    with session_factory() as session:
        assert session.scalar(select(Transaction)) is None
        assert session.scalar(select(Import)) is None
        # The account and vendors the import created are left behind, unreferenced but
        # intact — a rule or a future import may still want them.
        assert queries.get_accounts(session)
        assert session.scalar(select(Vendor)) is not None


def test_delete_import_of_an_unknown_id_changes_nothing(tmp_path):
    session_factory = _session_factory(tmp_path)
    path = _write(tmp_path, "card.csv", CARD_CSV)
    with session_factory() as session:
        _learn(session, path, "card")
        import_csv(session, path)
    with session_factory() as session:
        with pytest.raises(UnknownImport):
            delete_import(session, 999)
    with session_factory() as session:
        assert len(queries.get_transactions(session)) == 2  # untouched


def test_delete_import_clears_the_surviving_leg_of_a_transfer(tmp_path):
    session_factory = _session_factory(tmp_path)
    out_path = _write(tmp_path, "out.csv", XFER_OUT_CSV)
    in_path = _write(tmp_path, "in.csv", XFER_IN_CSV)
    with session_factory() as session:
        _learn(session, out_path, "card")
        import_csv(session, out_path)
        import_csv(session, in_path)  # detect_transfers pairs it with the first leg

    with session_factory() as session:
        legs = list(session.scalars(select(Transaction)))
        assert len(legs) == 2
        assert all(t.transfer_group_id is not None for t in legs)
        assert all(t.category_source == "transfer" for t in legs)
        out_import_id = next(
            t.import_id for t in legs if t.description == "TRANSFER TO CARD"
        )

    with session_factory() as session:
        result = delete_import(session, out_import_id)
        session.commit()
    assert result.transactions_deleted == 1
    assert result.transfers_broken == 1

    with session_factory() as session:
        survivor = session.scalar(select(Transaction))
        assert survivor.description == "PAYMENT RECEIVED"
        assert survivor.transfer_group_id is None  # no longer treated as a transfer
        assert survivor.category_source == "unset"
        assert survivor.category_id is None


def test_delete_import_leaves_a_manually_set_category_alone(tmp_path):
    """category_source == 'transfer' is what triggers the reset; a category the user
    set by hand on the surviving leg is not transfer detection's to touch."""
    session_factory = _session_factory(tmp_path)
    out_path = _write(tmp_path, "out.csv", XFER_OUT_CSV)
    in_path = _write(tmp_path, "in.csv", XFER_IN_CSV)
    with session_factory() as session:
        _learn(session, out_path, "card")
        import_csv(session, out_path)
        import_csv(session, in_path)

    with session_factory() as session:
        survivor = session.scalar(
            select(Transaction).where(Transaction.description == "PAYMENT RECEIVED")
        )
        survivor.category_source = "manual"  # simulate a manual override afterwards
        out_import_id = session.scalar(
            select(Transaction.import_id).where(
                Transaction.description == "TRANSFER TO CARD"
            )
        )
        session.commit()

    with session_factory() as session:
        delete_import(session, out_import_id)
        session.commit()

    with session_factory() as session:
        survivor = session.scalar(select(Transaction))
        assert survivor.transfer_group_id is None  # pairing is still undone
        assert survivor.category_source == "manual"  # but the category is untouched


# ----------------------------------------------------------------------- round trip

def test_round_trip_wrong_polarity_delete_flip_reimport(tmp_path):
    """The actual bug scenario: a format learned with the wrong sign convention,
    discovered after import. Deleting and re-importing under the corrected flag must
    recover every row with the right sign, with nothing skipped as a duplicate — proof
    the hash never depended on the amount.
    """
    session_factory = _session_factory(tmp_path)
    path = _write(tmp_path, "card.csv", CARD_CSV)
    with session_factory() as session:
        _learn(session, path, "card", invert_amount=False)  # the original mistake
        first = import_csv(session, path)
    assert first.inserted == 2
    assert first.skipped_duplicates == 0

    with session_factory() as session:
        wrong_minors = sorted(
            t.value_minor for t in session.scalars(select(Transaction))
        )
        import_id = session.scalar(select(Import.id))
        deleted = delete_import(session, import_id)
        session.commit()
    assert deleted.transactions_deleted == first.inserted

    with session_factory() as session:
        assert session.scalar(select(Transaction)) is None  # nothing left to dedup off
        formats.set_invert_amount(session, "card", True)
        session.commit()
        second = import_csv(session, path)
    assert second.inserted == first.inserted  # every row came back
    assert second.skipped_duplicates == 0  # none mistaken for a duplicate

    with session_factory() as session:
        right_minors = sorted(
            t.value_minor for t in session.scalars(select(Transaction))
        )
    assert right_minors == sorted(-m for m in wrong_minors)


# --------------------------------------------------------------- browsing the inbox


def _inbox(tmp_path):
    """An inbox with a nested month folder and some noise to ignore."""
    root = tmp_path / "to_import"
    (root / "2026" / "january").mkdir(parents=True)
    (root / ".hidden").mkdir()
    (root / "top.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (root / "notes.txt").write_text("not a csv", encoding="utf-8")
    (root / "2026" / "year.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (root / "2026" / "january" / "jan.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (root / "2026" / "january" / "also.CSV").write_text("a,b\n1,2\n", encoding="utf-8")
    return root


def test_the_inbox_root_lists_its_folders_and_its_own_csvs(tmp_path):
    root = _inbox(tmp_path)
    listing = importer.list_inbox(root, root)

    assert listing.parent is None  # nowhere to climb to from the top
    assert [folder.name for folder in listing.folders] == ["2026"]
    assert [path.name for path in listing.files] == ["top.csv"]


def test_a_folder_counts_every_csv_beneath_it_however_deep(tmp_path):
    """The count is what tells you whether descending is worth it, so it cannot stop at
    the first level."""
    root = _inbox(tmp_path)
    listing = importer.list_inbox(root, root)
    assert listing.folders[0].csv_count == 3  # year.csv + two in january/


def test_descending_lists_that_folder_and_offers_the_way_back(tmp_path):
    root = _inbox(tmp_path)
    listing = importer.list_inbox(root / "2026", root)

    assert listing.parent == root
    assert [folder.name for folder in listing.folders] == ["january"]
    assert [path.name for path in listing.files] == ["year.csv"]


def test_the_walk_cannot_climb_out_of_the_inbox(tmp_path):
    """``parent`` is the only way up, so it has to stop at the root — otherwise browsing
    would start offering the rest of the filesystem for import."""
    root = _inbox(tmp_path)
    assert importer.list_inbox(root, root).parent is None
    # And a directory outside the inbox altogether gets no way up either.
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert importer.list_inbox(outside, root).parent is None


def test_hidden_entries_and_non_csv_files_are_skipped(tmp_path):
    root = _inbox(tmp_path)
    listing = importer.list_inbox(root, root)
    assert all(not folder.name.startswith(".") for folder in listing.folders)
    assert all(path.suffix.lower() == ".csv" for path in listing.files)
    assert "notes.txt" not in [path.name for path in listing.files]


def test_an_uppercase_extension_still_counts_as_a_csv(tmp_path):
    root = _inbox(tmp_path)
    listing = importer.list_inbox(root / "2026" / "january", root)
    assert sorted(path.name for path in listing.files) == ["also.CSV", "jan.csv"]


def test_an_unreadable_directory_lists_as_empty_rather_than_raising(tmp_path):
    """The picker showing nothing beats the app refusing to open."""
    root = _inbox(tmp_path)
    listing = importer.list_inbox(root / "does-not-exist", root)
    assert listing.folders == [] and listing.files == []
    # It is still inside the inbox, so it still knows the way back.
    assert listing.parent == root


def test_entries_are_sorted_case_insensitively(tmp_path):
    root = tmp_path / "to_import"
    root.mkdir()
    for name in ("Zebra.csv", "apple.csv", "Mango.csv"):
        (root / name).write_text("a\n1\n", encoding="utf-8")
    listing = importer.list_inbox(root, root)
    assert [path.name for path in listing.files] == [
        "apple.csv", "Mango.csv", "Zebra.csv"
    ]


def test_a_semicolon_file_is_recognized_without_a_quoted_delimiter(tmp_path):
    """csv.Sniffer resolves a semicolon confidently only when the sample happens to
    contain a *quoted* field with one inside it. A plainly separated file raises and
    falls back to comma, collapsing the whole header into a single column -- which is
    the original bug, surviving in a file that merely lacks a quoted address.
    """
    path = tmp_path / "plain.csv"
    path.write_text(
        "Account number:;0000 000;\n"
        "Valued in:;CHF;\n"
        "\n"
        "Trade date;Currency;Debit;Credit;Description\n"
        "2026-01-05;CHF;42.50;;Groceries\n"
        "2026-01-06;CHF;;100.00;Salary\n",
        encoding="utf-8",
    )
    fieldnames, rows = read_header_and_rows(path)
    assert fieldnames[:2] == ["Trade date", "Currency"]
    assert len(rows) == 2
    assert rows[0]["Debit"] == "42.50"


def test_a_tab_separated_file_is_recognized_too(tmp_path):
    path = tmp_path / "tabs.csv"
    path.write_text(
        "Date\tDescription\tAmount\n2026-01-05\tA SHOP\t-5.00\n", encoding="utf-8"
    )
    fieldnames, rows = read_header_and_rows(path)
    assert fieldnames == ["Date", "Description", "Amount"]
    assert rows[0]["Amount"] == "-5.00"


def test_a_comma_file_containing_semicolons_still_reads_as_commas(tmp_path):
    """Scoring picks whichever separator actually makes the file tabular, so stray
    semicolons inside descriptions must not win over the real delimiter."""
    path = tmp_path / "commas.csv"
    path.write_text(
        "Date,Description,Amount\n"
        "2026-01-05,\"A SHOP; MAIN ST\",-5.00\n"
        "2026-01-06,\"ANOTHER; PLACE; HERE\",-6.00\n",
        encoding="utf-8",
    )
    fieldnames, rows = read_header_and_rows(path)
    assert fieldnames == ["Date", "Description", "Amount"]
    assert rows[1]["Description"] == "ANOTHER; PLACE; HERE"


def test_comma_wins_a_tie_so_existing_layouts_are_untouched(tmp_path):
    """A file tabular under more than one candidate must keep reading as it always did."""
    path = tmp_path / "tie.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    assert read_header_and_rows(path)[0] == ["a", "b"]
