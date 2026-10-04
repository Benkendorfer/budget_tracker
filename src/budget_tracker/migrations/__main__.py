"""``python -m budget_tracker.migrations new "message"``.

See the package docstring in ``__init__.py`` for what this does and why a scratch
database is needed to autogenerate against.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import List, Optional

from alembic import command
from sqlalchemy import create_engine

from . import alembic_config


def new(message: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        engine = create_engine(f"sqlite:///{Path(tmp) / 'scratch.db'}", future=True)
        config = alembic_config(engine)
        command.upgrade(config, "head")
        command.revision(config, message=message, autogenerate=True)


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] != "new":
        print('usage: python -m budget_tracker.migrations new "message"', file=sys.stderr)
        return 1
    new(argv[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
