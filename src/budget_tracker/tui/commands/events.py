"""Textual message handlers for the sidebar lists and the data tables.

Routes clicks/selections to whichever command family owns what was clicked; the logic
of what happens next (filtering, drilling down, answering a setup question, importing a
file, opening a period) lives in that family's own module.
"""

from __future__ import annotations

from textual.widgets import DataTable, ListView

from budget_tracker import stats
from budget_tracker.tui import transactions


class EventCommands:
    """``on_list_view_selected``, and the two ``DataTable`` row-selection handlers."""

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index or 0
        list_id = event.list_view.id
        if list_id == "vendors" and index > self._vendor_shown_count():
            # The trailing "N more" row: not a vendor, just a count. See
            # VENDOR_SIDEBAR_CAP -- clicking it should not silently filter by whatever
            # real vendor happens to sit at that row index.
            self.notify(
                "That row is just a count, not a vendor. "
                "Use 'filter vendor:<text>' to find one further down the list.",
                severity="warning",
            )
            return
        # A sidebar filter is a new view; the flag it might invalidate is checked in
        # _set_drilled_from() (no-op if it was already clear).
        self._set_drilled_from(None)
        if list_id == "accounts":
            self.account_filter = None if index == 0 else self._accounts[index - 1].id
        elif list_id == "vendors":
            if index == 0:
                self.vendor_filter = None
            else:
                vendor = self._vendors[index - 1]
                self.vendor_filter = (vendor.kind, vendor.id)
        elif list_id == "categories":
            self.category_filter = None if index == 0 else self._categories[index - 1].id
        elif list_id == "tags":
            self.tag_filter = None if index == 0 else self._tags[index - 1].id
        elif list_id == "trips":
            self.trip_filter = None if index == 0 else self._trips[index - 1].id
        self.reload()

    def on_txn_table_select_clicked(
        self, event: transactions.TxnTable.SelectClicked
    ) -> None:
        """A click straight on the Sel column toggles that row, first click included."""
        self._toggle_txn_selected(event.row)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row in the imports panel imports that file."""
        if event.data_table.id == "stats_table":
            self._drill_into_category(event.cursor_row)
            return
        if event.data_table.id == "chart":
            self._drill_into_bar(event.cursor_row)
            return
        if event.data_table.id == "trip_table":
            self._drill_into_trip_row(event.cursor_row)
            return
        if event.data_table.id == "setup":
            if self._setup is not None and self._setup.question is not None:
                choices = self._setup.question.choices
                if 0 <= event.cursor_row < len(choices):
                    self._answer_setup(str(choices[event.cursor_row]))
            return
        if event.data_table.id == "periods":
            row = event.cursor_row
            if row == len(stats.PRESETS):  # the Custom… row, always last
                self._ask_range()
            elif 0 <= row < len(stats.PRESETS):
                self._open_period(stats.resolve(stats.PRESETS[row][0]))
            return
        if event.data_table.id == "txns":
            # Enter, and a click on a row the cursor is already on, both fire this;
            # either toggles the row, same as 'x'. A first click on the Sel column of
            # some other row is handled by TxnTable.SelectClicked instead, because
            # DataTable does not post this message for one. See _toggle_txn_selected().
            self._toggle_txn_selected(event.cursor_row)
            return
        if event.data_table.id != "imports":
            return
        row = event.cursor_row
        # The navigation rows come first, so a candidate's index is offset by them.
        if 0 <= row < len(self._import_nav):
            self._open_import_dir(self._import_nav[row])
            return
        row -= len(self._import_nav)
        if not 0 <= row < len(self._candidates):
            return
        self._import_candidate(self._candidates[row])
