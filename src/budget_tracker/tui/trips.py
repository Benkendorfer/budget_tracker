"""The trips panel: table rendering, the per-trip bucket bar, folding, and its legend.

``BudgetApp`` keeps the trip rows (fetched over a session -- see
``BudgetApp._build_trips``) and which trips are unfolded; everything here is pure given
those, the same split ``tui/stats.py`` and ``tui/pie.py`` already follow.

Folding here deliberately does *not* reuse ``BudgetApp._collapsed``/``_foldable_ids``
(the statistics panel's own state): those are keyed by category_id, and a trip is a
``Tag`` row with its own, independently-assigned id -- sharing one ``Set`` between the
two would mean folding category #3 in the statistics panel could silently fold trip #3
here too, the moment both happen to exist. A trip's fold state also starts *collapsed*
(space "unfolds" it) rather than expanded, unlike the statistics panel's fully-expanded
default -- there is no pre-folding behavior here to stay byte-for-byte with, and one row
per bucket (``len(trips.BUCKETS)`` of them) on first open would bury the one line (dates,
cost, bar) most of what this panel is for. ``toggle_fold``/``toggle_fold_all`` below are
therefore a small, deliberate duplicate of ``stats.toggle_fold``/``toggle_fold_all``'s
shape, not a shared import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

from rich.text import Text
from textual import events
from textual.widgets import DataTable

from .. import trips as trips_module
from ..charts import BLOCK
from ..queries import TripRow
from .formatting import FOLD_INDICATOR, OTHER_COLOR, PIE_COLORS, _fmt_amount, _truncate

# One color per bucket, in trips.BUCKETS order: every PIE_COLORS entry, in order, for
# the real buckets, and the shared neutral gray for the trailing misc -- the same
# convention PIE_COLORS already documents (gray means "a catch-all standing in for
# several things"), which is exactly what misc is. Derived from len(trips.BUCKETS)
# rather than a literal count, so a bucket trips.py adds later still gets a color
# instead of silently wrapping onto one already in use -- PIE_COLORS itself would need
# to grow first if the real-bucket count ever exceeds it (see too_many_colors() in
# tui/pie.py for how that same situation is flagged there rather than left ambiguous).
# A bucket is the same color in every trip's bar and in the legend above the table.
BUCKET_COLORS: Tuple[str, ...] = PIE_COLORS[: len(trips_module.BUCKETS) - 1] + (OTHER_COLOR,)


def _cost_cell(minor: int) -> Text:
    """A trip's cost, red.

    This is the one column in the app where the sign convention is inverted, so it
    cannot use ``formatting._amount_cell``. Everywhere else a figure is a signed
    balance and red means it went negative; here every figure is already a *cost*,
    reported positive (see ``queries.get_trips``), and money spent should read the same
    red it does everywhere else rather than green for being a positive number.

    A bucket whose refunds outran its spending is the genuine exception -- that is
    money that came back -- so a negative cost stays green, which keeps red/green
    meaning "out"/"in" throughout the app even though the sign flipped.
    """
    return Text(_fmt_amount(minor), style="green" if minor < 0 else "red", justify="right")


def _per_day_cell(minor: int, days: Optional[int]) -> Text:
    """Cost per day of the trip, or blank when there are no dates to divide by.

    Blank rather than zero: a trip with no dates has no *unknown* daily cost, it has
    no denominator at all, and a "0.00" would read as a trip that cost nothing a day.
    Colored like the cost beside it (see :func:`_cost_cell`) so the two agree.
    """
    if not days:
        return Text("")
    per_day = round(minor / days)
    return Text(
        _fmt_amount(per_day), style="green" if per_day < 0 else "red", justify="right"
    )


class TripTable(DataTable):
    """The trips table, which opens with no trip highlighted.

    A ``DataTable`` always has a cursor somewhere, so the panel would otherwise open
    with the first trip lit up -- which reads as "this one is selected" when the user
    has not chosen anything, and is actively misleading on a screen whose whole job is
    comparing trips against each other.

    The cursor is therefore hidden until the user touches the table, and from that
    point on the table behaves exactly like every other one in the app. Revealing on
    *any* key rather than only on the arrows is deliberate: ``space`` folds whichever
    trip the cursor is on, and folding a row the user cannot see would be worse than
    showing the cursor a keystroke early.
    """

    def hide_cursor(self) -> None:
        """Called when the panel is (re)built, so reopening it starts clean again."""
        self.show_cursor = False

    async def _on_key(self, event: events.Key) -> None:
        self.show_cursor = True

    async def _on_click(self, event: events.Click) -> None:
        self.show_cursor = True


# Column widths, fixed regardless of terminal size -- see bar_width() for the one
# column that is not. START_WIDTH/END_WIDTH each hold an ISO date (10) plus the "*"
# that marks one set by hand; TRIP_WIDTH matches the sidebar's own vendor-name
# magnitude, long names truncated with an ellipsis; COST_WIDTH fits a signed six-figure
# home-currency amount ("-999,999.99" is 12) with nothing to spare, same magnitude as
# the chart's own money columns.
#
# Start and end are two columns rather than one combined "2026-03-02..03-14" field:
# they are two facts, each independently overridable (models.Tag.start_date/end_date),
# and a combined field cannot be scanned down a column. The pair costs 26 columns
# against the old 24, which the Breakdown column absorbs without dropping to the
# narrower bar -- see bar_width().
START_WIDTH = 11
END_WIDTH = 11
TRIP_WIDTH = 22
# 10 rather than 12: a trip is not a lifetime of spending, and "99,999.99" is already
# well past any of them. The two columns the trim pays for -- Cost/day -- land the bar
# back on exactly 100 at the user's terminal, which is worth more than headroom no trip
# will use (see bar_width, and the render test that checks this rather than trusting it).
COST_WIDTH = 10
COST_PER_DAY_WIDTH = 9
# DataTable pads every column two cells (one each side), matching every other table's
# own column-budget comments in this app (see e.g. tui/chart.py's fill_chart).
COLUMN_PADDING = 2
# The panel's own round border, one column either side -- see bar_width()'s docstring.
BORDER_OVERHEAD = 2

# The bar's width: 100 cells when the Breakdown column has room for it, 50 otherwise.
# Both candidates live in one constant, widest first, so bar_width() has nothing to
# guess about which to prefer.
BAR_WIDTHS: Tuple[int, ...] = (100, 50)


def bar_width(main_panel_width: int) -> int:
    """100 when the Breakdown column has room in a panel this wide, else 50.

    ``main_panel_width`` is ``#main``'s own live width (the sidebar's 36 columns
    already excluded) -- read from the mounted widget by the caller rather than
    guessed, since a column width that "looks fine" has shipped off-screen before (see
    the panel's own render-and-check tests). Subtracts the table's round border and the
    three fixed columns, each with DataTable's own padding, to find what is actually
    left for Breakdown.
    """
    available = (
        main_panel_width
        - BORDER_OVERHEAD
        - (START_WIDTH + COLUMN_PADDING)
        - (END_WIDTH + COLUMN_PADDING)
        - (TRIP_WIDTH + COLUMN_PADDING)
        - (COST_WIDTH + COLUMN_PADDING)
        - (COST_PER_DAY_WIDTH + COLUMN_PADDING)
        - COLUMN_PADDING  # Breakdown's own
    )
    for width in BAR_WIDTHS:
        if available >= width:
            return width
    return BAR_WIDTHS[-1]


def _date_cell(day, is_manual: bool) -> Text:
    """One end of a trip: dimmed with a ``*`` when it was *derived* rather than set.

    The marker is on the guess, not on the correction. A date taken from the earliest
    or latest transaction is the app's inference and may well be wrong -- a flight
    booked months ahead, a last purchase days before flying home -- while a date the
    user typed is the one fact on the row nobody needs to check. Marking the derived
    one puts the flag on what still wants attention, and lets a column be scanned for
    trips that have not been confirmed yet.

    Blank for a trip with nothing to derive it from and no override. Matches
    ``budget trips``'s own ``*``, so a trip reads the same in both.
    """
    if day is None:
        return Text("")
    text = day.isoformat() + ("" if is_manual else "*")
    return Text(text, style="" if is_manual else "dim")


def _apportion(shares: List[float], width: int) -> List[int]:
    """Split ``width`` cells across ``shares`` (each already 0..1, summing to ~1) so
    the parts sum to exactly ``width`` -- largest-remainder apportionment, the same
    algorithm charts.build_share_bar uses for the same reason: rounding each share
    independently would leave the bar a cell or two short or long depending on the
    data. Not imported from charts.py: that module's own _apportion is private, and
    this panel's segments are trips.BUCKETS's fixed vocabulary rather than a folded,
    sorted category list, so reshaping this bar to fit build_share_bar's shape would
    cost more than the dozen lines below.
    """
    exact = [share * width for share in shares]
    cells = [int(value) for value in exact]
    shortfall = width - sum(cells)
    order = sorted(range(len(shares)), key=lambda i: (-(exact[i] - cells[i]), i))
    for i in order[:shortfall]:
        cells[i] += 1
    return cells


def _bucket_cells(buckets: Sequence[int], width: int) -> List[int]:
    """One bucket-index per cell of the bar.

    Negative buckets (refunds outweighing spend) are clamped to zero *here only* --
    queries.TripRow.buckets keeps the real signed figure for the unfolded row, so the
    bar and the number beside it are allowed to disagree in that one case rather than
    the bar quietly lying about having a negative length.
    """
    clamped = [max(0, amount) for amount in buckets]
    total = sum(clamped)
    if total <= 0:
        return []
    shares = [amount / total for amount in clamped]
    cells = _apportion(shares, width)
    owners: List[int] = []
    for index, count in enumerate(cells):
        owners.extend([index] * count)
    return owners


def _bar_text(buckets: Sequence[int], width: int) -> Text:
    """``width`` cells, each colored by whichever bucket owns it -- blank, not
    missing, for a trip with nothing to draw (no transactions, or every bucket net
    positive), padded to the same width as every other row's bar.
    """
    owners = _bucket_cells(buckets, width)
    text = Text()
    for owner in owners:
        text.append(BLOCK, style=BUCKET_COLORS[owner])
    text.append(" " * (width - len(owners)))
    return text


@dataclass(frozen=True)
class TripPanelRow:
    """One rendered row of the trips table: either a trip itself, or -- once unfolded
    -- one of its trips.BUCKETS rows. Parallel to the table so BudgetApp can map a
    cursor row back to what is actually there, the same discipline stats._stats_rows
    already follows for the statistics panel.
    """

    trip: TripRow  # always the parent trip, even for a bucket sub-row
    bucket_index: Optional[int] = None  # index into trips.BUCKETS / trip.buckets, else None


def bucket_category_ids(mapping: Dict[int, str], bucket: str) -> Tuple[Optional[int], ...]:
    """Every category id ``mapping`` (trips_module.resolve_buckets) resolves to
    ``bucket``, for BudgetApp's own bucket-row drill-down (see app._drill_into_trip_row).

    Plain filtering, not aggregation, so it stays here rather than in trips.py: the
    domain module already hands back the resolved map, and this only picks the ids a
    bucket drill-down needs out of it. ``None`` is appended for
    :data:`trips_module.MISC` -- an uncategorized transaction falls into misc the same
    way ``queries.get_trips`` already treats it (see that function's own docstring), so
    misc's own drill-down has to reach those rows too, or every uncategorized
    transaction on the trip would silently vanish from its own breakdown.
    """
    ids: Tuple[Optional[int], ...] = tuple(
        category_id for category_id, resolved in mapping.items() if resolved == bucket
    )
    if bucket == trips_module.MISC:
        ids = ids + (None,)
    return ids


def fill_trips(
    table: DataTable, rows: List[TripRow], expanded: Set[int], width: int
) -> Tuple[List[TripPanelRow], Set[int]]:
    """Render the trips table, honouring which trips are unfolded.

    Every trip is foldable -- trips.BUCKETS is a fixed vocabulary rather than a real
    tree, unlike the statistics panel, so there is no "leaf row" case to exclude.
    Returns the rows actually rendered (parallel to the table) and the foldable trip
    ids, so BudgetApp can keep a table row index mapped back to the right TripRow --
    see toggle_fold(), which depends on it.
    """
    table.clear(columns=True)
    table.add_column("Start", width=START_WIDTH)
    table.add_column("End", width=END_WIDTH)
    table.add_column("Trip", width=TRIP_WIDTH)
    table.add_column("Cost", width=COST_WIDTH)
    table.add_column("Cost/day", width=COST_PER_DAY_WIDTH)
    table.add_column("Breakdown", width=width)

    foldable_ids = {row.id for row in rows}
    panel_rows: List[TripPanelRow] = []
    for row in rows:
        panel_rows.append(TripPanelRow(trip=row))
        is_expanded = row.id in expanded
        label = row.name if is_expanded else f"{FOLD_INDICATOR} {row.name}"
        table.add_row(
            _date_cell(row.start, row.start_is_manual),
            _date_cell(row.end, row.end_is_manual),
            _truncate(label, TRIP_WIDTH),
            _cost_cell(row.total_minor),
            _per_day_cell(row.total_minor, row.days),
            _bar_text(row.buckets, width),
        )
        if not is_expanded:
            continue
        clamped_total = sum(max(0, amount) for amount in row.buckets)
        for index, bucket in enumerate(trips_module.BUCKETS):
            cost = row.buckets[index]
            # A bucket the trip spent nothing in is left out rather than printed as a
            # row of zeros. Most trips touch three or four of the eight, so showing all
            # of them buries the ones that matter under padding -- and the bar above
            # already draws nothing for them. `budget trips` skips them for the same
            # reason, so the two surfaces agree.
            if cost == 0:
                continue
            panel_rows.append(TripPanelRow(trip=row, bucket_index=index))
            # Same clamped basis as the bar itself (see _bucket_cells), so the share
            # printed here always matches what the bar actually drew -- a refunded
            # bucket reads 0.0% here even though its cost beside it is still negative.
            share = max(0, cost) / clamped_total if clamped_total else 0.0
            table.add_row(
                "",  # Start
                "",  # End
                Text(f"  {bucket}", style=BUCKET_COLORS[index]),
                _cost_cell(cost),
                _per_day_cell(cost, row.days),
                Text(_fmt_share(share), style=BUCKET_COLORS[index], justify="right"),
            )
    _add_total_row(table, rows, width)
    return panel_rows, foldable_ids


def _add_total_row(table: DataTable, rows: List[TripRow], width: int) -> None:
    """A closing row summing every trip: all travel, in one line.

    Deliberately **not** appended to ``panel_rows``, exactly as
    ``stats._add_stats_total_row`` is left out of ``stats_rows``: every bounds check
    that maps a cursor row back to a trip then rejects it for free, so the total cannot
    be folded or drilled into and no caller needs a special case for it.

    Its Cost/day divides by the total number of days *traveled*, summed per trip, not
    by the span from the first trip to the last -- the months at home between trips are
    not days anyone spent this money over. Trips without dates contribute their cost but
    no days, which is the honest treatment: leaving them out of the total entirely would
    make it disagree with the column above it.

    No trips, no total: a lone "TOTAL 0.00" reads as a result, where an empty table
    plainly says there is nothing here.
    """
    if not rows:
        return
    total = sum(row.total_minor for row in rows)
    buckets = tuple(
        sum(row.buckets[index] for row in rows) for index in range(len(trips_module.BUCKETS))
    )
    days = sum(row.days or 0 for row in rows)
    table.add_row(
        "",
        "",
        Text("TOTAL", style="bold"),
        _cost_cell(total),
        _per_day_cell(total, days),
        _bar_text(buckets, width),
    )


def _fmt_share(share: float) -> str:
    """A bucket's share of the trip, as a percentage.

    A real but tiny bucket -- a single paperback on a two-week trip -- rounds to
    ``0.0%``, which reads as a bug rather than as "small". ``<0.1%`` says what is
    actually true. Shared with ``cli/trips.py`` in spirit; both surfaces show a
    nonzero cost as a nonzero share.
    """
    if 0 < share < 0.001:
        return "<0.1%"
    return f"{share * 100:.1f}%"


def toggle_fold(
    row: int, panel_rows: List[TripPanelRow], foldable_ids: Set[int], expanded: Set[int]
) -> bool:
    """Flip the trip at ``row``'s membership in ``expanded``.

    Mutates ``expanded`` in place and returns whether it did -- false for a bucket
    sub-row or an out-of-range row, so the caller knows not to redraw or move the
    cursor for nothing.
    """
    if not 0 <= row < len(panel_rows):
        return False
    panel_row = panel_rows[row]
    if panel_row.bucket_index is not None:
        return False
    trip_id = panel_row.trip.id
    if trip_id not in foldable_ids:
        return False
    if trip_id in expanded:
        expanded.discard(trip_id)
    else:
        expanded.add(trip_id)
    return True


def toggle_fold_all(foldable_ids: Set[int], expanded: Set[int]) -> None:
    """``f``: unfold every trip if any is folded, else fold them all.

    "Any folded" rather than "all unfolded" so the key always visibly does something --
    a mix of folded and unfolded trips unfolds fully on the first press instead of
    silently folding the already-folded ones.
    """
    if foldable_ids - expanded:
        expanded |= foldable_ids
    else:
        expanded -= foldable_ids


def legend() -> Text:
    """Every trips.BUCKETS color, named -- colors mean nothing left unlabeled."""
    text = Text("  ")
    for index, bucket in enumerate(trips_module.BUCKETS):
        if index:
            text.append("   ")
        text.append("■ ", style=BUCKET_COLORS[index])
        text.append(bucket)
    return text


def trips_status(rows: List[TripRow]) -> str:
    """One line: how many trips, their combined cost, and the fold keys."""
    count = len(rows)
    total = sum(row.total_minor for row in rows)
    return (
        f"{count} trip{'s' if count != 1 else ''}   "
        f"total {_fmt_amount(total)}   "
        "space folds/unfolds a trip, f folds/unfolds them all   escape returns"
    )
