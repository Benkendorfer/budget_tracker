"""Import bank / credit-card CSV exports into the database.

Currently supports the Capital One-style export with columns::

    Transaction Date, Posted Date, Card No., Description, Category, Debit, Credit

One of ``Debit`` / ``Credit`` is populated per row. ``Debit`` is a charge
(outflow, stored negative); ``Credit`` is a payment or refund (inflow, positive).
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Dict, Tuple

# Bank exports are commonly UTF-8 or Windows-1252 (cp1252). Try them in order;
# cp1252 handles accented European merchant names ("Genève") that break UTF-8.
_ENCODINGS = ("utf-8-sig", "cp1252")

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Account, Category, Currency, Import, Transaction, Vendor

DEFAULT_CURRENCY_CODE = "USD"
EXPECTED_COLUMNS = [
    "Transaction Date",
    "Posted Date",
    "Card No.",
    "Description",
    "Category",
    "Debit",
    "Credit",
]
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


def _row_hash(
    card: str,
    txn_date: str,
    posted: str,
    description: str,
    debit: str,
    credit: str,
    occurrence: int,
) -> str:
    """Stable dedup key.

    The ``occurrence`` index (how many identical rows preceded this one in the
    file) means re-importing the same file is idempotent, while genuinely
    repeated same-day charges are still stored as separate rows.
    """
    raw = "|".join(
        [card, txn_date, posted, description, debit, credit, str(occurrence)]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    # Last resort: latin-1 never fails (every byte is a code point).
    return raw.decode("latin-1")


def import_csv(
    session: Session, path: Path, currency_code: str = DEFAULT_CURRENCY_CODE
) -> ImportResult:
    path = Path(path)
    reader = csv.DictReader(io.StringIO(_read_text(path)))
    fieldnames = reader.fieldnames or []
    missing = [c for c in EXPECTED_COLUMNS if c not in fieldnames]
    if missing:
        raise ValueError(
            f"CSV is missing expected columns {missing}; found {fieldnames}."
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

    for row in rows:
        card = (row.get("Card No.") or "").strip()
        txn_date = (row.get("Transaction Date") or "").strip()
        posted = (row.get("Posted Date") or "").strip()
        description = (row.get("Description") or "").strip()
        bank_category = (row.get("Category") or "").strip()
        debit = (row.get("Debit") or "").strip()
        credit = (row.get("Credit") or "").strip()

        key = (card, txn_date, posted, description, debit, credit)
        occurrence = occurrence_counter.get(key, 0)
        occurrence_counter[key] = occurrence + 1
        import_hash = _row_hash(
            card, txn_date, posted, description, debit, credit, occurrence
        )

        exists = session.scalar(
            select(Transaction.id).where(Transaction.import_hash == import_hash)
        )
        if exists is not None:
            skipped += 1
            continue

        account = _get_or_create_account(
            session, f"Card {card}" if card else "Unknown", currency
        )
        account_ids.add(account.id)

        vendor = _get_or_create_vendor(session, description) if description else None

        category = None
        category_source = "unset"
        if bank_category:
            category = _get_or_create_category(session, bank_category)
            category_source = "import"

        date_str = posted or txn_date
        posted_date = datetime.strptime(date_str, "%Y-%m-%d").date()

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
                value_minor=_parse_amount_minor(debit, credit, currency.decimal_places),
                category_source=category_source,
                import_hash=import_hash,
            )
        )
        inserted += 1

    # Attribute the import to a single account when the file only had one.
    if len(account_ids) == 1:
        import_record.account_id = next(iter(account_ids))

    session.commit()
    return ImportResult(
        source_file=path.name,
        total_rows=len(rows),
        inserted=inserted,
        skipped_duplicates=skipped,
    )
