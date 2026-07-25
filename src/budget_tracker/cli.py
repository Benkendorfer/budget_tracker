"""Command-line interface for the budget tracker."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from .db import DEFAULT_DB_PATH, get_engine, get_sessionmaker, init_db
from .importer import DEFAULT_CURRENCY_CODE, import_csv

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
    with session_factory() as session:
        result = import_csv(session, path, currency_code=args.currency)

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

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
