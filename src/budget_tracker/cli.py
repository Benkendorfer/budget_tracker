"""Command-line interface for the budget tracker."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import List, Optional

from . import formats
from .db import DEFAULT_DB_PATH, get_engine, get_sessionmaker, init_db
from .importer import DEFAULT_CURRENCY_CODE, import_csv, read_header_and_rows

# cli.py -> budget_tracker -> src -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parents[2]
TO_IMPORT_DIR = _REPO_ROOT / "data" / "to_import"


def _select_csv_interactively() -> Optional[Path]:
    csv_files = sorted(TO_IMPORT_DIR.glob("*.csv"))
    if not csv_files:
        print(f"No CSV files found in {TO_IMPORT_DIR}")
        return None
    print("Select a CSV file to import:")
    for index, path in enumerate(csv_files, start=1):
        print(f"  [{index}] {path.name}")
    choice = input(f"Enter number (1-{len(csv_files)}) or q to quit: ").strip()
    if choice.lower() in {"q", "quit", ""}:
        return None
    try:
        index = int(choice)
        if not 1 <= index <= len(csv_files):
            raise ValueError
    except ValueError:
        print("Invalid selection.")
        return None
    return csv_files[index - 1]


def _resolve_format(session, path: Path):
    """Detect the file's format, falling back to interactive setup."""
    fieldnames, rows = read_header_and_rows(path)
    try:
        return formats.detect(session, fieldnames)
    except formats.UnknownFormat:
        return _setup_format(session, path, fieldnames, rows)


def _cmd_import(args: argparse.Namespace) -> int:
    if args.path:
        path = Path(args.path).expanduser()
    else:
        path = _select_csv_interactively()
    if path is None:
        return 1
    if not path.is_file():
        print(f"File not found: {path}")
        return 1

    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    try:
        with session_factory() as session:
            fmt = formats.get_format(session, args.format) if args.format else None
            if fmt is None:
                fmt = _resolve_format(session, path)
                if fmt is None:
                    return 1
            result = import_csv(
                session,
                path,
                currency_code=args.currency,
                account_name=args.account,
                fmt=fmt,
            )
    except (formats.AccountRequired, formats.UnknownFormat) as error:
        print(error)
        return 1

    print(
        f"Imported '{result.source_file}': {result.inserted} added, "
        f"{result.skipped_duplicates} duplicates skipped "
        f"({result.total_rows} rows total)."
    )
    print(f"Database: {DEFAULT_DB_PATH}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    from rich.console import Console
    from rich.table import Table

    from . import queries

    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        account_id = None
        if args.account:
            account_id = queries.resolve_account(session, args.account)
            if account_id is None:
                print(f"No account named {args.account!r}.")
                return 1
        category_id = None
        if args.category:
            category_id = queries.resolve_category(session, args.category)
            if category_id is None:
                print(f"No category named {args.category!r}.")
                return 1
        vendor_filter = None
        if args.vendor:
            vendor_filter = queries.resolve_vendor_filter(session, args.vendor)
            if vendor_filter is None:
                print(f"No vendor named {args.vendor!r}.")
                return 1
        txns = queries.get_transactions(
            session, account_id, category_id, vendor_filter, limit=args.limit
        )
        totals = queries.get_totals(session, account_id, category_id, vendor_filter)

    console = Console()
    table = Table(box=None, pad_edge=False)
    table.add_column("Date")
    table.add_column("Description")
    table.add_column("Vendor")
    table.add_column("Category")
    table.add_column("Amount", justify="right")
    for txn in txns:
        style = "red" if txn.amount_minor < 0 else "green"
        table.add_row(
            txn.posted_date,
            txn.description,
            txn.vendor,
            txn.category,
            f"[{style}]{txn.amount_minor / 100:,.2f}[/{style}]",
        )
    console.print(table)
    console.print(
        f"[bold]{totals.count} txns[/bold]   "
        f"net {totals.net_minor / 100:,.2f}   "
        f"out {totals.outflow_minor / 100:,.2f}   "
        f"in {totals.inflow_minor / 100:,.2f}"
    )
    return 0


def _cmd_rename(args: argparse.Namespace) -> int:
    from . import vendors

    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        ok = vendors.set_override(session, args.raw, args.display)
    if not ok:
        print(f"No vendor named {args.raw!r}.")
        return 1
    print(f"Renamed {args.raw!r} -> {args.display!r}.")
    return 0


def _cmd_rule(args: argparse.Namespace) -> int:
    from . import vendors

    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)

    with session_factory() as session:
        if args.rule_command == "list":
            rules = vendors.list_rules(session)
            if not rules:
                print("No vendor rules defined.")
                return 0
            width = max(len(r.pattern) for r in rules)
            for rule in rules:
                print(f"  {rule.pattern:<{width}}  ->  {rule.vendor_name.value}")
            return 0

        if args.rule_command == "add":
            vendors.add_rule(session, args.pattern, args.display)
            changed = vendors.apply_rules(session)
            session.commit()
            print(f"Rule {args.pattern!r} -> {args.display!r}; {changed} vendors updated.")
            return 0

        if args.rule_command == "remove":
            if not vendors.remove_rule(session, args.pattern):
                print(f"No rule with pattern {args.pattern!r}.")
                return 1
            changed = vendors.apply_rules(session)
            session.commit()
            print(f"Removed {args.pattern!r}; {changed} vendors updated.")
            return 0

        changed = vendors.apply_rules(session)  # "apply"
        session.commit()
        print(f"Applied {len(vendors.list_rules(session))} rules; {changed} vendors updated.")
        return 0


def _ask(question: formats.Question) -> str:
    """Put one unresolved mapping question to the user."""
    print()
    print(f"  {question.prompt}")
    if question.choices:
        for index, choice in enumerate(question.choices, start=1):
            print(f"    [{index}] {choice}")
    suffix = f" [{question.default}]" if question.default else ""
    answer = input(f"  Answer{suffix}: ").strip()
    if not answer and question.default:
        return question.default
    if question.choices and answer.isdigit():
        index = int(answer)
        if 1 <= index <= len(question.choices):
            return question.choices[index - 1]
    return answer


def _setup_format(session, path: Path, fieldnames, rows) -> Optional[object]:
    """Walk the user through defining a format for an unrecognised CSV."""
    print(f"'{path.name}' does not match any format you have defined yet.")
    default_name = re.sub(r"[^a-z0-9]+", "_", path.stem.lower()).strip("_") or "format"
    name = input(f"Name for this layout [{default_name}]: ").strip() or default_name

    inference = formats.infer(name, fieldnames, rows)
    resolved = {k: v for k, v in inference.values.items() if v}
    print("\nWorked out from the header:")
    for field in (
        "posted_date_column",
        "txn_date_column",
        "description_column",
        "category_column",
        "account_column",
        "amount_column",
        "debit_column",
        "credit_column",
        "date_formats",
    ):
        if resolved.get(field):
            print(f"  {field:<20} {resolved[field]}")
    if not resolved.get("account_column"):
        print("  (no account column — imports of this layout will need --account)")

    # Answering one question can expose another — naming the date column is what makes
    # the date format checkable — so keep asking until nothing is left.
    values = inference.values
    questions = inference.questions
    for _ in range(4):
        if not questions:
            break
        answers = {q.field: _ask(q) for q in questions}
        values = formats.apply_answers(values, answers, fieldnames, rows)
        questions = formats.remaining_questions(values, rows, fieldnames)
    else:
        print("\nStill missing: " + ", ".join(q.field for q in questions))
        return None

    # The column gives an identifier like "8207"; only you know it should read
    # "Card 8207". Getting this wrong would create a second account for the same card.
    account_column = values.get("account_column")
    if account_column:
        sample = next(
            ((row.get(account_column) or "").strip() for row in rows if row.get(account_column)),
            "1234",
        )
        print()
        print(f"  Accounts will be named after {account_column!r}, e.g. {sample!r}.")
        prefix = input("  Prefix for those names, if any [none]: ").strip()
        if prefix:
            values["account_prefix"] = prefix + " " if not prefix.endswith(" ") else prefix
            print(f"  Accounts will be named e.g. {values['account_prefix']}{sample}")

    try:
        spec = formats.spec_from_values(values)
    except formats.InvalidFormat as error:
        print(f"\nCould not build a usable format: {error}")
        return None
    formats.save_format(session, spec)
    session.commit()
    print(f"\nSaved layout {spec.name!r}. Future imports of this shape are automatic.")
    return spec


def _cmd_format(args: argparse.Namespace) -> int:
    import json

    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)

    with session_factory() as session:
        try:
            if args.format_command == "remove":
                if not formats.remove_format(session, args.name):
                    print(f"No format named {args.name!r}.")
                    return 1
                session.commit()
                print(f"Removed format {args.name!r}.")
                return 0

            if args.format_command == "export":
                specs = (
                    [formats.get_format(session, args.name)]
                    if args.name
                    else formats.list_formats(session)
                )
                payload = [formats.to_dict(s) for s in specs]
                text = json.dumps(payload[0] if args.name else payload, indent=2)
                if args.output:
                    Path(args.output).expanduser().write_text(text, encoding="utf-8")
                    print(f"Wrote {len(specs)} format(s) to {args.output}.")
                else:
                    print(text)
                return 0

            specs = formats.list_formats(session)  # "list"
        except (formats.InvalidFormat, formats.UnknownFormat) as error:
            print(error)
            return 1

    if not specs:
        print(
            "No CSV layouts learned yet. Run 'budget import <file>' and you will be "
            "walked through the first one."
        )
        return 0
    width = max(len(s.name) for s in specs)
    for spec in specs:
        account = spec.account_column or "requires --account"
        print(f"  {spec.name:<{width}}  {spec.amount_style:<13}  account: {account}")
    return 0


def _cmd_tui(args: argparse.Namespace) -> int:
    # Imported lazily so plain `import` runs without pulling in Textual.
    from .tui import run

    run()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="budget_tracker", description="Personal budget tracker."
    )
    # No subcommand launches the interactive TUI.
    parser.set_defaults(func=_cmd_tui)
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser(
        "tui", help="Launch the interactive full-screen app (default)."
    ).set_defaults(func=_cmd_tui)

    import_parser = subparsers.add_parser(
        "import", help="Import a bank / credit-card CSV export."
    )
    import_parser.add_argument(
        "path",
        nargs="?",
        help="Path to the CSV file. If omitted, choose from data/to_import/.",
    )
    import_parser.add_argument(
        "--currency",
        default=DEFAULT_CURRENCY_CODE,
        help="ISO currency code for the transactions (default: %(default)s).",
    )
    import_parser.add_argument(
        "--account",
        help=(
            "Account these transactions belong to. Required for exports that carry no "
            "account column; overrides the derived name otherwise."
        ),
    )
    import_parser.add_argument(
        "--format",
        help=(
            "Force a defined CSV format instead of detecting it from the header "
            "(see 'budget format list')."
        ),
    )
    import_parser.set_defaults(func=_cmd_import)

    list_parser = subparsers.add_parser(
        "list", help="List transactions, optionally filtered."
    )
    list_parser.add_argument("--account", help="Filter by account name.")
    list_parser.add_argument(
        "--vendor", help="Filter by vendor (raw name or override display name)."
    )
    list_parser.add_argument("--category", help="Filter by category name.")
    list_parser.add_argument(
        "--limit", type=int, default=50, help="Max rows to show (default: %(default)s)."
    )
    list_parser.set_defaults(func=_cmd_list)

    rename_parser = subparsers.add_parser(
        "rename",
        help="Override a raw vendor name with a readable name (aggregates when reused).",
    )
    rename_parser.add_argument("raw", help="The raw vendor name as seen in imports.")
    rename_parser.add_argument("display", help="The readable display name.")
    rename_parser.set_defaults(func=_cmd_rename)

    rule_parser = subparsers.add_parser(
        "rule", help="Manage pattern-based vendor rename rules."
    )
    rule_parser.set_defaults(func=_cmd_rule, rule_command="list")
    rule_subparsers = rule_parser.add_subparsers(dest="rule_command")

    rule_add = rule_subparsers.add_parser(
        "add", help="Add or re-target a rule, then apply it."
    )
    rule_add.add_argument(
        "pattern", help="Glob matched against raw vendor names, e.g. 'Kindle Svcs*'."
    )
    rule_add.add_argument("display", help="The readable display name.")

    rule_remove = rule_subparsers.add_parser(
        "remove", help="Delete a rule and revert the vendors it named."
    )
    rule_remove.add_argument("pattern", help="The exact pattern to remove.")

    format_parser = subparsers.add_parser(
        "format",
        help=(
            "Inspect CSV layouts learned during import (stored in the database, not "
            "in the source tree)."
        ),
    )
    format_parser.set_defaults(func=_cmd_format, format_command="list", name=None)
    format_subparsers = format_parser.add_subparsers(dest="format_command")

    format_remove = format_subparsers.add_parser("remove", help="Delete a format.")
    format_remove.add_argument("name", help="The format name to remove.")

    format_subparsers.add_parser("list", help="Show defined formats (default).")

    format_export = format_subparsers.add_parser(
        "export", help="Print or write format definitions as JSON."
    )
    format_export.add_argument("name", nargs="?", help="Export just this format.")
    format_export.add_argument("--output", help="Write to this file instead of stdout.")

    rule_subparsers.add_parser("list", help="Show every rule (default).")
    rule_subparsers.add_parser(
        "apply", help="Re-run all rules, e.g. after importing outside the app."
    )

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
