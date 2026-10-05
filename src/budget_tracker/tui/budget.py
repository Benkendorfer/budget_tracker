"""The two budget panels: the monthly tracking table and the editable plan table.

``BudgetApp`` keeps the views themselves (``budget.TrackView``/``budget.PlanView``,
fetched over a session -- see ``commands/budget.py``'s ``_build_budget_track``/
``_build_budget_plan``); everything here is pure given those, the same split
``tui/stats.py`` and ``tui/trips.py`` already follow.

Both tables reuse one column shape per row kind (Category / Budget-or-target / actual
/ a derived figure), so the income row and an ordinary category row render through the
same cells -- see ``fill_track``/``fill_plan``. Neither function computes a figure that
is not already on the view it is given; a budget figure and the statistics panel's own
figure for the same category and month agree by construction (see :mod:`budget`'s own
module docstring), and nothing here is allowed to quietly re-derive one.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from typing import AbstractSet, List, Optional, Set, Tuple

from rich.text import Text
from textual.widgets import DataTable

from .. import budget as budget_core
from .. import charts
from . import stats as stats_panel
from .formatting import FOLD_INDICATOR, _fmt_amount, _truncate

# ------------------------------------------------------------------- shared constants

CATEGORY_WIDTH = 26  # same magnitude as the statistics table's own Category column
AMOUNT_WIDTH = 12  # fits a signed six-figure amount ("-999,999.99" is 11) with room
COLUMN_PADDING = 2  # DataTable pads every column one cell each side
BORDER_OVERHEAD = 2  # the panel's own round border, one column either side

# A bar's percentage figure, right-justified in this many cells including the "%" --
# "999%" is 4, so 6 leaves a cell of headroom without eating into the bar for any
# budget this app is meant to track (a category running 10x over its budget is already
# a conversation, not a rendering problem).
PCT_WIDTH = 6

# The Used column's width, widest first: 30 cells of bar when the tracking panel has
# room, else 16 -- picked the same way trips.bar_width() picks between its own two
# candidates, see track_used_width().
USED_WIDTHS: Tuple[int, ...] = (30, 16)


def _amount_or_dash(minor: Optional[int]) -> str:
    """"-" for a figure that has no value at all -- an unset plan amount, an income
    target that was never set -- as distinct from a real, zero amount."""
    return "–" if minor is None else _fmt_amount(minor)


def _progress_text(ratio: Optional[float], width: int, style: str) -> Text:
    """A bar plus its percentage, or a dash when ``ratio`` means "no target to pace
    against" (``None``) -- never a bar drawn against a budget of zero or less, which
    would make "100% used" and "unbounded" look the same.
    """
    if ratio is None:
        return Text(_amount_or_dash(None).rjust(width), style=style)
    bar_width = max(width - PCT_WIDTH - 1, 1)
    bar = charts.bar_text(min(max(ratio, 0.0), 1.0), bar_width).ljust(bar_width)
    pct = f"{ratio * 100:.0f}%".rjust(PCT_WIDTH - 1)
    return Text(_truncate(f"{bar} {pct}", width), style=style)


def _month_label(month: date) -> str:
    return month.strftime("%b %Y")


# ------------------------------------------------------------------------ track panel

TRACK_BUDGET_WIDTH = AMOUNT_WIDTH
TRACK_SPENT_WIDTH = AMOUNT_WIDTH
TRACK_LEFT_WIDTH = AMOUNT_WIDTH


def track_used_width(main_panel_width: int) -> int:
    """How wide the Used column (bar + %) can be in a panel this wide -- see
    trips.bar_width()'s own docstring for the same reasoning: read from the mounted
    widget rather than guessed, since a column width that "looks fine" has shipped
    off-screen before.
    """
    available = (
        main_panel_width
        - BORDER_OVERHEAD
        - (CATEGORY_WIDTH + COLUMN_PADDING)
        - (TRACK_BUDGET_WIDTH + COLUMN_PADDING)
        - (TRACK_SPENT_WIDTH + COLUMN_PADDING)
        - (TRACK_LEFT_WIDTH + COLUMN_PADDING)
        - COLUMN_PADDING  # Used's own
    )
    for width in USED_WIDTHS:
        if available >= width:
            return width
    return USED_WIDTHS[-1]


@dataclass(frozen=True)
class TrackPanelRow:
    """One rendered row of the tracking table: the income row (``row`` is ``None``) or
    a budgeted category. Parallel to the table, the same discipline ``stats._stats_rows``
    already follows, though the tracking table has nothing to map a cursor back to yet
    -- this exists so a future row action has somewhere to start from.
    """

    kind: str  # "income" or "expense"
    row: Optional[budget_core.TrackRow]


def _row_style(row: budget_core.TrackRow, error_color: str, warning_color: str) -> str:
    """Over-budget wins over ahead-of-pace -- a row cannot be both states' colors at
    once, and over is the more urgent of the two."""
    if row.over:
        return error_color
    if row.ahead_of_pace:
        return warning_color
    return ""


def fill_track(
    table: DataTable,
    view: budget_core.TrackView,
    error_color: str,
    warning_color: str,
    used_width: int,
) -> List[TrackPanelRow]:
    """Render the tracking table: the income row first, then every budgeted category
    in ``view.rows`` (already depth-first, see budget.track()).

    ``error_color``/``warning_color`` are the running theme's resolved ``$error``/
    ``$warning`` hex values (``App.get_css_variables()``) -- resolved by the caller, not
    here, so this stays a plain function of its data and reads correctly in light and
    dark themes alike.
    """
    table.clear(columns=True)
    table.add_column("Category", width=CATEGORY_WIDTH)
    table.add_column("Budget", width=TRACK_BUDGET_WIDTH)
    table.add_column("Spent", width=TRACK_SPENT_WIDTH)
    table.add_column("Left", width=TRACK_LEFT_WIDTH)
    table.add_column("Used", width=used_width)

    panel_rows: List[TrackPanelRow] = [TrackPanelRow(kind="income", row=None)]
    income_left = (
        view.income_target_minor - view.income_actual_minor
        if view.income_target_minor is not None
        else None
    )
    income_ratio = (
        view.income_actual_minor / view.income_target_minor
        if view.income_target_minor
        else None
    )
    table.add_row(
        Text("Income", style="bold"),
        Text(_amount_or_dash(view.income_target_minor), style="bold", justify="right"),
        Text(_fmt_amount(view.income_actual_minor), style="bold", justify="right"),
        Text(_amount_or_dash(income_left), style="bold", justify="right"),
        _progress_text(income_ratio, used_width, "bold"),
    )

    for row in view.rows:
        panel_rows.append(TrackPanelRow(kind="expense", row=row))
        style = _row_style(row, error_color, warning_color)
        label = "  " * row.depth + row.name
        ratio = row.used if row.budget_minor > 0 else None
        table.add_row(
            Text(_truncate(label, CATEGORY_WIDTH), style=style),
            # A derived budget (the sum of its subcategories') is yellow in its own cell;
            # the whole row going yellow already means "ahead of pace".
            Text(
                _fmt_amount(row.budget_minor),
                style=f"{style} {warning_color}".strip() if row.budget_derived else style,
                justify="right",
            ),
            Text(_fmt_amount(row.spent_minor), style=style, justify="right"),
            Text(_fmt_amount(row.left_minor), style=style, justify="right"),
            _progress_text(ratio, used_width, style),
        )
    return panel_rows


def track_status(view: budget_core.TrackView, source_month: Optional[date]) -> str:
    """One line: the month, how far through it we are, how many categories are over,
    what is left across all of them, and what spending none of them cover.

    ``source_month`` is ``budget.effective_month()`` for ``view.month`` -- ``None`` if
    nothing has ever been budgeted at or before it, a different month if this one's
    plan was copied forward (see budget.py's module docstring), or ``view.month`` itself
    when it owns its own rows outright, in which case neither note is shown.
    """
    parts = [_month_label(view.month)]
    if view.is_current:
        days_in_month = calendar.monthrange(view.month.year, view.month.month)[1]
        day = max(1, round(view.elapsed_fraction * days_in_month))
        pct = round(view.elapsed_fraction * 100)
        parts.append(f"day {day} of {days_in_month} ({pct}%)")
    if source_month is None:
        parts.append("no plan set")
    elif source_month != view.month:
        parts.append(f"plan from {_month_label(source_month)}")
    over_count = sum(1 for row in view.rows if row.over)
    parts.append(f"{over_count} over")
    left = view.total_budget_minor - view.total_spent_minor
    parts.append(f"{_fmt_amount(left)} left")
    parts.append(f"not budgeted {_fmt_amount(view.not_budgeted_minor)}")
    return " · ".join(parts)


# ------------------------------------------------------------------------- plan panel

PLAN_AVG_WIDTH = AMOUNT_WIDTH
PLAN_LAST_WIDTH = AMOUNT_WIDTH
# Wider than the other amounts: an over-committed parent shows "1,000.00 < 1,300.00"
# (its own budget against its subcategories' total), two six-figure amounts and " < ".
PLAN_BUDGET_WIDTH = 2 * AMOUNT_WIDTH + 3


@dataclass(frozen=True)
class PlanPanelRow:
    """One editable row of the plan table: the income row (``row`` is ``None``) or a
    category row. Parallel to the table *excluding* the trailing "Total budgeted"/
    "Unallocated" rows (see fill_plan) -- those are never edited, the same way
    stats._add_stats_total_row's row is left out of stats_rows.
    """

    kind: str  # "income" or "expense"
    row: Optional[budget_core.PlanRow]


def plan_foldable_ids(view: budget_core.PlanView) -> Set[int]:
    """Category rows with something beneath them -- the ones ``z`` can fold. Plan rows
    carry the same ``category_id``/``depth`` shape as statistics rows, so the
    statistics panel's own rule (and its hiding logic below) applies unchanged."""
    return stats_panel._foldable_category_ids(view.rows)


def fill_plan(
    table: DataTable,
    view: budget_core.PlanView,
    error_color: str = "red",
    collapsed: AbstractSet[int] = frozenset(),
    warning_color: str = "yellow",
) -> List[PlanPanelRow]:
    """Render the plan table: the income row, then every row of ``view.rows`` (already
    depth-first, ancestors included for indentation -- see budget.plan_rows()), then
    the two closing totals.

    A category with no budget shows its Budget cell dimmed, the same convention
    trips.py uses for a derived (rather than set-by-hand) date -- it marks the value as
    *absent*, not wrong.

    A category whose subcategories were budgeted more than it was itself
    (``PlanRow.overcommitted``) is shown in ``error_color``, with the subcategories'
    total beside its own budget, since that one *is* wrong: the parent cannot hold them.
    """
    table.clear(columns=True)
    table.add_column("Category", width=CATEGORY_WIDTH)
    table.add_column(f"Avg/mo ({view.averaging_months}m)", width=PLAN_AVG_WIDTH)
    table.add_column("Last month", width=PLAN_LAST_WIDTH)
    table.add_column("Budget", width=PLAN_BUDGET_WIDTH)

    panel_rows: List[PlanPanelRow] = [PlanPanelRow(kind="income", row=None)]
    table.add_row(
        Text("Income", style="bold"),
        Text(_fmt_amount(view.income_avg_minor), style="bold", justify="right"),
        Text(_fmt_amount(view.income_last_month_minor), style="bold", justify="right"),
        Text(_amount_or_dash(view.income_target_minor), style="bold", justify="right"),
    )

    visible = stats_panel._visible_stats(view.rows, set(collapsed), plan_foldable_ids(view))
    for row in visible:
        panel_rows.append(PlanPanelRow(kind="expense", row=row))
        fold = f"{FOLD_INDICATOR} " if row.category_id in collapsed else ""
        label = "  " * row.depth + fold + row.name
        budget_style = "" if row.budget_minor is not None else "dim"
        if row.budget_derived:
            # No budget of its own: the sum of its subcategories', computed, in yellow.
            budget_style = warning_color
        row_style = error_color if row.overcommitted else ""
        budget_text = _amount_or_dash(row.budget_minor)
        if row.overcommitted:
            # e.g. "1,000.00 < 1,300.00": its own budget against what its subcategories
            # were given. Fits the column: the plan's Budget column is the last one.
            budget_text = f"{budget_text} < {_fmt_amount(row.subcategory_budget_minor)}"
            budget_style = f"bold {error_color}"
        table.add_row(
            Text(_truncate(label, CATEGORY_WIDTH), style=row_style),
            Text(_fmt_amount(row.avg_minor), style=row_style, justify="right"),
            Text(_fmt_amount(row.last_month_minor), style=row_style, justify="right"),
            Text(budget_text, style=budget_style, justify="right"),
        )

    table.add_row(
        Text("Total budgeted", style="bold"),
        Text(""),
        Text(""),
        Text(_fmt_amount(view.total_budget_minor), style="bold", justify="right"),
    )
    table.add_row(
        Text("Unallocated", style="bold"),
        Text(""),
        Text(""),
        Text(_amount_or_dash(view.unallocated_minor), style="bold", justify="right"),
    )
    return panel_rows


@dataclass(frozen=True)
class BudgetEditTarget:
    """What ``enter`` on a plan row is about to change -- set by
    ``commands/budget.py``'s ``_edit_budget_row`` and read back by
    ``_answer_budget_edit`` once the amount is typed, the same shape as
    ``tui/imports.py``'s ``_Setup``.
    """

    kind: str  # budget_core.INCOME or budget_core.EXPENSE
    category: Optional[str]  # name budget.set_amount resolves; None for income
    name: str  # for prompts/notifications
    row: int  # table row to keep the cursor on afterwards
    month: date


def plan_status(view: budget_core.PlanView, source_month: Optional[date]) -> str:
    """One line: the planned month, the averaging window, whether its plan was copied
    forward, and what is left unallocated against the income target."""
    note = ""
    if source_month is None:
        note = "   no plan set"
    elif source_month != view.month:
        note = f"   plan from {_month_label(source_month)}"
    if view.income_target_minor is not None:
        allocation = f"unallocated {_fmt_amount(view.unallocated_minor)}"
    else:
        allocation = "no income target"
    return (
        f"{_month_label(view.month)}   avg over last {view.averaging_months} months"
        f"{note}   {allocation}   "
        "enter edits a row   n cycles 3/6/12 months   escape returns"
    )
