"""The ``trips`` command: list trips, and manage the travel-bucket map.

Follows ``tags.py``'s convention: :mod:`..trips` itself never commits, so every write
here owns its own ``session.commit()``. ``budget trips`` and ``budget trips buckets``
both seed the bucket map first (:func:`..trips.seed_default_buckets`) so the CLI is
useful on a database that has never opened the trips panel; seeding is idempotent and
never overwrites a row the user already set.
"""

from __future__ import annotations

import argparse
from datetime import date
from typing import Optional

from .. import queries
from .. import trips as trips_module
from ..db import get_engine, get_sessionmaker, init_db


def _format_dates(start: Optional[date], end: Optional[date]) -> str:
    """``2026-03-02..03-14`` -- the end's year elided when it matches the start's,
    since a trip is usually inside one stretch of the calendar. Blank for a trip with
    no transactions (both ``None``); a single date for a one-day trip.
    """
    if start is None or end is None:
        return ""
    if start == end:
        return start.isoformat()
    if end.year == start.year:
        return f"{start.isoformat()}..{end:%m-%d}"
    return f"{start.isoformat()}..{end.isoformat()}"


def _format_breakdown(row: queries.TripRow) -> str:
    """Per-bucket percentages, skipping any bucket the trip spent nothing in.

    A bucket whose net is a refund (negative -- see ``queries.get_trips``) is clamped
    to 0 here, the same "bar can't show a negative segment" rule the TUI's bar applies,
    so this is a text rendering of the same proportions rather than a second policy.

    A bucket that is real but tiny -- one paperback on a two-week trip -- would round
    to ``0%``, which reads as a bug rather than as "small", so it prints ``<1%``. The
    panel does the same thing at its own precision (``<0.1%``).
    """
    clamped = [max(cost, 0) for cost in row.buckets]
    denominator = sum(clamped)
    if denominator <= 0:
        return ""
    parts = []
    for bucket, cost in zip(trips_module.BUCKETS, clamped):
        if cost <= 0:
            continue
        share = cost * 100 / denominator
        parts.append(f"{bucket} <1%" if share < 0.5 else f"{bucket} {share:.0f}%")
    return ", ".join(parts)


def _print_trips(rows) -> None:
    if not rows:
        print("No trips yet.")
        return
    date_width = max((len(_format_dates(r.start, r.end)) for r in rows), default=0)
    name_width = max(len(r.name) for r in rows)
    for row in rows:
        dates = _format_dates(row.start, row.end)
        print(
            f"  {dates:<{date_width}}  {row.name:<{name_width}}  "
            f"{row.count:>6} txns  {row.total_minor / 100:>12,.2f}  "
            f"{_format_breakdown(row)}"
        )


def _print_buckets(grouped) -> None:
    for bucket in trips_module.BUCKETS:
        paths = grouped[bucket]
        print(f"{bucket}:")
        if not paths:
            print("  (none)")
        else:
            for path in paths:
                print(f"  {path}")


def _cmd_trips(args: argparse.Namespace) -> int:
    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)

    # argparse defaults a subparser's dest to None when the subcommand is omitted,
    # which wins over the parser-level default -- same trap as `tags`.
    command = args.trips_command or "list"

    with session_factory() as session:
        if command == "buckets":
            trips_module.seed_default_buckets(session)
            session.commit()
            grouped = trips_module.list_buckets(session)
            _print_buckets(grouped)
            return 0

        if command == "bucket":
            trips_module.seed_default_buckets(session)
            if args.clear:
                categories = args.categories
                try:
                    removed = trips_module.clear_bucket(session, categories)
                except ValueError as error:
                    print(error)
                    return 1
                session.commit()
                noun = "category" if removed == 1 else "categories"
                print(f"Unmapped {removed} {noun}.")
                return 0

            if len(args.categories) < 2:
                print(
                    "Usage: budget trips bucket <category>... <bucket>, "
                    "or --clear to unmap."
                )
                return 1
            *categories, bucket = args.categories
            try:
                written = trips_module.set_bucket(session, categories, bucket)
            except ValueError as error:
                print(error)
                return 1
            session.commit()
            noun = "category" if written == 1 else "categories"
            print(f"Set {written} {noun} to {bucket!r}.")
            return 0

        # "list"
        trips_module.seed_default_buckets(session)
        session.commit()
        rows = queries.get_trips(session)

    _print_trips(rows)
    return 0
