"""``budget`` (the tracking panel) and ``budget plan`` (the editable plan panel).

Both read :mod:`budget_tracker.budget` for every figure -- nothing here computes a
spend, an average, or a total; see that module's own docstring for why. The plan
panel's editing flow (``enter`` on a row, an amount typed into the command bar) follows
the same ``_pending_*``/``_answer_*`` shape every other in-app question in this app
does -- see ``tui/imports.py``'s ``_Setup`` or ``commands/categories.py``'s
``_pending_category``.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import List, Optional

from rich.text import Text
from textual.widgets import DataTable, Static

from .. import budget as budget_panel
from ... import budget as budget_core
from ... import queries
from ..formatting import _fmt_amount

_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

# Cycled by the 'n' key (see check_action()'s "cycle_budget_months" gate and
# action_cycle_budget_months() below) -- the three windows the architect's spec names,
# in the order a shorter plan graduates to a longer one.
_PLAN_MONTHS = (3, 6, 12)

DEFAULT_PLAN_MONTHS = 6


class BudgetCommands:
    """``budget``, ``budget plan``, and the plan panel's in-place editing."""

    BUDGET_USAGE = "Usage: budget [YYYY-MM]   |   budget plan [YYYY-MM] [months]"

    # ------------------------------------------------------------------- parsing

    def _parse_budget_month(self, text: str) -> Optional[date]:
        text = text.strip()
        if not _MONTH_RE.match(text):
            return None
        year, month = text.split("-")
        try:
            return date(int(year), int(month), 1)
        except ValueError:
            return None

    def _parse_budget_amount(self, text: str) -> int:
        """A typed amount to minor units -- home currency, always two decimal places
        (every budget figure is, see budget.py's own module docstring)."""
        cleaned = text.replace(",", "").replace("$", "").strip()
        try:
            value = Decimal(cleaned)
        except InvalidOperation:
            raise ValueError(f"Not a number: {text!r}")
        if value < 0:
            raise ValueError("A budget amount can't be negative.")
        return int((value * 100).to_integral_value(rounding=ROUND_HALF_UP))

    # --------------------------------------------------------------- dispatch

    def _do_budget(self, arg: str) -> None:
        """``budget [YYYY-MM]`` opens the tracking panel; ``budget plan ...`` opens
        the editable plan panel instead."""
        arg = arg.strip()
        if arg.lower() == "plan" or arg.lower().startswith("plan "):
            self._do_budget_plan(arg[4:].strip())
            return
        parts = arg.split()
        if len(parts) > 1:
            self.notify(self.BUDGET_USAGE, severity="warning")
            return
        if parts:
            month = self._parse_budget_month(parts[0])
            if month is None:
                self.notify(
                    f"Bad month {parts[0]!r}; use YYYY-MM.", severity="warning", markup=False
                )
                return
        else:
            month = budget_core.month_start(date.today())
        self._show_budget_track(month)

    def _do_budget_plan(self, arg: str) -> None:
        parts = arg.split()
        if len(parts) > 2:
            self.notify(self.BUDGET_USAGE, severity="warning")
            return
        month = budget_core.month_start(date.today())
        months = DEFAULT_PLAN_MONTHS
        if len(parts) >= 1:
            parsed = self._parse_budget_month(parts[0])
            if parsed is None:
                self.notify(
                    f"Bad month {parts[0]!r}; use YYYY-MM.", severity="warning", markup=False
                )
                return
            month = parsed
        if len(parts) == 2:
            if not parts[1].isdigit() or int(parts[1]) <= 0:
                self.notify(self.BUDGET_USAGE, severity="warning")
                return
            months = int(parts[1])
        self._show_budget_plan(month, months)

    # ---------------------------------------------------------------- tracking

    def _show_budget_track(self, month: date) -> None:
        self._budget_month = month
        self._build_budget_track()
        self._fill_budget_track()
        self._set_panel("budget_track")

    def _build_budget_track(self) -> None:
        """Fetch the tracking view and which stored month it actually came from, for
        the "plan from ..." status note -- see budget_panel.track_status()."""
        with self.session_factory() as session:
            self._budget_track_view = budget_core.track(session, self._budget_month)
            self._budget_track_source_month = budget_core.effective_month(
                session, self._budget_month
            )

    def _fill_budget_track(self) -> None:
        table = self.query_one("#budget_track", DataTable)
        width = budget_panel.track_used_width(table.size.width)
        colors = self.get_css_variables()
        self._budget_track_rows = budget_panel.fill_track(
            table, self._budget_track_view, colors["error"], colors["warning"], width
        )

    def _budget_track_status(self) -> str:
        return budget_panel.track_status(self._budget_track_view, self._budget_track_source_month)

    # -------------------------------------------------------------------- plan

    def _show_budget_plan(self, month: date, months: int) -> None:
        self._budget_plan_month = month
        self._budget_plan_months = months
        self._build_budget_plan()
        self._fill_budget_plan()
        self._set_panel("budget_plan")

    def _build_budget_plan(self) -> None:
        with self.session_factory() as session:
            self._budget_plan_view = budget_core.plan_rows(
                session, self._budget_plan_month, self._budget_plan_months
            )
            self._budget_plan_source_month = budget_core.effective_month(
                session, self._budget_plan_month
            )

    def _fill_budget_plan(self) -> None:
        table = self.query_one("#budget_plan", DataTable)
        self._budget_plan_rows = budget_panel.fill_plan(
            table,
            self._budget_plan_view,
            self.get_css_variables()["error"],
            self._budget_plan_collapsed,
            self.get_css_variables()["warning"],
        )

    def _toggle_budget_fold(self, row: int) -> None:
        """``z`` (or space) on a plan row: fold/unfold the categories beneath it. The
        income row and leaf rows do nothing, as a statistics leaf does."""
        if not 0 <= row < len(self._budget_plan_rows):
            return
        plan_row = self._budget_plan_rows[row].row
        if plan_row is None or self._budget_plan_view is None:
            return
        if plan_row.category_id not in budget_panel.plan_foldable_ids(self._budget_plan_view):
            return
        self._budget_plan_collapsed ^= {plan_row.category_id}
        self._fill_budget_plan()
        # The folded row's own subtree is what changes, always right after it, so its
        # index is unchanged and the cursor can stay put -- as in statistics.
        self.query_one("#budget_plan", DataTable).move_cursor(row=row)

    def _toggle_budget_fold_all(self) -> None:
        """``f``: fold every group if any is open, else open them all."""
        if self._budget_plan_view is None:
            return
        from budget_tracker.tui import stats as stats_panel

        foldable = budget_panel.plan_foldable_ids(self._budget_plan_view)
        if not foldable:
            return
        table = self.query_one("#budget_plan", DataTable)
        row = table.cursor_row
        stats_panel.toggle_fold_all(foldable, self._budget_plan_collapsed)
        self._fill_budget_plan()
        if table.row_count:
            table.move_cursor(row=min(row, table.row_count - 1))

    def _budget_plan_status(self) -> str:
        return budget_panel.plan_status(self._budget_plan_view, self._budget_plan_source_month)

    def action_cycle_budget_months(self) -> None:
        """``n`` on the plan panel: step the averaging window 3 -> 6 -> 12 -> 3.

        See check_action()'s "cycle_budget_months" gate for why this is inert
        everywhere else -- a plain letter binding, same shape as 'b'/'m' on the chart.
        """
        if self._panel != "budget_plan":
            return
        table = self.query_one("#budget_plan", DataTable)
        row = table.cursor_row
        current = self._budget_plan_months
        next_months = (
            _PLAN_MONTHS[(_PLAN_MONTHS.index(current) + 1) % len(_PLAN_MONTHS)]
            if current in _PLAN_MONTHS
            else _PLAN_MONTHS[0]
        )
        self._show_budget_plan(self._budget_plan_month, next_months)
        if 0 <= row < table.row_count:
            table.move_cursor(row=row)

    # ----------------------------------------------------------------- editing

    def _edit_budget_row(self, row: int) -> None:
        """``enter`` on a plan row: ask for the amount in the command bar, prefilled
        with the current one (blank to clear) -- see ``_answer_budget_edit``."""
        if not 0 <= row < len(self._budget_plan_rows):
            return
        panel_row = self._budget_plan_rows[row]
        month = self._budget_plan_month
        if panel_row.kind == "income":
            name = "Income target"
            category: Optional[str] = None
            kind = budget_core.INCOME
            current = self._budget_plan_view.income_target_minor
        else:
            plan_row = panel_row.row
            if plan_row.category_id == queries.UNCATEGORIZED_ID:
                self.notify("Uncategorized spending can't be budgeted.", severity="warning")
                return
            name = plan_row.name
            category = name
            kind = budget_core.EXPENSE
            current = plan_row.budget_minor

        self._pending_budget_edit = budget_panel.BudgetEditTarget(
            kind=kind, category=category, name=name, row=row, month=month
        )
        self._prompt_panel = "budget_plan"
        month_label = month.strftime("%b %Y")
        prompt = self.query_one("#prompt", Static)
        # Text.assemble(), not markup: a category name is user data and may hold
        # brackets -- the same reason every other prompt in this app does this.
        prompt.update(
            Text.assemble(
                (f"Budget for {name} in {month_label}:\n", "bold"),
                (
                    "Type an amount in the command bar below; blank clears. "
                    "Escape cancels.",
                    "dim",
                ),
            )
        )
        prompt.display = True
        prefill = "" if current is None else f"{Decimal(current).scaleb(-2):.2f}"
        self._prefill_command(prefill)

    def _answer_budget_edit(self, text: str) -> None:
        try:
            self._save_budget_edit(text)
        finally:
            # Back to the table whatever happened -- saved, cleared, or a mistyped amount
            # refused -- so the next row is one arrow and one enter away.
            self._refocus_budget_plan()

    def _refocus_budget_plan(self) -> None:
        if self._panel == "budget_plan":
            self.query_one("#budget_plan", DataTable).focus()

    def _save_budget_edit(self, text: str) -> None:
        target = self._pending_budget_edit
        self._cancel_budget_edit()
        text = text.strip()
        if text == "":
            amount_minor: Optional[int] = None
        else:
            try:
                amount_minor = self._parse_budget_amount(text)
            except ValueError as error:
                self.notify(str(error), severity="warning", markup=False)
                return
        with self.session_factory() as session:
            try:
                budget_core.set_amount(
                    session, target.month, target.category, amount_minor, kind=target.kind
                )
            except ValueError as error:
                self.notify(str(error), severity="error", markup=False)
                return
            session.commit()
        if self._panel == "budget_plan":
            self._build_budget_plan()
            self._fill_budget_plan()
            table = self.query_one("#budget_plan", DataTable)
            if 0 <= target.row < table.row_count:
                table.move_cursor(row=target.row)
            self._refresh_status()
        if amount_minor is None:
            self.notify(f"{target.name}: budget cleared.", markup=False)
        else:
            self.notify(f"{target.name}: {_fmt_amount(amount_minor)}.", markup=False)

    def _cancel_budget_edit(self) -> None:
        self._pending_budget_edit = None
        self._prompt_panel = None
        self.query_one("#prompt", Static).display = False
