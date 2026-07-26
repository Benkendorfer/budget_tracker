"""Read-side queries for the UI.

These return plain dataclasses (never ORM objects tied to a live session) so the
TUI can render without worrying about session lifetime.

A vendor filter is a ``(kind, id)`` tuple: ``("name", vendor_name_id)`` matches every
raw vendor that overrides to that name (aggregation), while ``("raw", vendor_id)``
matches a single un-overridden vendor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    Account,
    Category,
    Currency,
    Transaction,
    Vendor,
    VendorName,
    VendorRule,
)

VendorFilter = Tuple[str, int]


@dataclass
class AccountRow:
    id: int
    name: str
    currency: str
    count: int


@dataclass
class VendorRow:
    kind: str  # "name" (override group) or "raw" (single un-overridden vendor)
    id: int
    name: str
    count: int


@dataclass
class CategoryRow:
    id: int
    name: str
    count: int
    total_minor: int


@dataclass
class TxnRow:
    id: int
    posted_date: str
    description: str
    vendor: str  # effective (display) name — the override if there is one
    vendor_raw: str  # the raw merchant string, which overrides are keyed on
    category: str
    amount_minor: int
    currency: str


@dataclass
class RuleRow:
    id: int
    pattern: str
    name: str
    vendor_count: int  # raw vendors this rule currently names


@dataclass
class Totals:
    count: int  # every matching transaction, transfers included
    net_minor: int
    outflow_minor: int
    inflow_minor: int
    transfer_count: int = 0  # of `count`, how many were excluded from the money figures


def get_accounts(session: Session) -> List[AccountRow]:
    rows = session.execute(
        select(
            Account.id,
            Account.name,
            Currency.value,
            func.count(Transaction.id),
        )
        .join(Currency, Currency.id == Account.currency_id)
        .join(Transaction, Transaction.account_id == Account.id, isouter=True)
        .group_by(Account.id)
        .order_by(Account.name)
    ).all()
    return [AccountRow(id=r[0], name=r[1], currency=r[2], count=r[3]) for r in rows]


def get_vendors(session: Session) -> List[VendorRow]:
    """Effective vendor list: override groups plus un-overridden raw vendors."""
    rows: List[VendorRow] = []

    overridden = session.execute(
        select(VendorName.id, VendorName.value, func.count(Transaction.id))
        .select_from(VendorName)
        .join(Vendor, Vendor.vendor_name_id == VendorName.id)
        .join(Transaction, Transaction.vendor_id == Vendor.id)
        .group_by(VendorName.id)
    ).all()
    rows.extend(VendorRow(kind="name", id=r[0], name=r[1], count=r[2]) for r in overridden)

    raw = session.execute(
        select(Vendor.id, Vendor.name, func.count(Transaction.id))
        .join(Transaction, Transaction.vendor_id == Vendor.id)
        .where(Vendor.vendor_name_id.is_(None))
        .group_by(Vendor.id)
    ).all()
    rows.extend(VendorRow(kind="raw", id=r[0], name=r[1], count=r[2]) for r in raw)

    rows.sort(key=lambda v: (-v.count, v.name.lower()))
    return rows


def get_categories(session: Session) -> List[CategoryRow]:
    rows = session.execute(
        select(
            Category.id,
            Category.value,
            func.count(Transaction.id),
            func.coalesce(func.sum(Transaction.value_minor), 0),
        )
        .join(Transaction, Transaction.category_id == Category.id)
        .group_by(Category.id)
        .order_by(func.sum(Transaction.value_minor))
    ).all()
    return [
        CategoryRow(id=r[0], name=r[1], count=r[2], total_minor=r[3]) for r in rows
    ]


def get_rules(session: Session) -> List[RuleRow]:
    """Vendor rules with the number of raw vendors each one currently names.

    Attribution mirrors :func:`vendors.apply_rules` — first match in ``id`` order wins —
    so two rules pointing at the same display name are still counted separately.
    """
    from .vendors import RULE, matches  # local import keeps the dependency one-way

    rules = list(session.scalars(select(VendorRule).order_by(VendorRule.id)))
    counts = {rule.id: 0 for rule in rules}
    owned = session.scalars(select(Vendor).where(Vendor.vendor_name_source == RULE))
    for vendor in owned:
        match = next((r for r in rules if matches(r.pattern, vendor.name)), None)
        if match is not None:
            counts[match.id] += 1
    return [
        RuleRow(
            id=rule.id,
            pattern=rule.pattern,
            name=rule.vendor_name.value,
            vendor_count=counts[rule.id],
        )
        for rule in rules
    ]


def _txn_query(
    account_id: Optional[int],
    category_id: Optional[int],
    vendor_filter: Optional[VendorFilter],
):
    query = select(Transaction)
    if account_id is not None:
        query = query.where(Transaction.account_id == account_id)
    if category_id is not None:
        query = query.where(Transaction.category_id == category_id)
    if vendor_filter is not None:
        kind, vendor_id = vendor_filter
        if kind == "name":
            query = query.where(
                Transaction.vendor_id.in_(
                    select(Vendor.id).where(Vendor.vendor_name_id == vendor_id)
                )
            )
        else:
            query = query.where(Transaction.vendor_id == vendor_id)
    return query


def get_transactions(
    session: Session,
    account_id: Optional[int] = None,
    category_id: Optional[int] = None,
    vendor_filter: Optional[VendorFilter] = None,
    limit: int = 2000,
) -> List[TxnRow]:
    query = (
        _txn_query(account_id, category_id, vendor_filter)
        .order_by(Transaction.posted_date.desc(), Transaction.id.desc())
        .limit(limit)
    )
    result = []
    for txn in session.scalars(query):
        result.append(
            TxnRow(
                id=txn.id,
                posted_date=txn.posted_date.isoformat(),
                description=txn.description,
                vendor=txn.vendor.display_name if txn.vendor else "",
                vendor_raw=txn.vendor.name if txn.vendor else "",
                category=txn.category.value if txn.category else "",
                amount_minor=txn.value_minor,
                currency=txn.currency.value,
            )
        )
    return result


def get_totals(
    session: Session,
    account_id: Optional[int] = None,
    category_id: Optional[int] = None,
    vendor_filter: Optional[VendorFilter] = None,
) -> Totals:
    base = _txn_query(account_id, category_id, vendor_filter).subquery()
    amount = base.c.value_minor
    count = session.scalar(select(func.count()).select_from(base)) or 0
    transfer_count = (
        session.scalar(
            select(func.count())
            .select_from(base)
            .where(base.c.transfer_group_id.is_not(None))
        )
        or 0
    )

    # Both legs of a transfer are real rows, but they move money between your own
    # accounts, so counting them would inflate spending and income alike. Filtering
    # (rather than relying on the legs cancelling out) also keeps the figures right
    # when a filter selects only one leg.
    real = base.c.transfer_group_id.is_(None)
    total = lambda condition: (  # noqa: E731 - reads better than three near-copies
        session.scalar(select(func.coalesce(func.sum(amount), 0)).where(condition)) or 0
    )
    return Totals(
        count=count,
        net_minor=total(real),
        outflow_minor=total(real & (amount < 0)),
        inflow_minor=total(real & (amount > 0)),
        transfer_count=transfer_count,
    )


# --------------------------------------------------------------------- resolvers
# Turn user-supplied names (CLI arguments) into filter ids.

def resolve_account(session: Session, name: str) -> Optional[int]:
    return session.scalar(select(Account.id).where(Account.name == name.strip()))


def resolve_category(session: Session, name: str) -> Optional[int]:
    return session.scalar(select(Category.id).where(Category.value == name.strip()))


def resolve_vendor_filter(session: Session, name: str) -> Optional[VendorFilter]:
    name = name.strip()
    vendor_name = session.scalar(
        select(VendorName.id).where(VendorName.value == name)
    )
    if vendor_name is not None:
        return ("name", vendor_name)
    vendor = session.scalar(select(Vendor.id).where(Vendor.name == name))
    if vendor is not None:
        return ("raw", vendor)
    return None
