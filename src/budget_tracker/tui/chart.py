"""The bar chart: measures, buckets, the closing total row, and its status line."""

from __future__ import annotations

from typing import List, Optional

from rich.text import Text
from textual.widgets import DataTable

from .. import charts, queries, stats
from .formatting import (
    CHART_COLUMNS,
    CHART_WIDTH,
    TRANSFER_MARK,
    UNCONVERTED_MARK,
    _amount_cell,
    _fmt_amount,
    _range_label,
    _truncate,
)


def _bar_cell(bar: charts.Bar, measure: str) -> Text:
    """The bar, colored by direction: red is money out, green is money in.

    On a net chart that means the two sides of the axis are different colors, which
    is the same rule the amount columns already follow — it just happens to land on
    one row at a time there.
    """
    inflow_side = "green" if measure != "spend" else "red"
    return Text.assemble((bar.left, "red"), (bar.axis, "dim"), (bar.right, inflow_side))


def _money_cell(bar: charts.Bar, field: str) -> Text:
    """One of the two figure columns beside the bar, per CHART_COLUMNS."""
    if field == "net":
        return _amount_cell(bar.net_minor)
    # Outflow and inflow are positive magnitudes, so _amount_cell's sign rule would
    # paint them both green; the direction is what the color has to carry here.
    value = bar.outflow_minor if field == "outflow" else bar.inflow_minor
    style = ("red" if field == "outflow" else "green") if value else "dim"
    return Text(_fmt_amount(value), style=style, justify="right")


def _add_chart_total_row(table: DataTable, chart: charts.Chart, measure: str, bucket: str) -> None:
    """A closing total, plus the per-bucket average in the bar column.

    The average belongs there because that column is the only one whose units are
    per-bucket; putting a summed bar there would be meaningless, and leaving it blank
    wastes the widest column on the row.

    A window holding no transactions gets no total, for the same reason the
    statistics table skips one: a row of zeroes reads as a finding. The test is the
    transaction count, not the bar list — buckets are zero-filled, so an empty
    window still has a full set of (empty) bars to draw.
    """
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
        Text(f"avg {_fmt_amount(chart.avg_minor)}/{bucket}", style="dim"),
        *[figure(field) for _header, field in CHART_COLUMNS[measure]],
        Text(
            str(sum(bar.count for bar in chart.bars)),
            style="bold",
            justify="right",
        ),
    )


def fill_chart(
    table: DataTable, chart: Optional[charts.Chart], measure: str, bucket: str
) -> None:
    """Redraw the table, columns included — two headers name the current measure."""
    table.clear(columns=True)
    if chart is None:
        return
    columns = CHART_COLUMNS[measure]
    # 9 + CHART_WIDTH + 12 + 12 + 5 plus two cells of padding each: 76 of the ~92 the
    # main panel has, leaving the bars room to be the widest thing on the row (see
    # test_chart_table_fits_the_main_panel).
    table.add_column("Period", width=9)
    table.add_column(charts.MEASURE_HEADERS[measure], width=CHART_WIDTH)
    for header, _field in columns:
        table.add_column(header, width=12)
    table.add_column("Txns", width=5)

    for bar in chart.bars:
        table.add_row(
            bar.label,
            _bar_cell(bar, measure),
            *[_money_cell(bar, field) for _header, field in columns],
            Text(str(bar.count), justify="right"),
        )
    _add_chart_total_row(table, chart, measure, bucket)


def chart_scope(
    category_filter: Optional[int],
    categories: List[queries.CategoryRow],
    account_filter: Optional[int],
    vendor_filter: Optional[queries.VendorFilter],
    text_filter: Optional[queries.TextFilter],
) -> str:
    """The active category, named, plus a terse marker for any other filter.

    Naming the category is the point — the whole feature is charting one category, so
    a chart that does not say which one it is showing is a trap. The other filters
    only get a marker, as they do in the transactions status line.
    """
    parts = []
    if category_filter is not None:
        name = next((c.name for c in categories if c.id == category_filter), None)
        parts.append(_truncate(name, 16) if name else "category")
    if account_filter is not None:
        parts.append("account")
    if vendor_filter is not None:
        parts.append("vendor")
    if text_filter is not None:
        parts.append("text")
    return f"[{', '.join(parts)}] " if parts else ""


def chart_status(
    chart: charts.Chart,
    window: stats.Window,
    measure: str,
    bucket: str,
    chart_transfers: int,
    category_filter: Optional[int],
    categories: List[queries.CategoryRow],
    account_filter: Optional[int],
    vendor_filter: Optional[queries.VendorFilter],
    text_filter: Optional[queries.TextFilter],
    chart_unconverted: int = 0,
) -> str:
    """One line, under the same 92-column budget as every other panel's status."""
    label = "custom" if window.key == "custom" else window.label
    scope = chart_scope(category_filter, categories, account_filter, vendor_filter, text_filter)
    excluded = f"{TRANSFER_MARK} {chart_transfers} " if chart_transfers else ""
    # As terse as the transfer marker beside it, for the same reason (see
    # stats_panel.stats_status): this line has no room left for the word
    # "unconverted", and stacking both markers at once is not budgeted for.
    unconverted = f"{UNCONVERTED_MARK} {chart_unconverted} " if chart_unconverted else ""
    return (
        f"{label} {_range_label((window.start, window.end))} "
        f"{measure}/{bucket} "
        f"{scope}"
        f"{excluded}"
        f"{unconverted}"
        f"total {_fmt_amount(chart.total_minor)} "
        f"peak {_fmt_amount(chart.peak_minor)}"
    )
