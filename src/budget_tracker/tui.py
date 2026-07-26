"""Full-screen Textual TUI for the budget tracker.

Layout: an accounts/categories sidebar (click a row to filter), a scrollable
transactions table, a totals line, and a command bar at the bottom.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from . import accounts, formats, queries, transfers, vendors
from .db import get_engine, get_sessionmaker, init_db
from .importer import (
    ImportCandidate,
    import_csv,
    inspect_csv,
    read_header_and_rows,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
TO_IMPORT_DIR = _REPO_ROOT / "data" / "to_import"


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


def _fmt_amount(minor: int, decimal_places: int = 2) -> str:
    return f"{minor / (10 ** decimal_places):,.{decimal_places}f}"


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


# Transfers are shown greyed and flagged, because the totals deliberately ignore them —
# a row that looks like ordinary spending but is missing from the figures reads as a bug.
TRANSFER_MARK = "⇄"
TRANSFER_STYLE = "dim italic"


def _amount_cell(minor: int, is_transfer: bool = False) -> Text:
    if is_transfer:
        return Text(_fmt_amount(minor), style=TRANSFER_STYLE, justify="right")
    style = "red" if minor < 0 else "green"
    return Text(_fmt_amount(minor), style=style, justify="right")


def _txn_cell(text: str, width: int, is_transfer: bool) -> Text:
    return Text(_truncate(text, width), style=TRANSFER_STYLE if is_transfer else "")


class BudgetApp(App):
    CSS = """
    #sidebar { width: 36; }
    #accounts, #categories { border: round $accent; height: 1fr; }
    #txns, #rules, #imports, #setup { border: round $accent; height: 1fr; }
    #prompt { height: auto; padding: 1 1 0 1; color: $accent; }
    #status { height: 1; padding: 0 1; color: $text-muted; background: $panel; }
    #command { border: tall $accent; }
    .heading { padding: 0 1; text-style: bold; color: $accent; }
    """

    PANELS = ("txns", "rules", "imports", "setup")

    BINDINGS = [
        ("ctrl+r", "refresh", "Refresh"),
        ("ctrl+l", "clear_filters", "Clear filters"),
        ("ctrl+n", "rename_vendor", "Rename vendor"),
        ("escape", "show_transactions", "Back to transactions"),
        ("ctrl+c", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.engine = get_engine()
        init_db(self.engine)
        self.session_factory = get_sessionmaker(self.engine)
        self.account_filter: Optional[int] = None
        self.vendor_filter: Optional[queries.VendorFilter] = None
        self.category_filter: Optional[int] = None
        self.text_filter: Optional[queries.TextFilter] = None
        self._accounts: List[queries.AccountRow] = []
        self._vendors: List[queries.VendorRow] = []
        self._categories: List[queries.CategoryRow] = []
        # Parallel to the rows in #txns, so a cursor index maps back to a transaction.
        self._txns: List[queries.TxnRow] = []
        self._rules: List[queries.RuleRow] = []
        self._candidates: List[ImportCandidate] = []
        self._panel = "txns"
        self._setup: Optional[_Setup] = None
        self._totals = queries.Totals(count=0, net_minor=0, outflow_minor=0, inflow_minor=0)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Label("Accounts", classes="heading")
                yield ListView(id="accounts")
                yield Label("Vendors", classes="heading")
                yield ListView(id="vendors")
                yield Label("Categories", classes="heading")
                yield ListView(id="categories")
            with Vertical(id="main"):
                yield DataTable(id="txns")
                yield DataTable(id="rules")
                yield DataTable(id="imports")
                yield Static("", id="prompt")
                yield DataTable(id="setup")
                yield Static("", id="status")
        yield Input(
            placeholder="command: import | filter | rules | all | refresh | help | quit",
            id="command",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Budget Tracker"
        table = self.query_one("#txns", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("Date", width=10)
        table.add_column("Description", width=26)
        table.add_column("Vendor", width=18)
        table.add_column("Category", width=12)
        table.add_column("Amount", width=12)
        table.add_column("Account", width=18)

        rules = self.query_one("#rules", DataTable)
        rules.cursor_type = "row"
        rules.zebra_stripes = True
        # Widths are chosen so all three columns fit beside the 36-wide sidebar without
        # the count — the point of the panel — scrolling off the right edge.
        rules.add_column("Pattern", width=28)
        rules.add_column("Display name", width=18)
        rules.add_column("Vendors", width=7)
        rules.display = False  # the transactions table owns the panel by default

        imports = self.query_one("#imports", DataTable)
        imports.cursor_type = "row"
        imports.zebra_stripes = True
        imports.add_column("File", width=34)
        imports.add_column("Rows", width=5)
        imports.add_column("Status", width=15)
        imports.display = False

        setup = self.query_one("#setup", DataTable)
        setup.cursor_type = "row"
        setup.zebra_stripes = True
        setup.add_column("#", width=4)
        setup.add_column("Choice", width=44)
        setup.display = False
        self.query_one("#prompt", Static).display = False

        self.reload()
        self.query_one("#command", Input).focus()

    # ------------------------------------------------------------------ data
    def reload(self) -> None:
        with self.session_factory() as session:
            self._accounts = queries.get_accounts(session)
            self._vendors = queries.get_vendors(session)
            self._categories = queries.get_categories(session)
            self._rules = queries.get_rules(session)
            txns = queries.get_transactions(
                session,
                self.account_filter,
                self.category_filter,
                self.vendor_filter,
                text_filter=self.text_filter,
            )
            totals = queries.get_totals(
                session,
                self.account_filter,
                self.category_filter,
                self.vendor_filter,
                text_filter=self.text_filter,
            )
        self._fill_list("#accounts", [f"{a.name} ({a.count})" for a in self._accounts])
        self._fill_list("#vendors", [f"{v.name} ({v.count})" for v in self._vendors])
        self._fill_list(
            "#categories",
            [f"{c.name} ({c.count})  {_fmt_amount(c.total_minor)}" for c in self._categories],
        )
        self._fill_txns(txns)
        self._fill_rules()
        self._totals = totals
        self._refresh_status()

    def _fill_list(self, selector: str, labels: List[str]) -> None:
        list_view = self.query_one(selector, ListView)
        list_view.clear()
        list_view.append(ListItem(Label("— All —")))
        for label in labels:
            list_view.append(ListItem(Label(label)))

    def _fill_txns(self, txns: List[queries.TxnRow]) -> None:
        table = self.query_one("#txns", DataTable)
        table.clear()
        self._txns = txns
        for txn in txns:
            marked = f"{TRANSFER_MARK} {txn.description}" if txn.is_transfer else txn.description
            table.add_row(
                _txn_cell(txn.posted_date, 10, txn.is_transfer),
                _txn_cell(marked, 26, txn.is_transfer),
                _txn_cell(txn.vendor, 18, txn.is_transfer),
                _txn_cell(txn.category, 12, txn.is_transfer),
                _amount_cell(txn.amount_minor, txn.is_transfer),
                _txn_cell(txn.account, 18, txn.is_transfer),
            )

    def _fill_rules(self) -> None:
        table = self.query_one("#rules", DataTable)
        table.clear()
        for rule in self._rules:
            table.add_row(
                _truncate(rule.pattern, 28),
                _truncate(rule.name, 18),
                Text(str(rule.vendor_count), justify="right"),
            )

    def _fill_imports(self) -> None:
        table = self.query_one("#imports", DataTable)
        table.clear()
        for candidate in self._candidates:
            status = Text(
                _truncate(candidate.status, 15),
                style="" if candidate.ready else "yellow",
            )
            table.add_row(
                _truncate(candidate.path.name, 34),
                Text(str(candidate.row_count), justify="right"),
                status,
            )

    def _refresh_status(self) -> None:
        status = self.query_one("#status", Static)
        if self._panel == "rules":
            count = len(self._rules)
            named = sum(rule.vendor_count for rule in self._rules)
            status.update(
                f"{count} rule{'s' if count != 1 else ''}   "
                f"naming {named} vendors   "
                "escape to return to transactions"
            )
            return
        if self._panel == "setup" and self._setup is not None:
            status.update(
                f"setting up {self._setup.path.name}   escape cancels"
            )
            return
        if self._panel == "imports":
            ready = sum(1 for c in self._candidates if c.ready)
            status.update(
                f"{len(self._candidates)} file(s), {ready} ready   "
                "enter to import   escape to return to transactions"
            )
            return
        self._set_status(self._totals)

    def _set_status(self, totals: queries.Totals) -> None:
        scope = []
        if self.account_filter is not None:
            scope.append("account")
        if self.vendor_filter is not None:
            scope.append("vendor")
        if self.category_filter is not None:
            scope.append("category")
        if self.text_filter is not None:
            scope.append(f'{self.text_filter.field}~"{self.text_filter.text}"')
        scope_label = f" [filtered: {', '.join(scope)}]" if scope else ""
        transfers_label = (
            f"   ({totals.transfer_count} transfers excluded)"
            if totals.transfer_count
            else ""
        )
        self.query_one("#status", Static).update(
            f"{totals.count} txns{scope_label}{transfers_label}   "
            f"net {_fmt_amount(totals.net_minor)}   "
            f"out {_fmt_amount(totals.outflow_minor)}   "
            f"in {_fmt_amount(totals.inflow_minor)}"
        )

    # ---------------------------------------------------------------- events
    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index or 0
        list_id = event.list_view.id
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
        self.reload()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row in the imports panel imports that file."""
        if event.data_table.id == "setup":
            if self._setup is not None and self._setup.question is not None:
                choices = self._setup.question.choices
                if 0 <= event.cursor_row < len(choices):
                    self._answer_setup(str(choices[event.cursor_row]))
            return
        if event.data_table.id != "imports":
            return
        row = event.cursor_row
        if not 0 <= row < len(self._candidates):
            return
        self._import_candidate(self._candidates[row])

    def _import_candidate(self, candidate: ImportCandidate) -> None:
        """Import the file, first walking through whatever it still needs."""
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

    def _next_setup_question(self, setup: _Setup) -> Optional[formats.Question]:
        """The next thing we need from the user, or None when ready to import."""
        if setup.spec is None:
            if "name" not in setup.asked:
                return formats.Question(
                    field="name",
                    prompt="Name for this layout",
                    default=setup.values.get("name"),
                )
            pending = formats.remaining_questions(
                setup.values, setup.rows, setup.fieldnames
            )
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
                prompt=(
                    f"{setup.path.name} does not say which account it is. "
                    "Name the account"
                ),
            )
        return None

    def _advance_setup(self) -> None:
        """Ask the next question, or finish: save the layout and import."""
        setup = self._setup
        if setup is None:
            return

        question = self._next_setup_question(setup)
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
            question = self._next_setup_question(setup)

        if question is not None:
            setup.question = question
            self._show_setup_question()
            return

        self._finish_setup()

    def _finish_setup(self) -> None:
        setup = self._setup
        self._setup = None
        with self.session_factory() as session:
            result = import_csv(session, setup.path, account_name=setup.account_name)
        self.reload()
        self._show_imports()
        self.notify(
            f"{setup.path.name}: {result.inserted} added, "
            f"{result.skipped_duplicates} skipped."
        )

    def _cancel_setup(self) -> None:
        self._setup = None
        self._show_imports()

    def _show_setup_question(self) -> None:
        table = self.query_one("#setup", DataTable)
        table.clear()
        question = self._setup.question
        for index, choice in enumerate(question.choices, start=1):
            table.add_row(Text(str(index), justify="right"), _truncate(str(choice), 44))
        self._set_panel("setup")

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
        self.query_one("#prompt", Static).update(
            Text.assemble((question.prompt + "\n", "bold"), (hint, "dim"))
        )
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

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value
        event.input.value = ""
        if self._setup is not None and self._setup.question is not None:
            self._answer_setup(text)
            return
        self._run_command(text.strip())

    def _run_command(self, command: str) -> None:
        if not command:
            return
        parts = command.split(maxsplit=1)
        name = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if name in {"quit", "q", "exit"}:
            self.exit()
        elif name == "refresh":
            self.reload()
            self.notify("Refreshed.")
        elif name in {"all", "clear"}:
            self.action_clear_filters()
        elif name == "import":
            self._do_import(arg)
        elif name == "rename":
            self._do_rename(arg)
        elif name == "rule":
            self._do_rule(arg)
        elif name == "rules":
            self._show_rules()
        elif name == "transfers":
            self._do_transfers(arg)
        elif name == "merge":
            self._do_merge(arg)
        elif name == "filter":
            self._do_filter(arg)
        elif name == "help":
            self.notify(
                "import — browse data/to_import; enter imports the selected file\n"
                "import all | import <path> — import without browsing\n"
                "rename <raw vendor> = <display name> — override / aggregate a vendor\n"
                "rule <pattern> = <display name> — rename every matching vendor,\n"
                "  now and on future imports (e.g. rule Kindle Svcs* = Kindle)\n"
                "rules — list the rules you have defined (escape returns)\n"
                "transfers [reset] — pair up movements between your own accounts\n"
                "merge <account> = <account> — fold one account into another\n"
                "filter <text> — search description, vendor, and raw name\n"
                "filter vendor:<text> — search one field (description/vendor/raw)\n"
                "filter — clear the text filter\n"
                "all — clear filters   refresh — reload   quit — exit\n"
                "Click an account/vendor/category to filter.\n"
                "ctrl+n — prefill rename for the selected transaction's vendor,\n"
                "  or for the selected vendor in the sidebar.",
                title="Commands",
                timeout=8,
            )
        else:
            self.notify(f"Unknown command: {name}", severity="warning")

    def _do_import(self, arg: str) -> None:
        if not arg:
            # Bare "import" browses the inbox; enter on a row imports that file.
            self._show_imports()
            return
        if arg == "all":
            paths = sorted(TO_IMPORT_DIR.glob("*.csv"))
            if not paths:
                self.notify(f"No CSVs in {TO_IMPORT_DIR}", severity="warning")
                return
        else:
            path = Path(arg).expanduser()
            if not path.is_file():
                self.notify(f"File not found: {path}", severity="error")
                return
            paths = [path]

        added = skipped = 0
        imported = 0
        problems: List[str] = []
        with self.session_factory() as session:
            for path in paths:
                try:
                    result = import_csv(session, path)
                except (formats.AccountRequired, formats.UnknownFormat) as error:
                    problems.append(f"{path.name}: {error}")
                    continue
                imported += 1
                added += result.inserted
                skipped += result.skipped_duplicates
        self.reload()
        self.notify(
            f"Imported {imported} file(s): {added} added, {skipped} skipped."
        )
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
        if not ok:
            self.notify(f"No vendor named {raw!r}.", severity="error")
            return
        self.reload()
        self.notify(f"{raw!r} → {display!r}")

    def _set_panel(self, panel: str) -> None:
        """Show one of the main-view panels; escape always returns to transactions."""
        self._panel = panel
        for name in self.PANELS:
            self.query_one(f"#{name}", DataTable).display = name == panel
        self.query_one("#prompt", Static).display = panel == "setup"
        if panel == "txns":
            self.query_one("#command", Input).focus()
        else:
            self.query_one(f"#{panel}", DataTable).focus()
        self._refresh_status()

    def _show_rules(self) -> None:
        self.reload()
        self._set_panel("rules")
        if not self._rules:
            self.notify(
                "No vendor rules yet. Add one with:  rule <pattern> = <display name>"
            )

    def _show_imports(self) -> None:
        """List the files in the inbox, with whatever blocks each one."""
        paths = sorted(TO_IMPORT_DIR.glob("*.csv"))
        with self.session_factory() as session:
            self._candidates = [inspect_csv(session, path) for path in paths]
        self._fill_imports()
        self._set_panel("imports")
        if not self._candidates:
            self.notify(f"No CSVs in {TO_IMPORT_DIR}", severity="warning")

    def _do_filter(self, arg: str) -> None:
        """`filter text` searches everything; `filter vendor:text` narrows the field."""
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

    def _do_merge(self, arg: str) -> None:
        if "=" not in arg:
            self.notify("Usage: merge <source account> = <target account>", severity="warning")
            return
        source, target = (part.strip() for part in arg.split("=", 1))
        if not source or not target:
            self.notify("Usage: merge <source account> = <target account>", severity="warning")
            return
        with self.session_factory() as session:
            try:
                result = accounts.merge_accounts(session, source, target)
            except accounts.AccountError as error:
                self.notify(str(error), severity="error", markup=False)
                return
            session.commit()
        self.reload()
        message = (
            f"Merged {result.source!r} into {result.target!r}: "
            f"{result.moved_transactions} transactions moved."
        )
        if result.unpaired_transfers:
            message += f"\n{result.unpaired_transfers} same-account transfer legs un-paired."
        self.notify(message, markup=False, timeout=8)

    def _do_transfers(self, arg: str) -> None:
        with self.session_factory() as session:
            if arg.strip() in {"reset", "clear"}:
                reset = transfers.clear_transfers(session)
                session.commit()
                message = f"Un-paired {reset} transaction(s)."
            else:
                pairs = transfers.detect_transfers(session)
                session.commit()
                message = f"Found {pairs} new transfer pair(s)."
        self.reload()
        self.notify(message)

    def _do_rule(self, arg: str) -> None:
        if not arg:
            self._show_rules()
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
            session.commit()
        self.reload()
        self.notify(f"{pattern!r} → {display!r} ({changed} vendors updated)")

    # --------------------------------------------------------------- actions
    def _selected_vendor(self) -> Optional[queries.VendorRow]:
        """The vendor ctrl+n targets: the active filter, else the highlighted row."""
        if self.vendor_filter is not None:
            kind, vendor_id = self.vendor_filter
            for vendor in self._vendors:
                if (vendor.kind, vendor.id) == (kind, vendor_id):
                    return vendor
            return None
        # Index 0 is the "— All —" row, so the list is offset by one.
        index = self.query_one("#vendors", ListView).index or 0
        if not 1 <= index <= len(self._vendors):
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

    def action_rename_vendor(self) -> None:
        # In the transaction table, target the selected transaction's vendor. Rows carry
        # the raw merchant string, so this works even for already-grouped vendors.
        txn = self._cursor_txn()
        if txn is not None:
            if not txn.vendor_raw:
                self.notify("That transaction has no vendor.", severity="warning")
                return
            self._prefill_command(f"rename {txn.vendor_raw} = ")
            return

        vendor = self._selected_vendor()
        if vendor is None:
            self.notify("Select a vendor in the sidebar first.", severity="warning")
            return
        if vendor.kind != "raw":
            # set_override() matches on the raw vendor string, which the sidebar no
            # longer shows once a group exists, so we can only prefill the verb.
            self.notify(
                f"{vendor.name!r} is an override group — rename a raw vendor instead.",
                severity="warning",
            )
            self._prefill_command("rename ")
            return
        self._prefill_command(f"rename {vendor.name} = ")

    def action_show_transactions(self) -> None:
        if self._setup is not None:
            self.notify(f"Setup for {self._setup.path.name} cancelled.")
            self._cancel_setup()
            return
        if self._panel != "txns":
            self._set_panel("txns")

    def action_refresh(self) -> None:
        self.reload()

    def action_clear_filters(self) -> None:
        self.account_filter = None
        self.vendor_filter = None
        self.category_filter = None
        self.text_filter = None
        self.reload()
        self.notify("Filters cleared.")


def run() -> None:
    BudgetApp().run()
