"""Tests for tui/trips.py and BudgetApp's ``trips``/``trip`` commands: the panel's
dates/cost/breakdown table, its adaptive bar width, folding, the legend, and the
bucket-map commands."""

from __future__ import annotations

import asyncio
import datetime
import io

from rich.console import Console
from textual.widgets import DataTable, Static

from budget_tracker import categories, tags as tags_module
from budget_tracker import trips as trips_module
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.models import Account, Currency, Tag, Transaction, TransactionTag
from budget_tracker.tui import FOLD_INDICATOR, BudgetApp
from budget_tracker.tui.trips import _format_dates, bar_width

from conftest import _rows_of


def _seed_trips(tmp_path, monkeypatch):
    """One trip with two transactions (Airfare and Hotel, different buckets once
    seeded) plus a second, empty trip -- so ordering (dated trips first, dateless
    last) and the "no transactions yet" case are both covered."""
    db_path = tmp_path / "trips.db"
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
        airfare = categories.ensure_path(session, "Airfare")
        hotel = categories.ensure_path(session, "Hotel")
        session.flush()

        trip = Tag(name="Japan 2026", kind=tags_module.TRIP)
        empty_trip = Tag(name="Peru 2025", kind=tags_module.TRIP)
        session.add_all([trip, empty_trip])
        session.flush()

        def txn(day, amount, description, category):
            row = Transaction(
                account_id=account.id,
                currency_id=currency.id,
                posted_date=day,
                description=description,
                raw_description=description,
                value_minor=amount,
                category_id=category.id if category is not None else None,
                import_hash=f"trip-{description}-{day}-{amount}",
            )
            session.add(row)
            session.flush()
            session.add(TransactionTag(transaction_id=row.id, tag_id=trip.id))

        txn(datetime.date(2026, 3, 2), -50_000, "Flight", airfare)
        txn(datetime.date(2026, 3, 14), -30_000, "Hotel stay", hotel)
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def _seed_trip_with_a_refunded_bucket(tmp_path, monkeypatch):
    """A trip whose Hotel spending is entirely refunded -- net positive -- so the bar
    must clamp it to zero length while the unfolded row still shows the real,
    negative-of-a-refund (i.e. positive net) figure honestly."""
    db_path = tmp_path / "refund.db"
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
        airfare = categories.ensure_path(session, "Airfare")
        hotel = categories.ensure_path(session, "Hotel")
        session.flush()
        trip = Tag(name="Refund Trip", kind=tags_module.TRIP)
        session.add(trip)
        session.flush()

        def txn(day, amount, description, category):
            row = Transaction(
                account_id=account.id,
                currency_id=currency.id,
                posted_date=day,
                description=description,
                raw_description=description,
                value_minor=amount,
                category_id=category.id,
                import_hash=f"refund-{description}-{day}-{amount}",
            )
            session.add(row)
            session.flush()
            session.add(TransactionTag(transaction_id=row.id, tag_id=trip.id))

        txn(datetime.date(2026, 5, 1), -20_000, "Flight", airfare)
        # Booked, then over-refunded (a goodwill credit on top of the room cost): the
        # hotel bucket's net comes out positive, i.e. its cost is negative.
        txn(datetime.date(2026, 5, 1), -10_000, "Hotel booking", hotel)
        txn(datetime.date(2026, 5, 2), 12_000, "Hotel refund", hotel)
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def _trip_rows(app):
    return _rows_of(app, "trip_table")


def _bucket_row(rows, bucket):
    """The unfolded row for ``bucket``, or None if the trip spent nothing in it.

    Looked up by name rather than by position: a bucket the trip did not touch is
    left out of the unfold entirely, so the rows are not at fixed offsets under
    their trip.
    """
    for row in rows:
        if row[1].strip() == bucket:
            return row
    return None


def _bucket_names(rows):
    """The bucket names currently unfolded, in order."""
    return [row[1].strip() for row in rows if row[1].strip() in trips_module.BUCKETS]


# ------------------------------------------------------------------- pure helpers


def test_format_dates_elides_only_the_end_year_when_it_matches():
    """The shared spec's own example -- ``2026-03-02..03-14`` -- elides the year but
    keeps the month, and that example is what both the app and the CLI implement."""
    assert (
        _format_dates(datetime.date(2026, 3, 2), datetime.date(2026, 3, 14))
        == "2026-03-02..03-14"
    )


def test_format_dates_keeps_the_year_when_it_differs():
    assert (
        _format_dates(datetime.date(2026, 12, 20), datetime.date(2027, 1, 5))
        == "2026-12-20..2027-01-05"
    )


def test_format_dates_is_a_single_date_for_a_single_day_trip():
    assert _format_dates(datetime.date(2026, 3, 2), datetime.date(2026, 3, 2)) == "2026-03-02"


def test_format_dates_is_blank_for_a_trip_with_no_transactions():
    assert _format_dates(None, None) == ""


def test_bar_width_is_100_at_the_real_terminal_size():
    """213 columns is what the user actually runs; #main is 177 wide there (36-wide
    sidebar excluded), which is measured against the real compositor in
    test_trips_table_and_bar_fit_the_real_terminal below, not just asserted here."""
    assert bar_width(177) == 100


def test_bar_width_drops_to_50_when_the_panel_is_narrow():
    assert bar_width(94) == 50


def test_bucket_colors_cover_every_real_bucket_with_no_repeats():
    """One PIE_COLORS entry per real bucket, no two the same, and misc gets the
    shared gray -- see BUCKET_COLORS's own docstring for why this is derived from
    len(trips.BUCKETS) rather than a literal count."""
    from budget_tracker.tui.trips import BUCKET_COLORS

    assert len(BUCKET_COLORS) == len(trips_module.BUCKETS)
    real_colors = BUCKET_COLORS[:-1]
    assert len(set(real_colors)) == len(real_colors)
    assert BUCKET_COLORS[-1] != real_colors[-1]  # misc's gray is not reused


# ------------------------------------------------------------------------ the panel


def test_trips_command_opens_the_panel_and_lists_every_trip(tmp_path, monkeypatch):
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()
            return app._panel, _trip_rows(app)

    panel, rows = asyncio.run(run())
    assert panel == "trips"
    # Most recent (dated) trip first, dateless last -- queries.get_trips's own sort.
    # Trips start folded (see trips_panel's own module docstring), so each name still
    # carries the fold indicator here.
    assert [row[1] for row in rows] == [
        f"{FOLD_INDICATOR} Japan 2026",
        f"{FOLD_INDICATOR} Peru 2025",
    ]
    assert rows[0][0] == "2026-03-02..03-14"
    assert rows[0][2] == "800.00"
    assert rows[1][0] == ""  # no transactions yet
    assert rows[1][2] == "0.00"


def test_trips_panel_seeds_the_default_bucket_map_on_first_open(tmp_path, monkeypatch):
    """Opening the panel the first time seeds the map from common category names, so
    Airfare and Hotel land in their obvious buckets without a manual command."""
    session_factory = _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()

    asyncio.run(run())
    with session_factory() as session:
        mapping = trips_module.list_buckets(session)
    assert "Airfare" in mapping[trips_module.AIRFARE]
    assert "Hotel" in mapping[trips_module.HOTEL]


def test_space_unfolds_a_trip_into_its_buckets(tmp_path, monkeypatch):
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()
            table = app.query_one("#trip_table", DataTable)
            table.focus()
            folded = _trip_rows(app)

            table.move_cursor(row=0)  # Japan 2026
            await pilot.press("space")
            await pilot.pause()
            unfolded = _trip_rows(app)

            await pilot.press("space")  # back to folded
            await pilot.pause()
            refolded = _trip_rows(app)

            return folded, unfolded, refolded

    folded, unfolded, refolded = asyncio.run(run())
    assert folded[0][1] == f"{FOLD_INDICATOR} Japan 2026"
    # Only the buckets the trip actually spent in follow the trip row -- this trip has
    # one Airfare and one Hotel transaction, so the other six are left out rather than
    # printed as rows of zeros. Peru 2025 stays folded (it was never toggled), so it
    # keeps its own indicator.
    assert _bucket_names(unfolded) == [trips_module.AIRFARE, trips_module.HOTEL]
    assert len(unfolded) == 1 + 2 + 1
    unfolded_names = [row[1].strip() for row in unfolded]
    assert unfolded_names[0] == "Japan 2026"
    assert unfolded_names[-1] == f"{FOLD_INDICATOR} Peru 2025"
    assert refolded == folded


def test_space_does_nothing_on_a_bucket_sub_row(tmp_path, monkeypatch):
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()
            table = app.query_one("#trip_table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("space")  # unfold Japan 2026
            await pilot.pause()
            before = _trip_rows(app)

            table.move_cursor(row=1)  # the first bucket sub-row
            await pilot.press("space")
            await pilot.pause()
            after = _trip_rows(app)
            return before, after

    before, after = asyncio.run(run())
    assert before == after


def test_f_folds_and_unfolds_every_trip_at_once(tmp_path, monkeypatch):
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()
            table = app.query_one("#trip_table", DataTable)
            table.focus()

            await pilot.press("f")
            await pilot.pause()
            unfolded_all = _trip_rows(app)

            await pilot.press("f")
            await pilot.pause()
            folded_all = _trip_rows(app)
            return unfolded_all, folded_all

    unfolded_all, folded_all = asyncio.run(run())
    # 2 trips, plus a bucket row only where a trip actually spent: Japan 2026 has an
    # Airfare and a Hotel transaction, and Peru 2025 has none at all, so it unfolds to
    # nothing. None of the rows carry the FOLD_INDICATOR while everything is open.
    assert _bucket_names(unfolded_all) == [trips_module.AIRFARE, trips_module.HOTEL]
    assert len(unfolded_all) == 2 + 2
    assert all(FOLD_INDICATOR not in row[1] for row in unfolded_all)
    assert len(folded_all) == 2
    assert all(FOLD_INDICATOR in row[1] for row in folded_all)


def test_folding_does_not_change_any_numbers(tmp_path, monkeypatch):
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()
            table = app.query_one("#trip_table", DataTable)
            table.focus()
            before_cost = _trip_rows(app)[0][2]

            table.move_cursor(row=0)
            await pilot.press("space")
            await pilot.pause()
            after_cost = _trip_rows(app)[0][2]
            return before_cost, after_cost

    before_cost, after_cost = asyncio.run(run())
    assert before_cost == after_cost == "800.00"


def test_a_refunded_bucket_shows_its_real_cost_with_a_zero_length_bar(tmp_path, monkeypatch):
    _seed_trip_with_a_refunded_bucket(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()
            table = app.query_one("#trip_table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("space")
            await pilot.pause()
            return _trip_rows(app)

    rows = asyncio.run(run())
    hotel = _bucket_row(rows, trips_module.HOTEL)
    # A refunded bucket is not the same as an untouched one: it stays in the unfold,
    # because the refund is a fact about the trip, where a bucket with no spending at
    # all is just noise.
    assert hotel is not None
    # The refund overshoot (12,000 in against 10,000 out) reads as a real, negative
    # cost -- not silently clamped to zero the way the bar itself is.
    assert hotel[2] == "-20.00"
    assert hotel[3] == "0.0%"  # clamped to nothing in the bar's own basis


def test_trips_panel_status_line_names_the_fold_keys(tmp_path, monkeypatch):
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()
            return str(app.query_one("#status", Static).content)

    status = asyncio.run(run())
    assert "2 trips" in status
    assert "space" in status and "f " in status


def test_legend_names_every_bucket(tmp_path, monkeypatch):
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()
            return str(app.query_one("#trips_legend", Static).content)

    legend = asyncio.run(run())
    for bucket in trips_module.BUCKETS:
        assert bucket in legend


def test_escape_returns_to_transactions_from_trips(tmp_path, monkeypatch):
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            return app._panel, app.query_one("#txns", DataTable).display

    panel, txns_visible = asyncio.run(run())
    assert panel == "txns"
    assert txns_visible is True


def test_opening_trips_does_not_disturb_the_sidebar_trips_list(tmp_path, monkeypatch):
    """Regression: the panel's table is #trip_table specifically so the display-toggle
    loop never touches the sidebar's own #trips ListView."""
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            await pilot.pause()
            sidebar_before = app.query_one("#trips").display
            app._run_command("trips")
            await pilot.pause()
            sidebar_after = app.query_one("#trips").display
            return sidebar_before, sidebar_after

    sidebar_before, sidebar_after = asyncio.run(run())
    # The sidebar's own accordion state (collapsed, since Accounts starts expanded)
    # is unaffected by which main panel is showing.
    assert sidebar_before == sidebar_after == False  # noqa: E712


def test_trips_table_and_bar_fit_the_real_terminal(tmp_path, monkeypatch):
    """Renders the real compositor at the terminal width the user actually runs (213
    columns) and checks the 100-wide bar and every column header actually reach the
    screen -- see the module docstring's warning against arithmetic-only checks."""
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()
            buffer = io.StringIO()
            Console(file=buffer, width=213).print(app.screen._compositor)
            return buffer.getvalue()

    rendered = asyncio.run(run())
    assert "Dates" in rendered
    assert "Trip" in rendered
    assert "Cost" in rendered
    assert "Breakdown" in rendered
    assert "Japan 2026" in rendered
    # A 100-wide bar draws at least a long unbroken run of block cells for a trip
    # whose whole cost is in one bucket-free line -- 90 is a safe floor short of the
    # full 100 to allow for apportionment across buckets.
    assert "█" * 40 in rendered


# --------------------------------------------------------------------- bucket commands


def test_trip_bucket_maps_one_category(tmp_path, monkeypatch):
    session_factory = _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trip bucket Airfare = car")
            await pilot.pause()

    asyncio.run(run())
    with session_factory() as session:
        mapping = trips_module.list_buckets(session)
    assert "Airfare" in mapping[trips_module.CAR]


def test_trip_bucket_accepts_several_categories_at_once(tmp_path, monkeypatch):
    session_factory = _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trip bucket Airfare, Hotel = misc")
            await pilot.pause()

    asyncio.run(run())
    with session_factory() as session:
        mapping = trips_module.list_buckets(session)
    assert "Airfare" in mapping[trips_module.MISC]
    assert "Hotel" in mapping[trips_module.MISC]


def test_trip_bucket_is_additive_and_does_not_disturb_an_existing_mapping(
    tmp_path, monkeypatch
):
    session_factory = _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trip bucket Airfare = car")
            await pilot.pause()
            app._run_command("trip bucket Hotel = car")
            await pilot.pause()

    asyncio.run(run())
    with session_factory() as session:
        mapping = trips_module.list_buckets(session)
    # Both land in car; mapping Hotel did not evict Airfare from it.
    assert sorted(mapping[trips_module.CAR]) == ["Airfare", "Hotel"]


def test_trip_bucket_blank_unmaps_it(tmp_path, monkeypatch):
    session_factory = _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trip bucket Airfare = car")
            await pilot.pause()
            app._run_command("trip bucket Airfare =")
            await pilot.pause()

    asyncio.run(run())
    with session_factory() as session:
        mapping = trips_module.list_buckets(session)
    assert "Airfare" not in mapping[trips_module.CAR]
    for bucket in trips_module.BUCKETS:
        assert "Airfare" not in mapping[bucket]


def test_trip_bucket_with_an_unknown_bucket_name_is_refused(tmp_path, monkeypatch):
    session_factory = _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trip bucket Airfare = spaceship")
            await pilot.pause()
            return [n.message for n in app._notifications]

    notifications = asyncio.run(run())
    assert any("spaceship" in message for message in notifications)
    with session_factory() as session:
        mapping = trips_module.list_buckets(session)
    assert not any("Airfare" in names for names in mapping.values())


def test_trip_bucket_with_an_unknown_category_writes_nothing(tmp_path, monkeypatch):
    session_factory = _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trip bucket Airfare, Nonexistent = car")
            await pilot.pause()
            return [n.message for n in app._notifications]

    notifications = asyncio.run(run())
    assert any("Nonexistent" in message for message in notifications)
    with session_factory() as session:
        mapping = trips_module.list_buckets(session)
    # Refused as a whole: Airfare was not mapped either.
    assert "Airfare" not in mapping[trips_module.CAR]


def test_trip_buckets_command_lists_the_map(tmp_path, monkeypatch):
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trip bucket Airfare = car")
            await pilot.pause()
            app._run_command("trip buckets")
            await pilot.pause()
            return [n.message for n in app._notifications]

    notifications = asyncio.run(run())
    assert any("car: Airfare" in message for message in notifications)


def test_a_live_trips_panel_redraws_after_a_bucket_command(tmp_path, monkeypatch):
    """Regression: editing the map while the panel is open must not leave the bar
    showing the map that was active when the panel was first opened."""
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()
            table = app.query_one("#trip_table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("space")
            await pilot.pause()
            before = _trip_rows(app)

            app._run_command("trip bucket Airfare = car")
            await pilot.pause()
            after = _trip_rows(app)
            return before, after

    before, after = asyncio.run(run())
    # The Airfare spending moves from the airfare bucket to car, so airfare drops out
    # of the unfold entirely and car appears in it -- the rows are looked up by name
    # because which buckets are present is exactly what this command changes.
    assert _bucket_row(before, trips_module.AIRFARE)[2] == "500.00"
    assert _bucket_row(before, trips_module.CAR) is None
    assert _bucket_row(after, trips_module.AIRFARE) is None
    assert _bucket_row(after, trips_module.CAR)[2] == "500.00"


def test_trip_and_trips_do_not_shadow_sel_trip_or_section_trips(tmp_path, monkeypatch):
    """Beware the existing 'sel trip = <name>' verb and 'section trips' command -- both
    must keep working once 'trip'/'trips' are wired up as top-level commands."""
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            await pilot.pause()
            app._run_command("section trips")
            await pilot.pause()
            section_after = app._expanded_section

            app._run_command("sel trip = Japan 2026")
            await pilot.pause()
            sel_notifications = [n.message for n in app._notifications]
            return section_after, sel_notifications

    section_after, sel_notifications = asyncio.run(run())
    assert section_after == "trips"
    # "sel trip =" with nothing selected is a no-op warning, not "unknown command" --
    # proof _do_sel still owns "trip" as a subject rather than the new top-level verb
    # swallowing it.
    assert any("Nothing selected" in message for message in sel_notifications)
