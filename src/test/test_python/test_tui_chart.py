"""Tests for tui/chart.py: the bar chart's measures, buckets, total row, status line,
and drilling down from a bar into that bucket's transactions."""

from __future__ import annotations

import asyncio
import datetime
import io

from rich.console import Console
from textual.widgets import DataTable, Input, Static

from budget_tracker import stats, transfers
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.models import Account, Currency, Transaction
from budget_tracker.tui import TRANSFER_MARK, UNCONVERTED_MARK, BudgetApp

from conftest import CHART_WINDOW, HALF, _chart_headers, _chart_rows, _setup_chart


def test_the_chart_defaults_to_net_drawn_either_side_of_the_axis(tmp_path, monkeypatch):
    """January and February cost money and grow left; March took in far more than it
    spent, so it grows right. That sign is the whole reason net is the default."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            return _chart_rows(app), app._panel, app._measure, app.focused.id

    rows, panel, measure, focused = asyncio.run(run())
    assert (panel, measure, focused) == ("chart", "net", "chart")
    assert [row[0] for row in rows[:3]] == ["2026-01", "2026-02", "2026-03"]
    # March is the peak (+2,960.00) and fills its whole side.
    assert rows[2][1] == " " * HALF + "│" + "█" * HALF
    # January (-30.00) is a rounding sliver on the other side of the axis.
    assert rows[0][1] == " " * (HALF - 1) + "█" + "│" + " " * HALF
    assert [row[2] for row in rows[:3]] == ["-30.00", "-5.00", "2,960.00"]


def test_the_net_axis_lines_up_on_every_row(tmp_path, monkeypatch):
    """A diverging chart whose centre wanders is not readable as a chart."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            return [row[1] for row in _chart_rows(app)[:3]]

    bars = asyncio.run(run())
    assert all(len(bar) == 2 * HALF + 1 for bar in bars)
    assert all(bar[HALF] == "│" for bar in bars)


def test_a_bucket_that_came_out_even_sits_on_the_axis(tmp_path, monkeypatch):
    """200.00 spent and 200.00 refunded is not a heavy month, and must not draw as one."""
    session_factory = _setup_chart(tmp_path, monkeypatch)
    with session_factory() as session:
        currency = session.query(Currency).one()
        account = session.query(Account).one()
        for amount, description in ((-20_000, "BIG BUY"), (20_000, "REFUNDED")):
            session.add(
                Transaction(
                    account_id=account.id,
                    currency_id=currency.id,
                    posted_date=datetime.date(2026, 2, 20),
                    description=description,
                    raw_description=description,
                    value_minor=amount,
                    import_hash=f"even-{description}",
                )
            )
        session.commit()

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month spending")
            await pilot.pause()
            spending = _chart_rows(app)
            app._run_command("chart net")
            await pilot.pause()
            return spending, _chart_rows(app)

    spending, net = asyncio.run(run())
    # Charted as spending, February is now the biggest month of the three.
    assert spending[1][2] == "205.00"
    assert spending[1][1] == "█" * 27
    # Charted as net, it is a sliver: the refund cancelled almost all of it.
    assert net[1][2] == "-5.00"
    assert net[1][1].count("█") == 1


def test_the_measure_key_cycles_net_spending_income(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            app.query_one("#chart", DataTable).focus()
            seen = [(app._measure, _chart_headers(app)[1])]
            for _ in range(3):
                await pilot.press("m")
                await pilot.pause()
                seen.append((app._measure, _chart_headers(app)[1]))
            return seen

    seen = asyncio.run(run())
    assert [measure for measure, _header in seen] == ["net", "spend", "income", "net"]
    # The header names what is being drawn, and the net one explains its own axis.
    assert seen[0][1] == "Net (← out | in →)"
    assert seen[1][1] == "Spending"
    assert seen[2][1] == "Income"


def test_the_figure_columns_follow_the_measure(tmp_path, monkeypatch):
    """The charted measure comes first, then whatever is most worth seeing beside it."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            headers, rows = {}, {}
            for measure in ("net", "spending", "income"):
                app._run_command(f"chart {CHART_WINDOW} month {measure}")
                await pilot.pause()
                headers[measure] = _chart_headers(app)
                rows[measure] = _chart_rows(app)
            return headers, rows

    headers, rows = asyncio.run(run())
    assert headers["net"] == ["Period", "Net (← out | in →)", "Net", "Out", "Txns"]
    assert headers["spending"] == ["Period", "Spending", "Out", "Net", "Txns"]
    assert headers["income"] == ["Period", "Income", "In", "Net", "Txns"]
    # March: 40.00 out, 3,000.00 in, 2,960.00 net — the same three figures, reordered.
    assert rows["net"][2][2:4] == ["2,960.00", "40.00"]
    assert rows["spending"][2][2:4] == ["40.00", "2,960.00"]
    assert rows["income"][2][2:4] == ["3,000.00", "2,960.00"]


def test_spending_and_income_bars_are_left_anchored_with_no_axis(tmp_path, monkeypatch):
    """Only net has a sign to encode, so the other two use the conventional bar and the
    full width of the column rather than half of it."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month spending")
            await pilot.pause()
            spending = _chart_rows(app)
            app._run_command("chart income")
            await pilot.pause()
            return spending, _chart_rows(app)

    spending, income = asyncio.run(run())
    assert all("│" not in row[1] for row in spending[:3])
    # March spends the most, so it fills the whole 27-column width.
    assert spending[2][1] == "█" * 27
    assert spending[0][1] == "█" * 20 + "▎"  # 30.00 of 40.00
    # Income lands only in March, so the other two months are empty.
    assert income[2][1] == "█" * 27
    assert [income[0][1], income[1][1]] == ["", ""]
    assert [income[0][2], income[1][2]] == ["0.00", "0.00"]


def test_a_quiet_bucket_is_drawn_empty_rather_than_dropped(tmp_path, monkeypatch):
    """A month with nothing in it must still occupy a row; a chart that silently skips
    it reads as though the calendar itself had no April."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("chart 2026-01-01..2026-04-30 month spending")
            await pilot.pause()
            return _chart_rows(app)

    rows = asyncio.run(run())
    assert [row[0] for row in rows[:4]] == ["2026-01", "2026-02", "2026-03", "2026-04"]
    assert rows[3][1] == ""  # April: present, empty, and not a zero-width lie
    assert rows[3][2] == "0.00"


def test_chart_total_row_follows_the_measure(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            totals = {}
            for measure in ("net", "spending", "income"):
                app._run_command(f"chart {CHART_WINDOW} month {measure}")
                await pilot.pause()
                totals[measure] = _chart_rows(app)[-1]
            return totals

    totals = asyncio.run(run())
    for measure, row in totals.items():
        assert row[0] == "TOTAL", measure
        assert row[4] == "5", measure
    # 3,000.00 in less 75.00 out, and the average is of whatever is charted.
    assert totals["net"][2:4] == ["2,925.00", "75.00"]
    assert totals["net"][1] == "avg 975.00/month"
    assert totals["spending"][2:4] == ["75.00", "2,925.00"]
    assert totals["spending"][1] == "avg 25.00/month"
    assert totals["income"][2:4] == ["3,000.00", "2,925.00"]
    assert totals["income"][1] == "avg 1,000.00/month"


def test_an_empty_window_charts_no_total_row(tmp_path, monkeypatch):
    """Same rule as the statistics table: a row of zeroes reads as a finding."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("chart 2020-01-01..2020-03-31 month")
            await pilot.pause()
            return _chart_rows(app)

    rows = asyncio.run(run())
    assert [row[0] for row in rows] == ["2020-01", "2020-02", "2020-03"]
    assert all("█" not in row[1] for row in rows)


def test_selecting_a_category_rescopes_the_open_chart(tmp_path, monkeypatch):
    """The headline of the feature: the sidebar is how a category is chosen, so the bars
    have to follow it without the user reissuing the command."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month spending")
            await pilot.pause()
            everything = _chart_rows(app)

            food = next(c for c in app._categories if c.name == "Food")
            app.category_filter = food.id
            app.reload()
            await pilot.pause()
            return everything, _chart_rows(app), str(app.query_one("#status", Static).content)

    everything, food_rows, status = asyncio.run(run())
    assert everything[-1][2] == "75.00"
    # Food is January's 20.00 + 10.00 and nothing else.
    assert [row[2] for row in food_rows[:3]] == ["30.00", "0.00", "0.00"]
    assert food_rows[-1][2] == "30.00"
    assert food_rows[0][1] == "█" * 27  # rescaled: January is now the peak
    # And the status line says which category, or the chart is unreadable.
    assert "Food" in status


def test_the_measure_survives_a_new_period_but_the_bucket_is_rederived(
    tmp_path, monkeypatch
):
    """The measure is a question about the money; the bucket is a property of the range.
    Daily bars chosen for one month are unreadable stretched over a year."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("chart 2026-01-01..2026-01-20 income")
            await pilot.pause()
            first = (app._bucket, app._measure)
            app._run_command("chart 2026-01-01..2026-12-31")
            await pilot.pause()
            return first, (app._bucket, app._measure)

    first, second = asyncio.run(run())
    assert first == ("day", "income")
    assert second == ("month", "income")


def test_the_bucket_key_cycles_day_week_month(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            app.query_one("#chart", DataTable).focus()
            seen = [app._bucket]
            for _ in range(3):
                await pilot.press("b")
                await pilot.pause()
                seen.append(app._bucket)
            return seen, len(_chart_rows(app))

    seen, row_count = asyncio.run(run())
    assert seen == ["month", "day", "week", "month"]
    assert row_count == 4  # back to three months and a total


def test_the_chart_keys_are_inert_outside_the_chart(tmp_path, monkeypatch):
    """'b' and 'm' are plain letters, so they must stay typeable everywhere else."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            app._run_command("stats " + CHART_WINDOW)
            await pilot.pause()
            app.query_one("#stats_table", DataTable).focus()
            await pilot.press("b")
            await pilot.press("m")
            await pilot.pause()
            on_stats = (app._bucket, app._measure)

            app.query_one("#command", Input).focus()
            await pilot.press("b", "m")
            await pilot.pause()
            return on_stats, (app._bucket, app._measure), app.query_one("#command", Input).value

    on_stats, after_typing, typed = asyncio.run(run())
    assert on_stats == ("month", "net") and after_typing == ("month", "net")
    assert typed == "bm"  # both reached the command bar as ordinary characters


def test_the_bucket_defaults_to_the_window_length(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            picked = {}
            for spec in (
                "2026-01-01..2026-01-20",
                "2026-01-01..2026-03-01",
                "2026-01-01..2026-12-31",
            ):
                app._run_command(f"chart {spec}")
                await pilot.pause()
                picked[spec] = app._bucket
            return picked

    picked = asyncio.run(run())
    assert picked["2026-01-01..2026-01-20"] == "day"
    assert picked["2026-01-01..2026-03-01"] == "week"
    assert picked["2026-01-01..2026-12-31"] == "month"


def test_a_bucket_or_measure_on_its_own_redraws_the_open_chart(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            app._run_command("chart week")
            await pilot.pause()
            after_bucket = (app._bucket, app._measure)
            app._run_command("chart income")
            await pilot.pause()
            return after_bucket, (app._bucket, app._measure), app.window.start, app.window.end

    after_bucket, after_measure, start, end = asyncio.run(run())
    assert after_bucket == ("week", "net")
    assert after_measure == ("week", "income")  # the bucket it was already using
    assert (start, end) == (datetime.date(2026, 1, 1), datetime.date(2026, 3, 31))


def test_both_words_can_be_given_at_once_in_either_order(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} week spending")
            await pilot.pause()
            first = (app._bucket, app._measure)
            app._run_command(f"chart {CHART_WINDOW} income day")
            await pilot.pause()
            return first, (app._bucket, app._measure)

    first, second = asyncio.run(run())
    assert first == ("week", "spend")
    assert second == ("day", "income")


def test_a_bucket_with_no_window_yet_says_so(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            messages = []
            app.notify = lambda text, **kw: messages.append(text)
            app._run_command("chart week")
            await pilot.pause()
            return messages, app._panel

    messages, panel = asyncio.run(run())
    assert panel == "txns"  # it did not open an empty chart
    assert "No period yet" in messages[0] and "chart 3m week" in messages[0]


def test_chart_status_line_fits_the_main_panel(tmp_path, monkeypatch):
    """Same 92-column budget as every other status line, with a long category name and
    five-figure money in it."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("chart 2020-01-01..2026-12-31 month income")
            await pilot.pause()
            food = next(c for c in app._categories if c.name == "Food")
            app.category_filter = food.id
            app.reload()
            await pilot.pause()
            return str(app.query_one("#status", Static).content)

    status = asyncio.run(run())
    assert len(status) <= 92, f"{len(status)} columns: {status!r}"
    assert "income/month" in status and "Food" in status and "peak" in status


def _seed_chart_with_an_unconvertible_currency(tmp_path, monkeypatch):
    """A USD row and a CHF row inside CHART_WINDOW, with no CHF rate cached -- so the
    chart's own totals have something genuinely unconverted to report."""
    db_path = tmp_path / "chart_multi.db"
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
                posted_date=datetime.date(2026, 2, 1), description="US",
                raw_description="US", value_minor=-1000, import_hash="cm-us",
            )
        )
        session.add(
            Transaction(
                account_id=swiss.id, currency_id=chf.id,
                posted_date=datetime.date(2026, 2, 2), description="CH",
                raw_description="CH", value_minor=-2000, import_hash="cm-ch",
            )
        )
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def test_chart_status_line_shows_the_unconverted_count(tmp_path, monkeypatch):
    """The CHF row has no cached rate, so it is missing from the bars -- and now says
    so, the same way the transactions and statistics status lines do."""
    _seed_chart_with_an_unconvertible_currency(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            return app._chart_status()

    status = asyncio.run(run())
    assert f"{UNCONVERTED_MARK} 1 " in status


def test_chart_status_line_shows_unconverted_marker_and_still_fits(tmp_path, monkeypatch):
    """Same 92-column budget as every other status line, stressing unconverted_count
    instead of transfer_count -- a different reason money can be missing."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("chart 2020-01-01..2026-12-31 month income")
            await pilot.pause()
            width = app.query_one("#status", Static).size.width
            food = next(c for c in app._categories if c.name == "Food")
            app.category_filter = food.id
            app.reload()
            await pilot.pause()
            real = app._chart_status()
            app._chart_unconverted = 999
            worst_case = app._chart_status()
            return real, worst_case, width

    real, worst_case, width = asyncio.run(run())
    assert len(real) <= width - 2
    assert len(worst_case) <= width - 2
    assert UNCONVERTED_MARK in worst_case and UNCONVERTED_MARK not in real


def test_chart_table_fits_the_main_panel(tmp_path, monkeypatch):
    """Every column, the full-width bar included, has to land inside the panel."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month spending")
            await pilot.pause()
            buffer = io.StringIO()
            Console(file=buffer, width=130).print(app.screen._compositor)
            return buffer.getvalue()

    rendered = asyncio.run(run())
    header = next(line for line in rendered.splitlines() if "Period" in line)
    for column in ("Period", "Spending", "Out", "Net", "Txns"):
        assert column in header, f"{column!r} clipped: {header.strip()!r}"
    # The widest bar plus every other column still leaves the panel's right border on
    # screen, so nothing has been pushed off the edge.
    peak_line = next(line for line in rendered.splitlines() if "█" * 27 in line)
    assert len(peak_line.rstrip()) <= 130


def test_the_net_header_survives_its_own_column_width(tmp_path, monkeypatch):
    """The net header carries the legend for the axis, so it is the longest of the three
    and the one that would clip first."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            buffer = io.StringIO()
            Console(file=buffer, width=130).print(app.screen._compositor)
            return buffer.getvalue()

    rendered = asyncio.run(run())
    assert "Net (← out | in →)" in rendered


def test_footer_shows_the_chart_keys(tmp_path, monkeypatch):
    """The footer truncates mid-word, so a new binding has to be checked on the panel it
    actually appears on — see test_footer_shows_every_shortcut_at_a_normal_width."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            app.query_one("#chart", DataTable).focus()
            await pilot.pause()
            buffer = io.StringIO()
            Console(file=buffer, width=130).print(app.screen._compositor)
            return buffer.getvalue()

    rendered = asyncio.run(run())
    footer = next(line for line in rendered.splitlines() if "palette" in line)
    for label in (
        "Refresh", "Clear", "Rename", "Categorise", "Transactions", "Bucket", "Measure",
    ):
        assert label in footer, f"{label!r} missing or truncated: {footer.strip()!r}"


def test_escape_leaves_the_chart_for_the_transactions(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            return app._panel, app.query_one("#chart", DataTable).display

    panel, chart_visible = asyncio.run(run())
    assert panel == "txns"
    assert chart_visible is False


def test_graph_is_a_synonym_for_chart(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"graph {CHART_WINDOW} month")
            await pilot.pause()
            return app._panel

    assert asyncio.run(run()) == "chart"


def test_a_bad_chart_period_is_reported_not_opened(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            messages = []
            app.notify = lambda text, **kw: messages.append(text)
            app._run_command("chart last tuesday")
            await pilot.pause()
            return messages, app._panel

    messages, panel = asyncio.run(run())
    assert panel == "txns"
    assert "last tuesday" in messages[0]


def test_transfers_left_out_of_the_bars_are_counted_in_the_status(tmp_path, monkeypatch):
    """The bars exclude transfers, as every other figure does. Dropping money between
    your own accounts without saying so reads as missing spending."""
    session_factory = _setup_chart(tmp_path, monkeypatch)
    with session_factory() as session:
        currency = session.query(Currency).one()
        savings = Account(name="Savings", currency_id=currency.id)
        session.add(savings)
        session.flush()
        checking = session.query(Account).filter_by(name="Checking").one()
        for account, amount in ((checking, -50_000), (savings, 50_000)):
            session.add(
                Transaction(
                    account_id=account.id,
                    currency_id=currency.id,
                    posted_date=datetime.date(2026, 2, 14),
                    description="MOVE",
                    raw_description="MOVE",
                    value_minor=amount,
                    import_hash=f"move-{account.id}",
                )
            )
        session.commit()
        transfers.detect_transfers(session)
        session.commit()

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month spending")
            await pilot.pause()
            return _chart_rows(app), str(app.query_one("#status", Static).content)

    rows, status = asyncio.run(run())
    # February's 500.00 transfer is not in the bar...
    assert rows[1][2] == "5.00"
    # ...and the status line says two transactions went missing rather than hiding it.
    assert f"{TRANSFER_MARK} 2" in status


# ------------------------------------------------------- arrow-key chart navigation


def test_right_arrow_on_a_chart_bar_drills_down_like_enter(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            table = app.query_one("#chart", DataTable)
            table.focus()
            table.move_cursor(row=0)  # January
            await pilot.press("right")
            await pilot.pause()
            return (
                app._panel,
                app.date_filter,
                sorted(t.description for t in app._txns),
            )

    panel, date_filter, descriptions = asyncio.run(run())
    assert panel == "txns"
    assert date_filter == (datetime.date(2026, 1, 1), datetime.date(2026, 1, 31))
    assert descriptions == ["SHOP A", "SHOP B"]


def test_left_arrow_returns_to_the_chart_with_the_full_series(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            before = _chart_rows(app)
            table = app.query_one("#chart", DataTable)
            table.focus()
            table.move_cursor(row=2)  # March
            await pilot.press("right")
            await pilot.pause()
            drilled = (app._panel, app.date_filter)

            await pilot.press("left")
            await pilot.pause()
            back_panel = app._panel
            after = _chart_rows(app)
            cursor_row = app.query_one("#chart", DataTable).cursor_row
            return before, drilled, back_panel, after, app.date_filter, cursor_row

    before, drilled, back_panel, after, date_filter, cursor_row = asyncio.run(run())
    assert drilled[0] == "txns" and drilled[1] is not None
    assert back_panel == "chart"
    # The date filter the drill-down set is gone, or the chart would be scoped to March.
    assert date_filter is None
    assert after == before
    assert cursor_row == 2  # back on the March row that was drilled from


def test_a_partial_edge_bucket_drills_into_only_the_days_in_the_window(
    tmp_path, monkeypatch
):
    """The window's own edges, not the calendar month's. Without clamping,
    bucket_date_range would reconstruct January as the whole month and March as the
    whole month, pulling in SHOP A (before the window) and SHOP D (after it) — rows the
    bars themselves never counted, so the drilled-down list would stop summing to them.
    """
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("chart 2026-01-18..2026-03-05 month net")
            await pilot.pause()
            table = app.query_one("#chart", DataTable)
            table.focus()

            table.move_cursor(row=0)  # January, clamped to 01-18..01-31
            await pilot.press("right")
            await pilot.pause()
            january = sorted(t.description for t in app._txns), app.date_filter
            await pilot.press("left")
            await pilot.pause()

            table.move_cursor(row=2)  # March, clamped to 03-01..03-05
            await pilot.press("right")
            await pilot.pause()
            march = sorted(t.description for t in app._txns), app.date_filter

            return january, march

    january, march = asyncio.run(run())
    # SHOP A (2026-01-15) is outside the window; only SHOP B (01-20) counts.
    assert january == (["SHOP B"], (datetime.date(2026, 1, 18), datetime.date(2026, 1, 31)))
    # SHOP D (2026-03-06) is outside the window; only the 03-05 paycheck counts.
    assert march == (["PAYCHECK"], (datetime.date(2026, 3, 1), datetime.date(2026, 3, 5)))


def test_left_arrow_after_a_chart_drill_restores_filters_set_before_it(
    tmp_path, monkeypatch
):
    """A chart drill-down only ever narrows the date; a category filter set before it
    must survive the round trip untouched."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            food = next(c for c in app._categories if c.name == "Food")
            app.category_filter = food.id
            app.reload()
            await pilot.pause()

            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            table = app.query_one("#chart", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("right")
            await pilot.pause()
            drilled_category_filter = app.category_filter

            await pilot.press("left")
            await pilot.pause()
            return food.id, drilled_category_filter, app.category_filter, app._panel

    food_id, drilled_category_filter, restored_category_filter, panel = asyncio.run(run())
    assert drilled_category_filter == food_id
    assert restored_category_filter == food_id
    assert panel == "chart"


def test_a_new_filter_clears_the_stale_chart_drill_flag(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            table = app.query_one("#chart", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("right")
            await pilot.pause()
            drilled = app._drilled_from_chart

            app._run_command("filter shop")
            await pilot.pause()
            after_filter = app._drilled_from_chart
            panel = app._panel

            await pilot.press("left")
            await pilot.pause()
            return drilled, after_filter, panel, app._panel

    drilled, after_filter, panel_before_left, panel_after_left = asyncio.run(run())
    assert drilled is True
    assert after_filter is False
    assert panel_before_left == "txns"
    assert panel_after_left == "txns"  # left arrow did not send us anywhere


def test_escape_clears_the_stale_chart_drill_down_flag(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            table = app.query_one("#chart", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("right")
            await pilot.pause()

            await pilot.press("escape")
            await pilot.pause()
            return app._drilled_from_chart

    assert asyncio.run(run()) is False


def test_the_chart_total_row_cannot_be_drilled(tmp_path, monkeypatch):
    """Mirrors the statistics panel's guard: the closing TOTAL row is not a bar."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            table = app.query_one("#chart", DataTable)
            table.focus()
            table.move_cursor(row=table.row_count - 1)  # the TOTAL row
            await pilot.press("right")
            await pilot.pause()
            return app._panel, app.date_filter

    panel, date_filter = asyncio.run(run())
    assert panel == "chart"
    assert date_filter is None


def test_drilling_from_stats_and_from_the_chart_do_not_confuse_each_others_back_link(
    tmp_path, monkeypatch
):
    """Leaving one drilled-down view for the other panel invalidates the back-link it
    leaves behind, so a stale left arrow never sends you somewhere unrelated."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"stats {CHART_WINDOW}")
            await pilot.pause()
            app.query_one("#stats_table", DataTable).focus()
            await pilot.press("right")
            await pilot.pause()
            drilled_from_stats = app._drilled_from_stats

            # Opening the chart leaves the stats drill behind.
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            after_chart_open = (app._drilled_from_stats, app._drilled_from_chart)

            app.query_one("#chart", DataTable).focus()
            await pilot.press("right")
            await pilot.pause()
            drilled_from_chart = app._drilled_from_chart

            # And the reverse: opening stats leaves the chart drill behind in turn.
            app._run_command(f"stats {CHART_WINDOW}")
            await pilot.pause()
            after_stats_open = (app._drilled_from_stats, app._drilled_from_chart)

            return (
                drilled_from_stats,
                after_chart_open,
                drilled_from_chart,
                after_stats_open,
            )

    (
        drilled_from_stats,
        after_chart_open,
        drilled_from_chart,
        after_stats_open,
    ) = asyncio.run(run())
    assert drilled_from_stats is True
    assert after_chart_open == (False, False)
    assert drilled_from_chart is True
    assert after_stats_open == (False, False)


def test_the_chart_drill_down_keys_are_advertised_in_the_footer(tmp_path, monkeypatch):
    """The chart's twin of test_the_drill_down_keys_are_advertised_in_the_footer."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            app.query_one("#chart", DataTable).focus()
            await pilot.pause()
            on_chart = dict(app.active_bindings)

            table = app.query_one("#chart", DataTable)
            table.move_cursor(row=0)
            await pilot.press("right")
            await pilot.pause()
            drilled = dict(app.active_bindings)

            return on_chart, drilled

    def action_for(bindings, key):
        binding = bindings.get(key)
        return binding.binding.action if binding else None

    on_chart, drilled = asyncio.run(run())
    assert action_for(on_chart, "right") == "drill_down"
    assert on_chart["right"].binding.description == "Drill down"
    assert action_for(drilled, "left") == "drill_up"


# ---------------------------------------------------------- browsing the import inbox
