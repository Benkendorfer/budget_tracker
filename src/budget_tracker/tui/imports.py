"""The import browser: folder navigation, candidates, and the setup walkthrough.

``BudgetApp`` still owns the imperative flow — the walkthrough talks to a session,
notifies, and moves between panels — but the pure pieces (what the next question is,
how a directory's rows are rendered) live here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from rich.text import Text
from textual.widgets import DataTable

from .. import formats, queries
from ..importer import ImportCandidate, InboxFolder
from .formatting import FOLDER_MARK, TRANSFER_STYLE, _truncate


@dataclass
class _Setup:
    """State for the in-app walkthrough that teaches the app a new CSV layout.

    Questions come from :mod:`.formats`, the same source the CLI uses, so both routes
    ask exactly the same things in the same order.
    """

    path: Path
    fieldnames: List[str] = field(default_factory=list)
    rows: List[dict] = field(default_factory=list)
    values: dict = field(default_factory=dict)
    asked: set = field(default_factory=set)
    spec: Optional[formats.FormatSpec] = None
    account_name: Optional[str] = None
    question: Optional[formats.Question] = None


def fill_imports(
    table: DataTable,
    import_nav: List[Path],
    import_folders: List[InboxFolder],
    candidates: List[ImportCandidate],
    imports: List[queries.ImportRow],
) -> None:
    table.clear()
    # Navigation first, in the order _import_nav records: ".." (when there is one),
    # then the sub-directories. Enter on any of them moves the browser rather than
    # importing anything.
    if import_nav and len(import_nav) > len(import_folders):
        table.add_row(
            Text(f"{FOLDER_MARK} ..", style="bold"),
            "",
            Text("up", style=TRANSFER_STYLE),
            "",
        )
    for folder in import_folders:
        table.add_row(
            Text(f"{FOLDER_MARK} {_truncate(folder.name, 32)}", style="bold"),
            Text(str(folder.csv_count), justify="right"),
            Text("folder", style=TRANSFER_STYLE),
            "",
        )
    for candidate in candidates:
        status = Text(
            _truncate(candidate.status, 15),
            style="" if candidate.ready else "yellow",
        )
        table.add_row(
            _truncate(candidate.path.name, 34),
            Text(str(candidate.row_count), justify="right"),
            status,
            "",  # not imported yet, so no id
        )
    # Already-imported files, dimmed: not actionable by enter here (that only
    # imports a candidate), but this is where an `unimport <id>` id comes from.
    for imp in imports:
        table.add_row(
            Text(_truncate(imp.source_file, 34), style=TRANSFER_STYLE),
            Text(str(imp.transaction_count), justify="right", style=TRANSFER_STYLE),
            Text("imported", style=TRANSFER_STYLE),
            Text(str(imp.id), justify="right", style=TRANSFER_STYLE),
        )


def import_label(import_dir: Path, to_import_dir: Path) -> str:
    """The current directory, relative to the inbox, named rather than ``.``.

    At the top that is the inbox folder's own name: "." is technically the relative
    path but reads as an error in a status line, and the point of the label is to say
    where you are in words you would recognise.
    """
    try:
        relative = import_dir.relative_to(to_import_dir)
    except ValueError:
        return str(import_dir)
    return to_import_dir.name if relative == Path(".") else str(relative)


def next_setup_question(setup: _Setup) -> Optional[formats.Question]:
    """The next thing we need from the user, or None when ready to import."""
    if setup.spec is None:
        if "name" not in setup.asked:
            return formats.Question(
                field="name",
                prompt="Name for this layout",
                default=setup.values.get("name"),
            )
        pending = formats.remaining_questions(setup.values, setup.rows, setup.fieldnames)
        if pending:
            return pending[0]
        if setup.values.get("account_column") and "account_prefix" not in setup.asked:
            column = setup.values["account_column"]
            sample = next(
                ((r.get(column) or "").strip() for r in setup.rows if r.get(column)),
                "1234",
            )
            return formats.Question(
                field="account_prefix",
                prompt=(
                    f"Accounts are named after {column!r}, e.g. {sample!r}. "
                    "Prefix for those names? (blank for none)"
                ),
                allow_empty=True,
            )
        return None
    if setup.spec.needs_account and setup.account_name is None:
        return formats.Question(
            field="__account",
            prompt=(f"{setup.path.name} does not say which account it is. Name the account"),
        )
    return None


def fill_setup_choices(table: DataTable, question: formats.Question) -> None:
    table.clear()
    for index, choice in enumerate(question.choices, start=1):
        table.add_row(Text(str(index), justify="right"), _truncate(str(choice), 44))


def setup_prompt_text(question: formats.Question) -> Text:
    hint = (
        "Enter on a row, or type the number below."
        if question.choices
        else "Type your answer in the command bar below."
    )
    if question.default:
        hint += f"  Blank accepts {question.default!r}."
    elif question.allow_empty:
        hint += "  Blank is allowed."
    # Text(), not markup: prompts quote column names that may contain brackets.
    return Text.assemble((question.prompt + "\n", "bold"), (hint, "dim"))
