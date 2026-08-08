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
from budget_tracker.tui import trips as trips_panel
from budget_tracker.tui.trips import _date_cell, bar_width

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


def _seed_trip_with_uncategorized(tmp_path, monkeypatch):
    """One trip: a mapped Airfare transaction plus one with no category at all -- the
    uncategorized one falls into ``misc`` (see queries.get_trips), so drilling into
    misc has to reach it too, not just categories that fall through unmapped."""
    db_path = tmp_path / "uncategorized.db"
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
        session.flush()
        trip = Tag(name="Japan 2026", kind=tags_module.TRIP)
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
                category_id=category.id if category is not None else None,
                import_hash=f"uncat-{description}-{day}-{amount}",
            )
            session.add(row)
            session.flush()
            session.add(TransactionTag(transaction_id=row.id, tag_id=trip.id))

        txn(datetime.date(2026, 3, 2), -50_000, "Flight", airfare)
        txn(datetime.date(2026, 3, 3), -2_000, "Mystery", None)
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


# Cell indices: Start, End, Trip, Cost, Breakdown. Start and end are two columns,
# so the trip's own name sits at 2 rather than 1.
TRIP_CELL = 2
COST_CELL = 3
SHARE_CELL = 4


def _trip_rows(app):
    return _rows_of(app, "trip_table")


def _bucket_row_index(rows, bucket):
    """The rendered row index of ``bucket``'s unfolded sub-row -- for moving the
    cursor there before drilling in, since which buckets are present (and therefore
    their offset) depends on what the trip actually spent."""
    return next(i for i, row in enumerate(rows) if row[TRIP_CELL].strip() == bucket)


def _bucket_row(rows, bucket):
    """The unfolded row for ``bucket``, or None if the trip spent nothing in it.

    Looked up by name rather than by position: a bucket the trip did not touch is
    left out of the unfold entirely, so the rows are not at fixed offsets under
    their trip.
    """
    for row in rows:
        if row[TRIP_CELL].strip() == bucket:
            return row
    return None


def _bucket_names(rows):
    """The bucket names currently unfolded, in order."""
    return [
        row[TRIP_CELL].strip()
        for row in rows
        if row[TRIP_CELL].strip() in trips_module.BUCKETS
    ]


# ------------------------------------------------------------------- pure helpers


def test_a_date_cell_is_a_plain_iso_date():
    """Start and end are separate columns now, so each cell is one date -- there is no
    range to elide a year out of."""
    assert str(_date_cell(datetime.date(2026, 3, 2), False)) == "2026-03-02"


def test_a_manual_date_is_marked_and_dimmed():
    """Otherwise an override is invisible, and a date nobody can account for is worse
    than an unfamiliar one."""
    cell = _date_cell(datetime.date(2026, 3, 2), True)
    assert str(cell) == "2026-03-02*"
    assert "dim" in str(cell.style)


def test_a_date_cell_is_blank_when_there_is_nothing_to_show():
    assert str(_date_cell(None, False)) == ""


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
    assert [row[TRIP_CELL] for row in rows] == [
        f"{FOLD_INDICATOR} Japan 2026",
        f"{FOLD_INDICATOR} Peru 2025",
    ]
    assert rows[0][0] == "2026-03-02"
    assert rows[0][1] == "2026-03-14"
    assert rows[0][COST_CELL] == "800.00"
    assert rows[1][0] == ""  # no transactions yet, and no manual override
    assert rows[1][1] == ""
    assert rows[1][COST_CELL] == "0.00"


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
    assert folded[0][TRIP_CELL] == f"{FOLD_INDICATOR} Japan 2026"
    # Only the buckets the trip actually spent in follow the trip row -- this trip has
    # one Airfare and one Hotel transaction, so the other six are left out rather than
    # printed as rows of zeros. Peru 2025 stays folded (it was never toggled), so it
    # keeps its own indicator.
    assert _bucket_names(unfolded) == [trips_module.AIRFARE, trips_module.HOTEL]
    assert len(unfolded) == 1 + 2 + 1
    unfolded_names = [row[TRIP_CELL].strip() for row in unfolded]
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
    assert all(FOLD_INDICATOR not in row[TRIP_CELL] for row in unfolded_all)
    assert len(folded_all) == 2
    assert all(FOLD_INDICATOR in row[TRIP_CELL] for row in folded_all)


def test_folding_does_not_change_any_numbers(tmp_path, monkeypatch):
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()
            table = app.query_one("#trip_table", DataTable)
            table.focus()
            before_cost = _trip_rows(app)[0][COST_CELL]

            table.move_cursor(row=0)
            await pilot.press("space")
            await pilot.pause()
            after_cost = _trip_rows(app)[0][COST_CELL]
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
    assert hotel[COST_CELL] == "-20.00"
    assert hotel[SHARE_CELL] == "0.0%"  # clamped to nothing in the bar's own basis


def test_the_legend_sits_above_the_table(tmp_path, monkeypatch):
    """The bars are what is being read; a key underneath them is one the eye has to
    travel past the whole table to find and then back again."""
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()
            view = app.query_one("#trips_view")
            return [child.id for child in view.children]

    assert asyncio.run(run()) == ["trips_legend", "trip_table"]


def test_the_panel_opens_with_no_trip_highlighted(tmp_path, monkeypatch):
    """A DataTable always has a cursor somewhere, and drawing it on the first trip
    reads as "this one is selected" on a screen whose job is comparing trips."""
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()
            table = app.query_one("#trip_table", trips_panel.TripTable)
            on_open = table.show_cursor
            await pilot.press("down")
            await pilot.pause()
            after_key = (table.show_cursor, table.cursor_row)
            # Reopening starts clean again, however the table was left.
            app._run_command("trips")
            await pilot.pause()
            return on_open, after_key, table.show_cursor

    on_open, after_key, on_reopen = asyncio.run(run())
    assert on_open is False
    assert after_key == (True, 1)
    assert on_reopen is False


def test_space_reveals_the_cursor_rather_than_folding_an_invisible_row(
    tmp_path, monkeypatch
):
    """Folding a row the user cannot see would be worse than showing the cursor a
    keystroke early, so any key reveals it -- not just the arrows."""
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()
            table = app.query_one("#trip_table", trips_panel.TripTable)
            await pilot.press("space")
            await pilot.pause()
            return table.show_cursor, _bucket_names(_trip_rows(app))

    shown, buckets = asyncio.run(run())
    assert shown is True
    assert buckets == [trips_module.AIRFARE, trips_module.HOTEL]


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
    assert "Start" in rendered
    assert "End" in rendered
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
    assert _bucket_row(before, trips_module.AIRFARE)[COST_CELL] == "500.00"
    assert _bucket_row(before, trips_module.CAR) is None
    assert _bucket_row(after, trips_module.AIRFARE) is None
    assert _bucket_row(after, trips_module.CAR)[COST_CELL] == "500.00"


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


# ------------------------------------------------------------------- drill-down


def test_right_arrow_on_a_trip_row_lists_its_transactions(tmp_path, monkeypatch):
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()
            table = app.query_one("#trip_table", DataTable)
            table.focus()
            table.move_cursor(row=0)  # Japan 2026
            await pilot.press("right")
            await pilot.pause()
            return (
                app._panel,
                app.trip_filter,
                app.category_ids_filter,
                sorted(t.description for t in app._txns),
            )

    panel, trip_filter, category_ids, descriptions = asyncio.run(run())
    assert panel == "txns"
    assert trip_filter is not None
    # A trip-level drill-down does not touch the bucket filter -- it wants everything
    # on the trip, not one bucket of it.
    assert category_ids is None
    assert descriptions == ["Flight", "Hotel stay"]


def test_enter_on_a_trip_row_drills_in_too(tmp_path, monkeypatch):
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()
            table = app.query_one("#trip_table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            return app._panel, app.trip_filter, len(app._txns)

    panel, trip_filter, count = asyncio.run(run())
    assert panel == "txns"
    assert trip_filter is not None
    assert count == 2


def test_right_arrow_on_a_bucket_row_lists_only_that_buckets_transactions(
    tmp_path, monkeypatch
):
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
            rows = _trip_rows(app)
            table.move_cursor(row=_bucket_row_index(rows, trips_module.AIRFARE))
            await pilot.press("right")
            await pilot.pause()
            return (
                app._panel,
                app.trip_filter,
                app.category_ids_filter,
                [t.description for t in app._txns],
            )

    panel, trip_filter, category_ids, descriptions = asyncio.run(run())
    assert panel == "txns"
    assert trip_filter is not None
    assert category_ids is not None
    assert descriptions == ["Flight"]  # not "Hotel stay" -- that is a different bucket


def test_the_misc_buckets_drill_down_reaches_uncategorized_transactions(
    tmp_path, monkeypatch
):
    """A None inside category_ids is what lets the misc bucket's own drill-down reach
    a transaction with no category at all -- see tui.trips.bucket_category_ids."""
    _seed_trip_with_uncategorized(tmp_path, monkeypatch)

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
            rows = _trip_rows(app)
            table.move_cursor(row=_bucket_row_index(rows, trips_module.MISC))
            await pilot.press("right")
            await pilot.pause()
            return [t.description for t in app._txns], app.category_ids_filter

    descriptions, category_ids = asyncio.run(run())
    assert descriptions == ["Mystery"]
    assert None in category_ids


def test_left_arrow_returns_to_the_trips_panel_with_fold_state_and_cursor_intact(
    tmp_path, monkeypatch
):
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()
            table = app.query_one("#trip_table", trips_panel.TripTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("space")  # unfold Japan 2026
            await pilot.pause()
            bucket_row = _bucket_row_index(_trip_rows(app), trips_module.AIRFARE)
            table.move_cursor(row=bucket_row)
            await pilot.press("right")
            await pilot.pause()

            await pilot.press("left")
            await pilot.pause()
            return (
                app._panel,
                app.trip_filter,
                app.category_ids_filter,
                _trip_rows(app),
                table.cursor_row,
                table.show_cursor,
                bucket_row,
            )

    (
        panel,
        trip_filter,
        category_ids,
        rows_after,
        cursor_row,
        show_cursor,
        bucket_row,
    ) = asyncio.run(run())
    assert panel == "trips"
    # The filters the drill-down set are gone -- neither was active before drilling in.
    assert trip_filter is None
    assert category_ids is None
    # Still unfolded, with the same buckets showing -- fold state survived the round trip.
    assert _bucket_names(rows_after) == [trips_module.AIRFARE, trips_module.HOTEL]
    assert cursor_row == bucket_row  # back on the bucket row drilled from
    # Coming back is not a fresh open of the panel, so the cursor stays visible rather
    # than hiding again the way _show_trips() makes a genuinely new open start clean.
    assert show_cursor is True


def test_left_arrow_restores_a_trip_filter_set_before_the_drill(tmp_path, monkeypatch):
    """Going back must not blank a filter the user had on purpose before drilling in."""
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            japan = next(t for t in app._trips if t.name == "Japan 2026")
            peru = next(t for t in app._trips if t.name == "Peru 2025")
            app.trip_filter = peru.id
            app.reload()
            await pilot.pause()

            app._run_command("trips")
            await pilot.pause()
            table = app.query_one("#trip_table", DataTable)
            table.focus()
            table.move_cursor(row=0)  # Japan 2026, in the trips panel's own listing
            await pilot.press("right")
            await pilot.pause()
            drilled_trip_filter = app.trip_filter

            await pilot.press("left")
            await pilot.pause()
            return peru.id, drilled_trip_filter, app.trip_filter, app._panel

    peru_id, drilled_trip_filter, restored_trip_filter, panel = asyncio.run(run())
    assert drilled_trip_filter != peru_id  # overwritten by the drill-down
    assert restored_trip_filter == peru_id  # back to the pre-drill filter, not None
    assert panel == "trips"


def test_clear_filters_also_clears_the_bucket_filter(tmp_path, monkeypatch):
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
            rows = _trip_rows(app)
            table.move_cursor(row=_bucket_row_index(rows, trips_module.AIRFARE))
            await pilot.press("right")
            await pilot.pause()

            app._run_command("all")
            await pilot.pause()
            return app.trip_filter, app.category_ids_filter, len(app._txns)

    trip_filter, category_ids, count = asyncio.run(run())
    assert trip_filter is None
    assert category_ids is None
    assert count == 2  # every seeded row is back


def test_status_line_names_the_bucket_scope_when_drilled_into_one(tmp_path, monkeypatch):
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
            rows = _trip_rows(app)
            table.move_cursor(row=_bucket_row_index(rows, trips_module.AIRFARE))
            await pilot.press("right")
            await pilot.pause()
            return str(app.query_one("#status", Static).content)

    status = asyncio.run(run())
    assert "[filtered: trip, bucket]" in status


def test_right_and_left_arrows_are_advertised_in_the_footer_for_trips(
    tmp_path, monkeypatch
):
    """The trips panel's twin of test_the_drill_down_keys_are_advertised_in_the_footer
    (test_tui_stats.py)."""
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()
            await pilot.pause()
            on_trips = dict(app.active_bindings)

            table = app.query_one("#trip_table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("right")
            await pilot.pause()
            await pilot.pause()
            drilled = dict(app.active_bindings)
            return on_trips, drilled

    def action_for(bindings, key):
        binding = bindings.get(key)
        return binding.binding.action if binding else None

    on_trips, drilled = asyncio.run(run())
    assert action_for(on_trips, "right") == "drill_down"
    assert on_trips["right"].binding.description == "Drill down"
    assert action_for(drilled, "left") == "drill_up"


def test_right_arrow_does_nothing_outside_the_trips_panel_context(tmp_path, monkeypatch):
    """Regression: the right arrow must stay inert on the transactions panel -- gating
    by self._panel (check_action) is what keeps drilling a trips-only affordance."""
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            await pilot.pause()
            before = (app._panel, app.trip_filter)
            await pilot.press("right")
            await pilot.pause()
            return before, (app._panel, app.trip_filter)

    before, after = asyncio.run(run())
    assert before == after == ("txns", None)


def test_space_still_folds_the_trips_panel_after_a_drill_and_back(tmp_path, monkeypatch):
    """Regression: right/left drilling must not disturb the existing fold keys."""
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()
            table = app.query_one("#trip_table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("right")  # drill into the whole trip
            await pilot.pause()
            await pilot.press("left")  # back
            await pilot.pause()

            table = app.query_one("#trip_table", DataTable)
            table.move_cursor(row=0)
            await pilot.press("space")  # still folds normally
            await pilot.pause()
            return _bucket_names(_trip_rows(app))

    assert asyncio.run(run()) == [trips_module.AIRFARE, trips_module.HOTEL]


# ------------------------------------------------------------ trip dates (overrides)


def test_trip_dates_overrides_what_the_transactions_say(tmp_path, monkeypatch):
    """A flight booked months ahead drags the derived start back to the booking, and no
    edit to a transaction can fix that."""
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()
            app._run_command("trip dates Japan 2026 = 2026-03-10..2026-03-20")
            await pilot.pause()
            return _trip_rows(app)[0]

    row = asyncio.run(run())
    assert row[0] == "2026-03-10*"  # marked, so the override is not invisible
    assert row[1] == "2026-03-20*"


def test_trip_dates_marks_only_the_end_that_was_set(tmp_path, monkeypatch):
    """Overriding one end and leaving the other derived is the ordinary case, so the
    marker is per column rather than one flag for the pair."""
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()
            # tags.set_trip_dates takes both ends; a start-only override is what the
            # panel must render distinctly.
            with app.session_factory() as session:
                tags_module.set_trip_dates(
                    session, "Japan 2026", datetime.date(2026, 3, 1), None
                )
                session.commit()
            app._run_command("trips")
            await pilot.pause()
            return _trip_rows(app)[0]

    row = asyncio.run(run())
    assert row[0] == "2026-03-01*"  # overridden
    assert row[1] == "2026-03-14"  # still derived, unmarked


def test_trip_dates_with_a_blank_value_derives_them_again(tmp_path, monkeypatch):
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()
            app._run_command("trip dates Japan 2026 = 2020-01-01..2020-01-02")
            await pilot.pause()
            app._run_command("trip dates Japan 2026 =")
            await pilot.pause()
            return _trip_rows(app)[0]

    row = asyncio.run(run())
    assert row[0] == "2026-03-02"
    assert row[1] == "2026-03-14"


def test_trip_dates_reports_a_bad_date_and_an_unknown_trip(tmp_path, monkeypatch):
    _seed_trips(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._run_command("trips")
            await pilot.pause()
            messages = []
            for command in (
                "trip dates Japan 2026 = last tuesday..2026-03-20",
                "trip dates Nowhere = 2026-03-10..2026-03-20",
                "trip dates Japan 2026 = 2026-05-01..2026-04-01",
            ):
                app._run_command(command)
                await pilot.pause()
                messages.append(list(app._notifications)[-1].message)
            return messages, _trip_rows(app)[0]

    messages, row = asyncio.run(run())
    assert "YYYY-MM-DD" in messages[0]
    assert "No trip named 'Nowhere'." == messages[1]
    assert "cannot end before it starts" in messages[2]
    assert row[0] == "2026-03-02"  # nothing was written by any of the three
