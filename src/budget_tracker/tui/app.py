"""Full-screen Textual TUI for the budget tracker.

Layout: an accounts/categories sidebar (click a row to filter), a scrollable
transactions table, a totals line, and a command bar at the bottom.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from rich.text import Text
from sqlalchemy.orm import Session
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

from .. import (
    accounts,
    categories,
    charts,
    formats,
    importer,
    queries,
    rates,
    stats,
    transfers,
    vendors,
)
from ..db import DuplicateCategoryNamesError, get_engine, get_sessionmaker, init_db
from ..importer import (
    ImportCandidate,
    InboxFolder,
    UnknownImport,
    delete_import,
    import_csv,
    inspect_csv,
    list_inbox,
    read_header_and_rows,
)
from . import chart as chart_panel
from . import imports as imports_panel
from . import periods as periods_panel
from . import pie as pie_panel
from . import rules as rules_panel
from . import stats as stats_panel
from . import transactions
from .formatting import CHART_WIDTH, _fmt_amount, _range_label, _truncate
from .imports import _Setup

_REPO_ROOT = Path(__file__).resolve().parents[3]
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

# The chart panel's own 'b' cycle, kept separate from queries.BUCKETS now that the
# latter also offers "year" — daily bars are only useful on the chart, over a window
# short enough to read, so adding "year" there must not change what 'b' cycles through.
_CHART_BUCKETS = ("day", "week", "month")

# The pie panel's own 'b' cycle: no daily bucket (a year by day is 365 rows), but a
# yearly one, since a multi-year window benefits from it in a way the chart's shorter
# windows rarely do.
_SHARE_BUCKETS = ("week", "month", "year")


class BudgetApp(App):
    CSS = """
    #sidebar { width: 36; }
    #accounts, #categories { border: round $accent; height: 1fr; }
    #txns, #rules, #imports, #setup, #periods { border: round $accent; height: 1fr; }
    #stats { height: 1fr; }
    #stats_table, #chart { border: round $accent; height: 1fr; }
    #pie { border: round $accent; height: 1fr; padding: 1; }
    #prompt { height: auto; padding: 1 1 0 1; color: $accent; }
    #status { height: 1; padding: 0 1; color: $text-muted; background: $panel; }
    #command { border: tall $accent; }
    .heading { padding: 0 1; text-style: bold; color: $accent; }
    """

    PANELS = ("txns", "rules", "imports", "setup", "periods", "stats", "chart", "pie")
    # Panels whose widget is not itself focusable name the child that takes focus.
    PANEL_FOCUS = {"stats": "#stats_table"}

    # The vendor sidebar mounts one ListItem per row, and a real history runs to
    # hundreds of distinct merchants. _fill_list's cache guard already skips rebuilding
    # it when nothing changed, but Textual's full (non-scroll) reflow walks *every*
    # mounted widget regardless of whether it changed -- see
    # src/profiling/sidebar_isolation.py, where merely having ~1,000 vendor widgets
    # mounted cost ~400ms on every reload() no matter how little of the app actually
    # changed. Capping the list is the only lever that touches that: the vendors are
    # already sorted by transaction count (queries.get_vendors), so the cap only hides
    # the long tail of one-off merchants, and `filter vendor:<text>` still reaches any
    # of them. Chosen from the same profile: 200 keeps the round trip close to the
    # floor of never rebuilding the sidebar at all, while 300 was already visibly worse.
    VENDOR_SIDEBAR_CAP = 200

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
        # By ISO code, so a per-row currency (queries.TxnRow.currency) can be formatted
        # in its own decimal places and symbol rather than always assuming two decimal
        # places — see _fmt_amount_for().
        self._currencies: Dict[str, queries.CurrencyRow] = {}
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
        # Same idea for rows a missing exchange rate left out of the bars entirely --
        # see queries.Totals.unconverted_count and UNCONVERTED_MARK.
        self._chart_unconverted = 0
        # The pie panel's last-built stacked share chart: one bar for the whole window
        # plus one per bucket, drawn from the same report the statistics panel uses
        # (see reload()'s guard) plus its own per-bucket query (see _build_pie()).
        self._pie: Optional[charts.StackedShareChart] = None
        # The pie panel's own bucket, cycled by 'b' independently of the chart's — see
        # _SHARE_BUCKETS. Monthly by default; sticky across a new period the same way
        # the chart's measure is, not re-derived from the window's length.
        self._pie_bucket = "month"
        self._range_pending = False  # awaiting a typed date range for the picker
        # Which panel the period picker is choosing for: "stats", "chart", or "pie". All
        # three open the same picker, and it has to know where the answer goes.
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
        # None unless the transactions panel is showing exactly what a drill-down put
        # there — "stats" or "chart", naming which panel to send the left arrow back to.
        # Anything that changes the view out from under it (a new filter, ctrl+l,
        # escape, opening another panel) has to clear it, or a stale flag could send a
        # later, unrelated left-arrow press somewhere the user did not ask for.
        self._drill_origin: Optional[str] = None
        # What the drill-down overwrote, so going back restores it rather than
        # unconditionally blanking a filter the user had set on purpose. A chart
        # drill-down only ever touches the date, so the category filter it "restores" is
        # simply whatever was already there.
        self._pre_drill_category_filter: Optional[int] = None
        self._pre_drill_date_filter: Optional[queries.DateRange] = None
        self._drill_source_row: Optional[int] = None  # table row to land back on

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
                yield Static("", id="pie")
                yield Static("", id="status")
        yield Input(
            placeholder=(
                "command: import | unimport | filter | categorize | category | format | "
                "stats | chart | pie | rates | rules | all | refresh | help | quit"
            ),
            id="command",
        )
        yield Footer()

    @property
    def _drilled_from_stats(self) -> bool:
        """True only right after a statistics drill-down — see ``_drill_origin``."""
        return self._drill_origin == "stats"

    @property
    def _drilled_from_chart(self) -> bool:
        """True only right after a chart drill-down — see ``_drill_origin``."""
        return self._drill_origin == "chart"

    def check_action(self, action: str, parameters: tuple) -> Optional[bool]:
        """Gate the priority left/right bindings so they only act where they mean something.

        Returning ``False`` (not just a no-op action body) matters: it is what makes
        ``_check_bindings`` fall through to the focused ``DataTable``'s own binding
        instead of swallowing the key everywhere, and it is also what hides the footer
        hint outside the panel it applies to.
        """
        if action == "drill_down":
            return self._panel in ("stats", "chart")
        if action == "drill_up":
            return self._drill_origin is not None
        if action in ("toggle_stats_fold", "toggle_all_stats_folds"):
            return (
                self._panel == "stats"
                and self.focused is not None
                and self.focused.id == "stats_table"
            )
        if action == "cycle_bucket":
            return self.focused is not None and (
                (self._panel == "chart" and self.focused.id == "chart")
                or (self._panel == "pie" and self.focused.id == "pie")
            )
        if action == "cycle_measure":
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
        # Wide enough for a three-character symbol (CHF) plus a signed six-figure
        # amount ("CHF-999,999.99" is 14) with a column to spare — see
        # test_txns_amount_column_fits_a_symbol_and_a_six_figure_amount.
        table.add_column("Amount", width=15)
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
        pie = self.query_one("#pie", Static)
        pie.display = False
        # A plain Static cannot take focus by default; it needs to here so 'b' reaches
        # action_cycle_bucket instead of being typed into the command bar (see
        # _set_panel()).
        pie.can_focus = True
        self.query_one("#prompt", Static).display = False

        self.reload()
        self.query_one("#command", Input).focus()

    # ------------------------------------------------------------------ data
    def _active_filters(self) -> queries.Filters:
        """The app's active filters as one value.

        These five always travel together and always mean the same thing; passing them
        one at a time is what made adding a sixth touch every signature. Named
        ``_active_filters`` rather than the obvious ``_filters`` because Textual's own
        ``App`` already owns that attribute -- a list of line filters -- and shadowing it
        replaces the method with a list the moment the app initialises. A caller that
        needs a different range says so with ``.replace(date_range=...)`` -- the
        statistics and chart panels do, because their window is the range, not whatever
        a drill-down happened to leave on ``self.date_filter``.
        """
        return queries.Filters(
            account_id=self.account_filter,
            category_id=self.category_filter,
            vendor_filter=self.vendor_filter,
            text_filter=self.text_filter,
            date_range=self.date_filter,
        )

    def reload(self) -> None:
        with self.session_factory() as session:
            self._accounts = queries.get_accounts(session)
            self._vendors = queries.get_vendors(session)
            self._categories = queries.get_categories(session)
            self._currencies = {c.code: c for c in queries.get_currencies(session)}
            self._rules = queries.get_rules(session)
            self._category_rules = queries.get_category_rules(session)
            txns = queries.get_transactions(
                session,
                filters=self._active_filters(),
            )
            totals = queries.get_totals(
                session,
                filters=self._active_filters(),
            )
        self._fill_list("#accounts", [f"{a.name} ({a.count})" for a in self._accounts])
        self._fill_list("#vendors", self._vendor_labels())
        self._fill_list(
            "#categories",
            [
                # Rolled up across every account a category has transactions in, so
                # there is no single currency to format this in — it stays plain
                # two-decimal-place formatting rather than guessing one (see
                # _fmt_amount_for's docstring).
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
        # And the pie: it draws from the same report the statistics panel does, plus
        # its own per-bucket series, so a filter change has to rebuild both or the bars
        # would keep showing the scope that was active when the panel was opened.
        if self._panel == "pie" and self.window is not None:
            self._build_report()
            self._build_pie()
            self._fill_pie()
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

    def _vendor_shown_count(self) -> int:
        """How many real vendor rows the sidebar has mounted -- see VENDOR_SIDEBAR_CAP."""
        return min(len(self._vendors), self.VENDOR_SIDEBAR_CAP)

    def _vendor_labels(self) -> List[str]:
        """Labels for the vendor sidebar, capped at VENDOR_SIDEBAR_CAP.

        self._vendors is already sorted by transaction count (queries.get_vendors), so
        truncating here only drops the long tail of one-off merchants, and the trailing
        row says how many and how to still reach them.
        """
        labels = [f"{v.name} ({v.count})" for v in self._vendors]
        shown = self._vendor_shown_count()
        if shown < len(labels):
            hidden = len(labels) - shown
            # 34: the sidebar's item content width (36 minus the ListView's own
            # padding) -- see test_vendor_sidebar_more_row_fits_the_sidebar_width.
            labels = labels[:shown] + [
                _truncate(f"… {hidden} more, try 'filter vendor:'", 34)
            ]
        return labels

    def _fill_txns(self, txns: List[queries.TxnRow]) -> None:
        table = self.query_one("#txns", DataTable)
        self._txns = txns
        transactions.fill_txns(table, txns, self._currencies)

    def _fill_rules(self) -> None:
        rules_panel.fill_rules(
            self.query_one("#rules", DataTable), self._rules, self._category_rules
        )

    def _fill_imports(self) -> None:
        imports_panel.fill_imports(
            self.query_one("#imports", DataTable),
            self._import_nav,
            self._import_folders,
            self._candidates,
            self._imports,
        )

    def _build_report(self) -> None:
        with self.session_factory() as session:
            self._report = stats.build_report(
                session,
                self.window,
                filters=self._active_filters().replace(date_range=None),
            )

    def _fill_stats(self) -> None:
        """Render the report, honouring folded subtrees. See stats_panel.fill_stats()."""
        table = self.query_one("#stats_table", DataTable)
        self._stats_rows, self._foldable_ids = stats_panel.fill_stats(
            table, self._report, self._collapsed
        )

    def _toggle_fold(self, row: int) -> None:
        """Space on a stats row: collapse/expand its subtree if it has one.

        A leaf row, the TOTAL row, or an out-of-range row does nothing — not a crash,
        not a notification, since space is not obviously "for" the stats table the way
        enter or the arrows are.
        """
        if not stats_panel.toggle_fold(row, self._stats_rows, self._foldable_ids, self._collapsed):
            return
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
        stats_panel.toggle_fold_all(self._foldable_ids, self._collapsed)
        self._fill_stats()
        # Collapsing/expanding everything moves rows around far more than a single
        # toggle does, so there is no single "same row" to return to — just keep the
        # cursor in range rather than landing on an arbitrary category.
        if table.row_count:
            table.move_cursor(row=min(row, table.row_count - 1))

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
                filters=self._active_filters().replace(date_range=None),
            )
            totals = queries.get_totals(
                session,
                filters=self._active_filters().replace(
                    date_range=(self.window.start, self.window.end)
                ),
            )
        self._chart = charts.build(series, measure=self._measure, width=CHART_WIDTH)
        self._chart_transfers = totals.transfer_count
        self._chart_unconverted = totals.unconverted_count

    def _fill_chart(self) -> None:
        """Redraw the table, columns included — two headers name the current measure."""
        table = self.query_one("#chart", DataTable)
        chart_panel.fill_chart(table, self._chart, self._measure, self._bucket)

    def _chart_status(self) -> str:
        """One line, under the same 92-column budget as every other panel's status."""
        return chart_panel.chart_status(
            self._chart,
            self.window,
            self._measure,
            self._bucket,
            self._chart_transfers,
            self.category_filter,
            self._categories,
            self.account_filter,
            self.vendor_filter,
            self.text_filter,
            self._chart_unconverted,
        )

    # ------------------------------------------------------------------- pie
    def _build_pie(self) -> None:
        """Fetch this window's per-bucket category series and turn it, with the
        already-built report, into the stacked share chart. See pie_panel.build_stacked().
        """
        if self.window is None:
            self._pie = None
            return
        with self.session_factory() as session:
            buckets = stats.category_share_series(
                session,
                self.window,
                self._pie_bucket,
                filters=self._active_filters().replace(date_range=None),
            )
        self._pie = pie_panel.build_stacked(self._report, buckets)

    def _fill_pie(self) -> None:
        """Render the top bar, the per-bucket bars, and their shared legend."""
        pie_panel.fill_pie(self.query_one("#pie", Static), self._pie, self.window)

    def _pie_status(self) -> str:
        """One line, the same shape as _stats_status()/_chart_status()."""
        return pie_panel.pie_status(self._report, self._pie, self._pie_bucket)

    def _fill_periods(self) -> None:
        periods_panel.fill_periods(self.query_one("#periods", DataTable))

    def _stats_status(self) -> str:
        """One line under the status budget. See stats_panel.stats_status()."""
        return stats_panel.stats_status(self._report)

    def _refresh_status(self) -> None:
        status = self.query_one("#status", Static)
        if self._panel == "stats" and self._report is not None:
            status.update(self._stats_status())
            return
        if self._panel == "chart" and self._chart is not None:
            status.update(self._chart_status())
            return
        if self._panel == "pie" and self._report is not None:
            status.update(self._pie_status())
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
        # Distinct from transfers_label on purpose (see UNCONVERTED_MARK): money is
        # missing here because a rate was never on file, not because it was excluded by
        # design, and "rates fetch" is the one thing that actually fixes it. There is
        # room to spell that out on this line (unlike stats_status and friends, which
        # already spend their whole budget on the transfer marker alone), but stacking
        # both a large transfer_count and a large unconverted_count is not — the same
        # pre-existing limit test_drill_down_status_line_fits_the_main_panel already
        # documents for filters.
        unconverted_label = (
            f"   ({totals.unconverted_count} unconverted, rates fetch)"
            if totals.unconverted_count
            else ""
        )
        self.query_one("#status", Static).update(
            f"{totals.count} txns{scope_label}{transfers_label}{unconverted_label}   "
            f"net {_fmt_amount(totals.net_minor)}   "
            f"out {_fmt_amount(totals.outflow_minor)}   "
            f"in {_fmt_amount(totals.inflow_minor)}"
        )

    # ---------------------------------------------------------------- events
    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index or 0
        list_id = event.list_view.id
        if list_id == "vendors" and index > self._vendor_shown_count():
            # The trailing "N more" row: not a vendor, just a count. See
            # VENDOR_SIDEBAR_CAP -- clicking it should not silently filter by whatever
            # real vendor happens to sit at that row index.
            self.notify(
                "That row is just a count, not a vendor. "
                "Use 'filter vendor:<text>' to find one further down the list.",
                severity="warning",
            )
            return
        # A sidebar filter is a new view; the flag it might invalidate is checked in
        # _set_drilled_from() (no-op if it was already clear).
        self._set_drilled_from(None)
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
        if event.data_table.id == "chart":
            self._drill_into_bar(event.cursor_row)
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
        """Import the file, first walking through whatever it still needs.

        A file with a built-in reader (e.g. a Wise transfer log) is recognized from its
        own columns rather than a saved layout, so ``candidate.format_name`` is a label,
        not a row in the csv_format table — there is nothing for the setup walkthrough
        to look up or ask about. Import it directly instead; import_csv already routes
        on the file's signature regardless of any format argument.
        """
        if candidate.format_name == importer.WISE_FORMAT_NAME:
            with self.session_factory() as session:
                try:
                    result = import_csv(session, candidate.path)
                except IMPORT_PROBLEMS as error:
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
        with self.session_factory() as session:
            try:
                result = import_csv(session, setup.path, account_name=setup.account_name)
            except IMPORT_PROBLEMS as error:
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
            if bucket is None and tail in _CHART_BUCKETS:
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

    def _do_pie(self, arg: str) -> None:
        """``pie`` opens the period picker; ``pie <period>`` skips it."""
        if not arg:
            self._show_periods("pie")
            return
        window = self._parse_window(arg)
        if window is not None:
            self._show_pie(window)

    def _open_period(self, window: stats.Window) -> None:
        """Send a period the picker just produced to whichever panel asked for it."""
        if self._picker_target == "chart":
            self._show_chart(window)
        elif self._picker_target == "pie":
            self._show_pie(window)
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

    def _show_pie(self, window: stats.Window) -> None:
        self.window = window
        self._range_pending = False
        self._prompt_panel = None
        self._build_report()
        self._build_pie()
        self._fill_pie()
        self._set_panel("pie")

    def _ask_range(self) -> None:
        """Ask for an explicit range, answered in the command bar below the picker."""
        self._range_pending = True
        self._prompt_panel = "periods"
        prompt = self.query_one("#prompt", Static)
        prompt.update(
            Text.assemble(
                ("Date range for the statistics\n", "bold"),
                (
                    f"Type it in the command bar below, as {periods_panel.RANGE_EXAMPLE}.  "
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
        elif name == "pie":
            self._do_pie(arg)
        elif name == "rates":
            self._do_rates(arg)
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
                f"  stats {periods_panel.RANGE_EXAMPLE})\n"
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
                "  enter, or the right arrow, on a bar lists that bucket's\n"
                "  transactions; the left arrow goes back to the chart\n"
                "pie — pick a period, then see each category's share of spending as\n"
                "  one bar for the whole window, plus one bar per bucket beneath it\n"
                "  showing the same breakdown over time, all in the same colors\n"
                "pie <period> — skip the picker (e.g. pie 6m, pie 1 year)\n"
                "  b cycles the bucket: week, month (default), year — no daily\n"
                "  only categories with real net spend get a segment — a category\n"
                "  that is all refund, or a window with no spending, draws none;\n"
                "  small categories fold into Other\n"
                "rates — list cached exchange rates (pair, source, span, count)\n"
                "rates fetch — cache ECB reference rates for every foreign currency\n"
                "  on file, over its whole date range; runs in the background so the\n"
                "  app stays responsive (an import does this on its own already)\n"
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
        import_ids: List[int] = []
        problems: List[str] = []
        with self.session_factory() as session:
            for path in paths:
                try:
                    result = import_csv(session, path)
                except IMPORT_PROBLEMS as error:
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

    def _set_drilled_from(self, origin: Optional[str]) -> None:
        """Flip the "back to stats/chart" flag, and nudge the footer to match.

        ``origin`` is ``"stats"``, ``"chart"``, or ``None`` to clear it. The footer only
        recomputes on its own when focus changes; a filter typed into the command bar
        clears this flag without moving focus, so the hint would go stale without an
        explicit refresh.
        """
        if origin == self._drill_origin:
            return
        self._drill_origin = origin
        self.screen.refresh_bindings()

    def _set_panel(self, panel: str) -> None:
        """Show one of the main-view panels; escape always returns to transactions."""
        # Leaving the drilled-down view for any other panel invalidates "back to
        # stats"/"back to chart". _drill_into_category()/_drill_into_bar() and
        # _go_back_from_drill() all set the flag to its real value themselves, after
        # calling this, so this cannot undo any of them.
        self._set_drilled_from(None)
        self._panel = panel
        for name in self.PANELS:
            self.query_one(f"#{name}").display = name == panel
        # The prompt belongs to whichever panel last raised a question.
        self.query_one("#prompt", Static).display = panel == self._prompt_panel
        if panel == "txns":
            self.query_one("#command", Input).focus()
        else:
            # "pie" is a Static with nothing to put a cursor on, but on_mount() still
            # makes it focusable so 'b' reaches action_cycle_bucket instead of being
            # typed into the command bar — Input swallows plain letter keys whenever it
            # holds focus (see test_the_chart_keys_are_inert_outside_the_chart).
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
        """The current directory, named for the status line. See imports_panel.import_label()."""
        return imports_panel.import_label(self._import_dir, TO_IMPORT_DIR)

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
        self._set_drilled_from(None)  # a new search is a new view, not the drill-down's
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

    # ------------------------------------------------------------------ rates
    def _run_rate_fetch(self, job: Callable[[Session], Optional[str]]) -> None:
        """Run ``job(session)`` off the event loop and report whatever it returns.

        ``fetch_ecb_rates`` alone can take up to 40 seconds per attempt, twice over —
        see its own docstring — so nothing that might call it runs on the UI thread.
        This is the one place that talks to a worker thread for it, shared by the
        post-import auto-fetch (``_fetch_rates_after_import``) and the ``rates fetch``
        command (``_do_rates_fetch``), so neither has to repeat the plumbing.

        ``job`` gets its own session (worker threads do not share one with the main
        thread) and returns the message to show, or ``None`` to say nothing — an import
        that turned out to need no foreign currency at all has nothing worth a
        notification. Never raises past this point: a database or network hiccup here
        must not take the whole app down, only report as "did not work".
        """

        def runner() -> None:
            try:
                with self.session_factory() as session:
                    message = job(session)
                    session.commit()
            except Exception as error:  # noqa: BLE001 - reported, not swallowed
                message = f"Rate fetch failed: {error}"
            if message:
                self.call_from_thread(self._on_rate_fetch_done, message)

        self.run_worker(runner, thread=True, group="rates", exit_on_error=False)

    def _on_rate_fetch_done(self, message: str) -> None:
        """Runs on the UI thread (via call_from_thread) once a rate fetch worker lands."""
        self.notify(message, markup=False)
        self.reload()

    def _fetch_rates_after_import(self, import_ids: List[int]) -> None:
        """Cache whatever ECB rates the import(s) that just finished need, if any.

        The decision -- which currencies, what span, whether anything is even missing
        -- is entirely rates.fetch_rates_for_import's; this only loops over the ids and
        turns its answer into a line of text. Offline or unreachable is reported, never
        raised: the import this follows has already committed and succeeded.
        """

        def job(session: Session) -> Optional[str]:
            messages = []
            for import_id in import_ids:
                outcome = rates.fetch_rates_for_import(session, import_id, queries.HOME_CURRENCY)
                if not outcome.attempted:
                    continue  # nothing but home_currency in this import
                quotes = ", ".join(outcome.quotes)
                if outcome.error is not None:
                    messages.append(
                        f"Could not fetch {queries.HOME_CURRENCY} -> {quotes} rates: "
                        f"{outcome.error} Run 'rates fetch' later."
                    )
                else:
                    messages.append(
                        f"Fetched {outcome.written} rate(s) for "
                        f"{queries.HOME_CURRENCY} -> {quotes}."
                    )
            return "\n".join(messages) if messages else None

        self._run_rate_fetch(job)

    RATES_USAGE = "Usage: rates | rates fetch"

    def _do_rates(self, arg: str) -> None:
        """Bare ``rates`` lists what is cached; ``rates fetch`` caches what is missing."""
        arg = arg.strip().lower()
        if arg in ("", "list"):
            self._notify_rates()
            return
        if arg == "fetch":
            self._do_rates_fetch()
            return
        self.notify(self.RATES_USAGE, severity="warning")

    def _notify_rates(self) -> None:
        with self.session_factory() as session:
            rows = queries.get_exchange_rates(session)
        if not rows:
            self.notify("No exchange rates cached yet. Run: rates fetch")
            return
        lines = []
        for row in rows:
            span = (
                row.first_day if row.first_day == row.last_day
                else f"{row.first_day}..{row.last_day}"
            )
            plural = "" if row.count == 1 else "s"
            lines.append(
                f"{row.base} -> {row.quote}   {row.source:<8} {span:<23} "
                f"{row.count} rate{plural}"
            )
        self.notify("\n".join(lines), title="Exchange rates", markup=False, timeout=8)

    def _do_rates_fetch(self) -> None:
        """Fetch ECB rates for every foreign currency on file, over its whole range —
        the same derivation ``budget rates fetch`` uses (queries.default_rate_fetch_span),
        so the two never drift.
        """

        def job(session: Session) -> str:
            derived = queries.default_rate_fetch_span(session, queries.HOME_CURRENCY)
            if derived is None:
                return (
                    "No transactions in the database to derive a date range from."
                )
            start, end, quotes = derived
            if not quotes:
                return f"Only one currency on file ({queries.HOME_CURRENCY}); nothing to fetch."
            try:
                written = rates.fetch_ecb_rates(
                    session, start, end, queries.HOME_CURRENCY, quotes
                )
            except rates.FrankfurterError as error:
                return f"Could not fetch ECB rates: {error}"
            return (
                f"Fetched {written} rate(s) for "
                f"{queries.HOME_CURRENCY} -> {', '.join(quotes)}."
            )

        self.notify("Fetching exchange rates…")
        self._run_rate_fetch(job)

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
        self._set_drilled_from("stats")
        self.reload()

    def _drill_into_bar(self, row: int) -> None:
        """Enter, or the right arrow, on a chart row lists the transactions behind that bar.

        The bar's own bucket becomes the date filter — clamped to the window's edges via
        charts.bucket_date_range(), since the first and last buckets are usually partial
        — intersected with whatever account/category/vendor/text filters already scope
        the chart. Without the clamp, a bucket at either edge of the window would pull in
        transactions the chart never drew, and the drilled-down rows would not sum back
        to the bar just clicked.

        Unlike a statistics drill-down this never touches the category filter — a bar is
        a slice of time, not of category — so _pre_drill_category_filter just records the
        filter already in place, and going back "restores" it as a no-op.
        """
        if self._chart is None or self.window is None or self._bucket is None:
            return
        if not 0 <= row < len(self._chart.bars):
            return
        bar = self._chart.bars[row]
        self._pre_drill_category_filter = self.category_filter
        self._pre_drill_date_filter = self.date_filter
        self._drill_source_row = row
        self.date_filter = charts.bucket_date_range(bar.key, self._bucket, self.window)
        # Panel first: reload() only rebuilds the chart while the chart panel is up, and
        # rebuilding it under the new date filter would rewrite the bars we just read.
        self._set_panel("txns")
        self._set_drilled_from("chart")
        self.reload()

    def _go_back_from_drill(self) -> None:
        """Left arrow, undoing exactly the drill-down that produced this view.

        Mirrors _drill_into_category()/_drill_into_bar(): restores the filters either one
        overwrote (which may be None, or may be a filter the user had set before drilling
        in), rebuilds whichever panel the drill-down came from, and returns its cursor to
        the row that was drilled from.
        """
        origin = self._drill_origin
        row = self._drill_source_row
        self.category_filter = self._pre_drill_category_filter
        self.date_filter = self._pre_drill_date_filter
        self._pre_drill_category_filter = None
        self._pre_drill_date_filter = None
        self._drill_source_row = None
        self._set_drilled_from(None)
        # reload() while the panel is still "txns" resyncs the transactions/totals to the
        # restored filters without rebuilding the report or chart (see their own guards),
        # so whichever one is being returned to is rebuilt explicitly below, the same way
        # _show_stats()/_show_chart() does.
        self.reload()
        if origin == "chart":
            self._build_chart()
            self._fill_chart()
            self._set_panel("chart")
            if row is not None:
                table = self.query_one("#chart", DataTable)
                if 0 <= row < table.row_count:
                    table.move_cursor(row=row)
            return
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
        # Index 0 is the "— All —" row, so the list is offset by one. Bounded by what is
        # actually mounted (see VENDOR_SIDEBAR_CAP), not the full vendor count, or this
        # would resolve the trailing "N more" row to whatever real vendor happens to sit
        # at that index.
        index = self.query_one("#vendors", ListView).index or 0
        if not 1 <= index <= self._vendor_shown_count():
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
        """The right arrow's twin of enter on a statistics row or a chart bar."""
        if self._panel == "chart":
            table = self.query_one("#chart", DataTable)
            self._drill_into_bar(table.cursor_row)
            return
        table = self.query_one("#stats_table", DataTable)
        self._drill_into_category(table.cursor_row)

    def action_drill_up(self) -> None:
        """The left arrow's "back" out of a statistics or chart drill-down."""
        self._go_back_from_drill()

    def action_toggle_stats_fold(self) -> None:
        """Space on a statistics row: fold/unfold its subtree. See check_action()."""
        table = self.query_one("#stats_table", DataTable)
        self._toggle_fold(table.cursor_row)

    def action_toggle_all_stats_folds(self) -> None:
        """``f`` on the statistics table: fold/unfold every group. See check_action()."""
        self._toggle_fold_all()

    def action_cycle_bucket(self) -> None:
        """``b``: on the chart, step day → week → month → day; on the pie, step
        week → month → year → week. See check_action() for which panel gets which.

        A cycle rather than three keys, and it does not skip a bucket that would be
        unwieldy for the window: charting two years by day is a bad idea but it is the
        user's to make, and a key that silently refuses to do anything is worse.
        """
        if self._panel == "pie":
            if self.window is None:
                return
            order = _SHARE_BUCKETS
            self._pie_bucket = order[(order.index(self._pie_bucket) + 1) % len(order)]
            self._redraw_pie()
            return
        if self.window is None or self._bucket is None:
            return
        order = _CHART_BUCKETS
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

    def _redraw_pie(self) -> None:
        """The report is unaffected by which bucket is charted, so only the per-bucket
        series and the stacked chart built from it need rebuilding."""
        self._build_pie()
        self._fill_pie()
        self._refresh_status()

    def action_rename_vendor(self) -> None:
        self._prefill_for_vendor("rename")

    def action_categorize_vendor(self) -> None:
        self._prefill_for_vendor("categorize")

    def action_show_transactions(self) -> None:
        # Escape is the general-purpose "leave this view" key, so it drops the
        # drill-down's back-link even when the panel is already "txns".
        self._set_drilled_from(None)
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
        self._set_drilled_from(None)
        self.account_filter = None
        self.vendor_filter = None
        self.category_filter = None
        self.text_filter = None
        self.date_filter = None
        self.reload()
        self.notify("Filters cleared.")


def run() -> None:
    BudgetApp().run()
