"""Vendor-facing commands: ``rename``, ``rule``, ``categorize``, and the rules panel.

Distinct from :mod:`budget_tracker.tui.commands.categories`, which manages the category
*hierarchy* itself rather than which category a vendor's transactions fall under.
"""

from __future__ import annotations

from textual.widgets import DataTable

from budget_tracker import categories, queries, vendors
from budget_tracker.tui import rules as rules_panel


class RuleCommands:
    """``rename``, ``rule``, ``categorize``, and ``rules``."""

    CATEGORIZE_USAGE = "Usage: categorize <vendor> = <category>   (blank category undoes it)"
    CATEGORY_RULE_USAGE = "Usage: rule categorize <pattern> = <category>"

    def _do_rename(self, arg: str) -> None:
        if "=" not in arg:
            self.notify(
                "Usage: rename <raw vendor> = <display name>", severity="warning"
            )
            return
        raw, display = (part.strip() for part in arg.split("=", 1))
        if not raw or not display:
            self.notify(
                "Usage: rename <raw vendor> = <display name>", severity="warning"
            )
            return
        with self.session_factory() as session:
            ok = vendors.set_override(session, raw, display)
            if ok:
                # A category rule may be written against the new display name.
                categories.apply_category_rules(session)
                session.commit()
        if not ok:
            self.notify(f"No vendor named {raw!r}.", severity="error")
            return
        self.reload()
        self.notify(f"{raw!r} → {display!r}")

    def _do_rule(self, arg: str) -> None:
        """``rule <pattern> = <display name>`` renames; ``rule categorize ...`` categorizes.

        Both kinds of rule start with ``rule`` so they read as one family. A vendor
        pattern that genuinely starts with the word "categorize" is not a realistic
        merchant string, so the keyword is safe to claim.
        """
        if not arg:
            self._show_rules()
            return
        head, _, rest = arg.partition(" ")
        if head.lower() in {"categorize", "categorise", "cat"}:
            self._do_category_rule(rest.strip())
            return
        if "=" not in arg:
            self.notify(
                "Usage: rule <pattern> = <display name>", severity="warning"
            )
            return
        pattern, display = (part.strip() for part in arg.split("=", 1))
        if not pattern or not display:
            self.notify(
                "Usage: rule <pattern> = <display name>", severity="warning"
            )
            return
        with self.session_factory() as session:
            vendors.add_rule(session, pattern, display)
            changed = vendors.apply_rules(session)
            # Renames change display names, and a category rule may match those -- so
            # the category rules run after, as they do at the end of every import.
            categories.apply_category_rules(session)
            session.commit()
        self.reload()
        self.notify(f"{pattern!r} → {display!r} ({changed} vendors updated)")

    def _do_categorize(self, arg: str) -> None:
        """``categorize <vendor> = <category>``, its blank-category undo, and its rules."""
        arg = arg.strip()
        if not arg or arg.lower() == "rules":
            self._show_rules()
            return
        head, _, rest = arg.partition(" ")
        # The older spelling of `rule categorize`, kept so it still works.
        if head.lower() == "rule":
            self._do_category_rule(rest.strip())
            return
        if "=" not in arg:
            self.notify(self.CATEGORIZE_USAGE, severity="warning")
            return
        vendor, value = (part.strip() for part in arg.split("=", 1))
        if not vendor:
            self.notify(self.CATEGORIZE_USAGE, severity="warning")
            return

        with self.session_factory() as session:
            # Checked up front because both calls return 0 for an unknown vendor and for
            # one with nothing to change, and those deserve different answers.
            if queries.resolve_vendor_filter(session, vendor) is None:
                self.notify(f"No vendor named {vendor!r}.", severity="error", markup=False)
                return
            if value:
                changed = categories.set_category(session, vendor, value)
                message = f"{vendor!r} → {value!r} ({changed} transactions categorized)"
            else:
                # Mirrors a bare `filter`: leaving the right-hand side empty undoes it.
                changed = categories.clear_category(session, vendor)
                message = f"{vendor!r}: cleared the category on {changed} transactions."
            session.commit()
        self.reload()
        self.notify(message, markup=False)

    def _do_category_rule(self, arg: str) -> None:
        if not arg:
            self._show_rules()
            return
        if "=" not in arg:
            self.notify(self.CATEGORY_RULE_USAGE, severity="warning")
            return
        pattern, value = (part.strip() for part in arg.split("=", 1))
        if not pattern or not value:
            self.notify(self.CATEGORY_RULE_USAGE, severity="warning")
            return
        with self.session_factory() as session:
            categories.add_rule(session, pattern, value)
            changed = categories.apply_category_rules(session)
            session.commit()
        self.reload()
        # markup=False: patterns are globs, and may carry brackets.
        self.notify(
            f"{pattern!r} → {value!r} ({changed} transactions categorized)", markup=False
        )

    def _show_rules(self) -> None:
        self._build_rules()
        self._fill_rules()
        self._set_panel("rules")
        if not self._rules and not self._category_rules:
            self.notify(
                "No vendor rules yet, and no category rules. Add one with:\n"
                "  rule <pattern> = <display name>\n"
                "  rule categorize <pattern> = <category>"
            )

    def _build_rules(self) -> None:
        """Fetch both kinds of rule. Matches every rule against every vendor (see
        queries.get_rules/get_category_rules), so -- like _build_trips, _build_report --
        this runs only when the rules panel is being opened or is already on screen
        (see reload()'s guard), not on every reload regardless of what is showing.
        """
        with self.session_factory() as session:
            self._rules = queries.get_rules(session)
            self._category_rules = queries.get_category_rules(session)

    def _fill_rules(self) -> None:
        rules_panel.fill_rules(
            self.query_one("#rules", DataTable), self._rules, self._category_rules
        )
