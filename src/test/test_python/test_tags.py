"""Tests for tags and trips."""

from datetime import date

import pytest
from sqlalchemy import select, text

from budget_tracker import queries, tags
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.importer import delete_import, import_csv
from budget_tracker.models import Account, Base, Currency, Tag, Transaction, TransactionTag
from helpers import learn_format


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


def _txn(session, currency, account, day, amount, description="X"):
    txn = Transaction(
        account_id=account.id,
        currency_id=currency.id,
        posted_date=day,
        description=description,
        raw_description=description,
        value_minor=amount,
        import_hash=f"{account.id}-{day}-{amount}-{description}",
    )
    session.add(txn)
    session.flush()
    return txn


CSV = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2025-07-01,2025-07-02,8207,COFFEE SHOP A,,3.00,
2025-07-01,2025-07-02,8207,COFFEE SHOP B,,4.00,
"""


def _setup_import(tmp_path):
    engine = get_engine(tmp_path / "t.db")
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    csv_path = tmp_path / "in.csv"
    csv_path.write_text(CSV, encoding="utf-8")
    with session_factory() as session:
        learn_format(session, csv_path)
        import_csv(session, csv_path)
    return session_factory


# ------------------------------------------------------------------ get_or_create / resolve


def test_get_or_create_reuses_an_existing_tag(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        first = tags.get_or_create(session, "reimbursable")
        second = tags.get_or_create(session, "reimbursable")
        session.commit()
        assert first.id == second.id


def test_a_tag_and_a_trip_can_share_a_name(tmp_path):
    """Uniqueness is (name, kind), not name -- a trip and a tag are different things."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        tag = tags.get_or_create(session, "japan", tags.TAG)
        trip = tags.get_or_create(session, "japan", tags.TRIP)
        session.commit()
        assert tag.id != trip.id
        assert tag.kind == "tag"
        assert trip.kind == "trip"


def test_resolve_returns_none_for_an_unknown_name(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        assert tags.resolve(session, "nope") is None


def test_list_tags_filters_by_kind(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        tags.get_or_create(session, "reimbursable", tags.TAG)
        tags.get_or_create(session, "Japan 2026", tags.TRIP)
        session.commit()

    with session_factory() as session:
        assert [t.name for t in tags.list_tags(session, tags.TAG)] == ["reimbursable"]
        assert [t.name for t in tags.list_tags(session, tags.TRIP)] == ["Japan 2026"]
        assert len(tags.list_tags(session)) == 2


# ------------------------------------------------------------------------- add / remove


def test_add_tag_is_idempotent(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        txn = _txn(session, currency, accounts["Checking"], date(2025, 1, 1), -100)
        session.commit()
        txn_id = txn.id

    with session_factory() as session:
        assert tags.add_tag(session, [txn_id], "reimbursable") == 1
        assert tags.add_tag(session, [txn_id], "reimbursable") == 0  # already tagged
        session.commit()

    with session_factory() as session:
        assert len(list(session.scalars(select(TransactionTag)))) == 1


def test_add_tag_on_an_empty_selection_is_a_no_op(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        assert tags.add_tag(session, [], "reimbursable") == 0
        assert session.scalar(select(Tag)) is None  # not even the tag was created


def test_add_tag_covers_several_transactions_at_once(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        t1 = _txn(session, currency, accounts["Checking"], date(2025, 1, 1), -100, "A")
        t2 = _txn(session, currency, accounts["Checking"], date(2025, 1, 2), -200, "B")
        session.commit()
        ids = [t1.id, t2.id]

    with session_factory() as session:
        assert tags.add_tag(session, ids, "reimbursable") == 2
        session.commit()

    with session_factory() as session:
        by_txn = tags.tags_for(session, ids)
        assert {t.name for t in by_txn[ids[0]]} == {"reimbursable"}
        assert {t.name for t in by_txn[ids[1]]} == {"reimbursable"}


def test_remove_tag_leaves_untagged_transactions_alone(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        t1 = _txn(session, currency, accounts["Checking"], date(2025, 1, 1), -100, "A")
        t2 = _txn(session, currency, accounts["Checking"], date(2025, 1, 2), -200, "B")
        session.commit()
        ids = [t1.id, t2.id]

    with session_factory() as session:
        tags.add_tag(session, [ids[0]], "reimbursable")
        session.commit()

    with session_factory() as session:
        assert tags.remove_tag(session, ids, "reimbursable") == 1  # only t1 had it
        session.commit()

    with session_factory() as session:
        by_txn = tags.tags_for(session, ids)
        assert by_txn[ids[0]] == []
        assert by_txn[ids[1]] == []


def test_remove_tag_of_unknown_name_is_a_no_op(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        txn = _txn(session, currency, accounts["Checking"], date(2025, 1, 1), -100)
        session.commit()
        assert tags.remove_tag(session, [txn.id], "nope") == 0


def test_remove_tag_on_an_empty_selection_is_a_no_op(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        assert tags.remove_tag(session, [], "reimbursable") == 0


# ------------------------------------------------------------------------------- trips


def test_set_trip_replaces_any_other_trip(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        txn = _txn(session, currency, accounts["Checking"], date(2025, 1, 1), -100)
        session.commit()
        txn_id = txn.id

    with session_factory() as session:
        assert tags.set_trip(session, [txn_id], "Japan 2026") == 1
        session.commit()

    with session_factory() as session:
        assert tags.set_trip(session, [txn_id], "Italy 2027") == 1
        session.commit()

    with session_factory() as session:
        trips = [t.name for t in tags.tags_for(session, [txn_id])[txn_id] if t.kind == tags.TRIP]
        assert trips == ["Italy 2027"]
        # Only one trip link exists, not both -- the old one was deleted, not kept.
        assert len(list(session.scalars(select(TransactionTag)))) == 1


def test_set_trip_does_not_disturb_a_normal_tag_on_the_same_transaction(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        txn = _txn(session, currency, accounts["Checking"], date(2025, 1, 1), -100)
        session.commit()
        txn_id = txn.id

    with session_factory() as session:
        tags.add_tag(session, [txn_id], "reimbursable")
        tags.set_trip(session, [txn_id], "Japan 2026")
        session.commit()

    with session_factory() as session:
        names = {t.name for t in tags.tags_for(session, [txn_id])[txn_id]}
        assert names == {"reimbursable", "Japan 2026"}


def test_clear_trip_removes_only_the_trip_link(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        txn = _txn(session, currency, accounts["Checking"], date(2025, 1, 1), -100)
        session.commit()
        txn_id = txn.id

    with session_factory() as session:
        tags.add_tag(session, [txn_id], "reimbursable")
        tags.set_trip(session, [txn_id], "Japan 2026")
        session.commit()

    with session_factory() as session:
        assert tags.clear_trip(session, [txn_id]) == 1
        assert tags.clear_trip(session, [txn_id]) == 0  # already clear
        session.commit()

    with session_factory() as session:
        names = {t.name for t in tags.tags_for(session, [txn_id])[txn_id]}
        assert names == {"reimbursable"}


def test_clear_trip_on_an_empty_selection_is_a_no_op(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        assert tags.clear_trip(session, []) == 0


def test_set_trip_on_an_empty_selection_is_a_no_op(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        assert tags.set_trip(session, [], "Japan 2026") == 0
        assert session.scalar(select(Tag)) is None


# --------------------------------------------------------------------------- tags_for


def test_tags_for_returns_an_entry_for_every_id_even_with_no_tags(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        txn = _txn(session, currency, accounts["Checking"], date(2025, 1, 1), -100)
        session.commit()
        txn_id = txn.id

    with session_factory() as session:
        assert tags.tags_for(session, [txn_id]) == {txn_id: []}


def test_tags_for_on_an_empty_selection_is_a_no_op(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        assert tags.tags_for(session, []) == {}


# --------------------------------------------------------------------- rename / delete


def test_rename_tag(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        txn = _txn(session, currency, accounts["Checking"], date(2025, 1, 1), -100)
        session.commit()
        txn_id = txn.id

    with session_factory() as session:
        tags.add_tag(session, [txn_id], "reimbursable")
        session.commit()

    with session_factory() as session:
        assert tags.rename_tag(session, "reimbursable", "work expense")
        session.commit()

    with session_factory() as session:
        names = {t.name for t in tags.tags_for(session, [txn_id])[txn_id]}
        assert names == {"work expense"}
        assert tags.resolve(session, "reimbursable") is None


def test_rename_unknown_tag_returns_false(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        assert tags.rename_tag(session, "nope", "still nope") is False


def test_delete_tag_unlinks_everywhere_and_returns_the_count(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        t1 = _txn(session, currency, accounts["Checking"], date(2025, 1, 1), -100, "A")
        t2 = _txn(session, currency, accounts["Checking"], date(2025, 1, 2), -200, "B")
        session.commit()
        ids = [t1.id, t2.id]

    with session_factory() as session:
        tags.add_tag(session, ids, "reimbursable")
        session.commit()

    with session_factory() as session:
        assert tags.delete_tag(session, "reimbursable") == 2
        session.commit()

    with session_factory() as session:
        assert tags.resolve(session, "reimbursable") is None
        assert tags.tags_for(session, ids) == {ids[0]: [], ids[1]: []}


def test_delete_unknown_tag_returns_zero(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        assert tags.delete_tag(session, "nope") == 0


# ------------------------------------------------------------------------ get_tags


def test_get_tags_counts_and_sums_over_the_transactions_carrying_it(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        t1 = _txn(session, currency, accounts["Checking"], date(2025, 1, 1), -100, "A")
        t2 = _txn(session, currency, accounts["Checking"], date(2025, 1, 2), -200, "B")
        session.commit()
        ids = [t1.id, t2.id]

    with session_factory() as session:
        tags.add_tag(session, ids, "reimbursable")
        tags.add_tag(session, [ids[0]], "urgent")
        session.commit()

    with session_factory() as session:
        rows = {r.name: r for r in queries.get_tags(session, tags.TAG)}
        assert rows["reimbursable"].count == 2
        assert rows["reimbursable"].total_minor == -300
        assert rows["urgent"].count == 1
        assert rows["urgent"].total_minor == -100
        # Busiest first, alphabetical to break ties.
        assert [r.name for r in queries.get_tags(session, tags.TAG)] == [
            "reimbursable", "urgent",
        ]


def test_trips_sort_by_name_not_by_transaction_count(tmp_path):
    """A trip list is a handful of proper nouns the user already knows the name of, so
    it is scanned alphabetically; ordinary tags stay busiest-first."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        txns = [
            _txn(session, currency, accounts["Checking"], date(2025, 1, n + 1), -100, str(n))
            for n in range(3)
        ]
        session.commit()
        ids = [t.id for t in txns]

    with session_factory() as session:
        # Zurich gets the most transactions, Berlin the fewest -- so count order and
        # name order disagree, and the assertion can tell them apart.
        tags.set_trip(session, ids, "Zurich 2026")
        tags.set_trip(session, ids[:2], "Oregon 2026")
        tags.set_trip(session, ids[:1], "Berlin 2026")
        session.commit()

    with session_factory() as session:
        assert [r.name for r in queries.get_tags(session, tags.TRIP)] == [
            "Berlin 2026", "Oregon 2026", "Zurich 2026",
        ]


def test_a_mixed_listing_groups_tags_before_trips(tmp_path):
    """Two kinds sorted by two different rules have to be grouped, not interleaved."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        txn = _txn(session, currency, accounts["Checking"], date(2025, 1, 1), -100, "A")
        session.commit()
        ids = [txn.id]

    with session_factory() as session:
        tags.set_trip(session, ids, "Alps 2026")
        tags.add_tag(session, ids, "zzz-tag")
        session.commit()

    with session_factory() as session:
        rows = queries.get_tags(session)
        # Alphabetically 'Alps 2026' would come first; grouping by kind wins.
        assert [(r.kind, r.name) for r in rows] == [
            (tags.TAG, "zzz-tag"), (tags.TRIP, "Alps 2026"),
        ]


def test_get_tags_shows_a_tag_with_no_transactions(tmp_path):
    """Unlike get_categories, an empty tag stays visible -- a freshly created trip must
    be selectable to put something on it."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        tags.get_or_create(session, "Japan 2026", tags.TRIP)
        session.commit()

    with session_factory() as session:
        rows = queries.get_tags(session, tags.TRIP)
        assert len(rows) == 1
        assert rows[0].count == 0
        assert rows[0].total_minor == 0


def test_resolve_tag_returns_the_id_for_the_right_kind(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        tag = tags.get_or_create(session, "japan", tags.TAG)
        trip = tags.get_or_create(session, "japan", tags.TRIP)
        session.commit()
        tag_id, trip_id = tag.id, trip.id

    with session_factory() as session:
        assert queries.resolve_tag(session, "japan", tags.TAG) == tag_id
        assert queries.resolve_tag(session, "japan", tags.TRIP) == trip_id
        assert queries.resolve_tag(session, "nope") is None


# ---------------------------------------------------------------- filtering by tag/trip


def test_get_transactions_filters_by_tag_id(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        t1 = _txn(session, currency, accounts["Checking"], date(2025, 1, 1), -100, "A")
        _txn(session, currency, accounts["Checking"], date(2025, 1, 2), -200, "B")
        session.commit()
        t1_id = t1.id

    with session_factory() as session:
        tag = tags.get_or_create(session, "reimbursable", tags.TAG)
        tags.add_tag(session, [t1_id], "reimbursable")
        session.commit()
        tag_id = tag.id

    with session_factory() as session:
        rows = queries.get_transactions(
            session, filters=queries.Filters(tag_id=tag_id)
        )
        assert [r.description for r in rows] == ["A"]
        assert rows[0].tags == ("reimbursable",)


def test_get_transactions_filters_by_trip_id_independently_of_tag_id(tmp_path):
    """The two filters are independent and combine with AND."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        t1 = _txn(session, currency, accounts["Checking"], date(2025, 1, 1), -100, "A")
        t2 = _txn(session, currency, accounts["Checking"], date(2025, 1, 2), -200, "B")
        session.commit()
        t1_id, t2_id = t1.id, t2.id

    with session_factory() as session:
        tag = tags.get_or_create(session, "reimbursable", tags.TAG)
        trip = tags.get_or_create(session, "Japan 2026", tags.TRIP)
        tags.add_tag(session, [t1_id, t2_id], "reimbursable")
        tags.set_trip(session, [t1_id], "Japan 2026")
        session.commit()
        tag_id, trip_id = tag.id, trip.id

    with session_factory() as session:
        # Both filters together: only t1 carries both.
        rows = queries.get_transactions(
            session, filters=queries.Filters(tag_id=tag_id, trip_id=trip_id)
        )
        assert [r.description for r in rows] == ["A"]
        assert rows[0].trip == "Japan 2026"

        # Tag alone still matches both.
        rows = queries.get_transactions(session, filters=queries.Filters(tag_id=tag_id))
        assert sorted(r.description for r in rows) == ["A", "B"]

        # A trip filter on its own matches one side of the pair only.
        rows = queries.get_transactions(session, filters=queries.Filters(trip_id=trip_id))
        assert [r.description for r in rows] == ["A"]


def test_totals_and_category_totals_are_scoped_by_tag_id(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        t1 = _txn(session, currency, accounts["Checking"], date(2025, 1, 1), -100, "A")
        _txn(session, currency, accounts["Checking"], date(2025, 1, 2), -200, "B")
        session.commit()
        t1_id = t1.id

    with session_factory() as session:
        tag = tags.get_or_create(session, "reimbursable", tags.TAG)
        tags.add_tag(session, [t1_id], "reimbursable")
        session.commit()
        tag_id = tag.id

    with session_factory() as session:
        totals = queries.get_totals(session, filters=queries.Filters(tag_id=tag_id))
        assert totals.count == 1
        assert totals.outflow_minor == -100

        cat_totals = queries.get_category_totals(
            session, filters=queries.Filters(tag_id=tag_id)
        )
        assert len(cat_totals) == 1
        assert cat_totals[0].count == 1
        assert cat_totals[0].outflow_minor == -100


def test_no_tag_filter_is_unaffected_by_tags_existing(tmp_path):
    """A sanity check that adding tag_id/trip_id did not change the untagged default."""
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts = _seed(session)
        t1 = _txn(session, currency, accounts["Checking"], date(2025, 1, 1), -100, "A")
        session.commit()
        t1_id = t1.id

    with session_factory() as session:
        tags.add_tag(session, [t1_id], "reimbursable")
        session.commit()

    with session_factory() as session:
        rows = queries.get_transactions(session)
        assert len(rows) == 1
        totals = queries.get_totals(session)
        assert totals.count == 1


# ------------------------------------------------------------------ cascade on unimport


def test_unimporting_a_tagged_transaction_cascades_to_transaction_tag(tmp_path):
    """The whole reason ondelete='CASCADE' is on both FKs: without it, SQLite (with
    foreign_keys=ON, which db.py sets per connection) would refuse the delete with an
    IntegrityError instead of quietly cleaning up the tag link.
    """
    session_factory = _setup_import(tmp_path)
    with session_factory() as session:
        txn = session.scalar(select(Transaction).where(Transaction.description == "COFFEE SHOP A"))
        import_id = txn.import_id
        txn_id = txn.id
        tags.add_tag(session, [txn_id], "reimbursable")
        tags.set_trip(session, [txn_id], "Japan 2026")
        session.commit()

    with session_factory() as session:
        assert len(list(session.scalars(select(TransactionTag)))) == 2
        delete_import(session, import_id)  # must not raise IntegrityError
        session.commit()

    with session_factory() as session:
        assert session.get(Transaction, txn_id) is None
        # The cascade reached SQLite itself, not just the ORM's own bookkeeping.
        assert list(session.scalars(select(TransactionTag))) == []
        # The tags themselves survive -- only the link to the deleted row is gone.
        assert tags.resolve(session, "reimbursable") is not None
        assert tags.resolve(session, "Japan 2026", tags.TRIP) is not None


# ------------------------------------------------------------------------------ init_db


def test_init_db_creates_the_tag_tables_on_a_database_that_predates_them(tmp_path):
    """New tables need no ``_ADDED_COLUMNS`` entry -- ``create_all`` alone should pick
    them up on a database built before tag/transaction_tag existed."""
    engine = get_engine(tmp_path / "pre_tags.db")
    # A minimal legacy-shaped database: everything except tag/transaction_tag, built
    # directly rather than through Base.metadata so the two new tables are genuinely
    # absent going in.
    legacy_tables = [
        t for t in Base.metadata.sorted_tables if t.name not in ("tag", "transaction_tag")
    ]
    with engine.begin() as connection:
        for table in legacy_tables:
            table.create(connection, checkfirst=True)

    init_db(engine)  # must not raise, and must create the two missing tables

    with engine.begin() as connection:
        table_names = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    assert {"tag", "transaction_tag"} <= table_names

    # And it is now a fully working database -- tags can be created and linked.
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        currency, accounts = _seed(session)
        txn = _txn(session, currency, accounts["Checking"], date(2025, 1, 1), -100)
        session.commit()
        txn_id = txn.id

    with session_factory() as session:
        assert tags.add_tag(session, [txn_id], "reimbursable") == 1
        session.commit()
