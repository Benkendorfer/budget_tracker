"""The ``import``/``imports``/``unimport`` commands, plus the CSV-format walkthrough.

Importing is the one command family that can talk back to the user (the interactive
inbox picker, and the format-setup questions when a CSV's layout is not recognized), so
those helpers live here alongside the commands that trigger them.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Optional

from .. import formats, queries
from .. import rates as rates_module
from ..db import get_engine, get_sessionmaker, init_db, resolve_db_path
from ..importer import import_csv, list_inbox, read_header_and_rows

# import_cmds.py -> cli -> budget_tracker -> src -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parents[3]
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
    from .. import wise

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
            # A foreign-currency import with no rate on file yet would otherwise show
            # every money figure as a silent zero (see queries.Totals.unconverted_count)
            # until someone thinks to run `rates fetch` by hand. import_csv already
            # committed its own rows above; this is a second, unrelated write, so it is
            # committed on its own rather than folded into the same transaction.
            rate_outcome = rates_module.fetch_rates_for_import(
                session, result.import_id, queries.HOME_CURRENCY
            )
            session.commit()
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
    summary = _rate_fetch_summary(rate_outcome)
    if summary:
        print(summary)
    return 0


def _rate_fetch_summary(outcome: rates_module.ImportRatesOutcome) -> str:
    """One line describing what came of fetching rates for an import, or nothing to say."""
    if not outcome.attempted:
        return ""
    quotes = ", ".join(outcome.quotes)
    if outcome.error is not None:
        return (
            f"Could not fetch {queries.HOME_CURRENCY} -> {quotes} rates: {outcome.error} "
            "Run 'budget rates fetch' later."
        )
    return f"Fetched {outcome.written} rate(s) for {queries.HOME_CURRENCY} -> {quotes}."


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
    from ..importer import UnknownImport, delete_import

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
