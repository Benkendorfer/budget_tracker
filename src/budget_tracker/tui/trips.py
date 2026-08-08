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
default -- there is no pre-folding behaviour here to stay byte-for-byte with, and one row
per bucket (``len(trips.BUCKETS)`` of them) on first open would bury the one line (dates,
cost, bar) most of what this panel is for. ``toggle_fold``/``toggle_fold_all`` below are
therefore a small, deliberate duplicate of ``stats.toggle_fold``/``toggle_fold_all``'s
shape, not a shared import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Set, Tuple

from rich.text import Text
from textual.widgets import DataTable

from .. import trips as trips_module
from ..charts import BLOCK
from ..queries import TripRow
from .formatting import FOLD_INDICATOR, OTHER_COLOR, PIE_COLORS, _amount_cell, _fmt_amount, _truncate

# One color per bucket, in trips.BUCKETS order: every PIE_COLORS entry, in order, for
# the real buckets, and the shared neutral gray for the trailing misc -- the same
# convention PIE_COLORS already documents (gray means "a catch-all standing in for
# several things"), which is exactly what misc is. Derived from len(trips.BUCKETS)
# rather than a literal count, so a bucket trips.py adds later still gets a color
# instead of silently wrapping onto one already in use -- PIE_COLORS itself would need
# to grow first if the real-bucket count ever exceeds it (see too_many_colors() in
# tui/pie.py for how that same situation is flagged there rather than left ambiguous).
# A bucket is the same color in every trip's bar and in the legend beneath the table.
BUCKET_COLORS: Tuple[str, ...] = PIE_COLORS[: len(trips_module.BUCKETS) - 1] + (OTHER_COLOR,)

# Column widths, fixed regardless of terminal size -- see bar_width() for the one
# column that is not. DATES_WIDTH covers the cross-year worst case
# ("2026-03-02..2027-01-05", 22 characters); TRIP_WIDTH matches the sidebar's own
# vendor-name magnitude, long names truncated with an ellipsis; COST_WIDTH fits a
# signed six-figure home-currency amount ("-999,999.99" is 12) with nothing to spare,
# same magnitude as the chart's own money columns.
DATES_WIDTH = 22
TRIP_WIDTH = 22
COST_WIDTH = 12
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
        - (DATES_WIDTH + COLUMN_PADDING)
        - (TRIP_WIDTH + COLUMN_PADDING)
        - (COST_WIDTH + COLUMN_PADDING)
        - COLUMN_PADDING  # Breakdown's own
    )
    for width in BAR_WIDTHS:
        if available >= width:
            return width
    return BAR_WIDTHS[-1]


def _format_dates(start, end) -> str:
    """``"YYYY-MM-DD..MM-DD"``, eliding only the end's year when it matches the
    start's -- the month stays, since a trip is usually inside one. A single-day trip
    (``start == end``) prints just the one date, and a trip with no transactions is
    blank (both ``None``). Matches ``budget trips``'s own formatting exactly, so a trip
    reads the same in both -- see the shared spec's correction: §4's own example,
    ``2026-03-02..03-14``, elides only the year even though its wording says "year and
    month"; the example is what both this and the CLI actually implement.
    """
    if start is None or end is None:
        return ""
    if start == end:
        return start.isoformat()
    tail = end.strftime("%m-%d") if end.year == start.year else end.isoformat()
    return f"{start.isoformat()}..{tail}"


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
    table.add_column("Dates", width=DATES_WIDTH)
    table.add_column("Trip", width=TRIP_WIDTH)
    table.add_column("Cost", width=COST_WIDTH)
    table.add_column("Breakdown", width=width)

    foldable_ids = {row.id for row in rows}
    panel_rows: List[TripPanelRow] = []
    for row in rows:
        panel_rows.append(TripPanelRow(trip=row))
        is_expanded = row.id in expanded
        label = row.name if is_expanded else f"{FOLD_INDICATOR} {row.name}"
        table.add_row(
            _format_dates(row.start, row.end),
            _truncate(label, TRIP_WIDTH),
            _amount_cell(row.total_minor),
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
                "",
                Text(f"  {bucket}", style=BUCKET_COLORS[index]),
                _amount_cell(cost),
                Text(_fmt_share(share), style=BUCKET_COLORS[index], justify="right"),
            )
    return panel_rows, foldable_ids


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
