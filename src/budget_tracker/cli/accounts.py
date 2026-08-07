"""The ``account`` command: list, rename, or merge accounts."""

from __future__ import annotations

import argparse

from .. import queries
from ..db import get_engine, get_sessionmaker, init_db


def _cmd_account(args: argparse.Namespace) -> int:
    from .. import accounts

    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        try:
            if args.account_command == "rename":
                accounts.rename_account(session, args.old, args.new)
                session.commit()
                print(f"Renamed {args.old!r} -> {args.new!r}.")
                return 0
            if args.account_command == "merge":
                result = accounts.merge_accounts(session, args.source, args.target)
                session.commit()
                print(
                    f"Merged {result.source!r} into {result.target!r}: "
                    f"{result.moved_transactions} transactions, "
                    f"{result.moved_imports} import record(s) moved."
                )
                if result.unpaired_transfers:
                    print(
                        f"{result.unpaired_transfers} transaction(s) were un-paired: "
                        "both legs are now in the same account."
                    )
                return 0
        except accounts.AccountError as error:
            print(error)
            return 1
        rows = queries.get_accounts(session)  # "list"

    if not rows:
        print("No accounts yet.")
        return 0
    width = max(len(r.name) for r in rows)
    for row in rows:
        print(f"  {row.name:<{width}}  {row.count:>6} txns  {row.currency}")
    return 0
