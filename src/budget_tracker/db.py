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


def get_engine(db_path: Optional[Path] = None) -> Engine:
    """Create an engine for the given SQLite path (env ``BUDGET_DB`` overrides)."""
    if db_path is not None:
        path = Path(db_path)
    elif os.environ.get("BUDGET_DB"):
        path = Path(os.environ["BUDGET_DB"])
    else:
        path = DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{path}", future=True)


# Columns added to existing tables after the first release. ``create_all`` only creates
# missing *tables*, so these are applied by hand; SQLite ADD COLUMN is cheap and safe.
_ADDED_COLUMNS = {
    "vendor": {"vendor_name_source": "VARCHAR"},
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


def init_db(engine: Engine) -> None:
    """Create any missing tables, then patch in any columns added since."""
    _add_missing_columns(engine)
    Base.metadata.create_all(engine)


def get_sessionmaker(engine: Engine) -> "sessionmaker[Session]":
    return sessionmaker(bind=engine, future=True)
