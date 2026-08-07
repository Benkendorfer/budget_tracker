"""Tests for tui/periods.py: the period picker, and how it routes a chosen window
back to whichever of stats/chart/pie opened it."""

from __future__ import annotations

import asyncio

from textual.widgets import DataTable, Input, Static

from budget_tracker import stats
from budget_tracker.tui import BudgetApp

from conftest import CHART_WINDOW, _chart_rows, _rows_of, _setup, _setup_chart, _setup_recent, _stats_rows, _stats_state


def test_stats_command_opens_the_period_picker(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats")
            await pilot.pause()
            return _rows_of(app, "periods"), _stats_state(app), app.focused.id

    rows, state, focused = asyncio.run(run())
    assert [row[0] for row in rows] == [
        "1 month", "3 months", "6 months", "1 year", "2 years", "Custom…",
    ]
    # Every preset spells out the dates it resolves to.
    window = stats.resolve("6m")
    assert rows[2][1] == f"{window.start} → {window.end}"
    assert rows[5][1] == "2025-01-01..2025-06-30"  # the format a custom range takes
    assert state[0] is True and state[1] is False
    assert "choose a period" in state[2] and "escape to return" in state[2]
    assert focused == "periods"


def test_enter_on_a_preset_shows_the_stats_panel(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats")
            await pilot.pause()
            await pilot.press("enter")  # the cursor starts on "1 month"
            await pilot.pause()
            return _stats_rows(app), _stats_state(app), app.window, app.focused.id

    rows, state, window, focused = asyncio.run(run())
    assert window.key == "1m"
    assert state[0] is False and state[1] is True
    # Biggest spender first, income last; the 100-day-old row is out of this window.
    # The closing TOTAL row sums the depth-0 rows above it: -1,000 - 40 + 2,000 = 960.
    assert [row[0] for row in rows] == ["Housing", "Dining", "Income", "TOTAL"]
    assert [row[2] for row in rows] == ["-1,000.00", "-40.00", "2,000.00", "960.00"]
    assert [row[4] for row in rows] == ["96.2%", "3.8%", "0.0%", "100.0%"]
    assert rows[-1][1] == "4"  # every transaction in the window, not just the spending
    assert state[2].startswith("1 month ")
    assert "4 txns" in state[2] and "out -1,040.00" in state[2]
    assert "in 2,000.00" in state[2]
    assert focused == "stats_table"


def test_custom_row_prompts_for_a_range_and_applies_it(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats")
            await pilot.pause()
            table = app.query_one("#periods", DataTable)
            table.move_cursor(row=5)  # Custom…
            await pilot.press("enter")
            await pilot.pause()
            prompt = app.query_one("#prompt", Static)
            asked = (
                app._range_pending,
                prompt.display,
                str(prompt.content),
                app.focused.id,
                _stats_state(app)[0],
            )

            app.query_one("#command", Input).value = "2025-07-01..2025-07-31"
            await pilot.press("enter")
            await pilot.pause()
            return (
                asked,
                _stats_rows(app),
                _stats_state(app),
                app._range_pending,
                prompt.display,
            )

    asked, rows, state, pending, prompt_visible = asyncio.run(run())
    assert asked[0] is True and asked[1] is True  # the prompt is up, over the picker
    assert "2025-01-01..2025-06-30" in asked[2]  # and shows the expected format
    assert asked[3] == "command" and asked[4] is True
    assert pending is False and prompt_visible is False  # the question is done with
    # The trailing "" is % parent, blank at depth 0.
    assert state[1] is True and rows == [
        ["Dining", "3", "-10.50", "-10.31", "100.0%", ""],
        ["TOTAL", "3", "-10.50", "-10.31", "100.0%", ""],
    ]


def test_escape_at_the_range_prompt_returns_to_the_picker(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats")
            await pilot.pause()
            app.query_one("#periods", DataTable).move_cursor(row=5)
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            return (
                app._range_pending,
                app.query_one("#prompt", Static).display,
                _stats_state(app),
                app.focused.id,
            )

    pending, prompt_visible, state, focused = asyncio.run(run())
    assert pending is False and prompt_visible is False
    assert state[0] is True and state[1] is False  # back in the picker, not the table
    assert focused == "periods"


def test_bare_chart_opens_the_picker_and_the_picker_opens_the_chart(tmp_path, monkeypatch):
    """The period picker is shared with `stats`, so it has to remember who asked."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("chart")
            await pilot.pause()
            picker = app._panel
            app.query_one("#periods", DataTable).move_cursor(row=0)  # 1 month
            await pilot.press("enter")
            await pilot.pause()
            return picker, app._panel

    picker, landed = asyncio.run(run())
    assert picker == "periods"
    assert landed == "chart"


def test_the_picker_still_opens_the_statistics_panel_for_stats(tmp_path, monkeypatch):
    """The other half of the same seam: routing the picker must not have stolen it."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("chart")  # leaves the picker pointed at the chart
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            app._run_command("stats")
            await pilot.pause()
            app.query_one("#periods", DataTable).move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            return app._panel

    assert asyncio.run(run()) == "stats"


def test_a_custom_range_typed_into_the_picker_reaches_the_chart(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("chart")
            await pilot.pause()
            table = app.query_one("#periods", DataTable)
            table.move_cursor(row=len(stats.PRESETS))  # Custom…
            await pilot.press("enter")
            await pilot.pause()
            app.query_one("#command", Input).value = CHART_WINDOW
            await pilot.press("enter")
            await pilot.pause()
            return app._panel, app._bucket, _chart_rows(app)

    panel, bucket, rows = asyncio.run(run())
    assert panel == "chart"
    # 90 days, so it buckets by week without being asked — a range typed into the picker
    # goes through the same default as one typed on the command line.
    assert bucket == "week"
    assert rows[-1][0] == "TOTAL" and rows[-1][2] == "2,925.00"


def test_bare_pie_opens_the_picker_and_the_picker_opens_the_pie(tmp_path, monkeypatch):
    """The period picker is shared with stats/chart, so it has to remember who asked."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("pie")
            await pilot.pause()
            picker = app._panel
            app.query_one("#periods", DataTable).move_cursor(row=0)  # 1 month
            await pilot.press("enter")
            await pilot.pause()
            return picker, app._panel

    picker, landed = asyncio.run(run())
    assert picker == "periods"
    assert landed == "pie"
