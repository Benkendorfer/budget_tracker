"""The monthly budget: a per-month plan of category limits and an income target,
tracked against the same actuals the statistics panel shows.

A plan is stored **per month**, not as a standing limit with overrides -- see the
module-level ``BudgetAmount`` model. A month with no stored rows of its own uses the
most recent earlier month that has any ("copy forward"), as a whole: there is no
per-category fallback. :func:`set_amount` makes that copy explicit the first time a
month is edited, so every month with any row at all owns every row it shows.

Actuals are never recomputed here. Every spent/income figure comes from
:func:`stats.build_report` for the relevant month, which already applies "counts as
spending" (transfers and exclusions out, refunds netted, home currency) and the
category rollup (a parent's figure includes its descendants). That is the one
guarantee this module exists to keep: a budget figure and the statistics panel's
figure for the same category and month are computed the same way, by construction.

Nothing here commits; callers own the transaction, as in :mod:`.categories`.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Set

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import categories, queries, stats
from .models import BudgetAmount, Category

EXPENSE = "expense"
INCOME = "income"

# Ahead-of-pace needs a little slack: a category that is just barely past the
# month's elapsed share (say 51% used at 50% elapsed) is not meaningfully "ahead",
# it is rounding -- see track()'s ahead_of_pace.
_PACE_TOLERANCE = 0.05


def month_start(d: date) -> date:
    """The first of ``d``'s month -- the only form a budget month is ever stored or
    looked up by."""
    return date(d.year, d.month, 1)


def _add_months(month: date, n: int) -> date:
    """``month`` (already the first of a month) shifted by ``n`` whole calendar
    months, positive or negative. Never clamps -- there is no day-of-month to clamp,
    unlike :func:`stats.months_before`."""
    total = month.year * 12 + (month.month - 1) + n
    return date(total // 12, total % 12 + 1, 1)


def _month_window(month: date) -> stats.Window:
    """The whole calendar month ``month`` falls in, as a :class:`stats.Window`."""
    month = month_start(month)
    last_day = calendar.monthrange(month.year, month.month)[1]
    return stats.Window(
        key="custom",
        label=month.strftime("%Y-%m"),
        start=month,
        end=date(month.year, month.month, last_day),
    )


def effective_month(session: Session, month: date) -> Optional[date]:
    """Which stored month's rows apply to ``month``: the latest month <= ``month``
    that has any row at all (either kind), or ``None`` if nothing has ever been
    stored at or before ``month``."""
    month = month_start(month)
    return session.scalar(
        select(BudgetAmount.month)
        .where(BudgetAmount.month <= month)
        .order_by(BudgetAmount.month.desc())
        .limit(1)
    )


@dataclass(frozen=True)
class Plan:
    month: date
    source_month: Optional[date]  # None if nothing has ever been stored
    income_target_minor: Optional[int]
    expense: Dict[int, int]  # category_id -> amount_minor


def get_plan(session: Session, month: date) -> Plan:
    """The effective plan for ``month`` -- ``month``'s own rows, or a copied-forward
    earlier month's, read-only (no copy is written; see :func:`set_amount` for that)."""
    month = month_start(month)
    source_month = effective_month(session, month)
    if source_month is None:
        return Plan(month=month, source_month=None, income_target_minor=None, expense={})
    income_target_minor: Optional[int] = None
    expense: Dict[int, int] = {}
    for row in session.scalars(
        select(BudgetAmount).where(BudgetAmount.month == source_month)
    ):
        if row.kind == INCOME:
            income_target_minor = row.amount_minor
        else:
            expense[row.category_id] = row.amount_minor
    return Plan(
        month=month,
        source_month=source_month,
        income_target_minor=income_target_minor,
        expense=expense,
    )


def _copy_forward_if_needed(session: Session, month: date) -> None:
    """If ``month`` has no rows of its own yet, copy the effective (most recent
    earlier) plan's rows into it verbatim. After this, ``month`` either already had
    rows or now owns a full copy -- :func:`set_amount` always calls this before
    writing, so every edit only ever touches ``month``'s own rows from here on."""
    has_own_rows = (
        session.scalar(select(BudgetAmount.id).where(BudgetAmount.month == month).limit(1))
        is not None
    )
    if has_own_rows:
        return
    source_month = effective_month(session, month)
    if source_month is None:
        return  # nothing to copy; month starts blank
    for row in session.scalars(select(BudgetAmount).where(BudgetAmount.month == source_month)):
        session.add(
            BudgetAmount(
                month=month,
                category_id=row.category_id,
                kind=row.kind,
                amount_minor=row.amount_minor,
            )
        )
    session.flush()


def set_amount(
    session: Session,
    month: date,
    category: Optional[str],
    amount_minor: Optional[int],
    kind: str = EXPENSE,
) -> None:
    """Set (or, with ``amount_minor=None``, clear) one amount in ``month``'s plan.

    ``category`` is a name or a full path (``"Food > Dining"``), resolved the same
    way :func:`categories.resolve_path` resolves one anywhere else -- an unknown name
    is a :class:`ValueError`, never a silently created category. ``kind=INCOME``
    takes no category (the income target is the one row with ``category_id`` NULL);
    passing one is an error rather than ignored.

    Copies the effective plan forward into ``month`` first if ``month`` has no rows
    of its own yet (see :func:`_copy_forward_if_needed`), then applies just this one
    change to ``month``'s own rows. Clearing removes the row outright -- there is no
    "cleared" marker -- so a month that ends up with no rows at all (every amount it
    ever had cleared) stops counting as a stored month and :func:`effective_month`
    falls through to an earlier one for it. That is an accepted consequence of
    "a cleared amount is simply absent", not a bug: it only arises when a month's
    *entire* plan is cleared, not when one of several amounts is.

    No commit -- the caller owns the transaction.
    """
    month = month_start(month)
    if kind == INCOME:
        if category is not None:
            raise ValueError("An income target has no category.")
        category_id: Optional[int] = None
    elif kind == EXPENSE:
        if category is None:
            raise ValueError("An expense amount needs a category.")
        resolved = categories.resolve_path(session, category)
        if resolved is None:
            raise ValueError(f"No category named {category!r}.")
        category_id = resolved.id
    else:
        raise ValueError(f"Unknown kind {kind!r}; expected {EXPENSE!r} or {INCOME!r}.")

    _copy_forward_if_needed(session, month)

    category_clause = (
        BudgetAmount.category_id.is_(None)
        if category_id is None
        else BudgetAmount.category_id == category_id
    )
    existing = session.scalar(
        select(BudgetAmount).where(
            BudgetAmount.month == month, BudgetAmount.kind == kind, category_clause
        )
    )
    if amount_minor is None:
        if existing is not None:
            session.delete(existing)
            session.flush()
        return
    if existing is not None:
        existing.amount_minor = amount_minor
    else:
        session.add(
            BudgetAmount(month=month, category_id=category_id, kind=kind, amount_minor=amount_minor)
        )
    session.flush()


# --------------------------------------------------------------------- category tree
#
# Both plan_rows() and track() need the whole category tree in memory -- to compute a
# category's real depth and parent (not one relative to whatever a single month's
# report happens to include), and to order rows depth-first. There are only ever a
# couple of dozen categories (see stats._roll_up_categories), so one query for all of
# them is simpler than walking parent chains one row at a time.


def _category_tree(session: Session):
    category_by_id: Dict[int, Category] = {
        c.id: c for c in session.scalars(select(Category))
    }
    children: Dict[Optional[int], List[int]] = defaultdict(list)
    for category in category_by_id.values():
        children[category.parent_id].append(category.id)
    return category_by_id, children


def _name_depth_parent(category_by_id: Dict[int, Category], category_id: int):
    category = category_by_id[category_id]
    depth = 0
    node = category
    while node.parent_id is not None:
        node = category_by_id[node.parent_id]
        depth += 1
    return category.value, depth, category.parent_id


def _is_top_level_among(
    category_by_id: Dict[int, Category], category_id: int, ids: Set[int]
) -> bool:
    """Whether no ancestor of ``category_id`` is also in ``ids`` -- the rule behind
    every "don't double-count a parent and its own budgeted child" total below."""
    node = category_by_id[category_id]
    while node.parent_id is not None:
        if node.parent_id in ids:
            return False
        node = category_by_id[node.parent_id]
    return True


def _nearest_budgeted_ancestor(
    category_by_id: Dict[int, Category], category_id: int, budgeted: Set[int]
) -> Optional[int]:
    node = category_by_id[category_id]
    while node.parent_id is not None:
        if node.parent_id in budgeted:
            return node.parent_id
        node = category_by_id[node.parent_id]
    return None


def _subcategory_budgets(
    category_by_id: Dict[int, Category], expense: Dict[int, int]
) -> Dict[int, int]:
    """Budgeted category -> the sum of the budgets nested directly under it."""
    sums: Dict[int, int] = {}
    budgeted = set(expense)
    for category_id, amount in expense.items():
        owner = _nearest_budgeted_ancestor(category_by_id, category_id, budgeted)
        if owner is not None:
            sums[owner] = sums.get(owner, 0) + amount
    return sums


def _derived_budgets(
    category_by_id: Dict[int, Category], expense: Dict[int, int]
) -> Dict[int, int]:
    """A budget for every category that has none of its own but has budgeted
    categories beneath it: the sum of those, nearest first.

    Computed on every read, never stored, so it follows its subcategories as they
    change; setting the parent's own amount replaces it. Each budget adds itself to the
    ancestors above it up to (not including) the first one with its own budget -- that
    one already holds it -- so nothing is counted twice however deep the nesting.
    """
    derived: Dict[int, int] = {}
    for category_id, amount in expense.items():
        node = category_by_id[category_id]
        while node.parent_id is not None and node.parent_id not in expense:
            derived[node.parent_id] = derived.get(node.parent_id, 0) + amount
            node = category_by_id[node.parent_id]
    return derived


def _depth_first_rows(
    category_by_id: Dict[int, Category],
    children: Dict[Optional[int], List[int]],
    included: Set[int],
    weight,
) -> List[int]:
    """Every id in ``included``, depth-first and biggest-``weight``-first within each
    sibling group, ties broken by name. Recurses through an *excluded* category too
    (its subtree can still hold an included one) without emitting a row for it --
    that is how track() shows only budgeted categories while plan_rows() shows every
    ancestor of a shown one, from the same function with a different ``included``.
    """

    def walk(parent_id: Optional[int]) -> List[int]:
        siblings = sorted(
            children.get(parent_id, []),
            key=lambda cid: (-weight(cid), category_by_id[cid].value),
        )
        result: List[int] = []
        for cid in siblings:
            if cid in included:
                result.append(cid)
            result.extend(walk(cid))
        return result

    return walk(None)


# ------------------------------------------------------------------------ plan view

@dataclass(frozen=True)
class PlanRow:
    category_id: int
    name: str
    depth: int
    parent_id: Optional[int]
    avg_minor: int  # mean spend over the averaging window, divided by m regardless
    last_month_minor: int
    budget_minor: Optional[int]  # None if this category has no budget in the plan
    # The budgets set directly beneath this one: each budgeted descendant whose nearest
    # budgeted ancestor is this category (so a grandchild under a budgeted child counts
    # once, in the child). 0 when none.
    subcategory_budget_minor: int = 0
    # This category's own budget is smaller than what its subcategories were given, so
    # the parent cannot actually hold them -- a planning mistake worth flagging.
    overcommitted: bool = False
    # No budget of its own, so ``budget_minor`` is the sum of the budgets beneath it
    # (see _derived_budgets) rather than one the user set.
    budget_derived: bool = False


@dataclass(frozen=True)
class PlanView:
    month: date
    averaging_months: int
    income_avg_minor: int
    income_last_month_minor: int
    income_target_minor: Optional[int]
    rows: List[PlanRow]
    total_budget_minor: int  # top-most budgeted categories only, never double-counted
    unallocated_minor: Optional[int]  # None if there is no income target


def plan_rows(session: Session, month: date, averaging_months: int = 6) -> PlanView:
    """The plan editing view for ``month``: average and last-month actuals per
    category alongside ``month``'s own effective budget, for the ``averaging_months``
    complete months before ``month`` (``month``-1 .. ``month``-``averaging_months``).

    Actuals come from one :func:`stats.build_report` call per averaging-window month
    -- not from a single report over the whole span -- because a month's net
    contribution is computed per month (``-min(0, total_minor)``) before it is
    summed: a category net-positive one month and net-negative the next must not net
    across months into something smaller than either, the way summing raw totals
    first and then clamping would.
    """
    month = month_start(month)
    plan = get_plan(session, month)
    category_by_id, children = _category_tree(session)

    spend_sum: Dict[int, int] = defaultdict(int)
    spend_last_month: Dict[int, int] = {}
    income_sum = 0
    income_last_month = 0

    for i in range(averaging_months):
        report = stats.build_report(session, _month_window(_add_months(month, -(i + 1))))
        month_income = sum(max(0, c.total_minor) for c in report.categories if c.depth == 0)
        income_sum += month_income
        if i == 0:
            income_last_month = month_income
        for c in report.categories:
            spent = -min(0, c.total_minor)
            spend_sum[c.category_id] += spent
            if i == 0:
                spend_last_month[c.category_id] = spent

    def avg(category_id: int) -> int:
        if averaging_months <= 0:
            return 0
        return int(round(spend_sum.get(category_id, 0) / averaging_months))

    avg_income = int(round(income_sum / averaging_months)) if averaging_months > 0 else 0

    # Every category that showed any activity (own or rolled up from a descendant) in
    # the window, plus every budgeted category, plus every ancestor of a budgeted
    # category that had no activity of its own to pull it in already -- so a parent
    # is always shown for indentation above a budgeted or spending child, matching
    # the rollup the statistics panel already shows (see the module docstring).
    included: Set[int] = {cid for cid in spend_sum if cid != queries.UNCATEGORIZED_ID}
    included |= set(plan.expense)
    for category_id in plan.expense:
        node = category_by_id.get(category_id)
        while node is not None and node.parent_id is not None:
            included.add(node.parent_id)
            node = category_by_id.get(node.parent_id)

    ordered = _depth_first_rows(category_by_id, children, included, avg)

    sub_budgets = _subcategory_budgets(category_by_id, plan.expense)
    derived = _derived_budgets(category_by_id, plan.expense)
    rows: List[PlanRow] = []
    for category_id in ordered:
        name, depth, parent_id = _name_depth_parent(category_by_id, category_id)
        own_budget = plan.expense.get(category_id)
        budget = own_budget if own_budget is not None else derived.get(category_id)
        sub_budget = sub_budgets.get(category_id, 0)
        rows.append(
            PlanRow(
                category_id=category_id,
                name=name,
                depth=depth,
                parent_id=parent_id,
                avg_minor=avg(category_id),
                last_month_minor=spend_last_month.get(category_id, 0),
                budget_minor=budget,
                subcategory_budget_minor=sub_budget,
                overcommitted=own_budget is not None and sub_budget > own_budget,
                budget_derived=own_budget is None and category_id in derived,
            )
        )

    if queries.UNCATEGORIZED_ID in spend_sum:
        # Never budgetable (set_amount has no category to resolve it to), but shown
        # whenever it has spend, same as the statistics panel -- see module docstring.
        uncategorized_avg = avg(queries.UNCATEGORIZED_ID)
        uncategorized = PlanRow(
            category_id=queries.UNCATEGORIZED_ID,
            name=stats.UNCATEGORIZED,
            depth=0,
            parent_id=None,
            avg_minor=uncategorized_avg,
            last_month_minor=spend_last_month.get(queries.UNCATEGORIZED_ID, 0),
            budget_minor=None,
        )
        insert_at = len(rows)
        for i, existing in enumerate(rows):
            if existing.depth == 0 and existing.avg_minor < uncategorized_avg:
                insert_at = i
                break
        rows.insert(insert_at, uncategorized)

    top_level_budgeted = {
        category_id
        for category_id in plan.expense
        if _is_top_level_among(category_by_id, category_id, set(plan.expense))
    }
    total_budget_minor = sum(plan.expense[category_id] for category_id in top_level_budgeted)
    unallocated_minor = (
        plan.income_target_minor - total_budget_minor
        if plan.income_target_minor is not None
        else None
    )

    return PlanView(
        month=month,
        averaging_months=averaging_months,
        income_avg_minor=avg_income,
        income_last_month_minor=income_last_month,
        income_target_minor=plan.income_target_minor,
        rows=rows,
        total_budget_minor=total_budget_minor,
        unallocated_minor=unallocated_minor,
    )


# --------------------------------------------------------------------- tracking view

@dataclass(frozen=True)
class TrackRow:
    category_id: int
    name: str
    depth: int
    parent_id: Optional[int]
    budget_minor: int
    spent_minor: int
    left_minor: int
    used: float  # spent / budget; 0.0 if budget_minor <= 0
    ahead_of_pace: bool
    over: bool
    budget_derived: bool = False  # as PlanRow.budget_derived


@dataclass(frozen=True)
class TrackView:
    month: date
    is_current: bool
    elapsed_fraction: float  # 1.0 for a past (or future) month
    income_target_minor: Optional[int]
    income_actual_minor: int
    rows: List[TrackRow]
    total_budget_minor: int  # top-most budgeted categories only
    total_spent_minor: int  # same categories' actuals, same no-double-count rule
    not_budgeted_minor: int  # total_spending_minor - total_spent_minor
    total_spending_minor: int  # every depth-0 category's net spend, budgeted or not


def track(session: Session, month: date, today: Optional[date] = None) -> TrackView:
    """The tracking view for ``month``: each budgeted category's actual spend this
    month against its budget, plus an income row and the totals below it.

    ``today`` is never read from the system clock implicitly by a caller that wants a
    deterministic answer -- pass it explicitly; it only defaults to ``date.today()``
    when omitted.
    """
    month = month_start(month)
    today = today or date.today()
    is_current = month_start(today) == month
    if is_current:
        days_in_month = calendar.monthrange(month.year, month.month)[1]
        elapsed_fraction = today.day / days_in_month
    else:
        elapsed_fraction = 1.0  # a past month is fully elapsed; a future one paces nothing

    plan = get_plan(session, month)
    report = stats.build_report(session, _month_window(month))
    spent_by_id = {c.category_id: -min(0, c.total_minor) for c in report.categories}
    income_actual_minor = sum(max(0, c.total_minor) for c in report.categories if c.depth == 0)
    # Already the window's net spend, computed and signed the same way the
    # statistics panel's own total is -- see Report.net_spend_minor.
    total_spending_minor = -report.net_spend_minor

    category_by_id, _children = _category_tree(session)
    budgeted_ids = set(plan.expense)
    # Parents with no budget of their own get the sum of their subcategories' -- shown,
    # but left out of the totals below, which already count those subcategories.
    derived = _derived_budgets(category_by_id, plan.expense)
    effective = {**derived, **plan.expense}
    ordered = _depth_first_rows(
        category_by_id, _children, set(effective), lambda cid: effective.get(cid, 0)
    )

    rows: List[TrackRow] = []
    for category_id in ordered:
        budget_minor = effective[category_id]
        spent_minor = spent_by_id.get(category_id, 0)
        used = (spent_minor / budget_minor) if budget_minor > 0 else 0.0
        name, depth, parent_id = _name_depth_parent(category_by_id, category_id)
        rows.append(
            TrackRow(
                category_id=category_id,
                name=name,
                depth=depth,
                parent_id=parent_id,
                budget_minor=budget_minor,
                spent_minor=spent_minor,
                left_minor=budget_minor - spent_minor,
                used=used,
                ahead_of_pace=is_current and used > elapsed_fraction + _PACE_TOLERANCE,
                over=spent_minor > budget_minor,
                budget_derived=category_id not in plan.expense,
            )
        )

    top_level_budgeted = {
        category_id
        for category_id in budgeted_ids
        if _is_top_level_among(category_by_id, category_id, budgeted_ids)
    }
    total_budget_minor = sum(plan.expense[category_id] for category_id in top_level_budgeted)
    total_spent_minor = sum(spent_by_id.get(category_id, 0) for category_id in top_level_budgeted)

    return TrackView(
        month=month,
        is_current=is_current,
        elapsed_fraction=elapsed_fraction,
        income_target_minor=plan.income_target_minor,
        income_actual_minor=income_actual_minor,
        rows=rows,
        total_budget_minor=total_budget_minor,
        total_spent_minor=total_spent_minor,
        not_budgeted_minor=total_spending_minor - total_spent_minor,
        total_spending_minor=total_spending_minor,
    )
