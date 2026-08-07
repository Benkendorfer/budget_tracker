"""Tests for bar-chart scaling and layout.

Pure geometry, so almost none of this needs a database — the cases at the end are the
ones checking that a real series survives the trip from :func:`stats.spending_series`
into :func:`charts.build` with its numbers intact.
"""

from datetime import date

import pytest

from budget_tracker import charts, stats
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.models import Account, Category, Currency, Transaction
from budget_tracker.queries import BucketTotal


def _bucket(key, outflow, inflow=0, count=1, label=None):
    return BucketTotal(
        key=key,
        label=label if label is not None else key,
        count=count,
        outflow_minor=outflow,
        inflow_minor=inflow,
    )


def _bars(chart):
    return [bar.bar for bar in chart.bars]


# ----------------------------------------------------------------- bucket choice


def test_bucket_defaults_get_finer_as_the_window_shortens():
    assert charts.choose_bucket(stats.resolve("1m", date(2026, 6, 30))) == "day"
    assert charts.choose_bucket(stats.resolve("3m", date(2026, 6, 30))) == "week"
    assert charts.choose_bucket(stats.resolve("6m", date(2026, 6, 30))) == "week"
    assert charts.choose_bucket(stats.resolve("1y", date(2026, 6, 30))) == "month"
    assert charts.choose_bucket(stats.resolve("2y", date(2026, 6, 30))) == "month"


def test_bucket_choice_is_driven_by_length_not_by_preset():
    """A custom range of a given length buckets the same as a preset of that length."""
    assert charts.choose_bucket(stats.parse("2026-01-01..2026-01-20")) == "day"
    assert charts.choose_bucket(stats.parse("2026-01-01..2026-03-01")) == "week"
    assert charts.choose_bucket(stats.parse("2026-01-01..2026-12-31")) == "month"
    assert charts.choose_bucket(stats.parse("2026-01-01..2026-01-01")) == "day"


# --------------------------------------------------------------------- measures


def test_measure_names_and_aliases_parse():
    assert charts.parse_measure("net") == "net"
    assert charts.parse_measure("Spending") == "spend"
    assert charts.parse_measure("spend") == "spend"
    assert charts.parse_measure("  OUT ") == "spend"
    assert charts.parse_measure("income") == "income"
    assert charts.parse_measure("in") == "income"


def test_an_unknown_measure_is_rejected_by_name():
    with pytest.raises(ValueError, match="Unknown measure 'profit'"):
        charts.parse_measure("profit")
    with pytest.raises(ValueError, match="Unknown measure"):
        charts.build([], measure="profit")


def test_every_measure_has_a_header_and_a_place_in_the_cycle():
    assert set(charts.MEASURE_HEADERS) == set(charts.MEASURES)
    assert set(charts.MEASURE_ALIASES.values()) == set(charts.MEASURES)
    assert charts.MEASURES[0] == "net"  # the default


# ------------------------------------------------------- single-direction bars


def test_a_full_bar_fills_the_width_and_an_empty_one_is_blank():
    assert charts.bar_text(1.0, 10) == "█" * 10
    assert charts.bar_text(0.5, 10) == "█" * 5
    assert charts.bar_text(0.0, 10) == ""


def test_a_tiny_but_real_value_still_draws_something():
    """The whole point: "spent almost nothing" must not render as "spent nothing"."""
    assert charts.bar_text(0.0001, 28) == "▏"
    assert charts.bar_text(0.0, 28) == ""


def test_partial_cells_use_eighth_blocks():
    assert charts.bar_text(0.25, 3) == "▊"  # 0.75 of a cell — six eighths
    assert charts.bar_text(0.5, 3) == "█▌"


def test_a_bar_never_exceeds_its_width():
    assert charts.bar_text(1.4, 10) == "█" * 10


def test_a_zero_width_bar_is_rejected():
    with pytest.raises(ValueError, match="at least 1"):
        charts.bar_text(0.5, 0)


def test_spending_bars_scale_to_the_biggest_bucket():
    chart = charts.build(
        [_bucket("2026-01", -1000), _bucket("2026-02", -500), _bucket("2026-03", -250)],
        measure="spend",
        width=8,
    )
    assert chart.peak_minor == 1000
    assert [bar.fraction for bar in chart.bars] == [1.0, 0.5, 0.25]
    assert _bars(chart) == ["█" * 8, "█" * 4, "█" * 2]
    assert [bar.value_minor for bar in chart.bars] == [1000, 500, 250]
    assert chart.total_minor == 1750


def test_income_charts_the_money_coming_in():
    chart = charts.build(
        [_bucket("2026-01", -9999, inflow=400), _bucket("2026-02", 0, inflow=100)],
        measure="income",
        width=8,
    )
    # Spending is ignored entirely: the peak is the biggest *income*, not the biggest row.
    assert chart.peak_minor == 400
    assert [bar.value_minor for bar in chart.bars] == [400, 100]
    assert _bars(chart) == ["█" * 8, "█" * 2]
    assert chart.total_minor == 500


def test_single_direction_measures_have_no_axis():
    for measure in ("spend", "income"):
        chart = charts.build([_bucket("2026-01", -100, inflow=100)], measure=measure)
        assert chart.diverging is False
        assert all(bar.left == "" and bar.axis == "" for bar in chart.bars)


# ------------------------------------------------------------- diverging (net)


def test_net_grows_left_when_a_bucket_costs_and_right_when_it_pays():
    chart = charts.build(
        [_bucket("2026-01", -1000), _bucket("2026-02", 0, inflow=1000)],
        measure="net",
        width=9,  # 4 + axis + 4
    )
    cost, paid = chart.bars
    assert cost.net_minor == -1000 and paid.net_minor == 1000
    # Money out is right-aligned against the axis, so it grows away to the left.
    assert cost.bar == "████│    "
    # Money in is left-aligned against it, growing away to the right.
    assert paid.bar == "    │████"


def test_the_net_axis_never_moves():
    """Every row is the same width with the axis at the same offset, or the column stops
    being readable as a chart at all."""
    chart = charts.build(
        [
            _bucket("2026-01", -1000),
            _bucket("2026-02", -1),
            _bucket("2026-03", 0),
            _bucket("2026-04", 0, inflow=800),
        ],
        measure="net",
        width=9,
    )
    assert all(len(bar.bar) == 9 for bar in chart.bars)
    assert all(bar.bar[4] == charts.AXIS for bar in chart.bars)
    assert chart.width == 9


def test_a_bucket_that_came_out_even_sits_on_the_axis():
    """The reason for charting net: 200 spent and 200 refunded is not a heavy month."""
    chart = charts.build(
        [_bucket("2026-01", -20000, inflow=20000), _bucket("2026-02", -5000)],
        measure="net",
        width=9,
    )
    even, spent = chart.bars
    assert even.net_minor == 0
    assert even.bar == "    │    "  # nothing either side
    assert even.outflow_minor == 20000  # the spending is still recorded, just netted
    assert spent.bar == "████│    "


def test_one_peak_covers_both_directions_of_net():
    """A month that earned 500 and one that cost 500 have to mirror each other, not each
    fill its own side."""
    chart = charts.build(
        [_bucket("2026-01", -500), _bucket("2026-02", 0, inflow=500)],
        measure="net",
        width=9,
    )
    assert chart.peak_minor == 500
    assert [bar.fraction for bar in chart.bars] == [1.0, 1.0]
    assert chart.bars[0].left.strip() == chart.bars[1].right.strip()


def test_a_tiny_net_still_draws_one_cell():
    chart = charts.build(
        [_bucket("2026-01", -100000), _bucket("2026-02", -1)], measure="net", width=9
    )
    assert chart.bars[1].bar == "   █│    "


def test_an_even_width_loses_a_cell_rather_than_moving_the_axis():
    chart = charts.build([_bucket("2026-01", -100)], measure="net", width=10)
    bar = chart.bars[0]
    assert len(bar.left) == len(bar.right) == 4
    assert len(bar.bar) == 9  # one short of the 10 asked for, but symmetrical


# ------------------------------------------------------------------ every measure


def test_an_empty_window_charts_as_nothing_rather_than_dividing_by_zero():
    for measure in charts.MEASURES:
        chart = charts.build(
            [_bucket("2026-01", 0, count=0), _bucket("2026-02", 0, count=0)],
            measure=measure,
            width=9,
        )
        assert chart.peak_minor == 0
        assert chart.total_minor == 0
        assert [bar.fraction for bar in chart.bars] == [0.0, 0.0]
        assert all(charts.BLOCK not in bar.bar for bar in chart.bars)


def test_a_series_with_no_buckets_at_all_is_an_empty_chart():
    for measure in charts.MEASURES:
        chart = charts.build([], measure=measure)
        assert chart.bars == []
        assert chart.peak_minor == 0
        assert chart.avg_minor == 0


def test_every_bar_carries_all_three_figures_whichever_is_drawn():
    """The table shows a second column of context beside the bar, so the figures the
    measure is *not* charting still have to be there."""
    for measure in charts.MEASURES:
        chart = charts.build(
            [_bucket("2026-01", -3000, inflow=500000)], measure=measure, width=9
        )
        bar = chart.bars[0]
        assert (bar.outflow_minor, bar.inflow_minor, bar.net_minor) == (
            3000,
            500000,
            497000,
        )


def test_totals_and_averages_follow_the_measure():
    series = [
        _bucket("2026-01", -1000, inflow=200, count=3),
        _bucket("2026-02", -3000, count=5),
    ]
    assert charts.build(series, measure="spend").total_minor == 4000
    assert charts.build(series, measure="income").total_minor == 200
    assert charts.build(series, measure="net").total_minor == -3800
    # Zero-filled buckets count towards the average: a window that was mostly quiet has
    # a low run rate, and averaging over only the busy buckets would overstate it.
    quiet = [_bucket("2026-01", -3000), _bucket("2026-02", 0), _bucket("2026-03", 0)]
    assert charts.build(quiet, measure="spend").avg_minor == 1000
    assert charts.build(quiet, measure="net").avg_minor == -1000


def test_labels_and_keys_come_straight_from_the_series():
    chart = charts.build([_bucket("2026-W04", -100, label="W04")], measure="spend")
    assert chart.bars[0].key == "2026-W04"
    assert chart.bars[0].label == "W04"


# --------------------------------------------------------- against a real series


def _session_factory(tmp_path):
    engine = get_engine(tmp_path / "t.db")
    init_db(engine)
    return get_sessionmaker(engine)


def _seed(session):
    currency = Currency(value="USD", symbol="$", decimal_places=2)
    session.add(currency)
    session.flush()
    account = Account(name="Checking", currency_id=currency.id)
    session.add(account)
    session.flush()
    return currency, account


def _txn(session, currency, account, day, amount, description="X", category=None):
    session.add(
        Transaction(
            account_id=account.id,
            currency_id=currency.id,
            category_id=category.id if category is not None else None,
            posted_date=day,
            description=description,
            raw_description=description,
            value_minor=amount,
            import_hash=f"{day}-{amount}-{description}",
        )
    )
    session.flush()


def test_a_real_series_charts_with_its_quiet_buckets_intact(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, account = _seed(session)
        _txn(session, currency, account, date(2026, 1, 15), -2000, "A")
        _txn(session, currency, account, date(2026, 3, 10), -1000, "B")
        session.commit()

    window = stats.parse("2026-01-01..2026-03-31")
    with session_factory() as session:
        series = stats.spending_series(session, window, "month")
    chart = charts.build(series, measure="spend", width=10)

    assert [bar.label for bar in chart.bars] == ["2026-01", "2026-02", "2026-03"]
    assert [bar.value_minor for bar in chart.bars] == [2000, 0, 1000]
    assert chart.bars[1].bar == ""  # February: present and empty, not missing
    assert chart.bars[0].bar == "█" * 10
    assert chart.bars[2].bar == "█" * 5
    assert chart.total_minor == 3000


def test_a_month_of_income_swings_the_net_chart_across_the_axis(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, account = _seed(session)
        _txn(session, currency, account, date(2026, 1, 15), -4000, "A")
        _txn(session, currency, account, date(2026, 2, 10), -1000, "B")
        _txn(session, currency, account, date(2026, 2, 11), 5000, "PAY")
        session.commit()

    window = stats.parse("2026-01-01..2026-02-28")
    with session_factory() as session:
        series = stats.spending_series(session, window, "month")
    chart = charts.build(series, measure="net", width=9)

    january, february = chart.bars
    assert january.net_minor == -4000 and february.net_minor == 4000
    assert january.bar == "████│    "
    assert february.bar == "    │████"
    assert chart.total_minor == 0  # the month paid back exactly what the other cost


def test_a_category_filter_narrows_the_chart(tmp_path):
    """Charting one category has to go through the same filters the series takes, or the
    bars would silently show the whole window's spending under a category's name."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, account = _seed(session)
        food = Category(value="Food")
        session.add(food)
        session.flush()
        _txn(session, currency, account, date(2026, 1, 15), -2000, "A", category=food)
        _txn(session, currency, account, date(2026, 1, 20), -5000, "B")
        session.commit()
        food_id = food.id

    window = stats.parse("2026-01-01..2026-01-31")
    with session_factory() as session:
        everything = charts.build(
            stats.spending_series(session, window, "month"), measure="spend"
        )
        just_food = charts.build(
            stats.spending_series(session, window, "month", category_id=food_id),
            measure="spend",
        )

    assert everything.total_minor == 7000
    assert just_food.total_minor == 2000
    assert len(just_food.bars) == 1
