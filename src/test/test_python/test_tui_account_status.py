"""Tests for coloring the Accounts sidebar by each account's last sync status.

``queries.get_accounts`` already carries ``sync_status``/``sync_error`` from the
account's ``SyncAccount`` mapping (another agent's sync.py work records those); this
file is only about how ``tui/app.py`` turns that into the sidebar's ``.sync-ok`` /
``.sync-error`` classes and the failing account's tooltip. The sync worker itself
(the ``sync``/``sync preview`` commands) is covered by ``test_tui_sync.py``.
"""

from __future__ import annotations

import asyncio

from textual.widgets import ListView

from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.models import SYNC_ERROR, SYNC_OK, Account, Currency, SyncAccount, SyncConnection
from budget_tracker.tui.app import BudgetApp


def _setup_accounts(tmp_path, monkeypatch):
    """Three accounts, alphabetically "Card 1234", "Checking", "Savings":

    - "Card 1234" synced and last failed, with an error message.
    - "Checking" synced and last succeeded.
    - "Savings" is not synced at all (no ``SyncAccount`` row).

    Alphabetical order is what ``queries.get_accounts`` sorts by, so the sidebar rows
    land in that same order: index 0 is "— All —", 1 "Card 1234", 2 "Checking",
    3 "Savings".
    """
    db_path = tmp_path / "t.db"
    engine = get_engine(db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        currency = Currency(value="USD", symbol="$", decimal_places=2)
        session.add(currency)
        session.flush()
        card = Account(name="Card 1234", currency_id=currency.id)
        checking = Account(name="Checking", currency_id=currency.id)
        savings = Account(name="Savings", currency_id=currency.id)
        session.add_all([card, checking, savings])
        session.flush()
        connection = SyncConnection(name="conn1")
        session.add(connection)
        session.flush()
        session.add_all(
            [
                SyncAccount(
                    connection_id=connection.id,
                    remote_id="r-card",
                    remote_name="Remote Card",
                    account_id=card.id,
                    last_status=SYNC_ERROR,
                    last_error="401 Unauthorized",
                ),
                SyncAccount(
                    connection_id=connection.id,
                    remote_id="r-checking",
                    remote_name="Remote Checking",
                    account_id=checking.id,
                    last_status=SYNC_OK,
                    last_error=None,
                ),
            ]
        )
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def _account_item_classes(app):
    """The CSS classes of every real row in #accounts, "— All —" excluded."""
    items = app.query_one("#accounts", ListView).children[1:]
    return [
        next((c for c in item.classes if c.startswith("sync-")), None) for item in items
    ]


def test_accounts_colored_by_last_sync_status(tmp_path, monkeypatch):
    _setup_accounts(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test():
            return _account_item_classes(app)

    # Card 1234: error (red) / Checking: ok (green) / Savings: never synced (plain).
    assert asyncio.run(run()) == ["sync-error", "sync-ok", None]


def test_all_row_is_never_colored(tmp_path, monkeypatch):
    _setup_accounts(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test():
            all_row = app.query_one("#accounts", ListView).children[0]
            return [c for c in all_row.classes if c.startswith("sync-")]

    assert asyncio.run(run()) == []


def test_failing_account_tooltip_names_the_error(tmp_path, monkeypatch):
    _setup_accounts(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test():
            items = app.query_one("#accounts", ListView).children
            # index 1 == "Card 1234", the failing account -- see _setup_accounts.
            return items[1].tooltip, items[2].tooltip, items[3].tooltip

    card_tooltip, checking_tooltip, savings_tooltip = asyncio.run(run())
    assert card_tooltip == "401 Unauthorized"
    assert checking_tooltip is None
    assert savings_tooltip is None


def test_status_change_rerenders_even_with_an_unchanged_label(tmp_path, monkeypatch):
    """A status flip with the same name and the same transaction count still has to
    rebuild the list -- _fill_list's memo key has to fold the status in, not just the
    label text (see tui/app.py's _fill_list docstring)."""
    session_factory = _setup_accounts(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            before = _account_item_classes(app)
            with session_factory() as session:
                mapping = session.query(SyncAccount).filter_by(remote_id="r-card").one()
                mapping.last_status = SYNC_OK
                mapping.last_error = None
                session.commit()
            app.reload()
            await pilot.pause()
            after = _account_item_classes(app)
            return before, after

    before, after = asyncio.run(run())
    assert before == ["sync-error", "sync-ok", None]
    assert after == ["sync-ok", "sync-ok", None]


def test_clicking_an_account_still_filters_with_styling_in_place(tmp_path, monkeypatch):
    """Row indices are unchanged by the coloring -- selecting row 1 ("Card 1234")
    still sets account_filter to that account's id, exactly as it would unstyled."""
    _setup_accounts(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            accounts_list = app.query_one("#accounts", ListView)
            accounts_list.focus()
            accounts_list.index = 1  # "Card 1234"
            await pilot.press("enter")
            await pilot.pause()
            card = next(a for a in app._accounts if a.name == "Card 1234")
            return app.account_filter, card.id

    account_filter, card_id = asyncio.run(run())
    assert account_filter == card_id
