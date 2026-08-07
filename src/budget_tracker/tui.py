"""Full-screen Textual TUI for the budget tracker.

Layout: an accounts/categories sidebar (click a row to filter), a scrollable
transactions table, a totals line, and a command bar at the bottom.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

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

from . import accounts, categories, charts, formats, queries, stats, transfers, vendors
from .db import DuplicateCategoryNamesError, get_engine, get_sessionmaker, init_db
from .importer import (
    ImportCandidate,
    InboxFolder,
    UnknownImport,
    delete_import,
    import_csv,
    inspect_csv,
    list_inbox,
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


# A row of the import browser that moves you somewhere rather than importing something.
FOLDER_MARK = "▸"

# Transfers are shown greyed and flagged, because the totals deliberately ignore them —
# a row that looks like ordinary spending but is missing from the figures reads as a bug.
TRANSFER_MARK = "⇄"
TRANSFER_STYLE = "dim italic"

# The last row of the period picker, and the format its answer has to take.
CUSTOM_PERIOD = "Custom…"
RANGE_EXAMPLE = "2025-01-01..2025-06-30"

# Bar column width. The main panel has ~92 columns beside the 36-wide sidebar, and the
# other four columns plus DataTable's cell padding account for 48 of them. Odd, so the
# net chart's axis has equal halves either side of it (see charts.build).
CHART_WIDTH = 27

# Per measure: the header and the figure for the column beside the bar, then the same for
# the column after it. The charted measure comes first, and the second column is whatever
# is most worth seeing next to it — for a net chart, what the netting hid.
CHART_COLUMNS = {
    "net": (("Net", "net"), ("Out", "outflow")),
    "spend": (("Out", "outflow"), ("Net", "net")),
    "income": (("In", "inflow"), ("Net", "net")),
}


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


# A collapsed category shows this before its name; an expanded or leaf row shows nothing,
# so the default (fully expanded) rendering is byte-for-byte what it was before folding
# existed.
FOLD_INDICATOR = "▸"


def _foldable_category_ids(cats: List[stats.CategoryStat]) -> Set[int]:
    """category_ids that own at least one child in ``cats``.

    ``cats`` is depth-first (see stats.Report.categories), so a row has children exactly
    when the next row is deeper.
    """
    return {
        cats[i].category_id
        for i in range(len(cats) - 1)
        if cats[i + 1].depth > cats[i].depth
    }


def _visible_stats(
    cats: List[stats.CategoryStat], collapsed: Set[int], foldable: Set[int]
) -> List[stats.CategoryStat]:
    """``cats`` with every collapsed row's subtree hidden — recursively.

    A row is hidden while some ancestor still in ``collapsed`` is being skipped; the
    parent itself always stays visible. Both a nested collapse and the parent's own are
    remembered independently, keyed by category_id, so re-expanding a parent reveals
    whatever fold state its children already had.
    """
    visible = []
    hide_below_depth: Optional[int] = None
    for stat in cats:
        if hide_below_depth is not None:
            if stat.depth > hide_below_depth:
                continue
            hide_below_depth = None
        visible.append(stat)
        if stat.category_id in foldable and stat.category_id in collapsed:
            hide_below_depth = stat.depth
    return visible


def _stats_label(stat: stats.CategoryStat, collapsed: Set[int]) -> str:
    indent = "  " * stat.depth
    if stat.category_id in collapsed:
        return f"{indent}{FOLD_INDICATOR} {stat.name}"
    return f"{indent}{stat.name}"


class BudgetApp(App):
    CSS = """
    #sidebar { width: 36; }
    #accounts, #categories { border: round $accent; height: 1fr; }
    #txns, #rules, #imports, #setup, #periods { border: round $accent; height: 1fr; }
    #stats { height: 1fr; }
    #stats_table, #chart { border: round $accent; height: 1fr; }
    #prompt { height: auto; padding: 1 1 0 1; color: $accent; }
    #status { height: 1; padding: 0 1; color: $text-muted; background: $panel; }
    #command { border: tall $accent; }
    .heading { padding: 0 1; text-style: bold; color: $accent; }
    """

    PANELS = ("txns", "rules", "imports", "setup", "periods", "stats", "chart")
    # Panels whose widget is not itself focusable name the child that takes focus.
    PANEL_FOCUS = {"stats": "#stats_table"}

    # Footer labels are terse on purpose. Textual's Footer truncates mid-word rather than
    # dropping whole entries, so a verbose label does not cost itself — it costs every
    # binding after it, silently. At 130 columns the descriptive originals ran to ~160
    # and cut "Fold/unfold" to "F", hiding the last two bindings entirely. The key names
    # carry most of the meaning anyway, and `help` spells all of them out in full.
    BINDINGS = [
        ("ctrl+r", "refresh", "Refresh"),
        ("ctrl+l", "clear_filters", "Clear"),
        ("ctrl+n", "rename_vendor", "Rename"),
        ("ctrl+t", "categorize_vendor", "Categorise"),
        ("escape", "show_transactions", "Transactions"),
        # DataTable binds left/right itself (cursor movement between cells), which would
        # otherwise eat these before an ordinary App binding ever saw them. priority=True
        # checks the App first; check_action() below opts out — returning False, not just
        # doing nothing — everywhere but the one row cursor_type="row" already leaves
        # left/right without a visible job of their own, so falling through there is safe.
        Binding("right", "drill_down", "Drill down", show=True, priority=True),
        Binding("left", "drill_up", "Back to stats", show=True, priority=True),
        # Deliberately not priority=True: the #command Input holds focus most of the
        # time, and a priority binding would steal the space bar before Input ever saw
        # it, breaking ordinary typing. DataTable does not bind space itself, so a
        # plain (non-priority) binding reaches this once it has bubbled past whatever
        # is focused — see check_action() below for the "only on #stats_table" gate.
        Binding("space", "toggle_stats_fold", "Fold", show=True),
        # Same reasoning as space: a plain letter binding, not priority, so a command
        # like "filter foo" still gets its 'f' typed into #command rather than toggling
        # every fold in the stats table out from under the user.
        Binding("f", "toggle_all_stats_folds", "Fold all", show=True),
        # Same again: a plain letter, gated to the chart panel by check_action, so 'b'
        # typed into a command is still just a letter.
        Binding("b", "cycle_bucket", "Bucket", show=True),
        Binding("m", "cycle_measure", "Measure", show=True),
        ("ctrl+c", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.engine = get_engine()
        try:
            init_db(self.engine)
        except DuplicateCategoryNamesError as error:
            # init_db's own message names the duplicates; this just turns "the app
            # cannot open" into "here is the exact command that unblocks it" instead of
            # a raw traceback with no path forward.
            raise DuplicateCategoryNamesError(
                f"{error}\n\nThe app can't open until every duplicate is merged. From "
                "the command line (not this app, since it can't start either): "
                "'budget category merge <source> <target> --yes' for each name listed "
                "above, then run budget again."
            ) from error
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
        # Last labels rendered into each sidebar list, so _fill_list can skip a rebuild
        # that would produce exactly what is already on screen.
        self._list_labels: Dict[str, List[str]] = {}
        # Parallel to the rows in #txns, so a cursor index maps back to a transaction.
        self._txns: List[queries.TxnRow] = []
        self._rules: List[queries.RuleRow] = []
        self._category_rules: List[queries.CategoryRuleRow] = []
        self._candidates: List[ImportCandidate] = []
        # Where the import browser is currently looking, and the rows above the files
        # that move it: the parent (as "..") first when there is one, then each
        # sub-directory. Kept parallel to the table's leading rows so a cursor index maps
        # back to a destination — the same discipline _stats_rows follows.
        self._import_dir = TO_IMPORT_DIR
        self._import_nav: List[Path] = []
        self._import_folders: List[InboxFolder] = []
        # Past imports (from the database), shown alongside the candidates so an
        # ``unimport`` id is something the user can actually look up.
        self._imports: List[queries.ImportRow] = []
        self._panel = "txns"
        self._setup: Optional[_Setup] = None
        self._totals = queries.Totals(count=0, net_minor=0, outflow_minor=0, inflow_minor=0)
        # The statistics window survives panel switches, so reload() can re-scope it.
        self.window: Optional[stats.Window] = None
        self._report: Optional[stats.Report] = None
        # category_ids folded shut, keyed by id rather than table row so the state
        # survives a rebuild (a new window, a filter, drilling in and back out) — see
        # _fill_stats() and _visible_stats().
        self._collapsed: Set[int] = set()
        self._foldable_ids: Set[int] = set()
        # Parallel to the rendered rows of #stats_table, *excluding* the closing TOTAL
        # row — a row hidden by folding is not in it, so a table row index always maps
        # back to the right CategoryStat (see _drill_into_category()).
        self._stats_rows: List[stats.CategoryStat] = []
        # The chart's bucket size and its last-built bars. The bucket is chosen from the
        # window's length the first time (charts.choose_bucket) and then kept, so
        # re-scoping by category does not silently undo a bucket the user picked.
        self._bucket: Optional[str] = None
        # Which of spending / income / net the bars draw. Unlike the bucket this is a
        # preference, not a function of the window, so it survives a new period.
        self._measure = charts.MEASURES[0]
        self._chart: Optional[charts.Chart] = None
        # Transfers are left out of the bars, as they are out of every other figure; the
        # count is carried so the status line can say so rather than quietly losing them.
        self._chart_transfers = 0
        self._range_pending = False  # awaiting a typed date range for the picker
        # Which panel the period picker is choosing for: "stats" or "chart". Both open
        # the same picker, and it has to know where the answer goes.
        self._picker_target = "stats"
        # Awaiting a typed "yes" to confirm a pending `unimport`; holds what it would
        # destroy, read up front so the confirmation names real numbers.
        self._pending_unimport: Optional[queries.ImportDeletePreview] = None
        # Awaiting a typed "yes" to confirm a `category` that would relocate an
        # existing category; holds the raw path text so the confirmed apply can just
        # re-run ensure_path with confirm_relocation=True.
        self._pending_category: Optional[str] = None
        # Same idea for `category merge`; holds (source, target) as typed.
        self._pending_category_merge: Optional[tuple] = None
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
                yield DataTable(id="chart")
                yield Static("", id="status")
        yield Input(
            placeholder=(
                "command: import | unimport | filter | categorize | category | format | "
                "stats | chart | rules | all | refresh | help | quit"
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
        if action in ("toggle_stats_fold", "toggle_all_stats_folds"):
            return (
                self._panel == "stats"
                and self.focused is not None
                and self.focused.id == "stats_table"
            )
        if action in ("cycle_bucket", "cycle_measure"):
            return (
                self._panel == "chart"
                and self.focused is not None
                and self.focused.id == "chart"
            )
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
        # Blank for a not-yet-imported candidate; past imports carry the id `unimport`
        # needs, so this is the only place that id is ever shown. Wide enough for a
        # five-digit id — plausible after years of monthly imports — without clipping.
        imports.add_column("ID", width=6)
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

        chart = self.query_one("#chart", DataTable)
        chart.cursor_type = "row"
        # Columns are added in _fill_chart, not here: two of the headers name the measure
        # being charted, so they change when 'm' does.

        self.query_one("#stats", Vertical).display = False
        chart.display = False
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
        # Same for the chart: picking a category in the sidebar is how a chart gets
        # scoped, so it has to redraw here or the bars would keep showing the old scope.
        if self._panel == "chart" and self.window is not None:
            self._build_chart()
            self._fill_chart()
        self._refresh_status()

    def _fill_list(self, selector: str, labels: List[str]) -> None:
        """Render a sidebar list, but only when its contents have actually changed.

        reload() runs on every filter change and every statistics drill-down, and none of
        those touch the sidebar: get_accounts/get_vendors/get_categories take no filter
        arguments, so their output depends on the database alone. Rebuilding anyway cost
        more than everything else in a drill-down put together — the vendor list runs to
        several hundred rows, and mounting that many widgets is far dearer than the query
        behind it. Comparing the labels first is cheap, correct whatever the caller
        wanted, and keeps the list's scroll position across a drill.

        extend() mounts the items in one pass; appending in a loop mounts them one at a
        time and is several times slower on a list this long.
        """
        if self._list_labels.get(selector) == labels:
            return
        self._list_labels[selector] = list(labels)
        list_view = self.query_one(selector, ListView)
        list_view.clear()
        list_view.extend(
            [ListItem(Label("— All —"))] + [ListItem(Label(label)) for label in labels]
        )

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
        # Navigation first, in the order _import_nav records: ".." (when there is one),
        # then the sub-directories. Enter on any of them moves the browser rather than
        # importing anything.
        if self._import_nav and len(self._import_nav) > len(self._import_folders):
            table.add_row(
                Text(f"{FOLDER_MARK} ..", style="bold"),
                "",
                Text("up", style=TRANSFER_STYLE),
                "",
            )
        for folder in self._import_folders:
            table.add_row(
                Text(f"{FOLDER_MARK} {_truncate(folder.name, 32)}", style="bold"),
                Text(str(folder.csv_count), justify="right"),
                Text("folder", style=TRANSFER_STYLE),
                "",
            )
        for candidate in self._candidates:
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
        for imp in self._imports:
            table.add_row(
                Text(_truncate(imp.source_file, 34), style=TRANSFER_STYLE),
                Text(str(imp.transaction_count), justify="right", style=TRANSFER_STYLE),
                Text("imported", style=TRANSFER_STYLE),
                Text(str(imp.id), justify="right", style=TRANSFER_STYLE),
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
        """Render the report, honouring folded subtrees.

        Folding never changes a number: every row already rolls up its descendants (see
        stats.CategoryStat), so hiding them here only removes rows, never edits one.
        self._stats_rows is rebuilt in lock-step with the table, so a table row index
        always maps back to the CategoryStat it actually shows — see
        _drill_into_category(), which depends on that and would otherwise open the
        wrong category once rows can be hidden.
        """
        table = self.query_one("#stats_table", DataTable)
        table.clear()
        if self._report is None:
            self._stats_rows = []
            self._foldable_ids = set()
            return
        cats = self._report.categories
        self._foldable_ids = _foldable_category_ids(cats)
        self._stats_rows = _visible_stats(cats, self._collapsed, self._foldable_ids)
        for stat in self._stats_rows:
            # Blank at depth 0: parent_share is identical to share there by construction
            # (see stats.CategoryStat.parent_share), so printing it twice is noise.
            parent_pct = (
                "" if stat.depth == 0 else f"{abs(stat.parent_share) * 100:.1f}%"
            )
            table.add_row(
                _truncate(_stats_label(stat, self._collapsed), 26),
                Text(str(stat.count), justify="right"),
                _amount_cell(stat.total_minor),
                _amount_cell(stat.avg_month_minor),
                # Shares are a fraction of a negative outflow, so a category that spent
                # nothing comes out as -0.0; abs() keeps that off the screen.
                Text(f"{abs(stat.share) * 100:.1f}%", justify="right"),
                Text(parent_pct, justify="right"),
            )
        self._add_stats_total_row(table)

    def _toggle_fold(self, row: int) -> None:
        """Space on a stats row: collapse/expand its subtree if it has one.

        A leaf row, the TOTAL row, or an out-of-range row does nothing — not a crash,
        not a notification, since space is not obviously "for" the stats table the way
        enter or the arrows are.
        """
        if not 0 <= row < len(self._stats_rows):
            return
        stat = self._stats_rows[row]
        if stat.category_id not in self._foldable_ids:
            return
        if stat.category_id in self._collapsed:
            self._collapsed.discard(stat.category_id)
        else:
            self._collapsed.add(stat.category_id)
        self._fill_stats()
        # The toggled row's own subtree is what grows or shrinks, always right after it,
        # so its own row index is unchanged by the toggle — the cursor can just stay put.
        table = self.query_one("#stats_table", DataTable)
        if 0 <= row < table.row_count:
            table.move_cursor(row=row)

    def _toggle_fold_all(self) -> None:
        """``f``: fold every group if any is expanded, else unfold them all.

        "Any expanded" rather than "all collapsed" so the key always visibly does
        something — a mix of folded and unfolded groups collapses fully on the first
        press instead of silently unfolding the already-collapsed ones.
        """
        if not self._foldable_ids:
            return
        table = self.query_one("#stats_table", DataTable)
        row = table.cursor_row
        if self._foldable_ids - self._collapsed:
            self._collapsed |= self._foldable_ids
        else:
            self._collapsed -= self._foldable_ids
        self._fill_stats()
        # Collapsing/expanding everything moves rows around far more than a single
        # toggle does, so there is no single "same row" to return to — just keep the
        # cursor in range rather than landing on an arbitrary category.
        if table.row_count:
            table.move_cursor(row=min(row, table.row_count - 1))

    def _add_stats_total_row(self, table: DataTable) -> None:
        """A closing total, summing the depth-0 rows above it.

        Only the depth-0 rows: every row's money already includes its descendants, so
        adding the nested ones too would count a parent's spending twice (see
        stats.Report.categories). These figures come from the report's own totals rather
        than from re-adding the column, so the row cannot drift from the status line.

        Selecting it does nothing — _drill_into_category()'s bounds check already rejects
        a row index past the last category, which is exactly this one.

        A window with no transactions gets no total: a lone "TOTAL 0.00" reads as a
        result, where an empty table plainly says there is nothing here.
        """
        report = self._report
        if not report.categories:
            return
        table.add_row(
            Text("TOTAL", style="bold"),
            Text(str(report.count), style="bold", justify="right"),
            Text(
                _fmt_amount(report.net_minor),
                style="bold " + ("red" if report.net_minor < 0 else "green"),
                justify="right",
            ),
            Text(
                _fmt_amount(stats.per_month(report.net_minor, report.window)),
                style="bold "
                + ("red" if report.net_minor < 0 else "green"),
                justify="right",
            ),
            # 100% by construction, and worth printing: it says the column above is a
            # share of this window's spending and nothing has been left out of it.
            Text("100.0%" if report.outflow_minor else "", style="bold", justify="right"),
            Text("", justify="right"),
        )

    # ---------------------------------------------------------------- chart
    def _build_chart(self) -> None:
        """Fetch the series and scale it, under exactly the filters everything else uses.

        The same filters go to the transfer count as to the series, so the "N transfers
        excluded" the status line prints is the count actually missing from these bars.
        """
        with self.session_factory() as session:
            series = stats.spending_series(
                session,
                self.window,
                self._bucket,
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
                date_range=(self.window.start, self.window.end),
            )
        self._chart = charts.build(series, measure=self._measure, width=CHART_WIDTH)
        self._chart_transfers = totals.transfer_count

    def _bar_cell(self, bar: charts.Bar) -> Text:
        """The bar, coloured by direction: red is money out, green is money in.

        On a net chart that means the two sides of the axis are different colours, which
        is the same rule the amount columns already follow — it just happens to land on
        one row at a time there.
        """
        inflow_side = "green" if self._measure != "spend" else "red"
        return Text.assemble(
            (bar.left, "red"), (bar.axis, "dim"), (bar.right, inflow_side)
        )

    def _money_cell(self, bar: charts.Bar, field: str) -> Text:
        """One of the two figure columns beside the bar, per CHART_COLUMNS."""
        if field == "net":
            return _amount_cell(bar.net_minor)
        # Outflow and inflow are positive magnitudes, so _amount_cell's sign rule would
        # paint them both green; the direction is what the colour has to carry here.
        value = bar.outflow_minor if field == "outflow" else bar.inflow_minor
        style = ("red" if field == "outflow" else "green") if value else "dim"
        return Text(_fmt_amount(value), style=style, justify="right")

    def _fill_chart(self) -> None:
        """Redraw the table, columns included — two headers name the current measure."""
        table = self.query_one("#chart", DataTable)
        table.clear(columns=True)
        if self._chart is None:
            return
        columns = CHART_COLUMNS[self._measure]
        # 9 + CHART_WIDTH + 12 + 12 + 5 plus two cells of padding each: 76 of the ~92 the
        # main panel has, leaving the bars room to be the widest thing on the row (see
        # test_chart_table_fits_the_main_panel).
        table.add_column("Period", width=9)
        table.add_column(charts.MEASURE_HEADERS[self._measure], width=CHART_WIDTH)
        for header, _field in columns:
            table.add_column(header, width=12)
        table.add_column("Txns", width=5)

        for bar in self._chart.bars:
            table.add_row(
                bar.label,
                self._bar_cell(bar),
                *[self._money_cell(bar, field) for _header, field in columns],
                Text(str(bar.count), justify="right"),
            )
        self._add_chart_total_row(table)

    def _add_chart_total_row(self, table: DataTable) -> None:
        """A closing total, plus the per-bucket average in the bar column.

        The average belongs there because that column is the only one whose units are
        per-bucket; putting a summed bar there would be meaningless, and leaving it blank
        wastes the widest column on the row.

        A window holding no transactions gets no total, for the same reason the
        statistics table skips one: a row of zeroes reads as a finding. The test is the
        transaction count, not the bar list — buckets are zero-filled, so an empty
        window still has a full set of (empty) bars to draw.
        """
        chart = self._chart
        if not any(bar.count for bar in chart.bars):
            return

        def figure(field: str) -> Text:
            value = {
                "net": chart.net_minor,
                "outflow": chart.outflow_minor,
                "inflow": chart.inflow_minor,
            }[field]
            if field == "net":
                style = "red" if value < 0 else "green"
            else:
                style = "red" if field == "outflow" else "green"
            return Text(_fmt_amount(value), style=f"bold {style}", justify="right")

        table.add_row(
            Text("TOTAL", style="bold"),
            Text(f"avg {_fmt_amount(chart.avg_minor)}/{self._bucket}", style="dim"),
            *[figure(field) for _header, field in CHART_COLUMNS[self._measure]],
            Text(
                str(sum(bar.count for bar in chart.bars)),
                style="bold",
                justify="right",
            ),
        )

    def _chart_status(self) -> str:
        """One line, under the same 92-column budget as every other panel's status."""
        chart = self._chart
        window = self.window
        label = "custom" if window.key == "custom" else window.label
        scope = self._chart_scope()
        excluded = f"{TRANSFER_MARK} {self._chart_transfers} " if self._chart_transfers else ""
        return (
            f"{label} {_range_label((window.start, window.end))} "
            f"{self._measure}/{self._bucket} "
            f"{scope}"
            f"{excluded}"
            f"total {_fmt_amount(chart.total_minor)} "
            f"peak {_fmt_amount(chart.peak_minor)}"
        )

    def _chart_scope(self) -> str:
        """The active category, named, plus a terse marker for any other filter.

        Naming the category is the point — the whole feature is charting one category, so
        a chart that does not say which one it is showing is a trap. The other filters
        only get a marker, as they do in the transactions status line.
        """
        parts = []
        if self.category_filter is not None:
            name = next(
                (c.name for c in self._categories if c.id == self.category_filter), None
            )
            parts.append(_truncate(name, 16) if name else "category")
        if self.account_filter is not None:
            parts.append("account")
        if self.vendor_filter is not None:
            parts.append("vendor")
        if self.text_filter is not None:
            parts.append("text")
        return f"[{', '.join(parts)}] " if parts else ""

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
        if self._panel == "chart" and self._chart is not None:
            status.update(self._chart_status())
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
            # The directory comes first: every count below it is scoped to that folder,
            # and a panel that does not say where it is looking invites importing the
            # wrong month.
            status.update(
                f"{_truncate(self._import_label(), 24)}   "
                f"{len(self._candidates)} file(s), {ready} ready   "
                "enter to import, or open a folder   "
                f"{len(self._imports)} past   escape returns"
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
                self._open_period(stats.resolve(stats.PRESETS[row][0]))
            return
        if event.data_table.id != "imports":
            return
        row = event.cursor_row
        # The navigation rows come first, so a candidate's index is offset by them.
        if 0 <= row < len(self._import_nav):
            self._open_import_dir(self._import_nav[row])
            return
        row -= len(self._import_nav)
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

    def _do_chart(self, arg: str) -> None:
        """``chart`` opens the period picker; ``chart <period> [bucket] [measure]`` skips it.

        Trailing ``day``/``week``/``month`` and ``net``/``spending``/``income`` words set
        the bucket and the measure, in either order, so ``chart 1y month spending`` and
        ``chart 1 year`` both read the way they look. Either word on its own re-draws the
        chart already on screen — the same thing ``b`` and ``m`` do, for anyone who would
        rather type it than remember a key.
        """
        arg = arg.strip()
        if not arg:
            self._show_periods("chart")
            return

        parts = arg.split()
        bucket = measure = None
        while parts:
            tail = parts[-1].lower()
            if bucket is None and tail in queries.BUCKETS:
                bucket = tail
            elif measure is None and tail in charts.MEASURE_ALIASES:
                measure = charts.MEASURE_ALIASES[tail]
            else:
                break
            parts = parts[:-1]
        text = " ".join(parts)

        if not text:
            if self.window is None:
                self.notify(
                    "No period yet: try 'chart 3m "
                    f"{bucket or measure}', or bare 'chart' to pick one.",
                    severity="warning",
                )
                return
            self._show_chart(self.window, bucket, measure)
            return

        window = self._parse_window(text)
        if window is not None:
            self._show_chart(window, bucket, measure)

    def _show_periods(self, target: str = "stats") -> None:
        self._picker_target = target
        self._fill_periods()
        self._range_pending = False
        self._prompt_panel = None
        self._set_panel("periods")

    def _open_period(self, window: stats.Window) -> None:
        """Send a period the picker just produced to whichever panel asked for it."""
        if self._picker_target == "chart":
            self._show_chart(window)
        else:
            self._show_stats(window)

    def _show_stats(self, window: stats.Window) -> None:
        self.window = window
        self._range_pending = False
        self._prompt_panel = None
        self._build_report()
        self._fill_stats()
        self._set_panel("stats")

    def _show_chart(
        self,
        window: stats.Window,
        bucket: Optional[str] = None,
        measure: Optional[str] = None,
    ) -> None:
        """Open the chart for ``window``, bucketed explicitly or by the window's length.

        A bucket the user asked for is remembered across re-scopes, but a *new* window
        re-derives its own: daily bars chosen for one month are unreadable stretched over
        two years, and silently keeping them would be worse than overriding a choice the
        user made about a range they have now left. The measure is not like that — it is
        a question about the money, not about the range — so it simply sticks.
        """
        rebucket = bucket is not None or self._bucket is None or window != self.window
        self.window = window
        if rebucket:
            self._bucket = bucket or charts.choose_bucket(window)
        if measure is not None:
            self._measure = measure
        self._range_pending = False
        self._prompt_panel = None
        self._build_chart()
        self._fill_chart()
        self._set_panel("chart")

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
        self._open_period(window)

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
        if self._pending_unimport is not None:
            self._answer_unimport(text)
            return
        if self._pending_category is not None:
            self._answer_category(text)
            return
        if self._pending_category_merge is not None:
            self._answer_category_merge(text)
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
        elif name == "unimport":
            self._do_unimport(arg)
        elif name == "format":
            self._do_format(arg)
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
        elif name in {"chart", "graph"}:
            self._do_chart(arg)
        elif name == "help":
            self.notify(
                "import — browse data/to_import; enter imports the selected file,\n"
                "  and lists past imports (with their id) below the candidates\n"
                "import all | import <path> — import without browsing\n"
                "unimport <id> — delete a past import and its transactions;\n"
                "  asks for confirmation, naming what it will destroy\n"
                "format — list learned CSV layouts and their amount polarity\n"
                "format <name> invert on|off — flip whether a positive amount means\n"
                "  money out for that layout (future imports only; fix a bad import\n"
                "  with unimport, then re-import)\n"
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
                "  category names are unique across the whole tree, so if that would\n"
                "  move an existing category rather than create one, you are asked to\n"
                "  confirm what would move; a genuinely separate category needs its\n"
                "  own distinct name, e.g. 'Dining (Travel)'\n"
                "category | category list — show the category tree, indented\n"
                "category merge <source> = <target> — fold one category into another:\n"
                "  repoints its transactions, rules, and children, then deletes it\n"
                "  (asks for confirmation, naming what will move)\n"
                "transfers — pair up movements between your own accounts\n"
                "transfers same-account — also pair legs within the same account;\n"
                "  off by default, since it makes an accidental false pairing more\n"
                "  likely (for providers whose sub-accounts you track as one account)\n"
                "transfers reset — un-pair everything transfers detected\n"
                "merge <account> = <account> — fold one account into another\n"
                "filter <text> — search description, vendor, and raw name\n"
                "filter vendor:<text> — search one field (description/vendor/raw)\n"
                "filter — clear the text filter\n"
                "stats — pick a period, then see spending per category\n"
                "stats <period> — skip the picker (e.g. stats 6m, stats 1 year,\n"
                f"  stats {RANGE_EXAMPLE})\n"
                "  enter, or the right arrow, on a category row lists that window's\n"
                "  transactions; the left arrow goes back to the breakdown\n"
                "  space, on a category row with children, folds/unfolds its subtree\n"
                "  f folds/unfolds every group at once\n"
                "chart — pick a period, then see money per day/week/month as bars\n"
                "chart <period> [day|week|month] [net|spending|income] — skip the\n"
                "  picker, set the bar width and what the bars measure (e.g.\n"
                "  chart 1y month spending); the bucket defaults to the period's\n"
                "  length. b cycles the bucket, m the measure. graph = chart\n"
                "  net draws either side of a centre line: money out to the left,\n"
                "  money in to the right, so an even month sits on the line\n"
                "  click a category in the sidebar to chart just that category\n"
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
            # The directory being browsed, not the whole tree: "all" should import what
            # the panel is showing, not quietly reach into folders you have not opened.
            paths = list(list_inbox(self._import_dir, TO_IMPORT_DIR).files)
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
            self.notify("Unimport cancelled.")
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
        """Browse one directory of the inbox: where to go, what to import, plus history.

        Only the CSVs *directly* here become candidates. Inspecting a whole tree up front
        would mean reading every file under the inbox to draw one screen, and the folder
        rows already say how many are down there.
        """
        listing = list_inbox(self._import_dir, TO_IMPORT_DIR)
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

    def _import_label(self) -> str:
        """The current directory, relative to the inbox, named rather than ``.``.

        At the top that is the inbox folder's own name: "." is technically the relative
        path but reads as an error in a status line, and the point of the label is to say
        where you are in words you would recognise.
        """
        try:
            relative = self._import_dir.relative_to(TO_IMPORT_DIR)
        except ValueError:
            return str(self._import_dir)
        return TO_IMPORT_DIR.name if relative == Path(".") else str(relative)

    def _open_import_dir(self, path: Path) -> None:
        self._import_dir = path
        self._show_imports()
        # A fresh directory starts at its first row rather than wherever the cursor
        # happened to be in the directory just left.
        table = self.query_one("#imports", DataTable)
        if table.row_count:
            table.move_cursor(row=0)

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
        arg = arg.strip()
        with self.session_factory() as session:
            if arg in {"reset", "clear"}:
                reset = transfers.clear_transfers(session)
                session.commit()
                message = f"Un-paired {reset} transaction(s)."
            elif arg == "same-account":
                # Opt-in only: see transfers.detect_transfers for why this is not the
                # default (a false same-account pairing silently drops two real
                # transactions from the totals).
                pairs = transfers.detect_transfers(session, allow_same_account=True)
                session.commit()
                message = f"Found {pairs} new transfer pair(s) (same-account allowed)."
            elif arg:
                self.notify(
                    f"Unknown transfers option: {arg!r}. Try 'transfers', "
                    "'transfers same-account', or 'transfers reset'.",
                    severity="warning",
                    markup=False,
                )
                return
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

    CATEGORY_MERGE_USAGE = "Usage: category merge <source> = <target>"

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

    FORMAT_USAGE = "Usage: format | format <name> invert on|off"

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

    # ------------------------------------------------------------- drill-down
    def _drill_into_category(self, row: int) -> None:
        """Enter, or the right arrow, on a statistics row lists the transactions behind it.

        The report's window comes along as a date filter. Without it the table would show
        every transaction that category ever had, and the figures the user just clicked
        would not match the rows they are now looking at.
        """
        if self._report is None or not 0 <= row < len(self._stats_rows):
            return
        stat = self._stats_rows[row]
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

    def action_toggle_stats_fold(self) -> None:
        """Space on a statistics row: fold/unfold its subtree. See check_action()."""
        table = self.query_one("#stats_table", DataTable)
        self._toggle_fold(table.cursor_row)

    def action_toggle_all_stats_folds(self) -> None:
        """``f`` on the statistics table: fold/unfold every group. See check_action()."""
        self._toggle_fold_all()

    def action_cycle_bucket(self) -> None:
        """``b`` on the chart: step day → week → month → day. See check_action().

        A cycle rather than three keys, and it does not skip a bucket that would be
        unwieldy for the window: charting two years by day is a bad idea but it is the
        user's to make, and a key that silently refuses to do anything is worse.
        """
        if self.window is None or self._bucket is None:
            return
        order = queries.BUCKETS
        self._bucket = order[(order.index(self._bucket) + 1) % len(order)]
        self._redraw_chart()

    def action_cycle_measure(self) -> None:
        """``m`` on the chart: step net → spending → income → net. See check_action()."""
        if self.window is None:
            return
        order = charts.MEASURES
        self._measure = order[(order.index(self._measure) + 1) % len(order)]
        self._redraw_chart()

    def _redraw_chart(self) -> None:
        self._build_chart()
        self._fill_chart()
        self._refresh_status()

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
        if self._pending_unimport is not None:
            self.notify("Unimport cancelled.")
            self._cancel_unimport()
            return
        if self._pending_category is not None:
            self.notify("Category move cancelled.")
            self._cancel_category()
            return
        if self._pending_category_merge is not None:
            self.notify("Merge cancelled.")
            self._cancel_category_merge()
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
