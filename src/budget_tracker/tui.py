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
from textual.binding import Binding
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

from . import accounts, categories, formats, queries, stats, transfers, vendors
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

# The last row of the period picker, and the format its answer has to take.
CUSTOM_PERIOD = "Custom…"
RANGE_EXAMPLE = "2025-01-01..2025-06-30"


def _amount_cell(minor: int, is_transfer: bool = False) -> Text:
    if is_transfer:
        return Text(_fmt_amount(minor), style=TRANSFER_STYLE, justify="right")
    style = "red" if minor < 0 else "green"
    return Text(_fmt_amount(minor), style=style, justify="right")


def _txn_cell(text: str, width: int, is_transfer: bool) -> Text:
    return Text(_truncate(text, width), style=TRANSFER_STYLE if is_transfer else "")


def _range_label(date_range: queries.DateRange) -> str:
    """``2025-07-01→12-31``, dropping a repeated year.

    The status line has 90 columns for a count, a scope list, and three money figures, so
    five columns of year that the start date has already given are not worth spending.
    """
    start, end = date_range
    tail = end.isoformat()[5:] if end.year == start.year else end.isoformat()
    return f"{start}→{tail}"


class BudgetApp(App):
    CSS = """
    #sidebar { width: 36; }
    #accounts, #categories { border: round $accent; height: 1fr; }
    #txns, #rules, #imports, #setup, #periods { border: round $accent; height: 1fr; }
    #stats { height: 1fr; }
    #stats_table { border: round $accent; height: 1fr; }
    #prompt { height: auto; padding: 1 1 0 1; color: $accent; }
    #status { height: 1; padding: 0 1; color: $text-muted; background: $panel; }
    #command { border: tall $accent; }
    .heading { padding: 0 1; text-style: bold; color: $accent; }
    """

    PANELS = ("txns", "rules", "imports", "setup", "periods", "stats")
    # Panels whose widget is not itself focusable name the child that takes focus.
    PANEL_FOCUS = {"stats": "#stats_table"}

    BINDINGS = [
        ("ctrl+r", "refresh", "Refresh"),
        ("ctrl+l", "clear_filters", "Clear filters"),
        ("ctrl+n", "rename_vendor", "Rename vendor"),
        ("ctrl+t", "categorize_vendor", "Categorise vendor"),
        ("escape", "show_transactions", "Back to transactions"),
        # DataTable binds left/right itself (cursor movement between cells), which would
        # otherwise eat these before an ordinary App binding ever saw them. priority=True
        # checks the App first; check_action() below opts out — returning False, not just
        # doing nothing — everywhere but the one row cursor_type="row" already leaves
        # left/right without a visible job of their own, so falling through there is safe.
        Binding("right", "drill_down", "Drill down", show=True, priority=True),
        Binding("left", "drill_up", "Back to stats", show=True, priority=True),
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
        # Only the statistics drill-down sets this: the transactions it opens have to be
        # the window's, not all of history, or they will not add up to the row clicked.
        self.date_filter: Optional[queries.DateRange] = None
        self._accounts: List[queries.AccountRow] = []
        self._vendors: List[queries.VendorRow] = []
        self._categories: List[queries.CategoryRow] = []
        # Parallel to the rows in #txns, so a cursor index maps back to a transaction.
        self._txns: List[queries.TxnRow] = []
        self._rules: List[queries.RuleRow] = []
        self._category_rules: List[queries.CategoryRuleRow] = []
        self._candidates: List[ImportCandidate] = []
        self._panel = "txns"
        self._setup: Optional[_Setup] = None
        self._totals = queries.Totals(count=0, net_minor=0, outflow_minor=0, inflow_minor=0)
        # The statistics window survives panel switches, so reload() can re-scope it.
        self.window: Optional[stats.Window] = None
        self._report: Optional[stats.Report] = None
        self._range_pending = False  # awaiting a typed date range for the picker
        self._prompt_panel: Optional[str] = None  # which panel the #prompt belongs to
        # True only while the transactions panel is showing exactly what a statistics
        # drill-down put there — the left arrow's "back" is only meaningful then.
        # Anything that changes the view out from under it (a new filter, ctrl+l,
        # escape, opening another panel) has to clear it, or a stale flag could send a
        # later, unrelated left-arrow press somewhere the user did not ask for.
        self._drilled_from_stats = False
        # What the drill-down overwrote, so going back restores it rather than
        # unconditionally blanking a filter the user had set on purpose.
        self._pre_drill_category_filter: Optional[int] = None
        self._pre_drill_date_filter: Optional[queries.DateRange] = None
        self._drill_source_row: Optional[int] = None  # stats_table row to land back on

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
                yield DataTable(id="periods")
                with Vertical(id="stats"):
                    yield DataTable(id="stats_table")
                    # Seam for the charts to come: a bar chart of spending per bucket and
                    # a category pie go here, under the table, fed by
                    # stats.spending_series() and CategoryStat.share.
                yield Static("", id="status")
        yield Input(
            placeholder=(
                "command: import | filter | categorize | category | stats | rules | "
                "all | refresh | help | quit"
            ),
            id="command",
        )
        yield Footer()

    def check_action(self, action: str, parameters: tuple) -> Optional[bool]:
        """Gate the priority left/right bindings so they only act where they mean something.

        Returning ``False`` (not just a no-op action body) matters: it is what makes
        ``_check_bindings`` fall through to the focused ``DataTable``'s own binding
        instead of swallowing the key everywhere, and it is also what hides the footer
        hint outside the panel it applies to.
        """
        if action == "drill_down":
            return self._panel == "stats"
        if action == "drill_up":
            return self._drilled_from_stats
        return True

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
        # Vendor and category rules share the panel, so a Kind column says which is which
        # and Value covers both a display name and a category. 9 + 26 + 18 + 7 plus two
        # cells of padding each is 68 of the ~92 the main panel has beside the 36-wide
        # sidebar, so the count — the point of the panel — never scrolls off the edge.
        rules.add_column("Kind", width=9)
        rules.add_column("Pattern", width=26)
        rules.add_column("Value", width=18)
        rules.add_column("Count", width=7)
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

        periods = self.query_one("#periods", DataTable)
        periods.cursor_type = "row"
        periods.zebra_stripes = True
        periods.add_column("Period", width=10)
        periods.add_column("Range", width=24)
        periods.display = False

        stats_table = self.query_one("#stats_table", DataTable)
        stats_table.cursor_type = "row"
        stats_table.zebra_stripes = True
        # 26 + 5 + 12 + 12 + 7 + 8 plus two cells of padding each: 82 of the ~92 the main
        # panel has beside the 36-wide sidebar, so neither share column is pushed
        # off-screen (see test_stats_table_fits_the_main_panel). % parent needs width 8,
        # not 7 like % spend, or its own 8-character header ("% parent") clips.
        stats_table.add_column("Category", width=26)
        stats_table.add_column("Txns", width=5)
        stats_table.add_column("Total", width=12)
        stats_table.add_column("Avg/month", width=12)
        # Named for what it is a share *of*: income rows sit in the same table, and a
        # "Share" beside a positive total invites reading it as a share of that.
        stats_table.add_column("% spend", width=7)
        # This row's outflow as a fraction of its *parent's* rolled-up outflow — blank at
        # depth 0, where it would just repeat "% spend" (see stats.CategoryStat.parent_share).
        stats_table.add_column("% parent", width=8)
        self.query_one("#stats", Vertical).display = False
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
            self._category_rules = queries.get_category_rules(session)
            txns = queries.get_transactions(
                session,
                self.account_filter,
                self.category_filter,
                self.vendor_filter,
                text_filter=self.text_filter,
                date_range=self.date_filter,
            )
            totals = queries.get_totals(
                session,
                self.account_filter,
                self.category_filter,
                self.vendor_filter,
                text_filter=self.text_filter,
                date_range=self.date_filter,
            )
        self._fill_list("#accounts", [f"{a.name} ({a.count})" for a in self._accounts])
        self._fill_list("#vendors", [f"{v.name} ({v.count})" for v in self._vendors])
        self._fill_list(
            "#categories",
            [
                f"{'  ' * c.depth}{c.name} ({c.count})  {_fmt_amount(c.total_minor)}"
                for c in self._categories
            ],
        )
        self._fill_txns(txns)
        self._fill_rules()
        self._totals = totals
        # Statistics are scoped by exactly the filters above, so an open panel has to be
        # recomputed whenever they change.
        if self._panel == "stats" and self.window is not None:
            self._build_report()
            self._fill_stats()
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
                "vendor",
                _truncate(rule.pattern, 26),
                _truncate(rule.name, 18),
                Text(str(rule.vendor_count), justify="right"),
            )
        for rule in self._category_rules:
            table.add_row(
                "category",
                _truncate(rule.pattern, 26),
                _truncate(rule.category, 18),
                Text(str(rule.txn_count), justify="right"),
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

    def _build_report(self) -> None:
        with self.session_factory() as session:
            self._report = stats.build_report(
                session,
                self.window,
                self.account_filter,
                self.category_filter,
                self.vendor_filter,
                text_filter=self.text_filter,
            )

    def _fill_stats(self) -> None:
        table = self.query_one("#stats_table", DataTable)
        table.clear()
        if self._report is None:
            return
        for stat in self._report.categories:
            # Blank at depth 0: parent_share is identical to share there by construction
            # (see stats.CategoryStat.parent_share), so printing it twice is noise.
            parent_pct = (
                "" if stat.depth == 0 else f"{abs(stat.parent_share) * 100:.1f}%"
            )
            table.add_row(
                _truncate("  " * stat.depth + stat.name, 26),
                Text(str(stat.count), justify="right"),
                _amount_cell(stat.total_minor),
                _amount_cell(stat.avg_month_minor),
                # Shares are a fraction of a negative outflow, so a category that spent
                # nothing comes out as -0.0; abs() keeps that off the screen.
                Text(f"{abs(stat.share) * 100:.1f}%", justify="right"),
                Text(parent_pct, justify="right"),
            )

    def _fill_periods(self) -> None:
        table = self.query_one("#periods", DataTable)
        table.clear()
        # Spelling out the dates each preset resolves to saves the user working out what
        # "3 months" means when their imports are a month stale.
        for key, label, _months in stats.PRESETS:
            window = stats.resolve(key)
            table.add_row(label, f"{window.start} → {window.end}")
        table.add_row(CUSTOM_PERIOD, RANGE_EXAMPLE)

    def _stats_status(self) -> str:
        """One line: the main panel gives the status 92 columns, so every field is terse.

        No "escape returns" hint here, unlike the other panels — the footer already spells
        that key out, and against a real year of five-figure totals the sentence ran the
        line to exactly the panel width, one digit away from truncating the money. The
        right-arrow drill-down hint lives in the footer for the same reason: this line has
        no slack left to spend on it (see test_stats_status_line_fits_the_main_panel).
        """
        report = self._report
        window = report.window
        # A custom window's label is its own date range, which follows anyway.
        label = "custom" if window.key == "custom" else window.label
        # The money figures leave transfers out, so say how many vanished — silently
        # dropping a payment between your own accounts reads as missing spending.
        excluded = (
            f"{TRANSFER_MARK} {report.transfer_count} " if report.transfer_count else ""
        )
        return (
            f"{label} {window.start}→{window.end} "
            f"{report.count} txns "
            f"{excluded}"
            f"out {_fmt_amount(report.outflow_minor)} "
            f"in {_fmt_amount(report.inflow_minor)} "
            f"/mo {_fmt_amount(report.avg_month_outflow_minor)}"
        )

    def _refresh_status(self) -> None:
        status = self.query_one("#status", Static)
        if self._panel == "stats" and self._report is not None:
            status.update(self._stats_status())
            return
        if self._panel == "periods":
            status.update(
                "choose a period   enter selects   "
                "escape to return to transactions"
            )
            return
        if self._panel == "rules":
            count = len(self._rules) + len(self._category_rules)
            named = sum(rule.vendor_count for rule in self._rules)
            owned = sum(rule.txn_count for rule in self._category_rules)
            status.update(
                f"{count} rule{'s' if count != 1 else ''}   "
                f"{named} vendors named   "
                f"{owned} txns categorised   "
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
        # A drilled-down view's "back to stats" hint lives in the footer, not here: this
        # line already lands on the panel's 92-column budget with a year window and
        # five-figure amounts (test_drill_down_status_line_fits_the_main_panel), before
        # spending anything on a hint.
        scope = []
        if self.account_filter is not None:
            scope.append("account")
        if self.vendor_filter is not None:
            scope.append("vendor")
        if self.category_filter is not None:
            scope.append("category")
        if self.date_filter is not None:
            # Spelled out rather than labelled "date": the drill-down from a statistics
            # row is the only thing that sets it, and the user needs to see which window
            # they landed in to reconcile the numbers they just clicked.
            scope.append(_range_label(self.date_filter))
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
        # A sidebar filter is a new view; the flag it might invalidate is checked in
        # _set_drilled_from_stats() (no-op if it was already clear).
        self._set_drilled_from_stats(False)
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
        if event.data_table.id == "stats_table":
            self._drill_into_category(event.cursor_row)
            return
        if event.data_table.id == "setup":
            if self._setup is not None and self._setup.question is not None:
                choices = self._setup.question.choices
                if 0 <= event.cursor_row < len(choices):
                    self._answer_setup(str(choices[event.cursor_row]))
            return
        if event.data_table.id == "periods":
            row = event.cursor_row
            if row == len(stats.PRESETS):  # the Custom… row, always last
                self._ask_range()
            elif 0 <= row < len(stats.PRESETS):
                self._show_stats(stats.resolve(stats.PRESETS[row][0]))
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
        self._prompt_panel = "setup"
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

    # ----------------------------------------------------------- statistics
    def _do_stats(self, arg: str) -> None:
        """Bare ``stats`` opens the period picker; ``stats <spec>`` skips it."""
        if not arg:
            self._show_periods()
            return
        window = self._parse_window(arg)
        if window is not None:
            self._show_stats(window)

    def _parse_window(self, text: str) -> Optional[stats.Window]:
        try:
            return stats.parse(text)
        except ValueError as error:
            # The message names every accepted spelling, and may quote the user's text.
            self.notify(str(error), severity="error", markup=False)
            return None

    def _show_periods(self) -> None:
        self._fill_periods()
        self._range_pending = False
        self._prompt_panel = None
        self._set_panel("periods")

    def _show_stats(self, window: stats.Window) -> None:
        self.window = window
        self._range_pending = False
        self._prompt_panel = None
        self._build_report()
        self._fill_stats()
        self._set_panel("stats")

    def _ask_range(self) -> None:
        """Ask for an explicit range, answered in the command bar below the picker."""
        self._range_pending = True
        self._prompt_panel = "periods"
        prompt = self.query_one("#prompt", Static)
        prompt.update(
            Text.assemble(
                ("Date range for the statistics\n", "bold"),
                (
                    f"Type it in the command bar below, as {RANGE_EXAMPLE}.  "
                    "Escape returns to the list.",
                    "dim",
                ),
            )
        )
        prompt.display = True
        self.query_one("#command", Input).focus()

    def _answer_range(self, text: str) -> None:
        window = self._parse_window(text)
        if window is None:
            return  # a bad range leaves the prompt up, over the picker
        self._show_stats(window)

    def _cancel_range(self) -> None:
        # _show_periods() drops the pending question and hides the prompt with it.
        self._show_periods()

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
        if self._range_pending:
            self._answer_range(text)
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
        elif name in {"categorize", "categorise", "cat"}:
            self._do_categorize(arg)
        elif name == "category":
            self._do_category(arg)
        elif name == "transfers":
            self._do_transfers(arg)
        elif name == "merge":
            self._do_merge(arg)
        elif name == "filter":
            self._do_filter(arg)
        elif name == "stats":
            self._do_stats(arg)
        elif name == "help":
            self.notify(
                "import — browse data/to_import; enter imports the selected file\n"
                "import all | import <path> — import without browsing\n"
                "rename <raw vendor> = <display name> — override / aggregate a vendor\n"
                "rule <pattern> = <display name> — rename every matching vendor,\n"
                "  now and on future imports (e.g. rule Kindle Svcs* = Kindle)\n"
                "rules — list the rules you have defined (escape returns)\n"
                "categorize <vendor> = <category> — categorise that vendor's\n"
                "  transactions by hand (cat is short for categorize)\n"
                "categorize <vendor> = — undo a manual category\n"
                "categorize rule <pattern> = <category> — categorise every matching\n"
                "  vendor, now and on future imports\n"
                "categorize rules — list the rules you have defined (escape returns)\n"
                "category Food > Dining > Restaurants — build/move a category into\n"
                "  that spot, creating any missing levels\n"
                "category Dining — move an existing category to the top level\n"
                "category | category list — show the category tree, indented\n"
                "transfers [reset] — pair up movements between your own accounts\n"
                "merge <account> = <account> — fold one account into another\n"
                "filter <text> — search description, vendor, and raw name\n"
                "filter vendor:<text> — search one field (description/vendor/raw)\n"
                "filter — clear the text filter\n"
                "stats — pick a period, then see spending per category\n"
                "stats <period> — skip the picker (e.g. stats 6m, stats 1 year,\n"
                f"  stats {RANGE_EXAMPLE})\n"
                "  enter, or the right arrow, on a category row lists that window's\n"
                "  transactions; the left arrow goes back to the breakdown\n"
                "all — clear filters   refresh — reload   quit — exit\n"
                "Click an account/vendor/category to filter.\n"
                "ctrl+n / ctrl+t — prefill rename / categorize for the selected\n"
                "  transaction's vendor, or for the selected vendor in the sidebar.",
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

    def _set_drilled_from_stats(self, value: bool) -> None:
        """Flip the "back to stats" flag, and nudge the footer to match.

        The footer only recomputes on its own when focus changes; a filter typed into
        the command bar clears this flag without moving focus, so the hint would go
        stale without an explicit refresh.
        """
        if value == self._drilled_from_stats:
            return
        self._drilled_from_stats = value
        self.screen.refresh_bindings()

    def _set_panel(self, panel: str) -> None:
        """Show one of the main-view panels; escape always returns to transactions."""
        # Leaving the drilled-down view for any other panel invalidates "back to
        # stats". _drill_into_category() and _go_back_to_stats() both set the flag to
        # its real value themselves, after calling this, so this cannot undo either.
        self._set_drilled_from_stats(False)
        self._panel = panel
        for name in self.PANELS:
            self.query_one(f"#{name}").display = name == panel
        # The prompt belongs to whichever panel last raised a question.
        self.query_one("#prompt", Static).display = panel == self._prompt_panel
        if panel == "txns":
            self.query_one("#command", Input).focus()
        else:
            self.query_one(self.PANEL_FOCUS.get(panel, f"#{panel}")).focus()
        self._refresh_status()

    def _show_rules(self) -> None:
        self.reload()
        self._set_panel("rules")
        if not self._rules and not self._category_rules:
            self.notify(
                "No vendor rules yet, and no category rules. Add one with:\n"
                "  rule <pattern> = <display name>\n"
                "  categorize rule <pattern> = <category>"
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
        self._set_drilled_from_stats(False)  # a new search is a new view, not the drill-down's
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

    CATEGORIZE_USAGE = "Usage: categorize <vendor> = <category>   (blank category undoes it)"
    CATEGORY_RULE_USAGE = "Usage: categorize rule <pattern> = <category>"

    def _do_categorize(self, arg: str) -> None:
        """``categorize <vendor> = <category>``, its blank-category undo, and its rules."""
        arg = arg.strip()
        if not arg or arg.lower() == "rules":
            self._show_rules()
            return
        head, _, rest = arg.partition(" ")
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
                message = f"{vendor!r} → {value!r} ({changed} transactions categorised)"
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
            f"{pattern!r} → {value!r} ({changed} transactions categorised)", markup=False
        )

    def _do_category(self, arg: str) -> None:
        """``category <path>`` builds/moves a category; bare or ``list`` shows the tree.

        Distinct from ``categorize``: this manages the category hierarchy itself
        (creating, nesting, re-parenting), not which category a vendor's transactions
        get. A one-element path is a move to the top level (:func:`categories.ensure_path`).
        """
        arg = arg.strip()
        if not arg or arg.lower() == "list":
            self._notify_category_tree()
            return
        with self.session_factory() as session:
            try:
                category = categories.ensure_path(session, arg)
                path = categories.format_path(session, category)
            except categories.CategoryError as error:
                self.notify(str(error), severity="warning", markup=False)
                return
            session.commit()
        self.reload()
        self.notify(f"{path!r} ready.", markup=False)

    def _notify_category_tree(self) -> None:
        if not self._categories:
            self.notify("No categories yet. Add one with: category Food > Dining")
            return
        lines = [f"{'  ' * c.depth}{c.name} ({c.count})" for c in self._categories]
        self.notify("\n".join(lines), title="Categories", markup=False, timeout=8)

    # ------------------------------------------------------------- drill-down
    def _drill_into_category(self, row: int) -> None:
        """Enter, or the right arrow, on a statistics row lists the transactions behind it.

        The report's window comes along as a date filter. Without it the table would show
        every transaction that category ever had, and the figures the user just clicked
        would not match the rows they are now looking at.
        """
        if self._report is None or not 0 <= row < len(self._report.categories):
            return
        stat = self._report.categories[row]
        # Remember what the drill-down is about to overwrite, and where it came from, so
        # a left arrow can undo exactly this rather than blanking filters the user set
        # themselves, and can put the cursor back where it was.
        self._pre_drill_category_filter = self.category_filter
        self._pre_drill_date_filter = self.date_filter
        self._drill_source_row = row
        self.category_filter = stat.category_id
        self.date_filter = (self._report.window.start, self._report.window.end)
        # Panel first: reload() only rebuilds the report while the stats panel is up, and
        # rebuilding it under the new filter would rewrite the rows we just read.
        self._set_panel("txns")
        self._set_drilled_from_stats(True)
        self.reload()

    def _go_back_to_stats(self) -> None:
        """Left arrow, undoing exactly the drill-down that produced this view.

        Mirrors _drill_into_category(): restores the filters it overwrote (which may be
        None, or may be a filter the user had set before drilling in), rebuilds the
        report under them, and returns the stats cursor to the row that was drilled
        from.
        """
        row = self._drill_source_row
        self.category_filter = self._pre_drill_category_filter
        self.date_filter = self._pre_drill_date_filter
        self._pre_drill_category_filter = None
        self._pre_drill_date_filter = None
        self._drill_source_row = None
        self._set_drilled_from_stats(False)
        # reload() while the panel is still "txns" resyncs the transactions/totals to
        # the restored filters without rebuilding the report (see its own guard), so the
        # report is rebuilt explicitly here, the same way _show_stats() does.
        self.reload()
        self._build_report()
        self._fill_stats()
        self._set_panel("stats")
        if row is not None:
            table = self.query_one("#stats_table", DataTable)
            if 0 <= row < table.row_count:
                table.move_cursor(row=row)

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

    def _prefill_for_vendor(self, verb: str) -> None:
        """Prefill ``<verb> <raw vendor> = `` for whichever vendor is being pointed at.

        In the transaction table, that is the selected transaction's vendor. Rows carry
        the raw merchant string, so this works even for already-grouped vendors.
        Otherwise the sidebar decides: the active vendor filter, else the highlighted row.
        """
        txn = self._cursor_txn()
        if txn is not None:
            if not txn.vendor_raw:
                self.notify("That transaction has no vendor.", severity="warning")
                return
            self._prefill_command(f"{verb} {txn.vendor_raw} = ")
            return

        vendor = self._selected_vendor()
        if vendor is None:
            self.notify("Select a vendor in the sidebar first.", severity="warning")
            return
        if vendor.kind != "raw":
            # These commands are keyed on the raw vendor string, which the sidebar no
            # longer shows once a group exists, so we can only prefill the verb.
            self.notify(
                f"{vendor.name!r} is an override group — pick a raw vendor instead.",
                severity="warning",
            )
            self._prefill_command(f"{verb} ")
            return
        self._prefill_command(f"{verb} {vendor.name} = ")

    def action_drill_down(self) -> None:
        """The right arrow's twin of enter on a statistics row."""
        table = self.query_one("#stats_table", DataTable)
        self._drill_into_category(table.cursor_row)

    def action_drill_up(self) -> None:
        """The left arrow's "back" out of a statistics drill-down."""
        self._go_back_to_stats()

    def action_rename_vendor(self) -> None:
        self._prefill_for_vendor("rename")

    def action_categorize_vendor(self) -> None:
        self._prefill_for_vendor("categorize")

    def action_show_transactions(self) -> None:
        # Escape is the general-purpose "leave this view" key, so it drops the
        # drill-down's back-link even when the panel is already "txns".
        self._set_drilled_from_stats(False)
        if self._setup is not None:
            self.notify(f"Setup for {self._setup.path.name} cancelled.")
            self._cancel_setup()
            return
        if self._range_pending:
            self.notify("Custom range cancelled.")
            self._cancel_range()
            return
        if self._panel != "txns":
            self._set_panel("txns")

    def action_refresh(self) -> None:
        self.reload()

    def action_clear_filters(self) -> None:
        self._set_drilled_from_stats(False)
        self.account_filter = None
        self.vendor_filter = None
        self.category_filter = None
        self.text_filter = None
        self.date_filter = None
        self.reload()
        self.notify("Filters cleared.")


def run() -> None:
    BudgetApp().run()
