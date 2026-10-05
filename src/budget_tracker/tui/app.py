"""Full-screen Textual TUI for the budget tracker.

Layout: an accordion sidebar (accounts, vendors, categories, tags, trips -- one
section open at a time; click a row to filter), a scrollable transactions table, a
totals line, and a command bar at the bottom.

``BudgetApp`` itself holds only what is genuinely cross-cutting: its CSS, bindings,
``compose()``/``on_mount()``, the sidebar/filter/reload plumbing, and the command
dispatcher. What each command actually *does* lives one family per module under
``tui/commands/`` (selection, rules, categories, transfers, imports, filters, rates,
sync, trips, the period picker, stats, chart, pie, drill-down, events, and key-binding
actions) -- each a plain mixin class, composed onto ``BudgetApp`` below. A method's
name and the ``self.`` state it reads/writes are unchanged by which module it lives
in, so this is purely an organizational split: ``app._do_sel(...)``,
``app._show_stats(...)``, ``BudgetApp.SORT_USAGE``, and so on all still work exactly as
before.

None of those mixins override a Textual ``App`` attribute or method -- watch for that
if you add one. A past bug named a method ``_filters``, which shadowed ``App``'s own
attribute of that name (a list of line filters) and broke every test; the active
filters are ``self._active_filters()`` here for exactly that reason.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple, Union

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.events import Click
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

# import_csv and IMPORT_PROBLEMS are not called from this module -- ImportCommands
# (commands/imports.py) does the importing. Both are re-exported here, as plain module
# attributes, because the tests patch and import them as
# `budget_tracker.tui.app.import_csv`/`...IMPORT_PROBLEMS` (a pre-existing fixture this
# split does not get to change), and ImportCommands reads them back off this live
# module object at call time precisely so that patch takes effect -- see
# commands/imports.py's `_app_module()`.
from .. import budget as budget_module
from .. import charts, models, queries, stats, tags as tags_module
from ..db import DuplicateCategoryNamesError, get_engine, get_sessionmaker, init_db
from ..importer import ImportCandidate, InboxFolder, import_csv  # noqa: F401
from . import budget as budget_panel
from . import periods as periods_panel
from . import transactions
from . import trips as trips_panel
from .commands.actions import ActionCommands
from .commands.budget import BudgetCommands
from .commands.categories import CategoryCommands
from .commands.chart import ChartCommands
from .commands.drilldown import DrillDownCommands
from .commands.events import EventCommands
from .commands.filters import FilterCommands
from .commands.imports import IMPORT_PROBLEMS, ImportCommands, TO_IMPORT_DIR  # noqa: F401
from .commands.periods import PeriodPickerCommands
from .commands.pie import PieCommands
from .commands.rates import RatesCommands
from .commands.rules import RuleCommands
from .commands.selection import SelectionCommands
from .commands.stats import StatsCommands
from .commands.sync import SyncCommands
from .commands.transfers import TransferCommands
from .commands.trips import TripCommands
from .formatting import _fmt_amount, _range_label, _truncate
from .imports import _Setup


class BudgetApp(
    SelectionCommands,
    RuleCommands,
    CategoryCommands,
    TransferCommands,
    ImportCommands,
    FilterCommands,
    RatesCommands,
    SyncCommands,
    TripCommands,
    BudgetCommands,
    PeriodPickerCommands,
    StatsCommands,
    ChartCommands,
    PieCommands,
    DrillDownCommands,
    EventCommands,
    ActionCommands,
    App,
):
    CSS = """
    #sidebar { width: 36; }
    #accounts, #vendors, #categories, #tags, #trips {
        border: round $accent; height: 1fr;
    }
    #txns, #rules, #imports, #setup, #periods { border: round $accent; height: 1fr; }
    #stats { height: 1fr; }
    #stats_table, #chart { border: round $accent; height: 1fr; }
    #pie { border: round $accent; height: 1fr; padding: 1; }
    #trips_view { height: 1fr; }
    #trip_table { border: round $accent; height: 1fr; }
    #trips_legend { height: auto; padding: 0 1; color: $text-muted; }
    #budget_track, #budget_plan { border: round $accent; height: 1fr; }
    #prompt { height: auto; padding: 1 1 0 1; color: $accent; }
    #status { height: 1; padding: 0 1; color: $text-muted; background: $panel; }
    #command { border: tall $accent; }
    .heading { padding: 0 1; text-style: bold; color: $accent; }
    /* Accounts sidebar rows, colored by last sync status (see queries.AccountRow and
    reload()'s build of the #accounts list) -- $success/$error rather than a
    hard-coded color, so both read correctly in light and dark themes. */
    #accounts ListItem.sync-ok Label { color: $success; }
    #accounts ListItem.sync-error Label { color: $error; }
    """

    PANELS = (
        "txns", "rules", "imports", "setup", "periods", "stats", "chart", "pie", "trips",
        "budget_track", "budget_plan",
    )

    # The sidebar's five collapsible sections, in the order they are stacked. Exactly
    # one is expanded (its ListView shown) at a time; the rest collapse to their
    # heading -- see _expand_section(). Accounts, vendors and categories were already
    # crowded on their own; tags and trips only made a fixed-height sidebar worse, which
    # is the entire reason this is an accordion rather than five plain lists.
    SECTIONS = ("accounts", "vendors", "categories", "tags", "trips")
    SECTION_TITLES = {
        "accounts": "Accounts",
        "vendors": "Vendors",
        "categories": "Categories",
        "tags": "Tags",
        "trips": "Trips",
    }
    # Panels whose widget is not itself focusable name the child that takes focus.
    PANEL_FOCUS = {"stats": "#stats_table", "trips": "#trip_table"}
    # Panels whose *container* id cannot just be "#<panel name>": "trips" would
    # collide with the sidebar's own #trips ListView (see SECTIONS/compose()), so its
    # container is #trips_view instead. Every other panel's container id is still just
    # its own name -- see _set_panel()'s display-toggle loop, the one place this is
    # read.
    PANEL_CONTAINER = {"trips": "trips_view"}

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
        ("ctrl+n", "rename_vendor", "Rename vendor"),
        ("ctrl+t", "categorize_vendor", "Categorize"),
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
        # z folds too, vim's own fold key; same action and gate as space.
        Binding("z", "toggle_stats_fold", "Fold", show=False),
        # Same reasoning as space: a plain letter binding, not priority, so a command
        # like "filter foo" still gets its 'f' typed into #command rather than toggling
        # every fold in the stats table out from under the user.
        Binding("f", "toggle_all_stats_folds", "Fold all", show=True),
        # Same again: a plain letter, gated to the chart panel by check_action, so 'b'
        # typed into a command is still just a letter.
        Binding("b", "cycle_bucket", "Bucket", show=True),
        Binding("m", "cycle_measure", "Measure", show=True),
        # Same shape again: a plain letter, gated to the budget plan panel by
        # check_action, so 'n' typed into a command is still just a letter.
        Binding("n", "cycle_budget_months", "Months", show=True),
        # Same shape again: a plain letter, gated to the transactions table by
        # check_action, so 'x' typed into a command is still just a letter.
        Binding("x", "toggle_selected", "Select", show=True),
        # Vim-style movement, in the same shape as the letters above: plain bindings, so
        # the command bar still types them, and gated by check_action to a focused table
        # or sidebar list. h/l do whatever the arrows do in that spot (drill in and back
        # out of statistics, say), so the two never mean different things. Not shown in
        # the footer: it is full already, and anyone reaching for hjkl knows them.
        Binding("j", "vim_down", "Down", show=False),
        Binding("k", "vim_up", "Up", show=False),
        Binding("h", "vim_left", "Left", show=False),
        Binding("l", "vim_right", "Right", show=False),
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
        self.tag_filter: Optional[int] = None
        self.trip_filter: Optional[int] = None
        # Only a trips-panel bucket drill-down sets this: an explicit set of category
        # ids (a bucket is several unrelated categories at once, not a subtree -- see
        # queries.Filters.category_ids). None means no bucket restriction.
        self.category_ids_filter: Optional[Tuple[Optional[int], ...]] = None
        self._accounts: List[queries.AccountRow] = []
        self._vendors: List[queries.VendorRow] = []
        self._categories: List[queries.CategoryRow] = []
        # Zero-count tags/trips are kept (see queries.get_tags), unlike categories --
        # a freshly created trip with nothing on it yet still needs a sidebar row.
        self._tags: List[queries.TagRow] = []
        self._trips: List[queries.TagRow] = []
        # Which sidebar section is expanded; the rest collapse to their heading. See
        # _expand_section() and SECTIONS.
        self._expanded_section: str = "accounts"
        # By ISO code, so a per-row currency (queries.TxnRow.currency) can be formatted
        # in its own decimal places and symbol rather than always assuming two decimal
        # places — see _fmt_amount_for().
        self._currencies: Dict[str, queries.CurrencyRow] = {}
        # Last labels (or, for #accounts, (label, css_class, tooltip) triples) rendered
        # into each sidebar list, so _fill_list can skip a rebuild that would produce
        # exactly what is already on screen.
        self._list_labels: Dict[str, List[Union[str, Tuple[str, str, Optional[str]]]]] = {}
        # Parallel to the rows in #txns, so a cursor index maps back to a transaction.
        self._txns: List[queries.TxnRow] = []
        # Multi-select on the transactions table: transaction *ids*, not row indices,
        # because reload() re-queries and re-renders -- a row index would point at
        # whatever happened to land there after a filter changed. Pruned in
        # _fill_txns() to whatever is still in self._txns, so a selection never
        # silently outlives the rows it was made on.
        self._selected_ids: Set[int] = set()
        # The filters #txns was last filled under. A refill under the same filters is an
        # edit (a rule, a category, a rename) and keeps the user's place; a refill under
        # different ones is a new view and starts at the top. See _fill_txns.
        self._txns_filters: Optional[queries.Filters] = None
        self._txns_order = queries.ORDER_DATE
        # Set by `sort size` to the filters it was issued under. The size order belongs
        # to that one view: any filter change, or leaving the transactions panel, puts
        # the list back in date order (see _txn_order and _set_panel).
        self._size_sort_filters: Optional[queries.Filters] = None
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
        # commands/pie.py's _SHARE_BUCKETS. Monthly by default; sticky across a new
        # period the same way the chart's measure is, not re-derived from the window's
        # length.
        self._pie_bucket = "month"
        # The trips panel's own data (dates, cost, bucket breakdown) -- not scoped by
        # the app's other filters, since queries.get_trips takes none: a trip is
        # already its own scope. See _build_trips().
        self._trip_data: List[queries.TripRow] = []
        # Parallel to the rendered rows of #trip_table, bucket sub-rows included -- see
        # trips_panel.TripPanelRow and _fill_trips().
        self._trip_rows: List[trips_panel.TripPanelRow] = []
        # Which trips are currently unfolded, and which trip ids are foldable at all
        # (every one of them -- see trips_panel's own module docstring for why this is
        # a deliberately separate Set from the statistics panel's _collapsed/
        # _foldable_ids rather than a shared one).
        self._trips_expanded: Set[int] = set()
        self._trips_foldable_ids: Set[int] = set()
        # The tracking panel's month and last-built view, plus which stored month it
        # actually came from (None if nothing was ever budgeted; a different month if
        # this one copied forward -- see tui/budget.track_status()).
        self._budget_month: Optional[date] = None
        self._budget_track_view: Optional[budget_module.TrackView] = None
        self._budget_track_source_month: Optional[date] = None
        # Parallel to the rendered rows of #budget_track -- not yet used to map a
        # cursor back to anything (the tracking panel has no per-row action), kept for
        # symmetry with the plan panel and any future drill-down.
        self._budget_track_rows: List[budget_panel.TrackPanelRow] = []
        # Same shape for the plan panel, plus its own averaging window (see
        # commands/budget.py's action_cycle_budget_months) and the rows enter actually
        # edits -- see budget_panel.PlanPanelRow and _edit_budget_row().
        self._budget_plan_month: Optional[date] = None
        # commands/budget.py's DEFAULT_PLAN_MONTHS -- not imported for one constant;
        # _do_budget_plan() always passes an explicit N anyway, so this is only ever
        # seen before the panel has been opened once.
        self._budget_plan_months: int = 6
        self._budget_plan_view: Optional[budget_module.PlanView] = None
        self._budget_plan_source_month: Optional[date] = None
        self._budget_plan_rows: List[budget_panel.PlanPanelRow] = []
        # Plan rows folded with z, by category id -- kept across months and refreshes,
        # like the statistics panel's own folds.
        self._budget_plan_collapsed: Set[int] = set()
        # Awaiting a typed amount for the plan row enter opened -- see
        # commands/budget.py's _edit_budget_row/_answer_budget_edit.
        self._pending_budget_edit: Optional[budget_panel.BudgetEditTarget] = None
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
        # Only a trips drill-down (into a trip, or into one of its buckets) touches
        # these two -- a stats/chart drill-down leaves them recording their own
        # unchanged value, so _go_back_from_drill can restore all four unconditionally
        # rather than knowing which drill-down set which.
        self._pre_drill_trip_filter: Optional[int] = None
        self._pre_drill_category_ids_filter: Optional[Tuple[Optional[int], ...]] = None
        self._drill_source_row: Optional[int] = None  # table row to land back on

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="sidebar"):
                # Headings start collapsed except Accounts (see on_mount()'s call to
                # _expand_section, which also fills in the real "▼"/"▶" text and any
                # collapsed filter summary) -- the literal text here is only what shows
                # for the instant before that runs.
                yield Static("Accounts", id="head_accounts", classes="heading")
                yield ListView(id="accounts")
                yield Static("Vendors", id="head_vendors", classes="heading")
                yield ListView(id="vendors")
                yield Static("Categories", id="head_categories", classes="heading")
                yield ListView(id="categories")
                yield Static("Tags", id="head_tags", classes="heading")
                yield ListView(id="tags")
                yield Static("Trips", id="head_trips", classes="heading")
                yield ListView(id="trips")
            with Vertical(id="main"):
                yield transactions.TxnTable(id="txns")
                yield DataTable(id="rules")
                yield DataTable(id="imports")
                yield Static("", id="prompt")
                yield DataTable(id="setup")
                yield DataTable(id="periods")
                with Vertical(id="stats"):
                    yield DataTable(id="stats_table")
                yield DataTable(id="chart")
                yield Static("", id="pie")
                with Vertical(id="trips_view"):
                    # The legend goes above the table: the bars are the thing being
                    # read, and a key underneath them is one the eye has to travel
                    # past the whole table to find and then back again.
                    yield Static("", id="trips_legend")
                    yield trips_panel.TripTable(id="trip_table")
                yield DataTable(id="budget_track")
                yield DataTable(id="budget_plan")
                yield Static("", id="status")
        yield Input(
            placeholder=(
                "command: import | unimport | filter | categorize | category | sel | "
                "section | format | stats | chart | pie | trips | budget | rates | "
                "sync | sort | rules | all | refresh | help | quit"
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
            return self._panel in ("stats", "chart", "trips")
        if action == "drill_up":
            return self._drill_origin is not None
        if action in ("toggle_stats_fold", "toggle_all_stats_folds"):
            # Same keys fold the trips panel's per-trip buckets -- see
            # action_toggle_stats_fold()/action_toggle_all_stats_folds().
            if self._panel == "trips":
                return self.focused is not None and self.focused.id == "trip_table"
            if self._panel == "budget_plan":
                return self.focused is not None and self.focused.id == "budget_plan"
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
        if action in ("vim_down", "vim_up", "vim_left", "vim_right"):
            return isinstance(self.focused, (DataTable, ListView))
        if action == "cycle_budget_months":
            return (
                self._panel == "budget_plan"
                and self.focused is not None
                and self.focused.id == "budget_plan"
            )
        if action == "toggle_selected":
            return (
                self._panel == "txns"
                and self.focused is not None
                and self.focused.id == "txns"
            )
        return True

    def on_click(self, event: Click) -> None:
        """Clicking a sidebar heading expands its section, collapsing the rest.

        ``Click`` always bubbles to the App if nothing below it stops it, and a plain
        ``Static`` never does, so this is the one place that needs to know about the
        five ``#head_<section>`` widgets at all.
        """
        widget = event.widget
        if widget is None or widget.id is None:
            return
        if widget.id.startswith("head_"):
            self._expand_section(widget.id[len("head_") :])

    def on_mount(self) -> None:
        self.title = "Budget Tracker"
        table = self.query_one("#txns", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        # A row the multi-select feature can act on: "✓" when it is in
        # self._selected_ids, blank otherwise. Headed rather than blank so the column
        # announces what it is; 3 wide because "Sel" is, and a 2-wide column would
        # clip its own heading to "Se".
        table.add_column("Sel", width=3)
        table.add_column("Date", width=10)
        table.add_column("Description", width=30)
        table.add_column("Vendor", width=20)
        table.add_column("Category", width=16)
        # Wide enough for a three-character symbol (CHF) plus a signed six-figure
        # amount ("CHF-999,999.99" is 14) with a column to spare — see
        # test_txns_amount_column_fits_a_symbol_and_a_six_figure_amount.
        table.add_column("Amount", width=15)
        table.add_column("Account", width=18)
        # The trip (if any) then ordinary tags, from queries.TxnRow.trip/tags -- see
        # transactions._tags_cell(). 30 rather than 22 because a single trip and a
        # single tag ("✈Japan 2026 #reimbursable") is already 25 characters, and that
        # is the ordinary case rather than a worst case.
        table.add_column("Tags", width=transactions.TAGS_COLUMN_WIDTH)
        # 3 + 10 + 30 + 20 + 16 + 15 + 18 + 30 = 142, plus 2 columns of padding each
        # (16) = 158. The main panel is ~175 wide at the terminal size the user
        # actually runs (213 columns), not the 130-wide test default, so this fits
        # with room to spare -- see the 2026-08-08 spec correction that replaced the
        # earlier 92-column budget.

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

        trip_table = self.query_one("#trip_table", DataTable)
        trip_table.cursor_type = "row"
        trip_table.zebra_stripes = True
        # Columns are added in trips_panel.fill_trips, not here: the Breakdown column's
        # width is adaptive (see trips_panel.bar_width), so it is only known once the
        # panel's actual width is.

        budget_track = self.query_one("#budget_track", DataTable)
        budget_track.cursor_type = "row"
        budget_track.zebra_stripes = True
        # Columns are added in budget_panel.fill_track, not here: the Used column's
        # width is adaptive, same reason as the trips table's Breakdown column above.
        budget_track.display = False

        budget_plan = self.query_one("#budget_plan", DataTable)
        budget_plan.cursor_type = "row"
        budget_plan.zebra_stripes = True
        # Columns are added in budget_panel.fill_plan, not here: the Avg/mo header
        # names the current averaging window, which changes with 'n'.
        budget_plan.display = False

        self.query_one("#stats", Vertical).display = False
        chart.display = False
        pie = self.query_one("#pie", Static)
        pie.display = False
        # A plain Static cannot take focus by default; it needs to here so 'b' reaches
        # action_cycle_bucket instead of being typed into the command bar (see
        # _set_panel()).
        pie.can_focus = True
        self.query_one("#trips_view", Vertical).display = False
        self.query_one("#prompt", Static).display = False

        # Accounts starts expanded; every other section collapses to its heading. See
        # SECTIONS and _expand_section().
        self._expand_section("accounts")

        self.reload()
        self.query_one("#command", Input).focus()

    # ------------------------------------------------------------------ data
    def _active_filters(self) -> queries.Filters:
        """The app's active filters as one value.

        These always travel together and always mean the same thing; passing them one
        at a time is what made adding a sixth touch every signature. Named
        ``_active_filters`` rather than the obvious ``_filters`` because Textual's own
        ``App`` already owns that attribute -- a list of line filters -- and shadowing it
        replaces the method with a list the moment the app initializes. A caller that
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
            tag_id=self.tag_filter,
            trip_id=self.trip_filter,
            category_ids=self.category_ids_filter,
        )

    def reload(self) -> None:
        with self.session_factory() as session:
            self._accounts = queries.get_accounts(session)
            self._vendors = queries.get_vendors(session)
            self._categories = queries.get_categories(session)
            self._tags = queries.get_tags(session, kind=tags_module.TAG)
            self._trips = queries.get_tags(session, kind=tags_module.TRIP)
            self._currencies = {c.code: c for c in queries.get_currencies(session)}
            txns = queries.get_transactions(
                session,
                filters=self._active_filters(),
                order=self._txn_order(),
            )
            totals = queries.get_totals(
                session,
                filters=self._active_filters(),
            )
        self._fill_list("#accounts", self._account_items())
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
        self._fill_list("#tags", [f"{t.name} ({t.count})" for t in self._tags])
        self._fill_list("#trips", [f"{t.name} ({t.count})" for t in self._trips])
        self._fill_txns(txns)
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
        # The trips panel is not scoped by the app's other filters (queries.get_trips
        # takes none -- a trip is already its own scope), so this only needs to notice
        # the database changed, not any filter above.
        if self._panel == "trips":
            self._build_trips()
            self._fill_trips()
        # Same idea for both budget panels: neither is scoped by the app's other
        # filters (budget.track()/plan_rows() take none), so this only needs to
        # notice the database changed while one of them is actually on screen.
        if self._panel == "budget_track":
            self._build_budget_track()
            self._fill_budget_track()
        if self._panel == "budget_plan":
            self._build_budget_plan()
            self._fill_budget_plan()
        # The rules panel's data comes from get_rules/get_category_rules, which match
        # every rule against every vendor -- not cheap, and not needed at all when the
        # panel is hidden, so (like stats/chart/pie/trips above) it is only rebuilt
        # while actually on screen. _show_rules builds it once more, itself, when the
        # panel first opens.
        if self._panel == "rules":
            self._build_rules()
            self._fill_rules()
        # A collapsed heading's summary names whichever filter is active on it, so it
        # has to be redrawn whenever a filter (or the sidebar's own contents) changes --
        # not just when a section expands or collapses.
        self._update_section_headings()
        self._refresh_status()

    def _fill_list(
        self, selector: str, items: List[Union[str, Tuple[str, str, Optional[str]]]]
    ) -> None:
        """Render a sidebar list, but only when its contents have actually changed.

        reload() runs on every filter change and every statistics drill-down, and none of
        those touch the sidebar: get_accounts/get_vendors/get_categories take no filter
        arguments, so their output depends on the database alone. Rebuilding anyway cost
        more than everything else in a drill-down put together — the vendor list runs to
        several hundred rows, and mounting that many widgets is far dearer than the query
        behind it. Comparing the labels first is cheap, correct whatever the caller
        wanted, and keeps the list's scroll position across a drill.

        ``items`` is ordinarily a plain label per row. The accounts list is the one
        exception: it passes ``(label, css_class, tooltip)`` triples instead, so a sync
        status that changes without the label changing (same name, same count) still
        rebuilds -- the memo compares ``items`` as given, and the triple folds the
        status and its tooltip into the comparison along with the text.

        extend() mounts the items in one pass; appending in a loop mounts them one at a
        time and is several times slower on a list this long.
        """
        if self._list_labels.get(selector) == items:
            return
        self._list_labels[selector] = list(items)
        list_view = self.query_one(selector, ListView)
        list_view.clear()
        rows = [ListItem(Label("— All —"))]
        for item in items:
            if isinstance(item, tuple):
                label, css_class, tooltip = item
                list_item = ListItem(Label(label))
                if css_class:
                    list_item.add_class(css_class)
                if tooltip:
                    list_item.tooltip = tooltip
            else:
                list_item = ListItem(Label(item))
            rows.append(list_item)
        list_view.extend(rows)

    def _expand_section(self, name: str) -> None:
        """Expand ``name``'s sidebar section, collapsing every other one.

        Only ``display`` moves -- the ListView itself, its rows, its cursor and its
        scroll position are untouched, so collapsing and re-expanding a section leaves
        it exactly as the user left it. See SECTIONS for the five valid names.
        """
        if name not in self.SECTIONS:
            return
        self._expanded_section = name
        for section in self.SECTIONS:
            self.query_one(f"#{section}", ListView).display = section == name
        self._update_section_headings()

    def _section_filter_label(self, section: str) -> Optional[str]:
        """The name of whatever ``section``'s filter is currently scoped to, if any.

        Used only by a *collapsed* heading -- see _update_section_headings() -- so a
        filter set from a section the user has since closed is still visible rather
        than silently forgotten.
        """
        if section == "accounts" and self.account_filter is not None:
            return next(
                (a.name for a in self._accounts if a.id == self.account_filter), None
            )
        if section == "vendors" and self.vendor_filter is not None:
            kind, vendor_id = self.vendor_filter
            return next(
                (v.name for v in self._vendors if (v.kind, v.id) == (kind, vendor_id)),
                None,
            )
        if section == "categories" and self.category_filter is not None:
            return next(
                (c.name for c in self._categories if c.id == self.category_filter), None
            )
        if section == "tags" and self.tag_filter is not None:
            return next((t.name for t in self._tags if t.id == self.tag_filter), None)
        if section == "trips" and self.trip_filter is not None:
            return next((t.name for t in self._trips if t.id == self.trip_filter), None)
        return None

    def _update_section_headings(self) -> None:
        """Redraw every heading: "▼ Section" expanded, "▶ Section" collapsed, or
        "▶ Section — Filter" collapsed with a filter still active on it.

        That summary is the entire point of collapsing a section -- a filter the user
        cannot see is a filter they will not remember they set. Truncated to the
        sidebar's own content width (36 minus the heading's own padding).

        ``Text()``, not a plain string: a vendor, category, or tag name is user data
        and may hold brackets that ``Static``'s default markup parsing would silently
        eat (the same reason ``notify()`` calls elsewhere pass ``markup=False``).
        """
        for section in self.SECTIONS:
            title = self.SECTION_TITLES[section]
            if section == self._expanded_section:
                text = f"▼ {title}"
            else:
                filter_label = self._section_filter_label(section)
                text = f"▶ {title} — {filter_label}" if filter_label else f"▶ {title}"
            self.query_one(f"#head_{section}", Static).update(Text(_truncate(text, 34)))

    def _account_items(self) -> List[Tuple[str, str, Optional[str]]]:
        """Rows for the accounts sidebar, colored by each account's last sync status.

        green (``.sync-ok``) on success, red (``.sync-error``) on failure, no class
        (the default theme color) when it is not synced or has no status yet -- see
        queries.AccountRow.sync_status and models.SYNC_OK/SYNC_ERROR. A failing
        account also carries its ``sync_error`` as a tooltip, since "this one is red"
        is not itself an explanation.
        """
        items = []
        for account in self._accounts:
            if account.sync_status == models.SYNC_OK:
                css_class = "sync-ok"
                tooltip = None
            elif account.sync_status == models.SYNC_ERROR:
                css_class = "sync-error"
                tooltip = account.sync_error
            else:
                css_class = ""
                tooltip = None
            items.append((f"{account.name} ({account.count})", css_class, tooltip))
        return items

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
            # 32: the sidebar's item content width once its own round border (1 column
            # each side, now on every section -- see SECTIONS) and a vertical
            # scrollbar (1 column, always present once the list is this long) are
            # taken out of the 36-wide sidebar -- see
            # test_vendor_sidebar_more_row_fits_the_sidebar_width.
            labels = labels[:shown] + [
                _truncate(f"… {hidden} more, try 'filter vendor:'", 32)
            ]
        return labels

    def _fill_txns(self, txns: List[queries.TxnRow]) -> None:
        """Refill #txns, keeping the cursor on the same transaction across an edit.

        Every command that writes (a rule, a category, a rename, a bulk edit) ends in
        reload(), and clearing the table sends the cursor and scroll back to the top --
        so editing row 300 meant scrolling back down to row 300 for the next one. When
        the filters are unchanged the cursor returns to the same transaction, at the
        same height on screen; if that row has left the view (re-categorized out of a
        category filter, say) it stays at the same index, i.e. on the next row down.
        A changed filter is a different list, where the top is the right place to be.
        """
        table = self.query_one("#txns", DataTable)
        filters = self._active_filters()
        order = self._txn_order()
        # A re-sort is a new list too: starting at the top shows its largest rows.
        same_view = (
            filters == self._txns_filters
            and order == self._txns_order
            and table.row_count > 0
        )
        old_row, old_column = table.cursor_row, table.cursor_column
        old_id = self._txns[old_row].id if 0 <= old_row < len(self._txns) else None
        screen_offset = old_row - int(table.scroll_y)

        self._txns = txns
        self._txns_filters = filters
        self._txns_order = order
        # Drop any selected id no longer among the rows just fetched -- see
        # self._selected_ids.
        self._selected_ids &= {txn.id for txn in txns}
        transactions.fill_txns(table, txns, self._currencies, self._selected_ids)

        if not same_view or not txns:
            return
        row = next((i for i, txn in enumerate(txns) if txn.id == old_id), None)
        if row is None:
            row = min(old_row, len(txns) - 1)
        table.move_cursor(row=row, column=old_column, scroll=False)
        table.scroll_to(y=max(0, row - screen_offset), animate=False)

    def _toggle_txn_selected(self, row: int) -> None:
        """Toggle the row under ``row`` in and out of the selection.

        Used by both the ``x`` key and clicking/entering a row (see
        ``on_data_table_row_selected``). Updates just the select column's cell rather
        than re-rendering the whole table, so the cursor and scroll position the user
        is looking at do not move.
        """
        if not 0 <= row < len(self._txns):
            return
        txn_id = self._txns[row].id
        if txn_id in self._selected_ids:
            self._selected_ids.discard(txn_id)
            mark = ""
        else:
            self._selected_ids.add(txn_id)
            mark = transactions.SELECTED_MARK
        table = self.query_one("#txns", DataTable)
        table.update_cell_at(Coordinate(row, 0), mark)
        self._refresh_status()

    def _render_selection(self) -> None:
        """Redraw the select column and status line from ``self._selected_ids``.

        For commands (``sel all`` / ``sel none``) rather than a single row: no query
        needed, ``self._txns`` already holds every row currently on screen.
        """
        self._fill_txns(self._txns)
        self._refresh_status()

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
        if self._panel == "trips":
            status.update(trips_panel.trips_status(self._trip_data))
            return
        if self._panel == "budget_track" and self._budget_track_view is not None:
            status.update(self._budget_track_status())
            return
        if self._panel == "budget_plan" and self._budget_plan_view is not None:
            status.update(self._budget_plan_status())
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
                f"{owned} txns categorized   "
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
        if self.tag_filter is not None:
            scope.append("tag")
        if self.trip_filter is not None:
            scope.append("trip")
        if self.category_ids_filter is not None:
            # Set only by a trips-panel bucket drill-down (see _drill_into_trip_row) --
            # "bucket" rather than "category" so it reads as the distinct thing it is,
            # the same reason the date-filter branch below spells out a real range
            # rather than reusing the word "date".
            scope.append("bucket")
        if self.date_filter is not None:
            # Spelled out rather than labeled "date": the drill-down from a statistics
            # row is the only thing that sets it, and the user needs to see which window
            # they landed in to reconcile the numbers they just clicked.
            scope.append(_range_label(self.date_filter))
        if self.text_filter is not None:
            scope.append(f'{self.text_filter.field}~"{self.text_filter.text}"')
        scope_label = f" [filtered: {', '.join(scope)}]" if scope else ""
        if self._size_sort_filters is not None:
            scope_label += "  by size"
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
        # Only when the selection is non-empty, so a line with nothing selected is
        # byte-for-byte what it was before multi-select existed.
        selected_label = (
            f"   {len(self._selected_ids)} selected" if self._selected_ids else ""
        )
        self.query_one("#status", Static).update(
            f"{totals.count} txns{scope_label}{transfers_label}{unconverted_label}   "
            f"net {_fmt_amount(totals.net_minor)}   "
            f"out {_fmt_amount(totals.outflow_minor)}   "
            f"in {_fmt_amount(totals.inflow_minor)}{selected_label}"
        )

    def _set_panel(self, panel: str) -> None:
        """Show one of the main-view panels; escape always returns to transactions."""
        # Leaving the transactions puts them back in date order, so returning finds the
        # default view. Refilled now, while the table is still the panel on show, so the
        # reload does not also rebuild whichever panel is being opened.
        if panel != "txns" and self._size_sort_filters is not None:
            self._size_sort_filters = None
            self.reload()
        # Leaving the drilled-down view for any other panel invalidates "back to
        # stats"/"back to chart"/"back to trips". _drill_into_category()/
        # _drill_into_bar()/_drill_into_trip_row() and _go_back_from_drill() all set
        # the flag to its real value themselves, after calling this, so this cannot
        # undo any of them.
        self._set_drilled_from(None)
        self._panel = panel
        for name in self.PANELS:
            container_id = self.PANEL_CONTAINER.get(name, name)
            self.query_one(f"#{container_id}").display = name == panel
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
        if self._pending_budget_edit is not None:
            self._answer_budget_edit(text)
            return
        self._run_command(text.strip())

    def _do_refresh(self, arg: str) -> None:
        self.reload()
        self.notify("Refreshed.")

    def _run_command(self, command: str) -> None:
        if not command:
            return
        parts = command.split(maxsplit=1)
        name = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if name in {"quit", "q", "exit"}:
            self.exit()
            return
        if name == "help":
            self._show_help()
            return

        # name -> handler(arg). Built per call (cheap: ~25 entries, only on Enter) so
        # every handler can be a plain bound method; aliases just appear twice.
        handlers: Dict[str, Callable[[str], None]] = {
            "refresh": self._do_refresh,
            "all": lambda arg: self.action_clear_filters(),
            "clear": lambda arg: self.action_clear_filters(),
            "section": self._do_section,
            "import": self._do_import,
            "unimport": self._do_unimport,
            "format": self._do_format,
            "rename": self._do_rename,
            "rule": self._do_rule,
            "rules": lambda arg: self._show_rules(),
            "categorize": self._do_categorize,
            "categorise": self._do_categorize,
            "cat": self._do_categorize,
            "category": self._do_category,
            "sel": self._do_sel,
            "transfers": self._do_transfers,
            "merge": self._do_merge,
            "filter": self._do_filter,
            "stats": self._do_stats,
            "chart": self._do_chart,
            "graph": self._do_chart,
            "pie": self._do_pie,
            "trips": lambda arg: self._show_trips(),
            "trip": self._do_trip,
            "budget": self._do_budget,
            "rates": self._do_rates,
            "sync": self._do_sync,
            "sort": self._do_sort,
        }
        handler = handlers.get(name)
        if handler is None:
            self.notify(f"Unknown command: {name}", severity="warning")
            return
        handler(arg)

    def _show_help(self) -> None:
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
            "categorize <vendor> = <category> — categorize that vendor's\n"
            "  transactions by hand (cat is short for categorize)\n"
            "categorize <vendor> = — undo a manual category\n"
            "rule categorize <pattern> = <category> — categorize every matching\n"
            "  vendor, now and on future imports (e.g. rule categorize *COFFEE* =\n"
            "  Dining)\n"
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
            "section <name> — expand that sidebar section (accounts, vendors,\n"
            "  categories, tags, trips), collapsing the rest; any unambiguous\n"
            "  prefix works, e.g. section cat. Click a heading to do the same\n"
            "x, or clicking a row — select/deselect a transaction for bulk edits\n"
            "sel all — select every transaction currently listed\n"
            "sel none — clear the selection\n"
            "sel category = <name> — categorize everything selected (blank undoes)\n"
            "sel vendor = <name> — point everything selected at that vendor\n"
            "sel tag = <name> / sel untag = <name> — add or remove a tag\n"
            "sel trip = <name> — put everything selected on a trip, replacing any\n"
            "  other trip; sel untrip takes them off it\n"
            "sel transfer — mark the 2 selected rows (one out, one in) as a\n"
            "  transfer; any fee is split off and still counts as spending.\n"
            "  sel untransfer undoes it\n"
            "sel exclude — leave the selected rows out of every income and\n"
            "  spending figure (grayed out, tagged #excluded), e.g. an ACATS move;\n"
            "  sel include counts them again\n"
            "  the selection survives an edit, so you can set a category and then\n"
            "  a tag on the same rows without reselecting\n"
            "  ctrl+n / ctrl+t prefill 'sel vendor = ' / 'sel category = ' once\n"
            "  anything is selected, in place of their usual per-vendor behavior\n"
            "transfers — pair up movements between your own accounts\n"
            "transfers same-account — also pair legs within the same account;\n"
            "  off by default, since it makes an accidental false pairing more\n"
            "  likely (for providers whose sub-accounts you track as one account)\n"
            "transfers reset — un-pair everything transfers detected\n"
            "merge <account> = <account> — fold one account into another\n"
            "filter <text> — search description, vendor, and raw name\n"
            "filter vendor:<text> — search one field (description/vendor/raw)\n"
            "filter — clear the text filter\n"
            "sort size — this view, largest amounts first (in or out); changing\n"
            "  a filter or leaving the transactions returns to date order\n"
            "sort date — back to newest first\n"
            "stats — pick a period, then see spending per category\n"
            "stats <period> — skip the picker (e.g. stats 6m, stats 1 year,\n"
            f"  stats {periods_panel.RANGE_EXAMPLE})\n"
            "  enter, or the right arrow, on a category row lists that window's\n"
            "  transactions; the left arrow goes back to the breakdown\n"
            "  space or z, on a category row with children, folds/unfolds its subtree\n"
            "  f folds/unfolds every group at once\n"
            "chart — pick a period, then see money per day/week/month as bars\n"
            "chart <period> [day|week|month] [net|spending|income] — skip the\n"
            "  picker, set the bar width and what the bars measure (e.g.\n"
            "  chart 1y month spending); the bucket defaults to the period's\n"
            "  length. b cycles the bucket, m the measure. graph = chart\n"
            "  net draws either side of a center line: money out to the left,\n"
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
            "trips — see each trip's dates, cost, and a travel-bucket breakdown\n"
            "  as a color bar (trip buckets lists the buckets themselves)\n"
            "  space folds/unfolds a trip into its buckets; f folds/unfolds every\n"
            "  trip at once\n"
            "  enter, or the right arrow, on a trip row lists that trip's\n"
            "  transactions; on an unfolded bucket row, just that bucket's; the\n"
            "  left arrow goes back to the trips panel\n"
            "trip bucket <categories> = <bucket> — map category spending into a\n"
            "  travel bucket; comma-separate several categories at once, e.g.\n"
            "  trip bucket Car Rental, Taxi = car — a blank bucket unmaps it\n"
            "trip buckets — show the bucket map, grouped by bucket\n"
            "trip dates <trip> = <start>..<end> — set a trip's dates by hand, when\n"
            "  the ones taken from its transactions are wrong (a flight booked\n"
            "  months ahead drags the start back). Leave either side of the '..'\n"
            "  empty to set just the other; a blank right-hand side derives both\n"
            "  again. Derived dates show dimmed with a '*'\n"
            "budget [YYYY-MM] — track this month's spending against its plan: an\n"
            "  income row (target vs actual), then each budgeted category's\n"
            "  Budget/Spent/Left and a Used bar + %; over-budget rows show in the\n"
            "  error color, ahead-of-pace ones in the warning color; a Budget cell\n"
            "  in the warning color is the sum of its subcategories' budgets\n"
            "budget plan [YYYY-MM] [months] — the editable plan for that month:\n"
            "  income target at top, then each category's average monthly spend\n"
            "  (over the window, default 6 months), last month's actual, and its\n"
            "  budget, down to a total vs the income target → unallocated\n"
            "  enter on a row asks for the amount in the command bar below,\n"
            "  prefilled with the current one; blank clears it\n"
            "  n cycles the averaging window 3/6/12 months; z (or space) folds a\n"
            "  category's subcategories, f folds/unfolds them all\n"
            "  a category with no budget of its own shows the sum of its\n"
            "  subcategories' in the warning color; one budgeted below that sum\n"
            "  shows red, with both totals\n"
            "  an unplanned month reuses the most recent month that was ('plan\n"
            "  from ...' in the status line)\n"
            "rates — list cached exchange rates (pair, source, span, count)\n"
            "rates fetch — cache ECB reference rates for every foreign currency\n"
            "  on file, over its whole date range; runs in the background so the\n"
            "  app stays responsive (an import does this on its own already)\n"
            "sync — pull new transactions from every sync connection (SimpleFIN, Synci),\n"
            "  in the background; connect one first with 'budget sync connect' in\n"
            "  a terminal (it asks for a one-time token on a hidden prompt, so\n"
            "  that step stays CLI-only)\n"
            "sync preview — show what a sync would do without writing anything\n"
            "  (sync dry is a synonym)\n"
            "all — clear filters   refresh — reload   quit — exit\n"
            "Click a row in an open sidebar section — account, vendor, category,\n"
            "  tag, or trip — to filter by it.\n"
            "ctrl+n / ctrl+t — prefill rename / categorize for the selected\n"
            "  transaction's vendor, or for the selected vendor in the sidebar.",
            title="Commands",
            timeout=8,
        )


def run() -> None:
    BudgetApp().run()
