"""Drilling from a statistics row, a chart bar, or a trips row into its transactions,
and the left arrow that undoes exactly that.
"""

from __future__ import annotations

from textual.widgets import DataTable

from budget_tracker import charts, trips
from budget_tracker.tui import trips as trips_panel


class DrillDownCommands:
    """Right/left arrow (and enter) between a breakdown panel and its transactions."""

    @property
    def _drilled_from_stats(self) -> bool:
        """True only right after a statistics drill-down — see ``_drill_origin``."""
        return self._drill_origin == "stats"

    @property
    def _drilled_from_chart(self) -> bool:
        """True only right after a chart drill-down — see ``_drill_origin``."""
        return self._drill_origin == "chart"

    @property
    def _drilled_from_trips(self) -> bool:
        """True only right after a trips-panel drill-down — see ``_drill_origin``."""
        return self._drill_origin == "trips"

    def _drill_into_category(self, row: int) -> None:
        """Enter, or the right arrow, on a statistics row lists the transactions behind it.

        The report's window comes along as a date filter. Without it the table would show
        every transaction that category ever had, and the figures the user just clicked
        would not match the rows they are now looking at.
        """
        if self._report is None or not 0 <= row < len(self._stats_rows):
            return
        stat = self._stats_rows[row]
        # Remember what the drill-down is about to overwrite, and where it came from, so
        # a left arrow can undo exactly this rather than blanking filters the user set
        # themselves, and can put the cursor back where it was. trip_filter and
        # category_ids_filter are untouched by this drill-down, but are still snapshot
        # here so _go_back_from_drill can restore all four the same way regardless of
        # which drill-down produced this view.
        self._pre_drill_category_filter = self.category_filter
        self._pre_drill_date_filter = self.date_filter
        self._pre_drill_trip_filter = self.trip_filter
        self._pre_drill_category_ids_filter = self.category_ids_filter
        self._drill_source_row = row
        self.category_filter = stat.category_id
        self.date_filter = (self._report.window.start, self._report.window.end)
        # Panel first: reload() only rebuilds the report while the stats panel is up, and
        # rebuilding it under the new filter would rewrite the rows we just read.
        self._set_panel("txns")
        self._set_drilled_from("stats")
        self.reload()

    def _drill_into_bar(self, row: int) -> None:
        """Enter, or the right arrow, on a chart row lists the transactions behind that bar.

        The bar's own bucket becomes the date filter — clamped to the window's edges via
        charts.bucket_date_range(), since the first and last buckets are usually partial
        — intersected with whatever account/category/vendor/text filters already scope
        the chart. Without the clamp, a bucket at either edge of the window would pull in
        transactions the chart never drew, and the drilled-down rows would not sum back
        to the bar just clicked.

        Unlike a statistics drill-down this never touches the category filter — a bar is
        a slice of time, not of category — so _pre_drill_category_filter just records the
        filter already in place, and going back "restores" it as a no-op.
        """
        if self._chart is None or self.window is None or self._bucket is None:
            return
        if not 0 <= row < len(self._chart.bars):
            return
        bar = self._chart.bars[row]
        self._pre_drill_category_filter = self.category_filter
        self._pre_drill_date_filter = self.date_filter
        self._pre_drill_trip_filter = self.trip_filter
        self._pre_drill_category_ids_filter = self.category_ids_filter
        self._drill_source_row = row
        self.date_filter = charts.bucket_date_range(bar.key, self._bucket, self.window)
        # Panel first: reload() only rebuilds the chart while the chart panel is up, and
        # rebuilding it under the new date filter would rewrite the bars we just read.
        self._set_panel("txns")
        self._set_drilled_from("chart")
        self.reload()

    def _drill_into_trip_row(self, row: int) -> None:
        """Enter, or the right arrow, on a trip row lists that trip's transactions; on
        one of its unfolded bucket rows, that trip's transactions in that bucket alone.

        A bucket is several unrelated categories at once (trips.resolve_buckets), which
        is exactly what queries.Filters.category_ids is for -- not a subtree, an
        explicit set. misc's set always includes None, or every uncategorized
        transaction on the trip would silently vanish from its own drill-down (see
        tui.trips.bucket_category_ids).
        """
        if not 0 <= row < len(self._trip_rows):
            return
        panel_row = self._trip_rows[row]
        # Same snapshot-everything discipline as _drill_into_category()/
        # _drill_into_bar(): category_filter/date_filter are untouched by this
        # drill-down, but are still recorded so _go_back_from_drill can restore all
        # four uniformly.
        self._pre_drill_category_filter = self.category_filter
        self._pre_drill_date_filter = self.date_filter
        self._pre_drill_trip_filter = self.trip_filter
        self._pre_drill_category_ids_filter = self.category_ids_filter
        self._drill_source_row = row
        self.trip_filter = panel_row.trip.id
        if panel_row.bucket_index is None:
            self.category_ids_filter = None
        else:
            bucket = trips.BUCKETS[panel_row.bucket_index]
            with self.session_factory() as session:
                mapping = trips.resolve_buckets(session)
            self.category_ids_filter = trips_panel.bucket_category_ids(mapping, bucket)
        # Panel first: reload() only rebuilds the trips panel while it is up, and
        # rebuilding it under the new filters would rewrite the rows we just read.
        self._set_panel("txns")
        self._set_drilled_from("trips")
        self.reload()

    def _go_back_from_drill(self) -> None:
        """Left arrow, undoing exactly the drill-down that produced this view.

        Mirrors _drill_into_category()/_drill_into_bar()/_drill_into_trip_row():
        restores the four filters any of them might have overwritten (each may be
        None, or may be a filter the user had set before drilling in -- see their own
        snapshot comments), rebuilds whichever panel the drill-down came from, and
        returns its cursor to the row that was drilled from.
        """
        origin = self._drill_origin
        row = self._drill_source_row
        self.category_filter = self._pre_drill_category_filter
        self.date_filter = self._pre_drill_date_filter
        self.trip_filter = self._pre_drill_trip_filter
        self.category_ids_filter = self._pre_drill_category_ids_filter
        self._pre_drill_category_filter = None
        self._pre_drill_date_filter = None
        self._pre_drill_trip_filter = None
        self._pre_drill_category_ids_filter = None
        self._drill_source_row = None
        self._set_drilled_from(None)
        # reload() while the panel is still "txns" resyncs the transactions/totals to the
        # restored filters without rebuilding the report, chart, or trips panel (see
        # their own guards), so whichever one is being returned to is rebuilt
        # explicitly below, the same way _show_stats()/_show_chart()/_show_trips() does.
        self.reload()
        if origin == "chart":
            self._build_chart()
            self._fill_chart()
            self._set_panel("chart")
            if row is not None:
                table = self.query_one("#chart", DataTable)
                if 0 <= row < table.row_count:
                    table.move_cursor(row=row)
            return
        if origin == "trips":
            self._build_trips()
            self._fill_trips()
            self._set_panel("trips")
            table = self.query_one("#trip_table", trips_panel.TripTable)
            # Coming back from a drill-down is not a fresh open of the panel -- the
            # cursor was already visible on the row the user drilled from (they had to
            # touch the table to get there), so it stays visible rather than hiding
            # again the way _show_trips() makes a genuinely new open start clean.
            table.show_cursor = True
            if row is not None and 0 <= row < table.row_count:
                table.move_cursor(row=row)
            return
        self._build_report()
        self._fill_stats()
        self._set_panel("stats")
        if row is not None:
            table = self.query_one("#stats_table", DataTable)
            if 0 <= row < table.row_count:
                table.move_cursor(row=row)

    def action_drill_down(self) -> None:
        """The right arrow's twin of enter on a statistics row, a chart bar, or a
        trips-panel row."""
        if self._panel == "chart":
            table = self.query_one("#chart", DataTable)
            self._drill_into_bar(table.cursor_row)
            return
        if self._panel == "trips":
            table = self.query_one("#trip_table", DataTable)
            self._drill_into_trip_row(table.cursor_row)
            return
        table = self.query_one("#stats_table", DataTable)
        self._drill_into_category(table.cursor_row)

    def action_drill_up(self) -> None:
        """The left arrow's "back" out of a statistics or chart drill-down."""
        self._go_back_from_drill()
