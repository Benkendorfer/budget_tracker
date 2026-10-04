"""Command-family mixins for ``BudgetApp``.

``app.py`` holds the composed ``BudgetApp`` class, its CSS, ``compose``/``on_mount``,
bindings, and ``reload()``/data plumbing. Everything a command (or a key binding) does
once it is dispatched lives in one mixin module here, grouped by the family of command
it belongs to (selection, rules, categories, transfers, imports, filters, rates, sync,
trips, periods, stats, chart, pie, drill-down, events, actions). ``BudgetApp`` inherits
from all of them; every method keeps the exact name and ``self.`` state it had when it
lived directly on ``BudgetApp``, so which module defines a given method is purely an
organizational detail -- callers (including the tests) just use ``app.<method>()``.

Each mixin is a plain class with no base of its own, composed onto ``BudgetApp``
alongside Textual's ``App``. None of them override a Textual ``App`` attribute or
method; see the module docstring note in ``app.py`` about the ``_filters`` collision
this is written to avoid repeating.
"""

from __future__ import annotations
