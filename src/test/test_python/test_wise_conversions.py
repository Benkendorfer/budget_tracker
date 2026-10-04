"""Tests for pairing currency-conversion legs (transfers.pair_conversions)."""

from datetime import date

from budget_tracker import categories, queries, transfers
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.models import Account, Currency, Transaction


def _session_factory(tmp_path):
    engine = get_engine(tmp_path / "t.db")
    init_db(engine)
    return get_sessionmaker(engine)


def _seed(session):
    usd = Currency(value="USD", symbol="$", decimal_places=2)
    chf = Currency(value="CHF", symbol="CHF", decimal_places=2)
    session.add_all([usd, chf])
    session.flush()
    usd_account = Account(name="Wise USD", currency_id=usd.id)
    chf_account = Account(name="Wise CHF", currency_id=chf.id)
    session.add_all([usd_account, chf_account])
    session.flush()
    return usd, chf, usd_account, chf_account


def _txn(session, currency, account, day, amount, description, **kwargs):
    txn = Transaction(
        account_id=account.id,
        currency_id=currency.id,
        posted_date=date(2026, 8, day),
        description=description,
        raw_description=description,
        value_minor=amount,
        import_hash=f"{account.id}-{day}-{amount}-{description}",
        **kwargs,
    )
    session.add(txn)
    session.flush()
    return txn


def test_wise_conversion_pairs_despite_fee(tmp_path):
    """Real-shaped rows: source leg is TO-amount-minus-fee, fee is its own row."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        usd, chf, usd_account, chf_account = _seed(session)
        outflow = _txn(
            session,
            usd,
            usd_account,
            12,
            -299162,
            "Converted 3,000.00 USD to 2,431.14 CHF (fee: 8.38 USD)",
        )
        fee = _txn(
            session,
            usd,
            usd_account,
            12,
            -838,
            "Wise Charges for: BALANCE-5859192942",
        )
        inflow = _txn(
            session, chf, chf_account, 12, 243114, "Converted 3,000.00 USD to 2,431.14 CHF"
        )

        assert transfers.detect_transfers(session) == 1
        session.commit()

        assert outflow.transfer_group_id == inflow.transfer_group_id
        assert outflow.transfer_group_id is not None
        # The fee is a real expense and must stay out of the transfer entirely.
        assert fee.transfer_group_id is None
        outflow_id, inflow_id, fee_id = outflow.id, inflow.id, fee.id

    with session_factory() as session:
        txns = {t.id: t for t in queries.get_transactions(session)}
        assert txns[outflow_id].category == "Transfer"
        assert txns[inflow_id].category == "Transfer"
        assert txns[fee_id].category == ""


def test_equal_amount_different_currency_still_not_paired_generically(tmp_path):
    """Plain (non-conversion) rows of equal size in different currencies never pair."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        usd, chf, usd_account, chf_account = _seed(session)
        out = _txn(session, usd, usd_account, 1, -200000, "Unrelated outflow")
        into = _txn(session, chf, chf_account, 2, 200000, "Unrelated inflow")

        assert transfers.detect_transfers(session) == 0
        session.commit()
        assert out.transfer_group_id is None
        assert into.transfer_group_id is None


def test_mismatched_amounts_dont_pair(tmp_path):
    """Two unrelated conversions whose parsed amounts differ never pair."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        usd, chf, usd_account, chf_account = _seed(session)
        out = _txn(
            session, usd, usd_account, 1, -10000, "Converted 100.00 USD to 90.00 CHF"
        )
        into = _txn(
            session, chf, chf_account, 1, 9500, "Converted 100.00 USD to 95.00 CHF"
        )

        assert transfers.pair_conversions(session) == 0
        session.commit()
        assert out.transfer_group_id is None
        assert into.transfer_group_id is None


def test_manual_category_is_preserved(tmp_path):
    """A category the user set by hand outranks the Transfer category, but pairing
    (transfer_group_id) still happens so the totals exclude it."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        usd, chf, usd_account, chf_account = _seed(session)
        groceries = categories.get_or_create(session, "Groceries")
        session.flush()
        out = _txn(
            session,
            usd,
            usd_account,
            12,
            -299162,
            "Converted 3,000.00 USD to 2,431.14 CHF (fee: 8.38 USD)",
            category_id=groceries.id,
            category_source=categories.MANUAL,
        )
        into = _txn(
            session, chf, chf_account, 12, 243114, "Converted 3,000.00 USD to 2,431.14 CHF"
        )

        assert transfers.pair_conversions(session) == 1
        session.commit()

        assert out.transfer_group_id == into.transfer_group_id
        assert out.category_id == groceries.id
        assert out.category_source == categories.MANUAL
        assert into.category.value == "Transfer"


def test_idempotent_rerun(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        usd, chf, usd_account, chf_account = _seed(session)
        _txn(
            session,
            usd,
            usd_account,
            12,
            -299162,
            "Converted 3,000.00 USD to 2,431.14 CHF (fee: 8.38 USD)",
        )
        _txn(
            session, chf, chf_account, 12, 243114, "Converted 3,000.00 USD to 2,431.14 CHF"
        )

        assert transfers.detect_transfers(session) == 1
        session.commit()

    with session_factory() as session:
        assert transfers.detect_transfers(session) == 0
        session.commit()


def test_reset_then_detect_restores(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        usd, chf, usd_account, chf_account = _seed(session)
        out = _txn(
            session,
            usd,
            usd_account,
            12,
            -299162,
            "Converted 3,000.00 USD to 2,431.14 CHF (fee: 8.38 USD)",
        )
        into = _txn(
            session, chf, chf_account, 12, 243114, "Converted 3,000.00 USD to 2,431.14 CHF"
        )

        assert transfers.detect_transfers(session) == 1
        session.commit()
        group_id_before = out.transfer_group_id
        out_id, into_id = out.id, into.id

    with session_factory() as session:
        assert transfers.clear_transfers(session) == 2
        session.commit()
        restored_out = session.get(Transaction, out_id)
        assert restored_out.transfer_group_id is None
        assert restored_out.category_source == "unset"

    with session_factory() as session:
        assert transfers.detect_transfers(session) == 1
        session.commit()
        restored_out = session.get(Transaction, out_id)
        restored_into = session.get(Transaction, into_id)
        assert restored_out.transfer_group_id == restored_into.transfer_group_id
        assert restored_out.transfer_group_id == group_id_before


def test_same_amount_different_dates_pairs_with_closest(tmp_path):
    """Two conversions with identical parsed amounts resolve to the closest-dated legs."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        usd, chf, usd_account, chf_account = _seed(session)
        # Both outflows sit within the 1-day window of the single inflow (delta 0 and
        # 1); the farther one must lose, not just whichever is seen first.
        out_near = _txn(
            session,
            usd,
            usd_account,
            13,
            -10000,
            "Converted 100.00 USD to 90.00 CHF",
        )
        out_far = _txn(
            session,
            usd,
            usd_account,
            14,
            -10000,
            "Converted 100.00 USD to 90.00 CHF",
        )
        into = _txn(
            session, chf, chf_account, 13, 9000, "Converted 100.00 USD to 90.00 CHF"
        )

        assert transfers.pair_conversions(session) == 1
        session.commit()

        assert out_near.transfer_group_id == into.transfer_group_id
        assert out_far.transfer_group_id is None
