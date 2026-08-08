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
from .. import tags as tags_module
from .. import trips as trips_module
from ..db import get_engine, get_sessionmaker, init_db


def _format_date(day: Optional[date]) -> str:
    """One end of a trip, or blank when there is nothing to derive it from.

    Start and end are separate columns rather than one ``2026-03-02..03-14`` field:
    they are two facts, they are independently overridable (see
    ``tags.set_trip_dates``), and a combined field cannot be scanned down a column or
    sorted by eye.
    """
    return day.isoformat() if day is not None else ""


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
    name_width = max(len(r.name) for r in rows)
    print(f"  {'Start':<11} {'End':<11} {'Trip':<{name_width}}")
    for row in rows:
        # A trailing "*" marks a date *derived* from the trip's transactions rather than
        # set by hand -- the flag belongs on the app's guess, which may well be wrong,
        # not on the one fact on the row nobody needs to check. Marked per end, since
        # correcting one and leaving the other derived is the usual case.
        # A trip with no transactions and no override has nothing to mark, so the
        # marker is appended only where there is a date to qualify.
        start = _format_date(row.start) + ("*" if row.start and not row.start_is_manual else "")
        end = _format_date(row.end) + ("*" if row.end and not row.end_is_manual else "")
        print(
            f"  {start:<11} {end:<11} {row.name:<{name_width}}  "
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

        if command == "dates":
            # --start/--end are separate flags, not two positionals, so either end can
            # be set on its own without the user having to restate the other. An end
            # not named is left exactly as it is (tags.KEEP), which is different from
            # --clear, which forgets both overrides and derives them again.
            if args.clear:
                start = end = None
            else:
                if args.start is None and args.end is None:
                    print(
                        "Usage: budget trips dates <trip> [--start YYYY-MM-DD] "
                        "[--end YYYY-MM-DD], or --clear to derive them again."
                    )
                    return 1
                try:
                    start = (
                        date.fromisoformat(args.start)
                        if args.start is not None
                        else tags_module.KEEP
                    )
                    end = (
                        date.fromisoformat(args.end)
                        if args.end is not None
                        else tags_module.KEEP
                    )
                except ValueError:
                    print("Dates must be YYYY-MM-DD.")
                    return 1
            try:
                found = tags_module.set_trip_dates(session, args.trip, start, end)
            except ValueError as error:
                print(error)
                return 1
            if not found:
                print(f"No trip named {args.trip!r}.")
                return 1
            session.commit()
            if args.clear:
                print(f"{args.trip!r}: dates back to whatever its transactions say.")
            elif end is tags_module.KEEP:
                print(f"{args.trip!r}: starts {start}.")
            elif start is tags_module.KEEP:
                print(f"{args.trip!r}: ends {end}.")
            else:
                print(f"{args.trip!r}: {start} .. {end}.")
            # Setting one end is checked against the other's *override*, which is where
            # to refuse outright. The end shown may still be the derived one, though,
            # and a manual start after a derived end reads as a trip that ended before
            # it began. Refusing would make correcting both ends one at a time
            # impossible, so say so and let the user finish.
            shown = next(
                (r for r in queries.get_trips(session) if r.name == args.trip), None
            )
            if shown and shown.start and shown.end and shown.start > shown.end:
                print("  Its start is now after its end; set the other end too.")
            return 0

        # "list"
        trips_module.seed_default_buckets(session)
        session.commit()
        rows = queries.get_trips(session)

    _print_trips(rows)
    return 0
