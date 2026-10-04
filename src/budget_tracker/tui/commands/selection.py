"""``sel ...`` -- act on the multi-select, the one place transactions are edited per row.

Every other write in the app keys off a vendor and hits all of that vendor's
transactions; these key off the rows the user actually picked. See ``BudgetApp._do_sel``
for the parsing shape shared with ``filter``/``categorize``.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from budget_tracker import categories, tags as tags_module, transfers, vendors
from budget_tracker.tui.formatting import _fmt_amount


def _set_vendor_then_categorize(session: Session, txn_ids, value: str) -> int:
    """``sel vendor = <name>``: moving rows to another vendor can bring them under a
    category rule (or out from under one), so the rules are re-run afterwards."""
    changed = vendors.set_vendor(session, txn_ids, value)
    categories.apply_category_rules(session)
    return changed


class SelectionCommands:
    """``sel ...`` and its sub-verbs."""

    SEL_USAGE = (
        "Usage: sel all | sel none | sel category = <name> | sel vendor = <name> | "
        "sel tag = <name> | sel untag = <name> | sel trip = <name> | sel untrip | "
        "sel transfer | sel untransfer | sel exclude | sel include"
    )

    # The `sel <subject> = <value>` verbs, and whether the value is allowed to be blank.
    # Only `category` is: a blank right-hand side undoes it, matching
    # `categorize <vendor> =`. Blanking a vendor or a tag has no obvious meaning, so
    # those are a usage error rather than a silent no-op.
    SEL_WRITE_SUBJECTS = {
        "category": True,
        "vendor": False,
        "tag": False,
        "untag": False,
        "trip": False,
    }

    def _do_sel(self, arg: str) -> None:
        """Act on the multi-select — the one place transactions are edited per row.

        Every other write in this app keys off a vendor and hits all of that vendor's
        transactions; these key off the rows the user actually picked. Parsed as a bare
        subject or ``<subject> = <value>``, so an empty right-hand side undoes, the way
        a bare ``filter`` and ``categorize <vendor> =`` already do.
        """
        arg = arg.strip()
        if not arg:
            self.notify(self.SEL_USAGE, severity="warning")
            return
        subject, separator, value = (part.strip() for part in arg.partition("="))
        subject = subject.lower()
        if subject == "all" and not separator:
            self._sel_all()
            return
        if subject == "none" and not separator:
            self._sel_none()
            return
        if subject == "untrip" and not separator:
            self._sel_write(tags_module.clear_trip, "taken off their trip")
            return
        if subject == "transfer" and not separator:
            self._sel_transfer()
            return
        if subject == "exclude" and not separator:
            self._sel_write(
                transfers.exclude,
                "excluded from income and spending (rows already a transfer are left "
                "as they are)",
            )
            return
        if subject == "include" and not separator:
            self._sel_write(transfers.include, "counted again")
            return
        if subject == "untransfer" and not separator:
            self._sel_write(
                transfers.unmark_manual_transfer, "taken out of their manual transfer"
            )
            return
        if subject in self.SEL_WRITE_SUBJECTS:
            if not separator:
                self.notify(self.SEL_USAGE, severity="warning")
                return
            self._sel_apply(subject, value)
            return
        self.notify(
            f"Unknown 'sel' command: {arg!r}\n{self.SEL_USAGE}",
            severity="warning",
            markup=False,
        )

    def _sel_transfer(self) -> None:
        """``sel transfer``: mark the two selected legs as one transfer, by hand.

        For pairs detection cannot see -- a Wise fee makes the legs differ. The fee is
        split off as its own row and stays in the spending figures (the money really is
        gone), and the pop-up says exactly how much that was, so nothing disappears
        from the totals unannounced.
        """
        ids = sorted(self._selected_ids)
        if len(ids) != 2:
            self.notify(
                f"sel transfer needs both legs selected: exactly 2 rows, one out and "
                f"one in ({len(ids)} selected).",
                severity="warning",
            )
            return
        with self.session_factory() as session:
            try:
                result = transfers.mark_manual_transfer(session, ids)
            except transfers.ManualTransferError as error:
                self.notify(str(error), severity="error", markup=False)
                return
            session.commit()
        self.reload()

        legs = (
            f"{result.outflow_account} {_fmt_amount(result.outflow_minor)} ⇄ "
            f"{result.inflow_account} {_fmt_amount(result.inflow_minor)}"
        )
        if result.difference_minor is None:
            detail = (
                "Different currencies, so no fee could be worked out without a rate; "
                "nothing was split off."
            )
        elif result.difference_minor == 0:
            detail = "The legs match exactly; no fee."
        else:
            kind = "fee" if result.difference_minor < 0 else "difference"
            detail = (
                f"Difference {_fmt_amount(result.difference_minor)} {result.currency}, "
                f"kept as a separate 'Transfer {kind}' row in {result.fee_account} "
                f"(category {transfers.FEE_CATEGORY}), so it still counts."
            )
        self.notify(
            f"{legs}\n{detail}\nTagged #{transfers.MANUAL_TRANSFER_TAG}; "
            "sel untransfer undoes it.",
            title="Manual transfer",
            severity="warning" if result.difference_minor else "information",
            timeout=15,
            markup=False,
        )

    def _sel_apply(self, subject: str, value: str) -> None:
        """Run one ``sel <subject> = <value>`` verb over the selection."""
        if not value and not self.SEL_WRITE_SUBJECTS[subject]:
            self.notify(self.SEL_USAGE, severity="warning")
            return

        if subject == "category":
            if value:
                self._sel_write(
                    lambda session, ids: categories.set_category_for(session, ids, value),
                    f"categorized {value!r}",
                )
            else:
                # Mirrors `categorize <vendor> =`: a blank right-hand side undoes.
                self._sel_write(categories.clear_category_for, "cleared of their category")
        elif subject == "vendor":
            self._sel_write(
                lambda session, ids: _set_vendor_then_categorize(session, ids, value),
                f"pointed at vendor {value!r}",
            )
        elif subject == "tag":
            self._sel_write(
                lambda session, ids: tags_module.add_tag(session, ids, value),
                f"tagged {value!r}",
            )
        elif subject == "untag":
            self._sel_write(
                lambda session, ids: tags_module.remove_tag(session, ids, value),
                f"untagged {value!r}",
            )
        elif subject == "trip":
            self._sel_write(
                lambda session, ids: tags_module.set_trip(session, ids, value),
                f"put on trip {value!r}",
            )

    def _sel_write(self, write, description: str) -> None:
        """Apply ``write(session, ids)`` to the selection, then reload and report.

        The selection deliberately survives: setting a category and then a tag on the
        same rows is the common case, and having to reselect between the two would make
        the feature tedious enough not to use. The core modules do not commit -- callers
        own the transaction -- so this does, the way :meth:`_do_categorize` does.
        """
        if not self._selected_ids:
            self.notify("Nothing selected.", severity="warning")
            return
        ids = sorted(self._selected_ids)
        with self.session_factory() as session:
            changed = write(session, ids)
            session.commit()
        self.reload()
        # markup=False: a category, vendor or tag name may contain square brackets,
        # which Rich would otherwise read as markup.
        self.notify(
            f"{changed} transaction{'s' if changed != 1 else ''} {description}.",
            markup=False,
        )

    def _sel_all(self) -> None:
        """Select every transaction the table is currently showing."""
        self._selected_ids = {txn.id for txn in self._txns}
        self._render_selection()
        count = len(self._selected_ids)
        self.notify(f"Selected {count} transaction{'s' if count != 1 else ''}.")

    def _sel_none(self) -> None:
        """Clear the selection."""
        self._selected_ids = set()
        self._render_selection()
        self.notify("Selection cleared.")
