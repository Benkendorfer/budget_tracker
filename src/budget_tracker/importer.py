"""Import bank / credit-card CSV exports into the database.

The per-bank details live in :mod:`.formats`; this module holds the one parsing path
they all share. The format is detected from the CSV header, so callers normally just
hand over a path.

Some exports carry no account column, in which case the caller must say which account
the file belongs to — see :class:`~.formats.AccountRequired`.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# Bank exports are commonly UTF-8 or Windows-1252 (cp1252). Try them in order;
# cp1252 handles accented European merchant names ("Genève") that break UTF-8.
_ENCODINGS = ("utf-8-sig", "cp1252")

from sqlalchemy import select
from sqlalchemy.orm import Session

from .formats import (
    SIGNED,
    AccountCurrencyMismatch,
    AccountRequired,
    FormatSpec,
    UnknownFormat,
    detect,
)
from .models import Account, Category, Currency, Import, Transaction, Vendor

DEFAULT_CURRENCY_CODE = "USD"
_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "CHF": "CHF"}

# Symbols amount cells may carry, longest first so "CHF" isn't shadowed by a shorter
# match. Reuses the set already known to the module rather than inventing a second list.
_CURRENCY_SYMBOLS = tuple(sorted(set(_SYMBOLS.values()), key=len, reverse=True))

# What is left once sign and symbol are peeled off: plain digits, or comma-grouped
# thousands ("9,999.99" / "99,999.99"), with an optional decimal part either way.
_AMOUNT_RE = re.compile(r"^\d{1,3}(,\d{3})*(\.\d+)?$|^\d+(\.\d+)?$")


@dataclass
class ImportResult:
    source_file: str
    total_rows: int
    inserted: int
    skipped_duplicates: int


# Shown in the import panel's Status column for a transfer-log export. Not a row in
# csv_format: this layout is recognized from its own columns, not learned.
WISE_FORMAT_NAME = "Wise transfer log"


def _get_or_create_currency(session: Session, code: str) -> Currency:
    currency = session.scalar(select(Currency).where(Currency.value == code))
    if currency is None:
        currency = Currency(value=code, symbol=_SYMBOLS.get(code), decimal_places=2)
        session.add(currency)
        session.flush()
    return currency


def _get_or_create_account(session: Session, name: str, currency: Currency) -> Account:
    """Look up ``name``, creating it in ``currency`` if it does not exist yet.

    Refuses to attach a transaction in a different currency to an account that already
    exists, rather than posting it anyway: an account holds one currency, so accepting
    a mismatched one would either mis-record the amount or leave a home-currency total
    silently wrong wherever it isn't converted.
    """
    account = session.scalar(select(Account).where(Account.name == name))
    if account is None:
        account = Account(name=name, currency_id=currency.id)
        session.add(account)
        session.flush()
        return account
    if account.currency_id != currency.id:
        existing = session.get(Currency, account.currency_id)
        existing_code = existing.value if existing else "unknown"
        raise AccountCurrencyMismatch(
            f"Account {name!r} is {existing_code}, but this import is "
            f"{currency.value}. Pass --account to use a different account, or fix "
            "the CSV format's currency if this was a setup mistake."
        )
    return account


def _get_or_create_vendor(session: Session, name: str) -> Vendor:
    name = name.strip()
    vendor = session.scalar(select(Vendor).where(Vendor.name == name))
    if vendor is None:
        vendor = Vendor(name=name)
        session.add(vendor)
        session.flush()
    return vendor


def _get_or_create_category(session: Session, value: str) -> Category:
    """The category ``value`` names, from anywhere in the tree. No commit.

    Delegates to :func:`categories.get_or_create` (imported here, not at module level,
    to keep the dependency one-way like the other cross-module calls below) so a bank's
    flat CSV category binds to an already-nested category of the same name instead of
    forking a rival top-level one.
    """
    from .categories import get_or_create

    return get_or_create(session, value)


def _normalize_amount(raw: str) -> Decimal:
    """Parse one CSV amount cell into a :class:`Decimal`, tolerating the shapes a real
    export mixes into a single column: a currency symbol, and comma thousands
    separators, in addition to plain "-75.00".

    The symbol and the sign can appear in either order ("-$75.00" and "$-75.00" both
    occur in the wild), so each is peeled from the front independently rather than
    assumed to nest one way. What is left must be a plain or comma-grouped number or
    this raises — silently returning 0 for text we don't understand would put a wrong
    figure in the user's ledger, which is worse than refusing to import.

    European "1.234,56" grouping is deliberately not attempted: without knowing the
    file's locale, a "." could be a thousands separator or a decimal point, and
    guessing wrong would corrupt the amount rather than merely reject it. Parenthesised
    negatives ("(75.00)") are likewise not handled — no format seen here uses them, and
    adding speculative shapes is how this kind of ambiguity creeps in.
    """
    text = raw.strip()
    negative = False
    # Sign and symbol can each appear once, in either order, at the front of the cell.
    for _ in range(2):
        if text[:1] == "-":
            negative = True
            text = text[1:].lstrip()
            continue
        for symbol in _CURRENCY_SYMBOLS:
            if text.startswith(symbol):
                text = text[len(symbol):].lstrip()
                break
        else:
            break
    if not _AMOUNT_RE.match(text):
        raise ValueError(f"Could not parse amount {raw!r}.")
    try:
        return Decimal(("-" if negative else "") + text.replace(",", ""))
    except InvalidOperation as error:
        raise ValueError(f"Could not parse amount {raw!r}.") from error


def _parse_amount_minor(debit: str, credit: str, decimal_places: int) -> int:
    """Signed minor units: debit (charge) negative, credit (payment) positive.

    The *column* decides the direction, not the value's own sign. Most providers write a
    bare magnitude in each column, but some write the debit already negated; negating
    that again turned an outflow into income, which looks plausible on screen rather than
    obviously broken. Taking the magnitude reads both conventions the same way.
    """
    scale = 10 ** decimal_places
    debit = (debit or "").strip()
    credit = (credit or "").strip()
    if debit:
        return -abs(int((_normalize_amount(debit) * scale).to_integral_value()))
    if credit:
        return abs(int((_normalize_amount(credit) * scale).to_integral_value()))
    return 0


def _parse_signed_minor(amount: str, decimal_places: int) -> int:
    """Signed minor units from a single already-signed column."""
    amount = (amount or "").strip()
    if not amount:
        return 0
    scale = 10 ** decimal_places
    return int((_normalize_amount(amount) * scale).to_integral_value())


def _row_hash(*parts) -> str:
    """Stable dedup key over a format's dedup columns plus an occurrence index.

    The ``occurrence`` index (how many identical rows preceded this one in the
    file) means re-importing the same file is idempotent, while genuinely
    repeated same-day charges are still stored as separate rows.

    The parts are a format's dedup column values in its declared order, so the hash is
    stable for as long as that declaration is.
    """
    raw = "|".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_blank(row: Dict[str, str]) -> bool:
    """True when every field in the row is empty.

    A row with more fields than the header puts the surplus under csv's ``restkey`` as a
    *list*, not a string, so this cannot assume the values are all strings.
    """
    for value in row.values():
        values = value if isinstance(value, list) else [value]
        if any((item or "").strip() for item in values):
            return False
    return True


def _parse_date(value: str, date_formats: Sequence[str]) -> date:
    value = (value or "").strip()
    for date_format in date_formats:
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue
    raise ValueError(f"Could not parse date {value!r} using {list(date_formats)}.")


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    # Last resort: latin-1 never fails (every byte is a code point).
    return raw.decode("latin-1")


@dataclass(frozen=True)
class InboxFolder:
    """A sub-directory of the inbox, offered for browsing rather than importing."""

    path: Path
    csv_count: int  # CSVs anywhere beneath it, however deep

    @property
    def name(self) -> str:
        return self.path.name


@dataclass(frozen=True)
class InboxListing:
    """One directory's worth of the inbox: where to go, and what to import here."""

    directory: Path
    # Where "up" leads, or None at the root. It is None rather than the real parent
    # precisely so browsing cannot climb out of the inbox and start offering the rest of
    # the filesystem for import.
    parent: Optional[Path]
    folders: Sequence[InboxFolder]
    files: Sequence[Path]


def _count_csvs(directory: Path) -> int:
    """CSVs anywhere beneath ``directory``, counted by the rules the listing itself uses.

    That means matching on the lowercased suffix rather than a ``*.csv`` glob (which is
    case-sensitive, and would miss a ``.CSV``) and skipping dot-prefixed names (which the
    listing hides, so counting them would promise files you cannot reach). A folder
    labeled "2 CSVs" that turns out to hold three is worse than no count at all.
    """
    try:
        return sum(
            1
            for path in directory.rglob("*")
            if path.is_file()
            and path.suffix.lower() == ".csv"
            and not any(
                part.startswith(".") for part in path.relative_to(directory).parts
            )
        )
    except OSError:
        return 0


def list_inbox(directory: Path, root: Path) -> InboxListing:
    """Sub-directories and CSVs directly inside ``directory``, plus the way back up.

    ``root`` bounds the walk: ``parent`` is None once ``directory`` is the root, or is
    anywhere outside it, so a caller that only ever follows ``parent`` can never leave
    the inbox. Names beginning with a dot are skipped — a ``.git`` or a ``.Trash`` in a
    downloads folder is noise, not something to import.

    An unreadable directory lists as empty rather than raising: the picker showing
    nothing is a better failure than the app refusing to open.
    """
    directory = Path(directory)
    try:
        entries = sorted(directory.iterdir(), key=lambda path: path.name.lower())
    except OSError:
        entries = []
    entries = [path for path in entries if not path.name.startswith(".")]

    folders = [
        InboxFolder(path=path, csv_count=_count_csvs(path))
        for path in entries
        if path.is_dir()
    ]
    files = [
        path for path in entries if path.is_file() and path.suffix.lower() == ".csv"
    ]

    parent = None
    try:
        resolved, resolved_root = directory.resolve(), Path(root).resolve()
        if resolved != resolved_root and resolved_root in resolved.parents:
            parent = directory.parent
    except OSError:
        pass

    return InboxListing(
        directory=directory, parent=parent, folders=folders, files=files
    )


@dataclass
class ImportCandidate:
    """A file offered for import, with whatever stands in the way of importing it."""

    path: Path
    row_count: int
    format_name: Optional[str] = None
    problem: Optional[str] = None

    @property
    def ready(self) -> bool:
        return self.problem is None

    @property
    def status(self) -> str:
        return self.problem or (self.format_name or "")


def inspect_csv(session: Session, path: Path) -> ImportCandidate:
    """Work out whether ``path`` can be imported as-is, without importing it."""
    path = Path(path)
    try:
        fieldnames, rows = read_header_and_rows(path)
    except OSError as error:
        return ImportCandidate(path=path, row_count=0, problem=f"unreadable: {error}")
    # Recognized by its own columns rather than by a learned format, so it is ready the
    # first time it is seen and never asks the layout questions — none of which it could
    # answer, since it carries two currencies per row and no single amount column.
    from . import wise

    if wise.looks_like_wise(fieldnames):
        return ImportCandidate(
            path=path, row_count=len(rows), format_name=WISE_FORMAT_NAME
        )
    try:
        fmt = detect(session, fieldnames)
    except UnknownFormat:
        return ImportCandidate(
            path=path, row_count=len(rows), problem="needs setup"
        )
    if fmt.needs_account:
        return ImportCandidate(
            path=path,
            row_count=len(rows),
            format_name=fmt.name,
            problem="needs account",
        )
    return ImportCandidate(path=path, row_count=len(rows), format_name=fmt.name)


CANDIDATE_DELIMITERS = (",", ";", "\t", "|")


def _delimiter_score(text: str, delimiter: str) -> Tuple[int, int, int]:
    """How much like a table ``text`` reads when split on ``delimiter``.

    ``(is_tabular, rows_agreeing, columns)`` -- bigger is better. The right separator
    turns a file into many rows that all have the same number of columns; a wrong one
    leaves nearly every line as a single field. That difference is what is measured,
    rather than any property of the punctuation itself.
    """
    try:
        rows = [row for row in csv.reader(io.StringIO(text), delimiter=delimiter) if row]
    except csv.Error:
        return (0, 0, 0)
    widths = Counter(len(row) for row in rows if any(cell.strip() for cell in row))
    if not widths:
        return (0, 0, 0)
    columns, agreeing = widths.most_common(1)[0]
    # A single column means the separator never appeared; that is the losing case.
    return (1 if columns > 1 else 0, agreeing, columns)


def _sniff_delimiter(text: str) -> str:
    """The field separator, chosen by which one actually makes the file tabular.

    csv.Sniffer is deliberately not trusted on its own here. It resolves a semicolon
    confidently only when the sample happens to contain a *quoted* field with a
    semicolon inside it; a plainly separated file with no such field raises csv.Error
    and falls back to comma, which silently reproduces the bug this replaced -- the
    whole header collapsing into one column. A real export only avoided that by
    happening to contain a quoted address.

    Scoring every candidate has no such blind spot and is deterministic: the delimiter
    that yields the most rows of equal, greater-than-one width wins. Comma breaks ties,
    so a file that is tabular under more than one candidate keeps reading as it always
    did.
    """
    best = ","
    best_score = _delimiter_score(text, ",")
    for delimiter in CANDIDATE_DELIMITERS:
        if delimiter == ",":
            continue
        score = _delimiter_score(text, delimiter)
        if score > best_score:
            best, best_score = delimiter, score
    return best


# What a data cell -- a date, an amount, an account or reference number -- is made of:
# digits plus the separators those shapes use. A column name essentially never matches
# this on its own, which is what makes it useful for telling the two apart below.
_NUMERIC_CELL_RE = re.compile(r"^[+-]?[$€£]?[\d.,:/\s-]+$")


def _looks_numeric(cell: str) -> bool:
    return bool(cell) and bool(_NUMERIC_CELL_RE.match(cell))


def _looks_like_header(cells: Sequence[str]) -> bool:
    """True when a row of cells reads as column names, not data or a padded title.

    At least two non-empty cells, all of them distinct, and not more than half
    numeric-looking. A title row padded to the file's column count ("Account
    Statement,,,,,,,") has exactly one non-empty cell and fails the first test; an
    ordinary data row fails the last.
    """
    values = [cell.strip() for cell in cells if (cell or "").strip()]
    if len(values) < 2 or len(set(values)) != len(values):
        return False
    numeric = sum(1 for value in values if _looks_numeric(value))
    return numeric * 2 <= len(values)


def _find_header_index(lines: Sequence[str], delimiter: str) -> int:
    """Index into ``lines`` of the physical line the real header starts on.

    Rows are parsed with a real csv.reader over the whole file, not line by line -- a
    quoted field spanning several physical lines (a legal disclaimer, an address with
    embedded newlines, both seen in real exports) would otherwise fragment into many
    single-cell "rows" on a naive per-line parse and swamp the modal field count with
    noise. The counting wrapper below is how each logical row's *starting* physical line
    is recovered even though csv.reader itself only yields the joined-up row -- the
    caller needs that line number to slice the original, untouched text.

    The header is the first row whose field count equals the file's modal count and
    that looks like column names (see _looks_like_header). Modal count alone is not
    enough: an export that pads every line -- title rows included -- to one width would
    match on the first line, which is a title, not a header. Falls back to the first
    non-blank row when nothing satisfies both, so a file neither test likes degrades to
    the old behavior (skip blank rows, take what's left) instead of failing outright.
    """
    consumed = 0

    def _counting_lines():
        nonlocal consumed
        for line in lines:
            consumed += 1
            yield line

    records: List[Tuple[int, List[str]]] = []
    start = 0
    for cells in csv.reader(_counting_lines(), delimiter=delimiter):
        records.append((start, cells))
        start = consumed

    counts = [len(cells) for _, cells in records if any((c or "").strip() for c in cells)]
    if counts:
        modal_count = Counter(counts).most_common(1)[0][0]
        for line_index, cells in records:
            if len(cells) == modal_count and _looks_like_header(cells):
                return line_index
    for line_index, cells in records:
        if any((c or "").strip() for c in cells):
            return line_index
    return 0  # nothing but blanks; let csv report it


def _dict_reader(text: str) -> "csv.DictReader":
    """A reader whose delimiter and header line are sniffed rather than assumed.

    Both :func:`read_header_and_rows` (inference, ``inspect_csv``) and
    :func:`import_csv` route through this one function on the same file text, which is
    what keeps them agreeing on where the header is: neither could compute it
    differently even if it wanted to. The header line and everything after it is
    rejoined untouched, so a quoted field containing a newline further down the file
    survives.
    """
    delimiter = _sniff_delimiter(text)
    lines = text.splitlines(keepends=True)
    index = _find_header_index(lines, delimiter)
    return csv.DictReader(io.StringIO("".join(lines[index:])), delimiter=delimiter)


def read_header_and_rows(path: Path):
    """Return ``(fieldnames, rows)`` for a CSV, for format detection and inference."""
    reader = _dict_reader(_read_text(Path(path)))
    return list(reader.fieldnames or []), list(reader)


def import_csv(
    session: Session,
    path: Path,
    currency_code: Optional[str] = None,
    account_name: Optional[str] = None,
    fmt: Optional[FormatSpec] = None,
) -> ImportResult:
    """Import ``path``, auto-detecting its format unless ``fmt`` is given.

    Formats come from the database (see :mod:`.formats`), so which banks you use is not
    recorded anywhere in the source tree.

    ``currency_code`` wins if given; otherwise the format's own ``currency`` is used
    (every format has one, defaulting to USD), falling back to :data:`DEFAULT_CURRENCY_CODE`
    only in the case -- unreachable in practice, since every ``FormatSpec`` carries a
    currency -- where no format is available to ask.

    ``account_name`` names the account for formats whose files carry no account column;
    passing it for other formats overrides the account they would have derived.

    A Wise transfer-log export is recognized here and handed to :func:`import_wise_csv`,
    so every caller — the app's import panel, ``import all``, the CLI — picks it up
    without needing to know the layout exists. It cannot go through the path below: a row
    carries two currencies and may imply two transactions, which :class:`FormatSpec`
    has no way to express.
    """
    path = Path(path)
    # Same reader as inference used, or the two would disagree about which line is the
    # header and a format learned from this file would not match it on import.
    reader = _dict_reader(_read_text(path))
    fieldnames = reader.fieldnames or []

    from . import wise

    # Checked before `fmt`, not only when it is absent. A caller that resolved a format
    # first -- which is what both the app's import panel and the CLI do -- would
    # otherwise hand this layout to the generic parser, and a row carrying two
    # currencies has no single amount column for it to read. There is no format that
    # could correctly describe this file, so a format argument cannot outrank the
    # signature.
    if wise.looks_like_wise(fieldnames):
        return import_wise_csv(session, path)

    if fmt is None:
        fmt = detect(session, fieldnames)
    missing = [c for c in fmt.signature if c not in fieldnames]
    if missing:
        raise ValueError(
            f"CSV is missing columns {missing} required by format {fmt.name!r}; "
            f"found {fieldnames}."
        )
    if fmt.needs_account and not account_name:
        raise AccountRequired(
            f"The {fmt.name!r} format carries no account column, so '{path.name}' "
            "cannot say which account it belongs to. Pass --account."
        )
    rows = list(reader)

    resolved_currency_code = currency_code or fmt.currency or DEFAULT_CURRENCY_CODE
    currency = _get_or_create_currency(session, resolved_currency_code)

    import_record = Import(source_file=path.name, row_count=len(rows))
    session.add(import_record)
    session.flush()

    inserted = 0
    skipped = 0
    occurrence_counter: Dict[Tuple[str, ...], int] = {}
    account_ids = set()

    def cell(row: Dict[str, str], column: Optional[str]) -> str:
        return (row.get(column) or "").strip() if column else ""

    for row in rows:
        # A spreadsheet round-trip leaves rows of bare commas behind, and csv reads one
        # as a dict of empty strings rather than as nothing. Importing it would fail on
        # the empty date, blaming the date format for a row that holds no data at all.
        if _is_blank(row):
            continue
        description = cell(row, fmt.description_column)
        bank_category = cell(row, fmt.category_column)

        # Dedup is keyed on the format's own columns, in the order the format lists
        # them. That order is part of the hash, so changing it for an existing format
        # would make every row already imported under it look new.
        values = tuple(cell(row, c) for c in fmt.dedup_columns)
        key = (fmt.name,) + values
        occurrence = occurrence_counter.get(key, 0)
        occurrence_counter[key] = occurrence + 1
        import_hash = _row_hash(*values, occurrence)

        exists = session.scalar(
            select(Transaction.id).where(Transaction.import_hash == import_hash)
        )
        if exists is not None:
            skipped += 1
            continue

        if account_name:
            resolved_account = account_name
        else:
            card = cell(row, fmt.account_column)
            resolved_account = f"{fmt.account_prefix}{card}" if card else "Unknown"
        account = _get_or_create_account(session, resolved_account, currency)
        account_ids.add(account.id)

        vendor = _get_or_create_vendor(session, description) if description else None

        category = None
        category_source = "unset"
        if bank_category:
            category = _get_or_create_category(session, bank_category)
            category_source = "import"

        date_str = cell(row, fmt.posted_date_column) or cell(row, fmt.txn_date_column)
        posted_date = _parse_date(date_str, fmt.date_formats)

        if fmt.amount_style == SIGNED:
            value_minor = _parse_signed_minor(
                cell(row, fmt.amount_column), currency.decimal_places
            )
        else:
            value_minor = _parse_amount_minor(
                cell(row, fmt.debit_column),
                cell(row, fmt.credit_column),
                currency.decimal_places,
            )
        if fmt.invert_amount:
            # Applied once, after either parse path, so the flag means the same thing
            # whichever amount_style the format uses. The dedup hash is built from raw
            # cell text (below), not this value, so flipping it never changes import_hash.
            value_minor = -value_minor

        session.add(
            Transaction(
                account_id=account.id,
                category_id=category.id if category else None,
                currency_id=currency.id,
                import_id=import_record.id,
                vendor_id=vendor.id if vendor else None,
                posted_date=posted_date,
                description=description,
                raw_description=description,
                value_minor=value_minor,
                category_source=category_source,
                import_hash=import_hash,
            )
        )
        inserted += 1

    # Attribute the import to a single account when the file only had one.
    if len(account_ids) == 1:
        import_record.account_id = next(iter(account_ids))

    # Name any newly-seen vendors that an existing rule covers. Imported here to keep
    # the module-level dependency one-way (vendors -> models only).
    from .vendors import apply_rules

    apply_rules(session)

    # After the renames, since a category rule may be written against a display name.
    from .categories import apply_category_rules

    apply_category_rules(session)

    # New rows may be the other leg of a transfer already in the database.
    from .transfers import detect_transfers

    detect_transfers(session)

    session.commit()
    return ImportResult(
        source_file=path.name,
        total_rows=len(rows),
        inserted=inserted,
        skipped_duplicates=skipped,
    )


class UnknownImport(ValueError):
    """No import exists with the given id."""


@dataclass
class DeleteResult:
    import_id: int
    transactions_deleted: int
    transfers_broken: int


def delete_import(session: Session, import_id: int) -> DeleteResult:
    """Remove an import and the transactions it created. No commit.

    Accounts, vendors, and categories the import touched are left in place — a
    hand-written rule or another transaction may now depend on them, and an unused
    category is harmless (``get_categories`` already hides categories with no
    transactions).

    A transaction whose *partner* leg is being deleted stops being a transfer: its
    ``transfer_group_id`` is cleared, and if transfer detection is what categorised it
    (``category_source == "transfer"``), that category is cleared too, leaving it in the
    same "unset" state :func:`.transfers.clear_transfers` would. A category set by hand
    or by the import itself is left alone.
    """
    import_record = session.get(Import, import_id)
    if import_record is None:
        raise UnknownImport(f"No import with id {import_id}.")

    doomed = list(
        session.scalars(select(Transaction).where(Transaction.import_id == import_id))
    )
    doomed_ids = {t.id for t in doomed}
    group_ids = {
        t.transfer_group_id for t in doomed if t.transfer_group_id is not None
    }

    # Imported here, as elsewhere in this module, to keep the module-level dependency
    # one-way (transfers -> models only).
    from .transfers import TRANSFER_SOURCE

    transfers_broken = 0
    if group_ids:
        survivors = session.scalars(
            select(Transaction).where(
                Transaction.transfer_group_id.in_(group_ids),
                Transaction.id.not_in(doomed_ids),
            )
        )
        for leg in survivors:
            leg.transfer_group_id = None
            if leg.category_source == TRANSFER_SOURCE:
                leg.category_id = None
                leg.category_source = "unset"
            transfers_broken += 1

    for txn in doomed:
        session.delete(txn)
    # Flushed separately: Import <-> Transaction has no ORM-level relationship telling
    # the unit of work which side must go first, so without this the import row can be
    # sent to the database before the rows that reference it, and SQLite's foreign key
    # check rejects it.
    session.flush()
    session.delete(import_record)
    session.flush()

    return DeleteResult(
        import_id=import_id,
        transactions_deleted=len(doomed),
        transfers_broken=transfers_broken,
    )


def import_wise_csv(
    session: Session,
    path: Path,
    account_prefix: str = "Wise",
) -> ImportResult:
    """Import a Wise transfer-log export. See :mod:`.wise` for what the rows mean.

    This bypasses :class:`~.formats.FormatSpec` entirely, because the layout is not a
    ledger: a row carries two currencies and can imply two transactions. :func:`wise.plan_rows`
    makes every decision about which leg is yours and what it means; this function does
    nothing but write the result down.

    Accounts are created per currency (``Wise USD``, ``Wise CHF``), derived from each
    row's own currency rather than asked for once per file — so a single export that
    mixes balances splits itself across the right accounts.
    """
    from . import wise

    path = Path(path)
    reader = _dict_reader(_read_text(path))
    rows = list(reader)
    if not wise.looks_like_wise(reader.fieldnames or []):
        raise UnknownFormat(
            f"'{path.name}' is not a Wise transfer export: it is missing "
            f"{[c for c in wise.SIGNATURE if c not in (reader.fieldnames or [])]}."
        )

    plan = wise.plan_rows(rows)

    import_record = Import(source_file=path.name, row_count=len(rows))
    session.add(import_record)
    session.flush()

    currencies: Dict[str, Currency] = {}
    accounts: Dict[str, Account] = {}
    inserted = 0
    skipped = 0
    conversions = []

    for planned in plan.transactions:
        # Namespaced by the reader, not by a format name: this hash must stay stable even
        # if the file is later renamed or re-exported, since the transfer ID is the only
        # thing that makes re-importing idempotent.
        import_hash = _row_hash("wise", planned.dedup_key)
        exists = session.scalar(
            select(Transaction.id).where(Transaction.import_hash == import_hash)
        )
        if exists is not None:
            skipped += 1
            continue

        currency = currencies.get(planned.currency)
        if currency is None:
            currency = _get_or_create_currency(session, planned.currency)
            currencies[planned.currency] = currency
        account = accounts.get(planned.currency)
        if account is None:
            account = _get_or_create_account(
                session, wise.account_name(planned.currency, account_prefix), currency
            )
            accounts[planned.currency] = account

        scale = 10 ** currency.decimal_places
        value_minor = int((planned.amount * scale).to_integral_value())

        vendor = (
            _get_or_create_vendor(session, planned.description)
            if planned.description
            else None
        )

        category = None
        category_source = "unset"
        if planned.category:
            category = _get_or_create_category(session, planned.category)
            category_source = "import"

        txn = Transaction(
            account_id=account.id,
            category_id=category.id if category else None,
            currency_id=currency.id,
            import_id=import_record.id,
            vendor_id=vendor.id if vendor else None,
            posted_date=planned.posted_date,
            description=planned.description,
            raw_description=planned.description,
            value_minor=value_minor,
            category_source=category_source,
            import_hash=import_hash,
        )
        session.add(txn)
        if planned.is_transfer:
            conversions.append(txn)
        inserted += 1

    # A conversion is a transfer with only one leg recorded: the money arriving in the
    # other currency is deliberately not stored (each account tracks only its own side).
    # It still needs a transfer_group_id, because that — not the two legs canceling — is
    # what keeps it out of the spending figures. Group ids elsewhere are transaction ids,
    # so a group of one uses its own; that needs the flush above it for an id to exist.
    if conversions:
        from .transfers import TRANSFER_CATEGORY, TRANSFER_SOURCE as _TRANSFER_SOURCE

        session.flush()
        transfer_category = _get_or_create_category(session, TRANSFER_CATEGORY)
        for txn in conversions:
            txn.transfer_group_id = txn.id
            txn.category_id = transfer_category.id
            txn.category_source = _TRANSFER_SOURCE

    if len(accounts) == 1:
        import_record.account_id = next(iter(accounts.values())).id

    # These are rates the user actually paid on real conversions, so they are the best
    # evidence for those specific days — better than a later reference rate. Recorded
    # unconditionally (not just for newly inserted rows): record_rate upserts on
    # (day, base, quote, source), so re-importing an already-seen file just rewrites the
    # same values rather than duplicating them.
    if plan.rates:
        from . import rates as rates_module

        rates_module.record_observed(
            session, ((r.day, r.base, r.quote, r.rate) for r in plan.rates)
        )

    from .vendors import apply_rules

    apply_rules(session)

    from .categories import apply_category_rules

    apply_category_rules(session)

    # Same as import_csv: newly imported rows may be the other leg of a transfer already
    # on file. A top-up from your own bank into a Wise balance is same-currency, equal
    # and opposite, and in a different account — an ordinary transfer that only this
    # finds. Conversions are already paired above, and detection skips paired rows, so
    # this cannot disturb them.
    from .transfers import detect_transfers

    detect_transfers(session)

    session.commit()
    return ImportResult(
        source_file=path.name,
        total_rows=len(rows),
        inserted=inserted,
        skipped_duplicates=skipped,
    )
