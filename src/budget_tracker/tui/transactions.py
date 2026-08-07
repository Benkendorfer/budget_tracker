"""The transactions table: the panel most of the app returns to.

Just the one rendering function — everything else about this panel (filters, the
totals-driven status line, the drill-down that lands here) is app state, and stays on
``BudgetApp``.
"""

from __future__ import annotations

from typing import Dict, List

from textual.widgets import DataTable

from .. import queries
from .formatting import TRANSFER_MARK, _amount_cell, _txn_cell


def fill_txns(
    table: DataTable, txns: List[queries.TxnRow], currencies: Dict[str, queries.CurrencyRow]
) -> None:
    table.clear()
    for txn in txns:
        marked = f"{TRANSFER_MARK} {txn.description}" if txn.is_transfer else txn.description
        table.add_row(
            _txn_cell(txn.posted_date, 10, txn.is_transfer),
            _txn_cell(marked, 26, txn.is_transfer),
            _txn_cell(txn.vendor, 18, txn.is_transfer),
            _txn_cell(txn.category, 12, txn.is_transfer),
            _amount_cell(txn.amount_minor, txn.is_transfer, currencies.get(txn.currency)),
            _txn_cell(txn.account, 18, txn.is_transfer),
        )
