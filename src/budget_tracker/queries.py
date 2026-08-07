"""Read-side queries for the UI.

These return plain dataclasses (never ORM objects tied to a live session) so the
TUI can render without worrying about session lifetime.

A vendor filter is a ``(kind, id)`` tuple: ``("name", vendor_name_id)`` matches every
raw vendor that overrides to that name (aggregation), while ``("raw", vendor_id)``
matches a single un-overridden vendor.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy import case, func, or_, select, true
from sqlalchemy.orm import Session, selectinload

from . import rates
from .models import (
    Account,
    Category,
    CategoryRule,
    Currency,
    ExchangeRate,
    Import,
    Transaction,
    Vendor,
    VendorName,
    VendorRule,
)

# Aggregates convert every amount into this currency before summing (see
# ``rates.convert``). A module-level constant rather than hard-coding "USD" at each
# call site, so every caller that wants the default gets the same one.
HOME_CURRENCY = "USD"

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


@dataclass(frozen=True)
class Filters:
    """The five filters that are always passed together and always mean the same thing:
    which transactions a query, report, or series should be scoped to.

    Every field defaults to ``None`` (no restriction), matching what leaving the
    corresponding keyword argument off already meant. Bundling them stops a sixth filter
    (``home_currency`` was almost one) from having to be threaded through eight
    signatures individually -- see ``resolve_filters`` for how a function accepts either
    this or its old individual arguments without the two being able to disagree.
    """

    account_id: Optional[int] = None
    category_id: Optional[int] = None
    vendor_filter: Optional[VendorFilter] = None
    text_filter: Optional[TextFilter] = None
    date_range: Optional[DateRange] = None

    def replace(self, **changes) -> "Filters":
        """The same filters with one or more fields swapped out.

        For a drill-down that keeps everything but the date range (or a click that keeps
        everything but adds a category), e.g. ``filters.replace(date_range=new_range)``.
        """
        return replace(self, **changes)


def resolve_filters(
    filters: Optional[Filters],
    account_id: Optional[int],
    category_id: Optional[int],
    vendor_filter: Optional[VendorFilter],
    text_filter: Optional[TextFilter],
    date_range: Optional[DateRange],
) -> Filters:
    """Reconcile a function's old individual filter arguments with its new optional
    ``Filters`` bundle, so every call site -- old or new -- ends up with exactly one.

    Passing both is rejected rather than given a silent precedence: there is no reading
    of "pass both" that is not a caller mistake, and picking one quietly would just move
    the bug somewhere harder to find.
    """
    individual = Filters(
        account_id=account_id,
        category_id=category_id,
        vendor_filter=vendor_filter,
        text_filter=text_filter,
        date_range=date_range,
    )
    if filters is None:
        return individual
    if individual != Filters():
        conflicting = [
            name
            for name in (
                "account_id", "category_id", "vendor_filter", "text_filter", "date_range",
            )
            if getattr(individual, name) is not None
        ]
        raise ValueError(
            f"Pass either `filters` or the individual filter argument(s) {conflicting}, "
            "not both."
        )
    return filters


@dataclass(frozen=True)
class CurrencyRow:
    """A currency's display facts, read once so callers need no live session.

    ``decimal_places`` is not always 2 — JPY has none — so anything turning minor units
    into text has to ask rather than assume.
    """

    code: str
    symbol: Optional[str]
    decimal_places: int


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
    # Of the real (non-transfer) rows, how many had no exchange rate to `home_currency`
    # for their posted date and so are missing from net/outflow/inflow entirely -- never
    # silently zeroed. See `get_totals`.
    unconverted_count: int = 0


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


@dataclass
class ImportRow:
    """One past import, for browsing history and finding the id ``unimport`` needs."""

    id: int
    source_file: str
    account: str  # "" when the import spanned more than one account
    transaction_count: int  # transactions still linked to it right now
    imported_at: str


@dataclass
class ImportDeletePreview:
    """What ``importer.delete_import`` would do, read without doing it.

    Mirrors its counting exactly (see :func:`preview_import_delete`), so a confirmation
    shown before deleting matches what deleting actually does.
    """

    import_id: int
    source_file: str
    transaction_count: int
    transfers_broken: int


def get_currencies(session: Session) -> List[CurrencyRow]:
    """Every currency the database knows, by ISO code."""
    rows = session.execute(
        select(Currency.value, Currency.symbol, Currency.decimal_places)
        .order_by(Currency.value)
    ).all()
    return [CurrencyRow(code=r[0], symbol=r[1], decimal_places=r[2]) for r in rows]


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


def _txn_query(filters: Filters):
    query = select(Transaction)
    if filters.date_range is not None:
        start, end = filters.date_range
        query = query.where(
            Transaction.posted_date >= start, Transaction.posted_date <= end
        )
    if filters.account_id is not None:
        query = query.where(Transaction.account_id == filters.account_id)
    if filters.category_id == UNCATEGORISED_ID:
        query = query.where(Transaction.category_id.is_(None))
    elif filters.category_id is not None:
        query = query.where(
            Transaction.category_id.in_(_subtree_ids(filters.category_id))
        )
    if filters.vendor_filter is not None:
        kind, vendor_id = filters.vendor_filter
        if kind == "name":
            query = query.where(
                Transaction.vendor_id.in_(
                    select(Vendor.id).where(Vendor.vendor_name_id == vendor_id)
                )
            )
        else:
            query = query.where(Transaction.vendor_id == vendor_id)
    if filters.text_filter is not None and filters.text_filter.text.strip():
        query = query.where(_text_condition(filters.text_filter))
    return query


def get_transactions(
    session: Session,
    account_id: Optional[int] = None,
    category_id: Optional[int] = None,
    vendor_filter: Optional[VendorFilter] = None,
    limit: int = 2000,
    text_filter: Optional[TextFilter] = None,
    date_range: Optional[DateRange] = None,
    filters: Optional[Filters] = None,
) -> List[TxnRow]:
    resolved = resolve_filters(
        filters, account_id, category_id, vendor_filter, text_filter, date_range
    )
    query = (
        _txn_query(resolved)
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


def _currencies_present(session: Session, base, condition=None) -> Set[str]:
    """Currency codes among ``base``'s rows, optionally narrowed by ``condition``.

    Used to decide whether an aggregate can take the single-currency fast path (see
    ``get_totals``): with everything already in ``home_currency``, no rate lookup or
    per-row conversion is needed and the original single-query plan runs unchanged.
    """
    query = (
        select(Currency.value)
        .select_from(base)
        .join(Currency, Currency.id == base.c.currency_id)
        .distinct()
    )
    if condition is not None:
        query = query.where(condition)
    return set(session.scalars(query))


def get_totals(
    session: Session,
    account_id: Optional[int] = None,
    category_id: Optional[int] = None,
    vendor_filter: Optional[VendorFilter] = None,
    text_filter: Optional[TextFilter] = None,
    date_range: Optional[DateRange] = None,
    home_currency: str = HOME_CURRENCY,
    filters: Optional[Filters] = None,
) -> Totals:
    resolved = resolve_filters(
        filters, account_id, category_id, vendor_filter, text_filter, date_range
    )
    base = _txn_query(resolved).subquery()
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
    # (rather than relying on the legs canceling out) also keeps the figures right
    # when a filter selects only one leg.
    real = base.c.transfer_group_id.is_(None)

    # Fast path: everything real is already in home_currency (true for every database
    # today), so this is exactly the query that ran before conversion existed.
    if _currencies_present(session, base, real) <= {home_currency}:
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

    # Slow path: more than one currency among the real rows. Summing centimes into
    # cents would be meaningless, so amounts are grouped by (currency, posted_date) --
    # small enough to pull into Python -- and converted at each day's own rate rather
    # than one rate for the whole window, since rates move.
    groups = session.execute(
        select(
            Currency.value,
            base.c.posted_date,
            func.count(),
            func.coalesce(func.sum(case((amount < 0, amount), else_=0)), 0),
            func.coalesce(func.sum(case((amount > 0, amount), else_=0)), 0),
        )
        .select_from(base)
        .join(Currency, Currency.id == base.c.currency_id)
        .where(real)
        .group_by(Currency.value, base.c.posted_date)
    ).all()

    outflow_minor = 0
    inflow_minor = 0
    unconverted_count = 0
    for currency, day, group_count, outflow_sum, inflow_sum in groups:
        converted_outflow = rates.convert(session, outflow_sum, currency, home_currency, day)
        converted_inflow = rates.convert(session, inflow_sum, currency, home_currency, day)
        # A missing rate is missing for the whole (currency, day) group -- rate_on does
        # not depend on the amount -- so either both convert or neither does. Never
        # guessed as zero: the group is left out and its rows counted as unconverted.
        if converted_outflow is None or converted_inflow is None:
            unconverted_count += group_count
            continue
        outflow_minor += converted_outflow
        inflow_minor += converted_inflow

    return Totals(
        count=count,
        net_minor=outflow_minor + inflow_minor,
        outflow_minor=outflow_minor,
        inflow_minor=inflow_minor,
        transfer_count=transfer_count,
        unconverted_count=unconverted_count,
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
    home_currency: str = HOME_CURRENCY,
    filters: Optional[Filters] = None,
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
    resolved = resolve_filters(
        filters, account_id, category_id, vendor_filter, text_filter, date_range
    )
    base = _txn_query(resolved).subquery()
    amount = base.c.value_minor
    real = base.c.transfer_group_id.is_(None)

    if _currencies_present(session, base, real) <= {home_currency}:
        total, outflow, inflow = _signed_sums(amount, real)
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

    # Slow path: category id/name/count come from the same query as always (currency
    # never affects counting), but money is grouped by (category, currency,
    # posted_date), converted per group at that day's rate, and summed in Python.
    info_rows = session.execute(
        select(
            base.c.category_id,
            func.coalesce(Category.value, ""),
            func.count(),
            Category.parent_id,
        )
        .select_from(base)
        .join(Category, Category.id == base.c.category_id, isouter=True)
        .group_by(base.c.category_id)
    ).all()

    money_groups = session.execute(
        select(
            base.c.category_id,
            Currency.value,
            base.c.posted_date,
            func.count(),
            func.coalesce(func.sum(case((amount < 0, amount), else_=0)), 0),
            func.coalesce(func.sum(case((amount > 0, amount), else_=0)), 0),
        )
        .select_from(base)
        .join(Currency, Currency.id == base.c.currency_id)
        .where(real)
        .group_by(base.c.category_id, Currency.value, base.c.posted_date)
    ).all()

    sums: Dict[Optional[int], Tuple[int, int]] = defaultdict(lambda: (0, 0))
    for cat_id, currency, day, _group_count, outflow_sum, inflow_sum in money_groups:
        converted_outflow = rates.convert(session, outflow_sum, currency, home_currency, day)
        converted_inflow = rates.convert(session, inflow_sum, currency, home_currency, day)
        if converted_outflow is None or converted_inflow is None:
            continue  # missing rate: this group's money is left out, not zeroed
        cur_outflow, cur_inflow = sums[cat_id]
        sums[cat_id] = (cur_outflow + converted_outflow, cur_inflow + converted_inflow)

    results = [
        CategoryTotal(
            id=cat_id,
            name=name,
            count=count,
            total_minor=sums[cat_id][0] + sums[cat_id][1],
            outflow_minor=sums[cat_id][0],
            inflow_minor=sums[cat_id][1],
            parent_id=parent_id,
        )
        for cat_id, name, count, parent_id in info_rows
    ]
    results.sort(key=lambda r: (r.outflow_minor, r.name))
    return results


def get_bucket_totals(
    session: Session,
    bucket: str = "month",
    account_id: Optional[int] = None,
    category_id: Optional[int] = None,
    vendor_filter: Optional[VendorFilter] = None,
    text_filter: Optional[TextFilter] = None,
    date_range: Optional[DateRange] = None,
    home_currency: str = HOME_CURRENCY,
    filters: Optional[Filters] = None,
) -> List[BucketTotal]:
    """Money grouped into day/week/month buckets, chronologically. Transfers excluded.

    Only buckets that actually contain a transaction are returned — a chart that needs an
    unbroken axis has to zero-fill the gaps itself (see :func:`stats.spending_series`).
    """
    patterns = BUCKET_FORMATS.get(bucket)
    if patterns is None:
        raise ValueError(f"Unknown bucket {bucket!r}; expected one of {list(BUCKETS)}.")
    key_format, label_format = patterns

    resolved = resolve_filters(
        filters, account_id, category_id, vendor_filter, text_filter, date_range
    )
    base = _txn_query(resolved).subquery()
    amount = base.c.value_minor
    real = base.c.transfer_group_id.is_(None)

    if _currencies_present(session, base, real) <= {home_currency}:
        _total, outflow, inflow = _signed_sums(amount)
        key = func.strftime(key_format, base.c.posted_date)
        label = func.strftime(label_format, base.c.posted_date)
        rows = session.execute(
            select(key, label, func.count(), outflow, inflow)
            .select_from(base)
            .where(real)
            .group_by(key, label)
            .order_by(key)
        ).all()
        return [
            BucketTotal(key=r[0], label=r[1], count=r[2], outflow_minor=r[3], inflow_minor=r[4])
            for r in rows
        ]

    # Slow path: grouped by (currency, posted_date) -- finer than the bucket itself --
    # so each day converts at its own rate before being rolled up into its bucket.
    groups = session.execute(
        select(
            Currency.value,
            base.c.posted_date,
            func.count(),
            func.coalesce(func.sum(case((amount < 0, amount), else_=0)), 0),
            func.coalesce(func.sum(case((amount > 0, amount), else_=0)), 0),
        )
        .select_from(base)
        .join(Currency, Currency.id == base.c.currency_id)
        .where(real)
        .group_by(Currency.value, base.c.posted_date)
    ).all()

    # (count, outflow, inflow) per bucket key, built up from the converted day groups.
    # Count is added unconditionally -- a real transaction that could not be converted
    # is still a real transaction, exactly as a category holding one still shows its
    # count (see get_category_totals) -- only its money is left out.
    buckets: Dict[str, List[int]] = {}
    labels: Dict[str, str] = {}
    for currency, day, group_count, outflow_sum, inflow_sum in groups:
        key = day.strftime(key_format)
        labels[key] = day.strftime(label_format)
        count, outflow, inflow = buckets.setdefault(key, [0, 0, 0])
        converted_outflow = rates.convert(session, outflow_sum, currency, home_currency, day)
        converted_inflow = rates.convert(session, inflow_sum, currency, home_currency, day)
        if converted_outflow is None or converted_inflow is None:
            buckets[key] = [count + group_count, outflow, inflow]
            continue
        buckets[key] = [
            count + group_count, outflow + converted_outflow, inflow + converted_inflow
        ]

    return [
        BucketTotal(key=key, label=labels[key], count=v[0], outflow_minor=v[1], inflow_minor=v[2])
        for key, v in sorted(buckets.items())
    ]


def get_imports(session: Session) -> List[ImportRow]:
    """Past imports, most recent first — where an ``unimport`` id comes from."""
    rows = session.execute(
        select(
            Import.id,
            Import.source_file,
            Account.name,
            func.count(Transaction.id),
            Import.imported_at,
        )
        .join(Account, Account.id == Import.account_id, isouter=True)
        .join(Transaction, Transaction.import_id == Import.id, isouter=True)
        .group_by(Import.id)
        .order_by(Import.id.desc())
    ).all()
    return [
        ImportRow(
            id=r[0],
            source_file=r[1],
            account=r[2] or "",
            transaction_count=r[3],
            imported_at=r[4].isoformat(sep=" ", timespec="minutes") if r[4] else "",
        )
        for r in rows
    ]


def preview_import_delete(
    session: Session, import_id: int
) -> Optional[ImportDeletePreview]:
    """Count what deleting ``import_id`` would do, without doing it.

    Mirrors ``importer.delete_import``'s own counting: every transaction the import
    created, plus every surviving transfer leg whose partner is among them (which would
    have its ``transfer_group_id`` cleared). Returns ``None`` for an unknown id, the same
    thing :func:`importer.delete_import` would raise :class:`importer.UnknownImport` for.
    """
    import_record = session.get(Import, import_id)
    if import_record is None:
        return None
    doomed_ids = list(
        session.scalars(select(Transaction.id).where(Transaction.import_id == import_id))
    )
    transfers_broken = 0
    if doomed_ids:
        group_ids = set(
            session.scalars(
                select(Transaction.transfer_group_id).where(
                    Transaction.id.in_(doomed_ids),
                    Transaction.transfer_group_id.is_not(None),
                )
            )
        )
        if group_ids:
            transfers_broken = (
                session.scalar(
                    select(func.count(Transaction.id)).where(
                        Transaction.transfer_group_id.in_(group_ids),
                        Transaction.id.not_in(doomed_ids),
                    )
                )
                or 0
            )
    return ImportDeletePreview(
        import_id=import_id,
        source_file=import_record.source_file,
        transaction_count=len(doomed_ids),
        transfers_broken=transfers_broken,
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


# ---------------------------------------------------------------- exchange rates
# Shared between the CLI's `rates` command and the TUI's, so the two never drift --
# see cli._cmd_rates and tui/app.py's _do_rates.

@dataclass
class ExchangeRateRow:
    """One cached (base, quote, source) triple, spanning every day recorded for it."""

    base: str
    quote: str
    source: str
    first_day: str  # isoformat
    last_day: str  # isoformat
    count: int


def get_exchange_rates(session: Session) -> List[ExchangeRateRow]:
    """Every cached rate, grouped by pair and source -- what a `rates` list shows."""
    rows = session.execute(
        select(
            ExchangeRate.base,
            ExchangeRate.quote,
            ExchangeRate.source,
            func.min(ExchangeRate.day),
            func.max(ExchangeRate.day),
            func.count(),
        )
        .group_by(ExchangeRate.base, ExchangeRate.quote, ExchangeRate.source)
        .order_by(ExchangeRate.base, ExchangeRate.quote, ExchangeRate.source)
    ).all()
    return [
        ExchangeRateRow(
            base=r[0], quote=r[1], source=r[2],
            first_day=r[3].isoformat(), last_day=r[4].isoformat(), count=r[5],
        )
        for r in rows
    ]


def default_rate_fetch_span(
    session: Session, home_currency: str = HOME_CURRENCY
) -> Optional[Tuple[date, date, List[str]]]:
    """``(start, end, quotes)`` covering every transaction on file -- what ``rates
    fetch`` with no explicit range or currencies should ask for.

    ``quotes`` may come back empty -- every account already in ``home_currency`` is a
    normal state ("nothing to fetch"), not an error, so the caller decides what to say
    about it. ``None`` is reserved for the one case that really has no answer: no
    transactions on file to derive a date range from at all. Currencies are read from
    account rows (``get_accounts``), not every ``Currency`` the database happens to
    know about, so a code with no account never turns into a wasted request.
    """
    first, last = session.execute(
        select(func.min(Transaction.posted_date), func.max(Transaction.posted_date))
    ).one()
    if first is None or last is None:
        return None
    currencies = sorted({row.currency for row in get_accounts(session)})
    quotes = [c for c in currencies if c != home_currency]
    return first, last, quotes
