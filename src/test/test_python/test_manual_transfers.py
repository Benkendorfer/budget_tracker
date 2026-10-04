"""Tests for pairing a fee-bearing transfer (e.g. Wise) by hand."""

from datetime import date

import pytest
from sqlalchemy import select

from budget_tracker import categories, importer, queries, tags, transfers
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.models import Account, Currency, Import, Transaction, Vendor


def _session_factory(tmp_path):
    engine = get_engine(tmp_path / "t.db")
    init_db(engine)
    return get_sessionmaker(engine)


def _seed(session):
    usd = Currency(value="USD", symbol="$", decimal_places=2)
    eur = Currency(value="EUR", symbol="€", decimal_places=2)
    session.add_all([usd, eur])
    session.flush()
    accounts = {
        "Checking": Account(name="Checking", currency_id=usd.id),
        "Wise USD": Account(name="Wise USD", currency_id=usd.id),
        "Wise EUR": Account(name="Wise EUR", currency_id=eur.id),
    }
    session.add_all(accounts.values())
    session.flush()
    return {"USD": usd, "EUR": eur}, accounts


def _txn(session, currency, account, day, amount, description="X", **kwargs):
    txn = Transaction(
        account_id=account.id,
        currency_id=currency.id,
        posted_date=date(2026, 7, day),
        description=description,
        raw_description=description,
        value_minor=amount,
        import_hash=kwargs.pop(
            "import_hash", f"{account.id}-{day}-{amount}-{description}"
        ),
        **kwargs,
    )
    session.add(txn)
    session.flush()
    return txn


def _account_id(session, name):
    return session.scalar(select(Account.id).where(Account.name == name))


def _currency_id(session, code):
    return session.scalar(select(Currency.id).where(Currency.value == code))


# ----------------------------------------------------------------- same-currency split


def test_fee_is_split_off_the_outflow_and_stays_spending(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currencies, accounts = _seed(session)
        out = _txn(
            session, currencies["USD"], accounts["Checking"], 1, -100000, "To Wise"
        )
        into = _txn(
            session, currencies["USD"], accounts["Wise USD"], 1, 99500, "From Checking"
        )
        out_id, into_id, out_hash = out.id, into.id, out.import_hash
        result = transfers.mark_manual_transfer(session, [out_id, into_id])
        session.commit()

        assert result.outflow_minor == -100000
        assert result.inflow_minor == 99500
        assert result.difference_minor == -500
        assert result.currency == "USD"
        assert result.outflow_account == "Checking"
        assert result.inflow_account == "Wise USD"
        assert result.group_id == min(out_id, into_id)
        fee_id = result.fee_txn_id
        assert fee_id is not None
        assert result.fee_account == "Checking"

    with session_factory() as session:
        out = session.get(Transaction, out_id)
        into = session.get(Transaction, into_id)
        fee = session.get(Transaction, fee_id)

        # The transfer legs now cancel exactly.
        assert out.value_minor == -99500
        assert into.value_minor == 99500
        assert out.value_minor + fee.value_minor == -100000  # leg + fee == original
        assert out.transfer_group_id == into.transfer_group_id
        assert out.category.value == "Transfer"
        assert into.category.value == "Transfer"
        assert out.category_source == transfers.MANUAL_TRANSFER_SOURCE
        assert into.category_source == transfers.MANUAL_TRANSFER_SOURCE

        # The fee is an ordinary, un-grouped spending row.
        assert fee.transfer_group_id is None
        assert fee.value_minor == -500
        assert fee.description == "Transfer fee"
        assert fee.raw_description == "Transfer fee"
        assert fee.account_id == _account_id(session, "Checking")
        assert fee.currency_id == _currency_id(session, "USD")
        assert fee.category.value == "Fees"
        assert fee.category_source == "manual"
        assert fee.vendor.name == "Transfer fee"
        assert fee.import_hash == f"{out_hash}:fee"
        assert fee.posted_date == out.posted_date

        totals = queries.get_totals(session)
        assert totals.count == 3
        assert totals.transfer_count == 2
        assert totals.outflow_minor == -500  # just the fee
        assert totals.inflow_minor == 0


def test_zero_difference_needs_no_fee_row(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currencies, accounts = _seed(session)
        out = _txn(session, currencies["USD"], accounts["Checking"], 1, -50000, "Out")
        into = _txn(session, currencies["USD"], accounts["Wise USD"], 1, 50000, "In")
        out_id, into_id = out.id, into.id
        result = transfers.mark_manual_transfer(session, [out_id, into_id])
        session.commit()

        assert result.difference_minor == 0
        assert result.fee_txn_id is None
        assert result.fee_account is None

    with session_factory() as session:
        out = session.get(Transaction, out_id)
        into = session.get(Transaction, into_id)
        assert out.value_minor == -50000  # untouched
        assert into.value_minor == 50000
        assert session.query(Transaction).count() == 2  # no fee row created


def test_positive_difference_splits_off_the_inflow(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currencies, accounts = _seed(session)
        out = _txn(session, currencies["USD"], accounts["Checking"], 1, -100000, "Out")
        into = _txn(
            session, currencies["USD"], accounts["Wise USD"], 1, 100500, "Received"
        )
        out_id, into_id, into_hash = out.id, into.id, into.import_hash
        result = transfers.mark_manual_transfer(session, [out_id, into_id])
        session.commit()

        assert result.difference_minor == 500
        fee_id = result.fee_txn_id
        assert result.fee_account == "Wise USD"

    with session_factory() as session:
        out = session.get(Transaction, out_id)
        into = session.get(Transaction, into_id)
        fee = session.get(Transaction, fee_id)

        assert out.value_minor == -100000  # untouched; the inflow absorbed it
        assert into.value_minor == 100000
        assert into.value_minor + fee.value_minor == 100500
        assert fee.value_minor == 500
        assert fee.description == "Transfer difference"
        assert fee.category.value == "Fees"
        assert fee.account_id == into.account_id
        assert fee.import_hash == f"{into_hash}:fee"


# -------------------------------------------------------------------- cross-currency


def test_different_currencies_pair_with_no_split(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currencies, accounts = _seed(session)
        out = _txn(session, currencies["USD"], accounts["Checking"], 1, -100000, "Out")
        into = _txn(session, currencies["EUR"], accounts["Wise EUR"], 1, 90000, "In")
        out_id, into_id = out.id, into.id
        result = transfers.mark_manual_transfer(session, [out_id, into_id])
        session.commit()

        assert result.difference_minor is None
        assert result.fee_txn_id is None
        assert result.currency == "USD"

    with session_factory() as session:
        out = session.get(Transaction, out_id)
        into = session.get(Transaction, into_id)
        assert out.value_minor == -100000  # neither leg touched
        assert into.value_minor == 90000
        assert out.transfer_group_id == into.transfer_group_id
        assert out.category.value == "Transfer"
        assert session.query(Transaction).count() == 2  # no fee row


# -------------------------------------------------------------------------- validation


def test_requires_exactly_two_transactions(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currencies, accounts = _seed(session)
        out = _txn(session, currencies["USD"], accounts["Checking"], 1, -50000)
        into = _txn(session, currencies["USD"], accounts["Wise USD"], 1, 49500)
        extra = _txn(session, currencies["USD"], accounts["Checking"], 2, -100, "Extra")

        with pytest.raises(transfers.ManualTransferError):
            transfers.mark_manual_transfer(session, [out.id])
        with pytest.raises(transfers.ManualTransferError):
            transfers.mark_manual_transfer(session, [out.id, into.id, extra.id])


def test_requires_opposite_signs(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currencies, accounts = _seed(session)
        a = _txn(session, currencies["USD"], accounts["Checking"], 1, -50000, "A")
        b = _txn(session, currencies["USD"], accounts["Wise USD"], 1, -49500, "B")
        with pytest.raises(transfers.ManualTransferError):
            transfers.mark_manual_transfer(session, [a.id, b.id])


def test_refuses_an_already_grouped_transaction(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currencies, accounts = _seed(session)
        out = _txn(session, currencies["USD"], accounts["Checking"], 1, -50000, "Out")
        into = _txn(session, currencies["USD"], accounts["Wise USD"], 1, 50000, "In")
        extra = _txn(session, currencies["USD"], accounts["Checking"], 2, -2500, "Z")
        transfers.detect_transfers(session)  # pairs out/into, leaves extra alone
        session.flush()
        assert out.transfer_group_id is not None

        with pytest.raises(transfers.ManualTransferError):
            transfers.mark_manual_transfer(session, [out.id, extra.id])


# ------------------------------------------------------------------- tags and rules


def test_tag_is_applied_to_both_legs(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currencies, accounts = _seed(session)
        out = _txn(session, currencies["USD"], accounts["Checking"], 1, -100000, "Out")
        into = _txn(session, currencies["USD"], accounts["Wise USD"], 1, 99500, "In")
        out_id, into_id = out.id, into.id
        transfers.mark_manual_transfer(session, [out_id, into_id])
        session.commit()

    with session_factory() as session:
        tag_map = tags.tags_for(session, [out_id, into_id])
        assert [t.name for t in tag_map[out_id]] == [transfers.MANUAL_TRANSFER_TAG]
        assert [t.name for t in tag_map[into_id]] == [transfers.MANUAL_TRANSFER_TAG]


def test_category_rules_do_not_overwrite_a_manual_transfer_leg(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currencies, accounts = _seed(session)
        out = _txn(session, currencies["USD"], accounts["Checking"], 1, -100000, "Out")
        into = _txn(session, currencies["USD"], accounts["Wise USD"], 1, 99500, "In")
        vendor = Vendor(name="WISE TRANSFER")
        session.add(vendor)
        session.flush()
        out.vendor_id = vendor.id
        into.vendor_id = vendor.id
        out_id, into_id = out.id, into.id
        transfers.mark_manual_transfer(session, [out_id, into_id])
        categories.add_rule(session, "WISE TRANSFER", "Shopping")
        categories.apply_category_rules(session)
        session.commit()

    with session_factory() as session:
        out = session.get(Transaction, out_id)
        into = session.get(Transaction, into_id)
        assert out.category.value == "Transfer"
        assert into.category.value == "Transfer"
        assert out.category_source == transfers.MANUAL_TRANSFER_SOURCE
        assert into.category_source == transfers.MANUAL_TRANSFER_SOURCE


# --------------------------------------------------------------- clear_transfers / detect


def test_clear_transfers_leaves_manual_transfers_alone(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currencies, accounts = _seed(session)
        # An ordinary detected pair, which clear_transfers must still undo.
        detected_out = _txn(
            session, currencies["USD"], accounts["Checking"], 1, -20000, "Detected Out"
        )
        _txn(
            session, currencies["USD"], accounts["Wise USD"], 1, 20000, "Detected In"
        )
        transfers.detect_transfers(session)

        manual_out = _txn(
            session, currencies["USD"], accounts["Checking"], 2, -100000, "Manual Out"
        )
        manual_in = _txn(
            session, currencies["USD"], accounts["Wise USD"], 2, 99500, "Manual In"
        )
        detected_out_id = detected_out.id
        manual_out_id, manual_in_id = manual_out.id, manual_in.id
        transfers.mark_manual_transfer(session, [manual_out_id, manual_in_id])
        session.commit()

    with session_factory() as session:
        cleared = transfers.clear_transfers(session)
        session.commit()
    assert cleared == 2  # only the detected pair

    with session_factory() as session:
        detected_out = session.get(Transaction, detected_out_id)
        manual_out = session.get(Transaction, manual_out_id)
        manual_in = session.get(Transaction, manual_in_id)
        assert detected_out.transfer_group_id is None  # undone
        assert manual_out.transfer_group_id == manual_in.transfer_group_id
        assert manual_out.category.value == "Transfer"
        assert manual_out.category_source == transfers.MANUAL_TRANSFER_SOURCE


def test_detect_transfers_does_not_touch_a_manual_pair(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currencies, accounts = _seed(session)
        out = _txn(session, currencies["USD"], accounts["Checking"], 1, -50000, "Out")
        into = _txn(session, currencies["USD"], accounts["Wise USD"], 1, 50000, "In")
        out_id, into_id = out.id, into.id
        result = transfers.mark_manual_transfer(session, [out_id, into_id])
        session.commit()
        group_id = result.group_id

    with session_factory() as session:
        # Equal amounts would otherwise be exactly what detect_transfers looks for.
        assert transfers.detect_transfers(session) == 0
        out = session.get(Transaction, out_id)
        assert out.transfer_group_id == group_id
        assert out.category_source == transfers.MANUAL_TRANSFER_SOURCE


# ------------------------------------------------------------------------------ undo


def test_unmark_restores_amounts_category_and_tag(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currencies, accounts = _seed(session)
        out = _txn(session, currencies["USD"], accounts["Checking"], 1, -100000, "Out")
        into = _txn(session, currencies["USD"], accounts["Wise USD"], 1, 99500, "In")
        out_id, into_id = out.id, into.id
        transfers.mark_manual_transfer(session, [out_id, into_id])
        session.commit()

    with session_factory() as session:
        restored = transfers.unmark_manual_transfer(session, [out_id, into_id])
        session.commit()
    assert restored == 2

    with session_factory() as session:
        out = session.get(Transaction, out_id)
        into = session.get(Transaction, into_id)
        assert out.value_minor == -100000  # fee folded back in
        assert into.value_minor == 99500
        assert out.transfer_group_id is None
        assert into.transfer_group_id is None
        assert out.category_id is None
        assert out.category_source == "unset"
        assert into.category_id is None
        assert into.category_source == "unset"
        assert session.query(Transaction).count() == 2  # the fee row is gone

        tag_map = tags.tags_for(session, [out_id, into_id])
        assert tag_map[out_id] == []
        assert tag_map[into_id] == []


def test_unmark_given_one_leg_restores_both(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currencies, accounts = _seed(session)
        out = _txn(session, currencies["USD"], accounts["Checking"], 1, -100000, "Out")
        into = _txn(session, currencies["USD"], accounts["Wise USD"], 1, 99500, "In")
        out_id, into_id = out.id, into.id
        transfers.mark_manual_transfer(session, [out_id, into_id])
        session.commit()

    with session_factory() as session:
        restored = transfers.unmark_manual_transfer(session, [into_id])
        session.commit()
    assert restored == 2

    with session_factory() as session:
        out = session.get(Transaction, out_id)
        into = session.get(Transaction, into_id)
        assert out.value_minor == -100000
        assert into.value_minor == 99500
        assert out.transfer_group_id is None
        assert into.transfer_group_id is None


def test_unmark_ignores_rows_that_are_not_manual_transfer_legs(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currencies, accounts = _seed(session)
        out = _txn(session, currencies["USD"], accounts["Checking"], 1, -50000, "Out")
        into = _txn(session, currencies["USD"], accounts["Wise USD"], 1, 50000, "In")
        out_id, into_id = out.id, into.id
        transfers.detect_transfers(session)  # ordinary pair, not a manual one
        session.commit()

    with session_factory() as session:
        assert transfers.unmark_manual_transfer(session, [out_id, into_id]) == 0


# ---------------------------------------------------------------------------- unimport


def test_unimport_removes_the_fee_row_with_its_leg(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currencies, accounts = _seed(session)
        checking_import = Import(
            account_id=accounts["Checking"].id, source_file="checking.csv"
        )
        wise_import = Import(account_id=accounts["Wise USD"].id, source_file="wise.csv")
        session.add_all([checking_import, wise_import])
        session.flush()

        out = _txn(
            session, currencies["USD"], accounts["Checking"], 1, -100000, "Out",
            import_id=checking_import.id,
        )
        into = _txn(
            session, currencies["USD"], accounts["Wise USD"], 1, 99500, "In",
            import_id=wise_import.id,
        )
        out_id, into_id = out.id, into.id
        result = transfers.mark_manual_transfer(session, [out_id, into_id])
        fee_id = result.fee_txn_id
        assert fee_id is not None
        session.commit()
        checking_import_id = checking_import.id

    with session_factory() as session:
        delete_result = importer.delete_import(session, checking_import_id)
        session.commit()

    # The fee shares the outflow leg's import_id, so deleting that import takes the
    # outflow and the fee together.
    assert delete_result.transactions_deleted == 2

    with session_factory() as session:
        assert session.get(Transaction, out_id) is None
        assert session.get(Transaction, fee_id) is None
        survivor = session.get(Transaction, into_id)
        assert survivor is not None
        assert survivor.transfer_group_id is None  # its partner is gone
        # ...and so it is no longer labeled a transfer either.
        assert survivor.category_id is None
        assert survivor.category_source == "unset"
