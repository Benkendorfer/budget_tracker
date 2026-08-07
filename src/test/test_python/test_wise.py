"""Tests for planning a Wise transfer-log export into transactions.

Planning is pure, so none of this needs a database. Every figure below is invented — the
shape of the export is what is under test, not anybody's money.
"""

import datetime
from datetime import date
from decimal import Decimal

import pytest

from budget_tracker import wise

HEADER = [
    "ID", "Status", "Direction", "Created on", "Finished on",
    "Source fee amount", "Source fee currency", "Target fee amount",
    "Target fee currency", "Source name", "Source amount (after fees)",
    "Source currency", "Target name", "Target amount (after fees)",
    "Target currency", "Exchange rate", "Reference", "Batch", "Created by",
    "Category", "Note",
]


def _row(**overrides):
    row = {name: "" for name in HEADER}
    row.update(
        {
            "ID": "BALANCE_TRANSACTION-1",
            "Status": "COMPLETED",
            "Direction": "OUT",
            "Created on": "2026-03-01 09:00:00",
            "Finished on": "2026-03-02 17:34:31",
            "Source amount (after fees)": "100.00",
            "Source currency": "USD",
            "Target amount (after fees)": "100.00",
            "Target currency": "USD",
            "Exchange rate": "1.0",
            "Source fee amount": "0.00",
            "Source fee currency": "USD",
            "Category": "General",
        }
    )
    row.update(overrides)
    return row


# ------------------------------------------------------------------- detection


def test_the_layout_is_recognized_from_its_signature_columns():
    assert wise.looks_like_wise(HEADER)
    # A tail Wise has changed before is not part of the judgement.
    assert wise.looks_like_wise([c for c in HEADER if c not in ("Batch", "Note")])


def test_an_ordinary_bank_export_is_not_mistaken_for_it():
    assert not wise.looks_like_wise(
        ["Transaction Date", "Description", "Debit", "Credit"]
    )
    assert not wise.looks_like_wise([])


# ------------------------------------------------------------ which leg is yours


def test_money_leaving_is_recorded_against_the_source_currency():
    (txn,) = wise.plan_row(
        _row(Direction="OUT", **{
            "Source amount (after fees)": "250.00", "Source currency": "CHF",
            "Target amount (after fees)": "230.00", "Target currency": "EUR",
            "Target name": "A Shop",
        })
    )
    assert txn.currency == "CHF"  # the balance the money actually left
    assert txn.amount == Decimal("-250.00")
    assert txn.description == "A Shop"
    assert txn.is_transfer is False


def test_money_arriving_is_recorded_against_the_target_currency():
    """The source of an incoming transfer is somebody else's account, so the leg that is
    yours is the target one."""
    (txn,) = wise.plan_row(
        _row(Direction="IN", **{
            "Source name": "An Employer",
            "Source amount (after fees)": "3000.00", "Source currency": "USD",
            "Target amount (after fees)": "3000.00", "Target currency": "USD",
        })
    )
    assert txn.currency == "USD"
    assert txn.amount == Decimal("3000.00")
    assert txn.description == "An Employer"


def test_a_conversion_is_one_row_on_the_source_account_flagged_as_a_transfer():
    """Converting and then spending must not count the same money twice."""
    (txn,) = wise.plan_row(
        _row(Direction="NEUTRAL", **{
            "Source amount (after fees)": "1495.82", "Source currency": "USD",
            "Target amount (after fees)": "1202.86", "Target currency": "CHF",
            "Exchange rate": "0.80415",
        })
    )
    assert txn.currency == "USD" and txn.amount == Decimal("-1495.82")
    assert txn.is_transfer is True
    assert txn.description == "Currency conversion USD → CHF"
    # The other side is kept for information, which is what makes the rate recoverable.
    assert txn.counter_currency == "CHF"
    assert txn.counter_amount == Decimal("1202.86")
    assert txn.exchange_rate == Decimal("0.80415")


def test_a_cross_currency_payment_is_spending_not_a_transfer():
    """The test is the Direction, not whether the currencies differ. Treating every
    cross-currency row as a transfer would erase real payments from the figures."""
    (txn,) = wise.plan_row(
        _row(Direction="OUT", **{
            "Source amount (after fees)": "100.00", "Source currency": "CHF",
            "Target amount (after fees)": "92.00", "Target currency": "EUR",
            "Exchange rate": "0.92",
        })
    )
    assert txn.is_transfer is False
    assert txn.currency == "CHF"


# -------------------------------------------------------------------------- fees


def test_a_fee_becomes_its_own_transaction_in_its_own_currency():
    main, fee = wise.plan_row(
        _row(Direction="NEUTRAL", **{
            "Source amount (after fees)": "1495.82", "Source currency": "USD",
            "Target amount (after fees)": "1202.86", "Target currency": "CHF",
            "Exchange rate": "0.80415",
            "Source fee amount": "4.19", "Source fee currency": "USD",
        })
    )
    assert fee.leg == wise.FEE_LEG
    assert fee.currency == "USD"
    assert fee.amount == Decimal("-4.19")
    assert fee.category == wise.FEE_CATEGORY
    # A fee is real money spent, never a transfer, or it would vanish from the totals.
    assert fee.is_transfer is False
    # Fees are charged on top, so the two legs sum to what actually left the balance.
    assert main.amount + fee.amount == Decimal("-1500.01")


def test_a_row_with_no_fee_produces_only_one_transaction():
    planned = wise.plan_row(_row(**{"Source fee amount": "0.00"}))
    assert len(planned) == 1


def test_the_two_legs_of_a_row_get_different_dedup_keys():
    main, fee = wise.plan_row(_row(**{"Source fee amount": "1.50"}))
    assert main.dedup_key != fee.dedup_key
    assert main.dedup_key.startswith("BALANCE_TRANSACTION-1")


# -------------------------------------------------------------------- whole files


def test_a_conversion_seen_in_both_exports_is_planned_once():
    """A conversion appears in the source balance's export *and* the target's, byte for
    byte. Importing both files must not double it."""
    conversion = _row(
        ID="BALANCE_TRANSACTION-99", Direction="NEUTRAL", **{
            "Source amount (after fees)": "1000.00", "Source currency": "USD",
            "Target amount (after fees)": "800.00", "Target currency": "CHF",
            "Exchange rate": "0.8", "Source fee amount": "2.80",
        }
    )
    plan = wise.plan_rows([conversion, dict(conversion)])
    assert len(plan.transactions) == 2  # the main leg and its fee, once each
    assert sorted(t.leg for t in plan.transactions) == ["fee", "main"]


def test_transfers_that_never_completed_are_skipped_and_counted():
    plan = wise.plan_rows([
        _row(ID="A"),
        _row(ID="B", Status="REFUNDED"),
        _row(ID="C", Status="CANCELLED"),
    ])
    assert [t.external_id for t in plan.transactions] == ["A"]
    assert plan.skipped_incomplete == 2


def test_every_currency_seen_gets_reported_so_accounts_can_be_split():
    plan = wise.plan_rows([
        _row(ID="A", **{"Source currency": "USD", "Target currency": "USD"}),
        _row(ID="B", **{"Source currency": "CHF", "Target currency": "CHF"}),
        _row(ID="C", Direction="IN",
             **{"Source currency": "EUR", "Target currency": "EUR"}),
    ])
    assert plan.currencies == ["CHF", "EUR", "USD"]


def test_a_mixed_currency_file_splits_itself_by_row():
    """One file, three balances: the account comes from each row's own currency rather
    than being asked for once for the whole file."""
    plan = wise.plan_rows([
        _row(ID="A", **{"Source currency": "USD", "Target currency": "USD"}),
        _row(ID="B", **{"Source currency": "CHF", "Target currency": "CHF"}),
    ])
    assert [t.currency for t in plan.transactions] == ["USD", "CHF"]
    assert [wise.account_name(c) for c in plan.currencies] == ["Wise CHF", "Wise USD"]


def test_blank_trailing_rows_are_ignored():
    plan = wise.plan_rows([_row(ID="A"), {name: "" for name in HEADER}])
    assert len(plan.transactions) == 1
    assert plan.skipped_incomplete == 0  # a blank line is not a refused transfer


def test_observed_rates_are_harvested_from_conversions_only():
    """A real rate you actually got on a real day beats one typed in later from memory."""
    plan = wise.plan_rows([
        _row(ID="A", Direction="NEUTRAL", **{
            "Source amount (after fees)": "1000.00", "Source currency": "USD",
            "Target amount (after fees)": "800.00", "Target currency": "CHF",
            "Exchange rate": "0.8",
        }),
        _row(ID="B", Direction="OUT"),  # same currency both sides, rate 1.0
    ])
    assert len(plan.rates) == 1
    rate = plan.rates[0]
    assert (rate.base, rate.quote, rate.rate) == ("USD", "CHF", Decimal("0.8"))
    assert rate.day == date(2026, 3, 2)


# ------------------------------------------------------------------------- dates


def test_the_date_is_when_the_money_moved_not_when_it_was_created():
    (txn,) = wise.plan_row(
        _row(**{"Created on": "2026-03-01 09:00:00",
                "Finished on": "2026-03-05 17:34:31"})
    )
    assert txn.posted_date == date(2026, 3, 5)


def test_an_unfinished_transfer_falls_back_to_when_it_was_created():
    (txn,) = wise.plan_row(
        _row(**{"Created on": "2026-03-01 09:00:00", "Finished on": ""})
    )
    assert txn.posted_date == date(2026, 3, 1)


def test_a_row_with_no_date_at_all_is_rejected_by_name():
    with pytest.raises(ValueError, match="neither a 'Finished on' nor a 'Created on'"):
        wise.plan_row(_row(**{"Created on": "", "Finished on": ""}))


# ------------------------------------------------------------------- bad input


def test_an_unknown_direction_is_rejected_rather_than_guessed():
    with pytest.raises(ValueError, match="Unknown Direction"):
        wise.plan_row(_row(Direction="SIDEWAYS"))


def test_a_row_with_no_id_cannot_be_planned_alone():
    """plan_rows treats it as a blank line, but planning one directly must not silently
    produce a transaction with no way to de-duplicate it."""
    with pytest.raises(ValueError, match="no ID"):
        wise.plan_row(_row(ID=""))


def test_an_unreadable_amount_is_reported_with_the_text_that_broke_it():
    with pytest.raises(ValueError, match="'not money'"):
        wise.plan_row(_row(**{"Source amount (after fees)": "not money"}))


def test_one_transfer_split_across_two_balances_keeps_both_legs():
    """When a card payment is short in the currency it is charged in, Wise funds it from
    two balances at once. Each balance's export shows its own share under the *same*
    transfer ID — so those two rows are not duplicates, and keying on the ID alone would
    silently drop one of the two real legs.
    """
    shared_id = "CARD_TRANSACTION-500"
    from_eur = _row(
        ID=shared_id, Direction="OUT", **{
            "Source amount (after fees)": "6.20", "Source currency": "EUR",
            "Source fee amount": "0.00", "Source fee currency": "EUR",
            "Target amount (after fees)": "6.20", "Target currency": "EUR",
        }
    )
    from_chf = _row(
        ID=shared_id, Direction="OUT", **{
            "Source amount (after fees)": "1.31", "Source currency": "CHF",
            "Source fee amount": "0.01", "Source fee currency": "CHF",
            "Target amount (after fees)": "1.43", "Target currency": "EUR",
        }
    )
    plan = wise.plan_rows([from_eur, from_chf])

    mains = [t for t in plan.transactions if t.leg == wise.MAIN_LEG]
    assert sorted((t.currency, t.amount) for t in mains) == [
        ("CHF", Decimal("-1.31")), ("EUR", Decimal("-6.20"))
    ]
    # And the fee belongs to the balance that was actually charged it — keeping one
    # balance's main leg beside the other's fee would be worse than dropping either.
    fees = [t for t in plan.transactions if t.leg == wise.FEE_LEG]
    assert [(t.currency, t.amount) for t in fees] == [("CHF", Decimal("-0.01"))]


def test_the_same_conversion_in_two_files_still_dedups():
    """The counterpart to the split-payment case: identical rows share a currency, so
    adding the currency to the key must not stop them collapsing."""
    conversion = _row(
        ID="BALANCE_TRANSACTION-77", Direction="NEUTRAL", **{
            "Source amount (after fees)": "500.00", "Source currency": "USD",
            "Target amount (after fees)": "400.00", "Target currency": "CHF",
            "Exchange rate": "0.8", "Source fee amount": "1.40",
        }
    )
    plan = wise.plan_rows([conversion, dict(conversion)])
    assert len(plan.transactions) == 2


# ---------------------------------------------------- writing a plan to the database

import csv as _csv
import io as _io

from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.formats import UnknownFormat
from budget_tracker.importer import import_wise_csv
from budget_tracker.models import Account, Category, Currency, Transaction


def _write_csv(path, rows):
    buffer = _io.StringIO()
    writer = _csv.DictWriter(buffer, fieldnames=HEADER)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    path.write_text(buffer.getvalue(), encoding="utf-8")
    return path


def _factory(tmp_path):
    engine = get_engine(tmp_path / "t.db")
    init_db(engine)
    return get_sessionmaker(engine)


def _conversion(**overrides):
    row = _row(
        ID="BALANCE_TRANSACTION-10", Direction="NEUTRAL", **{
            "Source amount (after fees)": "1000.00", "Source currency": "USD",
            "Target amount (after fees)": "800.00", "Target currency": "CHF",
            "Exchange rate": "0.8", "Source fee amount": "2.80",
            "Source fee currency": "USD",
        }
    )
    row.update(overrides)
    return row


def test_import_splits_one_file_into_an_account_per_currency(tmp_path):
    """The headline of the reader: the account comes from each row's own currency, so a
    mixed export lands in the right balances without being asked."""
    path = _write_csv(tmp_path / "wise.csv", [
        _row(ID="A", **{"Source currency": "USD", "Target currency": "USD"}),
        _row(ID="B", **{"Source currency": "CHF", "Target currency": "CHF"}),
        _row(ID="C", Direction="IN",
             **{"Source currency": "EUR", "Target currency": "EUR"}),
    ])
    factory = _factory(tmp_path)
    with factory() as session:
        result = import_wise_csv(session, path)
    assert result.inserted == 3

    with factory() as session:
        names = sorted(a.name for a in session.query(Account).all())
        assert names == ["Wise CHF", "Wise EUR", "Wise USD"]
        # Each account is denominated in its own currency, which is what keeps every
        # per-account total single-currency and correct.
        for account in session.query(Account).all():
            assert account.name.endswith(account.currency.value)


def test_a_conversion_is_recorded_once_and_kept_out_of_spending(tmp_path):
    path = _write_csv(tmp_path / "wise.csv", [_conversion()])
    factory = _factory(tmp_path)
    with factory() as session:
        import_wise_csv(session, path)

    with factory() as session:
        main = session.query(Transaction).filter(
            Transaction.value_minor == -100_000
        ).one()
        # Flagged so it drops out of the figures: converting and then spending the CHF
        # must not count the same money twice.
        assert main.transfer_group_id is not None
        assert main.category.value == "Transfer"
        assert main.category_source == "transfer"
        # Only the source leg exists — there is no CHF credit anywhere.
        assert session.query(Account).filter_by(name="Wise CHF").one_or_none() is None


def test_a_fee_is_its_own_spending_row_beside_the_conversion(tmp_path):
    path = _write_csv(tmp_path / "wise.csv", [_conversion()])
    factory = _factory(tmp_path)
    with factory() as session:
        import_wise_csv(session, path)

    with factory() as session:
        fee = session.query(Transaction).filter(Transaction.value_minor == -280).one()
        assert fee.category.value == "Fees"
        # Real money spent, so it must not be swept out of the totals with the transfer.
        assert fee.transfer_group_id is None
        total = sum(t.value_minor for t in session.query(Transaction).all())
        assert total == -100_280  # 1000.00 converted + 2.80 fee


def test_reimporting_the_same_export_adds_nothing(tmp_path):
    path = _write_csv(tmp_path / "wise.csv", [_conversion(), _row(ID="B")])
    factory = _factory(tmp_path)
    with factory() as session:
        first = import_wise_csv(session, path)
    with factory() as session:
        second = import_wise_csv(session, path)

    assert first.inserted == 3 and first.skipped_duplicates == 0
    assert second.inserted == 0 and second.skipped_duplicates == 3
    with factory() as session:
        assert session.query(Transaction).count() == 3


def test_the_same_conversion_in_two_exports_imports_once(tmp_path):
    """A conversion appears in both balances' files. Importing both must not double it."""
    source_side = _write_csv(tmp_path / "usd.csv", [_conversion()])
    target_side = _write_csv(tmp_path / "chf.csv", [_conversion()])
    factory = _factory(tmp_path)
    with factory() as session:
        import_wise_csv(session, source_side)
    with factory() as session:
        second = import_wise_csv(session, target_side)

    assert second.inserted == 0 and second.skipped_duplicates == 2
    with factory() as session:
        assert session.query(Transaction).count() == 2


def test_a_split_funded_payment_lands_on_both_accounts(tmp_path):
    """One card payment funded from two balances is two real legs under one ID."""
    shared = "CARD_TRANSACTION-77"
    path = _write_csv(tmp_path / "wise.csv", [
        _row(ID=shared, **{
            "Source amount (after fees)": "6.20", "Source currency": "EUR",
            "Target amount (after fees)": "6.20", "Target currency": "EUR",
        }),
        _row(ID=shared, **{
            "Source amount (after fees)": "1.31", "Source currency": "CHF",
            "Target amount (after fees)": "1.43", "Target currency": "EUR",
        }),
    ])
    factory = _factory(tmp_path)
    with factory() as session:
        result = import_wise_csv(session, path)
    assert result.inserted == 2

    with factory() as session:
        by_account = {
            t.account.name: t.value_minor for t in session.query(Transaction).all()
        }
        assert by_account == {"Wise EUR": -620, "Wise CHF": -131}


def test_the_export_category_is_carried_across(tmp_path):
    path = _write_csv(tmp_path / "wise.csv", [_row(ID="A", Category="Eating out")])
    factory = _factory(tmp_path)
    with factory() as session:
        import_wise_csv(session, path)
    with factory() as session:
        txn = session.query(Transaction).one()
        assert txn.category.value == "Eating out"
        assert txn.category_source == "import"


def test_currencies_are_created_with_their_symbols(tmp_path):
    path = _write_csv(tmp_path / "wise.csv", [
        _row(ID="A", **{"Source currency": "CHF", "Target currency": "CHF"}),
        _row(ID="B", **{"Source currency": "EUR", "Target currency": "EUR"}),
    ])
    factory = _factory(tmp_path)
    with factory() as session:
        import_wise_csv(session, path)
    with factory() as session:
        codes = {c.value: c.symbol for c in session.query(Currency).all()}
        assert codes["CHF"] == "CHF" and codes["EUR"] == "€"


def test_a_file_that_is_not_this_layout_is_refused_by_name(tmp_path):
    path = tmp_path / "other.csv"
    path.write_text("Date,Description,Amount\n2026-01-01,X,-5.00\n", encoding="utf-8")
    factory = _factory(tmp_path)
    with factory() as session:
        with pytest.raises(UnknownFormat, match="not a Wise transfer export"):
            import_wise_csv(session, path)


def test_an_incomplete_transfer_is_not_written(tmp_path):
    path = _write_csv(tmp_path / "wise.csv", [
        _row(ID="A"), _row(ID="B", Status="REFUNDED"),
    ])
    factory = _factory(tmp_path)
    with factory() as session:
        result = import_wise_csv(session, path)
    assert result.inserted == 1
    with factory() as session:
        assert [t.description for t in session.query(Transaction).all()] == ["Wise out"]


def test_a_wise_file_is_recognized_without_being_taught_the_layout(tmp_path):
    """It carries two currencies per row and no single amount column, so none of the
    layout questions could be answered for it. It must arrive ready instead."""
    from budget_tracker.importer import WISE_FORMAT_NAME, inspect_csv

    path = _write_csv(tmp_path / "wise.csv", [_row(ID="A"), _conversion()])
    factory = _factory(tmp_path)
    with factory() as session:
        candidate = inspect_csv(session, path)

    assert candidate.ready is True
    assert candidate.problem is None
    assert candidate.format_name == WISE_FORMAT_NAME
    assert candidate.row_count == 2


def test_the_ordinary_import_path_routes_a_wise_file_to_the_reader(tmp_path):
    """So the import panel, `import all`, and the CLI all pick it up without any of them
    needing to know this layout exists."""
    from budget_tracker.importer import import_csv

    path = _write_csv(tmp_path / "wise.csv", [_conversion()])
    factory = _factory(tmp_path)
    with factory() as session:
        result = import_csv(session, path)

    assert result.inserted == 2  # the conversion and its fee
    with factory() as session:
        assert [a.name for a in session.query(Account).all()] == ["Wise USD"]


def test_routing_does_not_hijack_an_ordinary_export(tmp_path):
    """The signature has to be specific enough that a normal bank CSV still goes through
    the learned-format path."""
    from budget_tracker.importer import inspect_csv

    path = tmp_path / "bank.csv"
    path.write_text(
        "Transaction Date,Posted Date,Description,Debit,Credit\n"
        "2026-01-01,2026-01-02,A SHOP,5.00,\n",
        encoding="utf-8",
    )
    factory = _factory(tmp_path)
    with factory() as session:
        candidate = inspect_csv(session, path)
    assert candidate.format_name != "Wise transfer log"
    assert candidate.problem == "needs setup"


def test_importing_pairs_a_top_up_against_the_bank_account_that_funded_it(tmp_path):
    """A top-up from your own bank into a Wise balance is same-currency, equal and
    opposite, and in a different account — an ordinary transfer. The generic importer
    runs detection at the end of every import; this one has to as well, or the pair sits
    unnoticed and both legs are counted as real income and real spending."""
    factory = _factory(tmp_path)
    with factory() as session:
        currency = Currency(value="USD", symbol="$", decimal_places=2)
        session.add(currency)
        session.flush()
        bank = Account(name="Checking", currency_id=currency.id)
        session.add(bank)
        session.flush()
        session.add(
            Transaction(
                account_id=bank.id,
                currency_id=currency.id,
                posted_date=datetime.date(2026, 3, 2),
                description="TRANSFER OUT",
                raw_description="TRANSFER OUT",
                value_minor=-150_000,
                import_hash="bank-topup",
            )
        )
        session.commit()

    path = _write_csv(tmp_path / "wise.csv", [
        _row(ID="TOPUP", Direction="IN", **{
            "Source name": "Checking", "Source currency": "USD",
            "Source amount (after fees)": "1500.00",
            "Target currency": "USD", "Target amount (after fees)": "1500.00",
        })
    ])
    with factory() as session:
        import_wise_csv(session, path)

    with factory() as session:
        legs = session.query(Transaction).all()
        assert len(legs) == 2
        groups = {leg.transfer_group_id for leg in legs}
        assert None not in groups, "the top-up was never paired with the bank leg"
        assert len(groups) == 1, "the two legs landed in different transfer groups"
