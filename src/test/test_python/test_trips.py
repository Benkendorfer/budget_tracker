"""Tests for the travel-bucket map (trips.py) and queries.get_trips."""

from datetime import date
from decimal import Decimal

import pytest

from budget_tracker import categories, queries, rates, tags, trips
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.models import Account, Category, Currency, Transaction


def _session_factory(tmp_path, name="t.db"):
    engine = get_engine(tmp_path / name)
    init_db(engine)
    return get_sessionmaker(engine)


def _seed(session, account_names=("Checking",)):
    currency = Currency(value="USD", symbol="$", decimal_places=2)
    session.add(currency)
    session.flush()
    accounts = {}
    for name in account_names:
        account = Account(name=name, currency_id=currency.id)
        session.add(account)
        accounts[name] = account
    session.flush()
    return currency, accounts


def _txn(
    session, currency, account, day, amount, description="X",
    category_id=None, transfer_group_id=None,
):
    txn = Transaction(
        account_id=account.id,
        currency_id=currency.id,
        category_id=category_id,
        posted_date=day,
        description=description,
        raw_description=description,
        value_minor=amount,
        transfer_group_id=transfer_group_id,
        import_hash=f"{account.id}-{day}-{amount}-{description}-{transfer_group_id}",
    )
    session.add(txn)
    session.flush()
    return txn


# ------------------------------------------------------------------------- seeding


def test_seed_default_buckets_matches_names_case_insensitively(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        food = Category(value="food")  # lowercase -- must still match "Food"
        session.add(food)
        session.flush()
        dining = Category(value="Dining", parent_id=food.id)
        transport = Category(value="Transport")
        session.add_all([dining, transport])
        session.flush()
        car_rental = Category(value="Car Rental", parent_id=transport.id)
        taxi = Category(value="Taxi", parent_id=transport.id)  # deliberately unmapped
        airfare = Category(value="Airfare")
        session.add_all([car_rental, taxi, airfare])
        session.flush()
        food_id, dining_id = food.id, dining.id
        car_rental_id, taxi_id, airfare_id = car_rental.id, taxi.id, airfare.id

        written = trips.seed_default_buckets(session)
        session.commit()

        assert written == 3  # food, Car Rental, Airfare -- Dining/Taxi/Transport not seeded
        mapping = trips.bucket_map(session)
        assert mapping == {
            food_id: trips.FOOD,
            car_rental_id: trips.CAR,
            airfare_id: trips.AIRFARE,
        }
        # Unmapped Taxi (and its unmapped parent Transport) fall through to misc.
        resolved = trips.resolve_buckets(session)
        assert resolved[taxi_id] == trips.MISC
        assert resolved[dining_id] == trips.FOOD  # inherited from its parent, food


def test_seeding_shopping_covers_its_subtree_and_sporting_goods(tmp_path):
    """Mapping the Shopping parent catches Clothing, Books and the rest from one row.

    Sporting Goods is seeded separately because it lives under Fitness rather than
    Shopping. That costs nothing elsewhere: a bucket only ever applies to a transaction
    that is on a trip, so a gym purchase made at home is never reached by it.
    """
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        shopping = Category(value="Shopping")
        fitness = Category(value="Fitness")
        session.add_all([shopping, fitness])
        session.flush()
        clothing = Category(value="Clothing", parent_id=shopping.id)
        books = Category(value="Books", parent_id=shopping.id)
        sporting = Category(value="Sporting Goods", parent_id=fitness.id)
        gym = Category(value="Gym Membership", parent_id=fitness.id)
        session.add_all([clothing, books, sporting, gym])
        session.flush()
        clothing_id, books_id = clothing.id, books.id
        sporting_id, gym_id = sporting.id, gym.id

        trips.seed_default_buckets(session)
        session.commit()

        resolved = trips.resolve_buckets(session)
        assert resolved[clothing_id] == trips.SHOPPING  # inherited from Shopping
        assert resolved[books_id] == trips.SHOPPING
        assert resolved[sporting_id] == trips.SHOPPING  # named in its own right
        # Its sibling under Fitness is untouched -- seeding one child of a parent does
        # not drag the parent, or the rest of the subtree, along with it.
        assert resolved[gym_id] == trips.MISC


def test_shopping_is_a_real_bucket_the_user_can_assign_to(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        session.add(Category(value="Electronics"))
        session.commit()

    with session_factory() as session:
        assert trips.set_bucket(session, ["Electronics"], trips.SHOPPING) == 1
        session.commit()
    with session_factory() as session:
        assert "Electronics" in trips.list_buckets(session)[trips.SHOPPING]


def test_seed_default_buckets_is_idempotent(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        session.add(Category(value="Airfare"))
        session.commit()

    with session_factory() as session:
        first = trips.seed_default_buckets(session)
        session.commit()
    with session_factory() as session:
        second = trips.seed_default_buckets(session)
        session.commit()
        assert (first, second) == (1, 0)


def test_seed_default_buckets_never_reseeds_once_the_table_has_any_row(tmp_path):
    """A single prior edit -- even to one category -- must stop the whole seed, not
    just protect the row the user touched."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        airfare = Category(value="Airfare")
        session.add(airfare)
        session.flush()
        airfare_id = airfare.id
        # A manual, deliberately "wrong" mapping: the user's choice, not the seed's.
        trips.set_bucket(session, ["Airfare"], trips.MISC)
        session.commit()

    with session_factory() as session:
        session.add(Category(value="Food"))  # would be seeded if the table were still empty
        session.commit()

    with session_factory() as session:
        written = trips.seed_default_buckets(session)
        session.commit()
        assert written == 0
        mapping = trips.bucket_map(session)
        assert mapping == {airfare_id: trips.MISC}  # untouched; Food never seeded


def test_seed_default_buckets_only_maps_categories_that_exist(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        written = trips.seed_default_buckets(session)
        session.commit()
        assert written == 0
        assert trips.bucket_map(session) == {}


# --------------------------------------------------------------------- resolve_buckets


def test_resolve_buckets_inherits_down_the_tree(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        food = Category(value="Food")
        session.add(food)
        session.flush()
        dining = Category(value="Dining", parent_id=food.id)
        groceries = Category(value="Groceries", parent_id=food.id)
        session.add_all([dining, groceries])
        session.flush()
        fine_dining = Category(value="Fine Dining", parent_id=dining.id)
        session.add(fine_dining)
        session.flush()
        dining_id, groceries_id, fine_dining_id = dining.id, groceries.id, fine_dining.id

        trips.set_bucket(session, ["Food"], trips.FOOD)
        session.commit()

    with session_factory() as session:
        resolved = trips.resolve_buckets(session)
        assert resolved[dining_id] == trips.FOOD
        assert resolved[groceries_id] == trips.FOOD
        # Two levels down, with nothing mapped in between: still inherited.
        assert resolved[fine_dining_id] == trips.FOOD


def test_resolve_buckets_a_mapping_on_the_child_wins_over_the_parent(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        food = Category(value="Food")
        session.add(food)
        session.flush()
        dining = Category(value="Dining", parent_id=food.id)
        session.add(dining)
        session.flush()
        food_id, dining_id = food.id, dining.id
        trips.set_bucket(session, ["Food"], trips.FOOD)
        trips.set_bucket(session, ["Dining"], trips.TOURISM)  # e.g. a trip's food tour
        session.commit()

    with session_factory() as session:
        resolved = trips.resolve_buckets(session)
        assert resolved[food_id] == trips.FOOD
        assert resolved[dining_id] == trips.TOURISM


def test_resolve_buckets_defaults_to_misc_when_nothing_is_mapped(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        session.add(Category(value="Whatever"))
        session.commit()

    with session_factory() as session:
        resolved = trips.resolve_buckets(session)
        assert list(resolved.values()) == [trips.MISC]


# -------------------------------------------------------------------- set/clear_bucket


def test_set_bucket_is_additive_and_does_not_disturb_siblings_already_in_the_bucket(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        car_rental = Category(value="Car Rental")
        taxi = Category(value="Taxi")
        session.add_all([car_rental, taxi])
        session.flush()
        car_rental_id, taxi_id = car_rental.id, taxi.id
        session.commit()

    with session_factory() as session:
        assert trips.set_bucket(session, ["Car Rental"], trips.CAR) == 1
        session.commit()
    with session_factory() as session:
        assert trips.set_bucket(session, ["Taxi"], trips.CAR) == 1
        session.commit()

    with session_factory() as session:
        mapping = trips.bucket_map(session)
        assert mapping == {car_rental_id: trips.CAR, taxi_id: trips.CAR}


def test_set_bucket_accepts_a_full_path(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        food = Category(value="Food")
        session.add(food)
        session.flush()
        dining = Category(value="Dining", parent_id=food.id)
        session.add(dining)
        session.flush()
        dining_id = dining.id
        session.commit()

    with session_factory() as session:
        assert trips.set_bucket(session, ["Food > Dining"], trips.FOOD) == 1
        session.commit()
        assert trips.bucket_map(session) == {dining_id: trips.FOOD}


def test_set_bucket_rejects_an_unknown_bucket_name(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        session.add(Category(value="Airfare"))
        session.commit()

    with session_factory() as session:
        with pytest.raises(ValueError, match="Unknown bucket"):
            trips.set_bucket(session, ["Airfare"], "spaceship")


def test_set_bucket_writes_nothing_when_any_name_fails_to_resolve(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        session.add(Category(value="Airfare"))
        session.commit()

    with session_factory() as session:
        with pytest.raises(ValueError, match="Does Not Exist"):
            trips.set_bucket(session, ["Airfare", "Does Not Exist"], trips.AIRFARE)
        # Nothing written -- not even the name that did resolve.
        assert trips.bucket_map(session) == {}


def test_clear_bucket_unmaps_so_an_ancestor_mapping_takes_back_over(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        food = Category(value="Food")
        session.add(food)
        session.flush()
        dining = Category(value="Dining", parent_id=food.id)
        session.add(dining)
        session.flush()
        dining_id = dining.id
        trips.set_bucket(session, ["Food"], trips.FOOD)
        trips.set_bucket(session, ["Dining"], trips.TOURISM)
        session.commit()

    with session_factory() as session:
        assert trips.clear_bucket(session, ["Dining"]) == 1
        session.commit()

    with session_factory() as session:
        assert dining_id not in trips.bucket_map(session)
        assert trips.resolve_buckets(session)[dining_id] == trips.FOOD  # inherited again


def test_clear_bucket_on_an_unmapped_category_is_a_noop(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        session.add(Category(value="Airfare"))
        session.commit()

    with session_factory() as session:
        assert trips.clear_bucket(session, ["Airfare"]) == 0


def test_clear_bucket_writes_nothing_when_any_name_fails_to_resolve(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        airfare = Category(value="Airfare")
        session.add(airfare)
        session.flush()
        airfare_id = airfare.id
        trips.set_bucket(session, ["Airfare"], trips.AIRFARE)
        session.commit()

    with session_factory() as session:
        with pytest.raises(ValueError, match="Does Not Exist"):
            trips.clear_bucket(session, ["Airfare", "Does Not Exist"])
        # The valid half of the request must not have been applied either.
        assert trips.bucket_map(session) == {airfare_id: trips.AIRFARE}


# -------------------------------------------------------------------------- cascade


def test_merging_a_mapped_category_deletes_its_bucket_mapping_via_cascade(tmp_path):
    """set_bucket + categories.merge_category: the FK's ondelete=CASCADE must let the
    merge (which deletes the source category) succeed, taking the mapping with it,
    rather than blocking on an FK violation."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        old_name = Category(value="Old Airfare")
        canonical = Category(value="Airfare")
        session.add_all([old_name, canonical])
        session.commit()

    with session_factory() as session:
        trips.set_bucket(session, ["Old Airfare"], trips.AIRFARE)
        session.commit()

    with session_factory() as session:
        assert len(trips.bucket_map(session)) == 1
        categories.merge_category(session, "Old Airfare", "Airfare")  # deletes Old Airfare
        session.commit()

    with session_factory() as session:
        # The merged-away category's mapping went with it -- not left dangling, and
        # not blocking the merge with an IntegrityError.
        assert trips.bucket_map(session) == {}


# ---------------------------------------------------------------------- list_buckets


def test_list_buckets_groups_by_bucket_with_sorted_paths(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        food = Category(value="Food")
        session.add(food)
        session.flush()
        dining = Category(value="Dining", parent_id=food.id)
        session.add(dining)
        session.add(Category(value="Airfare"))
        session.commit()

    with session_factory() as session:
        trips.set_bucket(session, ["Food > Dining", "Food"], trips.FOOD)
        trips.set_bucket(session, ["Airfare"], trips.AIRFARE)
        session.commit()

    with session_factory() as session:
        listed = trips.list_buckets(session)
        assert set(listed) == set(trips.BUCKETS)
        assert listed[trips.FOOD] == ["Food", "Food > Dining"]
        assert listed[trips.AIRFARE] == ["Airfare"]
        assert listed[trips.MISC] == []


# ============================================================== queries.get_trips


def test_get_trips_returns_a_dateless_trip_with_no_transactions(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        tags.get_or_create(session, "Empty Trip", tags.TRIP)
        session.commit()

    with session_factory() as session:
        rows = queries.get_trips(session)

    assert len(rows) == 1
    row = rows[0]
    assert row.name == "Empty Trip"
    assert (row.start, row.end, row.count, row.total_minor) == (None, None, 0, 0)
    assert row.buckets == (0,) * len(trips.BUCKETS)


def test_get_trips_single_currency_cost_counts_and_buckets(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        checking = accounts["Checking"]
        food = Category(value="Food")
        hotel = Category(value="Hotel")
        session.add_all([food, hotel])
        session.flush()
        trips.set_bucket(session, ["Food"], trips.FOOD)
        trips.set_bucket(session, ["Hotel"], trips.HOTEL)

        dinner = _txn(session, currency, checking, date(2026, 3, 2), -4000, "Dinner",
                      category_id=food.id)
        stay = _txn(session, currency, checking, date(2026, 3, 5), -50000, "Hotel",
                    category_id=hotel.id)
        misc_txn = _txn(session, currency, checking, date(2026, 3, 3), -1500, "Souvenir")
        transfer_leg = _txn(session, currency, checking, date(2026, 3, 4), -20000,
                             "To travel card", transfer_group_id=1)
        ids = [dinner.id, stay.id, misc_txn.id, transfer_leg.id]
        session.commit()

    with session_factory() as session:
        tags.set_trip(session, ids, "Spring Trip")
        session.commit()

    with session_factory() as session:
        rows = queries.get_trips(session)

    assert len(rows) == 1
    row = rows[0]
    assert (row.start, row.end) == (date(2026, 3, 2), date(2026, 3, 5))
    assert row.count == 4  # the transfer leg is counted...
    assert row.total_minor == 4000 + 50000 + 1500  # ...but excluded from the cost
    by_bucket = dict(zip(trips.BUCKETS, row.buckets))
    assert by_bucket[trips.FOOD] == 4000
    assert by_bucket[trips.HOTEL] == 50000
    assert by_bucket[trips.MISC] == 1500  # uncategorized transaction
    assert by_bucket[trips.AIRFARE] == 0
    assert sum(row.buckets) == row.total_minor


def test_get_trips_refund_reduces_cost_and_can_make_a_bucket_negative(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        checking = accounts["Checking"]
        hotel = Category(value="Hotel")
        session.add(hotel)
        session.flush()
        trips.set_bucket(session, ["Hotel"], trips.HOTEL)

        booking = _txn(session, currency, checking, date(2026, 4, 1), -10000, "Booking",
                        category_id=hotel.id)
        refund = _txn(session, currency, checking, date(2026, 4, 3), 12000, "Refund",
                       category_id=hotel.id)
        ids = [booking.id, refund.id]
        session.commit()

    with session_factory() as session:
        tags.set_trip(session, ids, "Canceled Trip")
        session.commit()

    with session_factory() as session:
        rows = queries.get_trips(session)

    row = rows[0]
    assert row.total_minor == -2000  # refund exceeded spending -- a net gain
    by_bucket = dict(zip(trips.BUCKETS, row.buckets))
    # Unfolded figure stays honest (negative), not clamped -- that is the bar's job.
    assert by_bucket[trips.HOTEL] == -2000


def test_get_trips_converts_a_second_currency_at_the_days_own_rate(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session, account_names=("Checking",))
        chf = Currency(value="CHF", symbol="Fr", decimal_places=2)
        session.add(chf)
        session.flush()
        card = Account(name="Card CHF", currency_id=chf.id)
        session.add(card)
        session.flush()
        day = date(2026, 6, 1)
        rates.record_rate(session, day, "CHF", "USD", Decimal("1.10"), rates.ECB)

        usd_txn = _txn(session, currency, accounts["Checking"], day, -2000, "Snack")
        chf_txn = _txn(session, chf, card, day, -10000, "Fondue")
        ids = [usd_txn.id, chf_txn.id]
        session.commit()

    with session_factory() as session:
        tags.set_trip(session, ids, "Swiss Trip")
        session.commit()

    with session_factory() as session:
        rows = queries.get_trips(session)

    row = rows[0]
    # -20.00 USD + (-100.00 CHF * 1.10) = -20.00 + -110.00 = -130.00 USD of spending.
    assert row.total_minor == 13000
    assert dict(zip(trips.BUCKETS, row.buckets))[trips.MISC] == 13000


def test_get_trips_sorts_most_recent_first_dateless_last(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        checking = accounts["Checking"]
        jan = _txn(session, currency, checking, date(2026, 1, 5), -1000, "Jan")
        jun = _txn(session, currency, checking, date(2026, 6, 5), -1000, "Jun")
        jan_id, jun_id = jan.id, jun.id
        session.commit()

    with session_factory() as session:
        tags.set_trip(session, [jan_id], "January Trip")
        tags.set_trip(session, [jun_id], "June Trip")
        tags.get_or_create(session, "Someday Trip", tags.TRIP)  # no transactions
        session.commit()

    with session_factory() as session:
        rows = queries.get_trips(session)

    assert [r.name for r in rows] == ["June Trip", "January Trip", "Someday Trip"]


def test_get_trips_sorts_by_when_a_trip_ended_not_when_it_began(tmp_path):
    """The two disagree once trips overlap: a long trip that began in April and ran
    into late June belongs above a short one that began later in June and was over
    first. This panel is read as "most recently back"."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        checking = accounts["Checking"]
        # Long trip: starts earliest, ends latest.
        long_start = _txn(session, currency, checking, date(2026, 4, 10), -1000, "LongA")
        long_end = _txn(session, currency, checking, date(2026, 6, 23), -1000, "LongB")
        # Short trip: starts later than the long one, but finishes before it.
        short_start = _txn(session, currency, checking, date(2026, 6, 19), -1000, "ShortA")
        short_end = _txn(session, currency, checking, date(2026, 6, 22), -1000, "ShortB")
        ids = (long_start.id, long_end.id, short_start.id, short_end.id)
        session.commit()

    with session_factory() as session:
        tags.set_trip(session, [ids[0], ids[1]], "Long Trip")
        tags.set_trip(session, [ids[2], ids[3]], "Short Trip")
        session.commit()

    with session_factory() as session:
        rows = {r.name: r for r in queries.get_trips(session)}
        order = [r.name for r in queries.get_trips(session)]

    # Sanity: the two orderings really do disagree on this data.
    assert rows["Long Trip"].start < rows["Short Trip"].start
    assert rows["Long Trip"].end > rows["Short Trip"].end
    assert order == ["Long Trip", "Short Trip"]


# --------------------------------------------------------- Filters.category_ids
#
# A travel bucket is several unrelated categories at once -- Airfare and Rail Travel
# share a bucket but not a parent -- so drilling into one cannot be a subtree filter.


def test_category_ids_filters_to_an_explicit_set(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        checking = accounts["Checking"]
        air = categories.ensure_path(session, "Airfare")
        rail = categories.ensure_path(session, "Rail Travel")
        food = categories.ensure_path(session, "Dining")
        session.flush()
        _txn(session, currency, checking, date(2026, 1, 1), -100, "Flight", air.id)
        _txn(session, currency, checking, date(2026, 1, 2), -200, "Train", rail.id)
        _txn(session, currency, checking, date(2026, 1, 3), -300, "Lunch", food.id)
        session.commit()
        wanted = (air.id, rail.id)

    with session_factory() as session:
        rows = queries.get_transactions(
            session, filters=queries.Filters(category_ids=wanted)
        )
        assert sorted(r.description for r in rows) == ["Flight", "Train"]


def test_a_none_in_category_ids_reaches_uncategorized_rows(tmp_path):
    """How the misc bucket picks up transactions with no category at all, rather than
    silently dropping them out of the trip they are on."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        checking = accounts["Checking"]
        shop = categories.ensure_path(session, "Merchandise")
        session.flush()
        _txn(session, currency, checking, date(2026, 1, 1), -100, "Souvenir", shop.id)
        _txn(session, currency, checking, date(2026, 1, 2), -200, "Mystery", None)
        session.commit()
        shop_id = shop.id

    with session_factory() as session:
        rows = queries.get_transactions(
            session, filters=queries.Filters(category_ids=(shop_id, None))
        )
        assert sorted(r.description for r in rows) == ["Mystery", "Souvenir"]

        only_none = queries.get_transactions(
            session, filters=queries.Filters(category_ids=(None,))
        )
        assert [r.description for r in only_none] == ["Mystery"]


def test_an_empty_category_ids_matches_nothing_rather_than_everything(tmp_path):
    """The honest reading of "these categories" when there are none -- degrading to
    "all" would silently show a bucket's drill-down as the whole trip."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        _txn(session, currency, accounts["Checking"], date(2026, 1, 1), -100, "A")
        session.commit()

    with session_factory() as session:
        rows = queries.get_transactions(
            session, filters=queries.Filters(category_ids=())
        )
        assert rows == []
        totals = queries.get_totals(session, filters=queries.Filters(category_ids=()))
        assert totals.count == 0


def test_category_ids_combines_with_a_trip_filter(tmp_path):
    """The actual drill-down: one bucket, within one trip."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        checking = accounts["Checking"]
        air = categories.ensure_path(session, "Airfare")
        session.flush()
        on_trip = _txn(session, currency, checking, date(2026, 1, 1), -100, "Flight", air.id)
        off_trip = _txn(session, currency, checking, date(2026, 1, 2), -900, "Other flight", air.id)
        session.commit()
        air_id, on_trip_id = air.id, on_trip.id
        assert off_trip.id != on_trip_id

    with session_factory() as session:
        tags.set_trip(session, [on_trip_id], "Japan 2026")
        session.commit()

    with session_factory() as session:
        trip_id = queries.resolve_tag(session, "Japan 2026", tags.TRIP)
        rows = queries.get_transactions(
            session,
            filters=queries.Filters(trip_id=trip_id, category_ids=(air_id,)),
        )
        assert [r.description for r in rows] == ["Flight"]


# ------------------------------------------------------------- manual trip dates


def _one_txn_trip(session_factory, name="Japan 2026", day=date(2026, 3, 10)):
    with session_factory() as session:
        currency, accounts = _seed(session)
        txn = _txn(session, currency, accounts["Checking"], day, -1000, "Flight")
        session.commit()
        txn_id = txn.id
    with session_factory() as session:
        tags.set_trip(session, [txn_id], name)
        session.commit()


def test_a_trip_derives_its_dates_from_its_transactions_by_default(tmp_path):
    session_factory = _session_factory(tmp_path)
    _one_txn_trip(session_factory)
    with session_factory() as session:
        row = queries.get_trips(session)[0]
        assert (row.start, row.end) == (date(2026, 3, 10), date(2026, 3, 10))
        assert (row.start_is_manual, row.end_is_manual) == (False, False)


def test_a_manual_start_overrides_the_derived_one(tmp_path):
    """The case this exists for: a flight booked months ahead drags the derived start
    back to the booking, while the end was right all along."""
    session_factory = _session_factory(tmp_path)
    _one_txn_trip(session_factory)
    with session_factory() as session:
        assert tags.set_trip_dates(session, "Japan 2026", date(2026, 3, 8), None)
        session.commit()

    with session_factory() as session:
        row = queries.get_trips(session)[0]
        assert row.start == date(2026, 3, 8)  # overridden
        assert row.end == date(2026, 3, 10)  # still derived
        # Marked per end: only the start was set, so only the start is manual.
        assert (row.start_is_manual, row.end_is_manual) == (True, False)


def test_clearing_an_override_goes_back_to_the_derived_date(tmp_path):
    session_factory = _session_factory(tmp_path)
    _one_txn_trip(session_factory)
    with session_factory() as session:
        tags.set_trip_dates(session, "Japan 2026", date(2020, 1, 1), date(2020, 1, 2))
        session.commit()
    with session_factory() as session:
        tags.set_trip_dates(session, "Japan 2026", None, None)
        session.commit()

    with session_factory() as session:
        row = queries.get_trips(session)[0]
        assert (row.start, row.end) == (date(2026, 3, 10), date(2026, 3, 10))
        assert (row.start_is_manual, row.end_is_manual) == (False, False)


def test_manual_dates_give_a_trip_with_no_transactions_a_place_in_the_order(tmp_path):
    """A trip planned but not yet paid for has no derived dates at all; setting them by
    hand is the only way it can sit anywhere but last."""
    session_factory = _session_factory(tmp_path)
    _one_txn_trip(session_factory, "Past Trip", date(2026, 1, 5))
    with session_factory() as session:
        tags.get_or_create(session, "Future Trip", tags.TRIP)
        session.commit()
    with session_factory() as session:
        tags.set_trip_dates(session, "Future Trip", date(2026, 9, 1), date(2026, 9, 20))
        session.commit()

    with session_factory() as session:
        rows = queries.get_trips(session)
        assert [r.name for r in rows] == ["Future Trip", "Past Trip"]
        assert rows[0].count == 0
        assert rows[0].total_minor == 0


def test_a_trip_cannot_end_before_it_starts(tmp_path):
    session_factory = _session_factory(tmp_path)
    _one_txn_trip(session_factory)
    with session_factory() as session:
        with pytest.raises(ValueError, match="cannot end before it starts"):
            tags.set_trip_dates(session, "Japan 2026", date(2026, 5, 1), date(2026, 4, 1))


def test_setting_dates_on_a_trip_that_does_not_exist_reports_rather_than_raises(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        assert tags.set_trip_dates(session, "Nowhere", date(2026, 1, 1), None) is False


def test_manual_dates_survive_on_a_database_that_predates_the_columns(tmp_path):
    """The columns are added by db._ADDED_COLUMNS, not create_all, since `tag` already
    exists in any database that has ever been opened."""
    import sqlalchemy

    db_path = tmp_path / "old.db"
    engine = get_engine(db_path)
    init_db(engine)
    # Drop back to the pre-override shape, then reopen. A database that old also predates
    # Alembic, so it has no alembic_version table -- without dropping it, init_db would
    # rightly treat this one as already current and leave the hand-made old table alone.
    with engine.begin() as connection:
        connection.execute(sqlalchemy.text("DROP TABLE alembic_version"))
        # Nor anything a later migration added -- the upgrade would make it again.
        connection.execute(sqlalchemy.text("DROP TABLE budget_amount"))
        connection.execute(sqlalchemy.text("DROP TABLE transaction_tag"))
        connection.execute(sqlalchemy.text("DROP TABLE tag"))
        connection.execute(
            sqlalchemy.text(
                "CREATE TABLE tag (id INTEGER PRIMARY KEY, name VARCHAR, kind VARCHAR, "
                "created_at DATETIME)"
            )
        )
        connection.execute(
            sqlalchemy.text("INSERT INTO tag (name, kind) VALUES ('Japan 2026', 'trip')")
        )

    reopened = get_engine(db_path)
    init_db(reopened)
    session_factory = get_sessionmaker(reopened)
    with session_factory() as session:
        assert tags.set_trip_dates(session, "Japan 2026", date(2026, 3, 8), date(2026, 3, 20))
        session.commit()
    with session_factory() as session:
        row = queries.get_trips(session)[0]
        assert (row.start, row.end) == (date(2026, 3, 8), date(2026, 3, 20))


def test_set_trip_dates_leaves_an_end_it_is_not_given(tmp_path):
    """KEEP, None and a date are three different instructions, not two: leaving an end
    alone is not the same as forgetting its override."""
    session_factory = _session_factory(tmp_path)
    _one_txn_trip(session_factory)
    with session_factory() as session:
        tags.set_trip_dates(session, "Japan 2026", date(2026, 3, 1), date(2026, 3, 20))
        session.commit()

    with session_factory() as session:
        # Only the start named: the end's override must survive untouched.
        tags.set_trip_dates(session, "Japan 2026", start=date(2026, 3, 5))
        session.commit()

    with session_factory() as session:
        row = queries.get_trips(session)[0]
        assert (row.start, row.end) == (date(2026, 3, 5), date(2026, 3, 20))
        assert (row.start_is_manual, row.end_is_manual) == (True, True)


def test_clearing_one_end_leaves_the_other_overridden(tmp_path):
    session_factory = _session_factory(tmp_path)
    _one_txn_trip(session_factory)
    with session_factory() as session:
        tags.set_trip_dates(session, "Japan 2026", date(2026, 3, 1), date(2026, 3, 20))
        session.commit()

    with session_factory() as session:
        tags.set_trip_dates(session, "Japan 2026", start=None)  # derive the start again
        session.commit()

    with session_factory() as session:
        row = queries.get_trips(session)[0]
        assert row.start == date(2026, 3, 10)  # back to the transaction's own date
        assert row.end == date(2026, 3, 20)  # still overridden
        assert (row.start_is_manual, row.end_is_manual) == (False, True)


def test_setting_one_end_is_checked_against_the_other_as_it_stands(tmp_path):
    """Otherwise setting one end could quietly invert it against the other's override."""
    session_factory = _session_factory(tmp_path)
    _one_txn_trip(session_factory)
    with session_factory() as session:
        tags.set_trip_dates(session, "Japan 2026", None, date(2026, 3, 5))
        session.commit()

    with session_factory() as session:
        with pytest.raises(ValueError, match="cannot end before it starts"):
            tags.set_trip_dates(session, "Japan 2026", start=date(2026, 4, 1))


# ------------------------------------------------------------------- days / cost per day


def test_days_counts_both_ends(tmp_path):
    """Inclusive, so a trip that left and returned the same day is one day, not zero --
    it is the denominator of a cost per day."""
    session_factory = _session_factory(tmp_path)
    _one_txn_trip(session_factory)
    with session_factory() as session:
        assert queries.get_trips(session)[0].days == 1
        tags.set_trip_dates(session, "Japan 2026", date(2026, 3, 1), date(2026, 3, 10))
        session.commit()
    with session_factory() as session:
        assert queries.get_trips(session)[0].days == 10


def test_days_is_none_without_dates(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        tags.get_or_create(session, "Someday", tags.TRIP)
        session.commit()
    with session_factory() as session:
        assert queries.get_trips(session)[0].days is None


def test_correcting_a_start_changes_the_daily_cost(tmp_path):
    """The point of being able to fix the dates: a booking months early does not just
    move the start, it stretches the denominator and halves the apparent daily cost."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        checking = accounts["Checking"]
        booking = _txn(session, currency, checking, date(2026, 1, 5), -60_000, "Flight")
        onsite = _txn(session, currency, checking, date(2026, 3, 10), -40_000, "Hotel")
        session.commit()
        ids = [booking.id, onsite.id]

    with session_factory() as session:
        tags.set_trip(session, ids, "Japan 2026")
        session.commit()

    with session_factory() as session:
        derived = queries.get_trips(session)[0]
        assert derived.days == 65  # 2026-01-05 .. 2026-03-10, dragged back by the booking
        tags.set_trip_dates(session, "Japan 2026", date(2026, 3, 8), date(2026, 3, 12))
        session.commit()

    with session_factory() as session:
        corrected = queries.get_trips(session)[0]
        assert corrected.days == 5
        assert corrected.total_minor == derived.total_minor  # the cost never moved
