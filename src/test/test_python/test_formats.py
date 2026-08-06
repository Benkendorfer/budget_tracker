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
    """Infer a format from a file and save it, as interactive setup would.

    Loops until every question is resolved rather than asserting one-pass completeness,
    because a question with a default (the ambiguous-date and invert-amount questions
    among them) is meant to be answerable by accepting the suggestion, and a real
    walkthrough would do exactly that.
    """
    fieldnames, rows = read_header_and_rows(path)
    inference = formats.infer(name, fieldnames, rows)
    values, questions = inference.values, inference.questions
    while questions:
        answers = {}
        for question in questions:
            if question.default is not None:
                answers[question.field] = question.default
            elif question.choices:
                answers[question.field] = question.choices[0]
            else:
                raise AssertionError(f"_learn cannot answer {question.field!r}")
        values = formats.apply_answers(values, answers, fieldnames, rows)
        questions = formats.remaining_questions(values, rows, fieldnames)
    spec = formats.spec_from_values(values)
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
    # Everything is inferred except the amount's polarity, which nothing in a single
    # signed column can settle on its own (see test_signed_layout_asks_about_polarity).
    assert [q.field for q in inference.questions] == ["invert_amount"]
    values = inference.values
    assert values["amount_style"] == formats.SIGNED
    assert values["amount_column"] == "Amount"
    assert values["posted_date_column"] == "Posting Date"
    assert values["category_column"] == "Transaction Category"
    assert values["account_column"] is None  # nothing in the file identifies it
    assert values["date_formats"] == ["%m/%d/%Y"]
    # A unique id column identifies a row on its own.
    assert values["dedup_columns"] == ["Transaction ID"]


def test_signed_layout_asks_about_polarity(tmp_path):
    """A single signed column carries no signal of which sign is an outflow, so it is
    always asked, with a real sample value so the choice is answerable at a glance."""
    path = _write(tmp_path, "signed.csv", SIGNED_CSV)
    fieldnames, rows = read_header_and_rows(path)
    inference = formats.infer("signed", fieldnames, rows)
    question = next(q for q in inference.questions if q.field == "invert_amount")
    assert "-200.00000" in question.prompt  # the file's own first sample amount
    assert list(question.choices) == ["yes", "no"]
    assert question.default == "no"

    values = formats.apply_answers(
        inference.values, {"invert_amount": "yes"}, fieldnames, rows
    )
    assert not formats.remaining_questions(values, rows, fieldnames)
    assert formats.spec_from_values(values).invert_amount is True


def test_debit_credit_layout_is_never_asked_about_polarity(tmp_path):
    """A debit/credit pair already says which side is an outflow, so no question."""
    path = _write(tmp_path, "pair.csv", PAIR_CSV)
    fieldnames, rows = read_header_and_rows(path)
    inference = formats.infer("pair", fieldnames, rows)
    assert "invert_amount" not in [q.field for q in inference.questions]
    assert inference.values.get("invert_amount") is None  # unset; the default holds
    assert formats.spec_from_values(inference.values).invert_amount is False


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
    # Naming the amount column also settles amount_style as signed, which is what
    # makes the invert-amount question askable in the same pass as the date one.
    assert {q.field for q in followups} == {"date_formats", "invert_amount"}

    values = formats.apply_answers(
        values, {"date_formats": "day", "invert_amount": "no"}, fieldnames, rows
    )
    assert not formats.remaining_questions(values, rows, fieldnames)
    spec = formats.spec_from_values(values)
    assert spec.date_formats == ["%d/%m/%Y"]
    assert spec.invert_amount is False
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


def test_set_account_prefix_keeps_a_merge_from_being_undone(tmp_path):
    """Without this, the next import recreates the account the merge just folded away."""
    session_factory = _session_factory(tmp_path)
    path = _write(tmp_path, "pair.csv", PAIR_CSV)
    with session_factory() as session:
        _learn(session, path, "cards")
        spec = formats.set_account_prefix(session, "cards", "Card")
        session.commit()
    assert spec.account_prefix == "Card "  # a separator is added for you

    with session_factory() as session:
        import_csv(session, path)
    with session_factory() as session:
        assert [a.name for a in queries.get_accounts(session)] == ["Card 8207"]

    with session_factory() as session:
        formats.set_account_prefix(session, "cards", "")  # removing it is allowed
        session.commit()
        assert formats.get_format(session, "cards").account_prefix == ""


def test_set_invert_amount_flips_a_stored_format(tmp_path):
    session_factory = _session_factory(tmp_path)
    path = _write(tmp_path, "pair.csv", PAIR_CSV)
    with session_factory() as session:
        _learn(session, path, "cards")
        spec = formats.set_invert_amount(session, "cards", True)
        session.commit()
    assert spec.invert_amount is True

    with session_factory() as session:
        assert formats.get_format(session, "cards").invert_amount is True
        spec = formats.set_invert_amount(session, "cards", False)  # reversible
        session.commit()
    assert spec.invert_amount is False


def test_set_invert_amount_reports_an_unknown_format(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        with pytest.raises(formats.UnknownFormat):
            formats.set_invert_amount(session, "nope", True)


# --------------------------------------------------------------------- schema migration

def test_init_db_adds_invert_amount_to_a_preexisting_csv_format_table(tmp_path):
    """Databases created before this flag existed must survive init_db()."""
    import sqlalchemy

    db_path = tmp_path / "old.db"
    engine = get_engine(db_path)
    with engine.begin() as connection:
        connection.execute(
            sqlalchemy.text(
                "CREATE TABLE csv_format (id INTEGER PRIMARY KEY, name VARCHAR UNIQUE, "
                "signature VARCHAR, posted_date_column VARCHAR, description_column "
                "VARCHAR, date_formats VARCHAR, amount_style VARCHAR, dedup_columns "
                "VARCHAR, txn_date_column VARCHAR, category_column VARCHAR, "
                "debit_column VARCHAR, credit_column VARCHAR, amount_column VARCHAR, "
                "account_column VARCHAR, account_prefix VARCHAR, created_at DATETIME)"
            )
        )
        connection.execute(
            sqlalchemy.text(
                "INSERT INTO csv_format (name, signature, posted_date_column, "
                "description_column, date_formats, amount_style, dedup_columns, "
                "account_prefix) VALUES ('old', '[]', 'Date', 'Desc', '[]', 'signed', "
                "'[]', '')"
            )
        )

    init_db(engine)

    with engine.begin() as connection:
        columns = {
            row[1]
            for row in connection.execute(sqlalchemy.text("PRAGMA table_info(csv_format)"))
        }
    assert "invert_amount" in columns

    # The pre-existing row is intact and defaults to no inversion, matching the
    # behaviour every format had before this column existed.
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        assert formats.get_format(session, "old").invert_amount is False
