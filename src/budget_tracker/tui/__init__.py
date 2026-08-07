"""Full-screen Textual TUI for the budget tracker.

The package is split one module per panel (see each module's own docstring), with
``app.py`` holding ``BudgetApp`` itself — state, ``reload()``, command dispatch,
bindings, and actions. This module re-exports the public surface so callers can keep
importing from ``budget_tracker.tui`` exactly as before the split: ``cli.py`` does
``from .tui import run``, the profiling scripts do ``from budget_tracker.tui import
BudgetApp``, and the tests import ``BudgetApp``, ``_fmt_amount``, ``FOLD_INDICATOR``,
and ``TRANSFER_MARK`` from here.
"""

from __future__ import annotations

from .app import BudgetApp, run
from .formatting import FOLD_INDICATOR, TRANSFER_MARK, _fmt_amount

__all__ = ["BudgetApp", "run", "FOLD_INDICATOR", "TRANSFER_MARK", "_fmt_amount"]
