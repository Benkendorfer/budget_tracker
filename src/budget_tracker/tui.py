"""Full-screen Textual TUI for the budget tracker.

Layout: an accounts/categories sidebar (click a row to filter), a scrollable
transactions table, a totals line, and a command bar at the bottom.
"""

from __future__ import annotations

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

from . import queries
from .db import get_engine, get_sessionmaker, init_db
from .importer import import_csv

_REPO_ROOT = Path(__file__).resolve().parents[2]
TO_IMPORT_DIR = _REPO_ROOT / "data" / "to_import"


def _fmt_amount(minor: int, decimal_places: int = 2) -> str:
    return f"{minor / (10 ** decimal_places):,.{decimal_places}f}"


def _amount_cell(minor: int) -> Text:
    style = "red" if minor < 0 else "green"
    return Text(_fmt_amount(minor), style=style, justify="right")


class BudgetApp(App):
    CSS = """
    #sidebar { width: 36; }
    #accounts, #categories { border: round $accent; height: 1fr; }
    #txns { border: round $accent; height: 1fr; }
    #status { height: 1; padding: 0 1; color: $text-muted; background: $panel; }
    #command { border: tall $accent; }
    .heading { padding: 0 1; text-style: bold; color: $accent; }
    """

    BINDINGS = [
        ("ctrl+r", "refresh", "Refresh"),
        ("ctrl+l", "clear_filters", "Clear filters"),
        ("ctrl+c", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.engine = get_engine()
        init_db(self.engine)
        self.session_factory = get_sessionmaker(self.engine)
        self.account_filter: Optional[int] = None
        self.category_filter: Optional[int] = None
        self._accounts: List[queries.AccountRow] = []
        self._categories: List[queries.CategoryRow] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Label("Accounts", classes="heading")
                yield ListView(id="accounts")
                yield Label("Categories", classes="heading")
                yield ListView(id="categories")
            with Vertical(id="main"):
                yield DataTable(id="txns")
                yield Static("", id="status")
        yield Input(
            placeholder="command: import [path] | all | refresh | help | quit",
            id="command",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Budget Tracker"
        table = self.query_one("#txns", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("Date", width=10)
        table.add_column("Description", width=40)
        table.add_column("Category", width=16)
        table.add_column("Amount", width=12)
        self.reload()
        self.query_one("#command", Input).focus()

    # ------------------------------------------------------------------ data
    def reload(self) -> None:
        with self.session_factory() as session:
            self._accounts = queries.get_accounts(session)
            self._categories = queries.get_categories(session)
            txns = queries.get_transactions(
                session, self.account_filter, self.category_filter
            )
            totals = queries.get_totals(
                session, self.account_filter, self.category_filter
            )
        self._fill_list("#accounts", [f"{a.name} ({a.count})" for a in self._accounts])
        self._fill_list(
            "#categories",
            [f"{c.name} ({c.count})  {_fmt_amount(c.total_minor)}" for c in self._categories],
        )
        self._fill_txns(txns)
        self._set_status(totals)

    def _fill_list(self, selector: str, labels: List[str]) -> None:
        list_view = self.query_one(selector, ListView)
        list_view.clear()
        list_view.append(ListItem(Label("— All —")))
        for label in labels:
            list_view.append(ListItem(Label(label)))

    def _fill_txns(self, txns: List[queries.TxnRow]) -> None:
        table = self.query_one("#txns", DataTable)
        table.clear()
        for txn in txns:
            description = txn.description if len(txn.description) <= 40 else txn.description[:37] + "…"
            table.add_row(
                txn.posted_date,
                description,
                txn.category,
                _amount_cell(txn.amount_minor),
            )

    def _set_status(self, totals: queries.Totals) -> None:
        scope = []
        if self.account_filter is not None:
            scope.append("account")
        if self.category_filter is not None:
            scope.append("category")
        scope_label = f" [filtered: {', '.join(scope)}]" if scope else ""
        self.query_one("#status", Static).update(
            f"{totals.count} txns{scope_label}   "
            f"net {_fmt_amount(totals.net_minor)}   "
            f"out {_fmt_amount(totals.outflow_minor)}   "
            f"in {_fmt_amount(totals.inflow_minor)}"
        )

    # ---------------------------------------------------------------- events
    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index or 0
        if event.list_view.id == "accounts":
            self.account_filter = None if index == 0 else self._accounts[index - 1].id
        elif event.list_view.id == "categories":
            self.category_filter = None if index == 0 else self._categories[index - 1].id
        self.reload()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        command = event.value.strip()
        event.input.value = ""
        self._run_command(command)

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
        elif name == "help":
            self.notify(
                "import [path] — import a CSV (or all in data/to_import)\n"
                "all — clear filters   refresh — reload   quit — exit\n"
                "Click an account/category to filter.",
                title="Commands",
                timeout=8,
            )
        else:
            self.notify(f"Unknown command: {name}", severity="warning")

    def _do_import(self, arg: str) -> None:
        if arg:
            path = Path(arg).expanduser()
            if not path.is_file():
                self.notify(f"File not found: {path}", severity="error")
                return
            paths = [path]
        else:
            paths = sorted(TO_IMPORT_DIR.glob("*.csv"))
            if not paths:
                self.notify(f"No CSVs in {TO_IMPORT_DIR}", severity="warning")
                return

        added = skipped = 0
        with self.session_factory() as session:
            for path in paths:
                result = import_csv(session, path)
                added += result.inserted
                skipped += result.skipped_duplicates
        self.reload()
        self.notify(
            f"Imported {len(paths)} file(s): {added} added, {skipped} skipped."
        )

    # --------------------------------------------------------------- actions
    def action_refresh(self) -> None:
        self.reload()

    def action_clear_filters(self) -> None:
        self.account_filter = None
        self.category_filter = None
        self.reload()
        self.notify("Filters cleared.")


def run() -> None:
    BudgetApp().run()
