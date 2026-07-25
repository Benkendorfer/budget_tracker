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

from . import queries, vendors
from .db import get_engine, get_sessionmaker, init_db
from .importer import import_csv

_REPO_ROOT = Path(__file__).resolve().parents[2]
TO_IMPORT_DIR = _REPO_ROOT / "data" / "to_import"


def _fmt_amount(minor: int, decimal_places: int = 2) -> str:
    return f"{minor / (10 ** decimal_places):,.{decimal_places}f}"


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _amount_cell(minor: int) -> Text:
    style = "red" if minor < 0 else "green"
    return Text(_fmt_amount(minor), style=style, justify="right")


class BudgetApp(App):
    CSS = """
    #sidebar { width: 36; }
    #accounts, #categories { border: round $accent; height: 1fr; }
    #txns, #rules { border: round $accent; height: 1fr; }
    #status { height: 1; padding: 0 1; color: $text-muted; background: $panel; }
    #command { border: tall $accent; }
    .heading { padding: 0 1; text-style: bold; color: $accent; }
    """

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
        self._accounts: List[queries.AccountRow] = []
        self._vendors: List[queries.VendorRow] = []
        self._categories: List[queries.CategoryRow] = []
        # Parallel to the rows in #txns, so a cursor index maps back to a transaction.
        self._txns: List[queries.TxnRow] = []
        self._rules: List[queries.RuleRow] = []
        self._rules_visible = False
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
        table.add_column("Description", width=32)
        table.add_column("Vendor", width=20)
        table.add_column("Category", width=14)
        table.add_column("Amount", width=12)

        rules = self.query_one("#rules", DataTable)
        rules.cursor_type = "row"
        rules.zebra_stripes = True
        # Widths are chosen so all three columns fit beside the 36-wide sidebar without
        # the count — the point of the panel — scrolling off the right edge.
        rules.add_column("Pattern", width=28)
        rules.add_column("Display name", width=18)
        rules.add_column("Vendors", width=7)
        rules.display = False  # the transactions table owns the panel by default

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
            )
            totals = queries.get_totals(
                session,
                self.account_filter,
                self.category_filter,
                self.vendor_filter,
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
            table.add_row(
                txn.posted_date,
                _truncate(txn.description, 32),
                _truncate(txn.vendor, 20),
                _truncate(txn.category, 14),
                _amount_cell(txn.amount_minor),
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

    def _refresh_status(self) -> None:
        if self._rules_visible:
            count = len(self._rules)
            named = sum(rule.vendor_count for rule in self._rules)
            self.query_one("#status", Static).update(
                f"{count} rule{'s' if count != 1 else ''}   "
                f"naming {named} vendors   "
                "escape to return to transactions"
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
        elif name == "rename":
            self._do_rename(arg)
        elif name == "rule":
            self._do_rule(arg)
        elif name == "rules":
            self._show_rules()
        elif name == "help":
            self.notify(
                "import [path] — import a CSV (or all in data/to_import)\n"
                "rename <raw vendor> = <display name> — override / aggregate a vendor\n"
                "rule <pattern> = <display name> — rename every matching vendor,\n"
                "  now and on future imports (e.g. rule Kindle Svcs* = Kindle)\n"
                "rules — list the rules you have defined (escape returns)\n"
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

    def _set_rules_visible(self, visible: bool) -> None:
        """Swap the main panel between the transactions table and the rules table."""
        self._rules_visible = visible
        self.query_one("#txns", DataTable).display = not visible
        self.query_one("#rules", DataTable).display = visible
        if visible:
            self.query_one("#rules", DataTable).focus()
        else:
            self.query_one("#command", Input).focus()
        self._refresh_status()

    def _show_rules(self) -> None:
        self.reload()
        self._set_rules_visible(True)
        if not self._rules:
            self.notify(
                "No vendor rules yet. Add one with:  rule <pattern> = <display name>"
            )

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
        if self._rules_visible:
            self._set_rules_visible(False)

    def action_refresh(self) -> None:
        self.reload()

    def action_clear_filters(self) -> None:
        self.account_filter = None
        self.vendor_filter = None
        self.category_filter = None
        self.reload()
        self.notify("Filters cleared.")


def run() -> None:
    BudgetApp().run()
