"""Tests for tui/pie.py: the pie panel's wedges, legend, and status line."""

from __future__ import annotations

import asyncio
import datetime
import io

from rich.console import Console
from textual.widgets import Static

from budget_tracker import categories, charts, stats
from budget_tracker.models import Account, Currency, Transaction
from budget_tracker.tui import BudgetApp

from conftest import (
    CHART_WINDOW,
    _seed_category_hierarchy,
    _seed_category_hierarchy_with_income_sibling,
    _setup_chart,
)


def test_pie_with_a_period_skips_the_picker(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"pie {CHART_WINDOW}")
            await pilot.pause()
            return app._panel, app.window.start, app.window.end

    panel, start, end = asyncio.run(run())
    assert panel == "pie"
    assert (start, end) == (datetime.date(2026, 1, 1), datetime.date(2026, 3, 31))


def test_pie_only_shows_depth_zero_categories_with_net_spend(tmp_path, monkeypatch):
    """Food (depth 0, real net spend) gets a wedge; Dining/Groceries (its descendants,
    already rolled into Food) do not, and neither does Income (depth 0 but net
    positive) — the exact filter stats.Report.categories warns about."""
    _seed_category_hierarchy_with_income_sibling(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("pie 2025-01-01..2025-12-31")
            await pilot.pause()
            return [s.name for s in app._pie.slices]

    names = asyncio.run(run())
    assert names == ["Food"]


def test_a_window_with_no_net_spending_shows_a_message_instead_of_a_blank_pie(
    tmp_path, monkeypatch
):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("pie 2020-01-01..2020-03-31")  # nothing seeded that early
            await pilot.pause()
            return app._pie.slices, str(app.query_one("#pie", Static).content)

    slices, content = asyncio.run(run())
    assert slices == []
    assert "No net spending" in content


def test_escape_leaves_the_pie_for_the_transactions(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"pie {CHART_WINDOW}")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            return app._panel, app.query_one("#pie", Static).display

    panel, pie_visible = asyncio.run(run())
    assert panel == "txns"
    assert pie_visible is False


def test_selecting_a_category_rescopes_the_open_pie(tmp_path, monkeypatch):
    """Like the chart, the pie is scoped by whatever filters are active — filtering to
    one category leaves a single, whole-circle wedge behind."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"pie {CHART_WINDOW}")
            await pilot.pause()
            everything = [s.name for s in app._pie.slices]

            food = next(c for c in app._categories if c.name == "Food")
            app.category_filter = food.id
            app.reload()
            await pilot.pause()
            return everything, app._pie.slices

    everything, food_slices = asyncio.run(run())
    assert set(everything) == {"Food", "Travel"}
    assert [s.name for s in food_slices] == ["Food"]
    assert food_slices[0].share == 1.0


def test_pie_status_line_fits_the_main_panel(tmp_path, monkeypatch):
    """Same 92-column budget as every other status line, against a custom range (the
    longest label) and a real year of spending in the hundreds of thousands."""
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("pie 2025-01-01..2025-12-31")
            await pilot.pause()
            return str(app.query_one("#status", Static).content)

    status = asyncio.run(run())
    assert len(status) <= 92, f"{len(status)} columns: {status!r}"
    assert "category" in status and "net spend" in status


def test_pie_panel_looks_like_a_circle_and_fits_the_main_panel(tmp_path, monkeypatch):
    """Renders the real compositor rather than assuming the geometry reads as round —
    checks every legend column lands inside the panel, with several categories, a long
    name, and a six-figure amount, the shape most likely to clip."""
    session_factory = _setup_chart(tmp_path, monkeypatch)
    with session_factory() as session:
        currency = session.query(Currency).one()
        account = session.query(Account).one()
        big = categories.ensure_path(
            session, "Restaurants and Fast Casual Spots Somewhere Nearby"
        )
        session.add(
            Transaction(
                account_id=account.id,
                currency_id=currency.id,
                category_id=big.id,
                posted_date=datetime.date(2026, 1, 10),
                description="BIG SPEND",
                raw_description="BIG SPEND",
                value_minor=-123_456_789,
                import_hash="big-spend",
            )
        )
        session.commit()

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"pie {CHART_WINDOW}")
            await pilot.pause()
            buffer = io.StringIO()
            Console(file=buffer, width=130).print(app.screen._compositor)
            return buffer.getvalue(), [s.name for s in app._pie.slices]

    rendered, names = asyncio.run(run())
    assert all(len(line.rstrip()) <= 130 for line in rendered.splitlines())
    assert set(names) == {"Food", "Travel", "Restaurants and Fast Casual Spots Somewhere Nearby"}
    # The pie itself drew something...
    assert charts.BLOCK in rendered
    # ...and the legend beside it names the categories, their shares, and their
    # amounts, none of it clipped off the edge of the panel.
    assert "Food" in rendered and "Travel" in rendered
    assert "Restaurants and Fas" in rendered  # the long name, truncated to fit the column
    assert "1,234,567.89" in rendered  # a six-figure amount, formatted not raw


def test_a_new_filter_does_not_disturb_an_open_pie_until_reload(tmp_path, monkeypatch):
    """Sanity check that the pie panel survives reload() being called for reasons that
    have nothing to do with it (e.g. importing more data) without erroring."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"pie {CHART_WINDOW}")
            await pilot.pause()
            app.reload()
            await pilot.pause()
            return app._panel, [s.name for s in app._pie.slices]

    panel, names = asyncio.run(run())
    assert panel == "pie"
    assert set(names) == {"Food", "Travel"}
