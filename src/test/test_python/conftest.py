"""Shared fixtures and helpers for the split TUI test suite.

Every ``test_tui_*.py`` file imports the specific pieces it needs from here rather
than redefining them, so a fixture only has one place to go stale. The autouse
notification fixture applies to every test in this directory automatically, without
being imported.
"""

from __future__ import annotations

import datetime

import pytest
from textual.widgets import DataTable, Static

from budget_tracker import categories, charts, stats
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.importer import import_csv
from budget_tracker.models import Account, Currency, Transaction
from budget_tracker.tui import BudgetApp


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Stop any test reaching the network, whether or not it meant to.

    Importing a statement in a foreign currency now fetches exchange rates, so four
    tests that merely imported a CHF or EUR file began calling api.frankfurter.dev for
    real -- eight requests between them, counting the retry. They still passed, because
    a failed fetch deliberately never fails an import, which is exactly what made it
    invisible: the suite quietly depended on the network and nobody would have noticed
    until it ran on a plane or in a sandbox, where each of those tests waits out two
    forty-second timeouts instead.

    Refusing at ``urlopen`` rather than per test means a test cannot acquire the
    dependency by accident later. The error is an OSError because that is what an
    unreachable host raises, so code paths that already handle being offline are
    exercised honestly rather than seeing an exception no real network produces.
    Anything that wants to test fetching stubs ``fetch_ecb_rates`` or its ``fetch``
    argument, well above this line.
    """

    def refuse(*args, **kwargs):
        raise OSError(
            "network access is disabled in tests; stub fetch_ecb_rates or its "
            "fetch argument instead of reaching a real host"
        )

    monkeypatch.setattr("urllib.request.urlopen", refuse)
from helpers import learn_format


@pytest.fixture(autouse=True)
def _keep_notifications_for_the_length_of_a_test(monkeypatch):
    """Stop Textual expiring notifications while a test is still looking at them.

    ``App.NOTIFICATION_TIMEOUT`` is 5 seconds and ``Notifications.__iter__`` reaps
    expired entries as a side effect of iterating, so roughly thirty assertions in this
    file that read ``app._notifications`` can find it empty purely because the machine
    was busy — the notification was raised, then quietly deleted before it was read.
    That surfaced as a single unreproducible failure in a full-suite run that passed
    when the same test ran alone.

    Nothing about what the tests assert changes; this only stops the wall clock from
    being part of the assertion.
    """
    monkeypatch.setattr(BudgetApp, "NOTIFICATION_TIMEOUT", 3600, raising=False)


CSV = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2025-07-01,2025-07-02,8207,COFFEE SHOP A,Dining,3.00,
2025-07-01,2025-07-02,8207,COFFEE SHOP B,Dining,4.00,
2025-07-03,2025-07-04,8207,COFFEE SHOP A,Dining,3.50,
"""


def _setup(tmp_path, monkeypatch):
    """Seed a DB and point the app at it via the BUDGET_DB override."""
    db_path = tmp_path / "t.db"
    engine = get_engine(db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    csv_path = tmp_path / "in.csv"
    csv_path.write_text(CSV, encoding="utf-8")
    with session_factory() as session:
        learn_format(session, csv_path)
        import_csv(session, csv_path)
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def _rows_of(app, table_id):
    table = app.query_one(f"#{table_id}", DataTable)
    return [[str(c) for c in table.get_row_at(i)] for i in range(table.row_count)]


def _panel_state(app):
    """(transactions visible, rules visible, status line)."""
    return (
        app.query_one("#txns", DataTable).display,
        app.query_one("#rules", DataTable).display,
        str(app.query_one("#status", Static).content),
    )


RECENT_ROWS = (
    # (days ago, description, category, debit, credit)
    (3, "CAFE ONE", "Dining", "10.00", ""),
    (6, "CAFE TWO", "Dining", "30.00", ""),
    (9, "RENT PAYMENT", "Housing", "1000.00", ""),
    (5, "PAYCHECK", "Income", "", "2000.00"),
    (100, "OLD SHOP", "Dining", "500.00", ""),  # outside 1m/3m, inside 6m
)


def _recent_csv():
    today = datetime.date.today()
    lines = ["Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit"]
    for days, description, category, debit, credit in RECENT_ROWS:
        day = (today - datetime.timedelta(days=days)).isoformat()
        lines.append(f"{day},{day},8207,{description},{category},{debit},{credit}")
    return "\n".join(lines) + "\n"


def _setup_recent(tmp_path, monkeypatch):
    """A database holding only the relative-dated rows, so window totals are exact."""
    db_path = tmp_path / "recent.db"
    engine = get_engine(db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    csv_path = tmp_path / "recent.csv"
    csv_path.write_text(_recent_csv(), encoding="utf-8")
    with session_factory() as session:
        learn_format(session, csv_path)
        import_csv(session, csv_path)
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def _stats_rows(app):
    return _rows_of(app, "stats_table")


def _stats_state(app):
    """(picker visible, stats visible, status line)."""
    return (
        app.query_one("#periods", DataTable).display,
        app.query_one("#stats").display,
        str(app.query_one("#status", Static).content),
    )


def _category_of(app, description):
    """The Category cell the transactions table shows for a description."""
    table = app.query_one("#txns", DataTable)
    for index, txn in enumerate(app._txns):
        if txn.description == description:
            # Column 4: select, date, description, vendor, then category.
            return str(table.get_row_at(index)[4])
    return None


def _seed_category_hierarchy(tmp_path, monkeypatch):
    """``Food > Dining > {Restaurants and Fast Casual Spots, Fast Food}``, ``Food > Groceries``.

    Six-figure amounts and a long leaf name, so the width guard below measures
    something real rather than passing vacuously on a handful of seeded dollars (see
    test_stats_status_line_fits_the_main_panel for the same concern on the status line).
    """
    db_path = tmp_path / "deep.db"
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
        restaurants = categories.ensure_path(
            session, "Food > Dining > Restaurants and Fast Casual Spots"
        )
        fast_food = categories.ensure_path(session, "Food > Dining > Fast Food")
        groceries = categories.ensure_path(session, "Food > Groceries")
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
                    import_hash=f"deep-{description}-{day}-{amount}",
                )
            )

        txn(datetime.date(2025, 3, 1), -12_345_678, "Big meal", restaurants)
        txn(datetime.date(2025, 3, 2), -2_345_678, "Fast food", fast_food)
        txn(datetime.date(2025, 3, 3), -3_456_789, "Groceries", groceries)
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def _seed_category_hierarchy_with_income_sibling(tmp_path, monkeypatch):
    """``_seed_category_hierarchy``'s Food tree, plus a leaf ``Income`` category beside
    it — so folding every group leaves one visible, non-foldable row (Income) whose
    table index still has to map back to the right category (see
    test_drill_down_after_fold_all_maps_to_the_row_actually_visible).
    """
    db_path = tmp_path / "deep_with_income.db"
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
        restaurants = categories.ensure_path(
            session, "Food > Dining > Restaurants and Fast Casual Spots"
        )
        fast_food = categories.ensure_path(session, "Food > Dining > Fast Food")
        groceries = categories.ensure_path(session, "Food > Groceries")
        income = categories.ensure_path(session, "Income")
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
                    import_hash=f"income-{description}-{day}-{amount}",
                )
            )

        txn(datetime.date(2025, 3, 1), -12_345_678, "Big meal", restaurants)
        txn(datetime.date(2025, 3, 2), -2_345_678, "Fast food", fast_food)
        txn(datetime.date(2025, 3, 3), -3_456_789, "Groceries", groceries)
        txn(datetime.date(2025, 3, 4), 500_000, "Paycheck", income)
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


CHART_ROWS = (
    # (day, minor amount, description, category)
    (datetime.date(2026, 1, 15), -2000, "SHOP A", "Food"),
    (datetime.date(2026, 1, 20), -1000, "SHOP B", "Food"),
    (datetime.date(2026, 2, 10), -500, "SHOP C", None),
    (datetime.date(2026, 3, 5), 300_000, "PAYCHECK", None),
    (datetime.date(2026, 3, 6), -4000, "SHOP D", "Travel"),
)

# The window the chart tests use, chosen so month buckets land on the seeded data
# exactly. Out: January 30.00, February 5.00, March 40.00. In: March 3,000.00.


CHART_WINDOW = "2026-01-01..2026-03-31"

# 13 cells either side of the axis, per charts.DEFAULT_WIDTH.


HALF = 13


def _setup_chart(tmp_path, monkeypatch):
    """Absolute dates, seeded directly, so bucket boundaries are not relative to today."""
    db_path = tmp_path / "chart.db"
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
        for day, amount, description, category_name in CHART_ROWS:
            category = (
                categories.ensure_path(session, category_name)
                if category_name
                else None
            )
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
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def _chart_rows(app):
    return _rows_of(app, "chart")


def _chart_headers(app):
    table = app.query_one("#chart", DataTable)
    return [str(column.label) for column in table.columns.values()]


def test_the_network_guard_is_active():
    """A guard nobody checks is a guard that quietly stops working."""
    import urllib.request

    with pytest.raises(OSError, match="network access is disabled"):
        urllib.request.urlopen("https://example.invalid")
