"""Key-binding actions (see ``BudgetApp.BINDINGS``) and the small helpers only they use.

``check_action`` (which gates several of these to the one panel each applies to) stays
on ``BudgetApp`` itself in ``app.py``, since it has to know about every panel these
dispatch across.
"""

from __future__ import annotations

from typing import Optional

from textual.widgets import DataTable, Input, ListView

from budget_tracker import charts, queries
from budget_tracker.tui.commands.chart import _CHART_BUCKETS
from budget_tracker.tui.commands.pie import _SHARE_BUCKETS


class ActionCommands:
    """``action_*`` bindings, plus the vendor/cursor helpers ``ctrl+n``/``ctrl+t`` use."""

    # ------------------------------------------------------------- vim keys
    # Gated by check_action to a focused DataTable or ListView, so self.focused is one.
    def action_vim_down(self) -> None:
        self.focused.action_cursor_down()

    def action_vim_up(self) -> None:
        self.focused.action_cursor_up()

    def action_vim_left(self) -> None:
        # The same thing the left arrow does here: back out of a drill-down when there is
        # one to leave, otherwise move the table's cursor.
        if self.check_action("drill_up", ()):
            self.action_drill_up()
        elif isinstance(self.focused, DataTable):
            self.focused.action_cursor_left()

    def action_vim_right(self) -> None:
        if self.check_action("drill_down", ()):
            self.action_drill_down()
        elif isinstance(self.focused, DataTable):
            self.focused.action_cursor_right()

    def _selected_vendor(self) -> Optional[queries.VendorRow]:
        """The vendor ctrl+n targets: the active filter, else the highlighted row."""
        if self.vendor_filter is not None:
            kind, vendor_id = self.vendor_filter
            for vendor in self._vendors:
                if (vendor.kind, vendor.id) == (kind, vendor_id):
                    return vendor
            return None
        # Index 0 is the "— All —" row, so the list is offset by one. Bounded by what is
        # actually mounted (see VENDOR_SIDEBAR_CAP), not the full vendor count, or this
        # would resolve the trailing "N more" row to whatever real vendor happens to sit
        # at that index.
        index = self.query_one("#vendors", ListView).index or 0
        if not 1 <= index <= self._vendor_shown_count():
            return None
        return self._vendors[index - 1]

    def _prefill_command(self, text: str) -> None:
        command = self.query_one("#command", Input)
        command.value = text
        command.cursor_position = len(text)
        command.focus()

    def _cursor_txn(self) -> Optional[queries.TxnRow]:
        """The transaction under the table cursor, when the table has focus."""
        table = self.query_one("#txns", DataTable)
        if self.focused is not table:
            return None
        row = table.cursor_row
        if not 0 <= row < len(self._txns):
            return None
        return self._txns[row]

    def _prefill_for_vendor(self, verb: str) -> None:
        """Prefill ``<verb> <raw vendor> = `` for whichever vendor is being pointed at.

        In the transaction table, that is the selected transaction's vendor. Rows carry
        the raw merchant string, so this works even for already-grouped vendors.
        Otherwise the sidebar decides: the active vendor filter, else the highlighted row.
        """
        txn = self._cursor_txn()
        if txn is not None:
            if not txn.vendor_raw:
                self.notify("That transaction has no vendor.", severity="warning")
                return
            self._prefill_command(f"{verb} {txn.vendor_raw} = ")
            return

        vendor = self._selected_vendor()
        if vendor is None:
            self.notify("Select a vendor in the sidebar first.", severity="warning")
            return
        if vendor.kind != "raw":
            # These commands are keyed on the raw vendor string, which the sidebar no
            # longer shows once a group exists, so we can only prefill the verb.
            self.notify(
                f"{vendor.name!r} is an override group — pick a raw vendor instead.",
                severity="warning",
            )
            self._prefill_command(f"{verb} ")
            return
        self._prefill_command(f"{verb} {vendor.name} = ")

    def action_toggle_stats_fold(self) -> None:
        """Space on a statistics row: fold/unfold its subtree.

        Same key, on the trips panel, folds/unfolds a trip's buckets instead --
        see check_action() for the gate and _toggle_trip_fold() for what it does.
        """
        if self._panel == "trips":
            table = self.query_one("#trip_table", DataTable)
            self._toggle_trip_fold(table.cursor_row)
            return
        if self._panel == "budget_plan":
            table = self.query_one("#budget_plan", DataTable)
            self._toggle_budget_fold(table.cursor_row)
            return
        table = self.query_one("#stats_table", DataTable)
        self._toggle_fold(table.cursor_row)

    def action_toggle_all_stats_folds(self) -> None:
        """``f``: fold/unfold every group in the statistics table, or every trip in
        the trips panel. See check_action()."""
        if self._panel == "trips":
            self._toggle_trip_fold_all()
            return
        if self._panel == "budget_plan":
            self._toggle_budget_fold_all()
            return
        self._toggle_fold_all()

    def action_toggle_selected(self) -> None:
        """``x`` on the transactions table: select/deselect the row under the cursor.

        See check_action() for the gate, and _toggle_txn_selected() for what it does.
        """
        table = self.query_one("#txns", DataTable)
        self._toggle_txn_selected(table.cursor_row)

    def action_cycle_bucket(self) -> None:
        """``b``: on the chart, step day → week → month → day; on the pie, step
        week → month → year → week. See check_action() for which panel gets which.

        A cycle rather than three keys, and it does not skip a bucket that would be
        unwieldy for the window: charting two years by day is a bad idea but it is the
        user's to make, and a key that silently refuses to do anything is worse.
        """
        if self._panel == "pie":
            if self.window is None:
                return
            order = _SHARE_BUCKETS
            self._pie_bucket = order[(order.index(self._pie_bucket) + 1) % len(order)]
            self._redraw_pie()
            return
        if self.window is None or self._bucket is None:
            return
        order = _CHART_BUCKETS
        self._bucket = order[(order.index(self._bucket) + 1) % len(order)]
        self._redraw_chart()

    def action_cycle_measure(self) -> None:
        """``m`` on the chart: step net → spending → income → net. See check_action()."""
        if self.window is None:
            return
        order = charts.MEASURES
        self._measure = order[(order.index(self._measure) + 1) % len(order)]
        self._redraw_chart()

    def action_rename_vendor(self) -> None:
        # A non-empty selection wins: bulk-editing several rows is what ctrl+n is for
        # once any are checked, over renaming just the one under the cursor.
        if self._selected_ids:
            self._prefill_command("sel vendor = ")
            return
        self._prefill_for_vendor("rename")

    def action_categorize_vendor(self) -> None:
        if self._selected_ids:
            self._prefill_command("sel category = ")
            return
        self._prefill_for_vendor("categorize")

    def action_show_transactions(self) -> None:
        # Escape is the general-purpose "leave this view" key, so it drops the
        # drill-down's back-link even when the panel is already "txns".
        self._set_drilled_from(None)
        if self._setup is not None:
            self.notify(f"Setup for {self._setup.path.name} canceled.")
            self._cancel_setup()
            return
        if self._range_pending:
            self.notify("Custom range canceled.")
            self._cancel_range()
            return
        if self._pending_unimport is not None:
            self.notify("Unimport canceled.")
            self._cancel_unimport()
            return
        if self._pending_category is not None:
            self.notify("Category move canceled.")
            self._cancel_category()
            return
        if self._pending_category_merge is not None:
            self.notify("Merge canceled.")
            self._cancel_category_merge()
            return
        if self._pending_budget_edit is not None:
            self.notify("Budget edit canceled.")
            self._cancel_budget_edit()
            self._refocus_budget_plan()
            return
        if self._panel != "txns":
            self._set_panel("txns")

    def action_refresh(self) -> None:
        self.reload()

    def action_clear_filters(self) -> None:
        self._set_drilled_from(None)
        self.account_filter = None
        self.vendor_filter = None
        self.category_filter = None
        self.text_filter = None
        self.date_filter = None
        self.tag_filter = None
        self.trip_filter = None
        self.category_ids_filter = None
        self.reload()
        self.notify("Filters cleared.")
