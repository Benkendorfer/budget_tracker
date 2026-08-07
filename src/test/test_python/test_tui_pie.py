"""Tests for tui/pie.py: the pie panel's wedges, legend, and status line."""

from __future__ import annotations

import asyncio
import datetime
import io

from rich.console import Console
from textual.widgets import Static

from dataclasses import replace

from budget_tracker import categories, charts, stats
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.models import Account, Currency, Transaction
from budget_tracker.tui import UNCONVERTED_MARK, BudgetApp

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


def _seed_pie_with_an_unconvertible_currency(tmp_path, monkeypatch):
    """A USD row and a CHF row in the same window, with no CHF rate cached -- so the
    pie's own report has something genuinely unconverted to report."""
    db_path = tmp_path / "pie_multi.db"
    engine = get_engine(db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        usd = Currency(value="USD", symbol="$", decimal_places=2)
        chf = Currency(value="CHF", symbol="CHF", decimal_places=2)
        session.add_all([usd, chf])
        session.flush()
        checking = Account(name="Checking", currency_id=usd.id)
        swiss = Account(name="Swiss", currency_id=chf.id)
        session.add_all([checking, swiss])
        session.flush()
        session.add(
            Transaction(
                account_id=checking.id, currency_id=usd.id,
                posted_date=datetime.date(2025, 6, 1), description="US",
                raw_description="US", value_minor=-1000, import_hash="pm-us",
            )
        )
        session.add(
            Transaction(
                account_id=swiss.id, currency_id=chf.id,
                posted_date=datetime.date(2025, 6, 2), description="CH",
                raw_description="CH", value_minor=-2000, import_hash="pm-ch",
            )
        )
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def test_pie_status_line_shows_the_unconverted_count(tmp_path, monkeypatch):
    """The CHF row has no cached rate, so it is missing from the wedges -- and now says
    so, the same way the other three status lines do."""
    _seed_pie_with_an_unconvertible_currency(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("pie 2025-06-01..2025-06-30")
            await pilot.pause()
            return app._pie_status()

    status = asyncio.run(run())
    assert f"{UNCONVERTED_MARK} 1 " in status


def test_pie_status_line_shows_unconverted_marker_and_still_fits(tmp_path, monkeypatch):
    """Same 92-column budget as every other status line, stressing unconverted_count
    instead of transfer_count -- a different reason money can be missing."""
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("pie 2025-01-01..2025-12-31")
            await pilot.pause()
            width = app.query_one("#status", Static).size.width
            real = app._pie_status()
            app._report = replace(app._report, transfer_count=0, unconverted_count=999)
            worst_case = app._pie_status()
            return real, worst_case, width

    real, worst_case, width = asyncio.run(run())
    assert len(real) <= width - 2
    assert len(worst_case) <= width - 2
    assert UNCONVERTED_MARK in worst_case and UNCONVERTED_MARK not in real


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
