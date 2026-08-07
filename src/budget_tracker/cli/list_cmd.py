"""The ``list`` command: transactions, optionally filtered, as a table."""

from __future__ import annotations

import argparse
from typing import Optional

from .. import categories as categories_module
from .. import queries
from ..db import get_engine, get_sessionmaker, init_db


def _resolve_category(session, name: str) -> Optional[int]:
    """``--category``'s lookup: a full path (``"Food > Dining"``) or a bare name.

    Routed through :func:`categories.resolve_path` rather than
    :func:`queries.resolve_category`, which matches on a bare name with no parent
    filter and would arbitrarily pick a match once a name can repeat across branches.
    """
    try:
        category = categories_module.resolve_path(session, name)
    except categories_module.CategoryError as error:
        print(error)
        return None
    if category is None:
        print(f"No category named {name!r}.")
        return None
    return category.id


def _cmd_list(args: argparse.Namespace) -> int:
    from rich.console import Console
    from rich.table import Table

    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        account_id = None
        if args.account:
            account_id = queries.resolve_account(session, args.account)
            if account_id is None:
                print(f"No account named {args.account!r}.")
                return 1
        category_id = None
        if args.category:
            category_id = _resolve_category(session, args.category)
            if category_id is None:
                return 1
        vendor_filter = None
        if args.vendor:
            vendor_filter = queries.resolve_vendor_filter(session, args.vendor)
            if vendor_filter is None:
                print(f"No vendor named {args.vendor!r}.")
                return 1
        text_filter = (
queries.TextFilter(args.search, args.search_in) if args.search else None
        )
        # One value for both calls: the table and the totals under it must be scoped
        # identically or the figures will not describe the rows above them.
        filters = queries.Filters(
            account_id=account_id,
            category_id=category_id,
            vendor_filter=vendor_filter,
            text_filter=text_filter,
        )
        txns = queries.get_transactions(
            session,
            limit=args.limit,
            filters=filters,
        )
        totals = queries.get_totals(session, filters=filters)

    console = Console()
    table = Table(box=None, pad_edge=False)
    table.add_column("Date")
    table.add_column("Description")
    table.add_column("Vendor")
    table.add_column("Category")
    table.add_column("Amount", justify="right")
    for txn in txns:
        # Match the app: transfers are dimmed and flagged so their absence from the
        # totals is visible rather than mysterious.
        style = "dim" if txn.is_transfer else ("red" if txn.amount_minor < 0 else "green")
        description = f"⇄ {txn.description}" if txn.is_transfer else txn.description
        row = [txn.posted_date, description, txn.vendor, txn.category]
        table.add_row(
            *([f"[dim]{c}[/dim]" for c in row] if txn.is_transfer else row),
            f"[{style}]{txn.amount_minor / 100:,.2f}[/{style}]",
        )
    console.print(table)
    console.print(
        f"[bold]{totals.count} txns[/bold]"
        + (f" ({totals.transfer_count} transfers excluded)" if totals.transfer_count else "")
        + "   "
        f"net {totals.net_minor / 100:,.2f}   "
        f"out {totals.outflow_minor / 100:,.2f}   "
        f"in {totals.inflow_minor / 100:,.2f}"
    )
    return 0
