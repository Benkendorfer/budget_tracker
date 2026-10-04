"""``stats`` -- the category breakdown panel, its report, and its folding."""

from __future__ import annotations

from textual.widgets import DataTable

from budget_tracker import stats
from budget_tracker.tui import stats as stats_panel


class StatsCommands:
    """``stats``, and the report-building/folding it shares with ``pie``."""

    def _do_stats(self, arg: str) -> None:
        """Bare ``stats`` opens the period picker; ``stats <spec>`` skips it."""
        if not arg:
            self._show_periods()
            return
        window = self._parse_window(arg)
        if window is not None:
            self._show_stats(window)

    def _show_stats(self, window: stats.Window) -> None:
        self.window = window
        self._range_pending = False
        self._prompt_panel = None
        self._build_report()
        self._fill_stats()
        self._set_panel("stats")

    def _build_report(self) -> None:
        with self.session_factory() as session:
            self._report = stats.build_report(
                session,
                self.window,
                filters=self._active_filters().replace(date_range=None),
            )

    def _fill_stats(self) -> None:
        """Render the report, honouring folded subtrees. See stats_panel.fill_stats()."""
        table = self.query_one("#stats_table", DataTable)
        self._stats_rows, self._foldable_ids = stats_panel.fill_stats(
            table, self._report, self._collapsed
        )

    def _toggle_fold(self, row: int) -> None:
        """Space on a stats row: collapse/expand its subtree if it has one.

        A leaf row, the TOTAL row, or an out-of-range row does nothing — not a crash,
        not a notification, since space is not obviously "for" the stats table the way
        enter or the arrows are.
        """
        if not stats_panel.toggle_fold(row, self._stats_rows, self._foldable_ids, self._collapsed):
            return
        self._fill_stats()
        # The toggled row's own subtree is what grows or shrinks, always right after it,
        # so its own row index is unchanged by the toggle — the cursor can just stay put.
        table = self.query_one("#stats_table", DataTable)
        if 0 <= row < table.row_count:
            table.move_cursor(row=row)

    def _toggle_fold_all(self) -> None:
        """``f``: fold every group if any is expanded, else unfold them all.

        "Any expanded" rather than "all collapsed" so the key always visibly does
        something — a mix of folded and unfolded groups collapses fully on the first
        press instead of silently unfolding the already-collapsed ones.
        """
        if not self._foldable_ids:
            return
        table = self.query_one("#stats_table", DataTable)
        row = table.cursor_row
        stats_panel.toggle_fold_all(self._foldable_ids, self._collapsed)
        self._fill_stats()
        # Collapsing/expanding everything moves rows around far more than a single
        # toggle does, so there is no single "same row" to return to — just keep the
        # cursor in range rather than landing on an arbitrary category.
        if table.row_count:
            table.move_cursor(row=min(row, table.row_count - 1))

    def _stats_status(self) -> str:
        """One line under the status budget. See stats_panel.stats_status()."""
        return stats_panel.stats_status(self._report)
