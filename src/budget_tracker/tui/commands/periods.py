"""The period picker shared by ``stats``, ``chart``, and ``pie``.

Parsing a window spec and opening the picker panel are not specific to any one of the
three -- :meth:`BudgetApp._open_period` is what sends a picked period back to whichever
one asked (``self._picker_target``).
"""

from __future__ import annotations

from typing import Optional

from rich.text import Text
from textual.widgets import DataTable, Input, Static

from budget_tracker import stats
from budget_tracker.tui import periods as periods_panel


class PeriodPickerCommands:
    """The period picker, and the custom-range prompt it can open."""

    def _parse_window(self, text: str) -> Optional[stats.Window]:
        try:
            return stats.parse(text)
        except ValueError as error:
            # The message names every accepted spelling, and may quote the user's text.
            self.notify(str(error), severity="error", markup=False)
            return None

    def _show_periods(self, target: str = "stats") -> None:
        self._picker_target = target
        self._fill_periods()
        self._range_pending = False
        self._prompt_panel = None
        self._set_panel("periods")

    def _open_period(self, window: stats.Window) -> None:
        """Send a period the picker just produced to whichever panel asked for it."""
        if self._picker_target == "chart":
            self._show_chart(window)
        elif self._picker_target == "pie":
            self._show_pie(window)
        else:
            self._show_stats(window)

    def _ask_range(self) -> None:
        """Ask for an explicit range, answered in the command bar below the picker."""
        self._range_pending = True
        self._prompt_panel = "periods"
        prompt = self.query_one("#prompt", Static)
        prompt.update(
            Text.assemble(
                ("Date range for the statistics\n", "bold"),
                (
                    f"Type it in the command bar below, as {periods_panel.RANGE_EXAMPLE}.  "
                    "Escape returns to the list.",
                    "dim",
                ),
            )
        )
        prompt.display = True
        self.query_one("#command", Input).focus()

    def _answer_range(self, text: str) -> None:
        window = self._parse_window(text)
        if window is None:
            return  # a bad range leaves the prompt up, over the picker
        self._open_period(window)

    def _cancel_range(self) -> None:
        # _show_periods() drops the pending question and hides the prompt with it.
        self._show_periods()

    def _fill_periods(self) -> None:
        periods_panel.fill_periods(self.query_one("#periods", DataTable))
