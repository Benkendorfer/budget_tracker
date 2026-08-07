"""The statistics table: fold state, rows, the closing total row, and its status line.

``BudgetApp`` keeps the report itself (fetched over a session — see ``_build_report``)
and the collapsed-category set; everything here is pure given those.
"""

from __future__ import annotations

from typing import List, Optional, Set, Tuple

from rich.text import Text
from textual.widgets import DataTable

from .. import stats
from .formatting import (
    FOLD_INDICATOR,
    TRANSFER_MARK,
    UNCONVERTED_MARK,
    _amount_cell,
    _fmt_amount,
    _truncate,
)


def _foldable_category_ids(cats: List[stats.CategoryStat]) -> Set[int]:
    """category_ids that own at least one child in ``cats``.

    ``cats`` is depth-first (see stats.Report.categories), so a row has children exactly
    when the next row is deeper.
    """
    return {
        cats[i].category_id
        for i in range(len(cats) - 1)
        if cats[i + 1].depth > cats[i].depth
    }


def _visible_stats(
    cats: List[stats.CategoryStat], collapsed: Set[int], foldable: Set[int]
) -> List[stats.CategoryStat]:
    """``cats`` with every collapsed row's subtree hidden — recursively.

    A row is hidden while some ancestor still in ``collapsed`` is being skipped; the
    parent itself always stays visible. Both a nested collapse and the parent's own are
    remembered independently, keyed by category_id, so re-expanding a parent reveals
    whatever fold state its children already had.
    """
    visible = []
    hide_below_depth = None
    for stat in cats:
        if hide_below_depth is not None:
            if stat.depth > hide_below_depth:
                continue
            hide_below_depth = None
        visible.append(stat)
        if stat.category_id in foldable and stat.category_id in collapsed:
            hide_below_depth = stat.depth
    return visible


def _stats_label(stat: stats.CategoryStat, collapsed: Set[int]) -> str:
    indent = "  " * stat.depth
    if stat.category_id in collapsed:
        return f"{indent}{FOLD_INDICATOR} {stat.name}"
    return f"{indent}{stat.name}"


def _add_stats_total_row(table: DataTable, report: stats.Report) -> None:
    """A closing total, summing the depth-0 rows above it.

    Only the depth-0 rows: every row's money already includes its descendants, so
    adding the nested ones too would count a parent's spending twice (see
    stats.Report.categories). These figures come from the report's own totals rather
    than from re-adding the column, so the row cannot drift from the status line.

    Selecting it does nothing — _drill_into_category()'s bounds check already rejects
    a row index past the last category, which is exactly this one.

    A window with no transactions gets no total: a lone "TOTAL 0.00" reads as a
    result, where an empty table plainly says there is nothing here.
    """
    if not report.categories:
        return
    table.add_row(
        Text("TOTAL", style="bold"),
        Text(str(report.count), style="bold", justify="right"),
        Text(
            _fmt_amount(report.net_minor),
            style="bold " + ("red" if report.net_minor < 0 else "green"),
            justify="right",
        ),
        Text(
            _fmt_amount(stats.per_month(report.net_minor, report.window)),
            style="bold "
            + ("red" if report.net_minor < 0 else "green"),
            justify="right",
        ),
        # 100% by construction, and worth printing: it says the column above is a
        # share of this window's spending and nothing has been left out of it.
        Text("100.0%" if report.outflow_minor else "", style="bold", justify="right"),
        Text("", justify="right"),
    )


def fill_stats(
    table: DataTable, report: Optional[stats.Report], collapsed: Set[int]
) -> Tuple[List[stats.CategoryStat], Set[int]]:
    """Render the report, honouring folded subtrees.

    Folding never changes a number: every row already rolls up its descendants (see
    stats.CategoryStat), so hiding them here only removes rows, never edits one.
    Returns the rows actually rendered (parallel to the table, *excluding* the closing
    TOTAL row) and the foldable category_ids, so ``BudgetApp`` can keep a table row
    index mapped back to the right CategoryStat — see _drill_into_category(), which
    depends on that and would otherwise open the wrong category once rows can be
    hidden.
    """
    table.clear()
    if report is None:
        return [], set()
    cats = report.categories
    foldable_ids = _foldable_category_ids(cats)
    stats_rows = _visible_stats(cats, collapsed, foldable_ids)
    for stat in stats_rows:
        # Blank at depth 0: parent_share is identical to share there by construction
        # (see stats.CategoryStat.parent_share), so printing it twice is noise.
        parent_pct = "" if stat.depth == 0 else f"{abs(stat.parent_share) * 100:.1f}%"
        table.add_row(
            _truncate(_stats_label(stat, collapsed), 26),
            Text(str(stat.count), justify="right"),
            _amount_cell(stat.total_minor),
            _amount_cell(stat.avg_month_minor),
            # Shares are a fraction of a negative outflow, so a category that spent
            # nothing comes out as -0.0; abs() keeps that off the screen.
            Text(f"{abs(stat.share) * 100:.1f}%", justify="right"),
            Text(parent_pct, justify="right"),
        )
    _add_stats_total_row(table, report)
    return stats_rows, foldable_ids


def toggle_fold(row: int, stats_rows: List[stats.CategoryStat], foldable_ids: Set[int], collapsed: Set[int]) -> bool:
    """Flip ``category_id``'s membership in ``collapsed`` for the row at ``row``.

    Mutates ``collapsed`` in place and returns whether it did — false for a leaf row,
    the TOTAL row, or an out-of-range row, so the caller knows not to redraw or move
    the cursor for nothing.
    """
    if not 0 <= row < len(stats_rows):
        return False
    stat = stats_rows[row]
    if stat.category_id not in foldable_ids:
        return False
    if stat.category_id in collapsed:
        collapsed.discard(stat.category_id)
    else:
        collapsed.add(stat.category_id)
    return True


def toggle_fold_all(foldable_ids: Set[int], collapsed: Set[int]) -> None:
    """Fold every group if any is expanded, else unfold them all.

    "Any expanded" rather than "all collapsed" so the key always visibly does
    something — a mix of folded and unfolded groups collapses fully on the first
    press instead of silently unfolding the already-collapsed ones.
    """
    if foldable_ids - collapsed:
        collapsed |= foldable_ids
    else:
        collapsed -= foldable_ids


def stats_status(report: stats.Report) -> str:
    """One line: the main panel gives the status 92 columns, so every field is terse.

    No "escape returns" hint here, unlike the other panels — the footer already spells
    that key out, and against a real year of five-figure totals the sentence ran the
    line to exactly the panel width, one digit away from truncating the money. The
    right-arrow drill-down hint lives in the footer for the same reason: this line has
    no slack left to spend on it (see test_stats_status_line_fits_the_main_panel).
    """
    window = report.window
    # A custom window's label is its own date range, which follows anyway.
    label = "custom" if window.key == "custom" else window.label
    # The money figures leave transfers out, so say how many vanished — silently
    # dropping a payment between your own accounts reads as missing spending.
    excluded = f"{TRANSFER_MARK} {report.transfer_count} " if report.transfer_count else ""
    # Same idea, a different reason: these rows are missing because no exchange rate
    # was on file for their day, not because they were excluded on purpose. This line
    # has no slack left to spend on the word "unconverted" (see the module docstring
    # note above and test_stats_status_line_fits_the_main_panel), so it is as terse as
    # the transfer marker it sits beside -- and, like that marker, stacking both at once
    # is not budgeted for.
    unconverted = (
        f"{UNCONVERTED_MARK} {report.unconverted_count} " if report.unconverted_count else ""
    )
    return (
        f"{label} {window.start}→{window.end} "
        f"{report.count} txns "
        f"{excluded}"
        f"{unconverted}"
        f"out {_fmt_amount(report.outflow_minor)} "
        f"in {_fmt_amount(report.inflow_minor)} "
        f"/mo {_fmt_amount(report.avg_month_outflow_minor)}"
    )
