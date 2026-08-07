"""Tests for tui/rules.py: the rules panel listing vendor and category rules."""

from __future__ import annotations

import asyncio

from textual.widgets import DataTable

from budget_tracker import vendors
from budget_tracker.tui import BudgetApp

from conftest import _category_of, _panel_state, _rows_of, _setup


def test_rules_command_opens_the_main_panel(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            before = _panel_state(app)

            app._run_command("rule COFFEE* = Coffee")
            app._run_command("rule *GROCER* = Groceries")
            await pilot.pause()
            app._run_command("rules")
            await pilot.pause()

            table = app.query_one("#rules", DataTable)
            rows = [
                [str(c) for c in table.get_row_at(i)] for i in range(table.row_count)
            ]
            return before, _panel_state(app), rows, app.focused.id

    before, after, rows, focused = asyncio.run(run())
    assert before == (True, False, before[2])  # transactions own the panel on mount
    assert after[0] is False and after[1] is True
    # COFFEE* names both coffee vendors; *GROCER* matches nothing in this data.
    # The leading cell is the rule's kind, now that category rules share the panel.
    assert rows == [
        ["vendor", "COFFEE*", "Coffee", "2"],
        ["vendor", "*GROCER*", "Groceries", "0"],
    ]
    assert "2 rules" in after[2] and "escape to return" in after[2]
    assert focused == "rules"


def test_escape_returns_to_transactions(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("rule COFFEE* = Coffee")
            await pilot.pause()
            app._run_command("rules")
            await pilot.pause()
            opened = _panel_state(app)

            await pilot.press("escape")
            await pilot.pause()
            return opened, _panel_state(app), app.focused.id

    opened, closed, focused = asyncio.run(run())
    assert opened[1] is True
    assert closed[0] is True and closed[1] is False
    assert "txns" in closed[2]  # the totals line is back
    assert focused == "command"


def test_escape_is_a_noop_in_transaction_view(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            await pilot.press("escape")
            await pilot.pause()
            return _panel_state(app)

    visible_txns, visible_rules, _ = asyncio.run(run())
    assert visible_txns is True and visible_rules is False


def test_bare_rule_command_also_opens_the_panel(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("rule COFFEE* = Coffee")
            await pilot.pause()
            app._run_command("rule")
            await pilot.pause()
            return _panel_state(app), app.query_one("#rules", DataTable).row_count

    state, row_count = asyncio.run(run())
    assert state[1] is True
    assert row_count == 1
    assert "1 rule " in state[2]  # singular


def test_rules_panel_with_no_rules_is_empty_and_notifies(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("rules")
            await pilot.pause()
            messages = [n.message for n in app._notifications]
            return _panel_state(app), app.query_one("#rules", DataTable).row_count, messages

    state, row_count, messages = asyncio.run(run())
    assert state[1] is True  # the panel still opens, just empty
    assert row_count == 0
    assert any("No vendor rules yet" in m for m in messages)


def test_new_rule_appears_in_an_open_panel(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("rules")
            await pilot.pause()
            app._run_command("rule COFFEE* = Coffee")
            await pilot.pause()
            table = app.query_one("#rules", DataTable)
            return _panel_state(app), table.row_count

    state, row_count = asyncio.run(run())
    assert state[1] is True  # still on the rules panel
    assert row_count == 1  # and it picked up the rule that was just added


def test_categorize_rule_categorises_matching_vendors(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("categorize rule COFFEE* = Coffee")
            await pilot.pause()
            return (
                [t.category for t in app._txns],
                [n.message for n in app._notifications],
            )

    category_cells, messages = asyncio.run(run())
    assert category_cells == ["Coffee"] * 3  # a rule overwrites the bank's own category
    assert any("3 transactions categorised" in m for m in messages)


def test_manual_category_outranks_a_later_rule(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("categorize COFFEE SHOP A = Treats")
            await pilot.pause()
            app._run_command("categorize rule COFFEE* = Coffee")
            await pilot.pause()
            return _category_of(app, "COFFEE SHOP A"), _category_of(app, "COFFEE SHOP B")

    assert asyncio.run(run()) == ("Treats", "Coffee")


def test_rules_panel_lists_both_kinds_of_rule(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("rule COFFEE* = Coffee")
            app._run_command("categorize rule *SHOP A = Treats")
            await pilot.pause()
            app._run_command("categorize rules")
            await pilot.pause()
            named = _panel_state(app)

            await pilot.press("escape")
            await pilot.pause()
            app._run_command("categorize")  # bare, like a bare `rule`
            await pilot.pause()
            return _rows_of(app, "rules"), _panel_state(app), app.focused.id, named

    rows, state, focused, named = asyncio.run(run())
    assert named[1] is True  # `categorize rules` opens the same panel
    assert rows == [
        ["vendor", "COFFEE*", "Coffee", "2"],  # 2 raw vendors named
        ["category", "*SHOP A", "Treats", "2"],  # 2 transactions categorised
    ]
    assert state[1] is True and focused == "rules"
    assert "2 rules" in state[2] and "2 vendors named" in state[2]
    assert "2 txns categorised" in state[2]


def test_a_manual_category_is_not_counted_against_a_rule(tmp_path, monkeypatch):
    """The count is what the rule owns, so rows it could not take are left out."""
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("categorize COFFEE SHOP A = Treats")
            await pilot.pause()
            app._run_command("categorize rule COFFEE* = Coffee")
            await pilot.pause()
            app._run_command("rules")
            await pilot.pause()
            return _rows_of(app, "rules")

    # Three coffee transactions match the pattern, but two are manual, so the rule
    # only owns COFFEE SHOP B's single row.
    assert asyncio.run(run()) == [["category", "COFFEE*", "Coffee", "1"]]


def test_rules_panel_columns_fit_the_main_panel(tmp_path, monkeypatch):
    """A column pushed off the right edge hides the count, which is the point."""
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("rule COFFEE* = Coffee")
            app._run_command("categorize rule COFFEE* = Coffee and cake")
            await pilot.pause()
            app._run_command("rules")
            await pilot.pause()
            table = app.query_one("#rules", DataTable)
            widths = [c.width for c in table.columns.values()]
            return widths, table.size.width

    widths, panel_width = asyncio.run(run())
    assert widths == [9, 26, 18, 7]
    # Two cells of padding per column, plus the panel's own round border.
    assert sum(widths) + 2 * len(widths) + 2 <= panel_width


# ------------------------------------------------------- statistics drill-down
# One row with no category at all, so the Uncategorised row of the report has
# something to drill into.
