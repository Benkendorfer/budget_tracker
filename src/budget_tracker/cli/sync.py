"""The ``sync`` command family: pull transactions from a SimpleFIN connection.

``sync.py`` owns mapping, overlap matching, and writing transactions; this module is
only the terminal front end for it -- reading the setup token with the terminal hidden
(never a flag, so it never lands in shell history or ``ps``), walking the user through
mapping each remote account, and rendering a :class:`~budget_tracker.sync.SyncResult`
in a way that puts a possible gap in coverage somewhere it cannot be scrolled past.

Every exception :mod:`.sync` or the modules underneath it can raise is caught here and
printed as a message, never a traceback: :class:`~budget_tracker.sync.SyncError` (and
its subclasses), :class:`~budget_tracker.simplefin.SimpleFINError`,
:class:`~budget_tracker.credentials.CredentialsUnavailable`, and
:class:`~budget_tracker.formats.AccountCurrencyMismatch`.
"""

from __future__ import annotations

import argparse
import getpass
import subprocess
from typing import Dict, List, Optional

from sqlalchemy import select

from .. import credentials, queries, simplefin
from .. import rates as rates_module
from .. import sync as sync_module
from ..db import get_engine, get_sessionmaker, init_db
from ..formats import AccountCurrencyMismatch
from ..models import SYNC_ERROR, SYNC_OK, SyncAccount, SyncConnection
from ..simplefin import RemoteAccount

# Every exception a connection/mapping/sync call can raise that is this module's job
# to print cleanly rather than let become a traceback. sync.AlreadyConnected is a
# SyncError too, but 'connect' needs to tell it apart from the rest -- see there.
_SYNC_ERRORS = (
    sync_module.SyncError,
    simplefin.SimpleFINError,
    credentials.CredentialsUnavailable,
    AccountCurrencyMismatch,
)


def _cmd_sync(args: argparse.Namespace) -> int:
    command = args.sync_command or "run"
    if command == "run":
        return _sync_run(args)
    if command == "connect":
        return _sync_connect(args)
    if command == "map":
        return _sync_map(args)
    if command == "unmap":
        return _sync_unmap(args)
    if command == "status":
        return _sync_status(args)
    if command == "disconnect":
        return _sync_disconnect(args)
    raise AssertionError(f"unknown sync subcommand: {command!r}")  # argparse guards this


# ------------------------------------------------------------------------------- run


def _rate_fetch_summary(outcome: rates_module.ImportRatesOutcome) -> str:
    """One line describing what came of fetching rates for a sync's import, or
    nothing to say. Mirrors ``import_cmds._rate_fetch_summary`` -- see there."""
    if not outcome.attempted:
        return ""
    quotes = ", ".join(outcome.quotes)
    if outcome.error is not None:
        return (
            f"Could not fetch {queries.HOME_CURRENCY} -> {quotes} rates: {outcome.error} "
            "Run 'budget rates fetch' later."
        )
    return f"Fetched {outcome.written} rate(s) for {queries.HOME_CURRENCY} -> {quotes}."


def _contacting(labels) -> str:
    """The progress line printed before a network call, naming who is being called."""
    names = sorted(set(labels)) or ["SimpleFIN"]
    return f"Contacting {' and '.join(names)} (this can take a minute)..."


def _label_of(session, name: str) -> str:
    connection = session.scalar(select(SyncConnection).where(SyncConnection.name == name))
    return sync_module.provider_of(connection).label if connection else "SimpleFIN"


def _sync_run(args: argparse.Namespace) -> int:
    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        if session.scalar(select(SyncConnection)) is None:
            print("Nothing is connected yet; run 'budget sync connect' first.")
            return 1

        connections = session.scalars(select(SyncConnection)).all()
        if args.connection:
            connections = [c for c in connections if c.name == args.connection]
        print(
            _contacting(sync_module.provider_of(c).label for c in connections),
            flush=True,
        )
        try:
            results = sync_module.run_sync(
                session, args.connection, dry_run=args.dry_run
            )
        except _SYNC_ERRORS as error:
            print(error)
            return 1

        rate_notes: List[str] = []
        if not args.dry_run:
            for result in results:
                if result.import_id is not None:
                    outcome = rates_module.fetch_rates_for_import(
                        session, result.import_id, queries.HOME_CURRENCY
                    )
                    session.commit()
                    summary = _rate_fetch_summary(outcome)
                    if summary:
                        rate_notes.append(f"{result.connection_name}: {summary}")

    _print_sync_results(results, dry_run=args.dry_run)
    for note in rate_notes:
        print(note)
    return 0


def _print_sync_results(results: List["sync_module.SyncResult"], *, dry_run: bool) -> None:
    """Render every connection's result, counts first, with any gap warning repeated
    in its own block at the very end -- the user asked for it to be impossible to
    miss, which scrolling past it in the middle of the counts would risk.
    """
    if dry_run:
        print("DRY RUN — nothing was written.")
    print()

    # (connection name, account name, warning) for every account flagged gap_warning,
    # collected as we go and repeated in their own block once everything else has
    # printed.
    gap_warnings: List[tuple] = []

    for result in results:
        print(f"Connection {result.connection_name!r}:")
        if not result.accounts:
            print("  (no accounts mapped; run 'budget sync map')")
        for account in result.accounts:
            print(f"  {account.account_name} (remote: {account.remote_name!r})")
            print(f"    start: {account.start.isoformat()}")
            print(
                f"    fetched {account.fetched}, inserted {account.inserted}, "
                f"matched {account.matched_existing} existing CSV row(s), "
                f"already synced {account.already_synced}, "
                f"pending skipped {account.skipped_pending}"
            )
            if account.inserted_before_cutoff > 0:
                print(
                    f"    {account.inserted_before_cutoff} inserted transaction(s) "
                    "are dated on or before your last CSV import -- worth a look."
                )
            for warning in account.warnings:
                print(f"    ! {warning}")
            if account.gap_warning:
                for warning in account.warnings:
                    gap_warnings.append(
                        (result.connection_name, account.account_name, warning)
                    )
        if result.errors:
            print(f"  Errors reported by {result.provider_label}:")
            for message in result.errors:
                print(f"    - {message}")
        if result.unmapped:
            print("  Unmapped remote accounts (not synced; see 'budget sync map'):")
            for name in result.unmapped:
                print(f"    - {name}")
        print()

    if gap_warnings:
        print("⚠ POSSIBLE GAP")
        for connection_name, account_name, warning in gap_warnings:
            print(f"  [{connection_name}/{account_name}] {warning}")
        print()

    if dry_run:
        print("DRY RUN — nothing was written.")


# --------------------------------------------------------------------------- connect


def _sync_connect(args: argparse.Namespace) -> int:
    provider = args.provider
    label = sync_module.PROVIDERS[provider].label
    # Named after its provider by default, so a second provider needs no --name.
    name = (args.name or provider).strip()
    if args.clipboard:
        token = _token_from_clipboard()
        if token is None:
            return 1
    else:
        token = getpass.getpass(
            f"Paste your {label} setup token (hidden input; it works once): "
        ).strip()
    if not token:
        print("No setup token entered.")
        return 1
    # The length, never the token: enough to tell a paste that was cut short (a hidden
    # prompt on macOS stops at about 1,024 characters) from one that arrived whole.
    print(f"Read a {len(token)}-character setup token.")

    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        print(_contacting([label]), flush=True)
        try:
            account_set = sync_module.connect(session, token, name, provider=provider)
        except sync_module.AlreadyConnected as error:
            print(error)
            return 1
        except _SYNC_ERRORS as error:
            # connect() stores the secret and commits the connection row before it
            # ever calls fetch(), so a failure here can still mean the connection
            # itself is fine -- only the balances fetch that follows it isn't.
            saved = (
                session.scalar(select(SyncConnection).where(SyncConnection.name == name))
                is not None
            )
            if saved:
                print(f"Could not fetch accounts to map: {error}")
                print(
                    f"The connection {name!r} is saved; run 'budget sync map' to "
                    "pick which accounts to track."
                )
                return 0
            print(error)
            return 1

        print(f"Connected as {name!r}.")
        if account_set.accounts:
            _walk_account_mapping(session, name, account_set.accounts)
        else:
            print("No remote accounts were returned to map.")
        _print_remote_errors(account_set.errors, label)

    print("\nRun 'budget sync --dry-run' to see what the first sync would do.")
    return 0


def _token_from_clipboard() -> Optional[str]:
    """The setup token, read from the macOS clipboard with ``pbpaste``.

    For when pasting at the hidden prompt fails: nothing echoes, so a paste that did
    not land is invisible, and the terminal's line limit truncates a long token. The
    clipboard sidesteps both and still keeps the token off the command line. Any
    whitespace a copy picked up -- line breaks from a wrapped token -- is removed, the
    same as decode_setup_token would.
    """
    try:
        pasted = subprocess.run(
            ["pbpaste"], capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Could not read the clipboard ({error}); paste at the prompt instead.")
        return None
    return "".join(pasted.split())


def _sync_map(args: argparse.Namespace) -> int:
    name = (args.connection or sync_module.DEFAULT_CONNECTION).strip()

    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        label = _label_of(session, name)
        print(_contacting([label]), flush=True)
        try:
            account_set = sync_module.remote_accounts(session, name)
        except _SYNC_ERRORS as error:
            print(error)
            return 1

        if not account_set.accounts:
            print("No remote accounts were returned to map.")
            _print_remote_errors(account_set.errors, label)
            return 0

        current = _current_mapping(session, name)
        # The usual reason to run 'map' again is a newly linked account, so by default
        # only those are asked about; --all brings the mapped ones back to change them.
        if args.all:
            to_walk = list(account_set.accounts)
        else:
            to_walk = [r for r in account_set.accounts if r.id not in current]
            for remote in account_set.accounts:
                if remote.id in current:
                    print(f"Already mapped: {remote.name!r} -> {current[remote.id]!r}")
        if to_walk:
            _walk_account_mapping(session, name, to_walk, current)
        else:
            print("\nEvery remote account is already mapped. Use --all to change one.")
        _print_remote_errors(account_set.errors, label)
    return 0


def _sync_unmap(args: argparse.Namespace) -> int:
    """Break the link between a local account and whatever remote account feeds it.

    Named by the *local* account, the one the user knows: after a bank is re-linked on
    SimpleFIN, the old remote account is gone from every listing, so it could not be
    picked out by its remote name. Transactions already synced stay where they are.
    """
    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        mapping = session.scalar(
            select(SyncAccount)
            .join(SyncAccount.account)
            .where(SyncAccount.account.has(name=args.account.strip()))
        )
        if mapping is None:
            print(f"No account named {args.account!r} is linked to a sync connection.")
            return 1
        connection_name = mapping.connection.name
        remote_name = mapping.remote_name
        sync_module.unmap_account(session, connection_name, mapping.remote_id)
    print(
        f"Unlinked {args.account!r} from {remote_name!r} ({connection_name}). Its "
        "transactions are kept; run 'budget sync map' to link it to another account."
    )
    return 0


def _current_mapping(session, name: str) -> Dict[str, str]:
    """``remote_id`` -> local account name for every account already mapped under
    ``name``, so a re-run of 'map' can show (and default to keeping) it."""
    connection = session.scalar(select(SyncConnection).where(SyncConnection.name == name))
    if connection is None:
        return {}
    return {
        mapping.remote_id: mapping.account.name
        for mapping in session.scalars(
            select(SyncAccount).where(SyncAccount.connection_id == connection.id)
        )
    }


def _print_remote_errors(errors, label: str = "SimpleFIN") -> None:
    if not errors:
        return
    print(f"\n{label} reported some errors while fetching balances:")
    for error in errors:
        print(f"  - {error.code}: {error.msg}")


def _walk_account_mapping(
    session,
    name: str,
    remote_accounts,
    current: Optional[Dict[str, str]] = None,
) -> None:
    """Ask, for each remote account, which local account should hold its
    transactions -- an existing one in the same currency, a new one, or skip it.
    """
    current = current or {}
    for remote in remote_accounts:
        existing_name = current.get(remote.id)
        choice = _prompt_account_for_remote(session, remote, existing_name)
        if choice is None:
            print(f"  Skipped {remote.name!r}.")
            continue
        try:
            sync_module.map_account(session, name, remote, choice)
        except _SYNC_ERRORS as error:
            print(f"  Could not map {remote.name!r}: {error}")
            continue
        print(f"  Mapped {remote.name!r} -> {choice!r}.")


def _prompt_account_for_remote(
    session, remote: RemoteAccount, existing_name: Optional[str]
) -> Optional[str]:
    """One account's worth of the mapping walk. Re-prompts on anything it cannot
    read as a choice, the same way ``_select_csv_interactively`` does.
    """
    print()
    # The institution, when the server names it: a card's own name ("Platinum Card")
    # says nothing about which bank it is once several connections are in play.
    institution = f" at {remote.connection_name}" if remote.connection_name else ""
    print(
        f"Remote account: {remote.name!r}{institution}  "
        f"({remote.currency} {remote.balance})"
    )
    if existing_name:
        print(f"  Currently mapped to {existing_name!r}.")
    candidates = [
        account for account in queries.get_accounts(session) if account.currency == remote.currency
    ]
    for index, account in enumerate(candidates, start=1):
        print(f"  [{index}] {account.name}")
    print("  [n] New account")
    print("  [s] Skip this account")

    while True:
        if existing_name:
            prompt = "Choice (enter to keep the current mapping): "
        else:
            prompt = "Choice: "
        choice = input(prompt).strip()

        if not choice:
            if existing_name:
                return existing_name
            print("  Invalid selection.")
            continue

        lowered = choice.lower()
        if lowered in ("s", "skip"):
            return None
        if lowered in ("n", "new"):
            default_name = remote.name
            typed = input(f"  New account name [{default_name}]: ").strip()
            return typed or default_name

        try:
            index = int(choice)
        except ValueError:
            print("  Invalid selection.")
            continue
        if 1 <= index <= len(candidates):
            return candidates[index - 1].name
        print("  Invalid selection.")


# ---------------------------------------------------------------------------- status


def _sync_status(args: argparse.Namespace) -> int:
    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        statuses = sync_module.list_connections(session)

    if not statuses:
        print("No sync connections yet. Run 'budget sync connect' to add one.")
        return 0

    for status in statuses:
        print(f"{status.name} ({status.provider})")
        if not status.accounts:
            print("  (no accounts mapped yet; run 'budget sync map')")
            continue
        for account in status.accounts:
            synced = (
                account.synced_through.isoformat() if account.synced_through else "never"
            )
            last_synced = (
                account.last_synced_at.isoformat(sep=" ", timespec="seconds")
                if account.last_synced_at
                else "never"
            )
            line = (
                f"  {account.local_name:<24} remote: {account.remote_name:<24} "
                f"synced through: {synced:<10} last synced: {last_synced}"
            )
            if account.last_status == SYNC_ERROR:
                line += f"  ERROR: {account.last_error}"
            elif account.last_status == SYNC_OK:
                line += "  ok"
            print(line)
    return 0


# ------------------------------------------------------------------------ disconnect


def _sync_disconnect(args: argparse.Namespace) -> int:
    name = (args.name or sync_module.DEFAULT_CONNECTION).strip()
    answer = input(
        f"Disconnect {name!r}? Its transactions are kept; only the connection and "
        "its keychain entry are removed. [y/N] "
    ).strip().lower()
    if answer not in ("y", "yes"):
        print("Not disconnected.")
        return 0

    engine = get_engine()
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        try:
            sync_module.disconnect(session, name)
        except sync_module.SyncError as error:
            print(error)
            return 1

    print(f"Disconnected {name!r}. Its transactions are still here.")
    return 0
