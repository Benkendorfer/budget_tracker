"""``trips`` (the panel) and ``trip ...`` (the bucket map and date overrides)."""

from __future__ import annotations

from datetime import date

from textual.widgets import DataTable, Static

from budget_tracker import queries, tags as tags_module, trips
from budget_tracker.tui import trips as trips_panel


class TripCommands:
    """``trips`` and ``trip``."""

    TRIP_USAGE = (
        "Usage: trip bucket <category>[, <category>...] = <bucket>   "
        "(blank bucket unmaps it) | trip buckets | "
        "trip dates <trip> = <start>..<end>   (leave either side of the '..' empty to "
        "set just the other; blank derives both again)"
    )

    def _build_trips(self) -> None:
        """Fetch every trip's dates, cost, and bucket breakdown -- no window, no
        filters: queries.get_trips takes none (a trip is already its own scope)."""
        with self.session_factory() as session:
            self._trip_data = queries.get_trips(session)

    def _fill_trips(self) -> None:
        """Redraw the table (columns included -- the Breakdown column's width is
        adaptive, see trips_panel.bar_width) and the shared legend beneath it."""
        table = self.query_one("#trip_table", DataTable)
        width = trips_panel.bar_width(self.query_one("#main").size.width)
        self._trip_rows, self._trips_foldable_ids = trips_panel.fill_trips(
            table, self._trip_data, self._trips_expanded, width
        )
        self.query_one("#trips_legend", Static).update(trips_panel.legend())

    def _toggle_trip_fold(self, row: int) -> None:
        """Space on a trip row: unfold/fold its trips.BUCKETS rows. See check_action()."""
        if not trips_panel.toggle_fold(
            row, self._trip_rows, self._trips_foldable_ids, self._trips_expanded
        ):
            return
        self._fill_trips()
        table = self.query_one("#trip_table", DataTable)
        if 0 <= row < table.row_count:
            table.move_cursor(row=row)

    def _toggle_trip_fold_all(self) -> None:
        """``f`` on the trips table: unfold/fold every trip. See check_action()."""
        if not self._trips_foldable_ids:
            return
        table = self.query_one("#trip_table", DataTable)
        row = table.cursor_row
        trips_panel.toggle_fold_all(self._trips_foldable_ids, self._trips_expanded)
        self._fill_trips()
        if table.row_count:
            table.move_cursor(row=min(row, table.row_count - 1))

    def _show_trips(self) -> None:
        """``trips``: seed the bucket map if it is still empty, then open the panel.

        Seeding is idempotent (trips.seed_default_buckets checks the table itself, not
        the individual names) so calling it on every open costs one cheap query once
        the user has actually edited the map, and makes the map useful the very first
        time without a separate setup step.
        """
        with self.session_factory() as session:
            trips.seed_default_buckets(session)
            session.commit()
        self._build_trips()
        self._fill_trips()
        # Opening the panel starts with nothing highlighted, however the table was left
        # last time -- see trips_panel.TripTable.
        self.query_one("#trip_table", trips_panel.TripTable).hide_cursor()
        self._set_panel("trips")

    def _do_trip(self, arg: str) -> None:
        """``trip bucket ... = ...`` sets the map; ``trip buckets`` shows it;
        ``trip dates ... = ...`` overrides a trip's dates."""
        arg = arg.strip()
        head, _, rest = arg.partition(" ")
        if head.lower() == "buckets":
            self._notify_trip_buckets()
            return
        if head.lower() == "bucket":
            self._do_trip_bucket(rest.strip())
            return
        if head.lower() == "dates":
            self._do_trip_dates(rest.strip())
            return
        self.notify(self.TRIP_USAGE, severity="warning")

    def _do_trip_dates(self, arg: str) -> None:
        """``trip dates <trip> = <start>..<end>``, overriding the derived dates.

        A trip takes its dates from its transactions, which is wrong in the two cases
        that matter: a flight booked months ahead drags the start back to the booking,
        and a trip whose last purchase was days before flying home ends early. Neither
        can be fixed by editing a transaction. A blank right-hand side goes back to
        deriving them, the way every other ``=`` command in this app undoes.
        """
        if "=" not in arg:
            self.notify(self.TRIP_USAGE, severity="warning")
            return
        name, value = (part.strip() for part in arg.split("=", 1))
        if not name:
            self.notify(self.TRIP_USAGE, severity="warning")
            return
        if value:
            start_text, separator, end_text = value.partition("..")
            if not separator:
                self.notify(self.TRIP_USAGE, severity="warning")
                return
            # An empty side of the ".." leaves that end alone rather than clearing it,
            # so "= 2026-05-14.." fixes a start without the user having to restate an
            # end that was already right. Clearing both is the bare "trip dates X ="
            # below, matching how every other blank right-hand side in this app undoes.
            try:
                start = (
                    date.fromisoformat(start_text.strip())
                    if start_text.strip()
                    else tags_module.KEEP
                )
                end = (
                    date.fromisoformat(end_text.strip())
                    if end_text.strip()
                    else tags_module.KEEP
                )
            except ValueError:
                self.notify("Dates must be YYYY-MM-DD.", severity="error")
                return
            if start is tags_module.KEEP and end is tags_module.KEEP:
                self.notify(self.TRIP_USAGE, severity="warning")
                return
        else:
            start = end = None

        with self.session_factory() as session:
            try:
                found = tags_module.set_trip_dates(session, name, start, end)
            except ValueError as error:
                self.notify(str(error), severity="error", markup=False)
                return
            if not found:
                self.notify(f"No trip named {name!r}.", severity="error", markup=False)
                return
            session.commit()
        if self._panel == "trips":
            self._build_trips()
            self._fill_trips()
        self.reload()
        if start is None and end is None:
            message = f"{name}: dates back to whatever its transactions say."
        elif end is tags_module.KEEP:
            message = f"{name}: starts {start}."
        elif start is tags_module.KEEP:
            message = f"{name}: ends {end}."
        else:
            message = f"{name}: {start} .. {end}."
        # Setting one end is checked against the other's *override*, which is the right
        # place to refuse outright. But the end the user actually sees may still be the
        # derived one, and a manual start after a derived end reads as a trip that ended
        # before it began. Refusing that would make correcting both ends impossible one
        # at a time -- the reason for setting them separately at all -- so say so and
        # let them finish.
        if self._trip_dates_are_inverted(name):
            message += " Its start is now after its end; set the other end too."
            self.notify(message, severity="warning", markup=False)
            return
        self.notify(message, markup=False)

    def _trip_dates_are_inverted(self, name: str) -> bool:
        """Whether ``name`` now *shows* a start after its end, override or derived."""
        with self.session_factory() as session:
            for row in queries.get_trips(session):
                if row.name == name:
                    return (
                        row.start is not None
                        and row.end is not None
                        and row.start > row.end
                    )
        return False

    def _do_trip_bucket(self, arg: str) -> None:
        """``trip bucket <categories> = <bucket>`` -- category (or comma-separated
        list) on the left, bucket on the right, matching every other ``=`` command in
        this app. A blank right-hand side unmaps, the way ``categorize <vendor> =``
        already does.
        """
        if "=" not in arg:
            self.notify(self.TRIP_USAGE, severity="warning")
            return
        left, value = (part.strip() for part in arg.split("=", 1))
        category_names = [name.strip() for name in left.split(",") if name.strip()]
        if not category_names:
            self.notify(self.TRIP_USAGE, severity="warning")
            return
        with self.session_factory() as session:
            try:
                if value:
                    changed = trips.set_bucket(session, category_names, value)
                    message = (
                        f"{', '.join(category_names)} → {value} "
                        f"({changed} categor{'y' if changed == 1 else 'ies'})"
                    )
                else:
                    changed = trips.clear_bucket(session, category_names)
                    message = (
                        f"{', '.join(category_names)}: unmapped "
                        f"({changed} categor{'y' if changed == 1 else 'ies'})"
                    )
            except ValueError as error:
                self.notify(str(error), severity="error", markup=False)
                return
            session.commit()
        if self._panel == "trips":
            self._build_trips()
            self._fill_trips()
            self._refresh_status()
        self.notify(message, markup=False)

    def _notify_trip_buckets(self) -> None:
        with self.session_factory() as session:
            mapping = trips.list_buckets(session)
        lines = [
            f"{bucket}: {', '.join(mapping[bucket]) if mapping[bucket] else '(none)'}"
            for bucket in trips.BUCKETS
        ]
        self.notify("\n".join(lines), title="Trip buckets", markup=False, timeout=8)
