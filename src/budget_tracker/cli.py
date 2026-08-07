"""Command-line interface for the budget tracker."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from . import categories as categories_module
from . import formats, queries
from . import transfers as transfers_module
from .db import get_engine, get_sessionmaker, init_db, resolve_db_path
from .importer import import_csv, list_inbox, read_header_and_rows

# cli.py -> budget_tracker -> src -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parents[2]
TO_IMPORT_DIR = _REPO_ROOT / "data" / "to_import"


def _select_csv_interactively() -> Optional[Path]:
    """Pick a CSV from the inbox, descending into sub-directories on the way.

    Sub-directories are offered as ordinary numbered choices, ``../`` among them, so
    reaching a nested file needs no path typing. :func:`importer.list_inbox` supplies the
    ``../`` only while there is one, which is what keeps the walk inside the inbox.
    """
    directory = TO_IMPORT_DIR
    while True:
        listing = list_inbox(directory, TO_IMPORT_DIR)
        entries = []  # (label, path, is_directory)
        if listing.parent is not None:
            entries.append(("../", listing.parent, True))
        for folder in listing.folders:
            plural = "" if folder.csv_count == 1 else "s"
            entries.append(
                (f"{folder.name}/  ({folder.csv_count} CSV{plural})", folder.path, True)
            )
        entries.extend((path.name, path, False) for path in listing.files)

        if not entries:
            print(f"Nothing to import in {directory}")
            return None

        print(f"\n{directory}")
        for index, (label, _path, _is_dir) in enumerate(entries, start=1):
            print(f"  [{index}] {label}")
        choice = input(f"Enter number (1-{len(entries)}) or q to quit: ").strip()
        if choice.lower() in {"q", "quit", ""}:
            return None
        try:
            index = int(choice)
            if not 1 <= index <= len(entries):
                raise ValueError
        except ValueError:
            # Re-prompt rather than giving up. A mistyped number is a slip, and quitting
            # the whole import over it means walking back down the tree again.
            print("Invalid selection.")
            continue

        _label, path, is_directory = entries[index - 1]
        if not is_directory:
            return path
        directory = path


def _has_builtin_reader(path: Path) -> bool:
    """Whether this file is a layout the code reads directly, with no learned format."""
    from . import wise

    fieldnames, _rows = read_header_and_rows(path)
    return wise.looks_like_wise(fieldnames)


def _resolve_format(session, path: Path):
    """Detect the file's format, falling back to interactive setup.

    ``None`` means the user abandoned the walkthrough, and nothing else -- see the
    caller, which treats it as a cancellation.
    """
    fieldnames, rows = read_header_and_rows(path)
    try:
        return formats.detect(session, fieldnames)
    except formats.UnknownFormat:
        return _setup_format(session, path, fieldnames, rows)


def _resolve_category(session, name: str) -> Optional[int]:
    """``--category``'s lookup: a full path (``"Food > Dining"``) or a bare name.

    Routed through :func:`categories.resolve_path` rather than
    :func:`queries.resolve_category`, which matches on a bare name with no parent
    filter and would arbitrarily pick a match once a name can repeat across branches.
    """
    try:
        category = categories_module.resolve_path(session, name)
    except categories_module.CategoryError as error:
        print(error)
        return None
    if category is None:
        print(f"No category named {name!r}.")
        return None
    return category.id


def _iso_date(text: str) -> date:
    """argparse ``type=`` for a ``YYYY-MM-DD`` flag; a bad value fails argparse's own
    usage error rather than a traceback."""
    return datetime.strptime(text, "%Y-%m-%d").date()


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
            # A layout with a built-in reader is recognized from its own columns rather
            # than learned, and none of the setup questions -- which column holds the
            # amount, does a positive number mean money out -- has an answer for a file
            # whose every row carries two currencies. Skipping resolution entirely lets
            # import_csv route on the signature.
            if fmt is None and not _has_builtin_reader(path):
                fmt = _resolve_format(session, path)
                if fmt is None:
                    return 1  # the user abandoned the walkthrough
            result = import_csv(
                session,
                path,
                currency_code=args.currency,
                account_name=args.account,
                fmt=fmt,
            )
    except (
        formats.AccountRequired,
        formats.UnknownFormat,
        formats.AccountCurrencyMismatch,
    ) as error:
        print(error)
        return 1

    print(
        f"Imported '{result.source_file}': {result.inserted} added, "
        f"{result.skipped_duplicates} duplicates skipped "
        f"({result.total_rows} rows total)."
    )
    print(f"Database: {resolve_db_path()}")
    return 0


def _cmd_imports(args: argparse.Namespace) -> int:
    """List past imports — where an ``unimport`` id comes from."""
    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        rows = queries.get_imports(session)

    if not rows:
        print("No imports yet.")
        return 0
    width = max(len(row.source_file) for row in rows)
    for row in rows:
        account = row.account or "multiple/none"
        print(
            f"  [{row.id:>4}] {row.source_file:<{width}}  "
            f"{row.transaction_count:>6} txns  {account}  {row.imported_at}"
        )
    return 0


def _cmd_unimport(args: argparse.Namespace) -> int:
    """Delete a past import and its transactions. Destructive, so ``--yes`` is required.

    Without it, this only previews what would happen — the source file, the transaction
    count, and any transfer pairings that would be broken — read up front through
    :func:`queries.preview_import_delete`, the same numbers the app's own confirmation
    shows, never found out by deleting first.
    """
    from .importer import UnknownImport, delete_import

    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        preview = queries.preview_import_delete(session, args.import_id)
        if preview is None:
            print(f"No import with id {args.import_id}.")
            return 1

        if not args.yes:
            transfers_note = (
                f", breaking {preview.transfers_broken} transfer pairing(s)"
                if preview.transfers_broken
                else ""
            )
            print(
                f"Would delete import #{args.import_id} ({preview.source_file}): "
                f"{preview.transaction_count} transaction(s){transfers_note}."
            )
            print("Re-run with --yes to actually delete it.")
            return 1

        try:
            result = delete_import(session, args.import_id)
        except UnknownImport as error:
            print(error)
            return 1
        session.commit()

    transfers_note = (
        f", {result.transfers_broken} transfer pairing(s) broken"
        if result.transfers_broken
        else ""
    )
    print(
        f"Deleted import #{result.import_id} ({preview.source_file}): "
        f"{result.transactions_deleted} transaction(s) removed{transfers_note}."
    )
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    from rich.console import Console
    from rich.table import Table

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
            category_id = _resolve_category(session, args.category)
            if category_id is None:
                return 1
        vendor_filter = None
        if args.vendor:
            vendor_filter = queries.resolve_vendor_filter(session, args.vendor)
            if vendor_filter is None:
                print(f"No vendor named {args.vendor!r}.")
                return 1
        text_filter = (
queries.TextFilter(args.search, args.search_in) if args.search else None
        )
        txns = queries.get_transactions(
            session,
            account_id,
            category_id,
            vendor_filter,
            limit=args.limit,
            text_filter=text_filter,
        )
        totals = queries.get_totals(
            session, account_id, category_id, vendor_filter, text_filter=text_filter
        )

    console = Console()
    table = Table(box=None, pad_edge=False)
    table.add_column("Date")
    table.add_column("Description")
    table.add_column("Vendor")
    table.add_column("Category")
    table.add_column("Amount", justify="right")
    for txn in txns:
        # Match the app: transfers are dimmed and flagged so their absence from the
        # totals is visible rather than mysterious.
        style = "dim" if txn.is_transfer else ("red" if txn.amount_minor < 0 else "green")
        description = f"⇄ {txn.description}" if txn.is_transfer else txn.description
        row = [txn.posted_date, description, txn.vendor, txn.category]
        table.add_row(
            *([f"[dim]{c}[/dim]" for c in row] if txn.is_transfer else row),
            f"[{style}]{txn.amount_minor / 100:,.2f}[/{style}]",
        )
    console.print(table)
    console.print(
        f"[bold]{totals.count} txns[/bold]"
        + (f" ({totals.transfer_count} transfers excluded)" if totals.transfer_count else "")
        + "   "
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


def _cmd_categorize(args: argparse.Namespace) -> int:
    from . import categories

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
    from . import categories

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
    """Walk the user through defining a format for an unrecognized CSV."""
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
            if args.format_command == "invert":
                spec = formats.set_invert_amount(session, args.name, args.state == "on")
                session.commit()
                state = "on" if spec.invert_amount else "off"
                print(f"{spec.name!r}: invert {state}.")
                return 0

            if args.format_command == "prefix":
                spec = formats.set_account_prefix(session, args.name, args.prefix)
                session.commit()
                print(
                    f"{spec.name!r} now names accounts "
                    f"{spec.account_prefix + '<value>' if spec.account_prefix else '<value>'}."
                )
                return 0

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
        # invert only means anything for a single signed column; a debit/credit pair
        # already says which side is an outflow, so it is left off there.
        polarity = (
            f"  invert: {'on' if spec.invert_amount else 'off'}"
            if spec.amount_style == formats.SIGNED
            else ""
        )
        print(f"  {spec.name:<{width}}  {spec.amount_style:<13}  account: {account}{polarity}")
    return 0


def _cmd_account(args: argparse.Namespace) -> int:
    from . import accounts

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


def _cmd_transfers(args: argparse.Namespace) -> int:
    from . import transfers

    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        if args.reset:
            reset = transfers.clear_transfers(session)
            session.commit()
            print(f"Un-paired {reset} transaction(s).")
            return 0
        pairs = transfers.detect_transfers(
            session, window_days=args.days, allow_same_account=args.same_account
        )
        session.commit()
        totals = queries.get_totals(session)
    scope = " (same-account allowed)" if args.same_account else ""
    print(f"Found {pairs} new transfer pair(s) within {args.days} day(s){scope}.")
    print(f"{totals.transfer_count} transaction(s) are now excluded from totals.")
    return 0


def _cmd_rates(args: argparse.Namespace) -> int:
    """List cached exchange rates, fetch ECB references, or set a manual one.

    ``rates.py`` and ``wise.py`` are finished modules this only wires up: the
    aggregation for ``list`` is done here with a direct query rather than through
    ``queries.py``, which this change does not touch.
    """
    from decimal import Decimal, InvalidOperation

    from sqlalchemy import func, select

    from . import rates as rates_module
    from .models import ExchangeRate, Transaction

    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    command = args.rates_command or "list"

    with session_factory() as session:
        if command == "set":
            try:
                rate = Decimal(args.rate)
            except InvalidOperation:
                print(f"Could not read {args.rate!r} as a rate.")
                return 1
            on = args.on or date.today()
            rates_module.record_rate(
                session, on, args.base, args.quote, rate, rates_module.MANUAL
            )
            session.commit()
            print(f"Set {args.base} -> {args.quote} = {rate} on {on.isoformat()} (manual).")
            return 0

        if command == "fetch":
            start, end = args.start, args.end
            if start is None or end is None:
                first, last = session.execute(
                    select(
                        func.min(Transaction.posted_date),
                        func.max(Transaction.posted_date),
                    )
                ).one()
                start = start or first
                end = end or last
            if start is None or end is None:
                print(
                    "No transactions in the database to derive a date range from; "
                    "pass --start and --end."
                )
                return 1
            print(f"Range: {start.isoformat()}..{end.isoformat()}")

            # Base on the currency totals are actually reported in. rate_on can invert
            # a cached pair but cannot chain one pair through another, so basing on
            # anything else would leave the conversions that matter unreachable: with a
            # USD home and a CHF base, EUR -> USD could not be resolved at all. Basing
            # here means every X -> home lookup is either cached or a single inversion.
            currencies = sorted({row.currency for row in queries.get_accounts(session)})
            base = queries.HOME_CURRENCY
            quotes = [c for c in currencies if c != base]
            if not quotes:
                print(f"Only one currency on file ({base or 'none'}); nothing to fetch.")
                return 0

            try:
                written = rates_module.fetch_ecb_rates(session, start, end, base, quotes)
            except rates_module.FrankfurterError as error:
                print(f"Could not fetch ECB rates: {error}")
                return 1
            session.commit()
            print(f"Wrote {written} rate(s) for {base} -> {', '.join(quotes)}.")
            return 0

        rows = session.execute(
            select(
                ExchangeRate.base,
                ExchangeRate.quote,
                ExchangeRate.source,
                func.min(ExchangeRate.day),
                func.max(ExchangeRate.day),
                func.count(),
            )
            .group_by(ExchangeRate.base, ExchangeRate.quote, ExchangeRate.source)
            .order_by(ExchangeRate.base, ExchangeRate.quote, ExchangeRate.source)
        ).all()  # "list"

    if not rows:
        print("No exchange rates cached yet. Run 'budget rates fetch' or 'budget rates set'.")
        return 0
    for base, quote, source, first, last, count in rows:
        span = first.isoformat() if first == last else f"{first.isoformat()}..{last.isoformat()}"
        plural = "" if count == 1 else "s"
        print(f"  {base} -> {quote}   {source:<8} {span:<23} {count} rate{plural}")
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
