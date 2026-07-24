"""Read-side queries for the UI.

These return plain dataclasses (never ORM objects tied to a live session) so the
TUI can render without worrying about session lifetime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Account, Category, Currency, Transaction


@dataclass
class AccountRow:
    id: int
    name: str
    currency: str
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


def _txn_query(account_id: Optional[int], category_id: Optional[int]):
    query = select(Transaction)
    if account_id is not None:
        query = query.where(Transaction.account_id == account_id)
    if category_id is not None:
        query = query.where(Transaction.category_id == category_id)
    return query


def get_transactions(
    session: Session,
    account_id: Optional[int] = None,
    category_id: Optional[int] = None,
    limit: int = 2000,
) -> List[TxnRow]:
    query = (
        _txn_query(account_id, category_id)
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
) -> Totals:
    base = _txn_query(account_id, category_id).subquery()
    amount = base.c.value_minor
    count = session.scalar(select(func.count()).select_from(base)) or 0
    net = session.scalar(select(func.coalesce(func.sum(amount), 0))) or 0
    outflow = (
        session.scalar(
            select(func.coalesce(func.sum(amount), 0)).where(amount < 0)
        )
        or 0
    )
    inflow = (
        session.scalar(
            select(func.coalesce(func.sum(amount), 0)).where(amount > 0)
        )
        or 0
    )
    return Totals(
        count=count, net_minor=net, outflow_minor=outflow, inflow_minor=inflow
    )
