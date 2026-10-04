"""Tests for the sync window's late-posting fix.

``_sync_one_connection`` used to filter a remote account's transactions to the sync
window on ``t.transacted or t.posted`` -- the *purchase* date. A charge that sits
pending for a while posts well after it was made; by the time it posts, a few syncs
may have already pushed ``plan.start`` (anchored on ``synced_through``) past its
purchase date, so the old filter dropped it forever, on every sync from then on,
having never been inserted while it was still pending to catch. The fix filters on
``t.posted`` instead -- the same date SimpleFIN's own ``start-date`` filter uses -- so
a late poster is still in the window on the sync right after it posts.

``TODAY`` is fixed throughout so nothing here depends on the real clock.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from budget_tracker import sync
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.importer import import_csv
from budget_tracker.models import SyncAccount, SyncConnection, Transaction
from budget_tracker.simplefin import AccountSet, RemoteAccount, RemoteTransaction
from helpers import learn_format

TODAY = date(2026, 10, 3)

AMEX_CSV = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2026-08-20,2026-08-21,1008,OLD SHOP,Shopping,5.00,
"""
AMEX_CUTOFF = date(2026, 8, 21)


def _db(tmp_path):
    engine = get_engine(tmp_path / "t.db")
    init_db(engine)
    return get_sessionmaker(engine)


def _import_amex(session, tmp_path):
    path = tmp_path / "amex.csv"
    path.write_text(AMEX_CSV, encoding="utf-8")
    learn_format(session, path, name="amex_window_test")
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


def _rtxn(remote_id, posted, amount, description="MERCHANT", pending=False, transacted=None):
    return RemoteTransaction(
        id=remote_id,
        posted=posted,
        amount=Decimal(amount),
        description=description,
        pending=pending,
        transacted=transacted,
    )


def _fetch_returning(account_set: AccountSet):
    calls = []

    def fetch(access_url, *, start=None, end=None, account_ids=(), balances_only=False, timeout=60):
        calls.append({"start": start, "end": end})
        return account_set

    fetch.calls = calls
    return fetch


def _loader(secrets):
    return lambda name: secrets.get(name)


def _setup_amex_sync(session, tmp_path):
    _import_amex(session, tmp_path)
    session.add(SyncConnection(name="simplefin"))
    session.commit()
    return sync.map_account(session, "simplefin", _racct(), "Amex 1008")


SECRETS = {"simplefin": "https://u:p@host/simplefin"}


def _run(session, fetch, *, today=TODAY, dry_run=False):
    return sync.run_sync(
        session,
        "simplefin",
        dry_run=dry_run,
        today=today,
        load_secret=_loader(SECRETS),
        fetch=fetch,
    )


def test_late_posting_charge_is_inserted_on_the_sync_after_it_posts(tmp_path):
    """Purchased Sep 19, pending through several syncs, posts Oct 3.

    By the time it posts, earlier syncs (driven by unrelated same-day activity) have
    pushed synced_through well past Sep 19 -- so this is exactly the case where
    filtering on the purchase date would drop it forever.
    """
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _setup_amex_sync(session, tmp_path)
        session.commit()

    purchase_date = date(2026, 9, 19)

    # Sync 1 (while the charge is still pending): some unrelated same-day activity
    # advances synced_through well past the purchase date, and the pending charge
    # itself is seen but skipped, not dropped.
    pending_charge = _rtxn(
        "rt-late", date(1970, 1, 1), "-88.00", "LATE SHOP", pending=True
    )
    everyday = _rtxn("rt-everyday", date(2026, 9, 25), "-3.00", "COFFEE")
    with session_factory() as session:
        _run(
            session,
            _fetch_returning(
                AccountSet(
                    accounts=(
                        _racct(txns=[pending_charge, everyday]),
                    )
                )
            ),
        )

    with session_factory() as session:
        mapping = session.scalar(select(SyncAccount))
        assert mapping.synced_through == date(2026, 9, 25)

    # Sync 2: still pending, more days pass, synced_through advances further --
    # this is what pushes plan.start past the Sep 19 purchase date.
    pending_charge_2 = _rtxn(
        "rt-late", date(1970, 1, 1), "-88.00", "LATE SHOP", pending=True
    )
    later_everyday = _rtxn("rt-everyday-2", date(2026, 10, 1), "-4.00", "COFFEE")
    with session_factory() as session:
        _run(
            session,
            _fetch_returning(
                AccountSet(
                    accounts=(
                        _racct(txns=[pending_charge_2, later_everyday]),
                    )
                )
            ),
        )

    with session_factory() as session:
        mapping = session.scalar(select(SyncAccount))
        assert mapping.synced_through == date(2026, 10, 1)
        # Confirm the window for the next sync would start after the purchase date --
        # the exact condition that used to drop it.
        assert mapping.synced_through - timedelta(days=sync.OVERLAP_DAYS) > purchase_date

    # Sync 3: the charge has now posted. SimpleFIN includes it because its *posted*
    # date (Oct 3) is inside the server's own posted-date window, even though its
    # purchase date (Sep 19) is not.
    posted_charge = _rtxn(
        "rt-late", date(2026, 10, 3), "-88.00", "LATE SHOP", transacted=purchase_date
    )
    with session_factory() as session:
        results = _run(session, _fetch_returning(AccountSet(accounts=(_racct(txns=[posted_charge]),))))

    account_result = results[0].accounts[0]
    assert account_result.inserted == 1

    with session_factory() as session:
        inserted = session.scalar(
            select(Transaction).where(Transaction.description == "LATE SHOP")
        )
        assert inserted is not None
        # The stored date is still the purchase date, not the posted date -- unchanged
        # by this fix.
        assert inserted.posted_date == purchase_date


def test_late_posting_charge_is_not_inserted_twice_on_the_next_sync(tmp_path):
    """Once the Sep 19 / posts-Oct-3 charge has been inserted, a later sync that sees
    it again (now a routine, already-posted row within the window) must not duplicate
    it -- dedup is by the provider's transaction id via import_hash, unaffected by
    which date the window filter uses.
    """
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _setup_amex_sync(session, tmp_path)
        session.commit()

    purchase_date = date(2026, 9, 19)
    posted_charge = _rtxn(
        "rt-late", date(2026, 10, 3), "-88.00", "LATE SHOP", transacted=purchase_date
    )
    with session_factory() as session:
        first = _run(session, _fetch_returning(AccountSet(accounts=(_racct(txns=[posted_charge]),))))
    assert first[0].accounts[0].inserted == 1

    # Next sync: the same now-posted row comes back again (well within the window,
    # since its posted date is recent).
    with session_factory() as session:
        second = _run(session, _fetch_returning(AccountSet(accounts=(_racct(txns=[posted_charge]),))))
    account_result = second[0].accounts[0]
    assert account_result.inserted == 0
    assert account_result.already_synced == 1

    with session_factory() as session:
        rows = session.scalars(
            select(Transaction).where(Transaction.description == "LATE SHOP")
        ).all()
        assert len(rows) == 1
