"""The transactions table keeps the user's place across an edit.

Every write ends in ``reload()``, which refills #txns; before this, the refill sent the
cursor and scroll back to row 0, so working down a long list meant scrolling back to
where you were after every rule or category.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

from sqlalchemy import select
from textual.widgets import DataTable

from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.importer import import_csv
from budget_tracker.models import Category
from budget_tracker.tui import BudgetApp

from helpers import learn_format

ROWS = 120


def _setup_long_list(tmp_path, monkeypatch):
    """ROWS transactions, one vendor each, so one rule touches exactly one row."""
    lines = ["Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit"]
    start = date(2025, 1, 1)
    for i in range(ROWS):
        day = (start + timedelta(days=i)).isoformat()
        lines.append(f"{day},{day},8207,SHOP {i:03d},Misc,{i + 1}.00,")
    csv_path = tmp_path / "long.csv"
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    db_path = tmp_path / "t.db"
    engine = get_engine(db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        learn_format(session, csv_path)
        import_csv(session, csv_path)
    monkeypatch.setenv("BUDGET_DB", str(db_path))


def _cursor_state(app):
    table = app.query_one("#txns", DataTable)
    row = table.cursor_row
    return app._txns[row].description, row, row - int(table.scroll_y)


def test_an_edit_keeps_the_cursor_on_the_same_transaction(tmp_path, monkeypatch):
    _setup_long_list(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            table = app.query_one("#txns", DataTable)
            table.focus()
            table.move_cursor(row=80)
            await pilot.pause()
            before = _cursor_state(app)
            app._run_command(f"rule categorize {before[0]} = Groceries")
            await pilot.pause()
            await pilot.pause()
            return before, _cursor_state(app), app._txns[table.cursor_row].category

    before, after, category = asyncio.run(run())
    assert before[1] == 80 and before[2] > 0  # scrolled, not at the top
    assert after == before  # same transaction, same row, same height on screen
    assert category == "Groceries"  # and the edit really happened


def test_a_row_that_leaves_the_view_hands_the_cursor_to_the_next_one(tmp_path, monkeypatch):
    _setup_long_list(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            with app.session_factory() as session:
                misc = session.scalar(select(Category).where(Category.value == "Misc"))
            app.category_filter = misc.id
            app.reload()
            await pilot.pause()
            table = app.query_one("#txns", DataTable)
            table.focus()
            table.move_cursor(row=50)
            await pilot.pause()
            leaving = app._txns[50].description
            next_down = app._txns[51].description
            # Re-categorized out of the Misc filter, so the row disappears.
            app._run_command(f"rule categorize {leaving} = Groceries")
            await pilot.pause()
            await pilot.pause()
            return leaving, next_down, table.cursor_row, app._txns[table.cursor_row].description

    leaving, next_down, row, description = asyncio.run(run())
    assert row == 50
    assert description == next_down != leaving


def test_a_new_filter_starts_at_the_top(tmp_path, monkeypatch):
    _setup_long_list(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            table = app.query_one("#txns", DataTable)
            table.focus()
            table.move_cursor(row=80)
            await pilot.pause()
            app._run_command("filter SHOP 1")
            await pilot.pause()
            return table.cursor_row, int(table.scroll_y)

    assert asyncio.run(run()) == (0, 0)
