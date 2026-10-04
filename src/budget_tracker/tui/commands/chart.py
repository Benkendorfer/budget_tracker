"""``chart`` -- money per day/week/month as bars."""

from __future__ import annotations

from typing import Optional

from textual.widgets import DataTable

from budget_tracker import charts, queries, stats
from budget_tracker.tui import chart as chart_panel
from budget_tracker.tui.formatting import CHART_WIDTH

# The chart panel's own 'b' cycle, kept separate from queries.BUCKETS now that the
# latter also offers "year" — daily bars are only useful on the chart, over a window
# short enough to read, so adding "year" there must not change what 'b' cycles through.
_CHART_BUCKETS = ("day", "week", "month")


class ChartCommands:
    """``chart``, and the bars/measure it draws."""

    def _do_chart(self, arg: str) -> None:
        """``chart`` opens the period picker; ``chart <period> [bucket] [measure]`` skips it.

        Trailing ``day``/``week``/``month`` and ``net``/``spending``/``income`` words set
        the bucket and the measure, in either order, so ``chart 1y month spending`` and
        ``chart 1 year`` both read the way they look. Either word on its own re-draws the
        chart already on screen — the same thing ``b`` and ``m`` do, for anyone who would
        rather type it than remember a key.
        """
        arg = arg.strip()
        if not arg:
            self._show_periods("chart")
            return

        parts = arg.split()
        bucket = measure = None
        while parts:
            tail = parts[-1].lower()
            if bucket is None and tail in _CHART_BUCKETS:
                bucket = tail
            elif measure is None and tail in charts.MEASURE_ALIASES:
                measure = charts.MEASURE_ALIASES[tail]
            else:
                break
            parts = parts[:-1]
        text = " ".join(parts)

        if not text:
            if self.window is None:
                self.notify(
                    "No period yet: try 'chart 3m "
                    f"{bucket or measure}', or bare 'chart' to pick one.",
                    severity="warning",
                )
                return
            self._show_chart(self.window, bucket, measure)
            return

        window = self._parse_window(text)
        if window is not None:
            self._show_chart(window, bucket, measure)

    def _show_chart(
        self,
        window: stats.Window,
        bucket: Optional[str] = None,
        measure: Optional[str] = None,
    ) -> None:
        """Open the chart for ``window``, bucketed explicitly or by the window's length.

        A bucket the user asked for is remembered across re-scopes, but a *new* window
        re-derives its own: daily bars chosen for one month are unreadable stretched over
        two years, and silently keeping them would be worse than overriding a choice the
        user made about a range they have now left. The measure is not like that — it is
        a question about the money, not about the range — so it simply sticks.
        """
        rebucket = bucket is not None or self._bucket is None or window != self.window
        self.window = window
        if rebucket:
            self._bucket = bucket or charts.choose_bucket(window)
        if measure is not None:
            self._measure = measure
        self._range_pending = False
        self._prompt_panel = None
        self._build_chart()
        self._fill_chart()
        self._set_panel("chart")

    def _build_chart(self) -> None:
        """Fetch the series and scale it, under exactly the filters everything else uses.

        The same filters go to the transfer count as to the series, so the "N transfers
        excluded" the status line prints is the count actually missing from these bars.
        """
        with self.session_factory() as session:
            series = stats.spending_series(
                session,
                self.window,
                self._bucket,
                filters=self._active_filters().replace(date_range=None),
            )
            totals = queries.get_totals(
                session,
                filters=self._active_filters().replace(
                    date_range=(self.window.start, self.window.end)
                ),
            )
        self._chart = charts.build(series, measure=self._measure, width=CHART_WIDTH)
        self._chart_transfers = totals.transfer_count
        self._chart_unconverted = totals.unconverted_count

    def _fill_chart(self) -> None:
        """Redraw the table, columns included — two headers name the current measure."""
        table = self.query_one("#chart", DataTable)
        chart_panel.fill_chart(table, self._chart, self._measure, self._bucket)

    def _chart_status(self) -> str:
        """One line, under the same 92-column budget as every other panel's status."""
        return chart_panel.chart_status(
            self._chart,
            self.window,
            self._measure,
            self._bucket,
            self._chart_transfers,
            self.category_filter,
            self._categories,
            self.account_filter,
            self.vendor_filter,
            self.text_filter,
            self._chart_unconverted,
        )

    def _redraw_chart(self) -> None:
        self._build_chart()
        self._fill_chart()
        self._refresh_status()
