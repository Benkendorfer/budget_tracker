"""Detect money moved between your own accounts.

A transfer shows up twice: once leaving one account and once arriving in another. Both
legs are real transactions, but counting them as spending and income double-counts money
that never left your control.

Two transactions are paired when they have the same amount with opposite signs, sit in
different accounts, and post within ``window_days`` of each other. Paired legs share a
``transfer_group_id``, are categorised as ``Transfer``, and are left out of the
inflow/outflow totals. Same-account pairing is off by default and opt-in via
``allow_same_account``, for the case where several sub-accounts of one provider are
tracked here as a single account.

Matching is by amount and date alone, so two unrelated transactions of the same size a
day apart can be paired by mistake. Detection is therefore reversible with
:func:`clear_transfers`, and never overwrites a category you set by hand.

A transfer that cost a fee (Wise is the common case: the outflow and inflow legs are
different amounts) never auto-pairs, since detection matches on equal amounts.
:func:`mark_manual_transfer` pairs such a pair by hand, splitting the fee off into its
own ordinary spending transaction so the transfer legs themselves still cancel exactly.
:func:`unmark_manual_transfer` undoes it, folding the fee back in.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Category, Currency, Transaction

TRANSFER_CATEGORY = "Transfer"
# Marks a category this module assigned, so clearing can undo exactly its own work.
TRANSFER_SOURCE = "transfer"
MANUAL = "manual"
DEFAULT_WINDOW_DAYS = 5

# A pair stamped by mark_manual_transfer rather than detect_transfers -- see that
# function's docstring. categories.PROTECTED_SOURCES includes this so a category rule
# never overwrites it, and clear_transfers skips it so `transfers reset` cannot undo a
# pairing the user made by hand.
MANUAL_TRANSFER_SOURCE = "manual-transfer"
# The ordinary (kind="tag") tag mark_manual_transfer puts on both legs, so they are
# findable (and groupable) independently of the category.
MANUAL_TRANSFER_TAG = "manual-transfer"
# Category the split-off fee/difference row lands in.
FEE_CATEGORY = "Fees"


def _get_or_create_transfer_category(session: Session) -> Category:
    category = session.scalar(
        select(Category).where(
            Category.parent_id.is_(None), Category.value == TRANSFER_CATEGORY
        )
    )
    if category is None:
        category = Category(value=TRANSFER_CATEGORY)
        session.add(category)
        session.flush()
    return category


# Anchored at the start; anything (a fee note, in Wise's case) may follow. Amounts can
# carry thousands separators, which is why the digits are stripped before parsing.
_CONVERSION_RE = re.compile(
    r"^Converted ([\d,]+(?:\.\d+)?) ([A-Z]{3}) to ([\d,]+(?:\.\d+)?) ([A-Z]{3})"
)


def _parse_conversion(
    description: Optional[str],
) -> Optional[Tuple[Decimal, str, Decimal, str]]:
    if not description:
        return None
    match = _CONVERSION_RE.match(description)
    if match is None:
        return None
    from_amount = Decimal(match.group(1).replace(",", ""))
    to_amount = Decimal(match.group(3).replace(",", ""))
    return from_amount, match.group(2), to_amount, match.group(4)


def _minor(amount: Decimal) -> int:
    return int((amount * 100).to_integral_value())


def pair_conversions(session: Session) -> int:
    """Pair currency-conversion legs that amount-matching alone cannot find. No commit.

    A currency conversion (Wise's statement wording is the model here: "Converted
    3,000.00 USD to 2,431.14 CHF") posts as two unlinked rows, one per balance, each in
    its own currency. :func:`detect_transfers` only pairs equal amounts in the same
    currency, so it never finds these, and a fee taken out of the source leg (the FROM
    amount minus the fee leaves the account, not the FROM amount itself) means the two
    legs would not match on amount even if currency were ignored.

    Candidates are unpaired rows whose description starts with that wording. A pair is
    one negative row in the FROM currency and one positive row in the TO currency whose
    value equals the parsed TO amount exactly, in different accounts, posted within a
    day of each other, both parsing to the same (from amount, from currency, to amount,
    to currency) — matched on the description, not the outflow's amount, since the fee
    makes that amount unrecoverable from the pair alone. Closest date first, ids as
    tie-break, each row used once, same as :func:`detect_transfers`.

    A fee posted as its own row (Wise: "Wise Charges for: ...") does not match this
    wording and is left as ordinary spending.
    """
    currency_codes = {c.id: c.value for c in session.scalars(select(Currency))}

    outflow_candidates: List[Tuple[Decimal, str, Decimal, str, Transaction]] = []
    inflow_candidates: List[Tuple[Decimal, str, Decimal, str, Transaction]] = []
    for txn in session.scalars(
        select(Transaction).where(Transaction.transfer_group_id.is_(None))
    ):
        if not txn.value_minor:
            continue
        parsed = _parse_conversion(txn.description)
        if parsed is None:
            continue
        from_amount, from_code, to_amount, to_code = parsed
        code = currency_codes.get(txn.currency_id)
        if txn.value_minor < 0 and code == from_code:
            outflow_candidates.append((from_amount, from_code, to_amount, to_code, txn))
        elif txn.value_minor > 0 and code == to_code and txn.value_minor == _minor(to_amount):
            inflow_candidates.append((from_amount, from_code, to_amount, to_code, txn))

    candidates = []
    for from_amount, from_code, to_amount, to_code, outflow in outflow_candidates:
        for ia, ic, ta, tc, inflow in inflow_candidates:
            if outflow.id == inflow.id or outflow.account_id == inflow.account_id:
                continue
            if (from_amount, from_code, to_amount, to_code) != (ia, ic, ta, tc):
                continue
            delta = abs((inflow.posted_date - outflow.posted_date).days)
            if delta <= 1:
                candidates.append((delta, outflow.id, inflow.id, outflow, inflow))
    candidates.sort(key=lambda c: c[:3])

    category: Optional[Category] = None
    used = set()
    pairs = 0
    for _, _, _, outflow, inflow in candidates:
        if outflow.id in used or inflow.id in used:
            continue
        if category is None:
            category = _get_or_create_transfer_category(session)
        group_id = min(outflow.id, inflow.id)
        for leg in (outflow, inflow):
            leg.transfer_group_id = group_id
            if leg.category_source != MANUAL:
                leg.category_id = category.id
                leg.category_source = TRANSFER_SOURCE
        used.update((outflow.id, inflow.id))
        pairs += 1

    session.flush()
    return pairs


def detect_transfers(
    session: Session,
    window_days: int = DEFAULT_WINDOW_DAYS,
    allow_same_account: bool = False,
) -> int:
    """Pair up unpaired transactions. Returns the number of pairs found. No commit.

    Already-paired transactions are left alone, so running this repeatedly is safe and
    only ever finds newly imported pairs. Starts by calling :func:`pair_conversions`,
    which catches currency conversions this function's amount-matching cannot, and
    whose pairs are folded into the count returned.

    ``allow_same_account`` opts into pairing two legs that sit in the *same* account —
    useful when several sub-accounts of one provider are tracked as a single account here,
    so a move between them never has a different ``account_id`` to pair across. It
    defaults to off: relaxing the check makes an accidental same-account pairing (two
    unrelated same-size transactions a few days apart) more likely, and a false pairing
    silently removes two real transactions from the user's totals — worse than leaving a
    real transfer undetected.
    """
    conversion_pairs = pair_conversions(session)

    unpaired = list(
        session.scalars(
            select(Transaction)
            .where(Transaction.transfer_group_id.is_(None))
            .order_by(Transaction.posted_date, Transaction.id)
        )
    )
    if not unpaired:
        return conversion_pairs

    # Keyed by currency as well as size: 2,000 USD arriving and 2,000 CHF leaving are not
    # the same money, however equal the numbers look. Bucketing on the number alone once
    # paired exactly that (a Wise USD top-up with a CHF payment to someone else) and
    # dropped both real transactions from the totals.
    by_amount: Dict[Tuple[int, int], List[Transaction]] = defaultdict(list)
    for txn in unpaired:
        if txn.value_minor:  # a zero-value row would "match" every other zero
            by_amount[(txn.currency_id, abs(txn.value_minor))].append(txn)

    category: Optional[Category] = None
    used = set()
    pairs = 0

    for amount in sorted(by_amount):
        group = by_amount[amount]
        outflows = [t for t in group if t.value_minor < 0]
        inflows = [t for t in group if t.value_minor > 0]
        if not outflows or not inflows:
            continue
        # Score every legal pairing, then take them closest-first. Walking outflows in
        # order instead would let an early one claim an inflow that is a same-day match
        # for a later outflow; ids break ties so the result is deterministic.
        candidates = []
        for outflow in outflows:
            for inflow in inflows:
                if outflow.id == inflow.id:
                    continue  # a txn can never pair with itself, same-account or not
                if inflow.account_id == outflow.account_id and not allow_same_account:
                    continue
                delta = abs((inflow.posted_date - outflow.posted_date).days)
                if delta <= window_days:
                    candidates.append((delta, outflow.id, inflow.id, outflow, inflow))
        candidates.sort(key=lambda c: c[:3])

        for _, _, _, outflow, inflow in candidates:
            if outflow.id in used or inflow.id in used:
                continue

            if category is None:
                category = _get_or_create_transfer_category(session)
            group_id = min(outflow.id, inflow.id)
            for leg in (outflow, inflow):
                leg.transfer_group_id = group_id
                # A category you chose by hand outranks anything inferred here.
                if leg.category_source != MANUAL:
                    leg.category_id = category.id
                    leg.category_source = TRANSFER_SOURCE
            used.update((outflow.id, inflow.id))
            pairs += 1

    session.flush()
    return pairs + conversion_pairs


def clear_transfers(session: Session) -> int:
    """Un-pair every detected transfer. Returns how many transactions were reset.

    Categories this module set are cleared; ones set manually or by the import are left
    as they are. A pair made by :func:`mark_manual_transfer` is skipped entirely -- not
    just its category -- so `transfers reset` can never undo a pairing the user made by
    hand; :func:`unmark_manual_transfer` is the only way back for those. The same goes
    for rows excluded with :func:`exclude`, which only :func:`include` reverses.
    """
    paired = list(
        session.scalars(
            select(Transaction).where(
                Transaction.transfer_group_id.is_not(None),
                Transaction.category_source != MANUAL_TRANSFER_SOURCE,
                Transaction.id.not_in(_excluded_ids_query()),
            )
        )
    )
    for txn in paired:
        txn.transfer_group_id = None
        if txn.category_source == TRANSFER_SOURCE:
            txn.category_id = None
            txn.category_source = "unset"
    session.flush()
    return len(paired)


class ManualTransferError(ValueError):
    """``txn_ids`` do not describe a pairable manual transfer."""


@dataclass
class ManualTransferResult:
    """What :func:`mark_manual_transfer` did, for the caller to report."""

    group_id: int
    outflow_id: int
    inflow_id: int
    outflow_account: str
    inflow_account: str
    outflow_minor: int  # before any fee split
    inflow_minor: int  # before any fee split
    difference_minor: Optional[int]  # None when the legs are in different currencies
    currency: str  # the outflow's currency code
    fee_txn_id: Optional[int] = None
    fee_account: Optional[str] = None


def mark_manual_transfer(
    session: Session, txn_ids: Sequence[int]
) -> ManualTransferResult:
    """Pair two transactions as a transfer by hand. No commit.

    For the case :func:`detect_transfers` cannot find on its own: a transfer that cost
    a fee, so the outflow and inflow legs are different amounts and never match on
    amount alone (Wise is the common case -- send $1,000.00, $995.00 arrives).

    Requires exactly two transactions, one negative (outflow) and one positive
    (inflow), neither already grouped; anything else raises :class:`ManualTransferError`
    with a message meant to be shown to the user as-is.

    **The fee stays spending.** When both legs share a currency and their amounts do
    not already cancel, the difference is split off the larger-magnitude leg into a new
    transaction of its own -- category ``Fees``, *not* part of the transfer group -- so
    the two transfer legs cancel exactly (an account's balance still foots) while the fee
    itself keeps counting as an expense. A leg that sent less than was received (a
    positive difference) splits the excess off the inflow leg the same way, labelled
    "Transfer difference" instead of "Transfer fee", but is otherwise identical.
    Different currencies are paired with no split at all -- there is no single number to
    call "the fee" without a conversion this function has no business doing -- and
    ``difference_minor`` comes back ``None`` to say so.

    Both legs are stamped ``transfer_group_id`` (the lower of the two ids), category
    ``Transfer`` with ``category_source`` :data:`MANUAL_TRANSFER_SOURCE` (so a category
    rule never overwrites them -- see ``categories.PROTECTED_SOURCES`` -- and
    :func:`clear_transfers` leaves them alone), and the ordinary tag
    :data:`MANUAL_TRANSFER_TAG`.
    """
    from . import categories, tags
    from .importer import _get_or_create_vendor

    txns = list(session.scalars(select(Transaction).where(Transaction.id.in_(txn_ids))))
    if len(txns) != 2:
        raise ManualTransferError(
            f"A manual transfer pairs exactly two transactions; {len(txns)} given."
        )
    if any(t.transfer_group_id is not None for t in txns):
        raise ManualTransferError(
            "One of these transactions is already part of a transfer."
        )
    outflows = [t for t in txns if t.value_minor < 0]
    inflows = [t for t in txns if t.value_minor > 0]
    if len(outflows) != 1 or len(inflows) != 1:
        raise ManualTransferError(
            "A manual transfer needs one outflow and one inflow, not two of the same "
            "sign (or a zero-value transaction)."
        )
    outflow, inflow = outflows[0], inflows[0]

    # Captured before any split below rewrites them.
    outflow_minor = outflow.value_minor
    inflow_minor = inflow.value_minor

    fee_txn_id: Optional[int] = None
    fee_account: Optional[str] = None
    difference_minor: Optional[int] = None

    if outflow.currency_id == inflow.currency_id:
        # Negative: the outflow leg is the larger-magnitude one, i.e. a fee was taken
        # out in transit. Positive: the inflow leg is larger -- more arrived than was
        # sent. Zero: the legs already cancel and there is nothing to split.
        difference_minor = outflow_minor + inflow_minor
        if difference_minor != 0:
            if difference_minor < 0:
                leg, description = outflow, "Transfer fee"
            else:
                leg, description = inflow, "Transfer difference"
            # Subtracting the difference from the split leg is what makes leg + fee
            # equal the original amount regardless of sign -- e.g. -1000 split by -5
            # leaves a -995 leg and a -5 fee; 1005 split by 5 leaves a 1000 leg and a 5
            # fee.
            leg.value_minor -= difference_minor
            fee_category = categories.get_or_create(session, FEE_CATEGORY)
            vendor = _get_or_create_vendor(session, description)
            fee = Transaction(
                account_id=leg.account_id,
                currency_id=leg.currency_id,
                import_id=leg.import_id,
                vendor_id=vendor.id,
                posted_date=leg.posted_date,
                description=description,
                raw_description=description,
                value_minor=difference_minor,
                category_id=fee_category.id,
                category_source=categories.MANUAL,
                # Deterministic and derived from the leg's own hash, so undo can find
                # it again (see unmark_manual_transfer) and deleting the leg's import
                # takes the fee with it (same import_id -> same delete_import query).
                import_hash=f"{leg.import_hash}:fee",
            )
            session.add(fee)
            session.flush()
            fee_txn_id = fee.id
            fee_account = leg.account.name

    group_id = min(outflow.id, inflow.id)
    category = _get_or_create_transfer_category(session)
    for leg in (outflow, inflow):
        leg.transfer_group_id = group_id
        leg.category_id = category.id
        leg.category_source = MANUAL_TRANSFER_SOURCE
    tags.add_tag(session, [outflow.id, inflow.id], MANUAL_TRANSFER_TAG)
    session.flush()

    currency = session.get(Currency, outflow.currency_id)
    return ManualTransferResult(
        group_id=group_id,
        outflow_id=outflow.id,
        inflow_id=inflow.id,
        outflow_account=outflow.account.name,
        inflow_account=inflow.account.name,
        outflow_minor=outflow_minor,
        inflow_minor=inflow_minor,
        difference_minor=difference_minor,
        currency=currency.value,
        fee_txn_id=fee_txn_id,
        fee_account=fee_account,
    )


def unmark_manual_transfer(session: Session, txn_ids: Sequence[int]) -> int:
    """Undo :func:`mark_manual_transfer`. Returns how many legs were restored.

    Only rows carrying :data:`MANUAL_TRANSFER_SOURCE` count as a manual-transfer leg;
    anything else in ``txn_ids`` is ignored, so this returns 0 if none of them is one.
    Passing just one leg of a pair still restores both -- a group this module made
    always has exactly two members, so there is nothing a partial undo could mean.

    Each leg is un-grouped, its category cleared back to ``unset`` (handing it back to
    rules or a manual re-categorisation, same as :func:`clear_transfers`), and the
    manual-transfer tag removed. A fee or difference row split off by the original call
    is folded back into its leg's amount and deleted, found by its ``":fee"``-suffixed
    import hash.
    """
    from . import categories, tags

    legs = list(
        session.scalars(
            select(Transaction).where(
                Transaction.id.in_(txn_ids),
                Transaction.category_source == MANUAL_TRANSFER_SOURCE,
            )
        )
    )
    if not legs:
        return 0

    group_ids = {t.transfer_group_id for t in legs if t.transfer_group_id is not None}
    group = list(
        session.scalars(
            select(Transaction).where(Transaction.transfer_group_id.in_(group_ids))
        )
    )

    for leg in group:
        fee = session.scalar(
            select(Transaction).where(Transaction.import_hash == f"{leg.import_hash}:fee")
        )
        if fee is not None:
            leg.value_minor += fee.value_minor
            session.delete(fee)
        leg.transfer_group_id = None
        leg.category_id = None
        leg.category_source = categories.UNSET

    tags.remove_tag(session, [leg.id for leg in group], MANUAL_TRANSFER_TAG)
    session.flush()
    return len(group)


# ------------------------------------------------------------------ manual exclusion

EXCLUDED_TAG = "excluded"


def _excluded_ids_query():
    """Ids of every transaction carrying the ``excluded`` tag, as a subquery."""
    from .models import Tag, TransactionTag
    from .tags import TAG

    return (
        select(TransactionTag.transaction_id)
        .join(Tag, Tag.id == TransactionTag.tag_id)
        .where(Tag.name == EXCLUDED_TAG, Tag.kind == TAG)
    )


def exclude(session: Session, txn_ids: Sequence[int]) -> int:
    """Leave ``txn_ids`` out of every income and spending figure. Returns rows excluded.

    For money that is neither, but that no pairing can describe: an in-kind ACATS move
    of securities, valued differently by each broker and sometimes arriving from an
    account that is not tracked here at all.

    Each row becomes a transfer group of one -- the shape a Wise conversion already has
    -- because "not real income or spending" is a single rule the totals, statistics,
    chart, pie and trips all apply to ``transfer_group_id``. Reusing it means every
    figure agrees without each of them having to learn a second flag. The ``excluded``
    tag marks which groups of one are the user's, so :func:`include` and
    :func:`clear_transfers` can tell them from a conversion. The category is left
    alone: excluding a row is a decision about the totals, not a claim about what it is.

    Rows already in a transfer group (paired, or already excluded) are left as they are.
    No commit.
    """
    from . import tags

    rows = [
        txn
        for txn in session.scalars(select(Transaction).where(Transaction.id.in_(txn_ids)))
        if txn.transfer_group_id is None
    ]
    for txn in rows:
        txn.transfer_group_id = txn.id
    tags.add_tag(session, [txn.id for txn in rows], EXCLUDED_TAG)
    session.flush()
    return len(rows)


def include(session: Session, txn_ids: Sequence[int]) -> int:
    """Undo :func:`exclude` for ``txn_ids``. Returns rows put back. No commit.

    Only rows :func:`exclude` made are touched: a detected or manual transfer, or a
    Wise conversion, among ``txn_ids`` keeps its pairing.
    """
    from . import tags

    excluded = set(session.scalars(_excluded_ids_query()))
    rows = [
        txn
        for txn in session.scalars(select(Transaction).where(Transaction.id.in_(txn_ids)))
        if txn.id in excluded and txn.transfer_group_id == txn.id
    ]
    for txn in rows:
        txn.transfer_group_id = None
    tags.remove_tag(session, [txn.id for txn in rows], EXCLUDED_TAG)
    session.flush()
    return len(rows)
