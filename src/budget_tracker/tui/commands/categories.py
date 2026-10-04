"""``category ...`` -- build, move, and merge the category hierarchy itself.

Distinct from :mod:`budget_tracker.tui.commands.rules`'s ``categorize``, which only
decides which category a vendor's transactions fall under.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Input, Static

from budget_tracker import categories


class CategoryCommands:
    """``category``, including its relocation/merge confirmation prompts."""

    CATEGORY_MERGE_USAGE = "Usage: category merge <source> = <target>"

    def _do_category(self, arg: str) -> None:
        """``category <path>`` builds/moves a category; bare or ``list`` shows the tree.

        Distinct from ``categorize``: this manages the category hierarchy itself
        (creating, nesting, re-parenting), not which category a vendor's transactions
        get. A one-element path is a move to the top level (:func:`categories.ensure_path`).

        Names are unique across the whole tree, so a path level that already exists
        somewhere else is a *relocation* of that whole category, not a new one — see
        :func:`categories.preview_path`. That is confirmed before it happens, the same
        shape as ``unimport``.
        """
        arg = arg.strip()
        if not arg or arg.lower() == "list":
            self._notify_category_tree()
            return
        head, _, rest = arg.partition(" ")
        if head.lower() == "merge":
            self._do_category_merge(rest.strip())
            return
        with self.session_factory() as session:
            try:
                preview = categories.preview_path(session, arg)
            except categories.CategoryError as error:
                self.notify(str(error), severity="warning", markup=False)
                return
            if preview.relocations:
                self._ask_category_relocation(arg, preview)
                return
            category = categories.ensure_path(session, arg)
            path = categories.format_path(session, category)
            session.commit()
        self.reload()
        self.notify(f"{path!r} ready.", markup=False)

    def _ask_category_relocation(self, path: str, preview: categories.PathPreview) -> None:
        self._pending_category = path
        self._prompt_panel = self._panel
        moved = "; ".join(
            f"{r.name!r} from {r.from_parent or 'the top level'} to "
            f"{r.to_parent or 'the top level'} ({r.transaction_count} transaction(s))"
            for r in preview.relocations
        )
        prompt = self.query_one("#prompt", Static)
        # Text(), not markup: a category name is user data and may hold brackets.
        prompt.update(
            Text.assemble(
                (f"{path!r} would relocate {moved}.\n", "bold"),
                (
                    "Type yes to confirm; anything else, or escape, cancels. A "
                    "separate category needs its own distinct name, e.g. "
                    "'Dining (Travel)'.",
                    "dim",
                ),
            )
        )
        prompt.display = True
        self.query_one("#command", Input).focus()

    def _answer_category(self, text: str) -> None:
        path = self._pending_category
        self._cancel_category()
        if text.strip().lower() != "yes":
            self.notify("Category move cancelled.")
            return
        with self.session_factory() as session:
            try:
                category = categories.ensure_path(session, path, confirm_relocation=True)
                result_path = categories.format_path(session, category)
            except categories.CategoryError as error:
                self.notify(str(error), severity="warning", markup=False)
                return
            session.commit()
        self.reload()
        self.notify(f"{result_path!r} ready.", markup=False)

    def _cancel_category(self) -> None:
        self._pending_category = None
        self._prompt_panel = None
        self.query_one("#prompt", Static).display = False

    def _do_category_merge(self, arg: str) -> None:
        """``category merge <source> = <target>`` — destructive, so it only previews.

        :func:`categories.merge_category` has no dry-run of its own, so the preview is
        the real call made inside a session that is never committed: closing it below
        discards everything it did, and the counts on the returned
        :class:`categories.MergeResult` are exactly what a real merge would move,
        read before the source category was deleted. ``_answer_category_merge`` re-runs
        it for real, and commits, only once the user has confirmed.
        """
        if "=" not in arg:
            self.notify(self.CATEGORY_MERGE_USAGE, severity="warning")
            return
        source, target = (part.strip() for part in arg.split("=", 1))
        if not source or not target:
            self.notify(self.CATEGORY_MERGE_USAGE, severity="warning")
            return
        with self.session_factory() as session:
            try:
                result = categories.merge_category(session, source, target)
            except categories.CategoryError as error:
                self.notify(str(error), severity="warning", markup=False)
                return
            # Not committed: leaving the `with` block below rolls this back.

        self._pending_category_merge = (source, target)
        self._prompt_panel = self._panel
        prompt = self.query_one("#prompt", Static)
        prompt.update(
            Text.assemble(
                (
                    f"Merge {result.source!r} into {result.target!r}: "
                    f"{result.moved_transactions} transaction(s), "
                    f"{result.moved_rules} rule(s), "
                    f"{result.moved_children} child categor"
                    f"{'y' if result.moved_children == 1 else 'ies'} moved, then "
                    f"{result.source!r} is deleted.\n",
                    "bold",
                ),
                ("Type yes to confirm; anything else, or escape, cancels.", "dim"),
            )
        )
        prompt.display = True
        self.query_one("#command", Input).focus()

    def _answer_category_merge(self, text: str) -> None:
        source, target = self._pending_category_merge
        self._cancel_category_merge()
        if text.strip().lower() != "yes":
            self.notify("Merge cancelled.")
            return
        with self.session_factory() as session:
            try:
                result = categories.merge_category(session, source, target)
            except categories.CategoryError as error:
                self.notify(str(error), severity="error", markup=False)
                return
            session.commit()
        self.reload()
        self.notify(
            f"Merged {result.source!r} into {result.target!r}: "
            f"{result.moved_transactions} transaction(s), {result.moved_rules} rule(s), "
            f"{result.moved_children} child categories moved.",
            markup=False,
        )

    def _cancel_category_merge(self) -> None:
        self._pending_category_merge = None
        self._prompt_panel = None
        self.query_one("#prompt", Static).display = False

    def _notify_category_tree(self) -> None:
        if not self._categories:
            self.notify("No categories yet. Add one with: category Food > Dining")
            return
        lines = [f"{'  ' * c.depth}{c.name} ({c.count})" for c in self._categories]
        self.notify("\n".join(lines), title="Categories", markup=False, timeout=8)
