"""The transactions table: the panel most of the app returns to.

Just the one rendering function — everything else about this panel (filters, the
totals-driven status line, the drill-down that lands here) is app state, and stays on
``BudgetApp``.
"""

from __future__ import annotations

from typing import AbstractSet, Dict, FrozenSet, List

from textual.widgets import DataTable

from .. import queries
from .formatting import TRANSFER_MARK, _amount_cell, _txn_cell

# The cell shown in the leading select column -- see BudgetApp._selected_ids.
SELECTED_MARK = "✓"

_NO_SELECTION: FrozenSet[int] = frozenset()


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
            # Always blank for now -- tags don't exist yet (queries.TxnRow has no
            # tags/trip field this wave). A later wave fills this from that data; the
            # column exists early so that wave is additive, not a reshape.
            "",
        )
