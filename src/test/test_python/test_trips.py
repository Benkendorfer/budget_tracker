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
    assert by_bucket[trips.MISC] == 1500  # uncategorised transaction
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
        tags.set_trip(session, ids, "Cancelled Trip")
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
