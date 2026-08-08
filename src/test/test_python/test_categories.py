"""Tests for manual categories and pattern-based category rules."""

from datetime import date

import pytest
from sqlalchemy import select

from budget_tracker import categories, queries, vendors
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.importer import import_csv
from budget_tracker.models import Account, Category, CategoryRule, Currency, Transaction
from helpers import learn_format


def _legacy_session_factory(tmp_path, name="legacy.db"):
    """A database whose category table predates the value-uniqueness constraint.

    :func:`categories.merge_category` has to work on exactly this: a database with two
    same-named categories, which is what it exists to clean up before
    :func:`budget_tracker.db.init_db` can add the constraint that forbids them. Bypasses
    ``init_db``, which would refuse to open a database with duplicates already in it.
    """
    from sqlalchemy import text

    from budget_tracker.models import Base

    engine = get_engine(tmp_path / name)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE category ("
                "id INTEGER PRIMARY KEY, parent_id INTEGER, value VARCHAR, "
                "created_at TIMESTAMP, updated_at TIMESTAMP, "
                "CONSTRAINT uq_category_parent_value UNIQUE (parent_id, value))"
            )
        )
    Base.metadata.create_all(engine)  # everything else; category already exists above
    return get_sessionmaker(engine)


def _seed_account_and_currency(session):
    """A minimal account/currency pair, for tests that hand-build transactions."""
    currency = session.scalar(select(Currency))
    if currency is None:
        currency = Currency(value="USD", symbol="$", decimal_places=2)
        session.add(currency)
        session.flush()
    account = session.scalar(select(Account))
    if account is None:
        account = Account(name="Checking", currency_id=currency.id)
        session.add(account)
        session.flush()
    return account, currency


def _plain_session_factory(tmp_path, name="plain.db"):
    """A blank database, for tests that build categories directly rather than importing."""
    engine = get_engine(tmp_path / name)
    init_db(engine)
    return get_sessionmaker(engine)

CSV = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2025-07-01,2025-07-02,8207,COFFEE SHOP A,,3.00,
2025-07-01,2025-07-02,8207,COFFEE SHOP B,,4.00,
2025-07-03,2025-07-04,8207,COFFEE SHOP A,,3.50,
2025-07-05,2025-07-06,8207,GROCERY STORE,Groceries,20.00,
"""

LATER_CSV = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2025-08-01,2025-08-02,8207,COFFEE SHOP A,,5.00,
2025-08-05,2025-08-06,8207,COFFEE SHOP B,,6.00,
"""


def _setup(tmp_path):
    engine = get_engine(tmp_path / "t.db")
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    csv_path = tmp_path / "in.csv"
    csv_path.write_text(CSV, encoding="utf-8")
    with session_factory() as session:
        learn_format(session, csv_path)
        import_csv(session, csv_path)
    return session_factory


def _rows(session):
    """``description -> (category value, source)`` for every transaction."""
    result = {}
    for txn in session.scalars(select(Transaction)):
        value = txn.category.value if txn.category else None
        result.setdefault(txn.description, []).append((value, txn.category_source))
    return result


def _categories(session, description):
    pairs = set(_rows(session).get(description, []))
    return sorted(pairs, key=lambda pair: (pair[0] or "", pair[1]))


# ------------------------------------------------------------------- manual


def test_set_category_by_raw_vendor_name(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        assert categories.set_category(session, "COFFEE SHOP A", "Dining") == 2
        session.commit()

    with session_factory() as session:
        assert _categories(session, "COFFEE SHOP A") == [("Dining", "manual")]
        assert _categories(session, "COFFEE SHOP B") == [(None, "unset")]


def test_set_category_by_display_name_covers_the_whole_group(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        vendors.set_override(session, "COFFEE SHOP A", "Coffee")
        vendors.set_override(session, "COFFEE SHOP B", "Coffee")
        assert categories.set_category(session, "Coffee", "Dining") == 3
        session.commit()

    with session_factory() as session:
        assert _categories(session, "COFFEE SHOP A") == [("Dining", "manual")]
        assert _categories(session, "COFFEE SHOP B") == [("Dining", "manual")]


def test_set_category_of_unknown_vendor_returns_zero(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        assert categories.set_category(session, "DOES NOT EXIST", "Dining") == 0


def test_manual_category_survives_reimport(tmp_path):
    """Re-importing must not clobber a category, and new rows must inherit it."""
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        categories.set_category(session, "COFFEE SHOP A", "Dining")
        session.commit()

    later = tmp_path / "later.csv"
    later.write_text(LATER_CSV, encoding="utf-8")
    with session_factory() as session:
        again = import_csv(session, tmp_path / "in.csv")
        assert (again.inserted, again.skipped_duplicates) == (0, 4)
        assert import_csv(session, later).inserted == 2

    with session_factory() as session:
        rows = sorted(_rows(session)["COFFEE SHOP A"], key=lambda p: p[1])
        # The two original rows kept their category through both imports. Categories
        # are per transaction, so the newly imported row is uncategorised until the
        # user says otherwise (a rule is how you make that automatic).
        assert rows == [("Dining", "manual"), ("Dining", "manual"), (None, "unset")]


def test_manual_category_is_not_overwritten_by_a_rule(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        categories.set_category(session, "COFFEE SHOP A", "Dining")
        categories.add_rule(session, "COFFEE SHOP*", "Coffee")
        assert categories.apply_category_rules(session) == 1  # only SHOP B
        session.commit()

    with session_factory() as session:
        assert _categories(session, "COFFEE SHOP A") == [("Dining", "manual")]
        assert _categories(session, "COFFEE SHOP B") == [("Coffee", "rule")]


def test_clear_category_reverts_only_manual_rows(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        categories.set_category(session, "COFFEE SHOP A", "Dining")
        session.commit()

    with session_factory() as session:
        assert categories.clear_category(session, "COFFEE SHOP A") == 2
        # The bank's own category is not this function's to undo.
        assert categories.clear_category(session, "GROCERY STORE") == 0
        session.commit()

    with session_factory() as session:
        assert _categories(session, "COFFEE SHOP A") == [(None, "unset")]
        assert _categories(session, "GROCERY STORE") == [("Groceries", "import")]


# --------------------------------------------------------------------- per-row form


def test_set_category_for_categorises_exactly_the_given_rows(tmp_path):
    """The per-row form must not spill onto other transactions of the same vendor --
    unlike set_category, which is deliberately vendor-wide."""
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        rows = {t.description: t.id for t in queries.get_transactions(session)}
        # Two "COFFEE SHOP A" rows exist; only tag the first one found.
        shop_a_ids = [
            t.id for t in session.scalars(
                select(Transaction).where(Transaction.description == "COFFEE SHOP A")
            )
        ]
        target_id = shop_a_ids[0]

    with session_factory() as session:
        assert categories.set_category_for(session, [target_id], "Dining") == 1
        session.commit()

    with session_factory() as session:
        target = session.get(Transaction, target_id)
        assert (target.category.value, target.category_source) == ("Dining", "manual")
        other_id = [i for i in shop_a_ids if i != target_id][0]
        other = session.get(Transaction, other_id)
        assert other.category_id is None  # the sibling row is untouched


def test_set_category_for_empty_selection_is_a_no_op(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        assert categories.set_category_for(session, [], "Dining") == 0


def test_clear_category_for_only_clears_manual_rows(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        grocery = session.scalar(
            select(Transaction).where(Transaction.description == "GROCERY STORE")
        )
        grocery_id = grocery.id
        shop_a_id = session.scalars(
            select(Transaction.id).where(Transaction.description == "COFFEE SHOP A")
        ).first()

    with session_factory() as session:
        categories.set_category_for(session, [shop_a_id], "Dining")
        session.commit()

    with session_factory() as session:
        # The bank-supplied category on GROCERY STORE is not this function's to undo.
        assert categories.clear_category_for(session, [grocery_id, shop_a_id]) == 1
        session.commit()

    with session_factory() as session:
        assert session.get(Transaction, shop_a_id).category_id is None
        grocery = session.get(Transaction, grocery_id)
        assert (grocery.category.value, grocery.category_source) == ("Groceries", "import")


def test_clear_category_for_empty_selection_is_a_no_op(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        assert categories.clear_category_for(session, []) == 0


def test_get_or_create_reuses_an_existing_category(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        existing = session.scalar(select(Category).where(Category.value == "Groceries"))
        assert categories.get_or_create(session, "Groceries").id == existing.id
        session.commit()

    with session_factory() as session:
        names = [c.value for c in session.scalars(select(Category))]
        assert names.count("Groceries") == 1


# -------------------------------------------------------------------- rules


def test_rule_categorises_matching_vendors(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        categories.add_rule(session, "COFFEE SHOP*", "Dining")
        assert categories.apply_category_rules(session) == 3
        session.commit()

    with session_factory() as session:
        assert _categories(session, "COFFEE SHOP A") == [("Dining", "rule")]
        assert _categories(session, "COFFEE SHOP B") == [("Dining", "rule")]
        assert _categories(session, "GROCERY STORE") == [("Groceries", "import")]
        assert categories.apply_category_rules(session) == 0  # idempotent


def test_rule_overwrites_the_banks_own_category(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        categories.add_rule(session, "GROCERY*", "Food")
        assert categories.apply_category_rules(session) == 1
        session.commit()

    with session_factory() as session:
        assert _categories(session, "GROCERY STORE") == [("Food", "rule")]


def test_rule_applies_to_rows_from_later_imports(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        categories.add_rule(session, "COFFEE SHOP*", "Dining")
        categories.apply_category_rules(session)
        session.commit()

    later = tmp_path / "later.csv"
    later.write_text(LATER_CSV, encoding="utf-8")
    with session_factory() as session:
        assert import_csv(session, later).inserted == 2

    with session_factory() as session:
        # Categorised by the importer, with no manual step.
        assert _categories(session, "COFFEE SHOP A") == [("Dining", "rule")]
        assert len(_rows(session)["COFFEE SHOP A"]) == 3


def test_rule_matches_the_display_name_after_a_rename(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        vendors.set_override(session, "COFFEE SHOP A", "Beanery")
        categories.add_rule(session, "Beanery", "Dining")
        assert categories.apply_category_rules(session) == 2
        session.commit()

    with session_factory() as session:
        assert _categories(session, "COFFEE SHOP A") == [("Dining", "rule")]
        assert _categories(session, "COFFEE SHOP B") == [(None, "unset")]


def test_rule_matches_the_raw_string_despite_a_rename(tmp_path):
    """A rule written before a rename keeps working after it."""
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        categories.add_rule(session, "COFFEE SHOP A", "Dining")
        categories.apply_category_rules(session)
        vendors.set_override(session, "COFFEE SHOP A", "Beanery")
        assert categories.apply_category_rules(session) == 0  # still matched
        session.commit()

    with session_factory() as session:
        assert _categories(session, "COFFEE SHOP A") == [("Dining", "rule")]


def test_first_matching_rule_wins(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        categories.add_rule(session, "COFFEE SHOP*", "Dining")
        categories.add_rule(session, "COFFEE SHOP A", "Treats")  # added second
        categories.apply_category_rules(session)
        session.commit()

    with session_factory() as session:
        assert _categories(session, "COFFEE SHOP A") == [("Dining", "rule")]


def test_retargeting_a_rule_updates_the_rows_it_owns(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        categories.add_rule(session, "COFFEE SHOP*", "Dining")
        categories.apply_category_rules(session)
        session.commit()

    with session_factory() as session:
        categories.add_rule(session, "COFFEE SHOP*", "Treats")
        assert categories.apply_category_rules(session) == 3
        assert len(categories.list_rules(session)) == 1  # re-targeted, not duplicated
        session.commit()

    with session_factory() as session:
        assert _categories(session, "COFFEE SHOP A") == [("Treats", "rule")]


def test_removing_a_rule_reverts_only_the_rows_it_owned(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        categories.set_category(session, "COFFEE SHOP A", "Dining")
        categories.add_rule(session, "COFFEE SHOP*", "Coffee")
        categories.apply_category_rules(session)
        session.commit()

    with session_factory() as session:
        assert categories.remove_rule(session, "COFFEE SHOP*")
        assert categories.remove_rule(session, "COFFEE SHOP*") is False
        assert categories.apply_category_rules(session) == 1
        session.commit()

    with session_factory() as session:
        assert _categories(session, "COFFEE SHOP B") == [(None, "unset")]
        assert _categories(session, "COFFEE SHOP A") == [("Dining", "manual")]
        assert _categories(session, "GROCERY STORE") == [("Groceries", "import")]


# ---------------------------------------------------------------- transfers

# Two card numbers, so the legs land in different accounts and pair up.
XFER_OUT_CSV = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2025-09-01,2025-09-02,8207,XFER OUT,,500.00,
"""

XFER_IN_CSV = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2025-09-02,2025-09-03,9911,XFER IN,,,500.00
"""


def _import_text(session_factory, tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    with session_factory() as session:
        return import_csv(session, path)


def _rule_first_database(tmp_path):
    """A second, independent database with the rule in place before anything is imported."""
    engine = get_engine(tmp_path / "rule_first.db")
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    csv_path = tmp_path / "seed.csv"
    csv_path.write_text(CSV, encoding="utf-8")
    with session_factory() as session:
        learn_format(session, csv_path)
        categories.add_rule(session, "XFER*", "Investing")
        session.commit()
    return session_factory


def test_detected_transfer_legs_are_categorised_transfer(tmp_path):
    session_factory = _setup(tmp_path)
    _import_text(session_factory, tmp_path, "out.csv", XFER_OUT_CSV)
    _import_text(session_factory, tmp_path, "in.csv2", XFER_IN_CSV)

    with session_factory() as session:
        assert _categories(session, "XFER OUT") == [("Transfer", "transfer")]
        assert _categories(session, "XFER IN") == [("Transfer", "transfer")]


def test_rules_leave_detected_transfer_legs_alone(tmp_path):
    """A paired leg reads as a transfer however the rule and the pairing were ordered.

    Rules run before detection on an import, and detection only stamps *newly* paired
    legs, so without this the answer depended on whether the rule existed when the pair
    was found.
    """
    session_factory = _setup(tmp_path)
    _import_text(session_factory, tmp_path, "out.csv", XFER_OUT_CSV)
    _import_text(session_factory, tmp_path, "in.csv2", XFER_IN_CSV)

    # Rule added after the pair was already detected.
    with session_factory() as session:
        categories.add_rule(session, "XFER*", "Investing")
        assert categories.apply_category_rules(session) == 0
        session.commit()

    with session_factory() as session:
        assert _categories(session, "XFER OUT") == [("Transfer", "transfer")]
        assert _categories(session, "XFER IN") == [("Transfer", "transfer")]

    # ...and a pair detected after the rule already existed reaches the same place.
    other = _rule_first_database(tmp_path)
    _import_text(other, tmp_path, "out2.csv", XFER_OUT_CSV)
    _import_text(other, tmp_path, "in2.csv", XFER_IN_CSV)
    with other() as session:
        assert _categories(session, "XFER OUT") == [("Transfer", "transfer")]
        assert _categories(session, "XFER IN") == [("Transfer", "transfer")]

    # A hand-picked category still outranks both.
    with session_factory() as session:
        assert categories.set_category(session, "XFER OUT", "Investing") == 1
        session.commit()
    with session_factory() as session:
        assert _categories(session, "XFER OUT") == [("Investing", "manual")]


def test_a_manual_category_survives_transfer_detection(tmp_path):
    session_factory = _setup(tmp_path)
    _import_text(session_factory, tmp_path, "out.csv", XFER_OUT_CSV)
    with session_factory() as session:
        assert categories.set_category(session, "XFER OUT", "Investing") == 1
        session.commit()

    _import_text(session_factory, tmp_path, "in.csv2", XFER_IN_CSV)

    with session_factory() as session:
        assert _categories(session, "XFER OUT") == [("Investing", "manual")]
        assert _categories(session, "XFER IN") == [("Transfer", "transfer")]
        # Both legs are still paired, so both stay out of the totals.
        flags = {t.description: t.is_transfer for t in queries.get_transactions(session)}
        assert flags["XFER OUT"] and flags["XFER IN"]


# ----------------------------------------------------------------------------- paths

def test_parse_path_splits_and_strips():
    assert categories.parse_path("Food > Dining") == ["Food", "Dining"]
    assert categories.parse_path("  Food  >  Dining  ") == ["Food", "Dining"]
    assert categories.parse_path("Food") == ["Food"]


def test_parse_path_rejects_empty_segments():
    with pytest.raises(categories.CategoryError, match="Bad category path"):
        categories.parse_path("Food > > Dining")
    with pytest.raises(categories.CategoryError, match="Bad category path"):
        categories.parse_path(" > Food")
    with pytest.raises(categories.CategoryError, match="Bad category path"):
        categories.parse_path("")


def test_format_path_walks_up_to_the_root(tmp_path):
    session_factory = _plain_session_factory(tmp_path)
    with session_factory() as session:
        leaf = categories.ensure_path(session, "Food > Dining > Restaurants")
        session.commit()
        leaf_id = leaf.id

    with session_factory() as session:
        leaf = session.get(Category, leaf_id)
        assert categories.format_path(session, leaf) == "Food > Dining > Restaurants"
        top = session.scalar(select(Category).where(Category.value == "Food"))
        assert categories.format_path(session, top) == "Food"


# ------------------------------------------------------------------- ensure_path / moves

def test_ensure_path_creates_a_chain(tmp_path):
    session_factory = _plain_session_factory(tmp_path)
    with session_factory() as session:
        leaf = categories.ensure_path(session, "Food > Dining > Restaurants")
        session.commit()
        assert leaf.value == "Restaurants"

    with session_factory() as session:
        names = sorted(c.value for c in session.scalars(select(Category)))
        assert names == ["Dining", "Food", "Restaurants"]
        leaf = session.scalar(select(Category).where(Category.value == "Restaurants"))
        assert categories.format_path(session, leaf) == "Food > Dining > Restaurants"


def test_ensure_path_is_idempotent(tmp_path):
    session_factory = _plain_session_factory(tmp_path)
    with session_factory() as session:
        first = categories.ensure_path(session, "Food > Dining")
        second = categories.ensure_path(session, "Food > Dining")
        session.commit()
        assert first.id == second.id

    with session_factory() as session:
        names = [c.value for c in session.scalars(select(Category))]
        assert names.count("Dining") == 1


def test_ensure_path_reparents_an_existing_category(tmp_path):
    """An existing top-level category is moved, not duplicated, when nested — but only
    once the relocation is confirmed; the default refuses it (see :func:`ensure_path`)."""
    session_factory = _plain_session_factory(tmp_path)
    with session_factory() as session:
        dining = categories.get_or_create(session, "Dining")
        session.commit()
        dining_id = dining.id

    with session_factory() as session:
        with pytest.raises(categories.CategoryError, match="relocate"):
            categories.ensure_path(session, "Food > Dining")
        moved = categories.ensure_path(session, "Food > Dining", confirm_relocation=True)
        session.commit()
        assert moved.id == dining_id

    with session_factory() as session:
        names = [c.value for c in session.scalars(select(Category))]
        assert names.count("Dining") == 1
        dining = session.scalar(select(Category).where(Category.value == "Dining"))
        assert categories.format_path(session, dining) == "Food > Dining"


def test_ensure_path_one_element_moves_to_top_level(tmp_path):
    """A one-element path is a relocation too — promoting a nested category to the top
    level needs the same confirmation as nesting one deeper does."""
    session_factory = _plain_session_factory(tmp_path)
    with session_factory() as session:
        categories.ensure_path(session, "Food > Dining")
        session.commit()

    with session_factory() as session:
        with pytest.raises(categories.CategoryError, match="relocate"):
            categories.ensure_path(session, "Dining")
        moved = categories.ensure_path(session, "Dining", confirm_relocation=True)
        session.commit()
        assert moved.parent_id is None

    with session_factory() as session:
        dining = session.scalar(select(Category).where(Category.value == "Dining"))
        assert dining.parent_id is None


# ----------------------------------------------------------------------- resolve_path

def test_resolve_path_exact_match(tmp_path):
    session_factory = _plain_session_factory(tmp_path)
    with session_factory() as session:
        categories.ensure_path(session, "Food > Dining > Restaurants")
        categories.ensure_path(session, "Travel > Airfare")
        session.commit()

    with session_factory() as session:
        found = categories.resolve_path(session, "Food > Dining > Restaurants")
        assert found is not None and found.value == "Restaurants"
        assert categories.resolve_path(session, "Nope > Dining") is None


def test_resolve_path_bare_name_unambiguous(tmp_path):
    session_factory = _plain_session_factory(tmp_path)
    with session_factory() as session:
        categories.ensure_path(session, "Food > Groceries")
        session.commit()

    with session_factory() as session:
        found = categories.resolve_path(session, "Groceries")
        assert found is not None and found.value == "Groceries"


def test_ensure_path_refuses_to_duplicate_a_name_across_branches(tmp_path):
    """Two categories can no longer share a name in different branches: asking to place
    ``Other`` under a second parent is a *relocation* of the existing one, not a fresh
    duplicate, and is refused without confirmation (see :func:`ensure_path`)."""
    session_factory = _plain_session_factory(tmp_path)
    with session_factory() as session:
        categories.ensure_path(session, "Food > Other")
        session.commit()

    with session_factory() as session:
        with pytest.raises(categories.CategoryError, match="relocate"):
            categories.ensure_path(session, "Travel > Other")

        moved = categories.ensure_path(session, "Travel > Other", confirm_relocation=True)
        session.commit()
        moved_id = moved.id

    with session_factory() as session:
        names = [c.value for c in session.scalars(select(Category))]
        assert names.count("Other") == 1  # moved, not duplicated
        assert session.get(Category, moved_id).value == "Other"
        assert categories.resolve_path(session, "Travel > Other") is not None
        assert categories.resolve_path(session, "Food > Other") is None  # moved away


# ------------------------------------------------------------------------- set_parent

def test_get_or_create_finds_a_nested_category_instead_of_forking_one(tmp_path):
    """The bug this module exists to fix: a name is looked up across the *whole* tree,
    so asking for an already-nested category by its bare name reuses it rather than
    creating a rival top-level one of the same name (which the unique constraint on
    ``category.value`` would now reject anyway)."""
    session_factory = _plain_session_factory(tmp_path)
    with session_factory() as session:
        nested = categories.ensure_path(session, "Food > Other")
        session.commit()
        nested_id = nested.id

    with session_factory() as session:
        found = categories.get_or_create(session, "Other")
        assert found.id == nested_id
        session.commit()

    with session_factory() as session:
        assert [c.value for c in session.scalars(select(Category))].count("Other") == 1


def test_set_parent_rejects_a_direct_cycle(tmp_path):
    session_factory = _plain_session_factory(tmp_path)
    with session_factory() as session:
        categories.ensure_path(session, "Food > Dining")
        food = session.scalar(select(Category).where(Category.value == "Food"))
        dining = session.scalar(select(Category).where(Category.value == "Dining"))
        session.commit()
        food_id, dining_id = food.id, dining.id

    with session_factory() as session:
        food = session.get(Category, food_id)
        dining = session.get(Category, dining_id)
        with pytest.raises(categories.CategoryError, match="cycle"):
            categories.set_parent(session, food, dining)


def test_set_parent_rejects_an_indirect_cycle(tmp_path):
    session_factory = _plain_session_factory(tmp_path)
    with session_factory() as session:
        leaf = categories.ensure_path(session, "Food > Dining > Restaurants")
        food = session.scalar(select(Category).where(Category.value == "Food"))
        session.commit()
        leaf_id, food_id = leaf.id, food.id

    with session_factory() as session:
        food = session.get(Category, food_id)
        restaurants = session.get(Category, leaf_id)
        with pytest.raises(categories.CategoryError, match="cycle"):
            categories.set_parent(session, food, restaurants)


def test_set_parent_to_none_moves_to_top_level(tmp_path):
    session_factory = _plain_session_factory(tmp_path)
    with session_factory() as session:
        leaf = categories.ensure_path(session, "Food > Dining")
        session.commit()
        leaf_id = leaf.id

    with session_factory() as session:
        dining = session.get(Category, leaf_id)
        categories.set_parent(session, dining, None)
        session.commit()

    with session_factory() as session:
        dining = session.scalar(select(Category).where(Category.value == "Dining"))
        assert dining.parent_id is None


# ------------------------------------------------------------ children / descendant_ids

def test_children_and_descendant_ids(tmp_path):
    session_factory = _plain_session_factory(tmp_path)
    with session_factory() as session:
        categories.ensure_path(session, "Food > Dining > Restaurants")
        categories.ensure_path(session, "Food > Dining > Fast Food")
        categories.ensure_path(session, "Food > Groceries")
        session.commit()

    with session_factory() as session:
        food = session.scalar(select(Category).where(Category.value == "Food"))
        dining = session.scalar(select(Category).where(Category.value == "Dining"))
        restaurants = session.scalar(select(Category).where(Category.value == "Restaurants"))

        assert sorted(c.value for c in categories.children(session, food.id)) == [
            "Dining", "Groceries",
        ]
        assert sorted(c.value for c in categories.children(session, dining.id)) == [
            "Fast Food", "Restaurants",
        ]
        assert categories.children(session, restaurants.id) == []  # a leaf has none

        all_ids = set(categories.descendant_ids(session, food.id))
        names = {c.value for c in session.scalars(select(Category)) if c.id in all_ids}
        assert names == {"Food", "Dining", "Restaurants", "Fast Food", "Groceries"}

        # Inclusive of the id itself, even with no children.
        assert categories.descendant_ids(session, restaurants.id) == [restaurants.id]

        dining_ids = set(categories.descendant_ids(session, dining.id))
        dining_names = {c.value for c in session.scalars(select(Category)) if c.id in dining_ids}
        assert dining_names == {"Dining", "Restaurants", "Fast Food"}


# ------------------------------------------------------------- subtree-aware filtering

def test_filtering_by_a_parent_returns_descendant_transactions(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        categories.set_category(session, "COFFEE SHOP A", "Restaurants")
        categories.set_category(session, "COFFEE SHOP B", "Fast Food")
        categories.ensure_path(session, "Dining > Restaurants", confirm_relocation=True)
        categories.ensure_path(session, "Dining > Fast Food", confirm_relocation=True)
        session.commit()

    with session_factory() as session:
        dining = session.scalar(select(Category).where(Category.value == "Dining"))
        rows = queries.get_transactions(session, category_id=dining.id)
        assert len(rows) == 3  # 2x SHOP A + 1x SHOP B
        assert {r.description for r in rows} == {"COFFEE SHOP A", "COFFEE SHOP B"}

        totals = queries.get_totals(session, category_id=dining.id)
        assert totals.count == 3

        # A leaf-only filter (one side of the two children) still returns just its own.
        restaurants_id = session.scalar(select(Category.id).where(Category.value == "Restaurants"))
        leaf_rows = queries.get_transactions(session, category_id=restaurants_id)
        assert {r.description for r in leaf_rows} == {"COFFEE SHOP A"}

        # The uncategorised sentinel is unaffected by the subtree change.
        assert queries.get_transactions(session, category_id=queries.UNCATEGORISED_ID) == []


def test_get_categories_rolls_up_parent_counts_and_totals(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        categories.set_category(session, "COFFEE SHOP A", "Restaurants")
        categories.set_category(session, "COFFEE SHOP B", "Fast Food")
        categories.ensure_path(session, "Dining > Restaurants", confirm_relocation=True)
        categories.ensure_path(session, "Dining > Fast Food", confirm_relocation=True)
        session.commit()

    with session_factory() as session:
        rows = {r.name: r for r in queries.get_categories(session)}

    assert rows["Dining"].count == 3
    assert rows["Dining"].total_minor == (
        rows["Restaurants"].total_minor + rows["Fast Food"].total_minor
    )
    assert rows["Dining"].depth == 0
    assert rows["Dining"].parent_id is None
    assert rows["Restaurants"].depth == 1
    assert rows["Restaurants"].parent_id is not None
    assert rows["Groceries"].depth == 0  # an untouched sibling tree


# ------------------------------------------ a rule/manual category surviving a re-parent

def test_manual_category_still_works_after_its_category_is_nested(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        assert categories.set_category(session, "COFFEE SHOP A", "Dining") == 2
        session.commit()

    with session_factory() as session:
        categories.ensure_path(session, "Food > Dining", confirm_relocation=True)  # re-parent after the fact
        session.commit()

    with session_factory() as session:
        assert _categories(session, "COFFEE SHOP A") == [("Dining", "manual")]
        food = session.scalar(select(Category).where(Category.value == "Food"))
        rows = queries.get_transactions(session, category_id=food.id)
        assert {r.description for r in rows} == {"COFFEE SHOP A"}


def test_category_rule_still_works_after_its_category_is_nested(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        categories.add_rule(session, "COFFEE SHOP*", "Dining")
        assert categories.apply_category_rules(session) == 3
        session.commit()

    with session_factory() as session:
        categories.ensure_path(session, "Food > Dining", confirm_relocation=True)
        session.commit()

    with session_factory() as session:
        assert _categories(session, "COFFEE SHOP A") == [("Dining", "rule")]
        assert _categories(session, "COFFEE SHOP B") == [("Dining", "rule")]
        assert categories.apply_category_rules(session) == 0  # still matched, unaffected


# ------------------------------------------------------------------ preview_path

def test_preview_path_reports_pure_creates_with_no_relocation(tmp_path):
    session_factory = _plain_session_factory(tmp_path)
    with session_factory() as session:
        preview = categories.preview_path(session, "Food > Dining > Restaurants")
        assert preview.creates == ["Food", "Dining", "Restaurants"]
        assert preview.relocations == []


def test_preview_path_reports_a_relocation_with_its_rolled_up_subtree_count(tmp_path):
    session_factory = _plain_session_factory(tmp_path)
    with session_factory() as session:
        dining = categories.ensure_path(session, "Dining")
        restaurants = categories.ensure_path(session, "Dining > Restaurants")
        account, currency = _seed_account_and_currency(session)
        session.add(
            Transaction(
                account_id=account.id, currency_id=currency.id,
                posted_date=date(2025, 1, 1),
                description="x", raw_description="x", value_minor=-100,
                category_id=dining.id, category_source="manual",
                import_hash="h1",
            )
        )
        session.add(
            Transaction(
                account_id=account.id, currency_id=currency.id,
                posted_date=date(2025, 1, 2),
                description="y", raw_description="y", value_minor=-200,
                category_id=restaurants.id, category_source="manual",
                import_hash="h2",
            )
        )
        session.commit()

    with session_factory() as session:
        preview = categories.preview_path(session, "Food > Dining")
        assert preview.creates == ["Food"]
        assert len(preview.relocations) == 1
        relocation = preview.relocations[0]
        assert relocation.name == "Dining"
        assert relocation.from_parent is None  # was at the top level
        assert relocation.to_parent == "Food"
        # Rolled up: Dining's own transaction plus Restaurants' underneath it.
        assert relocation.transaction_count == 2


# --------------------------------------------------------------------------- merging

def test_merge_category_repoints_transactions_rules_and_children(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        categories.set_category(session, "COFFEE SHOP A", "Restaurants")
        categories.add_rule(session, "COFFEE SHOP B*", "Restaurants")
        categories.apply_category_rules(session)
        # A second, unrelated category with a child, so the merge has something to
        # repoint on the child side too.
        dining = categories.ensure_path(session, "Dining")
        categories.ensure_path(session, "Dining > Snacks")
        session.commit()
        dining_id = dining.id

    with session_factory() as session:
        result = categories.merge_category(session, "Restaurants", "Dining")
        session.commit()
        assert result.source == "Restaurants"
        assert result.target == "Dining"
        assert result.moved_transactions == 3  # 2x SHOP A + 1x SHOP B
        assert result.moved_rules == 1
        assert result.moved_children == 0  # Restaurants had no children of its own

    with session_factory() as session:
        names = [c.value for c in session.scalars(select(Category))]
        assert "Restaurants" not in names
        assert _categories(session, "COFFEE SHOP A") == [("Dining", "manual")]
        assert _categories(session, "COFFEE SHOP B") == [("Dining", "rule")]
        rule = session.scalar(select(CategoryRule))
        assert rule.category.value == "Dining"
        # Dining's pre-existing child survived the merge untouched.
        assert sorted(c.value for c in categories.children(session, dining_id)) == ["Snacks"]


def test_merge_category_moves_children_of_the_merged_category(tmp_path):
    session_factory = _plain_session_factory(tmp_path)
    with session_factory() as session:
        categories.ensure_path(session, "Food > Dining > Restaurants")
        categories.ensure_path(session, "Food > Dining > Fast Food")
        session.commit()

    with session_factory() as session:
        categories.merge_category(session, "Dining", "Food")
        session.commit()

    with session_factory() as session:
        food = session.scalar(select(Category).where(Category.value == "Food"))
        assert sorted(c.value for c in categories.children(session, food.id)) == [
            "Fast Food", "Restaurants",
        ]
        assert session.scalar(select(Category).where(Category.value == "Dining")) is None


def test_merge_category_rejects_merging_into_itself(tmp_path):
    session_factory = _plain_session_factory(tmp_path)
    with session_factory() as session:
        categories.get_or_create(session, "Dining")
        session.commit()

    with session_factory() as session:
        with pytest.raises(categories.CategoryError, match="itself"):
            categories.merge_category(session, "Dining", "Dining")


def test_merge_category_rejects_merging_into_its_own_descendant(tmp_path):
    session_factory = _plain_session_factory(tmp_path)
    with session_factory() as session:
        categories.ensure_path(session, "Food > Dining")
        session.commit()

    with session_factory() as session:
        with pytest.raises(categories.CategoryError, match="descendant"):
            categories.merge_category(session, "Food", "Dining")


def test_merge_category_by_full_path_disambiguates_a_shared_leaf_name(tmp_path):
    """The scenario this whole change exists to fix, cleaned up: a nested and a
    top-level category share a name. Bare text picks the top-level one (its own path
    is just its value); the nested one needs its full path."""
    session_factory = _legacy_session_factory(tmp_path)
    with session_factory() as session:
        categories.ensure_path(session, "Travel > Airfare")
        # Simulate the pre-fix bug directly (get_or_create no longer creates this).
        stray = Category(value="Airfare", parent_id=None)
        session.add(stray)
        session.commit()

    with session_factory() as session:
        result = categories.merge_category(session, "Airfare", "Travel > Airfare")
        session.commit()
        assert result.source == "Airfare"
        assert result.target == "Airfare"

    with session_factory() as session:
        assert [c.value for c in session.scalars(select(Category))].count("Airfare") == 1
        survivor = session.scalar(select(Category).where(Category.value == "Airfare"))
        assert categories.format_path(session, survivor) == "Travel > Airfare"
