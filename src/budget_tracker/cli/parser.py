"""``build_parser`` and the ``main`` entry point.

The parser wires each subcommand to its handler; the handlers themselves live in the
command-family modules alongside this one (``import_cmds.py``, ``list_cmd.py``,
``vendors.py``, ``categories.py``, ``accounts.py``, ``transfers.py``, ``formats.py``,
``rates.py``). This module only assembles argparse structure — no handler logic — so it
stays readable as the single place every subcommand is registered.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from typing import List, Optional

from .. import queries
from .. import transfers as transfers_module
from .accounts import _cmd_account
from .categories import _cmd_categorize, _cmd_category, _cmd_category_rule
from .formats import _cmd_format
from .import_cmds import _cmd_import, _cmd_imports, _cmd_unimport
from .list_cmd import _cmd_list
from .rates import _cmd_rates
from .transfers import _cmd_transfers
from .vendors import _cmd_rename, _cmd_rule


def _iso_date(text: str) -> date:
    """argparse ``type=`` for a ``YYYY-MM-DD`` flag; a bad value fails argparse's own
    usage error rather than a traceback."""
    return datetime.strptime(text, "%Y-%m-%d").date()


def _cmd_tui(args: argparse.Namespace) -> int:
    # Imported lazily so plain `import` runs without pulling in Textual.
    from ..tui import run

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
        default=None,
        help=(
            "ISO currency code for the transactions. Defaults to the CSV format's own "
            "currency (USD unless set otherwise); passing this overrides it."
        ),
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

    imports_parser = subparsers.add_parser(
        "imports", help="List past imports (id, source file, transaction count)."
    )
    imports_parser.set_defaults(func=_cmd_imports)

    unimport_parser = subparsers.add_parser(
        "unimport",
        help=(
            "Delete a past import and its transactions (destructive; see 'budget "
            "imports' for ids)."
        ),
    )
    unimport_parser.add_argument("import_id", type=int, help="The import id to delete.")
    unimport_parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete. Without it, only preview what would be deleted.",
    )
    unimport_parser.set_defaults(func=_cmd_unimport)

    list_parser = subparsers.add_parser(
        "list", help="List transactions, optionally filtered."
    )
    list_parser.add_argument("--account", help="Filter by account name.")
    list_parser.add_argument(
        "--vendor", help="Filter by vendor (raw name or override display name)."
    )
    list_parser.add_argument(
        "--category",
        help="Filter by category name, or a full path (e.g. 'Food > Dining').",
    )
    list_parser.add_argument(
        "--search", help="Case-insensitive substring to look for."
    )
    list_parser.add_argument(
        "--search-in",
        choices=list(queries.TEXT_FIELDS),
        default="all",
        help="Which field --search looks at (default: %(default)s).",
    )
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

    categorize_parser = subparsers.add_parser(
        "categorize",
        help="Categorise every transaction of a vendor by hand (outranks rules).",
    )
    categorize_parser.add_argument(
        "vendor", help="Raw vendor name, or an override display name."
    )
    categorize_parser.add_argument(
        "category", nargs="?", help="The category to apply. Omit with --clear."
    )
    categorize_parser.add_argument(
        "--clear",
        action="store_true",
        help="Undo a manual category instead of setting one.",
    )
    categorize_parser.set_defaults(func=_cmd_categorize)

    category_parser = subparsers.add_parser(
        "category",
        help="Build the category hierarchy itself (parent > child nesting).",
    )
    category_parser.set_defaults(func=_cmd_category, category_command="list")
    category_subparsers = category_parser.add_subparsers(dest="category_command")

    category_add = category_subparsers.add_parser(
        "add",
        help="Create/move a category into place, creating any missing levels.",
    )
    category_add.add_argument(
        "path",
        help=(
            "Category path, e.g. 'Food > Dining > Restaurants'. A single name moves "
            "that category to the top level."
        ),
    )
    category_add.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Confirm relocating an existing category (names are unique tree-wide, so "
            "reusing one is a move, not a new category). Without it, only preview."
        ),
    )

    category_subparsers.add_parser("list", help="Show the category tree, indented (default).")

    category_merge = category_subparsers.add_parser(
        "merge",
        help=(
            "Fold one category into another: repoints its transactions, rules, and "
            "children, then deletes it (destructive; see --yes)."
        ),
    )
    category_merge.add_argument("source", help="Category to merge from (deleted).")
    category_merge.add_argument("target", help="Category to merge into (kept).")
    category_merge.add_argument(
        "--yes",
        action="store_true",
        help="Actually merge. Without it, only preview what would move.",
    )

    category_rule_parser = subparsers.add_parser(
        "category-rule", help="Manage pattern-based categorisation rules."
    )
    category_rule_parser.set_defaults(
        func=_cmd_category_rule, category_rule_command="list"
    )
    category_rule_subparsers = category_rule_parser.add_subparsers(
        dest="category_rule_command"
    )

    category_rule_add = category_rule_subparsers.add_parser(
        "add", help="Add or re-target a rule, then apply it."
    )
    category_rule_add.add_argument(
        "pattern",
        help="Glob matched against the raw vendor name or its display name.",
    )
    category_rule_add.add_argument("category", help="The category to apply.")

    category_rule_remove = category_rule_subparsers.add_parser(
        "remove", help="Delete a rule and clear the transactions it categorised."
    )
    category_rule_remove.add_argument("pattern", help="The exact pattern to remove.")

    category_rule_subparsers.add_parser("list", help="Show every rule (default).")
    category_rule_subparsers.add_parser(
        "apply", help="Re-run all rules, e.g. after importing outside the app."
    )

    account_parser = subparsers.add_parser(
        "account", help="List, rename, or merge accounts."
    )
    account_parser.set_defaults(func=_cmd_account, account_command="list")
    account_subparsers = account_parser.add_subparsers(dest="account_command")
    account_subparsers.add_parser("list", help="Show every account (default).")

    account_rename = account_subparsers.add_parser("rename", help="Rename an account.")
    account_rename.add_argument("old", help="Current account name.")
    account_rename.add_argument("new", help="New account name.")

    account_merge = account_subparsers.add_parser(
        "merge", help="Move everything from one account into another, then delete it."
    )
    account_merge.add_argument("source", help="Account to merge from (deleted).")
    account_merge.add_argument("target", help="Account to merge into (kept).")

    transfers_parser = subparsers.add_parser(
        "transfers",
        help="Pair up transactions that move money between your own accounts.",
    )
    transfers_parser.add_argument(
        "--days",
        type=int,
        default=transfers_module.DEFAULT_WINDOW_DAYS,
        help="How many days apart the two legs may post (default: %(default)s).",
    )
    transfers_parser.add_argument(
        "--reset", action="store_true", help="Un-pair every detected transfer."
    )
    transfers_parser.add_argument(
        "--same-account",
        action="store_true",
        help=(
            "Also pair legs that sit in the same account, for a provider whose "
            "sub-accounts you track here as one account. Off by default: it makes an "
            "accidental same-size, same-account pairing more likely, and a false "
            "pairing silently drops two real transactions from your totals."
        ),
    )
    transfers_parser.set_defaults(func=_cmd_transfers)

    rates_parser = subparsers.add_parser(
        "rates", help="Cache and inspect currency exchange rates."
    )
    rates_parser.set_defaults(func=_cmd_rates, rates_command="list")
    rates_subparsers = rates_parser.add_subparsers(dest="rates_command")

    rates_subparsers.add_parser(
        "list", help="Show cached rates: pair, source, date span, count (default)."
    )

    rates_fetch = rates_subparsers.add_parser(
        "fetch",
        help=(
            "Fetch ECB reference rates. With no dates, covers every transaction on "
            "file and every currency an account is in."
        ),
    )
    rates_fetch.add_argument(
        "--start", type=_iso_date, help="Start date (default: earliest transaction)."
    )
    rates_fetch.add_argument(
        "--end", type=_iso_date, help="End date (default: latest transaction)."
    )

    rates_set = rates_subparsers.add_parser(
        "set",
        help="Record a rate by hand; outranks other sources for that day.",
    )
    rates_set.add_argument("base", help="Base currency code, e.g. USD.")
    rates_set.add_argument("quote", help="Quote currency code, e.g. CHF.")
    rates_set.add_argument("rate", help="How many QUOTE units one BASE unit buys.")
    rates_set.add_argument(
        "--on", type=_iso_date, help="Date the rate applies to (default: today)."
    )

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

    format_prefix = format_subparsers.add_parser(
        "prefix", help="Set the prefix used for account names derived from a column."
    )
    format_prefix.add_argument("name", help="The format to change.")
    format_prefix.add_argument("prefix", help="New prefix; pass '' to remove it.")

    format_export = format_subparsers.add_parser(
        "export", help="Print or write format definitions as JSON."
    )
    format_export.add_argument("name", nargs="?", help="Export just this format.")
    format_export.add_argument("--output", help="Write to this file instead of stdout.")

    format_invert = format_subparsers.add_parser(
        "invert",
        help=(
            "Flip whether a positive amount means money leaving the account "
            "(providers disagree; future imports only)."
        ),
    )
    format_invert.add_argument("name", help="The format to change.")
    format_invert.add_argument("state", choices=["on", "off"], help="New polarity.")

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
