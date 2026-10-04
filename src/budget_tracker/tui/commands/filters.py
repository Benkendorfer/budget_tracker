"""``section``, ``filter``, and ``sort`` -- narrowing and ordering the transactions view."""

from __future__ import annotations

from budget_tracker import queries


class FilterCommands:
    """``section``, ``filter``, and ``sort``."""

    SORT_USAGE = "Usage: sort size | sort date"

    def _do_section(self, arg: str) -> None:
        """``section <name>`` expands that sidebar section, collapsing the rest.

        ``<name>`` may be any unambiguous prefix of accounts/vendors/categories/tags/
        trips, case-insensitive -- e.g. ``section cat`` for Categories, ``section tr``
        for Trips (the only section starting "tr", since "tags" does not). A prefix
        matching more than one name, like bare ``section t`` (tags and trips both
        qualify), is refused rather than guessed at.
        """
        name = arg.strip().lower()
        if not name:
            self.notify(
                "Usage: section <name>  (accounts, vendors, categories, tags, trips)",
                severity="warning",
            )
            return
        matches = [section for section in self.SECTIONS if section.startswith(name)]
        if len(matches) == 1:
            self._expand_section(matches[0])
            return
        if not matches:
            self.notify(f"Unknown section: {arg!r}", severity="warning", markup=False)
            return
        self.notify(
            f"Ambiguous section {arg!r}: matches {', '.join(matches)}.",
            severity="warning",
            markup=False,
        )

    def _do_filter(self, arg: str) -> None:
        """`filter text` searches everything; `filter vendor:text` narrows the field."""
        self._set_drilled_from(None)  # a new search is a new view, not the drill-down's
        arg = arg.strip()
        if not arg:
            self.text_filter = None
            self.reload()
            self.notify("Text filter cleared.")
            return

        field, _, rest = arg.partition(":")
        if rest.strip() and field.strip().lower() in queries.TEXT_FIELDS:
            text_filter = queries.TextFilter(rest.strip(), field.strip().lower())
        else:
            # No recognised prefix, so the whole argument is the search text. This also
            # means a colon inside ordinary text is treated literally.
            text_filter = queries.TextFilter(arg, "all")
        self.text_filter = text_filter
        self.reload()
        where = (
            "description, vendor, and raw name"
            if text_filter.field == "all"
            else text_filter.field
        )
        self.notify(f"Filtering {where} for {text_filter.text!r}.", markup=False)

    def _txn_order(self) -> str:
        """The order #txns should be in: size while `sort size`'s view lasts, else date.

        The view ends the moment the filters differ from the ones `sort size` was given,
        whichever command changed them -- so there is no list of filter-changing
        commands to keep in step with.
        """
        if (
            self._size_sort_filters is not None
            and self._size_sort_filters != self._active_filters()
        ):
            self._size_sort_filters = None
        return queries.ORDER_SIZE if self._size_sort_filters is not None else queries.ORDER_DATE

    def _do_sort(self, arg: str) -> None:
        """``sort size``: this view, largest first; ``sort date``: back to newest first."""
        arg = arg.strip().lower()
        if arg in ("size", "amount"):
            if self._panel != "txns":
                self._set_panel("txns")
            self._size_sort_filters = self._active_filters()
        elif arg == "date":
            self._size_sort_filters = None
        else:
            self.notify(self.SORT_USAGE, severity="warning")
            return
        self.reload()
