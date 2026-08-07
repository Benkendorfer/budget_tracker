"""Database engine, session, and schema setup.

Uses SQLite by default, stored in the gitignored ``data/`` directory. The
``sqlite:///`` URL keeps everything local while leaving room to switch to a
cloud Postgres URL later without touching the models.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

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


# Columns added to existing tables after the first release. ``create_all`` only creates
# missing *tables*, so these are applied by hand; SQLite ADD COLUMN is cheap and safe.
_ADDED_COLUMNS = {
    "vendor": {"vendor_name_source": "VARCHAR"},
    "csv_format": {
        "invert_amount": "BOOLEAN NOT NULL DEFAULT 0",
        # A format that predates this column carried no currency of its own, and every
        # one defined before now was USD, so that is the correct value to backfill.
        "currency": "VARCHAR NOT NULL DEFAULT 'USD'",
    },
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


def init_db(engine: Engine) -> None:
    """Create any missing tables, then patch in any columns/constraints added since."""
    _add_missing_columns(engine)
    Base.metadata.create_all(engine)
    _ensure_unique_category_names(engine)


def get_sessionmaker(engine: Engine) -> "sessionmaker[Session]":
    return sessionmaker(bind=engine, future=True)
