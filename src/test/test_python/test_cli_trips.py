"""Tests for the CLI twin of the trips feature (``budget trips``).

Follows ``test_cli_tags.py``: ``cli.main`` is called with an argv list, exactly as the
``budget`` entry point does, against a temporary database seeded by ``conftest._setup``.
The CLI has no way to select individual transactions, so trips are assembled through
the core :mod:`budget_tracker.tags` module directly, then exercised through the CLI on
top of that.
"""

from sqlalchemy import select

from budget_tracker import categories, cli, tags, trips
from budget_tracker.importer import import_csv
from budget_tracker.models import Transaction
from conftest import _setup
from helpers import learn_format


def _txn_ids(session_factory, description=None):
    with session_factory() as session:
        query = select(Transaction.id)
        if description is not None:
            query = query.where(Transaction.description == description)
        return list(session.scalars(query))


def _trip_fixture(session_factory, ids, name):
    with session_factory() as session:
        tags.set_trip(session, ids, name)
        session.commit()


# ------------------------------------------------------------------------------- list


def test_trips_list_shows_dates_name_count_total_and_breakdown(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    ids = _txn_ids(session_factory, "COFFEE SHOP A")  # 2025-07-02 -3.00, 2025-07-04 -3.50
    _trip_fixture(session_factory, ids, "Coffee Trip")
    with session_factory() as session:
        assert trips.set_bucket(session, ["Dining"], trips.FOOD) == 1
        session.commit()

    assert cli.main(["trips"]) == 0  # bare command defaults to "list"
    out = capsys.readouterr().out
    assert "Coffee Trip" in out
    # Start and end are separate columns, not one combined field -- they are two facts
    # and are independently overridable (see the `dates` tests below).
    assert "2025-07-02" in out
    assert "2025-07-04" in out
    assert "2 txns" in out
    assert "6.50" in out
    assert "food 100%" in out


def test_trips_list_shows_a_trip_with_no_transactions(tmp_path, monkeypatch, capsys):
    """An empty trip still appears -- there is no other way to make it selectable."""
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        tags.get_or_create(session, "Japan 2026", tags.TRIP)
        session.commit()

    assert cli.main(["trips", "list"]) == 0
    out = capsys.readouterr().out
    assert "Japan 2026" in out
    assert "0 txns" in out


def test_trips_list_with_no_trips(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["trips"]) == 0
    assert "No trips yet." in capsys.readouterr().out


def test_trips_list_skips_zero_buckets_and_clamps_refunds(tmp_path, monkeypatch, capsys):
    """A bucket whose refunds outweigh its spending is clamped to 0% and dropped from
    the breakdown entirely -- never printed negative, never printed as ``0%``."""
    session_factory = _setup(tmp_path, monkeypatch)
    refund_csv = tmp_path / "refund.csv"
    refund_csv.write_text(
        "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\n"
        "2025-08-01,2025-08-02,8207,DINING REFUND,Dining,,50.00\n"
        "2025-08-01,2025-08-02,8207,SOUVENIR SHOP,Gifts,20.00,\n",
        encoding="utf-8",
    )
    with session_factory() as session:
        learn_format(session, refund_csv, name="refund_layout")
        import_csv(session, refund_csv)
        trips.set_bucket(session, ["Dining"], trips.FOOD)
        session.commit()
    ids = _txn_ids(session_factory, "DINING REFUND") + _txn_ids(session_factory, "SOUVENIR SHOP")
    _trip_fixture(session_factory, ids, "Refund Trip")

    assert cli.main(["trips"]) == 0
    out = capsys.readouterr().out
    assert "misc 100%" in out
    assert "food" not in out


# ---------------------------------------------------------------------------- buckets


def test_trips_buckets_seeds_and_groups_by_bucket(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        categories.ensure_path(session, "Airfare")
        session.commit()

    assert cli.main(["trips", "buckets"]) == 0
    out = capsys.readouterr().out
    assert "airfare:" in out
    assert "Airfare" in out
    assert "rail:" in out
    assert "(none)" in out  # every other seeded bucket has nothing to seed from


def test_trips_buckets_does_not_reseed_over_a_user_edit(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        categories.ensure_path(session, "Airfare")
        session.commit()
        assert trips.set_bucket(session, ["Airfare"], trips.CAR) == 1
        session.commit()

    assert cli.main(["trips", "buckets"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    car_index = lines.index("car:")
    airfare_index = lines.index("airfare:")
    assert "Airfare" in lines[car_index + 1]
    assert lines[airfare_index + 1] == "  (none)"


# ----------------------------------------------------------------------------- bucket


def test_trips_bucket_sets_one_category(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)

    assert cli.main(["trips", "bucket", "Dining", "food"]) == 0
    out = capsys.readouterr().out
    assert "Set 1 category to 'food'." in out

    with session_factory() as session:
        assert trips.bucket_map(session)[categories.resolve_path(session, "Dining").id] == "food"


def test_trips_bucket_is_additive_across_categories(tmp_path, monkeypatch, capsys):
    """Setting one category's bucket must not disturb another already in it."""
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        categories.ensure_path(session, "Taxi")
        categories.ensure_path(session, "Car Rental")
        session.commit()

    assert cli.main(["trips", "bucket", "Car Rental", "car"]) == 0
    assert cli.main(["trips", "bucket", "Taxi", "car"]) == 0
    capsys.readouterr()

    with session_factory() as session:
        mapping = trips.bucket_map(session)
        assert mapping[categories.resolve_path(session, "Car Rental").id] == "car"
        assert mapping[categories.resolve_path(session, "Taxi").id] == "car"


def test_trips_bucket_accepts_several_categories_before_the_bucket(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        categories.ensure_path(session, "Taxi")
        categories.ensure_path(session, "Car Rental")
        session.commit()

    assert cli.main(["trips", "bucket", "Car Rental", "Taxi", "car"]) == 0
    out = capsys.readouterr().out
    assert "Set 2 categories to 'car'." in out

    with session_factory() as session:
        mapping = trips.bucket_map(session)
        assert mapping[categories.resolve_path(session, "Car Rental").id] == "car"
        assert mapping[categories.resolve_path(session, "Taxi").id] == "car"


def test_trips_bucket_clear_unmaps_it(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        categories.ensure_path(session, "Taxi")
        session.commit()
        trips.set_bucket(session, ["Taxi"], trips.CAR)
        session.commit()

    assert cli.main(["trips", "bucket", "Taxi", "--clear"]) == 0
    out = capsys.readouterr().out
    assert "Unmapped 1 category." in out

    with session_factory() as session:
        assert categories.resolve_path(session, "Taxi").id not in trips.bucket_map(session)


def test_trips_bucket_reports_an_unknown_bucket_name(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["trips", "bucket", "Dining", "spaceship"]) == 1
    out = capsys.readouterr().out
    assert "Unknown bucket 'spaceship'" in out
    for bucket in trips.BUCKETS:
        assert bucket in out


def test_trips_bucket_reports_an_unresolved_category_and_writes_nothing(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)

    assert cli.main(["trips", "bucket", "Nonexistent Category", "car"]) == 1
    out = capsys.readouterr().out
    assert "Nonexistent Category" in out

    with session_factory() as session:
        assert trips.bucket_map(session) == {}


def test_trips_bucket_without_a_bucket_or_clear_reports_usage(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["trips", "bucket", "Dining"]) == 1
    assert "Usage:" in capsys.readouterr().out


# -------------------------------------------------------------------------- dates


def test_trips_dates_overrides_the_derived_dates(tmp_path, monkeypatch, capsys):
    """The case this exists for: a flight booked months ahead drags the derived start
    back to the booking, and there is no transaction to correct."""
    session_factory = _setup(tmp_path, monkeypatch)
    ids = _txn_ids(session_factory, "COFFEE SHOP A")
    _trip_fixture(session_factory, ids, "Coffee Trip")

    assert cli.main(["trips", "dates", "Coffee Trip", "--start", "2025-06-30", "--end", "2025-07-10"]) == 0
    capsys.readouterr()

    assert cli.main(["trips"]) == 0
    out = capsys.readouterr().out
    assert "2025-06-30" in out
    assert "2025-07-10" in out
    # The marker is on the app's guess, not the correction, so a hand-set date is plain.
    assert "2025-07-10*" not in out


def test_trips_list_has_separate_start_and_end_columns(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    _trip_fixture(session_factory, _txn_ids(session_factory, "COFFEE SHOP A"), "Coffee Trip")

    assert cli.main(["trips"]) == 0
    out = capsys.readouterr().out
    header = out.splitlines()[0]
    assert "Start" in header and "End" in header
    assert header.index("Start") < header.index("End")


def test_trips_dates_clear_goes_back_to_deriving_them(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    _trip_fixture(session_factory, _txn_ids(session_factory, "COFFEE SHOP A"), "Coffee Trip")
    assert cli.main(["trips", "dates", "Coffee Trip", "--start", "2020-01-01", "--end", "2020-01-02"]) == 0
    capsys.readouterr()

    assert cli.main(["trips", "dates", "Coffee Trip", "--clear"]) == 0
    capsys.readouterr()
    assert cli.main(["trips"]) == 0
    out = capsys.readouterr().out
    assert "2020-01-01" not in out
    assert "2025-07-02*" in out  # the derived start is back, and flagged as derived


def test_trips_dates_rejects_an_end_before_the_start(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    _trip_fixture(session_factory, _txn_ids(session_factory, "COFFEE SHOP A"), "Coffee Trip")

    assert cli.main(["trips", "dates", "Coffee Trip", "--start", "2025-08-01", "--end", "2025-07-01"]) == 1
    assert "cannot end before it starts" in capsys.readouterr().out


def test_trips_dates_rejects_a_malformed_date(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    _trip_fixture(session_factory, _txn_ids(session_factory, "COFFEE SHOP A"), "Coffee Trip")

    assert cli.main(["trips", "dates", "Coffee Trip", "--start", "last tuesday"]) == 1
    assert "YYYY-MM-DD" in capsys.readouterr().out


def test_trips_dates_reports_an_unknown_trip(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    assert cli.main(["trips", "dates", "Nowhere", "--start", "2025-01-01"]) == 1
    assert "No trip named 'Nowhere'." in capsys.readouterr().out


def test_trips_dates_with_neither_end_named_is_a_usage_error(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    _trip_fixture(session_factory, _txn_ids(session_factory, "COFFEE SHOP A"), "Coffee Trip")

    assert cli.main(["trips", "dates", "Coffee Trip"]) == 1
    assert "Usage:" in capsys.readouterr().out


def test_trips_dates_can_set_just_one_end(tmp_path, monkeypatch, capsys):
    """--start and --end are independent; the end not named is left exactly as it is,
    which is different from --clear, which forgets both."""
    session_factory = _setup(tmp_path, monkeypatch)
    _trip_fixture(session_factory, _txn_ids(session_factory, "COFFEE SHOP A"), "Coffee Trip")

    assert cli.main(["trips", "dates", "Coffee Trip", "--start", "2025-06-30"]) == 0
    assert "starts 2025-06-30" in capsys.readouterr().out

    assert cli.main(["trips"]) == 0
    out = capsys.readouterr().out
    assert "2025-06-30" in out  # set, so unmarked
    assert "2025-07-04*" in out  # end untouched, still derived and flagged


def test_trips_dates_warns_when_one_end_lands_past_the_derived_other(
    tmp_path, monkeypatch, capsys
):
    """Refusing would make correcting both ends one at a time impossible, which is the
    reason for setting them separately at all -- so it warns and stores."""
    session_factory = _setup(tmp_path, monkeypatch)
    _trip_fixture(session_factory, _txn_ids(session_factory, "COFFEE SHOP A"), "Coffee Trip")

    # Derived end is 2025-07-04; a start past it is stored, with a warning.
    assert cli.main(["trips", "dates", "Coffee Trip", "--start", "2025-08-01"]) == 0
    out = capsys.readouterr().out
    assert "start is now after its end" in out

    # Finishing the correction clears it.
    assert cli.main(["trips", "dates", "Coffee Trip", "--end", "2025-08-10"]) == 0
    assert "start is now after its end" not in capsys.readouterr().out


def test_trips_list_shows_a_cost_per_day_and_a_total(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    _trip_fixture(session_factory, _txn_ids(session_factory, "COFFEE SHOP A"), "Coffee Trip")

    assert cli.main(["trips"]) == 0
    out = capsys.readouterr().out
    header, trip, total = [line for line in out.splitlines() if line.strip()][:3]
    assert "Cost/day" in header
    # 6.50 over 2025-07-02..07-04, both ends counted -> 3 days.
    assert "2.17" in trip
    assert total.strip().startswith("TOTAL")
    assert "6.50" in total
    assert "2.17" in total


def test_the_cli_total_divides_by_days_traveled_not_the_whole_span(
    tmp_path, monkeypatch, capsys
):
    """Two short trips months apart: the months at home in between are not days this
    money was spent over."""
    session_factory = _setup(tmp_path, monkeypatch)
    _trip_fixture(session_factory, _txn_ids(session_factory, "COFFEE SHOP A"), "Trip One")
    _trip_fixture(session_factory, _txn_ids(session_factory, "COFFEE SHOP B"), "Trip Two")

    assert cli.main(["trips"]) == 0
    total = [l for l in capsys.readouterr().out.splitlines() if "TOTAL" in l][0]
    # Trip One: 6.50 over 3 days. Trip Two: 4.00 over 1 day, so 4 days traveled --
    # not the 3 weeks between them. 1050 / 4 = 262.5 minor units, which Python's
    # round() takes to the even 262; the panel shares the same rounding, so the two
    # surfaces cannot disagree.
    assert "10.50" in total
    assert "2.62" in total


def test_a_trip_with_no_dates_contributes_cost_but_no_days(tmp_path, monkeypatch, capsys):
    """Leaving it out of the total entirely would make the total disagree with the
    column above it."""
    session_factory = _setup(tmp_path, monkeypatch)
    _trip_fixture(session_factory, _txn_ids(session_factory, "COFFEE SHOP A"), "Coffee Trip")
    with session_factory() as session:
        tags.get_or_create(session, "Someday", tags.TRIP)
        session.commit()

    assert cli.main(["trips"]) == 0
    lines = capsys.readouterr().out.splitlines()
    someday = [l for l in lines if "Someday" in l][0]
    total = [l for l in lines if "TOTAL" in l][0]
    assert someday.split()[-1] == "txns" or "0 txns" in someday
    assert "6.50" in total  # unchanged by the dateless trip
    assert "2.17" in total  # and its missing days did not dilute the rate
