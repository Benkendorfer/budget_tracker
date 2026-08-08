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
    assert "2025-07-02..07-04" in out
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
