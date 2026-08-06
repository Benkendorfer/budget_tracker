"""Time windows and the per-category report behind the statistics panel.

Core logic only: window arithmetic plus reshaping what :mod:`.queries` returns. Nothing
here knows how any of it is drawn.

Averages are per *average* month (365.25 / 12 days) rather than per calendar month, so a
window that starts mid-month is not penalised by a partial month at either end.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from . import queries
from .queries import BUCKET_FORMATS, BucketTotal, TextFilter, VendorFilter

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
    count: int
    total_minor: int
    outflow_minor: int
    inflow_minor: int
    avg_month_minor: int
    share: float  # fraction of the window's outflow, ready for a pie chart
    # Filter-ready, so a UI can drill from a row straight into its transactions:
    # UNCATEGORISED_ID rather than None for the null category.
    category_id: int = queries.UNCATEGORISED_ID


@dataclass(frozen=True)
class Report:
    window: Window
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

    outflow = totals.outflow_minor
    categories = [
        CategoryStat(
            name=row.name or UNCATEGORISED,
            count=row.count,
            total_minor=row.total_minor,
            outflow_minor=row.outflow_minor,
            inflow_minor=row.inflow_minor,
            avg_month_minor=per_month(row.total_minor, window),
            share=(row.outflow_minor / outflow) if outflow else 0.0,
            category_id=queries.UNCATEGORISED_ID if row.id is None else row.id,
        )
        for row in rows
    ]
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
