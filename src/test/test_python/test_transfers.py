"""Tests for detecting money moved between your own accounts."""

from datetime import date

from budget_tracker import queries, transfers
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.models import Account, Category, Currency, Transaction


def _session_factory(tmp_path):
    engine = get_engine(tmp_path / "t.db")
    init_db(engine)
    return get_sessionmaker(engine)


def _seed(session):
    currency = Currency(value="USD", symbol="$", decimal_places=2)
    session.add(currency)
    session.flush()
    accounts = {}
    for name in ("Checking", "Savings", "Card"):
        account = Account(name=name, currency_id=currency.id)
        session.add(account)
        accounts[name] = account
    session.flush()
    return currency, accounts


def _txn(session, currency, account, day, amount, description="X", **kwargs):
    txn = Transaction(
        account_id=account.id,
        currency_id=currency.id,
        posted_date=date(2026, 7, day),
        description=description,
        raw_description=description,
        value_minor=amount,
        import_hash=f"{account.id}-{day}-{amount}-{description}",
        **kwargs,
    )
    session.add(txn)
    session.flush()
    return txn


def test_opposite_amounts_in_different_accounts_pair_up(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        out = _txn(session, currency, accounts["Checking"], 1, -50000, "Xfer To")
        into = _txn(session, currency, accounts["Savings"], 2, 50000, "Xfer From")
        assert transfers.detect_transfers(session) == 1
        session.commit()
        assert out.transfer_group_id is not None
        assert out.transfer_group_id == into.transfer_group_id

    with session_factory() as session:
        txns = {t.description: t for t in queries.get_transactions(session)}
        assert txns["Xfer To"].category == "Transfer"
        assert txns["Xfer From"].category == "Transfer"


def test_transfers_are_excluded_from_totals(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        _txn(session, currency, accounts["Checking"], 1, -50000, "Xfer To")
        _txn(session, currency, accounts["Savings"], 2, 50000, "Xfer From")
        _txn(session, currency, accounts["Card"], 3, -2500, "Coffee")
        _txn(session, currency, accounts["Checking"], 4, 300000, "Salary")
        session.commit()

    with session_factory() as session:
        before = queries.get_totals(session)
        assert before.outflow_minor == -52500  # the transfer leg still counts
        assert before.inflow_minor == 350000

    with session_factory() as session:
        transfers.detect_transfers(session)
        session.commit()

    with session_factory() as session:
        after = queries.get_totals(session)
    assert after.count == 4  # every row is still listed
    assert after.transfer_count == 2
    assert after.outflow_minor == -2500  # just the coffee
    assert after.inflow_minor == 300000  # just the salary
    assert after.net_minor == 297500


def test_exclusion_holds_when_a_filter_selects_only_one_leg(tmp_path):
    """Filtering to one account leaves a transfer unbalanced; it must still be excluded."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        _txn(session, currency, accounts["Checking"], 1, -50000, "Xfer To")
        _txn(session, currency, accounts["Savings"], 2, 50000, "Xfer From")
        _txn(session, currency, accounts["Checking"], 3, -2500, "Coffee")
        transfers.detect_transfers(session)
        session.commit()

    with session_factory() as session:
        account_id = queries.resolve_account(session, "Checking")
        totals = queries.get_totals(session, account_id=account_id)
    assert totals.count == 2
    assert totals.transfer_count == 1
    assert totals.outflow_minor == -2500  # not -52500
    assert totals.net_minor == -2500


def test_same_account_is_not_a_transfer(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        _txn(session, currency, accounts["Checking"], 1, -50000, "Charge")
        _txn(session, currency, accounts["Checking"], 2, 50000, "Refund")
        assert transfers.detect_transfers(session) == 0


def test_dates_outside_the_window_do_not_pair(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        _txn(session, currency, accounts["Checking"], 1, -50000)
        _txn(session, currency, accounts["Savings"], 20, 50000)
        assert transfers.detect_transfers(session, window_days=5) == 0
        # A wider window does pair them.
        assert transfers.detect_transfers(session, window_days=30) == 1


def test_unequal_amounts_do_not_pair(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        _txn(session, currency, accounts["Checking"], 1, -50000)
        _txn(session, currency, accounts["Savings"], 2, 49900)
        assert transfers.detect_transfers(session) == 0


def test_the_closest_dated_pair_wins(tmp_path):
    """Two outflows compete for one inflow; the nearer date must win, not the first row."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        _txn(session, currency, accounts["Checking"], 1, -50000, "Out A")
        _txn(session, currency, accounts["Card"], 2, -50000, "Out B")
        _txn(session, currency, accounts["Savings"], 2, 50000, "In")
        assert transfers.detect_transfers(session) == 1
        session.commit()

    with session_factory() as session:
        paired = [
            t.description
            for t in session.query(Transaction).all()
            if t.transfer_group_id is not None
        ]
    # The closest-dated outflow wins; the other is left alone.
    assert sorted(paired) == ["In", "Out B"]


def test_detection_is_idempotent(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        _txn(session, currency, accounts["Checking"], 1, -50000)
        _txn(session, currency, accounts["Savings"], 2, 50000)
        assert transfers.detect_transfers(session) == 1
        assert transfers.detect_transfers(session) == 0  # nothing new to find
        session.commit()


def test_zero_amounts_never_pair(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        _txn(session, currency, accounts["Checking"], 1, 0, "Zero A")
        _txn(session, currency, accounts["Savings"], 2, 0, "Zero B")
        assert transfers.detect_transfers(session) == 0


def test_a_manual_category_is_not_overwritten(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        category = Category(value="Investing")
        session.add(category)
        session.flush()
        out = _txn(
            session,
            currency,
            accounts["Checking"],
            1,
            -50000,
            "Xfer To",
            category_id=category.id,
            category_source="manual",
        )
        into = _txn(session, currency, accounts["Savings"], 2, 50000, "Xfer From")
        assert transfers.detect_transfers(session) == 1
        session.commit()
        # Both legs are paired, but the hand-picked category survives.
        assert out.transfer_group_id == into.transfer_group_id
        assert out.category.value == "Investing"
        assert into.category.value == "Transfer"


def test_clear_transfers_undoes_detection(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        _txn(session, currency, accounts["Checking"], 1, -50000)
        _txn(session, currency, accounts["Savings"], 2, 50000)
        transfers.detect_transfers(session)
        session.commit()

    with session_factory() as session:
        assert transfers.clear_transfers(session) == 2
        session.commit()

    with session_factory() as session:
        totals = queries.get_totals(session)
        assert totals.transfer_count == 0
        assert totals.outflow_minor == -50000  # counted again
        assert all(t.category == "" for t in queries.get_transactions(session))


def test_clear_transfers_leaves_manual_categories_alone(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        category = Category(value="Investing")
        session.add(category)
        session.flush()
        _txn(
            session,
            currency,
            accounts["Checking"],
            1,
            -50000,
            "Xfer To",
            category_id=category.id,
            category_source="manual",
        )
        _txn(session, currency, accounts["Savings"], 2, 50000, "Xfer From")
        transfers.detect_transfers(session)
        transfers.clear_transfers(session)
        session.commit()

    with session_factory() as session:
        txns = {t.description: t for t in queries.get_transactions(session)}
    assert txns["Xfer To"].category == "Investing"  # untouched throughout
    assert txns["Xfer From"].category == ""  # cleared with the pairing
