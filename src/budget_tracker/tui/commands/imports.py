"""``import``, ``unimport``, ``format``, and the interactive CSV-layout setup walkthrough.

``TO_IMPORT_DIR`` is defined here (rather than in ``app.py``) because it is this
family's own constant; ``BudgetApp.__init__`` imports it back from here to seed
``self._import_dir``, which keeps the dependency one-directional (``app.py`` depends on
this module, not the other way around).

The tests monkeypatch ``budget_tracker.tui.app.TO_IMPORT_DIR`` and
``budget_tracker.tui.app.import_csv`` directly (a pre-existing fixture, not something
this split gets to change) and import ``IMPORT_PROBLEMS`` from there too. ``app.py``
re-exports all three so those names keep existing on it, but a patch only takes effect
if the code that reads them does so through the live ``app`` module object rather than
through a name copied into this module at import time -- see ``_app_module()`` below,
used at every call site that touches one of the three.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

from rich.text import Text
from textual.widgets import DataTable, Input, Static

from budget_tracker import formats, importer, queries
from budget_tracker.importer import (
    ImportCandidate,
    UnknownImport,
    delete_import,
    inspect_csv,
    list_inbox,
    read_header_and_rows,
)
from budget_tracker.tui import imports as imports_panel
from budget_tracker.tui.imports import _Setup

_REPO_ROOT = Path(__file__).resolve().parents[4]
TO_IMPORT_DIR = _REPO_ROOT / "data" / "to_import"

# Every way an import can refuse a file for a reason the user can act on. Gathered into
# one tuple because the app imports from three places and only one of them used to catch
# anything -- the other two crashed the whole app with a traceback, which is how a Wise
# file taking the wrong branch became a stack trace instead of a message.
IMPORT_PROBLEMS = (
    formats.AccountRequired,
    formats.UnknownFormat,
    formats.AccountCurrencyMismatch,
)


def _app_module():
    """The live ``budget_tracker.tui.app`` module object, fetched at call time.

    Deliberately not ``from budget_tracker.tui.app import TO_IMPORT_DIR`` at this
    module's own top level: that would copy the value once, and a test's
    ``monkeypatch.setattr("budget_tracker.tui.app.TO_IMPORT_DIR", ...)`` reassigns the
    attribute on the ``app`` module, not this module's copy of it. Importing the module
    itself here, and reading ``.TO_IMPORT_DIR``/``.import_csv``/``.IMPORT_PROBLEMS`` off
    it at each call site below, picks up whatever is on it right now.
    """
    import budget_tracker.tui.app as app

    return app


class ImportCommands:
    """``import``, ``unimport``, ``format``, and the setup walkthrough they can open."""

    FORMAT_USAGE = "Usage: format | format <name> invert on|off"

    def _do_import(self, arg: str) -> None:
        app = _app_module()
        if not arg:
            # Bare "import" browses the inbox; enter on a row imports that file.
            self._show_imports()
            return
        if arg == "all":
            # The directory being browsed, not the whole tree: "all" should import what
            # the panel is showing, not quietly reach into folders you have not opened.
            paths = list(list_inbox(self._import_dir, app.TO_IMPORT_DIR).files)
            if not paths:
                self.notify(
                    f"No CSVs in {self._import_label()}", severity="warning"
                )
                return
        else:
            path = Path(arg).expanduser()
            if not path.is_file():
                self.notify(f"File not found: {path}", severity="error")
                return
            paths = [path]

        added = skipped = 0
        imported = 0
        import_ids: List[int] = []
        problems: List[str] = []
        with self.session_factory() as session:
            for path in paths:
                try:
                    result = app.import_csv(session, path)
                except app.IMPORT_PROBLEMS as error:
                    problems.append(f"{path.name}: {error}")
                    continue
                imported += 1
                added += result.inserted
                skipped += result.skipped_duplicates
                import_ids.append(result.import_id)
        self.reload()
        self.notify(
            f"Imported {imported} file(s): {added} added, {skipped} skipped."
        )
        if import_ids:
            self._fetch_rates_after_import(import_ids)
        if problems:
            # An unknown layout needs the interactive setup, and an account-less file
            # needs --account; neither is something the app can decide for you.
            self.notify(
                "\n".join(problems) + "\n\nRun 'budget import <file>' to sort these out.",
                title=f"{len(problems)} file(s) not imported",
                severity="warning",
                timeout=12,
                markup=False,
            )

    def _do_unimport(self, arg: str) -> None:
        """``unimport <id>`` — destructive, so it only asks; ``_answer_unimport`` acts.

        The confirmation names the file, the transaction count, and any transfer
        pairings it would break — read up front through
        :func:`queries.preview_import_delete`, never guessed and never found out by
        deleting first.
        """
        arg = arg.strip()
        if not arg.isdigit():
            self.notify(
                "Usage: unimport <id>  (see the id column in 'import')",
                severity="warning",
            )
            return
        import_id = int(arg)
        with self.session_factory() as session:
            preview = queries.preview_import_delete(session, import_id)
        if preview is None:
            self.notify(f"No import with id {import_id}.", severity="error")
            return

        self._pending_unimport = preview
        self._prompt_panel = self._panel
        transfers_note = (
            f", breaking {preview.transfers_broken} transfer pairing(s)"
            if preview.transfers_broken
            else ""
        )
        prompt = self.query_one("#prompt", Static)
        # Text(), not markup: the source file name is user data and may hold brackets.
        prompt.update(
            Text.assemble(
                (
                    f"Delete import #{import_id} ({preview.source_file}): "
                    f"{preview.transaction_count} transaction(s){transfers_note}?\n",
                    "bold",
                ),
                ("Type yes to confirm; anything else, or escape, cancels.", "dim"),
            )
        )
        prompt.display = True
        self.query_one("#command", Input).focus()

    def _answer_unimport(self, text: str) -> None:
        pending = self._pending_unimport
        self._cancel_unimport()
        if text.strip().lower() != "yes":
            self.notify("Unimport canceled.")
            return
        with self.session_factory() as session:
            try:
                result = delete_import(session, pending.import_id)
            except UnknownImport as error:
                self.notify(str(error), severity="error", markup=False)
                return
            session.commit()
        self.reload()
        if self._panel == "imports":
            self._show_imports()
        message = (
            f"Deleted import #{result.import_id} ({pending.source_file}): "
            f"{result.transactions_deleted} transaction(s) removed"
        )
        if result.transfers_broken:
            message += f", {result.transfers_broken} transfer pairing(s) broken"
        self.notify(message + ".", markup=False)

    def _cancel_unimport(self) -> None:
        self._pending_unimport = None
        self._prompt_panel = None
        self.query_one("#prompt", Static).display = False

    def _import_candidate(self, candidate: ImportCandidate) -> None:
        """Import the file, first walking through whatever it still needs.

        A file with a built-in reader (e.g. a Wise transfer log) is recognized from its
        own columns rather than a saved layout, so ``candidate.format_name`` is a label,
        not a row in the csv_format table — there is nothing for the setup walkthrough
        to look up or ask about. Import it directly instead; import_csv already routes
        on the file's signature regardless of any format argument.
        """
        if candidate.format_name == importer.WISE_FORMAT_NAME:
            app = _app_module()
            with self.session_factory() as session:
                try:
                    result = app.import_csv(session, candidate.path)
                except app.IMPORT_PROBLEMS as error:
                    self.notify(str(error), severity="error", markup=False)
                    return
            self.reload()
            self._show_imports()
            self.notify(
                f"{candidate.path.name}: {result.inserted} added, "
                f"{result.skipped_duplicates} skipped."
            )
            self._fetch_rates_after_import([result.import_id])
            return
        setup = _Setup(path=candidate.path)
        if candidate.format_name is None:
            # An unseen layout: infer what we can, then ask about the rest.
            fieldnames, rows = read_header_and_rows(candidate.path)
            setup.fieldnames, setup.rows = fieldnames, rows
            default = re.sub(r"[^a-z0-9]+", "_", candidate.path.stem.lower()).strip("_")
            setup.values = formats.infer(default or "layout", fieldnames, rows).values
        else:
            with self.session_factory() as session:
                setup.spec = formats.get_format(session, candidate.format_name)
        self._setup = setup
        self._advance_setup()

    def _advance_setup(self) -> None:
        """Ask the next question, or finish: save the layout and import."""
        setup = self._setup
        if setup is None:
            return

        question = imports_panel.next_setup_question(setup)
        if question is None and setup.spec is None:
            try:
                spec = formats.spec_from_values(setup.values)
            except formats.InvalidFormat as error:
                self.notify(str(error), severity="error", markup=False)
                self._cancel_setup()
                return
            with self.session_factory() as session:
                formats.save_format(session, spec)
                session.commit()
            setup.spec = spec
            self.notify(f"Learned layout {spec.name!r}.")
            question = imports_panel.next_setup_question(setup)

        if question is not None:
            setup.question = question
            self._show_setup_question()
            return

        self._finish_setup()

    def _finish_setup(self) -> None:
        setup = self._setup
        self._setup = None
        app = _app_module()
        with self.session_factory() as session:
            try:
                result = app.import_csv(session, setup.path, account_name=setup.account_name)
            except app.IMPORT_PROBLEMS as error:
                # The walkthrough is already finished and its format saved, so there is
                # nothing to go back to -- report and return to the file list rather
                # than dying on a problem the user can act on.
                self.notify(str(error), severity="error", markup=False)
                self._show_imports()
                return
        self.reload()
        self._show_imports()
        self.notify(
            f"{setup.path.name}: {result.inserted} added, "
            f"{result.skipped_duplicates} skipped."
        )
        self._fetch_rates_after_import([result.import_id])

    def _cancel_setup(self) -> None:
        self._setup = None
        self._show_imports()

    def _show_setup_question(self) -> None:
        table = self.query_one("#setup", DataTable)
        question = self._setup.question
        imports_panel.fill_setup_choices(table, question)
        self._prompt_panel = "setup"
        self._set_panel("setup")
        self.query_one("#prompt", Static).update(imports_panel.setup_prompt_text(question))
        # An empty choices table is just noise, so only show it when there is a list.
        table.display = bool(question.choices)
        if not question.choices:
            self.query_one("#command", Input).focus()

    def _answer_setup(self, text: str) -> None:
        """Apply one answer, then move on to whatever is next."""
        setup = self._setup
        question = setup.question
        answer = text.strip()
        if not answer and question.default:
            answer = question.default
        if question.choices and answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(question.choices):
                answer = str(question.choices[index - 1])
        if not answer and not question.allow_empty:
            self.notify("An answer is needed; escape cancels.", severity="warning")
            return

        setup.asked.add(question.field)
        if question.field == "name":
            setup.values["name"] = answer
        elif question.field == "account_prefix":
            setup.values["account_prefix"] = (
                answer if not answer or answer.endswith(" ") else answer + " "
            )
        elif question.field == "__account":
            setup.account_name = answer
        else:
            setup.values = formats.apply_answers(
                setup.values, {question.field: answer}, setup.fieldnames, setup.rows
            )
        setup.question = None
        self._advance_setup()

    def _show_imports(self) -> None:
        """Browse one directory of the inbox: where to go, what to import, plus history.

        Only the CSVs *directly* here become candidates. Inspecting a whole tree up front
        would mean reading every file under the inbox to draw one screen, and the folder
        rows already say how many are down there.
        """
        listing = list_inbox(self._import_dir, _app_module().TO_IMPORT_DIR)
        self._import_nav = ([listing.parent] if listing.parent is not None else []) + [
            folder.path for folder in listing.folders
        ]
        self._import_folders = list(listing.folders)
        with self.session_factory() as session:
            self._candidates = [inspect_csv(session, path) for path in listing.files]
            self._imports = queries.get_imports(session)
        self._fill_imports()
        self._set_panel("imports")
        if not self._candidates and not listing.folders:
            self.notify(
                f"Nothing to import in {self._import_label()}", severity="warning"
            )

    def _fill_imports(self) -> None:
        imports_panel.fill_imports(
            self.query_one("#imports", DataTable),
            self._import_nav,
            self._import_folders,
            self._candidates,
            self._imports,
        )

    def _import_label(self) -> str:
        """The current directory, named for the status line. See imports_panel.import_label()."""
        return imports_panel.import_label(self._import_dir, _app_module().TO_IMPORT_DIR)

    def _open_import_dir(self, path: Path) -> None:
        self._import_dir = path
        self._show_imports()
        # A fresh directory starts at its first row rather than wherever the cursor
        # happened to be in the directory just left.
        table = self.query_one("#imports", DataTable)
        if table.row_count:
            table.move_cursor(row=0)

    def _do_format(self, arg: str) -> None:
        """Bare ``format`` lists learned layouts; ``format <name> invert on|off`` flips one.

        A positive amount means money leaving the account on some providers' exports and
        money arriving on others; flipping ``invert_amount`` here fixes every future
        import of that layout, without touching anything already imported (undo a bad
        import with ``unimport`` first, then re-import).
        """
        arg = arg.strip()
        if not arg:
            self._notify_formats()
            return
        parts = arg.split()
        if len(parts) < 3 or parts[-2].lower() != "invert" or parts[-1].lower() not in (
            "on",
            "off",
        ):
            self.notify(self.FORMAT_USAGE, severity="warning")
            return
        name = " ".join(parts[:-2])
        invert = parts[-1].lower() == "on"
        with self.session_factory() as session:
            try:
                spec = formats.set_invert_amount(session, name, invert)
            except formats.UnknownFormat as error:
                self.notify(str(error), severity="error", markup=False)
                return
            session.commit()
        state = "on" if spec.invert_amount else "off"
        self.notify(f"{spec.name!r}: invert {state}.", markup=False)

    def _notify_formats(self) -> None:
        with self.session_factory() as session:
            specs = formats.list_formats(session)
        if not specs:
            self.notify("No CSV layouts learned yet. Import a file to learn one.")
            return
        lines = [
            f"{s.name} — {s.amount_style}, invert "
            + ("on" if s.invert_amount else "off")
            for s in specs
        ]
        self.notify("\n".join(lines), title="Formats", markup=False, timeout=8)
