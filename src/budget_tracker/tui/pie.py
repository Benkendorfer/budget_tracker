"""The pie panel: reshaping a statistics report into wedges, and its status line."""

from __future__ import annotations

from typing import Optional

from rich.table import Table
from rich.text import Text
from textual.widgets import Static

from .. import charts, stats
from .formatting import PIE_COLORS, TRANSFER_MARK, UNCONVERTED_MARK, _fmt_amount, _truncate


def build_pie(report: Optional[stats.Report]) -> Optional[charts.Pie]:
    """Turn the already-built report into wedges — no query of its own.

    The pie and the statistics table read the same report (see reload()'s guard and
    _show_pie()), so this is pure reshaping: charts.build_pie() already does the
    depth-0, nonzero-share filtering (see its own docstring for why).
    """
    return charts.build_pie(report.categories) if report is not None else None


def fill_pie(static: Static, pie: Optional[charts.Pie]) -> None:
    """Render the wedges and their legend into the pie Static."""
    if pie is None or not pie.slices:
        static.update(Text("No net spending in this window.", style="dim"))
        return

    art = Text()
    for row in range(pie.height):
        for col in range(pie.width):
            index = pie.mask[row][col]
            if index is None:
                art.append(" ")
            else:
                art.append(charts.BLOCK, style=PIE_COLORS[index % len(PIE_COLORS)])
        art.append("\n")

    legend = Table.grid(padding=(0, 1))
    legend.add_column()  # swatch
    legend.add_column()  # category name
    legend.add_column(justify="right")  # share
    legend.add_column(justify="right")  # amount
    for index, one_slice in enumerate(pie.slices):
        color = PIE_COLORS[index % len(PIE_COLORS)]
        legend.add_row(
            Text(charts.BLOCK * 2, style=color),
            Text(_truncate(one_slice.name, 20)),
            Text(f"{one_slice.share * 100:.1f}%", justify="right"),
            Text(_fmt_amount(one_slice.amount_minor), style="red", justify="right"),
        )

    grid = Table.grid(padding=(0, 2))
    grid.add_column()
    grid.add_column()
    grid.add_row(art, legend)
    static.update(grid)


def pie_status(report: stats.Report, pie: Optional[charts.Pie]) -> str:
    """One line, the same shape as stats_panel.stats_status()/chart_panel.chart_status()."""
    window = report.window
    label = "custom" if window.key == "custom" else window.label
    count = len(pie.slices) if pie is not None else 0
    excluded = f"{TRANSFER_MARK} {report.transfer_count} " if report.transfer_count else ""
    # As terse as the transfer marker beside it, for the same reason (see
    # stats_panel.stats_status): no room on this line for the word "unconverted".
    unconverted = (
        f"{UNCONVERTED_MARK} {report.unconverted_count} " if report.unconverted_count else ""
    )
    return (
        f"{label} {window.start}→{window.end} "
        f"{count} categor{'y' if count == 1 else 'ies'} "
        f"{excluded}"
        f"{unconverted}"
        f"net spend {_fmt_amount(-report.net_spend_minor)}"
    )
