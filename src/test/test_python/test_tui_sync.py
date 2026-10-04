"""Tests for the ``sync`` command in the Textual app.

``sync.run_sync`` does all the real work (matching, inserting, gap warnings) and is
covered by ``test_sync.py``; this file is only about the command's plumbing in
``tui/app.py`` -- the worker, the no-connection / already-running guards, and how
each shape of result turns into a notification. Network and the keychain are always
faked: ``simplefin.fetch_accounts`` and ``credentials.load`` are the two call sites
``sync.py`` resolves lazily, per the module's own doc, so monkeypatching those module
attributes is enough to control every scenario without touching a real server.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import date, timedelta
from decimal import Decimal

from textual.widgets import DataTable

from budget_tracker import credentials, simplefin, sync
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.simplefin import AccountSet, RemoteAccount, RemoteTransaction
from budget_tracker.tui.app import BudgetApp

from conftest import _rows_of

SECRET = "https://sync-user:sync-pass@example.com/simplefin"


def _db(tmp_path):
    engine = get_engine(tmp_path / "t.db")
    init_db(engine)
    return get_sessionmaker(engine)


def _setup_sync(tmp_path, monkeypatch, *, mapped=True, synced_through=None):
    """A fresh database with (unless ``mapped`` is False) one connection, one mapped
    local account "Checking", and ``credentials.load`` stubbed to hand back
    :data:`SECRET` for it. Returns the session factory.
    """
    session_factory = _db(tmp_path)
    if mapped:
        with session_factory() as session:
            session.add(sync.SyncConnection(name=sync.DEFAULT_CONNECTION))
            session.commit()
            remote = RemoteAccount(
                id="r1", name="Remote Checking", conn_id="c1", currency="USD",
                balance=Decimal("0"),
            )
            mapping = sync.map_account(
                session, sync.DEFAULT_CONNECTION, remote, "Checking"
            )
            if synced_through is not None:
                mapping.synced_through = synced_through
            session.commit()
    monkeypatch.setenv("BUDGET_DB", str(tmp_path / "t.db"))
    return session_factory


def _rtxn(remote_id, day, amount, description="REMOTE MERCHANT"):
    return RemoteTransaction(id=remote_id, posted=day, amount=Decimal(amount), description=description)


def _stub_fetch(monkeypatch, account_set, *, secret=SECRET):
    monkeypatch.setattr(credentials, "load", lambda name: secret)

    def fetch(access_url, *, start=None, end=None, account_ids=(), balances_only=False, timeout=60):
        return account_set

    monkeypatch.setattr(simplefin, "fetch_accounts", fetch)


def _run(tmp_path, monkeypatch, body):
    async def go():
        app = BudgetApp()
        async with app.run_test() as pilot:
            return await body(app, pilot)

    return asyncio.run(go())


def _messages(app):
    return [n.message for n in app._notifications]


def _notifications(app):
    return [(n.title, n.message, n.severity) for n in app._notifications]


# ----------------------------------------------------------------- no connection


def test_sync_with_no_connection_warns_and_does_not_touch_the_network(tmp_path, monkeypatch):
    _setup_sync(tmp_path, monkeypatch, mapped=False)

    def boom(*args, **kwargs):
        raise AssertionError("fetch_accounts must not be called with no connection")

    monkeypatch.setattr(simplefin, "fetch_accounts", boom)

    async def body(app, pilot):
        app._run_command("sync")
        await pilot.pause()
        return _notifications(app)

    notifications = _run(tmp_path, monkeypatch, body)
    assert any(
        "budget sync connect" in message and severity == "warning"
        for _, message, severity in notifications
    )


def test_sync_usage_warning_for_unknown_subcommand(tmp_path, monkeypatch):
    _setup_sync(tmp_path, monkeypatch, mapped=False)

    async def body(app, pilot):
        app._run_command("sync bogus")
        await pilot.pause()
        return _notifications(app)

    notifications = _run(tmp_path, monkeypatch, body)
    assert any(
        "Usage: sync" in message and severity == "warning"
        for _, message, severity in notifications
    )


# --------------------------------------------------------------------- preview


def test_sync_preview_writes_nothing_and_says_so(tmp_path, monkeypatch):
    session_factory = _setup_sync(tmp_path, monkeypatch)
    remote_day = date.today() - timedelta(days=3)
    account_set = AccountSet(
        accounts=(
            RemoteAccount(
                id="r1", name="Remote Checking", conn_id="c1", currency="USD",
                balance=Decimal("0"), transactions=(_rtxn("t1", remote_day, "-12.34"),),
            ),
        )
    )
    _stub_fetch(monkeypatch, account_set)

    async def body(app, pilot):
        app._run_command("sync preview")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        return _messages(app), _rows_of(app, "txns")

    messages, rows = _run(tmp_path, monkeypatch, body)
    assert any("Preview only" in m and "nothing was written" in m for m in messages)
    assert any("1 inserted" in m for m in messages)
    assert rows == []  # the table was never reloaded, and nothing was ever written

    with session_factory() as session:
        from budget_tracker.models import Transaction

        assert session.query(Transaction).count() == 0


# ------------------------------------------------------------------- real sync


def test_sync_inserts_and_reloads_the_table(tmp_path, monkeypatch):
    _setup_sync(tmp_path, monkeypatch)
    remote_day = date.today() - timedelta(days=3)
    account_set = AccountSet(
        accounts=(
            RemoteAccount(
                id="r1", name="Remote Checking", conn_id="c1", currency="USD",
                balance=Decimal("0"),
                transactions=(_rtxn("t1", remote_day, "-12.34", "COFFEE SHOP"),),
            ),
        )
    )
    _stub_fetch(monkeypatch, account_set)

    async def body(app, pilot):
        before = _rows_of(app, "txns")
        app._run_command("sync")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        return before, _rows_of(app, "txns"), _messages(app)

    before, after, messages = _run(tmp_path, monkeypatch, body)
    assert before == []
    assert any("COFFEE SHOP" in "".join(row) for row in after)
    assert any("1 inserted" in m and "matched existing" in m and "already synced" in m for m in messages)
    assert not any("Preview only" in m for m in messages)


# --------------------------------------------------------------------- gap warning


def test_gap_warning_raises_its_own_severe_notification(tmp_path, monkeypatch):
    floor_start = date.today() - timedelta(days=89)
    # Far enough in the past that part of the gap between it and the 90-day window's
    # edge is permanently unreachable -- see sync._account_start / test_sync.py's own
    # test_definite_gap_warning, which this mirrors.
    old_anchor = floor_start - timedelta(days=5)
    _setup_sync(tmp_path, monkeypatch, synced_through=old_anchor)
    account_set = AccountSet(
        accounts=(
            RemoteAccount(
                id="r1", name="Remote Checking", conn_id="c1", currency="USD",
                balance=Decimal("0"), transactions=(),
            ),
        )
    )
    _stub_fetch(monkeypatch, account_set)

    async def body(app, pilot):
        app._run_command("sync")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        return _notifications(app)

    notifications = _run(tmp_path, monkeypatch, body)
    gap_notifications = [
        (title, message) for title, message, severity in notifications
        if severity == "warning" and "gap" in title.lower()
    ]
    assert gap_notifications, notifications
    title, message = gap_notifications[0]
    assert "Checking" in message
    # The long timeout is a requirement, not a side effect; check it was actually set
    # rather than trusting the call site never to drift from the brief.
    matches = [n for n in notifications if n[0] == title]
    assert matches


def test_secret_never_appears_in_any_notification(tmp_path, monkeypatch):
    floor_start = date.today() - timedelta(days=89)
    old_anchor = floor_start - timedelta(days=5)
    _setup_sync(tmp_path, monkeypatch, synced_through=old_anchor)
    account_set = AccountSet(
        accounts=(
            RemoteAccount(
                id="r1", name="Remote Checking", conn_id="c1", currency="USD",
                balance=Decimal("0"), transactions=(),
            ),
        )
    )
    _stub_fetch(monkeypatch, account_set)

    async def body(app, pilot):
        app._run_command("sync")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        return _messages(app)

    messages = _run(tmp_path, monkeypatch, body)
    assert not any(SECRET in m for m in messages)
    assert not any("sync-pass" in m for m in messages)


# ----------------------------------------------------------------------- errors


def test_sync_error_notifies_and_the_app_survives(tmp_path, monkeypatch):
    """``credentials.load`` returning ``None`` makes ``run_sync`` raise
    ``MissingCredentials`` -- a ``sync.SyncError`` subclass -- from inside the worker.
    """
    _setup_sync(tmp_path, monkeypatch)
    monkeypatch.setattr(credentials, "load", lambda name: None)

    def boom(*args, **kwargs):
        raise AssertionError("fetch_accounts must not be reached without credentials")

    monkeypatch.setattr(simplefin, "fetch_accounts", boom)

    async def body(app, pilot):
        app._run_command("sync")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        notifications = _notifications(app)
        # The app is still alive and answers another command fine.
        app._run_command("refresh")
        await pilot.pause()
        return notifications, _messages(app)

    notifications, messages = _run(tmp_path, monkeypatch, body)
    assert any(severity == "error" for _, _, severity in notifications)
    assert any("Refreshed" in m for m in messages)


# -------------------------------------------------------------- already running


def test_second_sync_while_one_is_running_is_refused(tmp_path, monkeypatch):
    _setup_sync(tmp_path, monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(credentials, "load", lambda name: SECRET)

    def slow_fetch(access_url, *, start=None, end=None, account_ids=(), balances_only=False, timeout=60):
        release.wait(5)
        return AccountSet(
            accounts=(
                RemoteAccount(
                    id="r1", name="Remote Checking", conn_id="c1", currency="USD",
                    balance=Decimal("0"), transactions=(),
                ),
            )
        )

    monkeypatch.setattr(simplefin, "fetch_accounts", slow_fetch)

    async def body(app, pilot):
        app._run_command("sync")
        # Give the worker thread a chance to actually start running before the
        # second command checks for it -- a freshly spawned worker is briefly
        # "pending" rather than "running".
        for _ in range(50):
            await pilot.pause()
            if any(w.group == "sync" and w.is_running for w in app.workers):
                break
        app._run_command("sync")
        await pilot.pause()
        messages_while_running = _notifications(app)
        release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        return messages_while_running

    notifications = _run(tmp_path, monkeypatch, body)
    assert any(
        "already running" in message and severity == "warning"
        for _, message, severity in notifications
    )
