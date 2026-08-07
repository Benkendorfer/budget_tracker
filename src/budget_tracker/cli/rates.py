"""The ``rates`` command: list cached exchange rates, fetch ECB references, or set one."""

from __future__ import annotations

import argparse
from datetime import date

from .. import queries
from .. import rates as rates_module
from ..db import get_engine, get_sessionmaker, init_db


def _cmd_rates(args: argparse.Namespace) -> int:
    """List cached exchange rates, fetch ECB references, or set a manual one.

    The ``list`` aggregation and the ``fetch`` default range/currency derivation both
    live in ``queries.py`` (:func:`queries.get_exchange_rates`,
    :func:`queries.default_rate_fetch_span`) rather than here, so the TUI's own
    ``rates`` command (see ``tui/app.py``'s ``_do_rates``) reads and derives exactly
    the same things this does instead of a second, drifting copy.
    """
    from decimal import Decimal, InvalidOperation

    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    command = args.rates_command or "list"

    with session_factory() as session:
        if command == "set":
            try:
                rate = Decimal(args.rate)
            except InvalidOperation:
                print(f"Could not read {args.rate!r} as a rate.")
                return 1
            on = args.on or date.today()
            rates_module.record_rate(
                session, on, args.base, args.quote, rate, rates_module.MANUAL
            )
            session.commit()
            print(f"Set {args.base} -> {args.quote} = {rate} on {on.isoformat()} (manual).")
            return 0

        if command == "fetch":
            # Base on the currency totals are actually reported in. rate_on can invert
            # a cached pair but cannot chain one pair through another, so basing on
            # anything else would leave the conversions that matter unreachable: with a
            # USD home and a CHF base, EUR -> USD could not be resolved at all. Basing
            # here means every X -> home lookup is either cached or a single inversion.
            base = queries.HOME_CURRENCY
            derived = queries.default_rate_fetch_span(session, base)
            if args.start is not None and args.end is not None:
                start, end = args.start, args.end
            elif derived is not None:
                start = args.start or derived[0]
                end = args.end or derived[1]
            else:
                start = end = None
            if start is None or end is None:
                print(
                    "No transactions in the database to derive a date range from; "
                    "pass --start and --end."
                )
                return 1
            print(f"Range: {start.isoformat()}..{end.isoformat()}")

            if derived is not None:
                quotes = derived[2]
            else:
                # Explicit --start/--end with no transactions on file at all: quotes
                # come from the accounts alone, same as default_rate_fetch_span would
                # derive if it had a date range to anchor on.
                quotes = [
                    c for c in sorted({row.currency for row in queries.get_accounts(session)})
                    if c != base
                ]
            if not quotes:
                print(f"Only one currency on file ({base or 'none'}); nothing to fetch.")
                return 0

            try:
                written = rates_module.fetch_ecb_rates(session, start, end, base, quotes)
            except rates_module.FrankfurterError as error:
                print(f"Could not fetch ECB rates: {error}")
                return 1
            session.commit()
            print(f"Wrote {written} rate(s) for {base} -> {', '.join(quotes)}.")
            return 0

        rows = queries.get_exchange_rates(session)  # "list"

    if not rows:
        print("No exchange rates cached yet. Run 'budget rates fetch' or 'budget rates set'.")
        return 0
    for row in rows:
        span = (
            row.first_day if row.first_day == row.last_day
            else f"{row.first_day}..{row.last_day}"
        )
        plural = "" if row.count == 1 else "s"
        print(f"  {row.base} -> {row.quote}   {row.source:<8} {span:<23} {row.count} rate{plural}")
    return 0
