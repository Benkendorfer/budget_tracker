"""Time windows and the per-category report behind the statistics panel.

Core logic only: window arithmetic plus reshaping what :mod:`.queries` returns. Nothing
here knows how any of it is drawn.

Averages are per *average* month (365.25 / 12 days) rather than per calendar month, so a
window that starts mid-month is not penalised by a partial month at either end.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import queries
from .models import Category
from .queries import BUCKET_FORMATS, BucketTotal, CategoryTotal, TextFilter, VendorFilter

UNCATEGORISED = "Uncategorised"

# The average length of a Gregorian month, used as the avg-per-month divisor.
DAYS_PER_MONTH = 30.4375

# (key, label, months) in the order the UI should offer them.
PRESETS = (
    ("1m", "1 month", 1),
    ("3m", "3 months", 3),
    ("6m", "6 months", 6),
    ("1y", "1 year", 12),
    ("2y", "2 years", 24),
)

RANGE_SEPARATOR = ".."


@dataclass(frozen=True)
class Window:
    key: str  # a preset key, or "custom"
    label: str
    start: date
    end: date  # inclusive

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def months(self) -> float:
        return self.days / DAYS_PER_MONTH


def months_before(day: date, n: int) -> date:
    """``n`` calendar months earlier, clamped to the end of a short month.

    So 2026-03-31 minus one month is 2026-02-28: there is no 31st to land on, and
    stepping back to the 28th keeps the result inside the intended month.
    """
    month = day.month - 1 - n
    year = day.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def resolve(key: str, today: Optional[date] = None) -> Window:
    """Turn a preset key into a window ending today, inclusive."""
    end = today or date.today()
    for preset_key, label, months in PRESETS:
        if preset_key == key:
            # +1 day because both ends are inclusive: "1 month" back from the 6th should
            # start on the 7th of the previous month, not include it twice.
            start = months_before(end, months) + timedelta(days=1)
            return Window(key=preset_key, label=label, start=start, end=end)
    raise ValueError(
        f"Unknown window {key!r}; expected one of {[p[0] for p in PRESETS]}."
    )


def _normalise(text: str) -> str:
    """Lowercase and drop a trailing plural, so "3 month" and "3 months" both match."""
    text = " ".join(text.lower().split())
    return text[:-1] if text.endswith("s") else text


def parse(text: str, today: Optional[date] = None) -> Window:
    """Parse a preset key, a preset label, or an explicit ``YYYY-MM-DD..YYYY-MM-DD``."""
    text = text.strip()
    if not text:
        raise ValueError("Empty window; expected a preset like '3m' or a date range.")

    if RANGE_SEPARATOR in text:
        first, _, second = text.partition(RANGE_SEPARATOR)
        try:
            start = date.fromisoformat(first.strip())
            end = date.fromisoformat(second.strip())
        except ValueError:
            raise ValueError(
                f"Bad date range {text!r}; expected YYYY-MM-DD..YYYY-MM-DD."
            ) from None
        if start > end:
            raise ValueError(f"Window starts after it ends: {start} > {end}.")
        return Window(
            key="custom", label=f"{start} → {end}", start=start, end=end
        )

    wanted = _normalise(text)
    for key, label, _months in PRESETS:
        if wanted in (_normalise(key), _normalise(label)):
            return resolve(key, today)

    options = ", ".join(f"{key} ({label})" for key, label, _ in PRESETS)
    raise ValueError(
        f"Unknown window {text!r}; expected one of {options}, "
        "or a range YYYY-MM-DD..YYYY-MM-DD."
    )


@dataclass(frozen=True)
class CategoryStat:
    name: str  # UNCATEGORISED for the null category
    count: int  # inclusive of every descendant
    total_minor: int  # inclusive of every descendant
    outflow_minor: int  # inclusive of every descendant
    inflow_minor: int  # inclusive of every descendant
    avg_month_minor: int
    share: float  # fraction of the window's total outflow, ready for a pie chart
    # This row's outflow as a fraction of its *parent's* rolled-up outflow (the same
    # figure the parent row displays) — not of the parent's own direct outflow. So
    # siblings under a parent sum to ~1.0 only when the parent holds no direct
    # transactions of its own; otherwise they sum to less, the remainder being the
    # parent's own share of itself. At depth 0 there is no parent, so the denominator is
    # the report's total outflow and this equals ``share``.
    parent_share: float
    # Filter-ready, so a UI can drill from a row straight into its transactions:
    # UNCATEGORISED_ID rather than None for the null category.
    category_id: int = queries.UNCATEGORISED_ID
    parent_id: Optional[int] = None
    depth: int = 0  # 0 for top level
    # This category's own direct figures (not its descendants'), so a UI can tell
    # "Food itself" from "Food and everything under it".
    own_total_minor: int = 0
    own_count: int = 0


@dataclass(frozen=True)
class Report:
    window: Window
    # A flat list in display order: depth-first, each parent immediately followed by its
    # subtree, siblings sorted biggest-spend-first (outflow ascending, name as tiebreak).
    # A UI renders it by indenting on ``depth`` — it must not have to rebuild the tree.
    #
    # Summing only the depth-0 rows reproduces ``outflow_minor``/``inflow_minor`` below
    # exactly, since every row's money is already inclusive of its descendants. Summing
    # *all* rows would double-count a parent's spend with its children's — the future pie
    # chart must use depth-0 rows only.
    categories: List[CategoryStat]
    count: int
    net_minor: int
    outflow_minor: int
    inflow_minor: int
    transfer_count: int

    @property
    def avg_month_outflow_minor(self) -> int:
        return per_month(self.outflow_minor, self.window)

    @property
    def avg_month_inflow_minor(self) -> int:
        return per_month(self.inflow_minor, self.window)


def per_month(value_minor: int, window: Window) -> int:
    """Spread a total over the window, rounded to a whole minor unit."""
    if window.months <= 0:
        return 0
    return int(round(value_minor / window.months))


# (count, total, outflow, inflow) — the shape rolled up and summed through the tree walk.
_Figures = Tuple[int, int, int, int]

_ZERO_FIGURES: _Figures = (0, 0, 0, 0)


def _add(a: _Figures, b: _Figures) -> _Figures:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2], a[3] + b[3])


def _roll_up_categories(
    session: Session,
    rows: List[CategoryTotal],
    window: Window,
    report_outflow: int,
    root_category_id: Optional[int] = None,
) -> List[CategoryStat]:
    """``rows`` (direct, per-exact-category figures from :func:`queries.get_category_totals`)
    turned into the flat, rolled-up, display-ordered list :attr:`Report.categories` promises.

    Ancestors of an involved category are fetched even when they hold no direct
    transactions of their own in this window, since a parent's rolled-up row still has to
    exist and include a child's spend. There are only ever a couple of dozen categories,
    so a Python tree walk over all of them is far clearer than recursive aggregate SQL.

    ``root_category_id``, when the report itself is filtered to one category's subtree
    (a drill-down — see the TUI's category click), stops that ancestor walk there and
    displays it at depth 0, rather than climbing past the filter to categories the report
    excludes. Without this a drill-down into "Dining" would misleadingly show "Food"
    heading the list, wrapping money the filter never included beyond Dining itself.
    """
    direct: Dict[int, CategoryTotal] = {row.id: row for row in rows if row.id is not None}
    uncategorised = next((row for row in rows if row.id is None), None)

    all_categories: Dict[int, Category] = {}
    if direct:
        # Walk each involved category's ancestor chain to pull in every category the
        # rollup needs, fetching one row at a time — the chains are only a few deep and
        # this avoids loading the whole (possibly much larger) category table.
        frontier = set(direct)
        while frontier:
            fetched = {
                cat.id: cat
                for cat in session.scalars(select(Category).where(Category.id.in_(frontier)))
            }
            all_categories.update(fetched)
            frontier = {
                cat.parent_id
                for cat in fetched.values()
                if cat.parent_id is not None
                and cat.id != root_category_id
                and cat.parent_id not in all_categories
            }

    def _display_parent(cat: Category) -> Optional[int]:
        return None if cat.id == root_category_id else cat.parent_id

    children: Dict[Optional[int], List[int]] = defaultdict(list)
    for cat in all_categories.values():
        children[_display_parent(cat)].append(cat.id)

    rolled: Dict[int, _Figures] = {}

    def roll(cid: int) -> _Figures:
        row = direct.get(cid)
        own: _Figures = (
            (row.count, row.total_minor, row.outflow_minor, row.inflow_minor)
            if row is not None
            else _ZERO_FIGURES
        )
        for child_id in children.get(cid, []):
            own = _add(own, roll(child_id))
        rolled[cid] = own
        return own

    for root_id in children.get(None, []):
        roll(root_id)

    def stat_for(cid: int, depth: int, parent_id: Optional[int], parent_outflow: int) -> CategoryStat:
        count, total, outflow, inflow = rolled[cid]
        row = direct.get(cid)
        return CategoryStat(
            name=all_categories[cid].value,
            count=count,
            total_minor=total,
            outflow_minor=outflow,
            inflow_minor=inflow,
            avg_month_minor=per_month(total, window),
            share=(outflow / report_outflow) if report_outflow else 0.0,
            parent_share=(outflow / parent_outflow) if parent_outflow else 0.0,
            category_id=cid,
            parent_id=parent_id,
            depth=depth,
            own_count=row.count if row is not None else 0,
            own_total_minor=row.total_minor if row is not None else 0,
        )

    def display_order(ids: List[int]) -> List[int]:
        # Outflow is negative, so ascending is biggest-spend-first; name breaks ties.
        return sorted(ids, key=lambda cid: (rolled[cid][2], all_categories[cid].value))

    def walk(cid: int, depth: int, parent_id: Optional[int], parent_outflow: int) -> List[CategoryStat]:
        result = [stat_for(cid, depth, parent_id, parent_outflow)]
        _, _, own_outflow, _ = rolled[cid]
        for child_id in display_order(children.get(cid, [])):
            result.extend(walk(child_id, depth + 1, cid, own_outflow))
        return result

    flat: List[CategoryStat] = []
    for root_id in display_order(children.get(None, [])):
        flat.extend(walk(root_id, 0, None, report_outflow))

    if uncategorised is not None:
        outflow = uncategorised.outflow_minor
        stat = CategoryStat(
            name=UNCATEGORISED,
            count=uncategorised.count,
            total_minor=uncategorised.total_minor,
            outflow_minor=outflow,
            inflow_minor=uncategorised.inflow_minor,
            avg_month_minor=per_month(uncategorised.total_minor, window),
            share=(outflow / report_outflow) if report_outflow else 0.0,
            parent_share=(outflow / report_outflow) if report_outflow else 0.0,
            category_id=queries.UNCATEGORISED_ID,
            parent_id=None,
            depth=0,
            own_count=uncategorised.count,
            own_total_minor=uncategorised.total_minor,
        )
        # Uncategorised is a depth-0 row like any other, so it takes part in the same
        # biggest-spend-first order rather than always trailing.
        insert_at = len(flat)
        for i, existing in enumerate(flat):
            if existing.depth == 0 and (existing.outflow_minor, existing.name) > (outflow, ""):
                insert_at = i
                break
        flat.insert(insert_at, stat)

    return flat


def build_report(
    session: Session,
    window: Window,
    account_id: Optional[int] = None,
    category_id: Optional[int] = None,
    vendor_filter: Optional[VendorFilter] = None,
    text_filter: Optional[TextFilter] = None,
) -> Report:
    """Totals and per-category rows for ``window``, honouring the active filters.

    The same filters and date range go to both queries, so the category rows always sum
    back to the report's own outflow and inflow.
    """
    date_range = (window.start, window.end)
    filters = dict(
        account_id=account_id,
        category_id=category_id,
        vendor_filter=vendor_filter,
        text_filter=text_filter,
        date_range=date_range,
    )
    totals = queries.get_totals(session, **filters)
    rows = queries.get_category_totals(session, **filters)

    root_category_id = (
        category_id if category_id not in (None, queries.UNCATEGORISED_ID) else None
    )
    categories = _roll_up_categories(
        session, rows, window, totals.outflow_minor, root_category_id
    )
    return Report(
        window=window,
        categories=categories,
        count=totals.count,
        net_minor=totals.net_minor,
        outflow_minor=totals.outflow_minor,
        inflow_minor=totals.inflow_minor,
        transfer_count=totals.transfer_count,
    )


def _bucket_days(bucket: str, window: Window):
    """One representative day per bucket in the window, chronologically.

    A day at a time, keeping the first day of each distinct bucket. Striding a whole
    bucket from the window's start instead would skip the final partial bucket whenever
    the window does not end on a bucket boundary — and a bucket missing from this list is
    dropped from the series even when it holds transactions.
    """
    key_format = BUCKET_FORMATS[bucket][0]
    seen = set()
    day = window.start
    while day <= window.end:
        key = day.strftime(key_format)
        if key not in seen:
            seen.add(key)
            yield day
        day += timedelta(days=1)


def spending_series(
    session: Session,
    window: Window,
    bucket: str = "month",
    account_id: Optional[int] = None,
    category_id: Optional[int] = None,
    vendor_filter: Optional[VendorFilter] = None,
    text_filter: Optional[TextFilter] = None,
) -> List[BucketTotal]:
    """The window's money bucketed for a bar chart, with empty buckets zero-filled.

    A bar chart that silently omits a quiet month reads as if that month never happened,
    so every bucket in the window gets a row.
    """
    totals = queries.get_bucket_totals(
        session,
        bucket=bucket,
        account_id=account_id,
        category_id=category_id,
        vendor_filter=vendor_filter,
        text_filter=text_filter,
        date_range=(window.start, window.end),
    )
    key_format, label_format = BUCKET_FORMATS[bucket]  # get_bucket_totals validated it
    found: Dict[str, BucketTotal] = {t.key: t for t in totals}
    return [
        found.get(
            day.strftime(key_format),
            BucketTotal(
                key=day.strftime(key_format),
                label=day.strftime(label_format),
                count=0,
                outflow_minor=0,
                inflow_minor=0,
            ),
        )
        for day in _bucket_days(bucket, window)
    ]
