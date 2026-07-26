"""Tests for renaming and merging accounts."""

from datetime import date

import pytest

from budget_tracker import accounts, queries, transfers
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.models import Account, Currency, Import, Transaction


def _session_factory(tmp_path):
    engine = get_engine(tmp_path / "t.db")
    init_db(engine)
    return get_sessionmaker(engine)


def _seed(session, names=("Old Card", "New Card", "Checking")):
    usd = Currency(value="USD", symbol="$", decimal_places=2)
    session.add(usd)
    session.flush()
    made = {}
    for name in names:
        account = Account(name=name, currency_id=usd.id)
        session.add(account)
        made[name] = account
    session.flush()
    return usd, made


def _txn(session, currency, account, day, amount, description="X"):
    txn = Transaction(
        account_id=account.id,
        currency_id=currency.id,
        posted_date=date(2026, 7, day),
        description=description,
        raw_description=description,
        value_minor=amount,
        import_hash=f"{account.id}-{day}-{amount}-{description}",
    )
    session.add(txn)
    session.flush()
    return txn


def test_merge_moves_transactions_and_deletes_the_source(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, made = _seed(session)
        _txn(session, currency, made["Old Card"], 1, -1000, "A")
        _txn(session, currency, made["Old Card"], 2, -2000, "B")
        _txn(session, currency, made["New Card"], 3, -3000, "C")
        session.commit()

        result = accounts.merge_accounts(session, "Old Card", "New Card")
        session.commit()
    assert result.moved_transactions == 2

    with session_factory() as session:
        rows = {a.name: a.count for a in queries.get_accounts(session)}
    assert "Old Card" not in rows  # the source is gone
    assert rows["New Card"] == 3  # everything landed in one place


def test_merge_moves_import_records(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _, made = _seed(session)
        session.add(Import(account_id=made["Old Card"].id, source_file="x.csv"))
        session.flush()
        result = accounts.merge_accounts(session, "Old Card", "New Card")
        session.commit()
    assert result.moved_imports == 1

    with session_factory() as session:
        records = list(session.scalars(__import__("sqlalchemy").select(Import)))
        assert [r.account_id for r in records] == [
            session.scalar(
                __import__("sqlalchemy").select(Account.id).where(
                    Account.name == "New Card"
                )
            )
        ]


def test_merge_unpairs_transfers_that_are_now_internal(tmp_path):
    """A pair spanning the two merged accounts is no longer a transfer afterwards."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, made = _seed(session)
        _txn(session, currency, made["Old Card"], 1, -50000, "Out")
        _txn(session, currency, made["New Card"], 2, 50000, "In")
        assert transfers.detect_transfers(session) == 1
        session.commit()

        totals = queries.get_totals(session)
        assert totals.transfer_count == 2  # excluded while the accounts are separate

        result = accounts.merge_accounts(session, "Old Card", "New Card")
        session.commit()
    assert result.unpaired_transfers == 2

    with session_factory() as session:
        totals = queries.get_totals(session)
        # Both rows count again, and the Transfer category is gone from them.
        assert totals.transfer_count == 0
        assert totals.outflow_minor == -50000
        assert totals.inflow_minor == 50000
        assert all(t.category == "" for t in queries.get_transactions(session))


def test_merge_keeps_transfers_that_still_span_accounts(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, made = _seed(session)
        _txn(session, currency, made["Old Card"], 1, -50000, "Out")
        _txn(session, currency, made["Checking"], 2, 50000, "In")
        transfers.detect_transfers(session)
        session.commit()

        result = accounts.merge_accounts(session, "Old Card", "New Card")
        session.commit()
    assert result.unpaired_transfers == 0

    with session_factory() as session:
        assert queries.get_totals(session).transfer_count == 2  # still excluded


def test_merge_refuses_mismatched_currencies(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _, made = _seed(session)
        eur = Currency(value="EUR", symbol="€", decimal_places=2)
        session.add(eur)
        session.flush()
        made["Old Card"].currency_id = eur.id
        session.flush()
        with pytest.raises(accounts.AccountError) as excinfo:
            accounts.merge_accounts(session, "Old Card", "New Card")
    assert "different currencies" in str(excinfo.value)


def test_merge_refuses_unknown_or_identical_accounts(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed(session)
        with pytest.raises(accounts.AccountError) as missing:
            accounts.merge_accounts(session, "Nope", "New Card")
        assert "No account named" in str(missing.value)
        assert "New Card" in str(missing.value)  # the error lists what does exist
        with pytest.raises(accounts.AccountError):
            accounts.merge_accounts(session, "New Card", "New Card")


def test_rename_account(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, made = _seed(session)
        _txn(session, currency, made["Old Card"], 1, -1000)
        accounts.rename_account(session, "Old Card", "Everyday Card")
        session.commit()

    with session_factory() as session:
        rows = {a.name: a.count for a in queries.get_accounts(session)}
    assert rows["Everyday Card"] == 1
    assert "Old Card" not in rows


def test_rename_onto_an_existing_name_is_refused(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed(session)
        with pytest.raises(accounts.AccountError) as excinfo:
            accounts.rename_account(session, "Old Card", "New Card")
    assert "merge them instead" in str(excinfo.value)
