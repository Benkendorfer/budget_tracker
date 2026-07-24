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

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
