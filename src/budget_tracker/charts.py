"""Bar-chart geometry for a spending series.

Scaling and block-character layout, and nothing else: no Textual, no widgets, no
session. :func:`stats.spending_series` fetches the money, this turns it into bars, and
the TUI only has to put the strings in a table. That split is what makes the awkward
parts — the peak scaling, an all-zero window, a bucket that lands exactly on nothing —
testable without a running app.

Three measures, because they answer different questions:

``spend``
    What went out, bucket by bucket. Left-anchored bars growing right, in eighth-cell
    steps, the whole width of the column.
``income``
    What came in, drawn the same way.
``net``
    The two added together, drawn either side of a centre axis: a bucket that cost more
    than it paid grows **left**, one that paid more than it cost grows **right**. So a
    month whose spending was matched by a refund sits at the axis instead of reading as
    a heavy month.

The net bars are whole cells where the others are eighths. A bar growing leftwards is
anchored at its right-hand end, and Unicode has no left-facing counterpart to the
``▏▎▍`` eighth blocks — only a half and an eighth — so sub-cell resolution is available
on one side of the axis and not the other. Quantising both sides of ``net`` to whole
cells keeps its two directions honestly comparable with each other, which matters more
than matching the resolution of a chart you are not looking at at the same time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional, Tuple

from .queries import BUCKET_FORMATS, BucketTotal
from .stats import BucketCategories, CategoryStat, Window

BLOCK = "█"

# Eighth-width blocks, 1/8 through 8/8, for the single-direction measures.
BAR_BLOCKS = "▏▎▍▌▋▊▉█"

# The centre line of a ``net`` chart. Bars never overwrite it, so it stays at one screen
# column all the way down the table and the eye can read "left of here cost me" at a
# glance.
AXIS = "│"

DEFAULT_WIDTH = 27

# In the order ``m`` cycles them. ``net`` leads because it is the default: it is the one
# that says whether a month was actually affordable.
MEASURES = ("net", "spend", "income")

# What each measure is called on the bar column's header. The net header has to explain
# its own axis — a diverging bar with no legend is a puzzle.
MEASURE_HEADERS = {
    "net": "Net (← out | in →)",
    "spend": "Spending",
    "income": "Income",
}

# Spellings accepted on the command line, including the header words themselves.
MEASURE_ALIASES = {
    "net": "net",
    "spend": "spend",
    "spending": "spend",
    "out": "spend",
    "income": "income",
    "in": "income",
}

# How long a window has to be before a finer bucket stops being readable. Daily bars are
# only worth it for a month or so; past half a year they are unreadable and monthly is
# the only sensible default.
_BUCKET_THRESHOLDS = ((45, "day"), (200, "week"))
FALLBACK_BUCKET = "month"


def choose_bucket(window: Window) -> str:
    """The bucket size a window of this length should default to."""
    for max_days, bucket in _BUCKET_THRESHOLDS:
        if window.days <= max_days:
            return bucket
    return FALLBACK_BUCKET


def bucket_date_range(key: str, bucket: str, window: Window) -> Tuple[date, date]:
    """The days of ``window`` that fall in the bucket named ``key`` — a bar's own range.

    Walks the window day by day and keeps the first and last day whose bucket key
    matches, rather than reconstructing the bucket's calendar boundaries from the key
    (which for ``week`` would mean redoing strftime's own ``%W`` arithmetic). That walk
    is also what makes the answer correct at the edges for free: the first and last
    buckets of a window are usually partial — a month bucket in a window that starts
    2025-08-08 must not claim July 2025-08-01..07, days the chart never drew — and a day
    outside ``window`` is never visited, so the range this returns is automatically
    clamped to it on both ends.

    Raises if ``key`` never occurs in ``window`` — a caller passing back a bar's own key
    from a chart built over this same window should never see that happen.
    """
    key_format = BUCKET_FORMATS[bucket][0]
    start: Optional[date] = None
    end: Optional[date] = None
    day = window.start
    while day <= window.end:
        if day.strftime(key_format) == key:
            if start is None:
                start = day
            end = day
        day += timedelta(days=1)
    if start is None or end is None:
        raise ValueError(
            f"Bucket {key!r} does not occur in window {window.start}..{window.end}."
        )
    return start, end


def parse_measure(text: str) -> str:
    """A measure name or alias, normalised. Raises on anything else."""
    measure = MEASURE_ALIASES.get(text.strip().lower())
    if measure is None:
        raise ValueError(
            f"Unknown measure {text!r}; expected one of {', '.join(MEASURES)}."
        )
    return measure


def bar_text(fraction: float, width: int) -> str:
    """``fraction`` of ``width`` cells, drawn left-anchored in eighth-cell steps.

    Any nonzero fraction gets at least the thinnest sliver. Rounding a small-but-real
    bucket down to an empty cell would draw it identically to a bucket in which nothing
    happened at all, which is the one thing a spending chart must not do.
    """
    if width < 1:
        raise ValueError(f"Bar width must be at least 1, got {width}.")
    if fraction <= 0:
        return ""
    eighths = min(max(int(round(fraction * width * 8)), 1), width * 8)
    full, rest = divmod(eighths, 8)
    return BLOCK * full + (BAR_BLOCKS[rest - 1] if rest else "")


def bar_cells(fraction: float, half_width: int) -> int:
    """How many whole cells ``fraction`` earns, with the same nonzero-is-visible rule."""
    if half_width < 1:
        raise ValueError(f"Half-width must be at least 1, got {half_width}.")
    if fraction <= 0:
        return 0
    return min(max(int(round(fraction * half_width)), 1), half_width)


def diverging_bar(
    value_minor: int, fraction: float, half_width: int
) -> Tuple[str, str]:
    """``(left, right)``: the cells either side of the axis, both fixed-width.

    Negative money goes left and is right-aligned so it grows *away* from the axis;
    positive goes right and is left-aligned for the same reason. Both sides are padded to
    ``half_width`` so the axis between them never moves down the column.
    """
    cells = bar_cells(fraction, half_width)
    if value_minor < 0:
        return (BLOCK * cells).rjust(half_width), " " * half_width
    return " " * half_width, (BLOCK * cells).ljust(half_width)


@dataclass(frozen=True)
class Bar:
    key: str  # the bucket's sortable key, e.g. "2026-03"
    label: str  # short axis label, e.g. "2026-03"
    count: int
    # All three figures on every bar, whichever one is being drawn, so the table can show
    # a second column of context without the caller re-deriving it.
    net_minor: int  # signed: negative when the bucket cost more than it paid
    outflow_minor: int  # money out, as a positive magnitude
    inflow_minor: int  # money in, positive
    value_minor: int  # the charted measure's own value — what the bar's length means
    fraction: float  # |value| as a fraction of the window's peak, 0.0–1.0
    left: str  # cells left of the axis; "" unless the measure diverges
    axis: str  # AXIS, or "" unless the measure diverges
    right: str  # the bar itself, or the right-hand side of a diverging one

    @property
    def bar(self) -> str:
        """The whole cell as one string — what the chart looks like without styling."""
        return f"{self.left}{self.axis}{self.right}"


@dataclass(frozen=True)
class Chart:
    bars: List[Bar]
    measure: str
    width: int
    peak_minor: int  # the largest single |value|; 0 when there is nothing to draw
    net_minor: int  # summed over every bucket
    outflow_minor: int  # summed, positive
    inflow_minor: int

    @property
    def diverging(self) -> bool:
        return self.measure == "net"

    @property
    def total_minor(self) -> int:
        """The charted measure summed over the window — what a TOTAL row should show."""
        return {
            "net": self.net_minor,
            "spend": self.outflow_minor,
            "income": self.inflow_minor,
        }[self.measure]

    @property
    def avg_minor(self) -> int:
        """Mean of the charted measure per bucket, rounded."""
        if not self.bars:
            return 0
        return int(round(self.total_minor / len(self.bars)))


def _value(total: BucketTotal, measure: str) -> int:
    if measure == "net":
        return total.outflow_minor + total.inflow_minor
    if measure == "spend":
        return -total.outflow_minor
    return total.inflow_minor


def build(
    series: List[BucketTotal], measure: str = "net", width: int = DEFAULT_WIDTH
) -> Chart:
    """Scale a zero-filled series to its own largest bucket.

    Scaling to the peak rather than to a fixed money amount is what makes the shape
    readable at any level; the cost is that two charts are only comparable via their
    ``peak_minor``, which is why the UI shows it. For ``net``, one peak covers both
    directions, so a month that earned 500 and one that cost 500 draw as mirror images
    rather than each filling its own side.

    A window with nothing in it produces bars of no cells rather than a division by zero
    — an empty chart, which is the honest rendering of one.
    """
    if measure not in MEASURES:
        raise ValueError(
            f"Unknown measure {measure!r}; expected one of {', '.join(MEASURES)}."
        )
    diverging = measure == "net"
    # The axis costs a column, and the two halves have to be equal or the centre would
    # drift; an even width therefore loses one cell rather than an uneven half.
    half_width = (width - len(AXIS)) // 2

    values = [_value(total, measure) for total in series]
    peak = max((abs(value) for value in values), default=0)

    bars = []
    for total, value in zip(series, values):
        fraction = (abs(value) / peak) if peak else 0.0
        if diverging:
            left, right = diverging_bar(value, fraction, half_width)
            axis = AXIS
        else:
            left, axis, right = "", "", bar_text(fraction, width)
        bars.append(
            Bar(
                key=total.key,
                label=total.label,
                count=total.count,
                net_minor=total.outflow_minor + total.inflow_minor,
                outflow_minor=-total.outflow_minor,
                inflow_minor=total.inflow_minor,
                value_minor=value,
                fraction=fraction,
                left=left,
                axis=axis,
                right=right,
            )
        )
    return Chart(
        bars=bars,
        measure=measure,
        width=width,
        peak_minor=peak,
        net_minor=sum(bar.net_minor for bar in bars),
        outflow_minor=sum(bar.outflow_minor for bar in bars),
        inflow_minor=sum(bar.inflow_minor for bar in bars),
    )


# ----------------------------------------------------------------------------- pie
#
# Category shares as a circle. Geometry only, same split as the bars above: this decides
# which cell belongs to which slice, and the TUI decides what colour that is.

# The pie's bounding box, in character cells. Twice as wide as it is tall, because a
# monospace terminal cell is itself roughly twice as tall as it is wide — a box this
# shape is what reads as a circle on screen rather than a vertical oval (checked by
# actually rendering it; see test_pie_panel_looks_like_a_circle in test_tui.py).
PIE_WIDTH = 40
PIE_HEIGHT = 20


# ------------------------------------------------------------------ the share bar

# One row, one cell per percent. A circle drawn in character cells is coarse — at the
# panel's size a slice below a few percent is a couple of ragged cells that read as
# noise — where a straight bar spends every cell it has on length, which is the one
# thing the eye compares accurately.
SHARE_BAR_WIDTH = 100

# Below this, a category is folded into "Other" rather than drawn. A slice thinner than
# a couple of cells cannot be told apart from its neighbours or matched to its legend
# entry, so drawing it separately claims a precision the display does not have.
OTHER_LABEL = "Other"
MIN_SEGMENT_CELLS = 2


@dataclass(frozen=True)
class ShareSegment:
    """One run of cells in the bar, in the order it is drawn, left to right."""

    name: str
    # None for the folded "Other" segment, which stands for several categories and so
    # cannot be drilled into. Making it None rather than borrowing a sentinel forces a
    # caller to notice: pointing a drill-down at UNCATEGORISED_ID here would quietly
    # show uncategorised transactions instead of the ones the segment represents.
    category_id: Optional[int]
    share: float  # the true share, before it was rounded to whole cells
    amount_minor: int  # net spend, as a positive magnitude
    cells: int  # how much of the bar it occupies
    is_other: bool = False  # the folded tail, which has no single category behind it


@dataclass(frozen=True)
class ShareBar:
    segments: List[ShareSegment]
    width: int
    total_minor: int  # every segment's spend added up

    @property
    def cell_owners(self) -> List[int]:
        """One index into ``segments`` per cell, so a caller can colour cell by cell.

        Exactly ``width`` long whenever there is anything to draw. No colours and no
        characters here, on purpose: this module knows proportions, and the UI turns an
        index into a colour.
        """
        owners: List[int] = []
        for index, segment in enumerate(self.segments):
            owners.extend([index] * segment.cells)
        return owners


def _apportion(shares: List[float], width: int) -> List[int]:
    """Split ``width`` cells across ``shares`` so the parts sum to exactly ``width``.

    Largest-remainder apportionment: floor every share, then hand the leftover cells to
    whoever was rounded down hardest. Rounding each share independently would leave the
    bar a cell or two short or long depending on the data, so its right-hand end would
    wander between windows — and a bar labelled "100%" that does not fill its own track
    is exactly the kind of small wrongness nobody reports but everybody notices.
    """
    exact = [share * width for share in shares]
    cells = [int(value) for value in exact]
    shortfall = width - sum(cells)
    # Ties broken by position so the same data always produces the same bar.
    order = sorted(
        range(len(shares)), key=lambda i: (-(exact[i] - cells[i]), i)
    )
    for i in order[:shortfall]:
        cells[i] += 1
    return cells


def build_share_bar(
    categories: List[CategoryStat], width: int = SHARE_BAR_WIDTH
) -> ShareBar:
    """Depth-0 category shares as a single proportional bar ``width`` cells long.

    Only ``depth == 0`` rows with a nonzero
    share, because summing every depth would count a parent with its children (see
    stats.Report.categories), and a net-positive row is already 0.0 by construction.

    Categories too thin to draw honestly are folded into a single trailing ``Other``
    segment rather than dropped, so the bar still accounts for the whole window and the
    legend can say what the remainder was worth. Everything is measured against the real
    shares first and only then rounded, so folding never changes what the kept segments
    are worth.

    At the default width one cell is one percent, which is what makes the bar readable
    without a scale printed under it.
    """
    if width < 1:
        raise ValueError(f"Share bar width must be at least 1, got {width}.")

    kept = [cat for cat in categories if cat.depth == 0 and cat.share > 0]
    if not kept:
        return ShareBar(segments=[], width=width, total_minor=0)

    # Fold the tail first, against true shares, so the decision does not depend on a
    # rounding that has not happened yet.
    threshold = MIN_SEGMENT_CELLS / width
    major = [cat for cat in kept if cat.share >= threshold]
    minor = [cat for cat in kept if cat.share < threshold]
    if not major:
        # Everything is tiny — a window split across very many small categories. Keep
        # the largest rather than folding the entire bar into a single "Other", which
        # would be technically true and completely useless.
        major, minor = [max(kept, key=lambda c: c.share)], []
        minor = [cat for cat in kept if cat is not major[0]]

    # Biggest first. Report.categories is ordered by gross outflow while `share` is net,
    # so the two diverge whenever a category has refunds in it — and a proportional bar
    # whose segments do not descend reads as broken even when every number on it is
    # right. Sorting by the quantity actually being drawn is what makes it legible.
    major.sort(key=lambda cat: (-cat.share, cat.name))

    shares = [cat.share for cat in major]
    other_share = sum(cat.share for cat in minor)
    if minor:
        shares.append(other_share)

    cells = _apportion(shares, width)

    segments = [
        ShareSegment(
            name=cat.name,
            category_id=cat.category_id,
            share=cat.share,
            amount_minor=-cat.total_minor,
            cells=count,
        )
        for cat, count in zip(major, cells)
    ]
    if minor:
        segments.append(
            ShareSegment(
                name=OTHER_LABEL,
                category_id=None,
                share=other_share,
                amount_minor=sum(-cat.total_minor for cat in minor),
                cells=cells[-1],
                is_other=True,
            )
        )
    return ShareBar(
        segments=segments,
        width=width,
        total_minor=sum(segment.amount_minor for segment in segments),
    )


# --------------------------------------------------------------- the stacked share bar
#
# One :func:`build_share_bar` for the whole window, plus one full-width bar per time
# bucket -- composition over time, comparable only because every bar draws the same
# segments in the same order (see build_stacked_share's docstring for why that is the
# one thing this layout cannot get wrong).


@dataclass(frozen=True)
class StackedBar:
    """One time bucket's own composition, drawn full width -- its own 100%, not a
    fraction of the window's."""

    key: str
    label: str
    total_minor: int  # this bucket's own net spend; the bar's own 100%
    # Same segment identities (name, category_id, is_other), same order, as the chart's
    # own `segments` -- only `share`/`amount_minor`/`cells` vary bucket to bucket. Empty
    # when the bucket had no net spend at all: an empty bar, not a missing row (the row
    # itself still exists in `StackedShareChart.bars`).
    segments: List[ShareSegment]


@dataclass(frozen=True)
class StackedShareChart:
    # The category set every bar -- `top` and each of `bars` -- draws, in the one order
    # they all share. Read once here rather than off any particular bar, so a caller has
    # exactly one place to build a legend and assign colours from.
    segments: List[ShareSegment]
    top: ShareBar  # the window-wide bar, unchanged from calling build_share_bar directly
    bars: List[StackedBar]
    width: int


def build_stacked_share(
    categories: List[CategoryStat],
    buckets: List[BucketCategories],
    width: int = SHARE_BAR_WIDTH,
) -> StackedShareChart:
    """The window-wide share bar plus one full-width bar per time bucket, all sharing
    the same segments in the same order.

    ``categories`` is the window's own depth-0 rollup (``stats.Report.categories``) and
    decides the segment set exactly as :func:`build_share_bar` already does; ``buckets``
    is the per-bucket rollup of the same window (``stats.category_share_series``), one
    entry per bucket in chronological order.

    The top bar's segments are the *only* thing that decides what a category is called
    and where it sits: a bucket where some category was large, or one where it did not
    appear at all, still draws every one of the top bar's segments in the same
    positions -- zero-width where that bucket had nothing -- and anything the top bar
    folded into ``Other`` (too small over the whole window) or never carried at all
    (net positive over the whole window, even if a real cost in this one bucket) goes
    into that bucket's ``Other`` too. A bucket is allowed to pick its own segments in
    no version of this chart: that is what would let the same colour mean two different
    categories on two different rows, which defeats the entire point of stacking them
    for comparison. Each bucket's own ``Other``, unlike the top bar's, can therefore
    hold money the top bar's own ``Other`` does not -- see the ``needs_other`` guard
    below -- so a category never silently loses its bucket-level spend just because it
    nets out over the whole window.

    A bucket with no net spend at all draws as an empty bar (``segments == []``) rather
    than a missing row, matching how :func:`build_share_bar` treats an empty window.
    """
    top = build_share_bar(categories, width=width)
    real_ids = [seg.category_id for seg in top.segments if not seg.is_other]
    real_id_set = set(real_ids)

    def other_spend(bucket: BucketCategories) -> int:
        return sum(
            -min(0, cat.total_minor)
            for cat in bucket.categories
            if cat.depth == 0 and cat.category_id not in real_id_set
        )

    # The top bar reserves an Other slot whenever it folded a sliver into one; a bucket
    # can also need the slot on its own, for a category that is entirely invisible in
    # `top` (net positive over the whole window) but a real cost in that one bucket.
    needs_other = any(seg.is_other for seg in top.segments) or any(
        other_spend(bucket) > 0 for bucket in buckets
    )

    if needs_other and not any(seg.is_other for seg in top.segments):
        # `top` itself drew no Other -- add a zero-share placeholder so `segments`
        # still matches what every bucket bar below actually draws.
        legend = list(top.segments) + [
            ShareSegment(
                name=OTHER_LABEL, category_id=None, share=0.0, amount_minor=0, cells=0,
                is_other=True,
            )
        ]
    else:
        legend = top.segments
    segment_ids = [seg.category_id for seg in legend]
    segment_names = [seg.name for seg in legend]

    bars: List[StackedBar] = []
    for bucket in buckets:
        by_id = {cat.category_id: cat for cat in bucket.categories if cat.depth == 0}
        amounts = [
            -min(0, by_id[cid].total_minor) if cid in by_id else 0 for cid in real_ids
        ]
        if needs_other:
            amounts.append(other_spend(bucket))
        bucket_total = sum(amounts)

        if bucket_total == 0:
            bars.append(
                StackedBar(key=bucket.key, label=bucket.label, total_minor=0, segments=[])
            )
            continue

        shares = [amount / bucket_total for amount in amounts]
        cells = _apportion(shares, width)
        segments = [
            ShareSegment(
                name=name, category_id=cid, share=share, amount_minor=amount, cells=count,
                is_other=(cid is None),
            )
            for name, cid, share, amount, count in zip(
                segment_names, segment_ids, shares, amounts, cells
            )
        ]
        bars.append(
            StackedBar(
                key=bucket.key, label=bucket.label, total_minor=bucket_total,
                segments=segments,
            )
        )

    return StackedShareChart(segments=legend, top=top, bars=bars, width=width)
