"""Shared rendering helpers and display constants for the TUI's panels.

Nothing here reads app state or touches a live widget — every function takes plain
data and returns a string or a ``rich.text.Text``, so any panel can call into this
module without depending on ``BudgetApp``.
"""

from __future__ import annotations

from typing import Optional

from rich.text import Text

from .. import queries

# A row of the import browser that moves you somewhere rather than importing something.
FOLDER_MARK = "▸"

# Transfers are shown grayed and flagged, because the totals deliberately ignore them —
# a row that looks like ordinary spending but is missing from the figures reads as a bug.
TRANSFER_MARK = "⇄"
TRANSFER_STYLE = "dim italic"

# A different figure is missing for a different reason than a transfer: nothing was
# excluded on purpose, a rate to convert it just wasn't on file yet — see
# queries.Totals.unconverted_count. Deliberately not TRANSFER_MARK, so "excluded by
# design" and "missing until `rates fetch` runs" never read as the same thing.
UNCONVERTED_MARK = "⚠"

# A collapsed category shows this before its name; an expanded or leaf row shows nothing,
# so the default (fully expanded) rendering is byte-for-byte what it was before folding
# existed.
FOLD_INDICATOR = "▸"

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

# Cycled by segment index. charts.build_stacked_share() only knows which cell belongs
# to which category — this is the styling half of that split, and the only place a
# color is chosen; the pie panel assigns one of these per StackedShareChart.segments
# entry and reuses it for the top bar and every bucket bar (see tui/pie.py).
#
# Explicit hex, not the named terminal colors this replaced: a name like "red" or
# "magenta" resolves however the user's own terminal theme decides, and on a real
# theme two names can land on nearly the same crimson — invisible exactly where a
# stacked bar puts them, side by side. Okabe–Ito, chosen to stay distinct under the
# common forms of color blindness as well as under an ordinary theme.
PIE_COLORS = (
    "#E69F00",  # orange
    "#56B4E9",  # sky blue
    "#009E73",  # bluish green
    "#F0E442",  # yellow
    "#0072B2",  # blue
    "#D55E00",  # vermillion
    "#CC79A7",  # reddish purple
)

# Other is a catch-all standing in for several categories, not a category itself (see
# ShareSegment.is_other) — a fixed neutral gray says so at a glance, rather than
# letting it take whichever hue the cycle would otherwise have handed the next real
# segment, which is one fewer hue available to tell two real categories apart.
OTHER_COLOR = "#999999"


def _fmt_amount(minor: int, decimal_places: int = 2) -> str:
    return f"{minor / (10 ** decimal_places):,.{decimal_places}f}"


def _fmt_amount_for(minor: int, currency: Optional[queries.CurrencyRow]) -> str:
    """Format ``minor`` in *its own* currency: the right decimal places, and its symbol.

    ``currency`` is ``None`` for a figure with no single obvious currency — a total
    summed across a filter that may mix currencies, or a code this database has no
    Currency row for. Either way a guess would be worse than the plain two-decimal
    figure this falls back to: showing JPY's zero decimal places on a EUR/CHF mix, or a
    symbol that names the wrong currency, is a worse error than showing no symbol at
    all. Callers that know which single currency they are summing (e.g. once
    home-currency conversion lands) can pass that currency's row instead.
    """
    if currency is None:
        return _fmt_amount(minor)
    number = _fmt_amount(minor, currency.decimal_places)
    return f"{currency.symbol}{number}" if currency.symbol else number


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _amount_cell(
    minor: int, is_transfer: bool = False, currency: Optional[queries.CurrencyRow] = None
) -> Text:
    text = _fmt_amount_for(minor, currency)
    if is_transfer:
        return Text(text, style=TRANSFER_STYLE, justify="right")
    style = "red" if minor < 0 else "green"
    return Text(text, style=style, justify="right")


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
