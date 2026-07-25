"""Tests for the TUI's rename shortcut (ctrl+n).

Driven through Textual's ``run_test`` pilot. ``asyncio.run`` wraps each case so the
suite does not need pytest-asyncio.
"""

import asyncio

from textual.widgets import DataTable, Input, ListView

from budget_tracker import vendors
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.importer import import_csv
from budget_tracker.tui import BudgetApp

CSV = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2025-07-01,2025-07-02,8207,COFFEE SHOP A,Dining,3.00,
2025-07-01,2025-07-02,8207,COFFEE SHOP B,Dining,4.00,
2025-07-03,2025-07-04,8207,COFFEE SHOP A,Dining,3.50,
"""


def _setup(tmp_path, monkeypatch):
    """Seed a DB and point the app at it via the BUDGET_DB override."""
    db_path = tmp_path / "t.db"
    engine = get_engine(db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    csv_path = tmp_path / "in.csv"
    csv_path.write_text(CSV, encoding="utf-8")
    with session_factory() as session:
        import_csv(session, csv_path)
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def test_shortcut_prefills_highlighted_raw_vendor(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            # Vendors sort by descending count, so index 1 is COFFEE SHOP A (2 txns).
            app.query_one("#vendors", ListView).index = 1
            await pilot.press("ctrl+n")
            return app.query_one("#command", Input).value

    assert asyncio.run(run()) == "rename COFFEE SHOP A = "


def test_shortcut_prefills_active_vendor_filter(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            raw = next(v for v in app._vendors if v.name == "COFFEE SHOP B")
            app.vendor_filter = (raw.kind, raw.id)
            # Highlight a different row to prove the filter wins over the cursor.
            app.query_one("#vendors", ListView).index = 1
            await pilot.press("ctrl+n")
            return app.query_one("#command", Input).value

    assert asyncio.run(run()) == "rename COFFEE SHOP B = "


def test_shortcut_on_override_group_prefills_verb_only(tmp_path, monkeypatch):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        vendors.set_override(session, "COFFEE SHOP A", "Coffee")
        vendors.set_override(session, "COFFEE SHOP B", "Coffee")

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            group = next(v for v in app._vendors if v.kind == "name")
            app.vendor_filter = (group.kind, group.id)
            await pilot.press("ctrl+n")
            return app.query_one("#command", Input).value

    assert asyncio.run(run()) == "rename "


def test_shortcut_uses_focused_transactions_cursor(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            table = app.query_one("#txns", DataTable)
            table.focus()
            # Transactions sort newest first, so row 0 is the 2025-07-03 COFFEE SHOP A.
            table.move_cursor(row=0)
            await pilot.press("ctrl+n")
            first = app.query_one("#command", Input).value

            table.focus()
            table.move_cursor(row=1)  # 2025-07-01 COFFEE SHOP B
            await pilot.press("ctrl+n")
            second = app.query_one("#command", Input).value
            return first, second

    assert asyncio.run(run()) == (
        "rename COFFEE SHOP A = ",
        "rename COFFEE SHOP B = ",
    )


def test_transaction_shortcut_uses_raw_name_not_display_name(tmp_path, monkeypatch):
    """A grouped vendor is still renameable from the table, which knows the raw string."""
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        vendors.set_override(session, "COFFEE SHOP A", "Coffee")

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            table = app.query_one("#txns", DataTable)
            row = next(i for i, t in enumerate(app._txns) if t.vendor == "Coffee")
            table.focus()
            table.move_cursor(row=row)
            await pilot.press("ctrl+n")
            return app.query_one("#command", Input).value

    # The sidebar would only know "Coffee", which set_override cannot match.
    assert asyncio.run(run()) == "rename COFFEE SHOP A = "


def test_sidebar_still_used_when_table_not_focused(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            # The command bar holds focus on mount; the table cursor must not win.
            app.query_one("#vendors", ListView).index = 2  # COFFEE SHOP B
            await pilot.press("ctrl+n")
            return app.query_one("#command", Input).value

    assert asyncio.run(run()) == "rename COFFEE SHOP B = "


def _notifications(app):
    return [(n.title, n.message) for n in app._notifications]


def test_rules_command_lists_rules(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("rules")  # nothing defined yet
            await pilot.pause()
            empty = _notifications(app)[-1]

            app._run_command("rule COFFEE* = Coffee")
            app._run_command("rule *GROCER* = Groceries")
            await pilot.pause()
            app._run_command("rules")
            await pilot.pause()
            listed = _notifications(app)[-1]
            return empty, listed

    empty, listed = asyncio.run(run())
    assert empty[0] == "Vendor rules"
    assert "No vendor rules yet" in empty[1]

    assert listed[0] == "2 vendor rules"
    # Patterns are padded to a common width and paired with their display name.
    assert listed[1].splitlines() == [
        "COFFEE*   ->  Coffee",
        "*GROCER*  ->  Groceries",
    ]


def test_bare_rule_command_also_lists(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("rule COFFEE* = Coffee")
            await pilot.pause()
            app._run_command("rule")
            await pilot.pause()
            return _notifications(app)[-1]

    title, message = asyncio.run(run())
    assert title == "1 vendor rule"  # singular
    assert message == "COFFEE*  ->  Coffee"


def test_rules_listing_is_capped(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            for i in range(app.RULES_SHOWN + 3):
                app._run_command(f"rule PATTERN{i:02d}* = Name{i:02d}")
            await pilot.pause()
            app._run_command("rules")
            await pilot.pause()
            return _notifications(app)[-1]

    title, message = asyncio.run(run())
    lines = message.splitlines()
    assert title == "15 vendor rules"
    assert len(lines) == 13  # 12 rules + the overflow hint
    assert lines[-1] == "... and 3 more (budget rule list)"


def test_shortcut_with_no_vendor_selected_does_nothing(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app.query_one("#vendors", ListView).index = 0  # the "— All —" row
            await pilot.press("ctrl+n")
            return app.query_one("#command", Input).value

    assert asyncio.run(run()) == ""
