"""Alembic migrations for the schema in :mod:`budget_tracker.models`.

Replaces the old scheme of hand-written ``ALTER TABLE`` patches
(``db._ADDED_COLUMNS``) re-checked on every start. New tables need nothing beyond
``create_all``, as before; a new *column* on an existing table, or anything
structural (new tables with foreign keys to backfill, data migrations, dropped
columns), now gets a real, ordered, versioned migration under ``versions/``.

There is a single baseline revision, ``0001_baseline`` (see
``versions/0001_baseline_full_schema.py``), equal to the complete schema in
``models.py`` the day Alembic was introduced -- i.e. everything the old
``_ADDED_COLUMNS`` patches and the category unique-index migration already added. See
:func:`budget_tracker.db.init_db` for how the three kinds of database it has to open
(brand new, pre-Alembic, already-versioned) each reach this revision or later.

Adding a migration
-------------------
After changing ``models.py``, generate the diff against a scratch database that is
already at head::

    PYTHONPATH=src .venv/bin/python -m budget_tracker.migrations new "add monthly_budget table"

This builds a temporary SQLite database, upgrades it to the current head, and asks
Alembic's autogenerate to diff it against ``Base.metadata`` -- the result lands as a
new file in ``versions/``. Autogenerate misses some things (server-side defaults,
check constraints, data backfills), so read the generated file before trusting it,
same as you would a hand-written one.

Running migrations
-------------------
There is no ``alembic.ini`` and nothing here depends on the current working
directory -- :func:`alembic_config` builds the :class:`alembic.config.Config`
programmatically, pointed at this package's own directory on disk, so it works the
same from a test's ``tmp_path``, an installed ``budget`` console script, or a
checkout. ``db.init_db`` is the only normal caller.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from alembic.config import Config
from sqlalchemy.engine import Connectable

MIGRATIONS_DIR = Path(__file__).resolve().parent

# The first revision, matching versions/0001_baseline_full_schema.py's `revision =`.
# A database that predates Alembic is walked to exactly this state by db.py's legacy
# column/index patches before being stamped here -- see init_db's docstring.
BASELINE_REVISION = "0001_baseline"


def alembic_config(connectable: Optional[Connectable] = None) -> Config:
    """Build a :class:`Config` that needs no on-disk ``alembic.ini``.

    Passing an open ``Engine`` or ``Connection`` makes migrations run against it
    directly instead of opening a second connection from a URL (see ``env.py``) --
    ``db.py`` always passes one, since it already holds the engine ``init_db`` was
    given. ``command.stamp``/``command.upgrade`` still require *some*
    ``sqlalchemy.url`` to be set even then; it is never actually read in that case.
    """
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", "sqlite://")
    if connectable is not None:
        config.attributes["connectable"] = connectable
    return config
