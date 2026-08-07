"""Tests for time windows and the statistics report."""

from datetime import date

import pytest

from sqlalchemy import event
from sqlalchemy.engine import Engine

from budget_tracker import categories, queries, stats, transfers
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.models import Account, Category, Currency, Transaction, Vendor


def _session_factory(tmp_path):
    engine = get_engine(tmp_path / "t.db")
    init_db(engine)
    return get_sessionmaker(engine)


def _seed(session, account_names=("Checking",), category_names=()):
    currency = Currency(value="USD", symbol="$", decimal_places=2)
    session.add(currency)
    session.flush()
    accounts = {}
    for name in account_names:
        account = Account(name=name, currency_id=currency.id)
        session.add(account)
        accounts[name] = account
    categories = {}
    for name in category_names:
        category = Category(value=name)
        session.add(category)
        categories[name] = category
    session.flush()
    return currency, accounts, categories


def _txn(session, currency, account, day, amount, description="X", category=None):
    txn = Transaction(
        account_id=account.id,
        currency_id=currency.id,
        category_id=category.id if category is not None else None,
        posted_date=day,
        description=description,
        raw_description=description,
        value_minor=amount,
        import_hash=f"{account.id}-{day}-{amount}-{description}",
    )
    session.add(txn)
    session.flush()
    return txn


def _custom(start, end):
    return stats.parse(f"{start}..{end}")


# ------------------------------------------------------------------ window arithmetic

def test_months_before_clamps_to_a_short_month():
    assert stats.months_before(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert stats.months_before(date(2024, 3, 31), 1) == date(2024, 2, 29)  # leap year
    assert stats.months_before(date(2026, 5, 31), 3) == date(2026, 2, 28)


def test_months_before_rolls_over_the_year():
    assert stats.months_before(date(2026, 2, 15), 3) == date(2025, 11, 15)
    assert stats.months_before(date(2026, 1, 31), 12) == date(2025, 1, 31)
    assert stats.months_before(date(2026, 6, 10), 24) == date(2024, 6, 10)


def test_resolve_starts_the_day_after_the_month_boundary():
    today = date(2026, 8, 6)
    expected = {
        "1m": date(2026, 7, 7),
        "3m": date(2026, 5, 7),
        "6m": date(2026, 2, 7),
        "1y": date(2025, 8, 7),
        "2y": date(2024, 8, 7),
    }
    for key, start in expected.items():
        window = stats.resolve(key, today=today)
        assert window.start == start
        assert window.end == today  # inclusive


def test_resolve_covers_every_preset_and_rejects_others():
    assert [p[0] for p in stats.PRESETS] == ["1m", "3m", "6m", "1y", "2y"]
    assert [p[2] for p in stats.PRESETS] == [1, 3, 6, 12, 24]
    with pytest.raises(ValueError, match="Unknown window"):
        stats.resolve("9m", today=date(2026, 8, 6))


def test_window_days_and_months_are_inclusive():
    window = _custom("2026-01-01", "2026-01-31")
    assert window.days == 31
    assert window.months == pytest.approx(31 / stats.DAYS_PER_MONTH)
    assert _custom("2026-01-01", "2026-01-01").days == 1


def test_parse_accepts_a_key_a_label_and_a_range():
    today = date(2026, 8, 6)
    assert stats.parse("3m", today=today) == stats.resolve("3m", today=today)
    assert stats.parse("3 months", today=today) == stats.resolve("3m", today=today)
    assert stats.parse("3 month", today=today) == stats.resolve("3m", today=today)
    assert stats.parse("  1 YEAR  ", today=today) == stats.resolve("1y", today=today)

    window = stats.parse("2025-01-01..2025-06-30")
    assert window.key == "custom"
    assert window.label == "2025-01-01 → 2025-06-30"
    assert (window.start, window.end) == (date(2025, 1, 1), date(2025, 6, 30))


def test_parse_errors():
    with pytest.raises(ValueError, match="Empty window"):
        stats.parse("   ")
    with pytest.raises(ValueError, match="Unknown window"):
        stats.parse("last week")
    with pytest.raises(ValueError, match="Bad date range"):
        stats.parse("2025-01-01..nonsense")
    with pytest.raises(ValueError, match="Bad date range"):
        stats.parse("2025-13-01..2025-06-30")
    with pytest.raises(ValueError, match="starts after it ends"):
        stats.parse("2025-06-30..2025-01-01")


# ------------------------------------------------------------------------- the report

def test_average_per_month_divides_by_the_window(tmp_path):
    """Six months of spending averages to roughly a sixth per month."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, categories = _seed(session, category_names=("Rent",))
        for month in range(1, 7):
            _txn(
                session,
                currency,
                accounts["Checking"],
                date(2025, month, 15),
                -10000,  # 100.00 a month, 600.00 over the window
                f"Rent {month}",
                categories["Rent"],
            )
        session.commit()

    window = _custom("2025-01-01", "2025-06-30")
    with session_factory() as session:
        report = stats.build_report(session, window)

    assert report.outflow_minor == -60000
    assert len(report.categories) == 1
    rent = report.categories[0]
    assert rent.name == "Rent"
    assert rent.count == 6
    # Assert against the declared divisor rather than a rounded-off 10000.
    assert rent.avg_month_minor == int(round(-60000 / window.months))
    assert rent.avg_month_minor == report.avg_month_outflow_minor
    assert -10100 < rent.avg_month_minor < -9900  # ~100.00 a month
    assert report.avg_month_inflow_minor == 0


def test_categories_are_ordered_by_spend_and_include_uncategorised(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, categories = _seed(
            session, category_names=("Rent", "Dining")
        )
        account = accounts["Checking"]
        _txn(session, currency, account, date(2025, 3, 1), -100000, "Rent",
             categories["Rent"])
        _txn(session, currency, account, date(2025, 3, 2), -3000, "Lunch",
             categories["Dining"])
        _txn(session, currency, account, date(2025, 3, 3), -1000, "Snack",
             categories["Dining"])
        _txn(session, currency, account, date(2025, 3, 4), -500, "Mystery")
        _txn(session, currency, account, date(2025, 3, 5), 250000, "Salary")
        session.commit()

    with session_factory() as session:
        report = stats.build_report(session, _custom("2025-03-01", "2025-03-31"))

    names = [c.name for c in report.categories]
    assert names == ["Rent", "Dining", stats.UNCATEGORISED]
    assert [c.outflow_minor for c in report.categories] == [-100000, -4000, -500]
    assert [c.count for c in report.categories] == [1, 2, 2]  # salary is uncategorised

    # The rows add back up to the report, and the shares are a probability distribution.
    assert sum(c.outflow_minor for c in report.categories) == report.outflow_minor
    assert sum(c.inflow_minor for c in report.categories) == report.inflow_minor
    assert sum(c.share for c in report.categories) == pytest.approx(1.0)
    # Net-based, not gross: Uncategorised nets to +249500 (-500 Mystery, +250000 Salary),
    # so it contributes 0 to the denominator despite -500 of gross outflow. The
    # denominator is net spend only: -100000 (Rent) + -4000 (Dining) = -104000, not the
    # old gross-outflow denominator of -104500.
    assert report.categories[0].share == pytest.approx(100000 / 104000)


def test_share_is_zero_when_nothing_was_spent(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, _ = _seed(session)
        _txn(session, currency, accounts["Checking"], date(2025, 3, 1), 250000, "Salary")
        session.commit()

    with session_factory() as session:
        report = stats.build_report(session, _custom("2025-03-01", "2025-03-31"))
    assert report.outflow_minor == 0
    assert [c.share for c in report.categories] == [0.0]


def test_share_is_net_based_not_gross(tmp_path):
    """A category with heavy churn (money out and mostly back in) must not out-rank a
    category that is purely a net cost, even though its gross outflow is much bigger."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, categories = _seed(
            session, category_names=("Churn", "Travel")
        )
        account = accounts["Checking"]
        # Churn: -31000 out, +28700 back in -> nets to -2300, a net cost.
        _txn(session, currency, account, date(2025, 3, 1), -31000, "Out",
             categories["Churn"])
        _txn(session, currency, account, date(2025, 3, 2), 28700, "Back",
             categories["Churn"])
        # Travel: pure spend, nets to -8900.
        _txn(session, currency, account, date(2025, 3, 3), -8900, "Trip",
             categories["Travel"])
        session.commit()

    with session_factory() as session:
        report = stats.build_report(session, _custom("2025-03-01", "2025-03-31"))
    rows = {c.name: c for c in report.categories}

    # Gross outflow still shows Churn as the bigger mover...
    assert rows["Churn"].outflow_minor == -31000
    assert rows["Travel"].outflow_minor == -8900
    # ...but net spend, and therefore share, correctly shows Travel costing more.
    assert rows["Churn"].total_minor == -2300
    assert rows["Travel"].total_minor == -8900
    assert report.net_spend_minor == -2300 + -8900
    assert rows["Churn"].share == pytest.approx(2300 / 11200)
    assert rows["Travel"].share == pytest.approx(8900 / 11200)
    assert rows["Travel"].share > rows["Churn"].share


def test_share_is_zero_for_a_net_positive_row(tmp_path):
    """A category that nets positive (more refunded than spent) is not "negative spend"."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, categories = _seed(
            session, category_names=("Refunds", "Dining")
        )
        account = accounts["Checking"]
        _txn(session, currency, account, date(2025, 3, 1), -1000, "Bought",
             categories["Refunds"])
        _txn(session, currency, account, date(2025, 3, 2), 5000, "Refunded",
             categories["Refunds"])
        _txn(session, currency, account, date(2025, 3, 3), -2000, "Lunch",
             categories["Dining"])
        session.commit()

    with session_factory() as session:
        report = stats.build_report(session, _custom("2025-03-01", "2025-03-31"))
    rows = {c.name: c for c in report.categories}

    assert rows["Refunds"].total_minor == 4000  # net positive
    assert rows["Refunds"].share == 0.0
    assert rows["Refunds"].parent_share == 0.0
    # Dining is the only net cost, so it takes the entire denominator.
    assert report.net_spend_minor == -2000
    assert rows["Dining"].share == pytest.approx(1.0)


def test_transfers_are_counted_but_contribute_no_money(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, categories = _seed(
            session, account_names=("Checking", "Savings"), category_names=("Dining",)
        )
        _txn(session, currency, accounts["Checking"], date(2025, 3, 1), -50000, "Xfer To")
        _txn(session, currency, accounts["Savings"], date(2025, 3, 2), 50000, "Xfer From")
        _txn(session, currency, accounts["Checking"], date(2025, 3, 3), -2500, "Coffee",
             categories["Dining"])
        transfers.detect_transfers(session)
        session.commit()

    with session_factory() as session:
        report = stats.build_report(session, _custom("2025-03-01", "2025-03-31"))

    assert report.count == 3  # every row in the window
    assert report.transfer_count == 2
    assert report.outflow_minor == -2500  # just the coffee
    assert report.inflow_minor == 0
    assert report.net_minor == -2500

    # The transfer legs got the "Transfer" category. It is listed, because the count is
    # what a drill-down opens and those two rows are really there — but it contributes no
    # money, so it cannot be mistaken for spending.
    rows = {c.name: c for c in report.categories}
    assert set(rows) == {"Dining", "Transfer"}
    assert (rows["Transfer"].count, rows["Transfer"].total_minor) == (2, 0)
    assert (rows["Transfer"].outflow_minor, rows["Transfer"].share) == (0, 0.0)
    assert (rows["Dining"].count, rows["Dining"].outflow_minor) == (1, -2500)
    # Spending still adds up to the report's own figure, transfers or not.
    assert sum(c.outflow_minor for c in report.categories) == report.outflow_minor


def test_the_date_range_clips_both_rows_and_totals(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, categories = _seed(
            session, category_names=("Dining", "Travel")
        )
        account = accounts["Checking"]
        _txn(session, currency, account, date(2025, 3, 15), -2500, "In", categories["Dining"])
        _txn(session, currency, account, date(2025, 2, 28), -9999, "Before",
             categories["Travel"])
        _txn(session, currency, account, date(2025, 4, 1), -8888, "After",
             categories["Travel"])
        session.commit()

    window = _custom("2025-03-01", "2025-03-31")
    with session_factory() as session:
        report = stats.build_report(session, window)
        # The boundary days themselves are inclusive.
        edges = stats.build_report(session, _custom("2025-02-28", "2025-04-01"))
        rows = queries.get_transactions(session, date_range=(window.start, window.end))

    assert report.count == 1
    assert report.outflow_minor == -2500
    assert [c.name for c in report.categories] == ["Dining"]
    assert "Travel" not in [c.name for c in report.categories]
    assert [r.description for r in rows] == ["In"]
    assert edges.count == 3


def test_filters_narrow_the_report(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, categories = _seed(
            session, account_names=("Checking", "Card"), category_names=("Dining",)
        )
        _txn(session, currency, accounts["Checking"], date(2025, 3, 1), -2500,
             "Coffee shop", categories["Dining"])
        _txn(session, currency, accounts["Card"], date(2025, 3, 2), -7500, "Hardware",
             categories["Dining"])
        session.commit()

    window = _custom("2025-03-01", "2025-03-31")
    with session_factory() as session:
        unfiltered = stats.build_report(session, window)
        account_id = queries.resolve_account(session, "Card")
        by_account = stats.build_report(session, window, account_id=account_id)
        by_text = stats.build_report(
            session, window, text_filter=queries.TextFilter("coffee")
        )

    assert unfiltered.outflow_minor == -10000
    assert by_account.count == 1
    assert by_account.outflow_minor == -7500
    assert by_text.count == 1
    assert by_text.outflow_minor == -2500
    assert by_text.categories[0].outflow_minor == -2500


# -------------------------------------------------------------------------- buckets

def test_bucket_totals_by_month(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, _ = _seed(session)
        account = accounts["Checking"]
        _txn(session, currency, account, date(2025, 1, 5), -1000, "A")
        _txn(session, currency, account, date(2025, 1, 20), -2000, "B")
        _txn(session, currency, account, date(2025, 3, 4), -3000, "C")
        _txn(session, currency, account, date(2025, 3, 5), 50000, "D")
        session.commit()

    with session_factory() as session:
        rows = queries.get_bucket_totals(session, "month")

    assert [r.key for r in rows] == ["2025-01", "2025-03"]  # chronological, gaps omitted
    assert [r.label for r in rows] == ["2025-01", "2025-03"]
    assert [r.outflow_minor for r in rows] == [-3000, -3000]
    assert [r.inflow_minor for r in rows] == [0, 50000]
    assert [r.count for r in rows] == [2, 2]


def test_bucket_totals_by_week_and_day(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, _ = _seed(session)
        account = accounts["Checking"]
        _txn(session, currency, account, date(2026, 3, 2), -1000, "Mon")  # week 09
        _txn(session, currency, account, date(2026, 3, 4), -2000, "Wed")  # week 09
        _txn(session, currency, account, date(2026, 3, 9), -4000, "Next")  # week 10
        session.commit()

    with session_factory() as session:
        weeks = queries.get_bucket_totals(session, "week")
        days = queries.get_bucket_totals(session, "day")

    assert [w.key for w in weeks] == ["2026-W09", "2026-W10"]
    assert [w.label for w in weeks] == ["W09", "W10"]
    assert [w.outflow_minor for w in weeks] == [-3000, -4000]

    assert [d.key for d in days] == ["2026-03-02", "2026-03-04", "2026-03-09"]
    assert [d.label for d in days] == ["03-02", "03-04", "03-09"]


def test_bucket_totals_exclude_transfers_and_honor_the_range(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, _ = _seed(session, account_names=("Checking", "Savings"))
        _txn(session, currency, accounts["Checking"], date(2025, 3, 1), -50000, "Xfer To")
        _txn(session, currency, accounts["Savings"], date(2025, 3, 2), 50000, "Xfer From")
        _txn(session, currency, accounts["Checking"], date(2025, 3, 3), -2500, "Coffee")
        _txn(session, currency, accounts["Checking"], date(2025, 5, 3), -900, "Later")
        transfers.detect_transfers(session)
        session.commit()

    with session_factory() as session:
        rows = queries.get_bucket_totals(
            session, "month", date_range=(date(2025, 3, 1), date(2025, 3, 31))
        )
    assert [(r.key, r.count, r.outflow_minor) for r in rows] == [("2025-03", 1, -2500)]


def test_unknown_bucket_is_rejected(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        with pytest.raises(ValueError, match="Unknown bucket"):
            queries.get_bucket_totals(session, "quarter")


def test_spending_series_zero_fills_empty_buckets(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, _ = _seed(session)
        _txn(session, currency, accounts["Checking"], date(2025, 1, 31), -1000, "A")
        _txn(session, currency, accounts["Checking"], date(2025, 4, 2), -3000, "B")
        session.commit()

    window = _custom("2025-01-31", "2025-04-30")
    with session_factory() as session:
        months = stats.spending_series(session, window, "month")
        days = stats.spending_series(
            session, _custom("2025-01-30", "2025-02-02"), "day"
        )
        weeks = stats.spending_series(
            session, _custom("2025-01-27", "2025-02-09"), "week"
        )

    assert [m.key for m in months] == ["2025-01", "2025-02", "2025-03", "2025-04"]
    assert [m.outflow_minor for m in months] == [-1000, 0, 0, -3000]
    assert [m.label for m in months] == ["2025-01", "2025-02", "2025-03", "2025-04"]

    assert [d.key for d in days] == [
        "2025-01-30", "2025-01-31", "2025-02-01", "2025-02-02"
    ]
    assert [d.outflow_minor for d in days] == [0, -1000, 0, 0]

    assert [w.key for w in weeks] == ["2025-W04", "2025-W05"]
    assert [w.outflow_minor for w in weeks] == [-1000, 0]


def test_get_transactions_does_not_query_per_row(tmp_path):
    """An N+1 guard.

    Each ``TxnRow`` reads through vendor (and its display name), category, currency and
    account. Left lazily loaded those cost one SELECT per distinct related object — on a
    real database, hundreds of them, and the whole cost of a filter keystroke. The bound
    is loose on purpose: it fails on a return to per-row loading, not on one more query.
    """
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, cats = _seed(session, category_names=("Dining",))
        # Distinct vendors are what makes lazy loading expensive, so give it plenty.
        for i in range(120):
            vendor = Vendor(name=f"SHOP {i}")
            session.add(vendor)
            session.flush()
            txn = _txn(session, currency, accounts["Checking"], date(2025, 3, 1),
                       -100 - i, f"SHOP {i}", category=cats["Dining"])
            txn.vendor_id = vendor.id
        session.commit()

    # Listening on the Engine class, not an engine instance: the helper above builds its
    # own and does not hand it back, so an instance listener would sit on a different
    # engine and count nothing — passing whatever the code did.
    executed = []
    listener = lambda *args, **kwargs: executed.append(1)  # noqa: E731
    event.listen(Engine, "before_cursor_execute", listener)
    try:
        with session_factory() as session:
            rows = queries.get_transactions(session)
    finally:
        event.remove(Engine, "before_cursor_execute", listener)

    assert len(rows) == 120
    assert {r.vendor for r in rows} == {f"SHOP {i}" for i in range(120)}
    assert len(executed) < 10, f"{len(executed)} queries for 120 rows — lazy loading is back"


def test_category_rows_carry_a_filter_ready_id(tmp_path):
    """Each row can be handed straight back as a category filter, nulls included."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, cats = _seed(session, category_names=("Dining",))
        _txn(session, currency, accounts["Checking"], date(2025, 3, 1), -1000, "A",
             category=cats["Dining"])
        _txn(session, currency, accounts["Checking"], date(2025, 3, 2), -2000, "B")
        session.commit()

    window = _custom("2025-03-01", "2025-03-31")
    with session_factory() as session:
        report = stats.build_report(session, window)
        by_name = {c.name: c.category_id for c in report.categories}
        dining_id = queries.resolve_category(session, "Dining")
        assert by_name["Dining"] == dining_id
        assert by_name[stats.UNCATEGORISED] == queries.UNCATEGORISED_ID

        # Round-trip: filtering by each id returns exactly that row's transactions.
        for name, category_id in by_name.items():
            rows = queries.get_transactions(
                session, category_id=category_id, date_range=(window.start, window.end)
            )
            expected = next(c.count for c in report.categories if c.name == name)
            assert len(rows) == expected


def test_uncategorised_filter_is_not_confused_with_no_filter(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, cats = _seed(session, category_names=("Dining",))
        _txn(session, currency, accounts["Checking"], date(2025, 3, 1), -1000, "A",
             category=cats["Dining"])
        _txn(session, currency, accounts["Checking"], date(2025, 3, 2), -2000, "B")
        session.commit()

    with session_factory() as session:
        everything = queries.get_transactions(session)
        uncategorised = queries.get_transactions(
            session, category_id=queries.UNCATEGORISED_ID
        )
    assert len(everything) == 2
    assert [t.description for t in uncategorised] == ["B"]


def test_spending_series_covers_a_partial_final_bucket(tmp_path):
    """A window ending mid-bucket must still report that bucket, data and all."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, _ = _seed(session)
        # 2026-03-09 is a Monday, so it opens a new %W week that the window only
        # partially covers; 2026-03-04 (Wednesday) sits in the week before it.
        _txn(session, currency, accounts["Checking"], date(2026, 3, 4), -1000, "A")
        _txn(session, currency, accounts["Checking"], date(2026, 3, 9), -2000, "B")
        session.commit()

    with session_factory() as session:
        weeks = stats.spending_series(session, _custom("2026-03-04", "2026-03-09"), "week")
        # Likewise a month window that stops before the month does.
        months = stats.spending_series(session, _custom("2026-02-15", "2026-03-09"), "month")

    assert [(w.key, w.outflow_minor) for w in weeks] == [
        ("2026-W09", -1000),
        ("2026-W10", -2000),
    ]
    assert [(m.key, m.outflow_minor) for m in months] == [
        ("2026-02", 0),
        ("2026-03", -3000),
    ]


def test_spending_series_passes_filters_through(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, _ = _seed(session, account_names=("Checking", "Card"))
        _txn(session, currency, accounts["Checking"], date(2025, 3, 1), -1000, "A")
        _txn(session, currency, accounts["Card"], date(2025, 3, 2), -2000, "B")
        session.commit()

    window = _custom("2025-03-01", "2025-03-31")
    with session_factory() as session:
        account_id = queries.resolve_account(session, "Card")
        rows = stats.spending_series(session, window, "month", account_id=account_id)
    assert [(r.key, r.outflow_minor) for r in rows] == [("2025-03", -2000)]


# --------------------------------------------------------------- nested category rollup

def test_category_rollup_and_display_order(tmp_path):
    """The Food > Dining > {Restaurants, Fast Food}, Food > Groceries example from the brief."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, _ = _seed(session)
        account = accounts["Checking"]
        dining = categories.ensure_path(session, "Food > Dining")
        restaurants = categories.ensure_path(session, "Food > Dining > Restaurants")
        fast_food = categories.ensure_path(session, "Food > Dining > Fast Food")
        groceries = categories.ensure_path(session, "Food > Groceries")
        session.flush()

        _txn(session, currency, account, date(2025, 3, 1), -6000, "Fancy dinner",
             category=restaurants)
        _txn(session, currency, account, date(2025, 3, 2), -1500, "Burger",
             category=fast_food)
        _txn(session, currency, account, date(2025, 3, 3), -4000, "Big shop",
             category=groceries)
        _txn(session, currency, account, date(2025, 3, 4), -500, "Snack",
             category=dining)  # Dining's own direct spend
        session.commit()

    window = _custom("2025-03-01", "2025-03-31")
    with session_factory() as session:
        report = stats.build_report(session, window)

    rows = {r.name: r for r in report.categories}
    assert set(rows) == {"Food", "Dining", "Restaurants", "Fast Food", "Groceries"}

    # Depth-first, siblings biggest-spend-first: Food is the sole root; under it Dining
    # (-8000 total) outspends Groceries (-4000); under Dining, Restaurants (-6000)
    # outspends Fast Food (-1500).
    assert [(r.name, r.depth) for r in report.categories] == [
        ("Food", 0), ("Dining", 1), ("Restaurants", 2), ("Fast Food", 2), ("Groceries", 1),
    ]

    # A parent's money is its own direct figures plus every descendant's.
    assert rows["Dining"].outflow_minor == -500 - 6000 - 1500
    assert rows["Dining"].own_total_minor == -500
    assert rows["Dining"].own_count == 1
    assert rows["Food"].outflow_minor == rows["Dining"].outflow_minor + rows["Groceries"].outflow_minor
    assert rows["Food"].own_total_minor == 0
    assert rows["Food"].own_count == 0

    # Only the depth-0 rows reproduce the report's own totals; summing every row would
    # double-count a parent together with its children.
    depth0 = [r for r in report.categories if r.depth == 0]
    assert [r.name for r in depth0] == ["Food"]
    assert sum(r.outflow_minor for r in depth0) == report.outflow_minor
    assert sum(r.inflow_minor for r in depth0) == report.inflow_minor

    # Share is a fraction of the whole report, not of the parent.
    assert rows["Restaurants"].share == pytest.approx(6000 / 12000)
    assert rows["Food"].share == pytest.approx(1.0)

    assert rows["Dining"].parent_id == rows["Food"].category_id
    assert rows["Restaurants"].parent_id == rows["Dining"].category_id
    assert rows["Food"].parent_id is None


def test_parent_share_of_siblings_with_no_direct_parent_spend(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, _ = _seed(session)
        account = accounts["Checking"]
        dining = categories.ensure_path(session, "Food > Dining")
        restaurants = categories.ensure_path(session, "Food > Dining > Restaurants")
        fast_food = categories.ensure_path(session, "Food > Dining > Fast Food")
        session.flush()
        _txn(session, currency, account, date(2025, 3, 1), -3000, "A", category=restaurants)
        _txn(session, currency, account, date(2025, 3, 2), -1000, "B", category=fast_food)
        session.commit()

    window = _custom("2025-03-01", "2025-03-31")
    with session_factory() as session:
        report = stats.build_report(session, window)
    rows = {r.name: r for r in report.categories}

    # Dining holds no direct spend of its own, so its children's parent_share sums to 1.0.
    assert rows["Restaurants"].parent_share == pytest.approx(3000 / 4000)
    assert rows["Fast Food"].parent_share == pytest.approx(1000 / 4000)
    assert (
        rows["Restaurants"].parent_share + rows["Fast Food"].parent_share
        == pytest.approx(1.0)
    )

    # A depth-0 row has no parent, so its parent_share equals its share.
    assert rows["Dining"].parent_share == rows["Dining"].share == pytest.approx(1.0)


def test_parent_share_when_the_parent_has_its_own_direct_spend(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, _ = _seed(session)
        account = accounts["Checking"]
        dining = categories.ensure_path(session, "Food > Dining")
        restaurants = categories.ensure_path(session, "Food > Dining > Restaurants")
        session.flush()
        _txn(session, currency, account, date(2025, 3, 1), -3000, "A", category=restaurants)
        _txn(session, currency, account, date(2025, 3, 2), -1000, "B", category=dining)
        session.commit()

    window = _custom("2025-03-01", "2025-03-31")
    with session_factory() as session:
        report = stats.build_report(session, window)
    rows = {r.name: r for r in report.categories}

    # Dining's rolled outflow is 4000 (3000 + 1000), but only 3000 of it is Restaurants';
    # the rest is Dining's own, so its one child sums to less than 1.0.
    assert rows["Restaurants"].parent_share == pytest.approx(3000 / 4000)
    assert rows["Restaurants"].parent_share < 1.0


def test_parent_share_zero_denominator_guard(tmp_path):
    """A parent whose rolled-up outflow is zero (all income under it) must not divide by zero."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, _ = _seed(session)
        account = accounts["Checking"]
        salary = categories.ensure_path(session, "Income > Salary")
        session.flush()
        _txn(session, currency, account, date(2025, 3, 1), 200000, "Paycheck",
             category=salary)
        session.commit()

    window = _custom("2025-03-01", "2025-03-31")
    with session_factory() as session:
        report = stats.build_report(session, window)
    rows = {r.name: r for r in report.categories}

    assert rows["Income"].outflow_minor == 0
    assert rows["Salary"].outflow_minor == 0
    assert rows["Salary"].parent_share == 0.0
    assert rows["Salary"].share == 0.0


def test_uncategorised_takes_part_in_the_same_display_order(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, _ = _seed(session)
        account = accounts["Checking"]
        dining = categories.ensure_path(session, "Food > Dining")
        session.flush()
        _txn(session, currency, account, date(2025, 3, 1), -1000, "Snack", category=dining)
        _txn(session, currency, account, date(2025, 3, 2), -5000, "Mystery")  # uncategorised
        session.commit()

    window = _custom("2025-03-01", "2025-03-31")
    with session_factory() as session:
        report = stats.build_report(session, window)

    # Uncategorised (-5000) outspends Food (-1000), so it heads the depth-0 order.
    assert [(r.name, r.depth) for r in report.categories] == [
        (stats.UNCATEGORISED, 0), ("Food", 0), ("Dining", 1),
    ]
    uncategorised = next(r for r in report.categories if r.name == stats.UNCATEGORISED)
    assert uncategorised.parent_id is None
    assert uncategorised.category_id == queries.UNCATEGORISED_ID
    assert uncategorised.own_total_minor == uncategorised.total_minor


def test_get_currencies_reports_symbols_and_decimal_places(tmp_path):
    """Anything formatting minor units needs these, and decimal_places is not always 2."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        session.add(Currency(value="USD", symbol="$", decimal_places=2))
        session.add(Currency(value="JPY", symbol="¥", decimal_places=0))
        session.commit()
    with session_factory() as session:
        rows = {c.code: c for c in queries.get_currencies(session)}
    assert rows["USD"].symbol == "$" and rows["USD"].decimal_places == 2
    assert rows["JPY"].decimal_places == 0
