"""The ``rename`` and ``rule`` commands: vendor display names and rename rules."""

from __future__ import annotations

import argparse

from ..db import get_engine, get_sessionmaker, init_db


def _recategorize(session) -> None:
    """Re-run the category rules and commit. A rename changes display names, and a
    category rule may be written against one -- the same order an import uses."""
    from .. import categories

    categories.apply_category_rules(session)
    session.commit()


def _cmd_rename(args: argparse.Namespace) -> int:
    from .. import vendors

    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        ok = vendors.set_override(session, args.raw, args.display)
        if ok:
            _recategorize(session)
    if not ok:
        print(f"No vendor named {args.raw!r}.")
        return 1
    print(f"Renamed {args.raw!r} -> {args.display!r}.")
    return 0


def _cmd_rule(args: argparse.Namespace) -> int:
    from .. import vendors

    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)

    with session_factory() as session:
        if args.rule_command == "list":
            rules = vendors.list_rules(session)
            if not rules:
                print("No vendor rules defined.")
                return 0

            from rich.console import Console
            from rich.table import Table

            table = Table(box=None, pad_edge=False)
            table.add_column("Pattern")
            table.add_column("Vendor")
            for rule in rules:
                table.add_row(rule.pattern, rule.vendor_name.value)
            Console().print(table)
            return 0

        if args.rule_command == "add":
            vendors.add_rule(session, args.pattern, args.display)
            changed = vendors.apply_rules(session)
            _recategorize(session)
            print(f"Rule {args.pattern!r} -> {args.display!r}; {changed} vendors updated.")
            return 0

        if args.rule_command == "remove":
            if not vendors.remove_rule(session, args.pattern):
                print(f"No rule with pattern {args.pattern!r}.")
                return 1
            changed = vendors.apply_rules(session)
            _recategorize(session)
            print(f"Removed {args.pattern!r}; {changed} vendors updated.")
            return 0

        changed = vendors.apply_rules(session)  # "apply"
        _recategorize(session)
        print(f"Applied {len(vendors.list_rules(session))} rules; {changed} vendors updated.")
        return 0
