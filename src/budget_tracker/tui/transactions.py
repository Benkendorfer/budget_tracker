"""The transactions table: the panel most of the app returns to.

Just the one rendering function — everything else about this panel (filters, the
totals-driven status line, the drill-down that lands here) is app state, and stays on
``BudgetApp``.
"""

from __future__ import annotations

from typing import AbstractSet, Dict, FrozenSet, List, Tuple

from rich.text import Text
from textual.widgets import DataTable

from .. import queries
from .formatting import TRANSFER_MARK, TRANSFER_STYLE, _amount_cell, _truncate, _txn_cell

# The cell shown in the leading select column -- see BudgetApp._selected_ids.
SELECTED_MARK = "✓"

# The trip marker in the Tags column, e.g. "✈Japan 2026" -- distinct from an ordinary
# tag's "#" prefix so a trip reads as a place, not a label.
TRIP_MARK = "✈"

# Shared with app.py's add_column so the declared width and the width the cell
# truncates to cannot drift apart.
TAGS_COLUMN_WIDTH = 30

_NO_SELECTION: FrozenSet[int] = frozenset()


def _tags_cell(trip: str, tags: Tuple[str, ...], width: int, is_transfer: bool) -> Text:
    """The Tags column: the trip first (``✈Japan 2026``), then ordinary tags sorted
    by name and space-separated (``#reimbursable``), dim-styled, truncated -- and
    following the same dim-on-transfer idiom every other cell in the row gets, so a
    tagged transfer still reads as a transfer rather than losing that at a glance.
    """
    parts = []
    if trip:
        parts.append(f"{TRIP_MARK}{trip}")
    parts.extend(f"#{tag}" for tag in sorted(tags))
    text = " ".join(parts)
    style = TRANSFER_STYLE if is_transfer else "dim"
    return Text(_truncate(text, width), style=style)


def fill_txns(
    table: DataTable,
    txns: List[queries.TxnRow],
    currencies: Dict[str, queries.CurrencyRow],
    selected_ids: AbstractSet[int] = _NO_SELECTION,
) -> None:
    table.clear()
    for txn in txns:
        marked = f"{TRANSFER_MARK} {txn.description}" if txn.is_transfer else txn.description
        table.add_row(
            SELECTED_MARK if txn.id in selected_ids else "",
            _txn_cell(txn.posted_date, 10, txn.is_transfer),
            _txn_cell(marked, 30, txn.is_transfer),
            _txn_cell(txn.vendor, 20, txn.is_transfer),
            _txn_cell(txn.category, 16, txn.is_transfer),
            _amount_cell(txn.amount_minor, txn.is_transfer, currencies.get(txn.currency)),
            _txn_cell(txn.account, 18, txn.is_transfer),
            _tags_cell(txn.trip or "", txn.tags, TAGS_COLUMN_WIDTH, txn.is_transfer),
        )
