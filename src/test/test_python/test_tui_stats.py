"""Tests for tui/stats.py: the statistics table, fold/unfold, its status line, and
drilling down from a category row into that window's transactions."""

from __future__ import annotations

import asyncio
import datetime

from textual.widgets import DataTable, Input, ListView, Static

from budget_tracker import categories, queries, stats, vendors
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.importer import import_csv
from budget_tracker.models import Account, Currency, Transaction
from budget_tracker.tui import (
    FOLD_INDICATOR,
    TRANSFER_MARK,
    UNCONVERTED_MARK,
    BudgetApp,
    _fmt_amount,
)

from conftest import (
    _panel_state,
    _rows_of,
    _seed_category_hierarchy,
    _seed_category_hierarchy_with_income_sibling,
    _setup,
    _setup_recent,
    _stats_rows,
    _stats_state,
)


def test_stats_status_line_fits_the_main_panel(tmp_path, monkeypatch):
    """The panel gives the status 92 columns; a truncated line hides the totals.

    Measured against the widest figures the line is designed to hold rather than the
    fixture's tens of dollars: a seeded ``-40.00`` says nothing about whether a real year
    of spending fits. The ceiling below — a million dollars through 99,999 transactions
    over the longest label ("custom") — is well past any plausible personal budget, and
    lands exactly on the budget, so widening any field here will fail this test.
    """
    _setup_recent(tmp_path, monkeypatch)
    window = stats.parse("2024-01-01..2025-12-31")
    big = stats.Report(
        window=window,
        categories=[],
        count=99_999,
        net_minor=0,
        outflow_minor=-99_999_999,  # -999,999.99
        inflow_minor=99_999_999,
        transfer_count=999,
    )

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2y")
            await pilot.pause()
            width = app.query_one("#status", Static).size.width
            real = app._stats_status()
            app._report = big
            return real, app._stats_status(), width

    real, worst_case, width = asyncio.run(run())
    assert len(real) <= width - 2  # padding: 0 1
    assert len(worst_case) <= width - 2
    # The transfer count only appears when there is one to report.
    assert TRANSFER_MARK in worst_case and TRANSFER_MARK not in real


def test_stats_status_line_shows_unconverted_marker_and_still_fits(tmp_path, monkeypatch):
    """Same shape as test_stats_status_line_fits_the_main_panel above, but stressing
    unconverted_count instead of transfer_count -- a different reason money can be
    missing (see UNCONVERTED_MARK), with a marker terse enough to fit the same budget.
    Stacking both markers at once is not budgeted for, same as stacking filters isn't
    (see test_drill_down_status_line_fits_the_main_panel).
    """
    _setup_recent(tmp_path, monkeypatch)
    window = stats.parse("2024-01-01..2025-12-31")
    big = stats.Report(
        window=window,
        categories=[],
        count=99_999,
        net_minor=0,
        outflow_minor=-99_999_999,
        inflow_minor=99_999_999,
        transfer_count=0,
        unconverted_count=999,
    )

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2y")
            await pilot.pause()
            width = app.query_one("#status", Static).size.width
            real = app._stats_status()
            app._report = big
            return real, app._stats_status(), width

    real, worst_case, width = asyncio.run(run())
    assert len(real) <= width - 2
    assert len(worst_case) <= width - 2
    assert UNCONVERTED_MARK in worst_case and UNCONVERTED_MARK not in real


def test_stats_with_a_preset_spec_skips_the_picker(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 6m")
            await pilot.pause()
            six = (_stats_rows(app), _stats_state(app), app.window.key)

            app._run_command("stats 1 year")  # the spelled-out label works too
            await pilot.pause()
            return six, app.window.key

    (rows, state, key), year_key = asyncio.run(run())
    assert key == "6m" and year_key == "1y"
    assert state[0] is False and state[1] is True  # the picker never appeared
    # Six months reaches the 100-day-old row, so Dining gains a transaction.
    assert [row[:3] for row in rows] == [
        ["Housing", "1", "-1,000.00"],
        ["Dining", "3", "-540.00"],
        ["Income", "1", "2,000.00"],
        ["TOTAL", "5", "460.00"],
    ]


def test_stats_with_an_explicit_range_skips_the_picker(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)  # the 2025-07 fixture rows

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 2025-01-01..2025-06-30")
            await pilot.pause()
            empty = (_stats_rows(app), _stats_state(app))

            app._run_command("stats 2025-07-01..2025-07-31")
            await pilot.pause()
            return empty, _stats_rows(app), _stats_state(app), app.window

    (empty_rows, empty_state), rows, state, window = asyncio.run(run())
    assert empty_state[0] is False and empty_state[1] is True
    assert empty_rows == []  # a window with nothing in it is empty, not an error
    assert window.key == "custom"
    # 3.00 + 4.00 + 3.50 over 31 days: 1050 * 30.4375 / 31 = 1030.9 minor units a month.
    # The trailing "" is % parent, blank at depth 0 (identical to % spend there).
    assert rows == [
        ["Dining", "3", "-10.50", "-10.31", "100.0%", ""],
        ["TOTAL", "3", "-10.50", "-10.31", "100.0%", ""],
    ]
    assert state[2].startswith("custom 2025-07-01→2025-07-31 3 txns")


def test_a_bad_spec_notifies_and_leaves_the_panel_alone(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats banana")
            await pilot.pause()
            from_command = (
                [n.message for n in app._notifications],
                _panel_state(app)[0],
                len(app._txns),
                app.window,
            )

            app._run_command("stats")
            await pilot.pause()
            table = app.query_one("#periods", DataTable)
            table.move_cursor(row=5)
            await pilot.press("enter")
            await pilot.pause()
            app.query_one("#command", Input).value = "2025-13-01..nonsense"
            await pilot.press("enter")
            await pilot.pause()
            return from_command, _stats_state(app), app._range_pending

    (messages, txns_visible, txn_count, window), state, pending = asyncio.run(run())
    assert any("banana" in m for m in messages)
    assert txns_visible is True and txn_count == 3  # nowhere confusing, nothing blanked
    assert window is None
    assert state[0] is True and pending is True  # still at the prompt, over the picker


def test_escape_returns_to_transactions_from_the_stats_panel(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 3m")
            await pilot.pause()
            opened = _stats_state(app)
            await pilot.press("escape")
            await pilot.pause()
            return opened, _stats_state(app), _panel_state(app), app.focused.id

    opened, closed, panel, focused = asyncio.run(run())
    assert opened[1] is True
    assert closed[1] is False and panel[0] is True
    assert "txns" in panel[2]  # the totals line is back
    assert focused == "command"


def test_stats_panel_rescopes_when_a_filter_changes(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 6m")
            await pilot.pause()
            before = _stats_rows(app)

            app._run_command("filter CAFE")  # runs reload() under the covers
            await pilot.pause()
            filtered = (_stats_rows(app), _stats_state(app))

            category = next(c for c in app._categories if c.name == "Housing")
            app.text_filter = None
            app.category_filter = category.id
            app.reload()
            await pilot.pause()
            return before, filtered, _stats_rows(app), _stats_state(app)

    before, (filtered_rows, filtered_state), housing_rows, housing_state = asyncio.run(run())
    assert len(before) == 4  # three categories plus the closing TOTAL row
    # Averages depend on the length of a window anchored to today, so this case checks
    # the scope; the fixed custom range above pins the arithmetic.
    # The trailing "" is % parent, blank at depth 0.
    assert [row[:3] + row[4:] for row in filtered_rows] == [
        ["Dining", "2", "-40.00", "100.0%", ""],
        ["TOTAL", "2", "-40.00", "100.0%", ""],
    ]
    assert "2 txns" in filtered_state[2] and "out -40.00" in filtered_state[2]
    assert [row[:3] + row[4:] for row in housing_rows] == [
        ["Housing", "1", "-1,000.00", "100.0%", ""],
        ["TOTAL", "1", "-1,000.00", "100.0%", ""],
    ]
    assert housing_state[1] is True  # still on the stats panel throughout


# ------------------------------------------------------------------ categories


def test_sidebar_is_not_rebuilt_when_only_the_filters_changed(tmp_path, monkeypatch):
    """Rebuilding the sidebar dominated every drill-down: it runs to hundreds of rows,
    and mounting that many widgets costs far more than the query behind it. Nothing a
    filter or a drill does can change it — the sidebar queries take no filter arguments —
    so the rebuild must be skipped, while a real data change must still come through.
    """
    _setup_recent(tmp_path, monkeypatch)
    rebuilt = []
    original_clear = ListView.clear

    def counting_clear(self, *args, **kwargs):
        rebuilt.append(self.id)
        return original_clear(self, *args, **kwargs)

    monkeypatch.setattr(ListView, "clear", counting_clear)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            at_startup = list(rebuilt)

            app._run_command("stats 1m")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.move_cursor(row=0)
            rebuilt.clear()
            await pilot.press("right")  # drill down
            await pilot.pause()
            await pilot.press("left")  # and back
            await pilot.pause()
            after_drill = list(rebuilt)

            app._run_command("filter rent")
            await pilot.pause()
            after_filter = list(rebuilt)

            rebuilt.clear()
            app._run_command("categorize RENT PAYMENT = Housing Costs")
            await pilot.pause()
            sidebar = [
                str(item.children[0].content)
                for item in app.query_one("#categories", ListView).children
            ]
            return at_startup, after_drill, after_filter, sidebar

    at_startup, after_drill, after_filter, sidebar = asyncio.run(run())
    # All five lists are populated once when the app opens.
    assert sorted(at_startup) == ["accounts", "categories", "tags", "trips", "vendors"]
    assert after_drill == []  # a full round trip rebuilds nothing
    assert after_filter == []
    # ...but a change to the underlying data still reaches the screen.
    assert any("Housing Costs" in label for label in sidebar), sidebar


UNCATEGORISED_ROW = (12, "MYSTERY CHARGE", "", "25.00", "")


def _setup_with_uncategorised(tmp_path, monkeypatch):
    session_factory = _setup_recent(tmp_path, monkeypatch)
    days, description, category, debit, credit = UNCATEGORISED_ROW
    day = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    extra = tmp_path / "extra.csv"
    extra.write_text(
        "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\n"
        f"{day},{day},8207,{description},{category},{debit},{credit}\n",
        encoding="utf-8",
    )
    with session_factory() as session:
        import_csv(session, extra)
    return session_factory


def test_enter_on_a_stats_row_drills_into_that_category(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 1m")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=1)  # Dining, the second-biggest spender
            shown = [str(c) for c in table.get_row_at(1)]
            await pilot.press("enter")
            await pilot.pause()
            return (
                shown,
                app._panel,
                app.category_filter,
                app.date_filter,
                [t.description for t in app._txns],
                _panel_state(app)[2],
            )

    shown, panel, category_filter, date_filter, descriptions, status = asyncio.run(run())
    assert panel == "txns"  # the transactions the row was made of
    assert category_filter is not None
    window = stats.resolve("1m")
    assert date_filter == (window.start, window.end)
    # The row said "2"; the table has to agree, or the drill-down lies.
    assert shown[1] == "2" and len(descriptions) == 2
    assert sorted(descriptions) == ["CAFE ONE", "CAFE TWO"]
    # The 100-day-old CAFE row is Dining too, and is outside the window.
    assert "OLD SHOP" not in descriptions
    assert "[filtered: category," in status and str(window.start) in status


def test_enter_on_the_uncategorised_row_drills_in_too(tmp_path, monkeypatch):
    _setup_with_uncategorised(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 1m")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            row = next(
                i for i in range(table.row_count)
                if str(table.get_row_at(i)[0]) == stats.UNCATEGORISED
            )
            shown = [str(c) for c in table.get_row_at(row)]
            table.focus()
            table.move_cursor(row=row)
            await pilot.press("enter")
            await pilot.pause()
            return shown, app.category_filter, [t.description for t in app._txns]

    shown, category_filter, descriptions = asyncio.run(run())
    assert category_filter == queries.UNCATEGORISED_ID  # the null category, not "none"
    assert shown[1] == "1" and descriptions == ["MYSTERY CHARGE"]


def test_a_custom_window_drill_down_uses_that_window(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)  # the fixed 2025-07 rows

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 2025-07-01..2025-07-02")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            return app.date_filter, [t.posted_date for t in app._txns]

    date_filter, dates = asyncio.run(run())
    assert date_filter == (datetime.date(2025, 7, 1), datetime.date(2025, 7, 2))
    assert dates == ["2025-07-02", "2025-07-02"]  # the 07-04 row is out of the window


def test_clear_filters_also_clears_the_date_filter(tmp_path, monkeypatch):
    """A drill-down must not be sticky, or the table quietly hides later rows."""
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 1m")
            await pilot.pause()
            app.query_one("#stats_table", DataTable).focus()
            await pilot.press("enter")
            await pilot.pause()
            drilled = (app.date_filter, len(app._txns))

            await pilot.press("ctrl+l")
            await pilot.pause()
            cleared = (app.date_filter, app.category_filter, len(app._txns))

            app._run_command("stats 1m")
            await pilot.pause()
            app.query_one("#stats_table", DataTable).focus()
            await pilot.press("enter")
            await pilot.pause()
            app._run_command("all")
            await pilot.pause()
            return drilled, cleared, (app.date_filter, len(app._txns))

    drilled, cleared, after_all = asyncio.run(run())
    assert drilled[0] is not None and drilled[1] == 1  # Housing, one transaction
    assert cleared == (None, None, 5)  # every seeded row is back
    assert after_all == (None, 5)  # the `all` command clears it too


def test_drill_down_status_line_fits_the_main_panel(tmp_path, monkeypatch):
    """The window in the scope label costs columns the line does not really have.

    The panel gives the status 92 columns, two of them padding. A count, a scope list and
    three money figures already fill most of that, so the drill-down's window is written
    ``2025-01-01→12-31`` — the end date's year is noise once the start has given it. This
    pins that: spelling the year out twice pushes the case below over the edge.

    The line is not width-budgeted beyond this. Stacking an account and a vendor filter on
    top, or a text filter of any length, already overflowed it before the window existed;
    that is pre-existing and would need the whole line redesigned.
    """
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            width = app.query_one("#status", Static).size.width
            app._run_command("stats 1m")
            await pilot.pause()
            app.query_one("#stats_table", DataTable).focus()
            await pilot.press("enter")
            await pilot.pause()
            real = str(app.query_one("#status", Static).content)

            # A year of one category, in the hundreds of transactions and thousands of
            # dollars: comfortably past a real personal budget's biggest category.
            app.date_filter = (datetime.date(2025, 1, 1), datetime.date(2025, 12, 31))
            app._set_status(
                queries.Totals(
                    count=999,
                    net_minor=-999_999,
                    outflow_minor=-999_999,
                    inflow_minor=0,
                )
            )
            return real, str(app.query_one("#status", Static).content), width

    real, worst_case, width = asyncio.run(run())
    assert len(real) <= width - 2
    assert len(worst_case) <= width - 2
    assert "2025-01-01→12-31" in worst_case  # the year is given once, not twice


# ------------------------------------------------- arrow-key statistics navigation


def test_right_arrow_on_a_stats_row_drills_down_like_enter(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 1m")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=1)  # Dining, the second-biggest spender
            await pilot.press("right")
            await pilot.pause()
            return (
                app._panel,
                app.category_filter,
                app.date_filter,
                sorted(t.description for t in app._txns),
            )

    panel, category_filter, date_filter, descriptions = asyncio.run(run())
    assert panel == "txns"
    assert category_filter is not None
    window = stats.resolve("1m")
    assert date_filter == (window.start, window.end)
    assert descriptions == ["CAFE ONE", "CAFE TWO"]


def test_left_arrow_returns_to_the_stats_panel_with_the_full_breakdown(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 1m")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=1)  # Dining
            await pilot.press("right")
            await pilot.pause()
            drilled = (app._panel, app.category_filter, app.date_filter)

            await pilot.press("left")
            await pilot.pause()
            back_panel = app._panel
            rows = _stats_rows(app)
            cursor_row = app.query_one("#stats_table", DataTable).cursor_row
            return drilled, back_panel, rows, app.category_filter, app.date_filter, cursor_row

    drilled, back_panel, rows, category_filter, date_filter, cursor_row = asyncio.run(run())
    assert drilled[0] == "txns" and drilled[1] is not None and drilled[2] is not None
    assert back_panel == "stats"
    # The category filter the drill-down set is gone, or this would be one row, not the
    # whole breakdown.
    assert category_filter is None
    assert date_filter is None
    assert len(rows) > 1
    assert [r[0] for r in rows] == ["Housing", "Dining", "Income", "TOTAL"]
    assert cursor_row == 1  # back on the Dining row that was drilled from


def test_left_arrow_restores_filters_set_before_the_drill(tmp_path, monkeypatch):
    """Going back must not blank a filter the user had on purpose before drilling in."""
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            housing = next(c for c in app._categories if c.name == "Housing")
            app.category_filter = housing.id
            app.reload()
            await pilot.pause()

            app._run_command("stats 1m")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=0)  # the only row: Housing, scoped by the filter above
            await pilot.press("right")
            await pilot.pause()
            drilled_category_filter = app.category_filter

            await pilot.press("left")
            await pilot.pause()
            return housing.id, drilled_category_filter, app.category_filter, app._panel

    housing_id, drilled_category_filter, restored_category_filter, panel = asyncio.run(run())
    assert drilled_category_filter == housing_id  # the drill only had one row to pick
    assert restored_category_filter == housing_id  # back to the pre-drill filter, not None
    assert panel == "stats"


def test_left_arrow_does_nothing_without_a_drill_down(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            before = (app._panel, app.focused.id)
            await pilot.press("left")
            await pilot.pause()
            return before, (app._panel, app.focused.id)

    before, after = asyncio.run(run())
    assert before == after == ("txns", "command")


def test_a_new_filter_clears_the_stale_drill_down_flag(tmp_path, monkeypatch):
    """Once the user has changed the view another way, left arrow must not fire later."""
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 1m")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=1)
            await pilot.press("right")
            await pilot.pause()
            drilled = app._drilled_from_stats

            app._run_command("filter cafe")
            await pilot.pause()
            after_filter = app._drilled_from_stats
            panel = app._panel

            await pilot.press("left")
            await pilot.pause()
            return drilled, after_filter, panel, app._panel

    drilled, after_filter, panel_before_left, panel_after_left = asyncio.run(run())
    assert drilled is True
    assert after_filter is False
    assert panel_before_left == "txns"
    assert panel_after_left == "txns"  # left arrow did not send us anywhere


def test_escape_also_clears_the_stale_drill_down_flag(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 1m")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=1)
            await pilot.press("right")
            await pilot.pause()

            await pilot.press("escape")
            await pilot.pause()
            return app._drilled_from_stats

    assert asyncio.run(run()) is False


def test_the_drill_down_keys_are_advertised_in_the_footer(tmp_path, monkeypatch):
    """The stats status line and the drilled-down transactions status line are both
    already pinned to their 92-column budget by other tests (see
    test_stats_status_line_fits_the_main_panel and
    test_drill_down_status_line_fits_the_main_panel), with no room left for a hint. The
    footer advertises the keys instead, and only while they do something: showing a key
    that only works in one context all the time would be its own kind of confusing.
    """
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            await pilot.pause()
            not_drilled = dict(app.active_bindings)

            app._run_command("stats 1m")
            await pilot.pause()
            await pilot.pause()
            on_stats = dict(app.active_bindings)

            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=1)
            await pilot.press("right")
            await pilot.pause()
            await pilot.pause()
            drilled = dict(app.active_bindings)

            return not_drilled, on_stats, drilled

    def action_for(bindings, key):
        """The action bound to a key, if any.

        left/right are also bound by the focused widget itself (an ``Input``'s own
        cursor movement, say), so the key alone is not enough to prove our binding is
        the one showing — the action name is.
        """
        binding = bindings.get(key)
        return binding.binding.action if binding else None

    not_drilled, on_stats, drilled = asyncio.run(run())
    assert action_for(not_drilled, "left") != "drill_up"  # nothing to go back to yet
    assert action_for(not_drilled, "right") != "drill_down"  # not looking at a stats row
    assert action_for(on_stats, "right") == "drill_down"
    assert on_stats["right"].binding.description == "Drill down"
    assert action_for(drilled, "left") == "drill_up"
    assert drilled["left"].binding.description == "Back to stats"


# --------------------------------------------------------------- category hierarchy


def test_stats_table_indents_by_depth_and_shows_percent_parent(tmp_path, monkeypatch):
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            return _stats_rows(app)

    rows = asyncio.run(run())
    names = [row[0] for row in rows]
    # Depth-first, each parent immediately followed by its subtree, two spaces of
    # indentation per level.
    assert names[0] == "Food"
    assert names[1] == "  Dining"
    assert names[2].startswith("    Restaurants")  # truncated, but still 3 levels deep
    assert names[3] == "    Fast Food"
    assert names[4] == "  Groceries"
    # % parent is blank at depth 0 (it would just repeat % spend there), and populated
    # — and different from % spend — everywhere below it.
    assert rows[0][4] == "100.0%" and rows[0][5] == ""  # Food
    assert rows[1][4] == "81.0%" and rows[1][5] == "81.0%"  # Dining: Food holds nothing directly
    assert rows[2][4] == "68.0%" and rows[2][5] == "84.0%"  # Restaurants, of Dining's total
    assert rows[3][4] == "12.9%" and rows[3][5] == "16.0%"  # Fast Food, of Dining's total
    assert rows[4][4] == "19.0%" and rows[4][5] == "19.0%"  # Groceries: Food holds nothing directly


def test_stats_table_fits_the_main_panel_with_a_deep_hierarchy(tmp_path, monkeypatch):
    """The % parent column and the deepest indentation must not push the table off-panel.

    Measured against the table's own actual size, not the ~92-column estimate in the
    add_column comment, and against a three-level hierarchy with six-figure totals — a
    seeded ``-40.00`` a couple of levels deep would pass this even if the columns
    genuinely did not fit.
    """
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            widths = {str(column.label): column.width for column in table.columns.values()}
            panel_width = table.size.width
            return widths, panel_width, _stats_rows(app)

    widths, panel_width, rows = asyncio.run(run())
    # Six columns plus two cells of padding each have to fit inside the width Textual
    # actually gave the table, not just the comment's estimate.
    padding = 2 * len(widths)
    assert sum(widths.values()) + padding <= panel_width
    # A column too narrow for its own header clips it silently (this caught % parent
    # needing width 8, one more than % spend, since "% parent" is eight characters).
    for label, width in widths.items():
        assert len(label) <= width, f"{label!r} does not fit in width {width}"
    # The indented, truncated leaf name still respects the Category column's budget.
    assert all(len(row[0]) <= widths["Category"] for row in rows)
    # Every row below the top rendered a % parent value; only the root leaves it blank.
    # The closing TOTAL row is not a category and leaves it blank too, so it is excluded
    # here rather than the assertion being weakened to tolerate any blank.
    assert rows[-1][0] == "TOTAL"
    assert rows[0][-1] == ""
    assert all(row[-1].endswith("%") for row in rows[1:-1])


def test_stats_total_row_agrees_with_the_report_and_cannot_be_drilled(
    tmp_path, monkeypatch
):
    """The total must come from the report's own figures, and must not act like a row.

    Re-adding the column would double-count a parent's spending with its children's,
    since every row's money already includes its descendants.
    """
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 1m")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            report = app._report
            table.move_cursor(row=table.row_count - 1)  # the TOTAL row
            await pilot.press("right")  # the drill-down key
            await pilot.pause()
            return _stats_rows(app), report, app._panel, app.category_filter

    rows, report, panel, category_filter = asyncio.run(run())
    total = rows[-1]
    assert total[0] == "TOTAL"
    assert total[1] == str(report.count)
    assert total[2] == _fmt_amount(report.net_minor)
    assert total[3] == _fmt_amount(stats.per_month(report.net_minor, report.window))
    # Summing the depth-0 rows reproduces it; summing every row would not.
    depth0 = [c for c in report.categories if c.depth == 0]
    assert sum(c.total_minor for c in depth0) == report.net_minor
    assert sum(c.count for c in depth0) == report.count
    # Drilling from the total row is a no-op: it is not a category.
    assert panel == "stats" and category_filter is None


def _seed_deep_foldable_hierarchy(tmp_path, monkeypatch):
    """A foldable category with a long name three levels deep, for the width guard.

    ``Food > Dining > Casual and Fast Dining Establishments > Takeout`` — the third
    level is both foldable (it has one child) and long enough that the fold indicator's
    two extra characters are the difference between fitting the Category column and not.
    """
    db_path = tmp_path / "deep_fold.db"
    engine = get_engine(db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        currency = Currency(value="USD", symbol="$", decimal_places=2)
        session.add(currency)
        session.flush()
        account = Account(name="Checking", currency_id=currency.id)
        session.add(account)
        session.flush()
        parent = categories.ensure_path(
            session, "Food > Dining > Casual and Fast Dining Establishments"
        )
        child = categories.ensure_path(
            session,
            "Food > Dining > Casual and Fast Dining Establishments > Takeout",
        )
        session.flush()

        def txn(day, amount, description, category):
            session.add(
                Transaction(
                    account_id=account.id,
                    currency_id=currency.id,
                    posted_date=day,
                    description=description,
                    raw_description=description,
                    value_minor=amount,
                    category_id=category.id,
                    category_source="manual",
                    import_hash=f"deepfold-{description}-{day}-{amount}",
                )
            )

        txn(datetime.date(2025, 3, 1), -12_345_678, "Big meal", parent)
        txn(datetime.date(2025, 3, 2), -2_345_678, "Takeout order", child)
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def test_space_folds_and_unfolds_a_stats_row_with_children(tmp_path, monkeypatch):
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            expanded = _stats_rows(app)

            table.move_cursor(row=1)  # Dining, which has two children
            await pilot.press("space")
            await pilot.pause()
            collapsed = _stats_rows(app)

            await pilot.press("space")  # press it again: back to expanded
            await pilot.pause()
            reexpanded = _stats_rows(app)

            return expanded, collapsed, reexpanded

    expanded, collapsed, reexpanded = asyncio.run(run())
    names_expanded = [row[0] for row in expanded]
    assert names_expanded == [
        "Food",
        "  Dining",
        "    Restaurants and Fast …",
        "    Fast Food",
        "  Groceries",
        "TOTAL",
    ]
    names_collapsed = [row[0] for row in collapsed]
    # Restaurants and Fast Food, Dining's subtree, are gone; Groceries moves up.
    assert names_collapsed == [
        "Food",
        f"  {FOLD_INDICATOR} Dining",
        "  Groceries",
        "TOTAL",
    ]
    assert [row[0] for row in reexpanded] == names_expanded


def test_folding_a_group_hides_its_whole_subtree_recursively(tmp_path, monkeypatch):
    """Collapsing ``Food`` (depth 0) must hide Dining, Groceries, and Dining's own
    children — not just its immediate row."""
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=0)  # Food, the root of everything seeded
            await pilot.press("space")
            await pilot.pause()
            return _stats_rows(app)

    rows = asyncio.run(run())
    assert [row[0] for row in rows] == [f"{FOLD_INDICATOR} Food", "TOTAL"]


def test_space_does_nothing_on_a_leaf_row_or_the_total_row(tmp_path, monkeypatch):
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            before = _stats_rows(app)

            table.move_cursor(row=4)  # Groceries, a leaf
            await pilot.press("space")
            await pilot.pause()
            after_leaf = _stats_rows(app)

            table.move_cursor(row=table.row_count - 1)  # TOTAL
            await pilot.press("space")
            await pilot.pause()
            after_total = _stats_rows(app)

            return before, after_leaf, after_total

    before, after_leaf, after_total = asyncio.run(run())
    assert before == after_leaf == after_total


def test_folding_does_not_change_any_numbers(tmp_path, monkeypatch):
    """Every row's figures already roll up its descendants, so folding — which only
    hides rows — must not edit a single one of them."""
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            before = _stats_rows(app)
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=1)  # Dining
            await pilot.press("space")
            await pilot.pause()
            after = _stats_rows(app)
            return before, after

    before, after = asyncio.run(run())
    assert len(before) == 6 and len(after) == 4
    # Food's row rolls up all of Dining's subtree; that total does not move.
    assert before[0] == after[0]
    # Dining's own figures are unchanged; only its name grows the fold indicator.
    assert before[1][1:] == after[1][1:]
    assert before[1][0] == "  Dining"
    assert after[1][0] == f"  {FOLD_INDICATOR} Dining"
    # Groceries and TOTAL, untouched by the fold, are byte-for-byte identical, just
    # moved up to fill the gap Dining's hidden children left.
    assert before[4] == after[2]
    assert before[5] == after[3]


def test_fold_state_survives_a_window_change_a_filter_and_a_drill_round_trip(
    tmp_path, monkeypatch
):
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=1)  # Dining
            await pilot.press("space")  # collapse
            await pilot.pause()
            after_fold = [row[0] for row in _stats_rows(app)]

            # A new window rebuilds the report from scratch.
            app._run_command("stats 2020-01-01..2025-12-31")
            await pilot.pause()
            after_window = [row[0] for row in _stats_rows(app)]

            # So does a filter.
            app.reload()
            await pilot.pause()
            after_filter = [row[0] for row in _stats_rows(app)]

            # And so does a full drill-in/drill-out round trip.
            table.move_cursor(row=0)  # Food, still visible with Dining collapsed
            await pilot.press("right")
            await pilot.pause()
            await pilot.press("left")
            await pilot.pause()
            after_drill = [row[0] for row in _stats_rows(app)]

            return after_fold, after_window, after_filter, after_drill

    after_fold, after_window, after_filter, after_drill = asyncio.run(run())
    expected = ["Food", f"  {FOLD_INDICATOR} Dining", "  Groceries", "TOTAL"]
    assert after_fold == expected
    assert after_window == expected
    assert after_filter == expected
    assert after_drill == expected


def test_drill_down_maps_to_the_row_actually_clicked_after_a_collapse(
    tmp_path, monkeypatch
):
    """The trap: once rows can be hidden, table row N is no longer report.categories[N].

    A row below a collapsed group has to drill into the category it visibly shows, not
    into whatever used to sit at that table index before the collapse.
    """
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=1)  # Dining
            await pilot.press("space")  # hides Restaurants and Fast Food
            await pilot.pause()
            rows = _stats_rows(app)
            assert rows[2][0] == "  Groceries"  # moved up into what was row 2

            table.move_cursor(row=2)
            await pilot.press("enter")
            await pilot.pause()
            return [t.description for t in app._txns]

    descriptions = asyncio.run(run())
    # Groceries' own transaction, not "Big meal"/"Fast food" from Dining's hidden rows.
    assert descriptions == ["Groceries"]


def test_left_arrow_after_a_collapsed_drill_down_lands_back_on_the_row_clicked(
    tmp_path, monkeypatch
):
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=1)  # Dining
            await pilot.press("space")  # collapse
            await pilot.pause()

            table.move_cursor(row=2)  # Groceries, directly below the collapsed group
            await pilot.press("right")  # drill down
            await pilot.pause()
            await pilot.press("left")  # back
            await pilot.pause()

            rows = _stats_rows(app)
            cursor_row = app.query_one("#stats_table", DataTable).cursor_row
            return rows, cursor_row, app._panel

    rows, cursor_row, panel = asyncio.run(run())
    assert panel == "stats"
    assert cursor_row == 2  # back on Groceries, not shifted by the still-collapsed group
    assert [row[0] for row in rows] == [
        "Food",
        f"  {FOLD_INDICATOR} Dining",
        "  Groceries",
        "TOTAL",
    ]


def test_space_does_nothing_outside_the_stats_table(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            txns = app.query_one("#txns", DataTable)
            txns.focus()
            before_txns = _rows_of(app, "txns")
            await pilot.press("space")
            await pilot.pause()
            after_txns = _rows_of(app, "txns")
            txns_panel = app._panel

            app._run_command("rules")
            await pilot.pause()
            rules = app.query_one("#rules", DataTable)
            rules.focus()
            before_rules = _rows_of(app, "rules")
            await pilot.press("space")
            await pilot.pause()
            after_rules = _rows_of(app, "rules")
            rules_panel = app._panel

            return (
                before_txns,
                after_txns,
                txns_panel,
                before_rules,
                after_rules,
                rules_panel,
            )

    (
        before_txns,
        after_txns,
        txns_panel,
        before_rules,
        after_rules,
        rules_panel,
    ) = asyncio.run(run())
    assert before_txns == after_txns
    assert txns_panel == "txns"
    assert before_rules == after_rules
    assert rules_panel == "rules"


def test_space_still_types_a_literal_space_in_the_command_bar(tmp_path, monkeypatch):
    """The fold binding is deliberately not priority=True, so it must never steal a
    space bar press from the Input that holds focus most of the app's life."""
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            text = "filter multi word search"
            keys = [char if char != " " else "space" for char in text]
            await pilot.press(*keys)
            return app.query_one("#command", Input).value

    assert asyncio.run(run()) == "filter multi word search"


def test_stats_table_fold_indicator_fits_the_main_panel_on_a_deep_row(
    tmp_path, monkeypatch
):
    """Extends test_stats_table_fits_the_main_panel_with_a_deep_hierarchy: the fold
    indicator's two extra characters must not push a deep, long-named, collapsed row
    past the Category column's 26-character budget.
    """
    _seed_deep_foldable_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=2)  # "Casual and Fast Dining Establishments", depth 2
            await pilot.press("space")
            await pilot.pause()
            widths = {str(column.label): column.width for column in table.columns.values()}
            panel_width = table.size.width
            return widths, panel_width, _stats_rows(app)

    widths, panel_width, rows = asyncio.run(run())
    padding = 2 * len(widths)
    assert sum(widths.values()) + padding <= panel_width
    collapsed_row = rows[2]
    assert collapsed_row[0].startswith(f"    {FOLD_INDICATOR} ")
    for label, width in widths.items():
        assert len(label) <= width, f"{label!r} does not fit in width {width}"
    assert all(len(row[0]) <= widths["Category"] for row in rows)


# ----------------------------------------------------- statistics fold/unfold all ("f")


def test_f_folds_and_unfolds_every_group_at_once(tmp_path, monkeypatch):
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            expanded = _stats_rows(app)

            await pilot.press("f")
            await pilot.pause()
            collapsed = _stats_rows(app)

            await pilot.press("f")  # press again: back to fully expanded
            await pilot.pause()
            reexpanded = _stats_rows(app)

            return expanded, collapsed, reexpanded

    expanded, collapsed, reexpanded = asyncio.run(run())
    names_expanded = [row[0] for row in expanded]
    assert names_expanded == [
        "Food",
        "  Dining",
        "    Restaurants and Fast …",
        "    Fast Food",
        "  Groceries",
        "TOTAL",
    ]
    # Food is the only top-level group, and folding it hides its entire subtree —
    # Dining included — even though Dining itself is also independently foldable.
    assert [row[0] for row in collapsed] == [f"{FOLD_INDICATOR} Food", "TOTAL"]
    assert [row[0] for row in reexpanded] == names_expanded


def test_f_always_collapses_when_any_group_is_expanded(tmp_path, monkeypatch):
    """A mix of folded and unfolded groups is not "some folded, some not" after ``f`` —
    the key always does something visible, so a partial state collapses fully rather
    than being read as "some already folded" and unfolding the rest."""
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=1)  # Dining
            await pilot.press("space")  # fold just Dining by hand
            await pilot.pause()
            partially_folded = _stats_rows(app)

            await pilot.press("f")  # Food is still expanded, so this collapses all
            await pilot.pause()
            fully_collapsed = _stats_rows(app)

            await pilot.press("f")  # and this expands all, Dining included
            await pilot.pause()
            fully_expanded = _stats_rows(app)

            return partially_folded, fully_collapsed, fully_expanded

    partially_folded, fully_collapsed, fully_expanded = asyncio.run(run())
    assert [row[0] for row in partially_folded] == [
        "Food",
        f"  {FOLD_INDICATOR} Dining",
        "  Groceries",
        "TOTAL",
    ]
    assert [row[0] for row in fully_collapsed] == [f"{FOLD_INDICATOR} Food", "TOTAL"]
    assert [row[0] for row in fully_expanded] == [
        "Food",
        "  Dining",
        "    Restaurants and Fast …",
        "    Fast Food",
        "  Groceries",
        "TOTAL",
    ]


def test_f_does_not_change_any_numbers(tmp_path, monkeypatch):
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            before = _stats_rows(app)
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            await pilot.press("f")
            await pilot.pause()
            after = _stats_rows(app)
            return before, after

    before, after = asyncio.run(run())
    # Food's row (rolled up, including everything folded away) is unchanged; only its
    # name grows the fold indicator.
    assert before[0][0] == "Food"
    assert after[0][0] == f"{FOLD_INDICATOR} Food"
    assert before[0][1:] == after[0][1:]
    # TOTAL, unaffected by folding, is byte-for-byte identical.
    assert before[-1] == after[-1]


def test_f_does_nothing_outside_the_stats_table(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            txns = app.query_one("#txns", DataTable)
            txns.focus()
            before_txns = _rows_of(app, "txns")
            await pilot.press("f")
            await pilot.pause()
            after_txns = _rows_of(app, "txns")
            txns_panel = app._panel

            app._run_command("rules")
            await pilot.pause()
            rules = app.query_one("#rules", DataTable)
            rules.focus()
            before_rules = _rows_of(app, "rules")
            await pilot.press("f")
            await pilot.pause()
            after_rules = _rows_of(app, "rules")
            rules_panel = app._panel

            return (
                before_txns,
                after_txns,
                txns_panel,
                before_rules,
                after_rules,
                rules_panel,
            )

    (
        before_txns,
        after_txns,
        txns_panel,
        before_rules,
        after_rules,
        rules_panel,
    ) = asyncio.run(run())
    assert before_txns == after_txns
    assert txns_panel == "txns"
    assert before_rules == after_rules
    assert rules_panel == "rules"


def test_f_still_types_a_literal_f_in_the_command_bar(tmp_path, monkeypatch):
    """The fold-all binding is deliberately not priority=True: a plain 'f' typed into a
    command must reach the Input, not get swallowed as a fold-all keypress."""
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            text = "filter food"
            await pilot.press(*text)
            return app.query_one("#command", Input).value

    assert asyncio.run(run()) == "filter food"


def test_drill_down_after_fold_all_maps_to_the_row_actually_visible(
    tmp_path, monkeypatch
):
    """The same trap the per-row fold has: once ``f`` hides Food's whole subtree, the
    next visible row (Income) is table row 1, and a drill-down there must open Income,
    not whatever used to sit at row 1 before everything collapsed.
    """
    _seed_category_hierarchy_with_income_sibling(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            await pilot.press("f")
            await pilot.pause()
            rows = _stats_rows(app)
            assert rows[1][0] == "Income"  # visible, right after collapsed Food

            table.move_cursor(row=1)
            await pilot.press("enter")
            await pilot.pause()
            return [t.description for t in app._txns]

    descriptions = asyncio.run(run())
    assert descriptions == ["Paycheck"]



# ------------------------------------------------------------------------- chart
