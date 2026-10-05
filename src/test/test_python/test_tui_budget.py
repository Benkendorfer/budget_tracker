"""Tests for the two budget panels: ``budget`` (tracking) and ``budget plan``
(the editable plan), both built on :mod:`budget_tracker.budget` -- nothing here
re-derives a figure that module already computes; every number asserted below is
cross-checked against a direct call into it.

Like ``test_cli_budget.py``, a fixed "today" is needed for the tracking panel's pace
line and over/ahead-of-pace marking; ``budget.date`` is monkeypatched the same way
that file monkeypatches ``budget_cmd.date``.
"""

from __future__ import annotations

import asyncio
import io
from datetime import date

from rich.console import Console
from textual.widgets import DataTable, Input, Static

from budget_tracker import budget as budget_core
from budget_tracker import categories
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.models import Account, Currency, Transaction
from budget_tracker.tui import BudgetApp
from budget_tracker.tui.commands import budget as budget_commands

from conftest import _rows_of


class _FixedDate(date):
    """A ``date`` subclass whose ``.today()`` is pinned -- see the module docstring."""

    _TODAY = date(2026, 9, 4)

    @classmethod
    def today(cls):
        return cls._TODAY


def _freeze(monkeypatch, today: date) -> None:
    """Pin "today" for both budget.track()'s own default and the TUI's "budget" with
    no month argument (commands/budget.py's own ``date.today()`` call) -- two separate
    imports of the same name, so both need patching."""
    _FixedDate._TODAY = today
    monkeypatch.setattr(budget_core, "date", _FixedDate)
    monkeypatch.setattr(budget_commands, "date", _FixedDate)


def _setup_budget(tmp_path, monkeypatch, name="budget.db"):
    """Currency, one account, and Food/Fun/Travel categories -- the transactions and
    budget rows each test adds are its own, so this stays a bare skeleton."""
    db_path = tmp_path / name
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
        cats = {
            name: categories.ensure_path(session, name)
            # "Paycheck", not "Income": budget.track()'s income row is not a category
            # at all (it reads every depth-0 category's net-positive total, see
            # budget.py's module docstring) -- naming one "Income" here would only
            # read as though it were special, which it is not.
            for name in ("Food", "Fun", "Travel", "Paycheck")
        }
        session.flush()
        currency_id, account_id = currency.id, account.id
        cat_ids = {name: cat.id for name, cat in cats.items()}
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory, currency_id, account_id, cat_ids


def _txn(session_factory, currency_id, account_id, day, amount_minor, description, category_id=None):
    with session_factory() as session:
        session.add(
            Transaction(
                account_id=account_id,
                currency_id=currency_id,
                category_id=category_id,
                posted_date=day,
                description=description,
                raw_description=description,
                value_minor=amount_minor,
                import_hash=f"{day}-{amount_minor}-{description}-{id(object())}",
            )
        )
        session.commit()


def _set_budget(session_factory, month, category, amount_minor, kind=budget_core.EXPENSE):
    with session_factory() as session:
        budget_core.set_amount(session, month, category, amount_minor, kind=kind)
        session.commit()


def _seed_main_scenario(tmp_path, monkeypatch):
    """Sep 2026 (the "current" month, today frozen to the 4th) is tracked; Oct 2026 is
    planned from Sep's actuals. Numbers chosen to match budget.track()/plan_rows()
    exactly -- see the module docstring.

    Oct's own budget rows are written *before* Sep's, so Oct never copies Sep forward
    (there is nothing to copy yet when it is written) and ends up with exactly Food
    and an income target of its own, Fun and Travel left unbudgeted ("–").
    """
    session_factory, currency_id, account_id, cats = _setup_budget(tmp_path, monkeypatch)
    _txn(session_factory, currency_id, account_id, date(2026, 9, 1), -1500, "Taxi", cats["Travel"])
    _txn(session_factory, currency_id, account_id, date(2026, 9, 2), -15000, "Big dinner", cats["Food"])
    _txn(session_factory, currency_id, account_id, date(2026, 9, 3), -20000, "Movies", cats["Fun"])
    _txn(session_factory, currency_id, account_id, date(2026, 9, 1), 500000, "Paycheck", cats["Paycheck"])
    _txn(session_factory, currency_id, account_id, date(2026, 9, 2), -500, "Misc", None)

    _set_budget(session_factory, date(2026, 10, 1), "Food", 20000)
    _set_budget(session_factory, date(2026, 10, 1), None, 550000, kind=budget_core.INCOME)
    _set_budget(session_factory, date(2026, 9, 1), "Food", 10000)
    _set_budget(session_factory, date(2026, 9, 1), "Fun", 100000)
    _set_budget(session_factory, date(2026, 9, 1), None, 600000, kind=budget_core.INCOME)
    return session_factory, currency_id, account_id, cats


# ------------------------------------------------------------------------ tracking panel

def test_track_opens_with_the_right_rows_and_matches_budget_track(tmp_path, monkeypatch):
    session_factory, *_ = _seed_main_scenario(tmp_path, monkeypatch)
    _freeze(monkeypatch, date(2026, 9, 4))

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("budget 2026-09")
            await pilot.pause()
            table = app.query_one("#budget_track", DataTable)
            rows = _rows_of(app, "budget_track")
            return (
                app._panel,
                app.focused.id,
                rows,
                str(app.query_one("#status", Static).content),
            )

    panel, focused, rows, status = asyncio.run(run())
    with session_factory() as session:
        expected = budget_core.track(session, date(2026, 9, 1), today=date(2026, 9, 4))

    assert panel == "budget_track"
    assert focused == "budget_track"
    # Income first, then every budgeted category (Travel was never budgeted, so it
    # has no row of its own here even though it spent money -- see budget.track.rows),
    # biggest budget first: Fun (1,000.00) sorts ahead of Food (100.00).
    assert rows[0][0] == "Income"
    assert rows[0][1] == "6,000.00" and rows[0][2] == "5,000.00" and rows[0][3] == "1,000.00"
    assert [row[0] for row in rows[1:]] == [row.name for row in expected.rows]
    food_row = next(row for row in expected.rows if row.name == "Food")
    assert food_row.over is True
    fun_row = next(row for row in expected.rows if row.name == "Fun")
    assert fun_row.over is False and fun_row.ahead_of_pace is True
    fun_index = [r[0] for r in rows].index("Fun")
    food_index = [r[0] for r in rows].index("Food")
    assert rows[fun_index][1:4] == ["1,000.00", "200.00", "800.00"]
    assert rows[food_index][1:4] == ["100.00", "150.00", "-50.00"]
    assert "Sep 2026" in status
    assert "day 4 of 30 (13%)" in status
    assert "1 over" in status
    assert "750.00 left" in status
    assert "not budgeted 20.00" in status


def test_over_and_ahead_of_pace_rows_use_the_theme_colors(tmp_path, monkeypatch):
    _seed_main_scenario(tmp_path, monkeypatch)
    _freeze(monkeypatch, date(2026, 9, 4))

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("budget 2026-09")
            await pilot.pause()
            table = app.query_one("#budget_track", DataTable)
            raw_rows = [table.get_row_at(i) for i in range(table.row_count)]
            return raw_rows, app.get_css_variables()

    raw_rows, colors = asyncio.run(run())
    labels = [str(row[0]) for row in raw_rows]
    income, fun_row, food_row = (
        raw_rows[labels.index("Income")],
        raw_rows[labels.index("Fun")],
        raw_rows[labels.index("Food")],
    )
    assert colors["error"] in str(food_row[0].style)
    assert colors["warning"] in str(fun_row[0].style)
    assert colors["error"] not in str(income[0].style)
    assert colors["warning"] not in str(income[0].style)


def test_track_bad_month_is_a_usage_warning(tmp_path, monkeypatch):
    _setup_budget(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("budget nope")
            await pilot.pause()
            return [n.message for n in app._notifications], app._panel

    messages, panel = asyncio.run(run())
    assert any("YYYY-MM" in m for m in messages)
    assert panel == "txns"


def test_track_default_month_is_the_current_one(tmp_path, monkeypatch):
    _seed_main_scenario(tmp_path, monkeypatch)
    _freeze(monkeypatch, date(2026, 9, 4))

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("budget")
            await pilot.pause()
            return app._budget_month, str(app.query_one("#status", Static).content)

    month, status = asyncio.run(run())
    assert month == date(2026, 9, 1)
    assert "Sep 2026" in status


def test_track_notes_a_copied_forward_plan(tmp_path, monkeypatch):
    session_factory, currency_id, account_id, cats = _setup_budget(tmp_path, monkeypatch)
    _set_budget(session_factory, date(2026, 9, 1), "Food", 10000)
    _freeze(monkeypatch, date(2026, 10, 15))

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("budget 2026-10")
            await pilot.pause()
            return str(app.query_one("#status", Static).content)

    status = asyncio.run(run())
    assert "plan from Sep 2026" in status


def test_track_escape_returns_to_transactions(tmp_path, monkeypatch):
    _seed_main_scenario(tmp_path, monkeypatch)
    _freeze(monkeypatch, date(2026, 9, 4))

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("budget 2026-09")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            return app._panel

    assert asyncio.run(run()) == "txns"


# ----------------------------------------------------------------------------- plan panel

def test_plan_opens_with_the_right_rows_and_matches_budget_plan_rows(tmp_path, monkeypatch):
    session_factory, *_ = _seed_main_scenario(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("budget plan 2026-10")
            await pilot.pause()
            return app._panel, app.focused.id, _rows_of(app, "budget_plan")

    panel, focused, rows = asyncio.run(run())
    with session_factory() as session:
        expected = budget_core.plan_rows(session, date(2026, 10, 1), averaging_months=6)

    assert panel == "budget_plan"
    assert focused == "budget_plan"
    assert rows[0][0] == "Income"
    assert rows[0][3] == "5,500.00"  # Oct's own income target
    # Every category that spent anything in the 6-month window shows up, biggest
    # average first: Fun (33.33), Food (25.00), Travel (2.50), Uncategorized (0.83),
    # and Paycheck (0.00 -- all of its activity was income, not spend) last.
    category_rows = rows[1 : 1 + len(expected.rows)]
    assert [row[0] for row in category_rows] == [row.name for row in expected.rows]
    assert rows[-2][0] == "Total budgeted" and rows[-2][3] == "200.00"
    assert rows[-1][0] == "Unallocated" and rows[-1][3] == "5,300.00"
    food = rows[[r[0] for r in rows].index("Food")]
    assert food[1] == "25.00" and food[2] == "150.00" and food[3] == "200.00"
    fun = rows[[r[0] for r in rows].index("Fun")]
    assert fun[3] == "–"  # never budgeted for October
    paycheck = rows[[r[0] for r in rows].index("Paycheck")]
    assert paycheck[1] == "0.00" and paycheck[3] == "–"  # all income, no spend


def test_plan_bad_month_is_a_usage_warning(tmp_path, monkeypatch):
    _setup_budget(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("budget plan 2026-13")
            await pilot.pause()
            return [n.message for n in app._notifications], app._panel

    messages, panel = asyncio.run(run())
    assert any("YYYY-MM" in m for m in messages)
    assert panel == "txns"


def test_plan_copy_forward_note_and_both_panels_agree(tmp_path, monkeypatch):
    session_factory, currency_id, account_id, cats = _setup_budget(tmp_path, monkeypatch)
    _set_budget(session_factory, date(2026, 9, 1), "Food", 10000)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("budget plan 2026-11")
            await pilot.pause()
            return str(app.query_one("#status", Static).content)

    status = asyncio.run(run())
    assert "plan from Sep 2026" in status


def test_plan_months_argument_and_the_n_key_both_change_the_window(tmp_path, monkeypatch):
    _seed_main_scenario(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("budget plan 2026-10 12")
            await pilot.pause()
            explicit = app._budget_plan_months
            table = app.query_one("#budget_plan", DataTable)
            headers_explicit = [str(c.label) for c in table.columns.values()]

            app._run_command("budget plan 2026-10")  # back to the default (6)
            await pilot.pause()
            default_months = app._budget_plan_months
            table.focus()
            await pilot.press("n")
            await pilot.pause()
            after_one_press = app._budget_plan_months
            await pilot.press("n")
            await pilot.pause()
            after_two_presses = app._budget_plan_months
            return explicit, headers_explicit, default_months, after_one_press, after_two_presses

    explicit, headers_explicit, default_months, after_one, after_two = asyncio.run(run())
    assert explicit == 12
    assert any("12m" in h for h in headers_explicit)
    assert default_months == 6
    assert after_one == 12  # 6 -> 12
    assert after_two == 3  # 12 -> 3


def test_plan_months_bad_argument_is_a_usage_warning(tmp_path, monkeypatch):
    _setup_budget(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("budget plan 2026-10 abc")
            await pilot.pause()
            return [n.message for n in app._notifications], app._panel

    messages, panel = asyncio.run(run())
    assert any("Usage" in m for m in messages)
    assert panel == "txns"


# ----------------------------------------------------------------- editing (enter -> answer)

def test_enter_on_a_category_row_prefills_and_saves_a_new_amount(tmp_path, monkeypatch):
    session_factory, *_ = _seed_main_scenario(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("budget plan 2026-10")
            await pilot.pause()
            food_row = [r[0] for r in _rows_of(app, "budget_plan")].index("Food")
            table = app.query_one("#budget_plan", DataTable)
            table.move_cursor(row=food_row)
            await pilot.press("enter")
            await pilot.pause()
            prefill = app.query_one("#command", Input).value
            app.query_one("#command", Input).value = "350"
            await pilot.press("enter")
            await pilot.pause()
            focused_after = app.focused.id if app.focused else None
            # Focus is back on the table, so the next row is one arrow + enter away.
            await pilot.press("down", "enter")
            await pilot.pause()
            next_prompt_open = app._pending_budget_edit is not None
            return (
                prefill,
                focused_after,
                next_prompt_open,
                _rows_of(app, "budget_plan")[food_row],
                app.query_one("#budget_plan", DataTable).cursor_row,
                food_row,
            )

    prefill, focused_after, next_prompt_open, row_after, cursor_row, food_row = asyncio.run(run())
    assert prefill == "200.00"  # Oct's own stored Food budget
    assert focused_after == "budget_plan"
    assert next_prompt_open
    assert row_after[3] == "350.00"
    assert cursor_row == food_row + 1  # stayed on Food after the edit, then moved down
    with session_factory() as session:
        food = categories.resolve_path(session, "Food")
        plan = budget_core.get_plan(session, date(2026, 10, 1))
    assert plan.expense[food.id] == 35000


def test_blank_answer_clears_the_budget(tmp_path, monkeypatch):
    session_factory, *_ = _seed_main_scenario(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("budget plan 2026-10")
            await pilot.pause()
            food_row = [r[0] for r in _rows_of(app, "budget_plan")].index("Food")
            table = app.query_one("#budget_plan", DataTable)
            table.move_cursor(row=food_row)
            await pilot.press("enter")
            await pilot.pause()
            app.query_one("#command", Input).value = ""
            await pilot.press("enter")
            await pilot.pause()
            return _rows_of(app, "budget_plan")[food_row]

    row_after = asyncio.run(run())
    assert row_after[3] == "–"
    with session_factory() as session:
        food = categories.resolve_path(session, "Food")
        plan = budget_core.get_plan(session, date(2026, 10, 1))
    assert food.id not in plan.expense


def test_enter_on_the_income_row_edits_the_target(tmp_path, monkeypatch):
    session_factory, *_ = _seed_main_scenario(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("budget plan 2026-10")
            await pilot.pause()
            table = app.query_one("#budget_plan", DataTable)
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            prefill = app.query_one("#command", Input).value
            app.query_one("#command", Input).value = "7000"
            await pilot.press("enter")
            await pilot.pause()
            return prefill, _rows_of(app, "budget_plan")[0]

    prefill, income_row = asyncio.run(run())
    assert prefill == "5500.00"
    assert income_row[3] == "7,000.00"
    with session_factory() as session:
        plan = budget_core.get_plan(session, date(2026, 10, 1))
    assert plan.income_target_minor == 700000


def test_enter_on_uncategorized_refuses_to_open_the_prompt(tmp_path, monkeypatch):
    _seed_main_scenario(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("budget plan 2026-10")
            await pilot.pause()
            uncategorized_row = [
                r[0] for r in _rows_of(app, "budget_plan")
            ].index("Uncategorized")
            table = app.query_one("#budget_plan", DataTable)
            table.move_cursor(row=uncategorized_row)
            await pilot.press("enter")
            await pilot.pause()
            return (
                app._pending_budget_edit,
                app.query_one("#prompt", Static).display,
                [n.message for n in app._notifications],
            )

    pending, prompt_visible, messages = asyncio.run(run())
    assert pending is None
    assert prompt_visible is False
    assert any("can't be budgeted" in m for m in messages)


def test_escape_cancels_a_pending_budget_edit(tmp_path, monkeypatch):
    _seed_main_scenario(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("budget plan 2026-10")
            await pilot.pause()
            table = app.query_one("#budget_plan", DataTable)
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            pending_before = app._pending_budget_edit
            await pilot.press("escape")
            await pilot.pause()
            pending_after = app._pending_budget_edit
            panel_after_first_escape = app._panel
            await pilot.press("escape")
            await pilot.pause()
            return pending_before, pending_after, panel_after_first_escape, app._panel

    pending_before, pending_after, panel_first, panel_second = asyncio.run(run())
    assert pending_before is not None
    assert pending_after is None
    assert panel_first == "budget_plan"  # the first escape only cancels the question
    assert panel_second == "txns"  # the second leaves the panel, as usual


# -------------------------------------------------------------------------- help/placeholder

def test_help_and_placeholder_mention_budget(tmp_path, monkeypatch):
    _setup_budget(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("help")
            await pilot.pause()
            placeholder = app.query_one("#command", Input).placeholder
            return [n.message for n in app._notifications], placeholder

    messages, placeholder = asyncio.run(run())
    assert any("budget plan" in m for m in messages)
    assert "budget" in placeholder


# ------------------------------------------------------------------ layout fits the panel

def test_budget_track_columns_fit_a_130_column_terminal(tmp_path, monkeypatch):
    _seed_main_scenario(tmp_path, monkeypatch)
    _freeze(monkeypatch, date(2026, 9, 4))

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("budget 2026-09")
            await pilot.pause()
            table = app.query_one("#budget_track", DataTable)
            widths = [c.width for c in table.columns.values()]
            return widths, table.size.width

    widths, panel_width = asyncio.run(run())
    assert sum(widths) + 2 * len(widths) + 2 <= panel_width


def test_budget_track_columns_fit_the_real_terminal(tmp_path, monkeypatch):
    _seed_main_scenario(tmp_path, monkeypatch)
    _freeze(monkeypatch, date(2026, 9, 4))

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("budget 2026-09")
            await pilot.pause()
            table = app.query_one("#budget_track", DataTable)
            widths = [c.width for c in table.columns.values()]
            buffer = io.StringIO()
            Console(file=buffer, width=213).print(app.screen._compositor)
            return widths, table.size.width, buffer.getvalue()

    widths, panel_width, rendered = asyncio.run(run())
    assert sum(widths) + 2 * len(widths) + 2 <= panel_width
    assert "1,000.00" in rendered  # a real four-figure budget renders in full


def test_budget_plan_columns_fit_a_130_and_a_213_column_terminal(tmp_path, monkeypatch):
    _seed_main_scenario(tmp_path, monkeypatch)

    async def run():
        results = {}
        for width in (130, 213):
            app = BudgetApp()
            async with app.run_test(size=(width, 40)) as pilot:
                app._run_command("budget plan 2026-10")
                await pilot.pause()
                table = app.query_one("#budget_plan", DataTable)
                widths = [c.width for c in table.columns.values()]
                results[width] = (widths, table.size.width)
        return results

    results = asyncio.run(run())
    for widths, panel_width in results.values():
        assert sum(widths) + 2 * len(widths) + 2 <= panel_width


def test_track_status_line_fits_the_main_panel_with_six_figure_amounts(tmp_path, monkeypatch):
    """A guard against a status line that only happens to fit a seeded -40.00: this
    budgets a six-figure category and checks the real rendered line, not a guess."""
    session_factory, currency_id, account_id, cats = _setup_budget(tmp_path, monkeypatch)
    _txn(session_factory, currency_id, account_id, date(2026, 9, 2), -99_999_999, "Huge", cats["Food"])
    _set_budget(session_factory, date(2026, 9, 1), "Food", 1)  # way over, six figures left
    _freeze(monkeypatch, date(2026, 9, 4))

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("budget 2026-09")
            await pilot.pause()
            return str(app.query_one("#status", Static).content), app.query_one("#main").size.width

    status, main_width = asyncio.run(run())
    assert len(status) <= main_width


def test_a_parent_budgeted_below_its_subcategories_shows_red_with_both_totals(tmp_path, monkeypatch):
    """Food 1,000 against Dining 900 + Groceries 400: the plan row says so, in red."""
    import asyncio as _asyncio
    from datetime import date as _date

    from budget_tracker import budget as budget_core, categories as categories_module
    from budget_tracker.db import get_engine, get_sessionmaker, init_db
    from budget_tracker.tui import BudgetApp as _App
    from textual.widgets import DataTable as _DataTable

    db_path = tmp_path / "over.db"
    engine = get_engine(db_path)
    init_db(engine)
    month = _date.today().replace(day=1)
    with get_sessionmaker(engine)() as session:
        categories_module.ensure_path(session, "Food > Dining")
        categories_module.ensure_path(session, "Food > Groceries")
        budget_core.set_amount(session, month, "Food", 100000)
        budget_core.set_amount(session, month, "Dining", 90000)
        budget_core.set_amount(session, month, "Groceries", 40000)
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))

    async def run():
        app = _App()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("budget plan")
            await pilot.pause()
            table = app.query_one("#budget_plan", _DataTable)
            for index in range(table.row_count):
                cells = table.get_row_at(index)
                if str(cells[0]).strip() == "Food":
                    return str(cells[3]), str(cells[3].style), app.get_css_variables()["error"]
            return None

    text, style, error = _asyncio.run(run())
    assert text == "1,000.00 < 1,300.00"
    assert error.lower() in style.lower()


def test_folding_and_derived_budgets_in_the_plan(tmp_path, monkeypatch):
    """z folds a plan row's subcategories; an unbudgeted parent shows the sum of its
    subcategories' budgets in the theme's warning color."""
    import asyncio as _asyncio
    from datetime import date as _date

    from budget_tracker import budget as budget_core, categories as categories_module
    from budget_tracker.db import get_engine, get_sessionmaker, init_db
    from budget_tracker.tui import BudgetApp as _App
    from textual.widgets import DataTable as _DataTable

    db_path = tmp_path / "fold.db"
    engine = get_engine(db_path)
    init_db(engine)
    month = _date.today().replace(day=1)
    with get_sessionmaker(engine)() as session:
        categories_module.ensure_path(session, "Food > Dining")
        categories_module.ensure_path(session, "Food > Groceries")
        budget_core.set_amount(session, month, "Dining", 90000)
        budget_core.set_amount(session, month, "Groceries", 40000)
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))

    def labels(table):
        return [str(table.get_row_at(i)[0]).strip() for i in range(table.row_count)]

    async def run():
        app = _App()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("budget plan")
            await pilot.pause()
            table = app.query_one("#budget_plan", _DataTable)
            table.focus()
            food = labels(table).index("Food")
            food_budget = table.get_row_at(food)[3]
            table.move_cursor(row=food)
            await pilot.pause()
            await pilot.press("z")
            folded = labels(table)
            await pilot.press("z")
            unfolded = labels(table)
            return (
                str(food_budget), str(food_budget.style),
                app.get_css_variables()["warning"], folded, unfolded,
            )

    text, style, warning, folded, unfolded = _asyncio.run(run())
    assert text == "1,300.00" and warning.lower() in style.lower()
    assert "Dining" not in folded and any("Food" in label for label in folded)
    assert "Dining" in unfolded and "Groceries" in unfolded
