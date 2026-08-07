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

from dataclasses import dataclass
from typing import List, Tuple

from .queries import BucketTotal
from .stats import Window

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
