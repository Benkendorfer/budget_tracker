"""``pie`` -- each category's share of spending, as a stacked bar per bucket."""

from __future__ import annotations

from textual.widgets import Static

from budget_tracker import stats
from budget_tracker.tui import pie as pie_panel

# The pie panel's own 'b' cycle: no daily bucket (a year by day is 365 rows), but a
# yearly one, since a multi-year window benefits from it in a way the chart's shorter
# windows rarely do.
_SHARE_BUCKETS = ("week", "month", "year")


class PieCommands:
    """``pie``, and the stacked share chart it draws."""

    def _do_pie(self, arg: str) -> None:
        """``pie`` opens the period picker; ``pie <period>`` skips it."""
        if not arg:
            self._show_periods("pie")
            return
        window = self._parse_window(arg)
        if window is not None:
            self._show_pie(window)

    def _show_pie(self, window: stats.Window) -> None:
        self.window = window
        self._range_pending = False
        self._prompt_panel = None
        self._build_report()
        self._build_pie()
        self._fill_pie()
        self._set_panel("pie")

    def _build_pie(self) -> None:
        """Fetch this window's per-bucket category series and turn it, with the
        already-built report, into the stacked share chart. See pie_panel.build_stacked().
        """
        if self.window is None:
            self._pie = None
            return
        with self.session_factory() as session:
            buckets = stats.category_share_series(
                session,
                self.window,
                self._pie_bucket,
                filters=self._active_filters().replace(date_range=None),
            )
        self._pie = pie_panel.build_stacked(self._report, buckets)

    def _fill_pie(self) -> None:
        """Render the top bar, the per-bucket bars, and their shared legend."""
        pie_panel.fill_pie(self.query_one("#pie", Static), self._pie, self.window)

    def _pie_status(self) -> str:
        """One line, the same shape as _stats_status()/_chart_status()."""
        return pie_panel.pie_status(self._report, self._pie, self._pie_bucket)

    def _redraw_pie(self) -> None:
        """The report is unaffected by which bucket is charted, so only the per-bucket
        series and the stacked chart built from it need rebuilding."""
        self._build_pie()
        self._fill_pie()
        self._refresh_status()
