"""Reader for Wise's transfer-log CSV export.

Every other format this app reads is a *ledger*: one row is one line of one account, in
one currency, with one signed amount. Wise exports a *transfer log* instead — one row is
one transfer, with a source side and a target side, each carrying its own amount and
currency, plus the exchange rate and any fee. That does not fit :class:`~.formats.FormatSpec`
at all, so this module plans the rows itself and :func:`~.importer.import_wise_csv`
writes them.

Planning is pure: rows in, :class:`PlannedTxn` out, no session and no database. All the
awkward decisions — which side of a transfer belongs to you, when a fee is real money,
which rows are the same transfer seen twice — are therefore testable without importing
anything.

Three rules do most of the work:

**The account is derived from the row's own currency**, never asked for. A file that
mixes currencies splits itself into ``Wise USD`` / ``Wise CHF`` / ``Wise EUR``, which is
what keeps every downstream total single-currency and correct.

**Only the leg that is yours is recorded, in that leg's currency.** For ``OUT`` and
``NEUTRAL`` that is the source side; for ``IN`` the source is somebody else, so it is the
target side. A conversion is one row on the source account, not two — the money arriving
in the other currency is deliberately not recorded (see ``is_transfer`` below).

**A ``NEUTRAL`` row is a conversion between your own balances, and is flagged as a
transfer** so it does not count as spending. Otherwise converting USD to CHF and then
spending the CHF would count the same money twice. Note that "the currencies differ" is
*not* the test: 14 of the ``OUT`` rows in a real export are cross-currency payments to
other people, which are ordinary spending and must stay in the figures.

Fees are charged on top of the amount, not taken out of it (verified against real
exports: ``source × rate == target`` to the cent, with the fee separate). So a row's main
leg plus its fee leg sum to exactly what left the balance, and the fee stays visible as
its own transaction instead of being quietly absorbed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Sequence

# Columns that identify the layout. Deliberately a subset: Wise has changed the tail of
# this export before (Batch, Created by, Note), and a signature that insists on every
# column would reject a file this reader can read perfectly well.
SIGNATURE = (
    "ID",
    "Direction",
    "Source amount (after fees)",
    "Source currency",
    "Target amount (after fees)",
    "Target currency",
)

# Only completed transfers moved money. A refunded one nets to nothing, and importing it
# would show a payment that was undone.
COMPLETED = "COMPLETED"

IN, OUT, NEUTRAL = "IN", "OUT", "NEUTRAL"

# The category fees are filed under, so "what is moving money costing me" is one filter
# away. Distinct from the bank's own Category column, which fees do not get.
FEE_CATEGORY = "Fees"

# Distinguishes the two transactions one row can produce, so each gets its own dedup key.
MAIN_LEG = "main"
FEE_LEG = "fee"

DEFAULT_ACCOUNT_PREFIX = "Wise"


def looks_like_wise(fieldnames: Sequence[str]) -> bool:
    """Whether a CSV header is this export, judged on the signature columns alone."""
    present = {name.strip() for name in fieldnames or ()}
    return all(column in present for column in SIGNATURE)


@dataclass(frozen=True)
class PlannedTxn:
    """One transaction a row implies, before anything touches the database.

    ``amount`` is a signed :class:`~decimal.Decimal` in major units — the reader has no
    business knowing a currency's minor-unit scale, so the importer converts.
    """

    external_id: str  # the export's own transfer ID
    leg: str  # MAIN_LEG or FEE_LEG; part of what makes the dedup key unique
    currency: str  # ISO code — also picks the account
    amount: Decimal  # signed: negative leaves the account
    posted_date: date
    description: str
    is_transfer: bool  # a conversion between your own balances
    category: Optional[str] = None
    # Only set on a conversion, and only for information: the other side of the trade,
    # which is what makes the historical rate recoverable later.
    counter_currency: Optional[str] = None
    counter_amount: Optional[Decimal] = None
    exchange_rate: Optional[Decimal] = None

    @property
    def dedup_key(self) -> str:
        """Stable across files, and unique per leg *and per currency*.

        A conversion appears in both balances' exports byte for byte, so keying on the
        transfer's own ID is what stops it importing twice. The currency has to be in the
        key as well, though, because one ID does not always mean one transaction: when a
        card payment is short in the currency it is charged in, Wise funds it from two
        balances at once and each balance's export shows *its own* share, under the same
        ID. Keying on the ID alone would silently drop one of the two real legs — and,
        worse, could keep one balance's main leg beside the other's fee.
        """
        return f"{self.external_id}:{self.leg}:{self.currency}"


@dataclass(frozen=True)
class ObservedRate:
    """An exchange rate the export witnessed, harvested from a conversion.

    Worth keeping because it is a real rate you actually got on a real day, which is a
    better basis for converting that day's other transactions than any rate typed in
    later from memory.
    """

    day: date
    base: str  # one unit of this...
    quote: str  # ...bought this many of this
    rate: Decimal


@dataclass(frozen=True)
class WisePlan:
    transactions: List[PlannedTxn]
    rates: List[ObservedRate]
    skipped_incomplete: int  # rows that never completed, so never moved money
    currencies: List[str]  # every currency seen, sorted — one account each


def _decimal(text: str) -> Decimal:
    text = (text or "").strip()
    if not text:
        return Decimal(0)
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ValueError(f"Could not read {text!r} as an amount.") from None


def _parse_day(row: Dict[str, str]) -> date:
    """When the money moved, falling back to when the transfer was created.

    "Finished on" is the honest date — a transfer created on the 1st and completed on the
    3rd belongs to the 3rd — but it is empty while a transfer is still in flight, and a
    row with no date at all cannot be imported.
    """
    for column in ("Finished on", "Created on"):
        text = (row.get(column) or "").strip()
        if not text:
            continue
        # "2025-12-02 17:34:31"; the app stores dates, not times.
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(text).date()
        except ValueError:
            continue
    raise ValueError("Row carries neither a 'Finished on' nor a 'Created on' date.")


def _cell(row: Dict[str, str], column: str) -> str:
    return (row.get(column) or "").strip()


def _describe(row: Dict[str, str], direction: str) -> str:
    """What the row should read as in the transactions table.

    Named after the other party where there is one, since that is what a vendor rule will
    want to match on. A conversion has no other party, so it is named for what it did.
    """
    if direction == NEUTRAL:
        return (
            f"Currency conversion {_cell(row, 'Source currency')} → "
            f"{_cell(row, 'Target currency')}"
        )
    counterparty = (
        _cell(row, "Source name") if direction == IN else _cell(row, "Target name")
    )
    return counterparty or _cell(row, "Reference") or f"Wise {direction.lower()}"


def plan_row(row: Dict[str, str]) -> List[PlannedTxn]:
    """The transactions one row implies: its own leg, plus a fee leg when it charged one."""
    direction = _cell(row, "Direction").upper()
    if direction not in (IN, OUT, NEUTRAL):
        raise ValueError(
            f"Unknown Direction {direction!r}; expected one of {IN}, {OUT}, {NEUTRAL}."
        )

    external_id = _cell(row, "ID")
    if not external_id:
        raise ValueError("Row carries no ID, so it cannot be de-duplicated.")

    day = _parse_day(row)
    source_currency = _cell(row, "Source currency")
    target_currency = _cell(row, "Target currency")
    description = _describe(row, direction)
    # The export's own spending category. Fees get FEE_CATEGORY instead.
    category = _cell(row, "Category") or None

    if direction == IN:
        # The source is somebody else's account, so the leg that is yours is the target.
        currency = target_currency
        amount = _decimal(_cell(row, "Target amount (after fees)"))
    else:
        currency = source_currency
        amount = -_decimal(_cell(row, "Source amount (after fees)"))

    planned = [
        PlannedTxn(
            external_id=external_id,
            leg=MAIN_LEG,
            currency=currency,
            amount=amount,
            posted_date=day,
            description=description,
            is_transfer=direction == NEUTRAL,
            category=category,
            counter_currency=target_currency if direction == NEUTRAL else None,
            counter_amount=(
                _decimal(_cell(row, "Target amount (after fees)"))
                if direction == NEUTRAL
                else None
            ),
            exchange_rate=(
                _decimal(_cell(row, "Exchange rate")) if direction == NEUTRAL else None
            ),
        )
    ]

    fee = _decimal(_cell(row, "Source fee amount"))
    if fee:
        # Charged on top of the amount rather than taken out of it, so this does not
        # double-count: main leg + fee leg is exactly what left the balance. It is filed
        # against the fee's *own* currency, which is what keeps it on the right account
        # when a conversion is charged in the currency being sold.
        planned.append(
            PlannedTxn(
                external_id=external_id,
                leg=FEE_LEG,
                currency=_cell(row, "Source fee currency") or source_currency,
                amount=-fee,
                posted_date=day,
                description=f"Wise fee — {description}",
                is_transfer=False,  # a fee is real spending, never a transfer
                category=FEE_CATEGORY,
            )
        )
    return planned


def plan_rows(rows: Sequence[Dict[str, str]]) -> WisePlan:
    """Plan a whole export, dropping incomplete transfers and rows already seen.

    De-duplication happens here as well as against the database, because a conversion
    appears in *both* balances' exports and a single import run can be handed both files.
    """
    transactions: List[PlannedTxn] = []
    rates: List[ObservedRate] = []
    seen = set()
    skipped_incomplete = 0

    for row in rows:
        if not _cell(row, "ID"):
            continue  # a trailing blank line, not a transfer
        status = _cell(row, "Status").upper()
        if status and status != COMPLETED:
            skipped_incomplete += 1
            continue

        for planned in plan_row(row):
            if planned.dedup_key in seen:
                continue
            seen.add(planned.dedup_key)
            transactions.append(planned)
            if (
                planned.exchange_rate
                and planned.counter_currency
                and planned.counter_currency != planned.currency
            ):
                rates.append(
                    ObservedRate(
                        day=planned.posted_date,
                        base=planned.currency,
                        quote=planned.counter_currency,
                        rate=planned.exchange_rate,
                    )
                )

    currencies = sorted({planned.currency for planned in transactions})
    return WisePlan(
        transactions=transactions,
        rates=rates,
        skipped_incomplete=skipped_incomplete,
        currencies=currencies,
    )


def account_name(currency: str, prefix: str = DEFAULT_ACCOUNT_PREFIX) -> str:
    """``Wise CHF``. One account per currency, so every total stays single-currency."""
    return f"{prefix} {currency}".strip()
