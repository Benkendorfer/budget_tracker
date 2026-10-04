"""Tests for the Alembic-backed migration path in :mod:`budget_tracker.db`.

``test_db.py`` already covers the category-uniqueness behavior in detail (it must
be preserved exactly); this file is about the three ways ``init_db`` can find a
database -- brand new, pre-Alembic, already versioned -- and that the baseline
revision really is the same schema ``models.py`` describes.
"""

from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Column, Integer, Table, inspect, text

from budget_tracker.db import DuplicateCategoryNamesError, get_engine, get_sessionmaker, init_db
from budget_tracker.migrations import BASELINE_REVISION, MIGRATIONS_DIR, alembic_config
from budget_tracker.models import Base, Category


def _version(engine):
    with engine.begin() as connection:
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _legacy_database(engine):
    """A database shaped like one from before every ``_ADDED_COLUMNS`` patch and the
    category unique-index migration existed -- enough tables to exercise several
    patches at once, each holding a row so data-preservation can be checked after.
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE category (id INTEGER PRIMARY KEY, parent_id INTEGER, "
                "value VARCHAR, created_at TIMESTAMP, updated_at TIMESTAMP, "
                "CONSTRAINT uq_category_parent_value UNIQUE (parent_id, value))"
            )
        )
        connection.execute(text("INSERT INTO category (value) VALUES ('Food')"))

        connection.execute(
            text("CREATE TABLE vendor (id INTEGER PRIMARY KEY, name VARCHAR UNIQUE)")
        )
        connection.execute(text("INSERT INTO vendor (name) VALUES ('COFFEE SHOP A')"))

        connection.execute(
            text(
                "CREATE TABLE tag (id INTEGER PRIMARY KEY, name VARCHAR, "
                "kind VARCHAR, created_at TIMESTAMP)"
            )
        )
        connection.execute(text("INSERT INTO tag (name, kind) VALUES ('Japan', 'trip')"))

        connection.execute(
            text(
                "CREATE TABLE csv_format (id INTEGER PRIMARY KEY, name VARCHAR UNIQUE, "
                "signature VARCHAR, posted_date_column VARCHAR, description_column VARCHAR, "
                "date_formats VARCHAR, amount_style VARCHAR, dedup_columns VARCHAR, "
                "txn_date_column VARCHAR, category_column VARCHAR, debit_column VARCHAR, "
                "credit_column VARCHAR, amount_column VARCHAR, account_column VARCHAR, "
                "account_prefix VARCHAR, created_at TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO csv_format "
                "(name, signature, posted_date_column, description_column, date_formats, "
                "amount_style, dedup_columns, account_prefix) "
                "VALUES ('old_layout', '[]', 'Posted', 'Description', '[]', 'single', '[]', '')"
            )
        )

        connection.execute(
            text(
                "CREATE TABLE import (id INTEGER PRIMARY KEY, account_id INTEGER, "
                "source_file VARCHAR, row_count INTEGER, imported_at TIMESTAMP)"
            )
        )
        connection.execute(
            text("INSERT INTO import (source_file, row_count) VALUES ('old.csv', 3)")
        )


def test_fresh_database_is_created_at_head(tmp_path):
    engine = get_engine(tmp_path / "fresh.db")
    init_db(engine)

    assert _version(engine) == BASELINE_REVISION  # the only revision today

    Session = get_sessionmaker(engine)
    with Session() as session:
        session.add(Category(value="Food"))
        session.commit()
        session.add(Category(value="Food"))
        with pytest.raises(Exception, match="UNIQUE"):
            session.commit()


def test_fresh_database_init_db_is_idempotent(tmp_path):
    engine = get_engine(tmp_path / "fresh2.db")
    init_db(engine)
    init_db(engine)  # already versioned; must take the upgrade-head branch and no-op

    assert _version(engine) == BASELINE_REVISION


def test_unversioned_database_migrates_data_intact_and_gets_stamped(tmp_path):
    engine = get_engine(tmp_path / "legacy.db")
    _legacy_database(engine)

    init_db(engine)  # must not raise

    assert _version(engine) == BASELINE_REVISION

    columns = {c["name"] for c in inspect(engine).get_columns("vendor")}
    assert "vendor_name_source" in columns

    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT name FROM vendor WHERE id = 1")
        ).scalar() == "COFFEE SHOP A"
        assert connection.execute(
            text("SELECT value FROM category WHERE id = 1")
        ).scalar() == "Food"
        assert connection.execute(
            text("SELECT name FROM tag WHERE id = 1")
        ).scalar() == "Japan"
        assert connection.execute(
            text("SELECT name FROM csv_format WHERE id = 1")
        ).scalar() == "old_layout"
        assert connection.execute(
            text("SELECT sync_connection_id FROM import WHERE id = 1")
        ).scalar() is None

        tables = {
            row[0]
            for row in connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
        assert {"sync_connection", "sync_account"} <= tables


def test_unversioned_database_second_init_db_is_a_no_op(tmp_path):
    engine = get_engine(tmp_path / "legacy_twice.db")
    _legacy_database(engine)

    init_db(engine)
    version_after_first = _version(engine)
    init_db(engine)  # now versioned; must take the upgrade-head branch, not re-migrate

    assert _version(engine) == version_after_first == BASELINE_REVISION


def test_unversioned_database_with_duplicate_category_names_is_still_refused(tmp_path):
    engine = get_engine(tmp_path / "legacy_dupes.db")
    _legacy_database(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO category (value) VALUES ('Food')"))  # dupe

    with pytest.raises(DuplicateCategoryNamesError, match="Food"):
        init_db(engine)

    # Refused, so never stamped -- the next init_db call retries the same migration
    # rather than skipping it as already done.
    with engine.begin() as connection:
        tables = {
            row[0]
            for row in connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
    assert "alembic_version" not in tables


def test_already_versioned_database_upgrades_to_head_as_a_no_op(tmp_path):
    engine = get_engine(tmp_path / "versioned.db")
    Base.metadata.create_all(engine)
    command.stamp(alembic_config(engine), BASELINE_REVISION)

    init_db(engine)  # must not raise, and must not touch anything since already at head

    assert _version(engine) == BASELINE_REVISION


def test_head_schema_matches_models_exactly(tmp_path):
    """The baseline revision must produce exactly what ``models.py`` describes --
    not a close approximation that happens to pass today's tests.
    """
    engine = get_engine(tmp_path / "compare.db")
    init_db(engine)

    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        diffs = compare_metadata(context, Base.metadata)

    assert diffs == []


def test_replaying_the_migration_from_empty_matches_models_exactly(tmp_path):
    """``init_db`` on an empty database takes the ``create_all`` fast path, which
    would pass even if the baseline migration script itself were wrong (say, if it
    went back to calling ``create_all`` against current models -- the bug this whole
    file guards against). This instead runs the actual migration machinery -- empty
    database, ``alembic upgrade head``, no fast path -- so a frozen baseline that
    drifted from ``models.py`` would be caught here even though every real database
    takes a different branch in ``init_db``.
    """
    engine = get_engine(tmp_path / "replay.db")
    command.upgrade(alembic_config(engine), "head")

    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        diffs = compare_metadata(context, Base.metadata)

    assert diffs == []


def test_autogenerate_detects_a_table_added_after_the_baseline(tmp_path):
    """Regression test for a baseline that called ``Base.metadata.create_all()``
    against *current* models instead of a frozen snapshot: once models.py grows a
    table, that baseline would create it too, so (a) autogenerating a new migration
    against a database already at head would see no difference and emit an empty
    migration, and (b) any database replayed from empty would get the table from the
    baseline, and the real migration that was supposed to add it would then fail
    trying to create it a second time.

    Adds a throwaway table straight onto ``Base.metadata`` -- the same object
    ``env.py`` reads as ``target_metadata`` -- removed again in ``finally`` so it
    cannot leak into another test.
    """
    throwaway = Table(
        "throwaway_regression_table", Base.metadata, Column("id", Integer, primary_key=True)
    )
    try:
        engine = get_engine(tmp_path / "newtable.db")
        config = alembic_config(engine)
        command.upgrade(config, "head")

        # The frozen baseline must not have created it as a side effect of the table
        # now existing on Base.metadata.
        assert "throwaway_regression_table" not in inspect(engine).get_table_names()

        # Mirrors `python -m budget_tracker.migrations new` (see __main__.py), but
        # redirects the generated file to a tmp dir instead of the real versions/ --
        # version_path must be a configured version_locations entry, so the real
        # versions dir is kept too (read-only here) purely so "head" still resolves.
        out_dir = tmp_path / "generated"
        out_dir.mkdir()
        config.set_main_option("path_separator", "os")
        config.set_main_option(
            "version_locations", os.pathsep.join([str(MIGRATIONS_DIR / "versions"), str(out_dir)])
        )
        command.revision(
            config, message="add throwaway table", autogenerate=True, version_path=str(out_dir)
        )

        generated = list(out_dir.glob("*.py"))
        assert len(generated) == 1
        text_ = generated[0].read_text()
        assert "down_revision" in text_ and BASELINE_REVISION in text_
        assert "create_table('throwaway_regression_table'" in text_
    finally:
        Base.metadata.remove(throwaway)
