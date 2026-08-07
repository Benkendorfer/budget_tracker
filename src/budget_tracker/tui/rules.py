"""The rules panel: vendor rules and category rules, shown together."""

from __future__ import annotations

from typing import List

from rich.text import Text
from textual.widgets import DataTable

from .. import queries
from .formatting import _truncate


def fill_rules(
    table: DataTable,
    rules: List[queries.RuleRow],
    category_rules: List[queries.CategoryRuleRow],
) -> None:
    table.clear()
    for rule in rules:
        table.add_row(
            "vendor",
            _truncate(rule.pattern, 26),
            _truncate(rule.name, 18),
            Text(str(rule.vendor_count), justify="right"),
        )
    for rule in category_rules:
        table.add_row(
            "category",
            _truncate(rule.pattern, 26),
            _truncate(rule.category, 18),
            Text(str(rule.txn_count), justify="right"),
        )
