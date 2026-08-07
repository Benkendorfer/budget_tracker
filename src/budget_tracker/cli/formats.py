"""The ``format`` command: inspect CSV layouts learned during import."""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import formats
from ..db import get_engine, get_sessionmaker, init_db


def _cmd_format(args: argparse.Namespace) -> int:
    import json

    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)

    with session_factory() as session:
        try:
            if args.format_command == "invert":
                spec = formats.set_invert_amount(session, args.name, args.state == "on")
                session.commit()
                state = "on" if spec.invert_amount else "off"
                print(f"{spec.name!r}: invert {state}.")
                return 0

            if args.format_command == "prefix":
                spec = formats.set_account_prefix(session, args.name, args.prefix)
                session.commit()
                print(
                    f"{spec.name!r} now names accounts "
                    f"{spec.account_prefix + '<value>' if spec.account_prefix else '<value>'}."
                )
                return 0

            if args.format_command == "remove":
                if not formats.remove_format(session, args.name):
                    print(f"No format named {args.name!r}.")
                    return 1
                session.commit()
                print(f"Removed format {args.name!r}.")
                return 0

            if args.format_command == "export":
                specs = (
                    [formats.get_format(session, args.name)]
                    if args.name
                    else formats.list_formats(session)
                )
                payload = [formats.to_dict(s) for s in specs]
                text = json.dumps(payload[0] if args.name else payload, indent=2)
                if args.output:
                    Path(args.output).expanduser().write_text(text, encoding="utf-8")
                    print(f"Wrote {len(specs)} format(s) to {args.output}.")
                else:
                    print(text)
                return 0

            specs = formats.list_formats(session)  # "list"
        except (formats.InvalidFormat, formats.UnknownFormat) as error:
            print(error)
            return 1

    if not specs:
        print(
            "No CSV layouts learned yet. Run 'budget import <file>' and you will be "
            "walked through the first one."
        )
        return 0
    width = max(len(s.name) for s in specs)
    for spec in specs:
        account = spec.account_column or "requires --account"
        # invert only means anything for a single signed column; a debit/credit pair
        # already says which side is an outflow, so it is left off there.
        polarity = (
            f"  invert: {'on' if spec.invert_amount else 'off'}"
            if spec.amount_style == formats.SIGNED
            else ""
        )
        print(f"  {spec.name:<{width}}  {spec.amount_style:<13}  account: {account}{polarity}")
    return 0
