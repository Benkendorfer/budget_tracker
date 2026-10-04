"""Database engine, session, and schema setup.

Uses SQLite by default, stored in the gitignored ``data/`` directory. The
``sqlite:///`` URL keeps everything local while leaving room to switch to a
cloud Postgres URL later without touching the models.

Schema changes are versioned Alembic migrations under :mod:`budget_tracker.migrations`
(see that package's docstring for how to add one). ``_ADDED_COLUMNS`` and the two
functions below it are not that mechanism -- they are the one-time path that walks a
database from before Alembic existed up to the baseline revision; see
:func:`init_db`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from alembic import command
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .migrations import BASELINE_REVISION, alembic_config
from .models import Base

# db.py -> budget_tracker -> src -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = _REPO_ROOT / "data" / "budget.db"


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    """SQLite ignores foreign keys unless this pragma is set per connection."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def resolve_db_path(db_path: Optional[Path] = None) -> Path:
    """Which file an engine built the same way would actually open.

    Split out of :func:`get_engine` so that anything *reporting* the database — the
    CLI's "Database: …" line, say — resolves it the same way rather than naming
    ``DEFAULT_DB_PATH`` and being wrong whenever ``BUDGET_DB`` is set. Telling the user
    you wrote to one file while writing to another is worse than saying nothing.
    """
    if db_path is not None:
        return Path(db_path)
    if os.environ.get("BUDGET_DB"):
        return Path(os.environ["BUDGET_DB"])
    return DEFAULT_DB_PATH


def get_engine(db_path: Optional[Path] = None) -> Engine:
    """Create an engine for the given SQLite path (env ``BUDGET_DB`` overrides)."""
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{path}", future=True)


# Columns added to existing tables before Alembic replaced this file as the way to do
# it. ``create_all`` only creates missing *tables*, so these were applied by hand;
# SQLite ADD COLUMN is cheap and safe. Still needed, and still correct, to walk a
# pre-Alembic database up to the baseline revision -- see init_db.
_ADDED_COLUMNS = {
    "vendor": {"vendor_name_source": "VARCHAR"},
    # Manual overrides for a trip's dates, which otherwise come from the transactions
    # on it. NULL means "derive it", so an existing database needs no backfill: every
    # trip made before this column existed keeps behaving exactly as it did.
    "tag": {"start_date": "DATE", "end_date": "DATE"},
    "csv_format": {
        "invert_amount": "BOOLEAN NOT NULL DEFAULT 0",
        # A format that predates this column carried no currency of its own, and every
        # one defined before now was USD, so that is the correct value to backfill.
        "currency": "VARCHAR NOT NULL DEFAULT 'USD'",
    },
    # NULL on every pre-existing row, which is correct: nothing imported before sync
    # existed could have come from one. The referenced table (sync_connection) may not
    # exist yet on an old database -- this runs before create_all() -- but SQLite does
    # not validate a REFERENCES target at ALTER TABLE time, only at DML time, so adding
    # the column first and creating the table after is safe (see test_sync.py).
    "import": {"sync_connection_id": "INTEGER REFERENCES sync_connection(id)"},
    # NULL until the next sync records a status, which reads as "not yet known".
    "sync_account": {"last_status": "VARCHAR", "last_error": "VARCHAR"},
}


def _add_missing_columns(engine: Engine) -> None:
    """Idempotently add known-new columns to tables that predate them."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue  # create_all() just made it, already correct
            present = {c["name"] for c in inspector.get_columns(table)}
            for column, sql_type in columns.items():
                if column not in present:
                    connection.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
                    )


class DuplicateCategoryNamesError(RuntimeError):
    """Two or more categories share a name, so global uniqueness can't be enforced.

    Raised by :func:`init_db` when opening a pre-existing database that predates the
    unique-name constraint. Merge the named duplicates with
    :func:`categories.merge_category` (which repoints their transactions, rules, and
    children before deleting the loser) and open the database again.
    """


def _ensure_unique_category_names(engine: Engine) -> None:
    """Add a unique index on ``category.value`` if it is not already unique.

    Called after ``create_all``, so the table always exists by now — either freshly
    made with the constraint already built into the model, or predating it. The old
    constraint was ``(parent_id, value)``, and SQLite treats NULLs in a unique index as
    distinct from one another, so it never actually constrained top-level categories
    either — a database opened before this migration can genuinely hold two rows with
    the same name. Adding the index straight away would fail with an opaque
    ``IntegrityError``, so duplicates are checked for and named first. ``CREATE UNIQUE
    INDEX IF NOT EXISTS`` makes the rest idempotent, and harmless to run again once a
    fresh ``create_all`` has already given the table this same constraint by name.
    """
    with engine.begin() as connection:
        duplicates = [
            row[0]
            for row in connection.execute(
                text("SELECT value FROM category GROUP BY value HAVING COUNT(*) > 1")
            )
        ]
        if duplicates:
            names = ", ".join(repr(v) for v in duplicates)
            raise DuplicateCategoryNamesError(
                f"Category name(s) used more than once: {names}. Category names must "
                "now be unique across the whole tree; merge each duplicate with "
                "categories.merge_category(session, source, target) before this "
                "database can be opened."
            )
        connection.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS uq_category_value ON category (value)")
        )


def _upgrade_to_head(engine: Engine) -> None:
    command.upgrade(alembic_config(engine), "head")


def _stamp(engine: Engine, revision: str) -> None:
    command.stamp(alembic_config(engine), revision)


def init_db(engine: Engine) -> None:
    """Bring the database to the current schema, versioned in ``alembic_version``.

    Three cases, told apart by what is already there:

    - **Brand new** (no tables at all): replaying every migration from nothing is
      just a slower way of doing what ``create_all`` does directly, so that is the
      fast path -- this is the common case in the test suite, called on a fresh
      ``tmp_path`` database hundreds of times. It is then stamped at head, so the
      *next* ``init_db`` call on it takes the "already versioned" branch below.
    - **Pre-Alembic** (tables exist, but no ``alembic_version`` table): every real
      database today, including the user's. The old patches --
      :func:`_add_missing_columns`, ``create_all`` for any table added since,
      :func:`_ensure_unique_category_names` -- are exactly how such a database
      reaches the baseline schema, so they still run verbatim, including
      :exc:`DuplicateCategoryNamesError` still blocking the database from opening at
      all until the duplicates are merged by hand. It is then stamped at the
      *baseline* revision, not head, and upgraded -- so a database that reaches the
      baseline today still picks up any migration added after it, exactly like a
      database that was already versioned.
    - **Versioned**: ``alembic upgrade head``. A no-op today (there is only the one,
      baseline revision); this is the branch every call takes once a database has
      been opened under this scheme at all.

    Nothing here recreates or drops a table that was not already empty, and the
    pre-Alembic branch never runs a migration script against real data -- only the
    same idempotent column/index patches it always ran.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    if "alembic_version" in existing_tables:
        _upgrade_to_head(engine)
        return

    if not existing_tables:
        Base.metadata.create_all(engine)
        _stamp(engine, "head")
        return

    _add_missing_columns(engine)
    Base.metadata.create_all(engine)
    _ensure_unique_category_names(engine)  # may raise DuplicateCategoryNamesError
    _stamp(engine, BASELINE_REVISION)
    _upgrade_to_head(engine)


def get_sessionmaker(engine: Engine) -> "sessionmaker[Session]":
    return sessionmaker(bind=engine, future=True)
