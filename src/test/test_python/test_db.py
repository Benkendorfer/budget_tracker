"""Tests for :mod:`budget_tracker.db`'s schema setup and migrations.

The category-name-uniqueness migration is the interesting one here: it has to cope
with a database created before names were unique across the whole tree, which means
building a legacy-shaped ``category`` table by hand rather than through the current
model (which already bakes the constraint in).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from budget_tracker.db import (
    DuplicateCategoryNamesError,
    get_engine,
    get_sessionmaker,
    init_db,
)
from budget_tracker.models import Base, Category


def _legacy_category_table(engine):
    """A ``category`` table as it looked under the old ``(parent_id, value)``
    constraint, which SQLite's NULL-distinctness meant never actually constrained
    top-level categories at all — the bug this whole change fixes.
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE category ("
                "id INTEGER PRIMARY KEY, parent_id INTEGER, value VARCHAR, "
                "created_at TIMESTAMP, updated_at TIMESTAMP, "
                "CONSTRAINT uq_category_parent_value UNIQUE (parent_id, value))"
            )
        )


def test_init_db_is_idempotent_on_a_fresh_database(tmp_path):
    engine = get_engine(tmp_path / "fresh.db")
    init_db(engine)
    init_db(engine)  # must not raise the second time either

    Session = get_sessionmaker(engine)
    with Session() as session:
        session.add(Category(value="Food"))
        session.commit()
        session.add(Category(value="Food"))
        with pytest.raises(Exception, match="UNIQUE"):
            session.commit()


def test_init_db_adds_the_unique_index_to_a_clean_legacy_table(tmp_path):
    """No duplicates yet, so the migration should just quietly add the constraint."""
    engine = get_engine(tmp_path / "legacy_clean.db")
    _legacy_category_table(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO category (value) VALUES ('Food')"))
        connection.execute(
            text("INSERT INTO category (parent_id, value) VALUES (1, 'Dining')")
        )

    init_db(engine)  # must not raise

    Session = get_sessionmaker(engine)
    with Session() as session:
        session.add(Category(value="Dining"))  # already used, now at the top level too
        with pytest.raises(Exception, match="UNIQUE"):
            session.commit()


def test_init_db_raises_a_clear_error_naming_the_duplicates(tmp_path):
    engine = get_engine(tmp_path / "legacy_dupes.db")
    _legacy_category_table(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO category (value) VALUES ('Travel')"))
        connection.execute(
            text("INSERT INTO category (parent_id, value) VALUES (1, 'Airfare')")
        )
        # The bug from the brief: a second, top-level "Airfare" alongside the nested one.
        connection.execute(text("INSERT INTO category (value) VALUES ('Airfare')"))

    with pytest.raises(DuplicateCategoryNamesError, match="Airfare"):
        init_db(engine)


def test_init_db_does_not_silently_merge_duplicates(tmp_path):
    """Opening the database must not be destructive on its own; merging is a separate,
    explicit call (categories.merge_category)."""
    engine = get_engine(tmp_path / "legacy_dupes2.db")
    _legacy_category_table(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO category (value) VALUES ('Airfare')"))
        connection.execute(text("INSERT INTO category (value) VALUES ('Airfare')"))

    with pytest.raises(DuplicateCategoryNamesError):
        init_db(engine)

    with engine.begin() as connection:
        count = connection.execute(
            text("SELECT COUNT(*) FROM category WHERE value = 'Airfare'")
        ).scalar()
    assert count == 2  # untouched


def test_merging_the_duplicates_unblocks_init_db(tmp_path):
    engine = get_engine(tmp_path / "fixed.db")
    _legacy_category_table(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO category (value) VALUES ('Travel')"))
        connection.execute(
            text("INSERT INTO category (parent_id, value) VALUES (1, 'Airfare')")
        )
        connection.execute(text("INSERT INTO category (value) VALUES ('Airfare')"))

    with pytest.raises(DuplicateCategoryNamesError):
        init_db(engine)

    # Everything else (account, currency, transactions...) still needs to exist for the
    # ORM to work, so create_all runs directly here rather than through init_db, which
    # would immediately re-hit the same duplicate check.
    Base.metadata.create_all(engine)
    from budget_tracker import categories

    Session = get_sessionmaker(engine)
    with Session() as session:
        categories.merge_category(session, "Airfare", "Travel > Airfare")
        session.commit()

    init_db(engine)  # now succeeds

    with Session() as session:
        session.add(Category(value="Airfare"))  # the constraint is live
        with pytest.raises(Exception, match="UNIQUE"):
            session.commit()
