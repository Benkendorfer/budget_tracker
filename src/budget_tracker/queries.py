"""Read-side queries for the UI.

These return plain dataclasses (never ORM objects tied to a live session) so the
TUI can render without worrying about session lifetime.

A vendor filter is a ``(kind, id)`` tuple: ``("name", vendor_name_id)`` matches every
raw vendor that overrides to that name (aggregation), while ``("raw", vendor_id)``
matches a single un-overridden vendor.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

from sqlalchemy import case, func, or_, select, true
from sqlalchemy.orm import Session, selectinload

from .models import (
    Account,
    Category,
    CategoryRule,
    Currency,
    Transaction,
    Vendor,
    VendorName,
    VendorRule,
)

VendorFilter = Tuple[str, int]
# Inclusive on both ends, so a one-day range is ``(day, day)``.
DateRange = Tuple[date, date]

# Which fields a text search looks at.
TEXT_FIELDS = ("all", "description", "vendor", "raw")

# A category filter meaning "the ones with no category at all". Real ids come from SQLite
# autoincrement and are always positive, so a negative sentinel keeps every signature
# ``Optional[int]`` rather than growing a parallel "uncategorised?" flag through them.
UNCATEGORISED_ID = -1

# Time buckets a series can be grouped into.
BUCKETS = ("day", "week", "month")

# ``(sort key, axis label)`` strftime patterns per bucket. SQLite and Python agree on
# every code used here — including ``%W``, week-of-year counting from the first Monday —
# so the same pattern serves the SQL grouping and any Python-side zero-filling.
BUCKET_FORMATS: Dict[str, Tuple[str, str]] = {
    # Day and week windows are short enough that the year is noise on an axis; a monthly
    # series routinely spans a year boundary, so it keeps the year.
    "day": ("%Y-%m-%d", "%m-%d"),
    "week": ("%Y-W%W", "W%W"),
    "month": ("%Y-%m", "%Y-%m"),
}


@dataclass(frozen=True)
class TextFilter:
    """A case-insensitive substring search over one or all of the text fields.

    ``vendor`` is the effective (display) name, ``raw`` the original merchant string,
    and ``all`` matches any of description, display name, or raw name.
    """

    text: str
    field: str = "all"

    def __post_init__(self):
        if self.field not in TEXT_FIELDS:
            raise ValueError(
                f"Unknown search field {self.field!r}; expected one of {list(TEXT_FIELDS)}."
            )


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
    count: int  # rolled up: this category plus every descendant
    total_minor: int  # rolled up, signed sum
    parent_id: Optional[int] = None
    depth: int = 0  # 0 for top level; a UI can indent on this later


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
    account: str = ""
    is_transfer: bool = False  # paired with a leg in another account, so not counted


@dataclass
class RuleRow:
    id: int
    pattern: str
    name: str
    vendor_count: int  # raw vendors this rule currently names


@dataclass
class CategoryRuleRow:
    id: int
    pattern: str
    category: str
    txn_count: int  # transactions this rule currently categorises


@dataclass
class Totals:
    count: int  # every matching transaction, transfers included
    net_minor: int
    outflow_minor: int
    inflow_minor: int
    transfer_count: int = 0  # of `count`, how many were excluded from the money figures


@dataclass
class CategoryTotal:
    id: Optional[int]  # None for uncategorised transactions
    name: str  # "" when uncategorised — the caller picks the label
    count: int
    total_minor: int  # signed sum
    outflow_minor: int  # sum of the negatives, so <= 0
    inflow_minor: int  # sum of the positives, so >= 0
    parent_id: Optional[int] = None  # None for uncategorised, or a top-level category


@dataclass
class BucketTotal:
    key: str  # sortable bucket key, e.g. "2026-03", "2026-W12", "2026-03-04"
    label: str  # short human label for an axis
    count: int
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
    """The sidebar list, each row rolled up over its own subtree.

    A parent's count and total must match what filtering by it now returns (see
    ``_txn_query``'s subtree CTE), so they are not just that category's own direct
    transactions. Rolled up here in Python — a tree walk over a couple of dozen rows is
    far clearer than a recursive aggregate query — rather than in SQL.

    Categories with nothing anywhere in their subtree are omitted, as before.
    """
    direct: Dict[int, Tuple[int, int]] = {
        r[0]: (r[1], r[2])
        for r in session.execute(
            select(
                Transaction.category_id,
                func.count(Transaction.id),
                func.coalesce(func.sum(Transaction.value_minor), 0),
            )
            .where(Transaction.category_id.is_not(None))
            .group_by(Transaction.category_id)
        ).all()
    }
    all_categories = list(session.scalars(select(Category)))
    by_parent: Dict[Optional[int], List[Category]] = defaultdict(list)
    for cat in all_categories:
        by_parent[cat.parent_id].append(cat)

    # (count, total, depth) per category id, filled in by a DFS from each top-level root.
    rolled: Dict[int, Tuple[int, int, int]] = {}

    def roll(cat: Category, depth: int) -> Tuple[int, int]:
        count, total = direct.get(cat.id, (0, 0))
        for child in by_parent.get(cat.id, []):
            c_count, c_total = roll(child, depth + 1)
            count += c_count
            total += c_total
        rolled[cat.id] = (count, total, depth)
        return count, total

    for root in by_parent.get(None, []):
        roll(root, 0)

    rows = [
        CategoryRow(
            id=cat.id, name=cat.value, count=rolled[cat.id][0],
            total_minor=rolled[cat.id][1], parent_id=cat.parent_id, depth=rolled[cat.id][2],
        )
        for cat in all_categories
        if cat.id in rolled and rolled[cat.id][0] > 0
    ]
    # Same key the pre-rollup version sorted by: ascending total, so biggest spend first.
    rows.sort(key=lambda r: r.total_minor)
    return rows


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


def get_category_rules(session: Session) -> List[CategoryRuleRow]:
    """Category rules with the number of transactions each one currently owns.

    "Owns" means the rows :func:`categories.apply_category_rules` stamped ``rule``, and
    attribution mirrors it — patterns are tried per vendor in ``id`` order and the first
    match wins — so two rules pointing at the same category are still counted separately.
    Rows a rule would have matched but could not take (a manual choice, or a transfer leg)
    are not counted, because the rule does not own them.
    """
    from .categories import RULE, matches  # local import keeps the dependency one-way

    rules = list(session.scalars(select(CategoryRule).order_by(CategoryRule.id)))
    counts = {rule.id: 0 for rule in rules}
    owner = {}
    for vendor in session.scalars(select(Vendor)):
        match = next((r for r in rules if matches(r.pattern, vendor)), None)
        if match is not None:
            owner[vendor.id] = match.id
    rows = session.execute(
        select(Transaction.vendor_id, func.count())
        .where(Transaction.category_source == RULE)
        .group_by(Transaction.vendor_id)
    ).all()
    for vendor_id, count in rows:
        rule_id = owner.get(vendor_id)
        if rule_id is not None:
            counts[rule_id] += count
    return [
        CategoryRuleRow(
            id=rule.id,
            pattern=rule.pattern,
            category=rule.category.value,
            txn_count=counts[rule.id],
        )
        for rule in rules
    ]


def _contains(column, text: str):
    """A case-insensitive substring test.

    ``instr`` rather than ``LIKE`` so that ``%`` and ``_`` in the user's text need no
    escaping — they are ordinary characters here. (The Postgres equivalent is ``strpos``,
    if the database ever moves.)
    """
    return func.instr(func.lower(column), text.lower()) > 0


def _text_condition(text_filter: TextFilter):
    text = text_filter.text.strip()
    field = text_filter.field
    # The display name is the override when there is one, else the raw name, so it has
    # to be matched through the join rather than on a single column.
    display = func.coalesce(VendorName.value, Vendor.name)

    vendor_conditions = []
    if field in ("all", "vendor"):
        vendor_conditions.append(_contains(display, text))
    if field in ("all", "raw"):
        vendor_conditions.append(_contains(Vendor.name, text))

    conditions = []
    if field in ("all", "description"):
        conditions.append(_contains(Transaction.description, text))
    if vendor_conditions:
        matching_vendors = (
            select(Vendor.id)
            .outerjoin(VendorName, VendorName.id == Vendor.vendor_name_id)
            .where(or_(*vendor_conditions))
        )
        conditions.append(Transaction.vendor_id.in_(matching_vendors))
    return or_(*conditions)


def _subtree_ids(category_id: int):
    """A recursive CTE selecting ``category_id`` and every category below it.

    Filtering by a category has to match its whole subtree, not just itself, and a
    category tree is arbitrarily deep — hence ``WITH RECURSIVE`` rather than a fixed
    number of joins.
    """
    root = select(Category.id.label("id")).where(Category.id == category_id)
    cte = root.cte(name="category_subtree", recursive=True)
    cte = cte.union_all(
        select(Category.id).where(Category.parent_id == cte.c.id)
    )
    return select(cte.c.id)


def _txn_query(
    account_id: Optional[int],
    category_id: Optional[int],
    vendor_filter: Optional[VendorFilter],
    text_filter: "Optional[TextFilter]" = None,
    date_range: "Optional[DateRange]" = None,
):
    query = select(Transaction)
    if date_range is not None:
        start, end = date_range
        query = query.where(
            Transaction.posted_date >= start, Transaction.posted_date <= end
        )
    if account_id is not None:
        query = query.where(Transaction.account_id == account_id)
    if category_id == UNCATEGORISED_ID:
        query = query.where(Transaction.category_id.is_(None))
    elif category_id is not None:
        query = query.where(Transaction.category_id.in_(_subtree_ids(category_id)))
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
    if text_filter is not None and text_filter.text.strip():
        query = query.where(_text_condition(text_filter))
    return query


def get_transactions(
    session: Session,
    account_id: Optional[int] = None,
    category_id: Optional[int] = None,
    vendor_filter: Optional[VendorFilter] = None,
    limit: int = 2000,
    text_filter: Optional[TextFilter] = None,
    date_range: Optional[DateRange] = None,
) -> List[TxnRow]:
    query = (
        _txn_query(account_id, category_id, vendor_filter, text_filter, date_range)
        # Every row below reads through all four of these relationships. Left lazy, each
        # distinct related object costs its own SELECT — ~900 of them on a real database,
        # for a query that should take one apiece. ``vendor_name`` is nested because
        # ``display_name`` reaches through the vendor to it.
        .options(
            selectinload(Transaction.vendor).selectinload(Vendor.vendor_name),
            selectinload(Transaction.category),
            selectinload(Transaction.currency),
            selectinload(Transaction.account),
        )
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
                account=txn.account.name,
                is_transfer=txn.transfer_group_id is not None,
            )
        )
    return result


def get_totals(
    session: Session,
    account_id: Optional[int] = None,
    category_id: Optional[int] = None,
    vendor_filter: Optional[VendorFilter] = None,
    text_filter: Optional[TextFilter] = None,
    date_range: Optional[DateRange] = None,
) -> Totals:
    base = _txn_query(
        account_id, category_id, vendor_filter, text_filter, date_range
    ).subquery()
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


def _signed_sums(amount, counts=None):
    """``(total, outflow, inflow)`` aggregates over a signed amount column.

    ``counts`` is a condition selecting the rows whose money should be added up; rows
    outside it still exist and are still counted, they just contribute 0. That is how
    transfers are kept out of the figures without being erased from the tallies.
    """
    def summed(condition):
        if counts is not None:
            condition = condition & counts
        return func.coalesce(func.sum(case((condition, amount), else_=0)), 0)

    return summed(true()), summed(amount < 0), summed(amount > 0)


def get_category_totals(
    session: Session,
    account_id: Optional[int] = None,
    category_id: Optional[int] = None,
    vendor_filter: Optional[VendorFilter] = None,
    text_filter: Optional[TextFilter] = None,
    date_range: Optional[DateRange] = None,
) -> List[CategoryTotal]:
    """Per-category rows, most-spent first.

    ``count`` is every transaction in the category, transfers included, while the money
    columns leave transfers out — the same split :class:`Totals` makes. Counting and
    summing the same set would be tidier arithmetic but a worse answer: the count is what
    a UI drills into, and a category holding a transfer would then promise fewer rows
    than it shows.

    Uncategorised transactions are a row of their own (``id`` None, ``name`` ""), reached
    by an outer join, so the rows always add back up to the totals.
    """
    base = _txn_query(
        account_id, category_id, vendor_filter, text_filter, date_range
    ).subquery()
    amount = base.c.value_minor
    total, outflow, inflow = _signed_sums(amount, base.c.transfer_group_id.is_(None))
    rows = session.execute(
        select(
            base.c.category_id,
            func.coalesce(Category.value, ""),
            func.count(),
            total,
            outflow,
            inflow,
            Category.parent_id,
        )
        .select_from(base)
        .join(Category, Category.id == base.c.category_id, isouter=True)
        .group_by(base.c.category_id)
        # Outflow is negative, so ascending is biggest-spend-first.
        .order_by(outflow, func.coalesce(Category.value, ""))
    ).all()
    return [
        CategoryTotal(
            id=r[0], name=r[1], count=r[2], total_minor=r[3], outflow_minor=r[4],
            inflow_minor=r[5], parent_id=r[6],
        )
        for r in rows
    ]


def get_bucket_totals(
    session: Session,
    bucket: str = "month",
    account_id: Optional[int] = None,
    category_id: Optional[int] = None,
    vendor_filter: Optional[VendorFilter] = None,
    text_filter: Optional[TextFilter] = None,
    date_range: Optional[DateRange] = None,
) -> List[BucketTotal]:
    """Money grouped into day/week/month buckets, chronologically. Transfers excluded.

    Only buckets that actually contain a transaction are returned — a chart that needs an
    unbroken axis has to zero-fill the gaps itself (see :func:`stats.spending_series`).
    """
    patterns = BUCKET_FORMATS.get(bucket)
    if patterns is None:
        raise ValueError(f"Unknown bucket {bucket!r}; expected one of {list(BUCKETS)}.")
    key_format, label_format = patterns

    base = _txn_query(
        account_id, category_id, vendor_filter, text_filter, date_range
    ).subquery()
    amount = base.c.value_minor
    _total, outflow, inflow = _signed_sums(amount)
    key = func.strftime(key_format, base.c.posted_date)
    label = func.strftime(label_format, base.c.posted_date)
    rows = session.execute(
        select(key, label, func.count(), outflow, inflow)
        .select_from(base)
        .where(base.c.transfer_group_id.is_(None))
        .group_by(key, label)
        .order_by(key)
    ).all()
    return [
        BucketTotal(key=r[0], label=r[1], count=r[2], outflow_minor=r[3], inflow_minor=r[4])
        for r in rows
    ]


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
