"""Tests for CSV format storage, header matching, and multi-format import.

Fixtures use two synthetic layouts that between them exercise every branch: a
debit/credit pair with an account column and ISO dates, and a single signed amount
column with slash dates and no account column.
"""

import hashlib

import pytest

from budget_tracker import formats, queries
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.importer import import_csv, read_header_and_rows

PAIR_CSV = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2026-07-19,2026-07-20,8207,COFFEE BAR,Dining,52.22,
2026-07-17,2026-07-20,8207,LAUNDRY,Other Services,20.90,
"""

# Quoted, M/D/YYYY dates, a single signed amount with five decimal places, an id column,
# and nothing identifying the account.
SIGNED_CSV = (
    '"Transaction ID","Posting Date","Effective Date","Transaction Type",'
    '"Posting Status","Amount","Reference Number","Description",'
    '"Transaction Category","Balance"\n'
    '"20260723 412678 20,000","7/23/2026","7/23/2026","Debit","Posted",'
    '"-200.00000","107496338","WIRE OUT","Transfer","3156.50000"\n'
    '"20260630 412679 3","6/30/2026","6/30/2026","Credit","Posted","0.03000",'
    '"106641475","Interest Paid","","107.30000"\n'
)


def _session_factory(tmp_path):
    engine = get_engine(tmp_path / "t.db")
    init_db(engine)
    return get_sessionmaker(engine)


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _learn(session, path, name):
    """Infer a format from a file and save it, as interactive setup would."""
    fieldnames, rows = read_header_and_rows(path)
    inference = formats.infer(name, fieldnames, rows)
    assert inference.complete, f"unexpected questions: {inference.questions}"
    spec = formats.spec_from_values(inference.values)
    formats.save_format(session, spec)
    session.commit()
    return spec


# ------------------------------------------------------------------------ inference

def test_infers_a_debit_credit_layout(tmp_path):
    path = _write(tmp_path, "pair.csv", PAIR_CSV)
    fieldnames, rows = read_header_and_rows(path)
    inference = formats.infer("pair", fieldnames, rows)
    assert inference.complete
    values = inference.values
    assert values["amount_style"] == formats.DEBIT_CREDIT
    assert (values["debit_column"], values["credit_column"]) == ("Debit", "Credit")
    assert values["posted_date_column"] == "Posted Date"
    assert values["txn_date_column"] == "Transaction Date"
    assert values["description_column"] == "Description"
    assert values["category_column"] == "Category"
    assert values["account_column"] == "Card No."
    assert values["date_formats"] == ["%Y-%m-%d"]


def test_infers_a_signed_layout_with_an_id_column(tmp_path):
    path = _write(tmp_path, "signed.csv", SIGNED_CSV)
    fieldnames, rows = read_header_and_rows(path)
    inference = formats.infer("signed", fieldnames, rows)
    assert inference.complete
    values = inference.values
    assert values["amount_style"] == formats.SIGNED
    assert values["amount_column"] == "Amount"
    assert values["posted_date_column"] == "Posting Date"
    assert values["category_column"] == "Transaction Category"
    assert values["account_column"] is None  # nothing in the file identifies it
    assert values["date_formats"] == ["%m/%d/%Y"]
    # A unique id column identifies a row on its own.
    assert values["dedup_columns"] == ["Transaction ID"]


def test_dedup_falls_back_to_mapped_columns_in_a_fixed_order(tmp_path):
    path = _write(tmp_path, "pair.csv", PAIR_CSV)
    fieldnames, rows = read_header_and_rows(path)
    values = formats.infer("pair", fieldnames, rows).values
    assert values["dedup_columns"] == [
        "Card No.",
        "Transaction Date",
        "Posted Date",
        "Description",
        "Debit",
        "Credit",
    ]


def test_asks_when_the_description_column_is_unrecognisable():
    inference = formats.infer("odd", ["Posted Date", "Amount", "Blurb"], [])
    fields = [q.field for q in inference.questions]
    assert "description_column" in fields
    question = next(q for q in inference.questions if q.field == "description_column")
    assert list(question.choices) == ["Posted Date", "Amount", "Blurb"]


def test_asks_when_no_amount_column_is_found():
    inference = formats.infer("odd", ["Posted Date", "Description", "Sum Total"], [])
    assert "amount_column" in [q.field for q in inference.questions]


def test_asks_when_day_month_order_is_ambiguous():
    rows = [{"Posted Date": "01/02/2026"}, {"Posted Date": "03/04/2026"}]
    inference = formats.infer("odd", ["Posted Date", "Description", "Amount"], rows)
    question = next(q for q in inference.questions if q.field == "date_formats")
    assert list(question.choices) == ["month", "day"]
    # A day above 12 settles it without asking.
    rows.append({"Posted Date": "25/04/2026"})
    later = formats.infer("odd", ["Posted Date", "Description", "Amount"], rows)
    assert not [q for q in later.questions if q.field == "date_formats"]
    assert later.values["date_formats"] == ["%d/%m/%Y"]


def test_answers_are_folded_back_in():
    fieldnames = ["Posted Date", "Blurb", "Sum Total"]
    inference = formats.infer("odd", fieldnames, [{"Posted Date": "2026-01-02"}])
    values = formats.apply_answers(
        inference.values,
        {"description_column": "Blurb", "amount_column": "Sum Total"},
        fieldnames,
    )
    spec = formats.spec_from_values(values)
    assert spec.description_column == "Blurb"
    assert spec.amount_column == "Sum Total"
    assert spec.amount_style == formats.SIGNED


def test_date_question_appears_only_once_the_date_column_is_known():
    """Naming the date column is what makes its format checkable.

    Inference cannot ask about a format for a column it has not identified yet, so the
    question has to surface on a later pass or setup dead-ends with no date format.
    """
    fieldnames = ["Buchungstag", "Beguenstigter", "Umsatz"]
    rows = [{"Buchungstag": "01/02/2026"}, {"Buchungstag": "03/04/2026"}]
    inference = formats.infer("euro", fieldnames, rows)
    assert "date_formats" not in [q.field for q in inference.questions]

    values = formats.apply_answers(
        inference.values,
        {
            "posted_date_column": "Buchungstag",
            "description_column": "Beguenstigter",
            "amount_column": "Umsatz",
        },
        fieldnames,
        rows,
    )
    followups = formats.remaining_questions(values, rows, fieldnames)
    assert [q.field for q in followups] == ["date_formats"]

    values = formats.apply_answers(values, {"date_formats": "day"}, fieldnames, rows)
    assert not formats.remaining_questions(values, rows, fieldnames)
    spec = formats.spec_from_values(values)
    assert spec.date_formats == ["%d/%m/%Y"]
    assert spec.dedup_columns  # recomputed once the columns were known


def test_month_day_answer_becomes_a_strptime_format():
    values = formats.apply_answers(
        {"date_formats": []}, {"date_formats": "day"}, ["Posted Date"]
    )
    assert values["date_formats"] == ["%d/%m/%Y"]


# -------------------------------------------------------------------------- storage

def test_formats_round_trip_through_the_database(tmp_path):
    session_factory = _session_factory(tmp_path)
    path = _write(tmp_path, "pair.csv", PAIR_CSV)
    with session_factory() as session:
        saved = _learn(session, path, "cards")
    with session_factory() as session:
        loaded = formats.get_format(session, "cards")
    assert loaded == saved
    assert list(loaded.signature) == list(saved.signature)


def test_saving_the_same_name_updates_in_place(tmp_path):
    session_factory = _session_factory(tmp_path)
    path = _write(tmp_path, "pair.csv", PAIR_CSV)
    with session_factory() as session:
        spec = _learn(session, path, "cards")
        formats.save_format(session, formats.FormatSpec(**{**formats.to_dict(spec), "account_prefix": "Acct "}))
        session.commit()
    with session_factory() as session:
        assert len(formats.list_formats(session)) == 1
        assert formats.get_format(session, "cards").account_prefix == "Acct "


def test_remove_format(tmp_path):
    session_factory = _session_factory(tmp_path)
    path = _write(tmp_path, "pair.csv", PAIR_CSV)
    with session_factory() as session:
        _learn(session, path, "cards")
        assert formats.remove_format(session, "cards")
        assert not formats.remove_format(session, "cards")
        session.commit()
    with session_factory() as session:
        assert formats.list_formats(session) == []


def test_validation_rejects_inconsistent_definitions():
    base = dict(
        name="x",
        signature=["A"],
        posted_date_column="A",
        description_column="A",
        date_formats=["%Y-%m-%d"],
        dedup_columns=["A"],
    )
    with pytest.raises(formats.InvalidFormat):
        formats.validate(formats.FormatSpec(amount_style="signed", **base))
    with pytest.raises(formats.InvalidFormat):
        formats.validate(formats.FormatSpec(amount_style="debit_credit", **base))
    with pytest.raises(formats.InvalidFormat):
        formats.validate(formats.FormatSpec(amount_style="nonsense", **base))


# ------------------------------------------------------------------------ detection

def test_detect_picks_the_matching_format(tmp_path):
    session_factory = _session_factory(tmp_path)
    pair = _write(tmp_path, "pair.csv", PAIR_CSV)
    signed = _write(tmp_path, "signed.csv", SIGNED_CSV)
    with session_factory() as session:
        _learn(session, pair, "cards")
        _learn(session, signed, "checking")
    with session_factory() as session:
        assert formats.detect(session, read_header_and_rows(pair)[0]).name == "cards"
        assert formats.detect(session, read_header_and_rows(signed)[0]).name == "checking"


def test_detect_reports_when_nothing_is_defined(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        with pytest.raises(formats.UnknownFormat) as excinfo:
            formats.detect(session, ["Date", "Payee"])
    assert "No CSV formats are defined" in str(excinfo.value)


def test_detect_lists_known_formats_when_nothing_matches(tmp_path):
    session_factory = _session_factory(tmp_path)
    path = _write(tmp_path, "pair.csv", PAIR_CSV)
    with session_factory() as session:
        _learn(session, path, "cards")
        with pytest.raises(formats.UnknownFormat) as excinfo:
            formats.detect(session, ["Date", "Payee"])
    assert "cards" in str(excinfo.value)


# ----------------------------------------------------------------- account handling

def test_import_without_account_is_refused_when_the_file_lacks_one(tmp_path):
    session_factory = _session_factory(tmp_path)
    path = _write(tmp_path, "checking.csv", SIGNED_CSV)
    with session_factory() as session:
        _learn(session, path, "checking")
        with pytest.raises(formats.AccountRequired) as excinfo:
            import_csv(session, path)
    message = str(excinfo.value)
    assert "checking.csv" in message and "--account" in message


def test_account_is_derived_when_the_file_has_a_column(tmp_path):
    session_factory = _session_factory(tmp_path)
    path = _write(tmp_path, "pair.csv", PAIR_CSV)
    with session_factory() as session:
        _learn(session, path, "cards")
        import_csv(session, path)
    with session_factory() as session:
        assert [a.name for a in queries.get_accounts(session)] == ["8207"]


def test_account_name_overrides_the_derived_one(tmp_path):
    session_factory = _session_factory(tmp_path)
    path = _write(tmp_path, "pair.csv", PAIR_CSV)
    with session_factory() as session:
        _learn(session, path, "cards")
        import_csv(session, path, account_name="Everyday Card")
    with session_factory() as session:
        assert [a.name for a in queries.get_accounts(session)] == ["Everyday Card"]


# ---------------------------------------------------------------------- import rows

def test_signed_rows_parse_dates_signs_and_categories(tmp_path):
    session_factory = _session_factory(tmp_path)
    path = _write(tmp_path, "checking.csv", SIGNED_CSV)
    with session_factory() as session:
        _learn(session, path, "checking")
        result = import_csv(session, path, account_name="Checking")
    assert result.inserted == 2

    with session_factory() as session:
        txns = {t.description: t for t in queries.get_transactions(session)}
    outflow = txns["WIRE OUT"]
    assert outflow.posted_date == "2026-07-23"  # 7/23/2026 read as M/D/YYYY
    assert outflow.amount_minor == -20000  # -200.00000, already signed
    assert outflow.category == "Transfer"

    interest = txns["Interest Paid"]
    assert interest.amount_minor == 3  # 0.03000 -> 3 minor units, not 3000
    assert interest.category == ""  # blank category stays uncategorized


def test_reimport_is_idempotent_for_both_layouts(tmp_path):
    session_factory = _session_factory(tmp_path)
    pair = _write(tmp_path, "pair.csv", PAIR_CSV)
    signed = _write(tmp_path, "signed.csv", SIGNED_CSV)
    with session_factory() as session:
        _learn(session, pair, "cards")
        _learn(session, signed, "checking")
        assert import_csv(session, pair).inserted == 2
        assert import_csv(session, signed, account_name="Checking").inserted == 2
    with session_factory() as session:
        assert import_csv(session, pair).skipped_duplicates == 2
        assert import_csv(session, signed, account_name="Checking").skipped_duplicates == 2


def test_both_layouts_coexist(tmp_path):
    session_factory = _session_factory(tmp_path)
    pair = _write(tmp_path, "pair.csv", PAIR_CSV)
    signed = _write(tmp_path, "signed.csv", SIGNED_CSV)
    with session_factory() as session:
        _learn(session, pair, "cards")
        _learn(session, signed, "checking")
        import_csv(session, pair)
        import_csv(session, signed, account_name="Checking")
    with session_factory() as session:
        assert len(queries.get_transactions(session)) == 4  # nothing swallowed


def test_forcing_a_mismatched_format_is_an_error(tmp_path):
    session_factory = _session_factory(tmp_path)
    pair = _write(tmp_path, "pair.csv", PAIR_CSV)
    signed = _write(tmp_path, "signed.csv", SIGNED_CSV)
    with session_factory() as session:
        _learn(session, pair, "cards")
        checking = _learn(session, signed, "checking")
        with pytest.raises(ValueError) as excinfo:
            import_csv(session, pair, fmt=checking, account_name="X")
    assert "missing columns" in str(excinfo.value)


# ----------------------------------------------------------------------- dedup hash

def test_dedup_hash_is_the_joined_column_values(tmp_path):
    """Pins the hash formula: rows already imported must keep deduplicating.

    The dedup column order is part of the hash, so a change here silently turns every
    stored transaction into a fresh one on the next import.
    """
    expected = hashlib.sha256(
        "|".join(["8207", "2026-07-19", "2026-07-20", "COFFEE BAR", "52.22", "", "0"])
        .encode("utf-8")
    ).hexdigest()

    session_factory = _session_factory(tmp_path)
    path = _write(tmp_path, "pair.csv", PAIR_CSV)
    with session_factory() as session:
        _learn(session, path, "cards")
        import_csv(session, path)
        import sqlalchemy

        from budget_tracker.models import Transaction

        hashes = set(session.scalars(sqlalchemy.select(Transaction.import_hash)))
    assert expected in hashes
