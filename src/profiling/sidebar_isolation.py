"""Attribute the drill-down's cost by disabling one piece of rendering at a time.

    PYTHONPATH="$PWD/src" .venv/bin/python src/profiling/sidebar_isolation.py [db]

A profiler says where time is spent; this says what would happen if it were not spent.
That distinction found the original problem: cProfile pointed at Textual's own layout
machinery, which is not actionable, while switching the sidebar rebuild off turned a
~2.4s transition into ~0.25s and named the cause exactly.

The rows are cumulative evidence, not a menu — "not rebuilt at all" is not a shippable
configuration, it is the floor that says how much the rebuild is costing.

Runs against a *copy* of the database, so a run can never write to real data.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_DB = Path("data/budget.db")
ROUNDS = 3


def _use_copy_of(source: Path) -> None:
    if not source.exists():
        sys.exit(f"No database at {source}. Pass one as the first argument.")
    copy = Path(tempfile.mkdtemp(prefix="budget-profile-")) / "profile.db"
    shutil.copy(source, copy)
    os.environ["BUDGET_DB"] = str(copy)


_use_copy_of(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB)

from textual.widgets import DataTable, Label, ListItem, ListView  # noqa: E402

from budget_tracker.tui import BudgetApp  # noqa: E402

_fill_list = BudgetApp._fill_list
_fill_txns = BudgetApp._fill_txns


def _always_rebuild(self, selector, labels):
    """_fill_list without its skip-if-unchanged guard, to price the guard itself."""
    view = self.query_one(selector, ListView)
    view.clear()
    view.extend([ListItem(Label("— All —"))] + [ListItem(Label(l)) for l in labels])


def _append_one_at_a_time(self, selector, labels):
    """The original implementation, for comparison: one mount per row."""
    view = self.query_one(selector, ListView)
    view.clear()
    view.append(ListItem(Label("— All —")))
    for label in labels:
        view.append(ListItem(Label(label)))


def _skip_txns(self, txns):
    self._txns = txns


async def _time(label, fill_list, fill_txns) -> None:
    BudgetApp._fill_list, BudgetApp._fill_txns = fill_list, fill_txns
    try:
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.press(*"stats 1y")
            await pilot.press("enter")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            target = max(range(len(app._report.categories)),
                         key=lambda i: -app._report.categories[i].outflow_minor)
            down = back = 0.0
            for _ in range(ROUNDS):
                table.move_cursor(row=target)
                start = time.perf_counter()
                await pilot.press("right")
                await pilot.pause()
                down += time.perf_counter() - start
                start = time.perf_counter()
                await pilot.press("left")
                await pilot.pause()
                back += time.perf_counter() - start
        print(f"{label:<42}{down / ROUNDS * 1000:>9.0f}{back / ROUNDS * 1000:>9.0f}")
    finally:
        BudgetApp._fill_list, BudgetApp._fill_txns = _fill_list, _fill_txns


async def main() -> None:
    print(f"{'configuration':<42}{'drill ms':>9}{'back ms':>9}")
    await _time("as shipped", _fill_list, _fill_txns)
    await _time("sidebar: rebuilt every time (batched)", _always_rebuild, _fill_txns)
    await _time("sidebar: rebuilt every time, one by one", _append_one_at_a_time, _fill_txns)
    await _time("sidebar: never rebuilt (floor)", lambda *a: None, _fill_txns)
    await _time("transactions table: not filled", _fill_list, _skip_txns)


if __name__ == "__main__":
    asyncio.run(main())
