"""The pie panel: one bar for the whole window, one bar per bucket beneath it, and the
shared legend below them -- category share over time. Kept as ``pie.py`` and the `pie`
command because that is what the user types; it no longer draws a circle.

The geometry (which cells belong to which category, in what order) is entirely
``charts.build_stacked_share``'s job -- this module only turns that into colored text
and a status line.
"""

from __future__ import annotations

from typing import List, Optional

from rich.text import Text
from textual.widgets import Static

from .. import charts, stats
from .formatting import (
    OTHER_COLOR,
    PIE_COLORS,
    TRANSFER_MARK,
    UNCONVERTED_MARK,
    _fmt_amount,
    _truncate,
)

# Fixed-width columns for every row -- the top bar, the separator, and each bucket --
# so the bar and the total line up top to bottom regardless of how long a label or an
# amount happens to be. Sized against #pie's own content width: the panel is a round
# border plus one cell of padding on every side, four columns narrower than the
# ~94-column region it occupies beside the 36-wide sidebar, which leaves 90 -- see
# test_pie_panel_fits_the_main_panel for the render this is checked against.
LABEL_WIDTH = 14
TOTAL_WIDTH = 14
MARGIN = "  "
GAP = "  "
BAR_WIDTH = 54


def build_stacked(
    report: Optional[stats.Report], buckets: List[stats.BucketCategories]
) -> Optional[charts.StackedShareChart]:
    """The window-wide share bar plus one bar per time bucket -- no query of its own.

    ``report`` and ``buckets`` are read from the same window and filters (see
    reload()'s guard and app._build_pie()), so this is pure reshaping:
    charts.build_stacked_share() already does the segment selection, the Other
    folding, and the per-bucket apportionment (see its own docstring for why every bar
    is guaranteed to share the same segments in the same order).
    """
    if report is None:
        return None
    return charts.build_stacked_share(report.categories, buckets, width=BAR_WIDTH)


def _colors(chart: charts.StackedShareChart) -> List[str]:
    """One color per segment, assigned once from the shared legend and reused for the
    top bar and every bucket bar -- see StackedShareChart.segments's own docstring for
    why a bucket is never allowed to pick its own segment order.

    Other gets the fixed neutral gray rather than a turn in the cycle (see
    formatting.OTHER_COLOR) -- it stands for several categories, not one, so it should
    not look like a real one, and skipping it leaves a real hue free for something
    that is. Real segments beyond PIE_COLORS wrap back to its start; see
    too_many_colors() for how that is flagged rather than left silently ambiguous.
    """
    colors = []
    real = 0
    for segment in chart.segments:
        if segment.is_other:
            colors.append(OTHER_COLOR)
        else:
            colors.append(PIE_COLORS[real % len(PIE_COLORS)])
            real += 1
    return colors


def too_many_colors(chart: charts.StackedShareChart) -> bool:
    """True once real (non-Other) segments outnumber PIE_COLORS, so two of them are
    drawn in the same color -- the legend says so rather than leaving it silently
    ambiguous which is which (see _legend())."""
    return sum(1 for segment in chart.segments if not segment.is_other) > len(PIE_COLORS)


def _cell_owners(segments: List[charts.ShareSegment]) -> List[int]:
    """One index per cell into the *chart's* shared segment list.

    A ``StackedBar``'s own ``segments`` are already in that same order (see
    StackedShareChart.build_stacked_share's docstring), so a segment's position in
    this list is a color index directly -- there is no id lookup to get wrong.
    """
    owners: List[int] = []
    for index, segment in enumerate(segments):
        owners.extend([index] * segment.cells)
    return owners


def _bar_text(cell_owners: List[int], colors: List[str]) -> Text:
    """``BAR_WIDTH`` cells, each colored by whichever segment owns it.

    Blank, not missing, when a bucket had no net spend at all -- padded out to the
    same width as every other row so the total beside it still lines up.
    """
    text = Text()
    for owner in cell_owners:
        text.append(charts.BLOCK, style=colors[owner])
    text.append(" " * (BAR_WIDTH - len(cell_owners)))
    return text


def _row(label: str, total_minor: int, cell_owners: List[int], colors: List[str]) -> Text:
    row = Text(MARGIN)
    row.append(_truncate(label, LABEL_WIDTH).ljust(LABEL_WIDTH))
    row.append(GAP)
    row.append(_bar_text(cell_owners, colors))
    row.append(GAP)
    row.append(
        _fmt_amount(total_minor).rjust(TOTAL_WIDTH),
        style="red" if total_minor else "dim",
    )
    return row


def _window_label(window: stats.Window) -> str:
    return "custom" if window.key == "custom" else window.label


def _legend(chart: charts.StackedShareChart, colors: List[str]) -> Text:
    legend = Text(MARGIN)
    for index, segment in enumerate(chart.segments):
        if index:
            legend.append("   ")
        legend.append("■ ", style=colors[index])
        legend.append(f"{_truncate(segment.name, 20)} {segment.share * 100:.1f}%")
    if too_many_colors(chart):
        # More real categories than PIE_COLORS has hues for one window is rare -- see
        # too_many_colors() -- but a silent repeat would make two segments
        # indistinguishable, which is worse than admitting it in words.
        legend.append(f"   (colors repeat past {len(PIE_COLORS)} categories)", style="dim")
    return legend


def fill_pie(
    static: Static,
    chart: Optional[charts.StackedShareChart],
    window: Optional[stats.Window],
) -> None:
    """Render the top bar, one row per bucket, and the shared legend beneath them.

    No categories at all -- an empty report, or a window that is all income -- leaves
    ``chart.segments`` empty (see charts.build_share_bar's own filter), and that is
    the one case this draws a message instead of an empty table.
    """
    if chart is None or not chart.segments or window is None:
        static.update(Text("No net spending in this window.", style="dim"))
        return

    colors = _colors(chart)
    art = Text()
    art.append(_row(_window_label(window), chart.top.total_minor, chart.top.cell_owners, colors))
    art.append("\n")
    art.append(MARGIN + "─" * (LABEL_WIDTH + len(GAP) + BAR_WIDTH), style="dim")
    for bar in chart.bars:
        art.append("\n")
        art.append(_row(bar.label, bar.total_minor, _cell_owners(bar.segments), colors))
    art.append("\n\n")
    art.append(_legend(chart, colors))
    static.update(art)


def pie_status(
    report: Optional[stats.Report],
    chart: Optional[charts.StackedShareChart],
    bucket: str,
) -> str:
    """One line, the same shape as stats_panel.stats_status()/chart_panel.chart_status()."""
    if report is None:
        return ""
    window = report.window
    label = _window_label(window)
    count = len(chart.segments) if chart is not None else 0
    excluded = f"{TRANSFER_MARK} {report.transfer_count} " if report.transfer_count else ""
    # As terse as the transfer marker beside it, for the same reason (see
    # stats_panel.stats_status): no room on this line for the word "unconverted".
    unconverted = (
        f"{UNCONVERTED_MARK} {report.unconverted_count} " if report.unconverted_count else ""
    )
    return (
        f"{label} {window.start}→{window.end} {bucket} "
        f"{count} categor{'y' if count == 1 else 'ies'} "
        f"{excluded}"
        f"{unconverted}"
        f"net spend {_fmt_amount(-report.net_spend_minor)}"
    )
