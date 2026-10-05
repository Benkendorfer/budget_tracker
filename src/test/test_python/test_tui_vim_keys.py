"""Vim-style hjkl: j/k move, h/l do what the arrows do, and the command bar still types."""

from __future__ import annotations

import asyncio

from textual.widgets import DataTable, Input, ListView

from budget_tracker.tui import BudgetApp

from conftest import _setup


def test_j_and_k_move_the_transactions_cursor(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            table = app.query_one("#txns", DataTable)
            table.focus()
            await pilot.pause()
            await pilot.press("j", "j")
            after_down = table.cursor_row
            await pilot.press("k")
            return after_down, table.cursor_row

    assert asyncio.run(run()) == (2, 1)


def test_j_moves_the_focused_sidebar_list(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            accounts = app.query_one("#accounts", ListView)
            accounts.focus()
            accounts.index = 0
            await pilot.pause()
            await pilot.press("j")
            return accounts.index

    assert asyncio.run(run()) == 1


def test_the_command_bar_still_types_hjkl(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            command = app.query_one("#command", Input)
            command.focus()
            await pilot.pause()
            await pilot.press("h", "j", "k", "l")
            return command.value, app.query_one("#txns", DataTable).cursor_row

    assert asyncio.run(run()) == ("hjkl", 0)


def test_l_and_h_drill_into_statistics_and_back_like_the_arrows(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def drill(forward: str, back: str):
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 2025-07-01..2025-07-31")
            await pilot.pause()
            app.query_one("#stats_table", DataTable).focus()
            await pilot.pause()
            await pilot.press(forward)
            drilled = (app._panel, app.category_filter is not None)
            # A drill lands with the command bar focused, where h is just a letter; the
            # arrow acts from anywhere. So h goes back once the table has focus.
            app.query_one("#txns", DataTable).focus()
            await pilot.pause()
            await pilot.press(back)
            return drilled, app._panel

    assert asyncio.run(drill("l", "h")) == asyncio.run(drill("right", "left"))
    (panel, filtered), back_to = asyncio.run(drill("l", "h"))
    assert (panel, filtered, back_to) == ("txns", True, "stats")


def test_z_folds_a_statistics_row_like_space(tmp_path, monkeypatch):
    from budget_tracker import categories

    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        # A parent above the fixture's Dining category, so there is something to fold.
        categories.ensure_path(session, "Food > Dining", confirm_relocation=True)
        session.commit()

    async def fold(key: str):
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 2025-07-01..2025-07-31")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press(key)
            return set(app._collapsed)

    folded_by_z = asyncio.run(fold("z"))
    assert folded_by_z  # something was actually folded
    assert folded_by_z == asyncio.run(fold("space"))
