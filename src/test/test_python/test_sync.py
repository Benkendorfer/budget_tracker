"""Tests for :mod:`budget_tracker.sync`.

The real scenario this is built against: an "Amex 1008" account holding CSV-imported
transactions through 2026-08-01, with the first SimpleFIN sync happening on
2026-10-03 -- a ~2-month gap, comfortably inside the 90-day window but requiring the
overlap match to not duplicate what the CSV already recorded. ``TODAY`` is fixed so
nothing here depends on the real clock.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
import sqlalchemy
from sqlalchemy import select, text

from budget_tracker import sync
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.formats import AccountCurrencyMismatch
from budget_tracker.importer import delete_import, import_csv
from budget_tracker.models import (
    Account,
    Currency,
    Import,
    SyncAccount,
    SyncConnection,
    Transaction,
)
from budget_tracker.simplefin import AccountSet, AuthFailed, RemoteAccount, RemoteError, RemoteTransaction
from helpers import learn_format

TODAY = date(2026, 10, 3)
FLOOR_START = TODAY - timedelta(days=89)  # the earliest day a fetch can ever reach

AMEX_CSV = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2026-07-21,2026-07-22,1008,AplPay TRADER JOE S BROOKLYN            NY,Groceries,42.17,
2026-07-23,2026-07-24,1008,COFFEE SHOP,Dining,4.50,
2026-07-28,2026-07-29,1008,GYM MEMBERSHIP,Health,30.00,
2026-08-01,2026-08-02,1008,AplPay TRADER JOE S BROOKLYN            NY,Groceries,38.00,
"""
AMEX_CUTOFF = date(2026, 8, 2)  # max Posted Date above


# ----------------------------------------------------------------------- fixtures


def _db(tmp_path):
    engine = get_engine(tmp_path / "t.db")
    init_db(engine)
    return get_sessionmaker(engine)


def _connection(session, name="simplefin") -> SyncConnection:
    connection = SyncConnection(name=name)
    session.add(connection)
    session.commit()
    return connection


def _import_amex(session, tmp_path, csv_text=AMEX_CSV, filename="amex.csv"):
    path = tmp_path / filename
    path.write_text(csv_text, encoding="utf-8")
    learn_format(session, path, name="amex_test_layout")
    return import_csv(session, path, account_name="Amex 1008")


def _racct(remote_id="r-amex", currency="USD", txns=(), name="Amex Card"):
    return RemoteAccount(
        id=remote_id,
        name=name,
        conn_id="c1",
        currency=currency,
        balance=Decimal("0"),
        transactions=tuple(txns),
    )


def _rtxn(remote_id, day, amount, description="MERCHANT", pending=False, transacted=None):
    return RemoteTransaction(
        id=remote_id,
        posted=day,
        amount=Decimal(amount),
        description=description,
        pending=pending,
        transacted=transacted,
    )


def _fetch_returning(account_set: AccountSet):
    """A fake ``fetch`` that always returns ``account_set``, recording every call."""
    calls = []

    def fetch(access_url, *, start=None, end=None, account_ids=(), balances_only=False, timeout=60):
        calls.append(
            {
                "access_url": access_url,
                "start": start,
                "end": end,
                "account_ids": list(account_ids),
                "balances_only": balances_only,
            }
        )
        return account_set

    fetch.calls = calls
    return fetch


def _fetch_raising(exc):
    def fetch(access_url, **kwargs):
        raise exc

    return fetch


def _loader(secrets):
    return lambda name: secrets.get(name)


def _deleter(secrets):
    return lambda name: secrets.pop(name, None) is not None


def _map(session, connection_name, remote, local_name):
    return sync.map_account(session, connection_name, remote, local_name)


# ------------------------------------------------------------------ connect/mapping


def test_connect_refuses_before_claiming_when_connection_row_exists(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _connection(session, "simplefin")

    def claim(token, **kwargs):
        raise AssertionError("claim must not be called when the name is already taken")

    with session_factory() as session:
        with pytest.raises(sync.AlreadyConnected):
            sync.connect(
                session,
                "token",
                "simplefin",
                claim=claim,
                store_secret=lambda n, s: None,
                load_secret=lambda n: None,
            )


def test_connect_refuses_before_claiming_when_secret_exists(tmp_path):
    session_factory = _db(tmp_path)

    def claim(token, **kwargs):
        raise AssertionError("claim must not be called when a secret already exists")

    with session_factory() as session:
        with pytest.raises(sync.AlreadyConnected):
            sync.connect(
                session,
                "token",
                "simplefin",
                claim=claim,
                store_secret=lambda n, s: None,
                load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            )


def test_connect_stores_secret_even_if_the_later_fetch_fails(tmp_path):
    session_factory = _db(tmp_path)
    stored = {}

    def claim(token, **kwargs):
        return "https://u:p@host/simplefin"

    def store_secret(name, secret):
        stored[name] = secret

    def fetch(access_url, **kwargs):
        raise RuntimeError("network is down")

    with session_factory() as session:
        with pytest.raises(RuntimeError):
            sync.connect(
                session,
                "token",
                "simplefin",
                claim=claim,
                store_secret=store_secret,
                load_secret=lambda n: None,
                fetch=fetch,
            )

    assert stored == {"simplefin": "https://u:p@host/simplefin"}
    with session_factory() as session:
        # Claim -> store -> create the row -> commit happens before fetch, so the
        # connection row survives even though fetch blew up.
        assert session.scalar(
            select(SyncConnection).where(SyncConnection.name == "simplefin")
        )


def test_connect_returns_the_fetched_account_set(tmp_path):
    session_factory = _db(tmp_path)
    remote = _racct()
    account_set = AccountSet(accounts=(remote,))

    with session_factory() as session:
        result = sync.connect(
            session,
            "token",
            claim=lambda token, **kwargs: "https://u:p@host/simplefin",
            store_secret=lambda n, s: None,
            load_secret=lambda n: None,
            fetch=_fetch_returning(account_set),
        )
    assert result.accounts == (remote,)


def test_map_account_creates_local_account(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _connection(session, "simplefin")
        remote = _racct(remote_id="r1", currency="USD")
        mapping = _map(session, "simplefin", remote, "Amex 1008")
        assert mapping.account_id is not None
        account = session.get(Account, mapping.account_id)
        assert account.name == "Amex 1008"
        currency = session.get(Currency, account.currency_id)
        assert currency.value == "USD"


def test_map_account_currency_mismatch_refused(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        eur = Currency(value="EUR", decimal_places=2)
        session.add(eur)
        session.flush()
        session.add(Account(name="Checking", currency_id=eur.id))
        session.commit()

    with session_factory() as session:
        _connection(session, "simplefin")
        remote = _racct(remote_id="r1", currency="USD")
        with pytest.raises(AccountCurrencyMismatch):
            _map(session, "simplefin", remote, "Checking")


def test_map_account_url_currency_refused(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _connection(session, "simplefin")
        remote = _racct(remote_id="r1", currency="https://example.com/custom-currency")
        with pytest.raises(sync.SyncError):
            _map(session, "simplefin", remote, "Weird")


def test_remapping_an_already_mapped_remote_id_updates_it(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _connection(session, "simplefin")
        remote = _racct(remote_id="r1", currency="USD", name="Old Name")
        _map(session, "simplefin", remote, "Amex 1008")

        renamed = _racct(remote_id="r1", currency="USD", name="New Name")
        mapping = _map(session, "simplefin", renamed, "Amex 1008")
        assert mapping.remote_name == "New Name"
        assert (
            session.scalar(
                select(sqlalchemy.func.count()).select_from(SyncAccount)
            )
            == 1
        )


def test_mapping_an_account_already_mapped_to_a_different_remote_id_is_refused(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _connection(session, "simplefin")
        _map(session, "simplefin", _racct(remote_id="r1", currency="USD"), "Amex 1008")
        with pytest.raises(sync.SyncError):
            _map(session, "simplefin", _racct(remote_id="r2", currency="USD"), "Amex 1008")


def test_unmap_account(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _connection(session, "simplefin")
        _map(session, "simplefin", _racct(remote_id="r1", currency="USD"), "Amex 1008")
        assert sync.unmap_account(session, "simplefin", "r1") is True
        assert sync.unmap_account(session, "simplefin", "r1") is False
        assert session.scalar(select(SyncAccount)) is None


def test_unknown_connection_raises(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        with pytest.raises(sync.UnknownConnection):
            sync.unmap_account(session, "nope", "r1")


# ---------------------------------------------------------------------------- sync


def _setup_amex_sync(session, tmp_path, connection_name="simplefin", csv_text=AMEX_CSV):
    _import_amex(session, tmp_path, csv_text)
    _connection(session, connection_name)
    mapping = _map(
        session, connection_name, _racct(remote_id="r-amex", currency="USD"), "Amex 1008"
    )
    return mapping


def test_missing_credentials_raises(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _setup_amex_sync(session, tmp_path)
        session.commit()

    with session_factory() as session:
        with pytest.raises(sync.MissingCredentials):
            sync.run_sync(
                session,
                "simplefin",
                today=TODAY,
                load_secret=lambda n: None,
                fetch=_fetch_returning(AccountSet()),
            )


def test_auth_failed_is_reraised_as_sync_error(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _setup_amex_sync(session, tmp_path)
        session.commit()

    with session_factory() as session:
        with pytest.raises(sync.SyncError):
            sync.run_sync(
                session,
                "simplefin",
                today=TODAY,
                load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
                fetch=_fetch_raising(AuthFailed("revoked")),
            )


def test_first_sync_starts_from_the_csv_cutoff(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _setup_amex_sync(session, tmp_path)
        session.commit()

    # Matches the first CSV row exactly, so coverage back to the cutoff is confirmed
    # and this test is only about where ``start`` lands, not the coverage warnings.
    confirming = _rtxn("rt-existing", date(2026, 7, 22), "-42.17", "AplPay TRADER JOE S")
    fetch = _fetch_returning(
        AccountSet(accounts=(_racct(remote_id="r-amex", currency="USD", txns=[confirming]),))
    )
    with session_factory() as session:
        results = sync.run_sync(
            session,
            "simplefin",
            dry_run=True,
            today=TODAY,
            load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            fetch=fetch,
        )
    expected_start = AMEX_CUTOFF - timedelta(days=sync.OVERLAP_DAYS)
    assert fetch.calls[0]["start"] == expected_start
    assert fetch.calls[0]["account_ids"] == ["r-amex"]
    assert results[0].accounts[0].start == expected_start
    assert results[0].accounts[0].gap_warning is False


def test_later_sync_starts_from_synced_through(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _setup_amex_sync(session, tmp_path)
        session.commit()

    # One non-pending, non-matching transaction so synced_through actually advances.
    first_txn_date = AMEX_CUTOFF + timedelta(days=3)
    first_fetch = _fetch_returning(
        AccountSet(
            accounts=(
                _racct(
                    remote_id="r-amex",
                    currency="USD",
                    txns=[_rtxn("t1", first_txn_date, "-9.99", "NEW SHOP")],
                ),
            )
        )
    )
    with session_factory() as session:
        sync.run_sync(
            session,
            "simplefin",
            today=TODAY,
            load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            fetch=first_fetch,
        )

    with session_factory() as session:
        mapping = session.scalar(select(SyncAccount))
        assert mapping.synced_through == first_txn_date
        assert mapping.last_synced_at is not None

    second_fetch = _fetch_returning(AccountSet(accounts=(_racct(remote_id="r-amex", currency="USD"),)))
    with session_factory() as session:
        sync.run_sync(
            session,
            "simplefin",
            dry_run=True,
            today=TODAY,
            load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            fetch=second_fetch,
        )
    assert second_fetch.calls[0]["start"] == first_txn_date - timedelta(days=sync.OVERLAP_DAYS)


def test_overlap_match_prefers_the_closer_csv_row(tmp_path):
    """A remote row with two same-amount CSV candidates claims the closer one."""
    csv_text = (
        "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\n"
        "2026-07-21,2026-07-22,1008,SHOP,Shopping,10.00,\n"
        "2026-07-24,2026-07-25,1008,SHOP,Shopping,10.00,\n"
    )
    session_factory = _db(tmp_path)
    with session_factory() as session:
        mapping = _setup_amex_sync(session, tmp_path, csv_text=csv_text)
        session.commit()
        closer_id = session.scalar(
            select(Transaction.id).where(Transaction.posted_date == date(2026, 7, 22))
        )

    fetch = _fetch_returning(
        AccountSet(
            accounts=(
                _racct(
                    remote_id="r-amex",
                    currency="USD",
                    txns=[_rtxn("rt1", date(2026, 7, 23), "-10.00", "SHOP")],
                ),
            )
        )
    )
    with session_factory() as session:
        results = sync.run_sync(
            session,
            "simplefin",
            dry_run=True,
            today=TODAY,
            load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            fetch=fetch,
        )
    account_result = results[0].accounts[0]
    assert (account_result.matched_existing, account_result.inserted) == (1, 0)


def test_overlap_match_does_not_double_claim_one_csv_row(tmp_path):
    """Two identical remote rows must not both claim the single matching CSV row."""
    csv_text = (
        "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\n"
        "2026-07-21,2026-07-22,1008,SHOP,Shopping,10.00,\n"
    )
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _setup_amex_sync(session, tmp_path, csv_text=csv_text)
        session.commit()

    fetch = _fetch_returning(
        AccountSet(
            accounts=(
                _racct(
                    remote_id="r-amex",
                    currency="USD",
                    txns=[
                        _rtxn("rt1", date(2026, 7, 22), "-10.00", "SHOP"),
                        _rtxn("rt2", date(2026, 7, 23), "-10.00", "SHOP"),
                    ],
                ),
            )
        )
    )
    with session_factory() as session:
        results = sync.run_sync(
            session,
            "simplefin",
            today=TODAY,
            load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            fetch=fetch,
        )
    account_result = results[0].accounts[0]
    # One claims the CSV row; the other, finding it already taken, is a new row.
    assert (account_result.matched_existing, account_result.inserted) == (1, 1)


def test_rerunning_sync_is_idempotent(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _setup_amex_sync(session, tmp_path)
        session.commit()

    new_txn = _rtxn("rt-new", AMEX_CUTOFF + timedelta(days=2), "-9.99", "NEW SHOP")
    account_set = AccountSet(accounts=(_racct(remote_id="r-amex", currency="USD", txns=[new_txn]),))

    with session_factory() as session:
        first = sync.run_sync(
            session,
            "simplefin",
            today=TODAY,
            load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            fetch=_fetch_returning(account_set),
        )
    assert first[0].accounts[0].inserted == 1
    assert first[0].accounts[0].already_synced == 0

    with session_factory() as session:
        second = sync.run_sync(
            session,
            "simplefin",
            today=TODAY,
            load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            fetch=_fetch_returning(account_set),
        )
    assert second[0].accounts[0].inserted == 0
    assert second[0].accounts[0].already_synced == 1


def test_pending_transactions_are_skipped_and_counted(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _setup_amex_sync(session, tmp_path)
        session.commit()

    # Dated the way the protocol actually sends a pending row: posted = 0, the epoch.
    # A realistic date here once hid that the window filter dropped these uncounted.
    pending_txn = _rtxn(
        "rt-pending", date(1970, 1, 1), "-5.00", "PENDING SHOP", pending=True
    )
    account_set = AccountSet(
        accounts=(_racct(remote_id="r-amex", currency="USD", txns=[pending_txn]),)
    )
    with session_factory() as session:
        results = sync.run_sync(
            session,
            "simplefin",
            today=TODAY,
            load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            fetch=_fetch_returning(account_set),
        )
    account_result = results[0].accounts[0]
    assert account_result.skipped_pending == 1
    assert account_result.fetched == 0
    assert account_result.inserted == 0


def test_inserted_before_cutoff_is_counted(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _setup_amex_sync(session, tmp_path)
        session.commit()

    # Dated inside the CSV's own range, but a distinct amount -- never matches.
    txn = _rtxn("rt1", date(2026, 7, 25), "-77.77", "ODD SHOP")
    account_set = AccountSet(accounts=(_racct(remote_id="r-amex", currency="USD", txns=[txn]),))
    with session_factory() as session:
        results = sync.run_sync(
            session,
            "simplefin",
            today=TODAY,
            load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            fetch=_fetch_returning(account_set),
        )
    account_result = results[0].accounts[0]
    assert (account_result.inserted, account_result.inserted_before_cutoff) == (1, 1)


def test_sign_inversion_warns_in_dry_run_and_raises_in_a_real_run(tmp_path):
    csv_text = (
        "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\n"
        "2026-07-21,2026-07-22,1008,SHOP A,Shopping,10.00,\n"
        "2026-07-22,2026-07-23,1008,SHOP B,Shopping,20.00,\n"
        "2026-07-23,2026-07-24,1008,SHOP C,Shopping,30.00,\n"
    )
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _setup_amex_sync(session, tmp_path, csv_text=csv_text)
        session.commit()

    # Same dates, positive amounts -- the inverse of what the CSV holds.
    inverted = [
        _rtxn("rt1", date(2026, 7, 22), "10.00", "SHOP A"),
        _rtxn("rt2", date(2026, 7, 23), "20.00", "SHOP B"),
        _rtxn("rt3", date(2026, 7, 24), "30.00", "SHOP C"),
    ]
    account_set = AccountSet(accounts=(_racct(remote_id="r-amex", currency="USD", txns=inverted),))

    with session_factory() as session:
        results = sync.run_sync(
            session,
            "simplefin",
            dry_run=True,
            today=TODAY,
            load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            fetch=_fetch_returning(account_set),
        )
    warnings = results[0].accounts[0].warnings
    assert any("sign" in w.lower() for w in warnings)

    with session_factory() as session:
        before_imports = session.scalar(select(sqlalchemy.func.count()).select_from(Import))
        with pytest.raises(sync.SyncError, match="sign"):
            sync.run_sync(
                session,
                "simplefin",
                today=TODAY,
                load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
                fetch=_fetch_returning(account_set),
            )
        after_imports = session.scalar(select(sqlalchemy.func.count()).select_from(Import))
        assert before_imports == after_imports  # nothing written
        mapping = session.scalar(select(SyncAccount))
        assert mapping.synced_through is None


def test_dry_run_writes_nothing_but_reports_the_same_counts(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _setup_amex_sync(session, tmp_path)
        session.commit()

    with session_factory() as session:
        # Baseline: one Import row already exists, from the CSV itself.
        baseline_imports = session.scalar(select(sqlalchemy.func.count()).select_from(Import))
    assert baseline_imports == 1

    # Confirms coverage back to the cutoff, same as test_first_sync_starts_from_the_
    # csv_cutoff, so the only difference between the dry and real run is the write.
    confirming = _rtxn("rt-existing", date(2026, 7, 22), "-42.17", "AplPay TRADER JOE S")
    new_txn = _rtxn("rt1", AMEX_CUTOFF + timedelta(days=1), "-12.34", "NEW SHOP")
    account_set = AccountSet(
        accounts=(_racct(remote_id="r-amex", currency="USD", txns=[confirming, new_txn]),)
    )

    with session_factory() as session:
        dry = sync.run_sync(
            session,
            "simplefin",
            dry_run=True,
            today=TODAY,
            load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            fetch=_fetch_returning(account_set),
        )[0]

    with session_factory() as session:
        assert (
            session.scalar(select(sqlalchemy.func.count()).select_from(Import))
            == baseline_imports
        )
        mapping = session.scalar(select(SyncAccount))
        assert mapping.synced_through is None
        assert mapping.last_synced_at is None

    with session_factory() as session:
        real = sync.run_sync(
            session,
            "simplefin",
            today=TODAY,
            load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            fetch=_fetch_returning(account_set),
        )[0]

    assert dry.accounts[0].inserted == real.accounts[0].inserted == 1
    assert dry.accounts[0].already_synced == real.accounts[0].already_synced
    assert dry.import_id is None
    assert real.import_id is not None

    with session_factory() as session:
        assert (
            session.scalar(select(sqlalchemy.func.count()).select_from(Import))
            == baseline_imports + 1
        )
        mapping = session.scalar(select(SyncAccount))
        assert mapping.synced_through == AMEX_CUTOFF + timedelta(days=1)


def test_empty_run_leaves_no_import_row(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _setup_amex_sync(session, tmp_path)
        session.commit()

    with session_factory() as session:
        baseline_imports = session.scalar(select(sqlalchemy.func.count()).select_from(Import))
    assert baseline_imports == 1  # the CSV's own Import row

    account_set = AccountSet(accounts=(_racct(remote_id="r-amex", currency="USD", txns=[]),))
    with session_factory() as session:
        results = sync.run_sync(
            session,
            "simplefin",
            today=TODAY,
            load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            fetch=_fetch_returning(account_set),
        )
    assert results[0].import_id is None
    with session_factory() as session:
        assert (
            session.scalar(select(sqlalchemy.func.count()).select_from(Import))
            == baseline_imports
        )


def test_unmapped_remote_accounts_are_reported(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _setup_amex_sync(session, tmp_path)
        session.commit()

    account_set = AccountSet(
        accounts=(
            _racct(remote_id="r-amex", currency="USD"),
            _racct(remote_id="r-other", currency="USD", name="Unmapped Checking"),
        )
    )
    with session_factory() as session:
        results = sync.run_sync(
            session,
            "simplefin",
            today=TODAY,
            load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            fetch=_fetch_returning(account_set),
        )
    assert results[0].unmapped == ["Unmapped Checking"]


def test_remote_errlist_is_surfaced(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _setup_amex_sync(session, tmp_path)
        session.commit()

    account_set = AccountSet(
        accounts=(_racct(remote_id="r-amex", currency="USD"),),
        errors=(RemoteError(code="bad-conn", msg="connection needs attention"),),
    )
    with session_factory() as session:
        results = sync.run_sync(
            session,
            "simplefin",
            today=TODAY,
            load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            fetch=_fetch_returning(account_set),
        )
    assert results[0].errors == ["bad-conn: connection needs attention"]


def test_currency_mismatch_warns_and_skips_only_that_account(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _setup_amex_sync(session, tmp_path)
        # A second mapped account, in EUR locally but reported as USD remotely.
        eur = Currency(value="EUR", decimal_places=2)
        session.add(eur)
        session.flush()
        checking = Account(name="Checking EUR", currency_id=eur.id)
        session.add(checking)
        session.flush()
        mapping = SyncAccount(
            connection_id=session.scalar(select(SyncConnection)).id,
            remote_id="r-checking",
            remote_name="Checking",
            account_id=checking.id,
        )
        session.add(mapping)
        session.commit()

    # Confirms coverage back to the cutoff for the Amex leg, so its warnings list is
    # empty for the reason this test cares about (not polluted by the separate
    # unconfirmed-coverage check, which is tested on its own elsewhere).
    confirming = _rtxn("rt-existing", date(2026, 7, 22), "-42.17", "AplPay TRADER JOE S")
    account_set = AccountSet(
        accounts=(
            _racct(remote_id="r-amex", currency="USD", txns=[confirming]),
            _racct(remote_id="r-checking", currency="USD", name="Checking"),
        )
    )
    with session_factory() as session:
        results = sync.run_sync(
            session,
            "simplefin",
            today=TODAY,
            load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            fetch=_fetch_returning(account_set),
        )
    by_name = {a.remote_name: a for a in results[0].accounts}
    assert any("skipped" in w for w in by_name["Checking"].warnings)
    # The other, unaffected account was still processed.
    assert by_name["Amex Card"].warnings == []


def test_vendor_and_category_rules_apply_to_synced_rows(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _setup_amex_sync(session, tmp_path)
        from budget_tracker import vendors

        vendors.add_rule(session, "WEIRD SHOP*", "Nice Shop")
        session.commit()

    txn = _rtxn("rt1", AMEX_CUTOFF + timedelta(days=1), "-12.34", "WEIRD SHOP#123")
    account_set = AccountSet(accounts=(_racct(remote_id="r-amex", currency="USD", txns=[txn]),))
    with session_factory() as session:
        sync.run_sync(
            session,
            "simplefin",
            today=TODAY,
            load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            fetch=_fetch_returning(account_set),
        )

    with session_factory() as session:
        inserted = session.scalar(
            select(Transaction).where(Transaction.description == "WEIRD SHOP#123")
        )
        assert inserted.vendor.display_name == "Nice Shop"


def test_transfer_detection_applies_to_synced_rows(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _setup_amex_sync(session, tmp_path)
        usd = session.scalar(select(Currency).where(Currency.value == "USD"))
        checking = Account(name="Checking", currency_id=usd.id)
        session.add(checking)
        session.flush()
        # The other leg of a transfer: money leaving Checking on the same day the
        # sync will insert the matching inflow on the card.
        session.add(
            Transaction(
                account_id=checking.id,
                currency_id=usd.id,
                posted_date=AMEX_CUTOFF + timedelta(days=1),
                description="PAYMENT TO CARD",
                raw_description="PAYMENT TO CARD",
                value_minor=-5000,
                import_hash="manual-transfer-leg",
            )
        )
        session.commit()

    txn = _rtxn("rt1", AMEX_CUTOFF + timedelta(days=1), "50.00", "PAYMENT RECEIVED")
    account_set = AccountSet(accounts=(_racct(remote_id="r-amex", currency="USD", txns=[txn]),))
    with session_factory() as session:
        sync.run_sync(
            session,
            "simplefin",
            today=TODAY,
            load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            fetch=_fetch_returning(account_set),
        )

    with session_factory() as session:
        inserted = session.scalar(
            select(Transaction).where(Transaction.description == "PAYMENT RECEIVED")
        )
        assert inserted.transfer_group_id is not None
        manual_leg = session.scalar(
            select(Transaction).where(Transaction.import_hash == "manual-transfer-leg")
        )
        assert manual_leg.transfer_group_id == inserted.transfer_group_id


# -------------------------------------------------------------- coverage warnings


def test_definite_gap_warning(tmp_path):
    """Anchor far enough in the past that part of the gap is permanently unreachable."""
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _connection(session, "simplefin")
        mapping = _map(
            session, "simplefin", _racct(remote_id="r1", currency="USD"), "Fresh Account"
        )
        mapping.synced_through = FLOOR_START - timedelta(days=5)
        session.commit()

    account_set = AccountSet(accounts=(_racct(remote_id="r1", currency="USD", txns=[]),))
    with session_factory() as session:
        results = sync.run_sync(
            session,
            "simplefin",
            dry_run=True,
            today=TODAY,
            load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            fetch=_fetch_returning(account_set),
        )
    account_result = results[0].accounts[0]
    assert account_result.gap_warning is True
    assert any("cannot be synced" in w for w in account_result.warnings)


def test_thin_overlap_warning(tmp_path):
    """Anchor close enough that the window reaches it, but with less than
    OVERLAP_DAYS of re-checked overlap."""
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _connection(session, "simplefin")
        mapping = _map(
            session, "simplefin", _racct(remote_id="r1", currency="USD"), "Fresh Account"
        )
        mapping.synced_through = FLOOR_START + timedelta(days=2)
        session.commit()

    # One transaction right at the anchor, so the separate unconfirmed-coverage
    # check (tested on its own elsewhere) does not also fire here.
    confirming = _rtxn("rt1", FLOOR_START + timedelta(days=2), "-5.00", "SHOP")
    account_set = AccountSet(
        accounts=(_racct(remote_id="r1", currency="USD", txns=[confirming]),)
    )
    with session_factory() as session:
        results = sync.run_sync(
            session,
            "simplefin",
            dry_run=True,
            today=TODAY,
            load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            fetch=_fetch_returning(account_set),
        )
    account_result = results[0].accounts[0]
    assert account_result.gap_warning is False
    assert any("2 day(s) of overlap" in w for w in account_result.warnings)


def test_unconfirmed_coverage_warning_fires_when_only_newer_rows_come_back(tmp_path):
    """The provider can return less history than asked for -- which must be told
    apart from a definite gap, since a quiet account can trigger this honestly."""
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _setup_amex_sync(session, tmp_path)
        session.commit()

    # Nothing anywhere near the CSV cutoff -- only a transaction well after it.
    txn = _rtxn("rt1", AMEX_CUTOFF + timedelta(days=20), "-9.99", "NEW SHOP")
    account_set = AccountSet(accounts=(_racct(remote_id="r-amex", currency="USD", txns=[txn]),))

    for dry_run in (True, False):
        with session_factory() as session:
            results = sync.run_sync(
                session,
                "simplefin",
                dry_run=dry_run,
                today=TODAY,
                load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
                fetch=_fetch_returning(account_set),
            )
        account_result = results[0].accounts[0]
        assert account_result.gap_warning is True
        assert any("could not confirm" in w.lower() for w in account_result.warnings)


def test_unconfirmed_coverage_warning_fires_when_nothing_at_all_comes_back(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _setup_amex_sync(session, tmp_path)
        session.commit()

    account_set = AccountSet(accounts=(_racct(remote_id="r-amex", currency="USD", txns=[]),))
    with session_factory() as session:
        results = sync.run_sync(
            session,
            "simplefin",
            dry_run=True,
            today=TODAY,
            load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            fetch=_fetch_returning(account_set),
        )
    account_result = results[0].accounts[0]
    assert account_result.gap_warning is True
    assert any("no transactions were returned" in w for w in account_result.warnings)


def test_fresh_account_with_no_anchor_gets_no_coverage_warnings(tmp_path):
    """No CSV history and no prior sync means there is no anchor, so none of the
    three coverage checks have anything to compare against."""
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _connection(session, "simplefin")
        _map(session, "simplefin", _racct(remote_id="r1", currency="USD"), "Fresh Account")
        session.commit()

    account_set = AccountSet(accounts=(_racct(remote_id="r1", currency="USD", txns=[]),))
    with session_factory() as session:
        results = sync.run_sync(
            session,
            "simplefin",
            dry_run=True,
            today=TODAY,
            load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            fetch=_fetch_returning(account_set),
        )
    account_result = results[0].accounts[0]
    assert account_result.warnings == []
    assert account_result.gap_warning is False
    assert account_result.start == FLOOR_START


# ------------------------------------------------------------- disconnect / unimport


def test_disconnect_keeps_transactions_and_import_rows(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _setup_amex_sync(session, tmp_path)
        session.commit()

    txn = _rtxn("rt1", AMEX_CUTOFF + timedelta(days=1), "-12.34", "NEW SHOP")
    account_set = AccountSet(accounts=(_racct(remote_id="r-amex", currency="USD", txns=[txn]),))
    with session_factory() as session:
        result = sync.run_sync(
            session,
            "simplefin",
            today=TODAY,
            load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            fetch=_fetch_returning(account_set),
        )[0]
    import_id = result.import_id
    assert import_id is not None

    secrets = {"simplefin": "https://u:p@host/simplefin"}
    with session_factory() as session:
        sync.disconnect(session, "simplefin", delete_secret=_deleter(secrets))

    assert secrets == {}
    with session_factory() as session:
        assert session.scalar(select(SyncConnection)) is None
        assert session.scalar(select(SyncAccount)) is None
        record = session.get(Import, import_id)
        assert record is not None
        assert record.sync_connection_id is None
        assert (
            session.scalar(select(Transaction).where(Transaction.description == "NEW SHOP"))
            is not None
        )


def test_unimporting_a_sync_import_works(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _setup_amex_sync(session, tmp_path)
        session.commit()

    txn = _rtxn("rt1", AMEX_CUTOFF + timedelta(days=1), "-12.34", "NEW SHOP")
    account_set = AccountSet(accounts=(_racct(remote_id="r-amex", currency="USD", txns=[txn]),))
    with session_factory() as session:
        result = sync.run_sync(
            session,
            "simplefin",
            today=TODAY,
            load_secret=_loader({"simplefin": "https://u:p@host/simplefin"}),
            fetch=_fetch_returning(account_set),
        )[0]

    with session_factory() as session:
        delete_result = delete_import(session, result.import_id)
        session.commit()
    assert delete_result.transactions_deleted == 1

    with session_factory() as session:
        assert session.get(Import, result.import_id) is None
        assert (
            session.scalar(select(Transaction).where(Transaction.description == "NEW SHOP"))
            is None
        )


def test_list_connections(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _setup_amex_sync(session, tmp_path)
        session.commit()

    with session_factory() as session:
        statuses = sync.list_connections(session)
    assert len(statuses) == 1
    assert statuses[0].name == "simplefin"
    assert statuses[0].accounts[0].local_name == "Amex 1008"
    assert statuses[0].accounts[0].remote_name == "Amex Card"


# ----------------------------------------------------------------- db migration


def test_init_db_adds_sync_connection_id_to_a_preexisting_import_table(tmp_path):
    """A database from before sync existed has an ``import`` table but no
    ``sync_connection``/``sync_account`` tables; init_db must cope with both at once,
    even though ``_add_missing_columns`` runs before ``create_all`` makes the table
    the new column's FK references.
    """
    engine = get_engine(tmp_path / "old.db")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE import (id INTEGER PRIMARY KEY, account_id INTEGER, "
                "source_file VARCHAR, row_count INTEGER, imported_at TIMESTAMP)"
            )
        )
        connection.execute(
            text("INSERT INTO import (source_file, row_count) VALUES ('old.csv', 3)")
        )

    init_db(engine)  # must not raise

    with engine.begin() as connection:
        columns = {row[1] for row in connection.execute(text("PRAGMA table_info(import)"))}
        assert "sync_connection_id" in columns
        tables = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
        assert {"sync_connection", "sync_account"} <= tables
        value = connection.execute(
            text("SELECT sync_connection_id FROM import WHERE source_file = 'old.csv'")
        ).scalar()
        assert value is None

    # Idempotent: a second init_db on the now-current schema must not raise either.
    init_db(engine)
