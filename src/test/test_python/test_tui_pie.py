"""Tests for tui/pie.py: the pie panel's top bar, per-bucket bars, shared legend, and
status line -- category share over time, not a circle (see its own module docstring
for why the command is still called `pie`)."""

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


def _pie_content(app):
    return app.query_one("#pie", Static).content


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


def test_pie_defaults_to_the_monthly_bucket(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"pie {CHART_WINDOW}")
            await pilot.pause()
            return app._pie_bucket, [bar.label for bar in app._pie.bars]

    bucket, labels = asyncio.run(run())
    assert bucket == "month"
    assert labels == ["2026-01", "2026-02", "2026-03"]


def test_b_cycles_week_month_year_and_never_offers_day(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"pie {CHART_WINDOW}")
            await pilot.pause()
            assert app.focused is not None and app.focused.id == "pie"
            seen = [app._pie_bucket]
            for _ in range(4):
                await pilot.press("b")
                await pilot.pause()
                seen.append(app._pie_bucket)
            return seen

    seen = asyncio.run(run())
    assert seen == ["month", "year", "week", "month", "year"]
    assert "day" not in seen


def test_the_pie_keys_are_inert_outside_the_pie_panel(tmp_path, monkeypatch):
    """'b' is a plain letter, so it must stay typeable in the command bar once the pie
    panel is left -- the same guarantee test_the_chart_keys_are_inert_outside_the_chart
    already gives the chart panel."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"pie {CHART_WINDOW}")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("b")
            await pilot.pause()
            return app._panel, app._pie_bucket, app.query_one("#command").value

    panel, bucket, typed = asyncio.run(run())
    assert panel == "txns" and bucket == "month"
    assert typed == "b"


def test_pie_only_shows_depth_zero_categories_with_net_spend(tmp_path, monkeypatch):
    """Food (depth 0, real net spend) gets a segment; Dining/Groceries (its
    descendants, already rolled into Food) do not, and neither does Income (depth 0
    but net positive) -- the exact filter stats.Report.categories warns about."""
    _seed_category_hierarchy_with_income_sibling(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("pie 2025-01-01..2025-12-31")
            await pilot.pause()
            return [s.name for s in app._pie.segments]

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
            return app._pie.segments, str(_pie_content(app))

    segments, content = asyncio.run(run())
    assert segments == []
    assert "No net spending" in content


def test_a_window_that_is_all_income_shows_the_message_too(tmp_path, monkeypatch):
    """Net-positive rows contribute 0.0 share by construction (see CategoryStat.share),
    so a window holding only a paycheck is exactly as empty as one with no
    transactions at all."""
    _seed_category_hierarchy_with_income_sibling(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("pie 2025-03-04..2025-03-04")  # just the Paycheck day
            await pilot.pause()
            return app._pie.segments, str(_pie_content(app))

    segments, content = asyncio.run(run())
    assert segments == []
    assert "No net spending" in content


def test_a_single_category_at_100_percent(tmp_path, monkeypatch):
    """January only has Food transactions, so the top bar -- and its one bucket -- are
    a single, whole-width segment."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("pie 2026-01-01..2026-01-31")
            await pilot.pause()
            return app._pie, str(_pie_content(app))

    chart, content = asyncio.run(run())
    assert [s.name for s in chart.segments] == ["Food"]
    assert chart.segments[0].share == 1.0
    assert chart.top.segments[0].cells == chart.top.width
    assert "100.0%" in content


def test_a_bucket_with_no_spending_is_an_empty_row_not_a_missing_one(tmp_path, monkeypatch):
    """April has nothing in it -- CHART_ROWS ends in March -- so it must still get a
    row, just an empty one, the same way a quiet month on the chart is drawn rather
    than skipped."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("pie 2026-01-01..2026-04-30")
            await pilot.pause()
            return app._pie.bars, str(_pie_content(app))

    bars, content = asyncio.run(run())
    labels = [bar.label for bar in bars]
    assert labels == ["2026-01", "2026-02", "2026-03", "2026-04"]
    april = bars[labels.index("2026-04")]
    assert april.total_minor == 0
    assert april.segments == []
    assert "2026-04" in content  # the row itself is drawn


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
    """Like the chart, the pie is scoped by whatever filters are active -- filtering to
    one category leaves a single, whole-window segment behind.

    The unfiltered window also carries a zero-share ``Other`` placeholder: February's
    lone uncategorised charge is real spend *within that bucket*, even though the
    window-wide Uncategorised row nets positive once March's paycheck is added in (see
    charts.build_stacked_share's ``needs_other`` guard) -- so the legend has to make
    room for a bucket that would otherwise have nowhere to put it.
    """
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"pie {CHART_WINDOW}")
            await pilot.pause()
            everything = [s.name for s in app._pie.segments]

            food = next(c for c in app._categories if c.name == "Food")
            app.category_filter = food.id
            app.reload()
            await pilot.pause()
            return everything, app._pie.segments

    everything, food_segments = asyncio.run(run())
    assert set(everything) == {"Food", "Travel", "Other"}
    assert [s.name for s in food_segments] == ["Food"]
    assert food_segments[0].share == 1.0


def test_every_bar_uses_the_same_colors_in_the_same_order(tmp_path, monkeypatch):
    """Food gets the whole of September and Travel the whole of October, so their bars
    are pure colors -- and the color the top bar drew for each has to be the exact one
    its own bucket bar uses, or the shared legend stops meaning anything."""
    db_path = tmp_path / "colors.db"
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
        food = categories.ensure_path(session, "Food")
        travel = categories.ensure_path(session, "Travel")
        session.add(
            Transaction(
                account_id=account.id, currency_id=currency.id, category_id=food.id,
                posted_date=datetime.date(2025, 9, 5), description="A",
                raw_description="A", value_minor=-100_000, import_hash="colors-a",
            )
        )
        session.add(
            Transaction(
                account_id=account.id, currency_id=currency.id, category_id=travel.id,
                posted_date=datetime.date(2025, 10, 5), description="B",
                raw_description="B", value_minor=-50_000, import_hash="colors-b",
            )
        )
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("pie 2025-09-01..2025-10-31")
            await pilot.pause()
            return _pie_content(app), [s.name for s in app._pie.segments]

    content, names = asyncio.run(run())
    # Food is the bigger share, so it is listed -- and drawn -- first.
    assert names == ["Food", "Travel"]

    console = Console()
    lines = content.split("\n")
    top_line, september_line, october_line = lines[0], lines[2], lines[3]

    food_col = top_line.plain.find(charts.BLOCK)
    travel_col = top_line.plain.rfind(charts.BLOCK)
    food_color = top_line.get_style_at_offset(console, food_col)
    travel_color = top_line.get_style_at_offset(console, travel_col)
    assert food_color != travel_color

    sept_col = september_line.plain.find(charts.BLOCK)
    oct_col = october_line.plain.find(charts.BLOCK)
    assert september_line.get_style_at_offset(console, sept_col) == food_color
    assert october_line.get_style_at_offset(console, oct_col) == travel_color


def test_other_is_a_fixed_neutral_color_not_a_turn_in_the_cycle(tmp_path, monkeypatch):
    """A tiny sliver of a third category folds into Other; Other must not borrow the
    next hue in the cycle, since it stands for a category, not for one."""
    db_path = tmp_path / "other_color.db"
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
        big = categories.ensure_path(session, "Big")
        tiny = categories.ensure_path(session, "Tiny")
        # Tiny's 3% share is under MIN_SEGMENT_CELLS/width (2/54 ~= 3.7%) so it folds
        # into Other, and apportions Other 2 cells of the 54 -- enough to render, not
        # so much it dominates the read.
        session.add(
            Transaction(
                account_id=account.id, currency_id=currency.id, category_id=big.id,
                posted_date=datetime.date(2025, 9, 5), description="A",
                raw_description="A", value_minor=-97_000, import_hash="other-a",
            )
        )
        session.add(
            Transaction(
                account_id=account.id, currency_id=currency.id, category_id=tiny.id,
                posted_date=datetime.date(2025, 9, 6), description="B",
                raw_description="B", value_minor=-3_000, import_hash="other-b",
            )
        )
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("pie 2025-09-01..2025-09-30")
            await pilot.pause()
            return _pie_content(app), app._pie

    content, chart = asyncio.run(run())
    assert [s.name for s in chart.segments] == ["Big", "Other"]
    console = Console()
    lines = content.split("\n")
    top_line = lines[0]
    other_style = top_line.get_style_at_offset(console, top_line.plain.rfind(charts.BLOCK))
    assert other_style.color.name == "#999999"
    assert "Other" in str(content)


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
    assert "category" in status and "net spend" in status and "month" in status


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


def test_pie_panel_renders_a_top_bar_and_bucket_rows_and_fits_the_main_panel(
    tmp_path, monkeypatch
):
    """Renders the real compositor rather than assuming the layout reads cleanly --
    checks the top bar, every bucket row, and the legend all land inside the panel,
    with a long category name and a six-figure total, the shape most likely to clip."""
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
            return buffer.getvalue(), [s.name for s in app._pie.segments]

    rendered, names = asyncio.run(run())
    assert all(len(line.rstrip()) <= 130 for line in rendered.splitlines())
    # Food and Travel's few dollars are dwarfed by the six-figure BIG SPEND and fold
    # into Other -- see charts.build_share_bar's MIN_SEGMENT_CELLS folding.
    assert set(names) == {
        "Restaurants and Fast Casual Spots Somewhere Nearby", "Other",
    }
    assert charts.BLOCK in rendered
    assert "2026-01" in rendered and "2026-02" in rendered and "2026-03" in rendered
    assert "Restaurants and Fas" in rendered  # the long name, truncated to fit the legend
    # The six-figure BIG SPEND plus CHART_ROWS's own Food/Travel dollars, formatted
    # not raw.
    assert "1,234,637.89" in rendered


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
            return app._panel, [s.name for s in app._pie.segments]

    panel, names = asyncio.run(run())
    assert panel == "pie"
    # See test_selecting_a_category_rescopes_the_open_pie for why Other is here too.
    assert set(names) == {"Food", "Travel", "Other"}
