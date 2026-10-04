"""The ``tags`` command: list, rename, or delete tags and trips.

Follows ``categories.py``'s convention: :mod:`.tags` itself never commits, so every
write here owns its own ``session.commit()`` (or, for an unconfirmed ``delete``, skips
it and lets the session close roll the delete back -- the same trick ``category
merge`` uses to preview a real result without keeping it).

There is deliberately no CLI command to put a tag on a transaction: the CLI has no way
to select individual transactions. That is the TUI's multi-select.
"""

from __future__ import annotations

import argparse

from .. import queries
from .. import tags as tags_module
from ..db import get_engine, get_sessionmaker, init_db
from ..tui.formatting import _fmt_amount


def _cmd_tags(args: argparse.Namespace) -> int:
    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)

    # argparse's subparser action defaults the dest to None, which wins over the
    # parser-level default, so a bare `budget tags` arrives with nothing set.
    command = args.tags_command or "list"

    with session_factory() as session:
        if command == "rename":
            kind = args.kind or tags_module.TAG
            if not tags_module.rename_tag(session, args.old, args.new, kind):
                print(f"No {kind} named {args.old!r}.")
                return 1
            session.commit()
            print(f"Renamed {args.old!r} -> {args.new!r}.")
            return 0

        if command == "delete":
            kind = args.kind or tags_module.TAG
            if tags_module.resolve(session, args.name, kind) is None:
                print(f"No {kind} named {args.name!r}.")
                return 1
            changed = tags_module.delete_tag(session, args.name, kind)
            if not args.yes:
                print(
                    f"Would remove {args.name!r} from {changed} transaction(s), "
                    "then delete it."
                )
                print("Re-run with --yes to actually do it.")
                return 1
            session.commit()
            print(f"Deleted {args.name!r}; removed from {changed} transaction(s).")
            return 0

        rows = queries.get_tags(session, kind=args.kind)  # "list"

    if not rows:
        print("No tags yet.")
        return 0

    from rich.console import Console
    from rich.table import Table

    table = Table(box=None, pad_edge=False)
    table.add_column("Name")
    table.add_column("Kind")
    table.add_column("Txns", justify="right")
    table.add_column("Total", justify="right")
    for row in rows:
        table.add_row(row.name, row.kind, f"{row.count} txns", _fmt_amount(row.total_minor))
    Console().print(table)
    return 0
