"""The period picker: the presets, plus the trailing custom-range row."""

from __future__ import annotations

from textual.widgets import DataTable

from .. import stats

# The last row of the period picker, and the format its answer has to take.
CUSTOM_PERIOD = "Custom…"
RANGE_EXAMPLE = "2025-01-01..2025-06-30"


def fill_periods(table: DataTable) -> None:
    table.clear()
    # Spelling out the dates each preset resolves to saves the user working out what
    # "3 months" means when their imports are a month stale.
    for key, label, _months in stats.PRESETS:
        window = stats.resolve(key)
        table.add_row(label, f"{window.start} → {window.end}")
    table.add_row(CUSTOM_PERIOD, RANGE_EXAMPLE)
