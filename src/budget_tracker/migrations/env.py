"""Alembic environment script.

Never invoked by hand -- see the package docstring in ``__init__.py`` for how
``budget_tracker.migrations.alembic_config`` reaches this without a CWD
``alembic.ini``. Kept close to Alembic's generic template so ``alembic``'s own docs
and examples still apply; the one deliberate departure is preferring a connectable
already passed in over opening one from a URL, so ``db.init_db`` can run migrations
against the exact engine it was given.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection, Engine

from budget_tracker.models import Base

target_metadata = Base.metadata

config = context.config


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite can't ALTER COLUMN (or drop/rename one) in place; batch mode copies
        # the table to a new one with the change applied, as `ALTER TABLE` would do
        # on a database that supported it directly.
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connectable")
    if connectable is None:
        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )

    if isinstance(connectable, Engine):
        with connectable.connect() as connection:
            _run_migrations(connection)
    else:
        # Already a live Connection -- reuse it rather than opening a second one.
        _run_migrations(connectable)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
