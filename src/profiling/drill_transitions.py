"""Profile the statistics drill-down and the return trip.

    PYTHONPATH="$PWD/src" .venv/bin/python src/profiling/drill_transitions.py [db]

Three views of the same two transitions, because they disagree usefully:

  * wall clock per phase — what the user actually waits for
  * SQL, counted and timed — rules the database in or out
  * cProfile, cumulative — attributes whatever the first two do not explain

Runs against a *copy* of the database, so a profiling run can never write to real
data. Defaults to data/budget.db; profiling a toy database tells you nothing, since
every cost here scales with how many rows the sidebar and tables hold.
"""

from __future__ import annotations

import asyncio
import cProfile
import io
import os
import pstats
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

DEFAULT_DB = Path("data/budget.db")


def _use_copy_of(source: Path) -> Path:
    if not source.exists():
        sys.exit(f"No database at {source}. Pass one as the first argument.")
    copy = Path(tempfile.mkdtemp(prefix="budget-profile-")) / "profile.db"
    shutil.copy(source, copy)
    os.environ["BUDGET_DB"] = str(copy)
    return copy


_use_copy_of(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB)

from sqlalchemy import event  # noqa: E402  (must follow BUDGET_DB)
from sqlalchemy.engine import Engine  # noqa: E402
from textual.widgets import DataTable  # noqa: E402

from budget_tracker.tui import BudgetApp  # noqa: E402

# Listen on the Engine *class*. The app builds its own engine, so a listener attached to
# an instance created here would silently observe nothing and every count would read 0.
_statements: Counter = Counter()
_sql_seconds = [0.0]
_starts: list = []
_recording = [False]


@event.listens_for(Engine, "before_cursor_execute")
def _before(conn, cursor, statement, parameters, context, executemany):
    _starts.append(time.perf_counter())


@event.listens_for(Engine, "after_cursor_execute")
def _after(conn, cursor, statement, parameters, context, executemany):
    elapsed = time.perf_counter() - _starts.pop()
    if _recording[0]:
        _sql_seconds[0] += elapsed
        _statements[statement.split("\n")[0][:68]] += 1


class Phase:
    """Time one transition, and the SQL it issues, in isolation."""

    def __init__(self, name: str, into: list) -> None:
        self.name, self.into = name, into

    def __enter__(self) -> "Phase":
        _statements.clear()
        _sql_seconds[0] = 0.0
        _recording[0] = True
        self.started = time.perf_counter()
        return self

    def __exit__(self, *exc) -> None:
        wall_ms = (time.perf_counter() - self.started) * 1000
        _recording[0] = False
        self.into.append(
            (self.name, wall_ms, sum(_statements.values()), _sql_seconds[0] * 1000,
             _statements.most_common(3))
        )


async def _profile() -> None:
    measured: list = []
    app = BudgetApp()
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.press(*"stats 1y")
        await pilot.press("enter")
        await pilot.pause()
        table = app.query_one("#stats_table", DataTable)
        categories = app._report.categories
        # The biggest spender, so the drill has a realistic number of rows to render.
        target = max(range(len(categories)),
                     key=lambda i: -categories[i].outflow_minor)
        stat = categories[target]
        print(f"{len(app._vendors)} vendors, {len(app._categories)} categories, "
              f"{table.row_count} statistics rows")
        print(f"drilling into {stat.name!r} ({stat.count} transactions)\n")

        table.move_cursor(row=target)
        with Phase("drill down (right arrow)", measured):
            await pilot.press("right")
            await pilot.pause()
        with Phase("return (left arrow)", measured):
            await pilot.press("left")
            await pilot.pause()

        # The return trip's three internal steps, timed apart from the repaint.
        table.move_cursor(row=target)
        await pilot.press("right")
        await pilot.pause()
        with Phase("  ...of which reload()", measured):
            app.reload()
        with Phase("  ...of which _build_report()", measured):
            app._build_report()
        with Phase("  ...of which _fill_stats()", measured):
            app._fill_stats()

    print(f"{'phase':<34}{'wall ms':>9}{'queries':>9}{'sql ms':>9}")
    for name, wall_ms, count, sql_ms, common in measured:
        print(f"{name:<34}{wall_ms:>9.1f}{count:>9}{sql_ms:>9.1f}")
        for statement, times in common:
            if times > 1:
                print(f"      {times:>4}x {statement}")

    print("\ncProfile, three round trips, cumulative:\n")
    profiler = cProfile.Profile()
    profiler.enable()
    await _round_trips(target)
    profiler.disable()
    report = io.StringIO()
    pstats.Stats(profiler, stream=report).sort_stats("cumulative").print_stats(30)
    for line in report.getvalue().splitlines():
        if "budget_tracker" in line or "ncalls" in line:
            print(line.rstrip()[:140])


async def _round_trips(target: int, rounds: int = 3) -> None:
    app = BudgetApp()
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.press(*"stats 1y")
        await pilot.press("enter")
        await pilot.pause()
        table = app.query_one("#stats_table", DataTable)
        for _ in range(rounds):
            table.move_cursor(row=target)
            await pilot.press("right")
            await pilot.pause()
            await pilot.press("left")
            await pilot.pause()


if __name__ == "__main__":
    asyncio.run(_profile())
