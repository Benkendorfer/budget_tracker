"""The ``limits`` command: the monthly budget -- tracking and planning.

Named ``limits`` rather than ``budget`` because the *program* is already ``budget``
(``budget budget ...`` reads badly); in the app the word "budget" is the view, while
the stored amounts are still called limits -- see :mod:`..budget`'s module docstring.

This is the one command family in this package that does **not** use a real argparse
subparsers tree for its sub-forms. ``trips``/``tags``/``account`` all pick a subcommand
*name* first and never need a bare value at that position, so a subparsers positional
(which must be the parser's last positional, and swallows everything from the first
matching token on) works for them. ``budget limits [YYYY-MM]`` needs the opposite: a
bare value with *no* keyword at all, sitting where ``plan``/``set``/``clear``/``income``
also sit for the other forms. argparse resolves a positional's arity before it looks at
any token's text, so a ``month`` positional ahead of subparsers greedily claims the
first token whether or not it looks like a subcommand name, and subparsers after a
``month`` positional never see anything (confirmed empirically -- `plan 2026-10
--months 3` lands "plan" in ``month`` and then fails on `2026-10` as an invalid
subcommand choice). So this module takes one catch-all ``tokens`` positional plus the
few flags that cannot collide with a positional (``--month``, ``--months``, ``--clear``),
and dispatches on ``tokens[0]`` itself in :func:`_split_command`.

Nothing here computes a budget figure -- every number comes from :mod:`..budget`,
which itself never recomputes an actual (see that module's docstring); this file only
parses argv, resolves names and amounts, and renders rich tables.
"""

from __future__ import annotations

import argparse
import calendar
import re
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional, Tuple

from .. import budget as budget_module
from ..db import get_engine, get_sessionmaker, init_db
from ..tui.formatting import _fmt_amount

_SUBCOMMANDS = {"plan", "set", "clear", "income"}

_DOLLARS_RE = re.compile(r"^[0-9]+(\.[0-9]{1,2})?$")


def _parse_month(text: str) -> date:
    """``YYYY-MM`` -> the first of that month; anything else is a clear error, not a
    guess (see :func:`..importer._normalize_amount` for the same philosophy on
    amounts)."""
    try:
        return datetime.strptime(text, "%Y-%m").date()
    except ValueError as error:
        raise ValueError(f"Could not parse month {text!r}; expected YYYY-MM.") from error


def _parse_dollars(text: str) -> int:
    """A dollar amount typed on the command line ("800", "1,200.50", "$42") into
    home-currency minor units.

    Deliberately strict and sign-less: a stored budget amount is always a positive
    limit or target (:func:`..budget.set_amount` clears with ``None``, never with a
    negative), so there is no "-" to accept here, unlike
    :func:`..importer._normalize_amount`, which does have to read a signed bank export.
    """
    cleaned = text.strip()
    if cleaned.startswith("$"):
        cleaned = cleaned[1:]
    cleaned = cleaned.replace(",", "")
    if not _DOLLARS_RE.match(cleaned):
        raise ValueError(
            f"Could not parse amount {text!r}; expected something like '800' or "
            "'1,200.50'."
        )
    return int((Decimal(cleaned) * 100).to_integral_value())


def _split_command(tokens: List[str]) -> Tuple[str, List[str]]:
    """``tokens[0]`` names a subcommand if it is one of the fixed keywords;
    otherwise this is the default "track" form, and every token (zero or one: the
    optional month) belongs to it instead."""
    if tokens and tokens[0] in _SUBCOMMANDS:
        return tokens[0], tokens[1:]
    return "track", tokens


def _month_arg(args: argparse.Namespace) -> Optional[date]:
    """``--month``, parsed, or ``None`` to mean "the current month" -- shared by
    set/clear/income, the three forms that take the month as a flag rather than a
    bare positional (their positionals are already busy with a category or amount)."""
    if args.month is None:
        return None
    return _parse_month(args.month)


def _cmd_budget_limits(args: argparse.Namespace) -> int:
    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)

    command, rest = _split_command(list(args.tokens))

    with session_factory() as session:
        try:
            if command == "set":
                return _do_set(session, rest, args)
            if command == "clear":
                return _do_clear(session, rest, args)
            if command == "income":
                return _do_income(session, rest, args)
            if command == "plan":
                return _do_plan(session, rest, args)
            return _do_track(session, rest, args)
        except ValueError as error:
            print(error)
            return 1


def _do_set(session, rest: List[str], args: argparse.Namespace) -> int:
    if len(rest) != 2:
        print("Usage: budget limits set <category> <amount> [--month YYYY-MM].")
        return 1
    category, amount_text = rest
    month = _month_arg(args) or budget_module.month_start(date.today())
    amount_minor = _parse_dollars(amount_text)
    budget_module.set_amount(session, month, category, amount_minor, kind=budget_module.EXPENSE)
    session.commit()
    print(
        f"Set {category!r} to {_fmt_amount(amount_minor)}/month for "
        f"{month.strftime('%Y-%m')}."
    )
    return 0


def _do_clear(session, rest: List[str], args: argparse.Namespace) -> int:
    if len(rest) != 1:
        print("Usage: budget limits clear <category> [--month YYYY-MM].")
        return 1
    category = rest[0]
    month = _month_arg(args) or budget_module.month_start(date.today())
    budget_module.set_amount(session, month, category, None, kind=budget_module.EXPENSE)
    session.commit()
    print(f"Cleared {category!r} for {month.strftime('%Y-%m')}.")
    return 0


def _do_income(session, rest: List[str], args: argparse.Namespace) -> int:
    if args.clear:
        if rest:
            print("Usage: budget limits income <amount> [--month YYYY-MM], or --clear.")
            return 1
        amount_minor: Optional[int] = None
    else:
        if len(rest) != 1:
            print("Usage: budget limits income <amount> [--month YYYY-MM], or --clear.")
            return 1
        amount_minor = _parse_dollars(rest[0])
    month = _month_arg(args) or budget_module.month_start(date.today())
    budget_module.set_amount(session, month, None, amount_minor, kind=budget_module.INCOME)
    session.commit()
    if amount_minor is None:
        print(f"Cleared the income target for {month.strftime('%Y-%m')}.")
    else:
        print(
            f"Set the income target to {_fmt_amount(amount_minor)}/month for "
            f"{month.strftime('%Y-%m')}."
        )
    return 0


def _do_plan(session, rest: List[str], args: argparse.Namespace) -> int:
    if len(rest) > 1:
        print("Usage: budget limits plan [YYYY-MM] [--months N].")
        return 1
    month = _parse_month(rest[0]) if rest else budget_module.month_start(date.today())
    view = budget_module.plan_rows(session, month, averaging_months=args.months)
    _print_plan(view)
    return 0


def _do_track(session, rest: List[str], args: argparse.Namespace) -> int:
    if len(rest) > 1:
        print("Usage: budget limits [YYYY-MM].")
        return 1
    # Read once, not inside budget.track's own default -- so a test can fix "today" by
    # monkeypatching this module's ``date`` (see test_cli_budget.py) without reaching
    # into budget.py, and so the month default and the pace shown against it can never
    # disagree because of two separate `date.today()` calls straddling midnight.
    today = date.today()
    month = _parse_month(rest[0]) if rest else budget_module.month_start(today)
    source_month = budget_module.effective_month(session, month)
    view = budget_module.track(session, month, today=today)
    _print_track(view, source_month)
    return 0


# ------------------------------------------------------------------------- rendering

def _used_cell(row) -> str:
    text = f"{round(row.used * 100)}%"
    if row.over:
        return f"{text}  OVER"
    if row.ahead_of_pace:
        return f"{text}  ahead"
    return text


def _row_style(row) -> Optional[str]:
    if row.over:
        return "red"
    if row.ahead_of_pace:
        return "yellow"
    return None


def _income_left(view) -> str:
    if view.income_target_minor is None:
        return "–"
    return _fmt_amount(view.income_actual_minor - view.income_target_minor)


def _income_used(view) -> str:
    if view.income_target_minor is None:
        return "–"
    return f"{round(view.income_actual_minor / view.income_target_minor * 100)}%"


def _print_track(view, source_month: Optional[date]) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    if source_month is not None and source_month != view.month:
        console.print(
            f"(plan copied forward from {source_month.strftime('%Y-%m')})", style="dim"
        )

    table = Table(box=None, pad_edge=False)
    table.add_column("")
    table.add_column("Budget", justify="right")
    table.add_column("Spent", justify="right")
    table.add_column("Left", justify="right")
    table.add_column("Used %", justify="right")

    table.add_row(
        "Income",
        _fmt_amount(view.income_target_minor) if view.income_target_minor is not None else "–",
        _fmt_amount(view.income_actual_minor),
        _income_left(view),
        _income_used(view),
        style="bold",
    )
    for row in view.rows:
        budget_cell = _fmt_amount(row.budget_minor)
        if row.budget_derived:
            budget_cell = f"[yellow]{budget_cell} (sum)[/yellow]"
        table.add_row(
            "  " * row.depth + row.name,
            budget_cell,
            _fmt_amount(row.spent_minor),
            _fmt_amount(row.left_minor),
            _used_cell(row),
            style=_row_style(row),
        )
    console.print(table)

    console.print(
        f"Total budget {_fmt_amount(view.total_budget_minor)}   "
        f"Spent {_fmt_amount(view.total_spent_minor)}   "
        f"Not budgeted {_fmt_amount(view.not_budgeted_minor)}   "
        f"Total spending {_fmt_amount(view.total_spending_minor)}"
    )
    if view.is_current:
        days_in_month = calendar.monthrange(view.month.year, view.month.month)[1]
        day = round(view.elapsed_fraction * days_in_month)
        pct = round(view.elapsed_fraction * 100)
        console.print(f"{view.month.strftime('%b')} {day} of {days_in_month} ({pct}% of the month)")


def _print_plan(view) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(box=None, pad_edge=False)
    table.add_column("")
    table.add_column(f"Avg/mo ({view.averaging_months}m)", justify="right")
    table.add_column("Last month", justify="right")
    table.add_column("Budget", justify="right")

    table.add_row(
        "Income",
        _fmt_amount(view.income_avg_minor),
        _fmt_amount(view.income_last_month_minor),
        (
            _fmt_amount(view.income_target_minor)
            if view.income_target_minor is not None
            else "–"
        ),
        style="bold",
    )
    for row in view.rows:
        budget_text = _fmt_amount(row.budget_minor) if row.budget_minor is not None else "–"
        if row.overcommitted:
            # Words as well as color, so it reads in a captured or piped stream too.
            budget_text += f" < {_fmt_amount(row.subcategory_budget_minor)} in subcategories"
        elif row.budget_derived:
            budget_text += " (sum)"
        table.add_row(
            "  " * row.depth + row.name,
            _fmt_amount(row.avg_minor),
            _fmt_amount(row.last_month_minor),
            budget_text,
            style="red" if row.overcommitted else ("yellow" if row.budget_derived else None),
        )
    console.print(table)

    footer = f"Total budgeted {_fmt_amount(view.total_budget_minor)}"
    if view.income_target_minor is not None:
        footer += (
            f"   Income target {_fmt_amount(view.income_target_minor)}"
            f"   Unallocated {_fmt_amount(view.unallocated_minor)}"
        )
    console.print(footer)
