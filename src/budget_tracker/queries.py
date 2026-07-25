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

from .models import Account, Category, Currency, Transaction, Vendor, VendorName

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
class Totals:
    count: int
    net_minor: int
    outflow_minor: int
    inflow_minor: int


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
    net = session.scalar(select(func.coalesce(func.sum(amount), 0))) or 0
    outflow = (
        session.scalar(select(func.coalesce(func.sum(amount), 0)).where(amount < 0))
        or 0
    )
    inflow = (
        session.scalar(select(func.coalesce(func.sum(amount), 0)).where(amount > 0))
        or 0
    )
    return Totals(
        count=count, net_minor=net, outflow_minor=outflow, inflow_minor=inflow
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
