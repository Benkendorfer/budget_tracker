"""Detect money moved between your own accounts.

A transfer shows up twice: once leaving one account and once arriving in another. Both
legs are real transactions, but counting them as spending and income double-counts money
that never left your control.

Two transactions are paired when they have the same amount with opposite signs, sit in
different accounts, and post within ``window_days`` of each other. Paired legs share a
``transfer_group_id``, are categorised as ``Transfer``, and are left out of the
inflow/outflow totals.

Matching is by amount and date alone, so two unrelated transactions of the same size a
day apart can be paired by mistake. Detection is therefore reversible with
:func:`clear_transfers`, and never overwrites a category you set by hand.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Category, Transaction

TRANSFER_CATEGORY = "Transfer"
# Marks a category this module assigned, so clearing can undo exactly its own work.
TRANSFER_SOURCE = "transfer"
MANUAL = "manual"
DEFAULT_WINDOW_DAYS = 5


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


def detect_transfers(
    session: Session, window_days: int = DEFAULT_WINDOW_DAYS
) -> int:
    """Pair up unpaired transactions. Returns the number of pairs found. No commit.

    Already-paired transactions are left alone, so running this repeatedly is safe and
    only ever finds newly imported pairs.
    """
    unpaired = list(
        session.scalars(
            select(Transaction)
            .where(Transaction.transfer_group_id.is_(None))
            .order_by(Transaction.posted_date, Transaction.id)
        )
    )
    if not unpaired:
        return 0

    by_amount: Dict[int, List[Transaction]] = defaultdict(list)
    for txn in unpaired:
        if txn.value_minor:  # a zero-value row would "match" every other zero
            by_amount[abs(txn.value_minor)].append(txn)

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
                if inflow.account_id == outflow.account_id:
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
    return pairs


def clear_transfers(session: Session) -> int:
    """Un-pair every detected transfer. Returns how many transactions were reset.

    Categories this module set are cleared; ones set manually or by the import are left
    as they are.
    """
    paired = list(
        session.scalars(
            select(Transaction).where(Transaction.transfer_group_id.is_not(None))
        )
    )
    for txn in paired:
        txn.transfer_group_id = None
        if txn.category_source == TRANSFER_SOURCE:
            txn.category_id = None
            txn.category_source = "unset"
    session.flush()
    return len(paired)
