"""Command-line interface for the budget tracker.

The package is split one module per command family (see each module's own docstring),
with ``parser.py`` holding ``build_parser`` and the ``main`` entry point. This module
re-exports the public surface so callers can keep importing from ``budget_tracker.cli``
exactly as before the split: ``pyproject.toml``'s ``budget`` script does
``budget_tracker.cli:main``, ``__main__.py`` does ``from .cli import main``, and the
tests import ``main`` and ``_select_csv_interactively`` from here.
"""

from __future__ import annotations

from .import_cmds import _select_csv_interactively
from .parser import build_parser, main

__all__ = ["main", "build_parser", "_select_csv_interactively"]
