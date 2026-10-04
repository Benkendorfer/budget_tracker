"""`sort size`: the current view, largest amounts first, until the view changes."""

from __future__ import annotations

import asyncio

from textual.widgets import DataTable, Static

from budget_tracker import queries
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.importer import import_csv
from budget_tracker.tui import BudgetApp

from helpers import learn_format

# A refund larger than every charge but one, so "size" has to mean absolute value.
CSV = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2025-07-01,2025-07-01,8207,SMALL OLD,Misc,5.00,
2025-07-02,2025-07-02,8207,HUGE,Misc,900.00,
2025-07-03,2025-07-03,8207,BIG REFUND,Misc,,400.00
2025-07-04,2025-07-04,8207,MEDIUM,Travel,250.00,
2025-07-05,2025-07-05,8207,SMALL NEW,Misc,7.00,
"""


def _setup(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    engine = get_engine(db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    csv_path = tmp_path / "in.csv"
    csv_path.write_text(CSV, encoding="utf-8")
    with session_factory() as session:
        learn_format(session, csv_path)
        import_csv(session, csv_path)
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def test_size_order_is_by_absolute_amount(tmp_path, monkeypatch):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        by_size = queries.get_transactions(session, order=queries.ORDER_SIZE)
        by_date = queries.get_transactions(session)
    assert [t.description for t in by_size] == [
        "HUGE", "BIG REFUND", "MEDIUM", "SMALL NEW", "SMALL OLD",
    ]
    assert [t.description for t in by_date] == [
        "SMALL NEW", "MEDIUM", "BIG REFUND", "HUGE", "SMALL OLD",
    ]


def _descriptions(app):
    return [t.description for t in app._txns]


def _status(app):
    return str(app.query_one("#status", Static).content)


def test_sort_size_sorts_the_current_filter_and_says_so(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("filter SMALL")
            await pilot.pause()
            app._run_command("sort size")
            await pilot.pause()
            return _descriptions(app), _status(app)

    rows, status = asyncio.run(run())
    assert rows == ["SMALL NEW", "SMALL OLD"]  # still filtered, now by size
    assert "by size" in status


def test_an_edit_keeps_the_size_order(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("sort size")
            await pilot.pause()
            app._run_command("rule categorize MEDIUM = Misc")
            await pilot.pause()
            return _descriptions(app)

    assert asyncio.run(run())[0] == "HUGE"


def test_changing_the_filter_returns_to_date_order(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("sort size")
            await pilot.pause()
            app._run_command("filter S")
            await pilot.pause()
            return _descriptions(app), _status(app)

    rows, status = asyncio.run(run())
    assert rows == ["SMALL NEW", "SMALL OLD"]
    assert "by size" not in status


def test_leaving_the_transactions_returns_to_date_order(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("sort size")
            await pilot.pause()
            app._run_command("rules")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            return app._panel, _descriptions(app), _status(app)

    panel, rows, status = asyncio.run(run())
    assert panel == "txns"
    assert rows[0] == "SMALL NEW"
    assert "by size" not in status


def test_sort_date_and_bad_arguments(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("sort size")
            await pilot.pause()
            app._run_command("sort date")
            await pilot.pause()
            after_date = _descriptions(app)[0]
            app._run_command("sort sideways")
            await pilot.pause()
            return after_date, [n.message for n in app._notifications]

    first, messages = asyncio.run(run())
    assert first == "SMALL NEW"
    assert BudgetApp.SORT_USAGE in messages
