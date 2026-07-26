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
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

# Bank exports are commonly UTF-8 or Windows-1252 (cp1252). Try them in order;
# cp1252 handles accented European merchant names ("Genève") that break UTF-8.
_ENCODINGS = ("utf-8-sig", "cp1252")

from sqlalchemy import select
from sqlalchemy.orm import Session

from .formats import SIGNED, AccountRequired, FormatSpec, UnknownFormat, detect
from .models import Account, Category, Currency, Import, Transaction, Vendor

DEFAULT_CURRENCY_CODE = "USD"
_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "CHF": "CHF"}


@dataclass
class ImportResult:
    source_file: str
    total_rows: int
    inserted: int
    skipped_duplicates: int


def _get_or_create_currency(session: Session, code: str) -> Currency:
    currency = session.scalar(select(Currency).where(Currency.value == code))
    if currency is None:
        currency = Currency(value=code, symbol=_SYMBOLS.get(code), decimal_places=2)
        session.add(currency)
        session.flush()
    return currency


def _get_or_create_account(session: Session, name: str, currency: Currency) -> Account:
    account = session.scalar(select(Account).where(Account.name == name))
    if account is None:
        account = Account(name=name, currency_id=currency.id)
        session.add(account)
        session.flush()
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
    value = value.strip()
    category = session.scalar(
        select(Category).where(Category.parent_id.is_(None), Category.value == value)
    )
    if category is None:
        category = Category(value=value)
        session.add(category)
        session.flush()
    return category


def _parse_amount_minor(debit: str, credit: str, decimal_places: int) -> int:
    """Signed minor units: debit (charge) negative, credit (payment) positive."""
    scale = 10 ** decimal_places
    debit = (debit or "").strip()
    credit = (credit or "").strip()
    if debit:
        return -int((Decimal(debit) * scale).to_integral_value())
    if credit:
        return int((Decimal(credit) * scale).to_integral_value())
    return 0


def _parse_signed_minor(amount: str, decimal_places: int) -> int:
    """Signed minor units from a single already-signed column."""
    amount = (amount or "").strip()
    if not amount:
        return 0
    scale = 10 ** decimal_places
    return int((Decimal(amount) * scale).to_integral_value())


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


def read_header_and_rows(path: Path):
    """Return ``(fieldnames, rows)`` for a CSV, for format detection and inference."""
    reader = csv.DictReader(io.StringIO(_read_text(Path(path))))
    return list(reader.fieldnames or []), list(reader)


def import_csv(
    session: Session,
    path: Path,
    currency_code: str = DEFAULT_CURRENCY_CODE,
    account_name: Optional[str] = None,
    fmt: Optional[FormatSpec] = None,
) -> ImportResult:
    """Import ``path``, auto-detecting its format unless ``fmt`` is given.

    Formats come from the database (see :mod:`.formats`), so which banks you use is not
    recorded anywhere in the source tree.

    ``account_name`` names the account for formats whose files carry no account column;
    passing it for other formats overrides the account they would have derived.
    """
    path = Path(path)
    reader = csv.DictReader(io.StringIO(_read_text(path)))
    fieldnames = reader.fieldnames or []
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

    currency = _get_or_create_currency(session, currency_code)

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
