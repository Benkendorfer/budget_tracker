"""The ``transfers`` command: pair up (or un-pair) money moved between own accounts."""

from __future__ import annotations

import argparse

from .. import queries
from ..db import get_engine, get_sessionmaker, init_db


def _cmd_transfers(args: argparse.Namespace) -> int:
    from .. import transfers

    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        if args.reset:
            reset = transfers.clear_transfers(session)
            session.commit()
            print(f"Un-paired {reset} transaction(s).")
            return 0
        pairs = transfers.detect_transfers(
            session, window_days=args.days, allow_same_account=args.same_account
        )
        session.commit()
        totals = queries.get_totals(session)
    scope = " (same-account allowed)" if args.same_account else ""
    print(f"Found {pairs} new transfer pair(s) within {args.days} day(s){scope}.")
    print(f"{totals.transfer_count} transaction(s) are now excluded from totals.")
    return 0
