"""The ``categorize``, ``category``, and ``category-rule`` commands.

``categorize``/``category-rule`` assign an existing category to transactions;
``category`` builds the category hierarchy itself (see ``_cmd_category``'s docstring).
"""

from __future__ import annotations

import argparse

from .. import categories as categories_module
from .. import queries
from ..db import get_engine, get_sessionmaker, init_db


def _cmd_categorize(args: argparse.Namespace) -> int:
    from .. import categories

    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        # Both calls return 0 for an unknown vendor and for one with nothing to change,
        # so the vendor is resolved up front to keep those answers apart.
        if queries.resolve_vendor_filter(session, args.vendor) is None:
            print(f"No vendor named {args.vendor!r}.")
            return 1
        if args.clear:
            changed = categories.clear_category(session, args.vendor)
            session.commit()
            print(f"Cleared the category on {changed} transaction(s) of {args.vendor!r}.")
            return 0
        if not args.category:
            print("A category is required, unless you pass --clear.")
            return 1
        changed = categories.set_category(session, args.vendor, args.category)
        session.commit()
        print(
            f"Categorised {changed} transaction(s) of {args.vendor!r} "
            f"as {args.category!r}."
        )
        return 0


def _format_relocation_preview(preview: "categories_module.PathPreview") -> str:
    return "; ".join(
        f"{r.name!r} from {r.from_parent or 'the top level'} to "
        f"{r.to_parent or 'the top level'} ({r.transaction_count} transaction(s))"
        for r in preview.relocations
    )


def _cmd_category(args: argparse.Namespace) -> int:
    """Build/inspect the category hierarchy itself — not which category a vendor gets.

    Distinct from ``categorize``/``category-rule``, which assign an existing category
    to transactions.
    """
    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)

    # argparse's subparser action defaults the dest to None, which wins over the
    # parser-level default, so a bare `budget category` arrives with nothing set.
    command = args.category_command or "list"

    with session_factory() as session:
        if command == "add":
            # Names are unique across the whole tree, so a level that already exists
            # somewhere else is a relocation of that whole category, not a new one —
            # refused unconfirmed (see categories.ensure_path).
            try:
                preview = categories_module.preview_path(session, args.path)
            except categories_module.CategoryError as error:
                print(error)
                return 1
            if preview.relocations and not args.yes:
                print(f"Would relocate {_format_relocation_preview(preview)}.")
                print(
                    "A separate category needs its own distinct name, e.g. "
                    "'Dining (Travel)'."
                )
                print("Re-run with --yes to actually move it.")
                return 1
            try:
                category = categories_module.ensure_path(
                    session, args.path, confirm_relocation=args.yes
                )
            except categories_module.CategoryError as error:
                print(error)
                return 1
            path = categories_module.format_path(session, category)
            session.commit()
            print(f"{path!r} ready.")
            return 0

        if command == "merge":
            # merge_category has no dry-run of its own; without --yes it is called
            # inside this session anyway (so its MergeResult names the real counts),
            # then simply never committed — closing the session below rolls it back.
            try:
                result = categories_module.merge_category(session, args.source, args.target)
            except categories_module.CategoryError as error:
                print(error)
                return 1
            plural = "y" if result.moved_children == 1 else "ies"
            summary = (
                f"{result.moved_transactions} transaction(s), "
                f"{result.moved_rules} rule(s), "
                f"{result.moved_children} child categor{plural} moved"
            )
            if not args.yes:
                print(
                    f"Would merge {result.source!r} into {result.target!r}: {summary}, "
                    f"then {result.source!r} deleted."
                )
                print("Re-run with --yes to actually do it.")
                return 1
            session.commit()
            print(f"Merged {result.source!r} into {result.target!r}: {summary}.")
            return 0

        rows = queries.get_categories(session)  # "list"

    if not rows:
        print(
            "No categories yet. Add one with: "
            "budget category add 'Food > Dining > Restaurants'"
        )
        return 0
    for row in rows:
        print(f"  {'  ' * row.depth}{row.name} ({row.count})")
    return 0


def _cmd_category_rule(args: argparse.Namespace) -> int:
    from .. import categories

    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)

    # argparse's subparser action defaults the dest to None, which wins over the
    # parser-level default, so a bare `budget category-rule` arrives with nothing set.
    command = args.category_rule_command or "list"

    with session_factory() as session:
        if command == "list":
            rules = categories.list_rules(session)
            if not rules:
                print("No category rules defined.")
                return 0
            width = max(len(r.pattern) for r in rules)
            for rule in rules:
                print(f"  {rule.pattern:<{width}}  ->  {rule.category.value}")
            return 0

        if command == "add":
            categories.add_rule(session, args.pattern, args.category)
            changed = categories.apply_category_rules(session)
            session.commit()
            print(
                f"Rule {args.pattern!r} -> {args.category!r}; "
                f"{changed} transactions updated."
            )
            return 0

        if command == "remove":
            if not categories.remove_rule(session, args.pattern):
                print(f"No category rule with pattern {args.pattern!r}.")
                return 1
            changed = categories.apply_category_rules(session)
            session.commit()
            print(f"Removed {args.pattern!r}; {changed} transactions updated.")
            return 0

        changed = categories.apply_category_rules(session)  # "apply"
        session.commit()
        print(
            f"Applied {len(categories.list_rules(session))} rules; "
            f"{changed} transactions updated."
        )
        return 0
