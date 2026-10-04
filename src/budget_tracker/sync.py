"""Pull transactions from a SimpleFIN (or compatible) server into the local database.

:mod:`.simplefin` only speaks the protocol; this module owns everything that touches
the database -- which remote account maps to which local one, which remote
transactions are already here under a different hat (a CSV import), and which are
genuinely new.

The central problem this module solves is the **overlap**: the user's first sync
happens weeks or months after their last CSV import, and SimpleFIN cannot know which
of the transactions in that gap the CSV already recorded. So every sync re-fetches a
little further back than its own high-water mark (:data:`OVERLAP_DAYS`) and tries to
match each remote row against an existing CSV-imported one before ever inserting a
new :class:`~.models.Transaction` -- see :func:`_find_csv_match` and the algorithm in
:func:`run_sync`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import importer
from .models import (
    SYNC_ERROR,
    SYNC_OK,
    Account,
    Currency,
    Import,
    SyncAccount,
    SyncConnection,
    Transaction,
)
from .simplefin import (
    AccountSet,
    AuthFailed,
    RemoteAccount,
    RemoteError,
    RemoteTransaction,
    SimpleFINError,
)

# Re-fetch this far back before the high-water mark (whether that is a previous sync's
# ``synced_through`` or, on a first sync, the CSV cutoff) so a provider that is still
# settling a transaction's final posted date gets another look, and so the overlap
# match below has something to compare against.
OVERLAP_DAYS = 10

# |date difference| allowed when matching a remote row to an existing CSV-imported one.
# Wider than a day because a card's "posted" date and a bank export's "Date" column
# routinely disagree by a day or two for the same purchase.
MATCH_WINDOW_DAYS = 3

DEFAULT_CONNECTION = "simplefin"



@dataclass(frozen=True)
class Provider:
    """A server that speaks the SimpleFIN protocol. They differ only in name and in how
    much history they keep, which is what the gap warnings measure against."""

    label: str
    history_days: int


# Keyed by SyncConnection.provider. Synci serves UK/EU banks through open banking; its
# Basic plan keeps 60 days (Pro keeps two years -- 60 errs toward warning, not missing).
PROVIDERS = {
    "simplefin": Provider("SimpleFIN", 90),
    "synci": Provider("Synci", 60),
}


def provider_of(connection: SyncConnection) -> Provider:
    return PROVIDERS.get(connection.provider, PROVIDERS["simplefin"])


def _max_lookback_days(provider: Provider) -> int:
    """The provider's history, minus one: the earliest day a sync can ever ask for,
    measured back from "today"."""
    return provider.history_days - 1


class SyncError(Exception):
    """Base for everything this module raises."""


class UnknownConnection(SyncError):
    """No :class:`~.models.SyncConnection` exists with the given name."""


class MissingCredentials(SyncError):
    """The connection row exists, but its keychain entry does not."""


class AlreadyConnected(SyncError):
    """A connection (or a stored secret) with this name already exists."""


@dataclass
class AccountSyncResult:
    account_name: str
    remote_name: str
    start: date
    fetched: int = 0  # posted (non-pending) remote txns in window
    inserted: int = 0
    already_synced: int = 0  # import_hash already present
    matched_existing: int = 0  # matched a CSV-imported row, skipped
    skipped_pending: int = 0
    inserted_before_cutoff: int = 0  # inserted, but dated <= the CSV cutoff
    warnings: List[str] = field(default_factory=list)
    # True exactly when a warning says there is (or may be) a real hole in coverage --
    # a definite 90-day-window gap, or unconfirmed reach-back -- as opposed to the
    # thin-overlap warning, which just means less re-checking happened than usual.
    gap_warning: bool = False


@dataclass
class SyncResult:
    connection_name: str
    dry_run: bool
    provider_label: str = "SimpleFIN"
    import_id: Optional[int] = None  # None if nothing inserted, or a dry run
    accounts: List[AccountSyncResult] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)  # remote errlist messages
    unmapped: List[str] = field(default_factory=list)  # remote accounts with no mapping


@dataclass
class ConnectionAccountStatus:
    local_name: str
    remote_name: str
    synced_through: Optional[date]
    last_synced_at: Optional[datetime]
    last_status: Optional[str] = None  # SYNC_OK / SYNC_ERROR / None (never synced)
    last_error: Optional[str] = None


@dataclass
class ConnectionStatus:
    name: str
    provider: str
    accounts: List[ConnectionAccountStatus] = field(default_factory=list)


def _require_connection(session: Session, name: str) -> SyncConnection:
    connection = session.scalar(
        select(SyncConnection).where(SyncConnection.name == name)
    )
    if connection is None:
        raise UnknownConnection(f"No sync connection named {name!r}.")
    return connection


def _is_url_currency(code: str) -> bool:
    """A SimpleFIN custom currency is expressed as a URL instead of an ISO code."""
    return "://" in code


def _matching_errors(
    errors: Sequence[RemoteError], remote_id: str, conn_id: Optional[str]
) -> List[RemoteError]:
    """``errlist`` entries that name this account directly, or name the connection
    it belongs to -- a provider can report a connection-level problem (e.g. an auth
    error) on an account it still returns, so presence in the response alone is not
    enough to call the account's own sync a success.
    """
    return [
        e
        for e in errors
        if e.account_id == remote_id
        or (conn_id is not None and _same_connection(e.conn_id, conn_id))
    ]


def _same_connection(a: Optional[str], b: Optional[str]) -> bool:
    """Whether two conn_ids name the same connection.

    Not plain equality: SimpleFIN Bridge names a connection ``MX-MBR-<uuid>`` on its
    accounts and in ``connections``, but ``MBR-<uuid>`` in the errlist entry about it
    (seen on a real ``con.auth`` error). So one may carry an extra leading ``<PREFIX>-``
    that the other lacks; the rest must match exactly.
    """
    if not a or not b:
        return False
    return a == b or a.endswith("-" + b) or b.endswith("-" + a)


def _join_error_messages(errors: Sequence[RemoteError]) -> str:
    return "; ".join(f"{e.code}: {e.msg}" for e in errors)


# ------------------------------------------------------------------- connection setup


def connect(
    session: Session,
    setup_token: str,
    name: str = DEFAULT_CONNECTION,
    *,
    provider: str = "simplefin",
    claim: Optional[Callable[..., str]] = None,
    store_secret: Optional[Callable[[str, str], None]] = None,
    load_secret: Optional[Callable[[str], Optional[str]]] = None,
    fetch: Optional[Callable[..., AccountSet]] = None,
) -> AccountSet:
    """Claim ``setup_token`` under ``name`` and fetch balances for the caller to map.

    Refuses *before* claiming if a connection or a stored secret already uses this
    name -- a setup token can only be claimed once, so claiming one that would be
    refused anyway burns it for nothing. Once claimed, the access URL is stored right
    away, before the :class:`~.models.SyncConnection` row even exists: losing it from
    here on would strand the user with a claimed token and no way to get the access
    URL back, which is worse than a connection row existing without one yet.
    """
    if claim is None:
        from .simplefin import claim as claim
    if store_secret is None:
        from .credentials import store as store_secret
    if load_secret is None:
        from .credentials import load as load_secret
    if fetch is None:
        from .simplefin import fetch_accounts as fetch

    name = name.strip()
    if provider not in PROVIDERS:
        raise SyncError(
            f"Unknown provider {provider!r}; expected one of {', '.join(PROVIDERS)}."
        )
    existing = session.scalar(select(SyncConnection).where(SyncConnection.name == name))
    if existing is not None or load_secret(name) is not None:
        raise AlreadyConnected(
            f"A connection named {name!r} already exists; disconnect it first or "
            "choose a different name."
        )

    access_url = claim(setup_token)
    store_secret(name, access_url)

    connection = SyncConnection(name=name, provider=provider)
    session.add(connection)
    session.commit()

    return fetch(access_url, balances_only=True)


def remote_accounts(
    session: Session,
    name: str = DEFAULT_CONNECTION,
    *,
    load_secret: Optional[Callable[[str], Optional[str]]] = None,
    fetch: Optional[Callable[..., AccountSet]] = None,
) -> AccountSet:
    """Fetch balances for an already-connected server, e.g. to map more accounts."""
    if load_secret is None:
        from .credentials import load as load_secret
    if fetch is None:
        from .simplefin import fetch_accounts as fetch

    _require_connection(session, name)
    access_url = load_secret(name)
    if access_url is None:
        raise MissingCredentials(
            f"No stored credentials for connection {name!r}; run connect again."
        )
    return fetch(access_url, balances_only=True)


def map_account(
    session: Session, name: str, remote: RemoteAccount, account_name: str
) -> SyncAccount:
    """Point ``remote`` at the local account named ``account_name``, creating it if
    it does not exist. Re-mapping an already-mapped remote id updates it. Commits.
    """
    connection = _require_connection(session, name)
    if _is_url_currency(remote.currency):
        raise SyncError(
            f"{remote.name!r} uses a custom currency ({remote.currency}), which this "
            "app cannot record; map a different account."
        )
    currency = importer._get_or_create_currency(session, remote.currency)
    account_name = account_name.strip()
    # Raises formats.AccountCurrencyMismatch if an existing account of this name is in
    # a different currency -- the same refusal a CSV import would give.
    account = importer._get_or_create_account(session, account_name, currency)

    existing_mapping = session.scalar(
        select(SyncAccount).where(
            SyncAccount.connection_id == connection.id,
            SyncAccount.remote_id == remote.id,
        )
    )
    # One local account can hold at most one remote account's data (see
    # SyncAccount's unique constraint); checked here too so the failure is this
    # module's own SyncError rather than a raw IntegrityError from the commit below.
    clash = session.scalar(select(SyncAccount).where(SyncAccount.account_id == account.id))
    if clash is not None and clash is not existing_mapping:
        # The account above may have just been created; refusing must not leave it
        # behind for the caller's next commit to write.
        session.rollback()
        raise SyncError(
            f"Account {account_name!r} is already mapped to a different remote "
            f"account ({clash.remote_name!r}); unmap it first."
        )

    if existing_mapping is None:
        mapping = SyncAccount(
            connection_id=connection.id,
            remote_id=remote.id,
            remote_name=remote.name,
            account_id=account.id,
        )
        session.add(mapping)
    else:
        existing_mapping.remote_name = remote.name
        existing_mapping.account_id = account.id
        mapping = existing_mapping

    session.commit()
    return mapping


def unmap_account(session: Session, name: str, remote_id: str) -> bool:
    """Remove the mapping for ``remote_id``. Leaves transactions untouched."""
    connection = _require_connection(session, name)
    mapping = session.scalar(
        select(SyncAccount).where(
            SyncAccount.connection_id == connection.id,
            SyncAccount.remote_id == remote_id,
        )
    )
    if mapping is None:
        return False
    session.delete(mapping)
    session.commit()
    return True


def disconnect(
    session: Session,
    name: str = DEFAULT_CONNECTION,
    *,
    delete_secret: Optional[Callable[[str], bool]] = None,
) -> None:
    """Delete the connection and its mappings, and the keychain entry. Never deletes
    transactions; Import rows the connection wrote keep existing, with
    ``sync_connection_id`` cleared first -- otherwise deleting the connection would
    fail the foreign key check those rows still hold.
    """
    if delete_secret is None:
        from .credentials import delete as delete_secret

    connection = _require_connection(session, name)
    for record in session.scalars(
        select(Import).where(Import.sync_connection_id == connection.id)
    ):
        record.sync_connection_id = None
    session.flush()
    # sync_account rows cascade (ON DELETE CASCADE); nothing references them that
    # doesn't.
    session.delete(connection)
    session.commit()
    delete_secret(name)


def list_connections(session: Session) -> List[ConnectionStatus]:
    statuses = []
    for connection in session.scalars(
        select(SyncConnection).order_by(SyncConnection.name)
    ):
        accounts = []
        for mapping in session.scalars(
            select(SyncAccount).where(SyncAccount.connection_id == connection.id)
        ):
            local = session.get(Account, mapping.account_id)
            accounts.append(
                ConnectionAccountStatus(
                    local_name=local.name if local else "?",
                    remote_name=mapping.remote_name,
                    synced_through=mapping.synced_through,
                    last_synced_at=mapping.last_synced_at,
                    last_status=mapping.last_status,
                    last_error=mapping.last_error,
                )
            )
        statuses.append(
            ConnectionStatus(
                name=connection.name, provider=connection.provider, accounts=accounts
            )
        )
    return statuses


# ------------------------------------------------------------------------------ sync


def _csv_cutoff(session: Session, account_id: int) -> Optional[date]:
    """Latest ``posted_date`` of a CSV-imported transaction in this account.

    "CSV-imported" means its Import row has no ``sync_connection_id`` -- the one place
    a transaction's origin is recorded (see :class:`~.models.Import`). A transaction
    with no Import at all (hand-seeded, e.g. in a test) is neither, so it is excluded
    by the join rather than counted as CSV-origin.
    """
    return session.scalar(
        select(func.max(Transaction.posted_date))
        .join(Import, Transaction.import_id == Import.id)
        .where(
            Transaction.account_id == account_id,
            Import.sync_connection_id.is_(None),
        )
    )


@dataclass
class _AccountPlan:
    mapping: SyncAccount
    start: date
    cutoff: Optional[date]  # CSV cutoff, independent of what the anchor is
    anchor: Optional[date]  # what OVERLAP_DAYS is measured back from
    warnings: List[str]
    gap_warning: bool


def _account_start(
    session: Session, mapping: SyncAccount, today: date, provider: Provider
) -> _AccountPlan:
    """Where to start fetching ``mapping`` from, and any coverage warnings that come
    with it.

    The *anchor* is ``synced_through`` once a sync has ever run, else the CSV cutoff
    (the newest transaction a CSV import already recorded in this account) -- that is
    what :data:`OVERLAP_DAYS` is measured back from. With no anchor at all (a brand
    new account, no CSV history) there is nothing to warn about: a fresh 89-day fetch
    is exactly what was asked for, not a shortfall.
    """
    floor_start = today - timedelta(days=_max_lookback_days(provider))
    cutoff = _csv_cutoff(session, mapping.account_id)
    anchor = mapping.synced_through if mapping.synced_through is not None else cutoff

    if anchor is None:
        return _AccountPlan(mapping, floor_start, cutoff, None, [], False)

    desired_start = anchor - timedelta(days=OVERLAP_DAYS)
    start = max(desired_start, floor_start)
    warnings: List[str] = []
    gap_warning = False

    # How many days short of the anchor the history window itself falls -- more than
    # one means some days between the anchor and the window's edge are permanently
    # unreachable, not just un-re-checked.
    days_short = (floor_start - anchor).days
    if days_short > 1:
        gap_start = anchor + timedelta(days=1)
        gap_end = floor_start - timedelta(days=1)
        warnings.append(
            f"Transactions from {gap_start.isoformat()} through {gap_end.isoformat()} "
            f"are older than {provider.label}'s {provider.history_days}-day history "
            "and cannot be synced; import them from a CSV if you need them."
        )
        gap_warning = True
    elif desired_start < floor_start:
        overlap_days = max(0, (anchor - floor_start).days)
        warnings.append(
            f"Only {overlap_days} day(s) of overlap before {anchor.isoformat()} "
            f"(wanted {OVERLAP_DAYS}); a transaction near the edge of the window may "
            "not get a chance to match an existing CSV row."
        )

    return _AccountPlan(mapping, start, cutoff, anchor, warnings, gap_warning)


def _find_csv_match(
    session: Session,
    account_id: int,
    value_minor: int,
    txn_date: date,
    claimed: set,
) -> Optional[int]:
    """The CSV-imported transaction ``txn_date``/``value_minor`` most likely matches,
    or None. Closest date wins, then lowest id; already-claimed rows (by an earlier
    remote transaction in the same run) are never offered twice.
    """
    low = txn_date - timedelta(days=MATCH_WINDOW_DAYS)
    high = txn_date + timedelta(days=MATCH_WINDOW_DAYS)
    candidates = [
        row
        for row in session.scalars(
            select(Transaction)
            .join(Import, Transaction.import_id == Import.id)
            .where(
                Transaction.account_id == account_id,
                Transaction.value_minor == value_minor,
                Transaction.posted_date >= low,
                Transaction.posted_date <= high,
                Import.sync_connection_id.is_(None),
            )
        )
        if row.id not in claimed
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda t: (abs((t.posted_date - txn_date).days), t.id))
    return candidates[0].id


@dataclass
class _PendingInsert:
    local_account: Account
    currency: Currency
    remote_txn: RemoteTransaction
    txn_date: date
    value_minor: int
    import_hash: str


@dataclass
class _MappingOutcome:
    """What one mapping's sync produced, applied to its ORM row right before this
    connection's own commit/rollback -- never before, so a dry run's rollback discards
    all of it together. ``synced`` is True only for a mapping that reached the fetch
    loop (remote account present, currency ok); that is what gates touching
    ``last_synced_at``/``synced_through`` at all, independent of whether ``status`` is
    also set.
    """

    status: Optional[str] = None
    error: Optional[str] = None
    synced: bool = False
    latest_fetched: Optional[date] = None


@dataclass
class _Classification:
    """The result of classifying every mapped account's remote transactions."""

    account_results: List[AccountSyncResult] = field(default_factory=list)
    insert_buffer: List[_PendingInsert] = field(default_factory=list)
    accounts_with_rows: set = field(default_factory=set)
    outcomes: Dict[int, _MappingOutcome] = field(default_factory=dict)  # by mapping.id
    inverted: List[Tuple[SyncAccount, str]] = field(default_factory=list)
    inverted_messages: List[str] = field(default_factory=list)


def run_sync(
    session: Session,
    name: Optional[str] = None,
    *,
    dry_run: bool = False,
    today: Optional[date] = None,
    load_secret: Optional[Callable[[str], Optional[str]]] = None,
    fetch: Optional[Callable[..., AccountSet]] = None,
) -> List[SyncResult]:
    """Sync one connection, or (``name`` is None) every connection in the database.

    Each connection is synced independently and in full before the next starts, and
    any exception (``MissingCredentials``, a re-raised ``AuthFailed``, an inverted-sign
    ``SyncError``) propagates immediately rather than being folded into that
    connection's result -- these all name a connection the user needs to act on, and
    silently skipping to the next one would bury that.
    """
    if load_secret is None:
        from .credentials import load as load_secret
    if fetch is None:
        from .simplefin import fetch_accounts as fetch
    if today is None:
        today = date.today()

    if name is not None:
        connections = [_require_connection(session, name)]
    else:
        connections = list(
            session.scalars(select(SyncConnection).order_by(SyncConnection.name))
        )

    return [
        _sync_one_connection(
            session,
            connection,
            dry_run=dry_run,
            today=today,
            load_secret=load_secret,
            fetch=fetch,
        )
        for connection in connections
    ]


def _fail_connection(
    session: Session, mappings: Sequence[SyncAccount], message: str, dry_run: bool
) -> None:
    """Mark every mapping of a connection as errored and commit, for a failure that
    happens before anything else is written -- a missing secret or a fetch failure.
    Skipped on a dry run, since there would be nothing to roll back; the caller raises
    either way.
    """
    if dry_run:
        return
    for mapping in mappings:
        mapping.last_status = SYNC_ERROR
        mapping.last_error = message
    session.commit()


def _build_plans(
    session: Session, mappings: Sequence[SyncAccount], today: date, provider: Provider
) -> Dict[str, _AccountPlan]:
    return {
        plan.mapping.remote_id: plan
        for plan in (_account_start(session, mapping, today, provider) for mapping in mappings)
    }


def _fetch_remote(
    session: Session,
    connection: SyncConnection,
    provider: Provider,
    secret: str,
    mappings: Sequence[SyncAccount],
    plans: Dict[str, _AccountPlan],
    today: date,
    fetch: Callable[..., AccountSet],
    dry_run: bool,
) -> AccountSet:
    """Fetch this connection's window. A failure here is connection-level, not
    account-level -- the whole fetch failed, not one account's data -- so every
    mapping is marked error (unless this is a dry run) and the exception propagates.
    """
    window_start = min(
        (p.start for p in plans.values()),
        default=today - timedelta(days=_max_lookback_days(provider)),
    )
    window_end = today + timedelta(days=1)

    try:
        return fetch(
            secret,
            start=window_start,
            end=window_end,
            account_ids=[m.remote_id for m in mappings],
        )
    except AuthFailed as error:
        message = (
            f"Access for connection {connection.name!r} was revoked; run "
            f"'budget sync connect' again. ({error})"
        )
        _fail_connection(session, mappings, message, dry_run)
        raise SyncError(message) from error
    except SimpleFINError as error:
        _fail_connection(session, mappings, str(error), dry_run)
        raise


def _currency_issue(
    remote_account: RemoteAccount, local_account: Account, local_currency: Currency
) -> Optional[str]:
    """None, or why this account can't be synced into ``local_account`` as-is."""
    if _is_url_currency(remote_account.currency):
        return (
            f"{remote_account.name!r} uses a custom currency "
            f"({remote_account.currency}), which this app cannot record; skipped."
        )
    if local_currency.value != remote_account.currency:
        return (
            f"{remote_account.name!r} is {remote_account.currency} but local "
            f"account {local_account.name!r} is {local_currency.value}; skipped."
        )
    return None


def _collect_account_inserts(
    session: Session,
    plan: _AccountPlan,
    local_account: Account,
    local_currency: Currency,
    remote_account: RemoteAccount,
    claimed_csv_ids: set,
    account_result: AccountSyncResult,
) -> Tuple[List[_PendingInsert], List[RemoteTransaction]]:
    """Walk one account's remote transactions, updating ``account_result``'s counters
    as it goes, and return the rows that are genuinely new -- not already synced
    (``import_hash`` present), not matched to an existing CSV row -- alongside every
    non-pending row considered, which the caller needs for ``synced_through``.
    """
    # Pending rows are kept regardless of date -- their ``posted`` is the epoch (1970)
    # -- so the loop below can count them as skipped rather than silently dropping
    # them here.
    #
    # Filtered on ``posted``, not ``transacted``/``txn_date`` -- the server's own
    # start-date filter works on posted dates, and so must ours: a charge that
    # stays pending for a while posts well after its purchase date, and by the time
    # it posts, ``plan.start`` (anchored on synced_through) may have moved past its
    # purchase date already. Filtering on the purchase date would then drop it on
    # every sync from here on, having never been inserted while pending. The row
    # that gets inserted still carries ``txn_date = transacted or posted`` (see
    # below) -- only which rows make it into the window changes here.
    txns_in_window = [
        t for t in remote_account.transactions if t.pending or t.posted >= plan.start
    ]

    account_inserts: List[_PendingInsert] = []
    for txn in txns_in_window:
        if txn.pending:
            account_result.skipped_pending += 1
            continue
        account_result.fetched += 1
        txn_date = txn.transacted or txn.posted
        import_hash = importer._row_hash("simplefin", remote_account.id, txn.id)
        already = session.scalar(
            select(Transaction.id).where(Transaction.import_hash == import_hash)
        )
        if already is not None:
            account_result.already_synced += 1
            continue

        scale = 10 ** local_currency.decimal_places
        value_minor = int((txn.amount * scale).to_integral_value())

        match_id = _find_csv_match(
            session, local_account.id, value_minor, txn_date, claimed_csv_ids
        )
        if match_id is not None:
            claimed_csv_ids.add(match_id)
            account_result.matched_existing += 1
            continue

        account_inserts.append(
            _PendingInsert(
                local_account=local_account,
                currency=local_currency,
                remote_txn=txn,
                txn_date=txn_date,
                value_minor=value_minor,
                import_hash=import_hash,
            )
        )
        account_result.inserted += 1
        if plan.cutoff is not None and txn_date <= plan.cutoff:
            account_result.inserted_before_cutoff += 1

    return account_inserts, txns_in_window


def _add_coverage_warning(
    remote_account: RemoteAccount, plan: _AccountPlan, account_result: AccountSyncResult
) -> None:
    """Flag when the raw (unwindowed) response doesn't reach back to the anchor --
    SimpleFIN can return less history than asked for. Worded "could not confirm", not
    "gap": a genuinely low-volume account can trigger this honestly with nothing
    actually missing.
    """
    if plan.anchor is None:
        return
    posted_all = [t for t in remote_account.transactions if not t.pending]
    if not posted_all:
        account_result.warnings.append(
            f"Could not confirm the data reaches back to "
            f"{plan.anchor.isoformat()}; no transactions were returned for "
            "this account, so there may be a gap."
        )
        account_result.gap_warning = True
        return
    earliest = min(t.transacted or t.posted for t in posted_all)
    if earliest > plan.anchor:
        account_result.warnings.append(
            f"Could not confirm the data reaches back to "
            f"{plan.anchor.isoformat()}; the earliest transaction "
            f"received was {earliest.isoformat()}, so there may be a gap "
            "between them."
        )
        account_result.gap_warning = True


def _sign_inversion_message(
    session: Session,
    local_account: Account,
    remote_account: RemoteAccount,
    plan: _AccountPlan,
    account_inserts: Sequence[_PendingInsert],
    claimed_csv_ids: set,
) -> Optional[str]:
    """None, or a message if unmatched rows inside the CSV-overlap window would mostly
    match if the amount were negated -- the provider's sign convention is backwards,
    not that 15+ purchases all genuinely vanished (Amex's polarity was wrong once
    already).
    """
    if plan.cutoff is None:
        return None
    overlap_unmatched = [i for i in account_inserts if i.txn_date <= plan.cutoff]
    if len(overlap_unmatched) < 3:
        return None
    flipped = sum(
        1
        for item in overlap_unmatched
        if _find_csv_match(
            session, local_account.id, -item.value_minor, item.txn_date, claimed_csv_ids
        )
        is not None
    )
    if flipped * 2 < len(overlap_unmatched):
        return None
    return (
        f"{remote_account.name!r}: {flipped} of "
        f"{len(overlap_unmatched)} unmatched transactions would match "
        "the existing CSV rows with the amount sign flipped. The "
        "provider's sign convention looks inverted."
    )


def _classify_accounts(
    session: Session,
    plans: Dict[str, _AccountPlan],
    remote_by_id: Dict[str, RemoteAccount],
    account_set: AccountSet,
    provider: Provider,
) -> _Classification:
    """Classify every mapped account's remote transactions into already-synced,
    matched-to-a-CSV-row, and genuinely new, and collect the coverage/sign-inversion
    warnings that come with it. Writes nothing; the caller decides what to do with the
    result.
    """
    classification = _Classification()
    claimed_csv_ids: set = set()

    for remote_id, plan in plans.items():
        mapping = plan.mapping
        local_account = session.get(Account, mapping.account_id)
        account_result = AccountSyncResult(
            account_name=local_account.name,
            remote_name=mapping.remote_name,
            start=plan.start,
        )
        account_result.warnings.extend(plan.warnings)
        account_result.gap_warning = plan.gap_warning
        classification.account_results.append(account_result)

        remote_account = remote_by_id.get(remote_id)
        if remote_account is None:
            # No conn_id to match on without the remote account itself; account_id is
            # all errlist has to go on here.
            matches = _matching_errors(account_set.errors, remote_id, None)
            reason = (
                _join_error_messages(matches)
                if matches
                else f"{mapping.remote_name!r} was not returned by {provider.label}."
            )
            classification.outcomes[mapping.id] = _MappingOutcome(
                status=SYNC_ERROR, error=reason
            )
            continue

        local_currency = session.get(Currency, local_account.currency_id)
        issue = _currency_issue(remote_account, local_account, local_currency)
        if issue is not None:
            account_result.warnings.append(issue)
            classification.outcomes[mapping.id] = _MappingOutcome(
                status=SYNC_ERROR, error=issue
            )
            continue

        account_inserts, txns_in_window = _collect_account_inserts(
            session,
            plan,
            local_account,
            local_currency,
            remote_account,
            claimed_csv_ids,
            account_result,
        )

        # The account was returned, but a provider can still flag its connection (or
        # the account itself) as broken alongside stale data -- e.g. SimpleFIN keeps
        # returning a brokerage account after its link needs re-auth, with a con.auth
        # errlist entry riding along. Presence in the response is not success.
        account_errors = _matching_errors(
            account_set.errors, remote_id, remote_account.conn_id
        )
        if account_errors:
            status, error = SYNC_ERROR, _join_error_messages(account_errors)
        else:
            status, error = SYNC_OK, None

        _add_coverage_warning(remote_account, plan, account_result)

        inverted_message = _sign_inversion_message(
            session, local_account, remote_account, plan, account_inserts, claimed_csv_ids
        )
        if inverted_message is not None:
            account_result.warnings.append(inverted_message)
            status, error = SYNC_ERROR, inverted_message
            classification.inverted.append((mapping, inverted_message))
            classification.inverted_messages.append(inverted_message)

        classification.insert_buffer.extend(account_inserts)
        if account_inserts:
            classification.accounts_with_rows.add(local_account.id)

        latest_fetched = max(
            (t.transacted or t.posted for t in txns_in_window if not t.pending),
            default=None,
        )
        classification.outcomes[mapping.id] = _MappingOutcome(
            status=status, error=error, synced=True, latest_fetched=latest_fetched
        )

    return classification


def _refuse_for_sign_inversion(
    session: Session,
    connection: SyncConnection,
    inverted: Sequence[Tuple[SyncAccount, str]],
    messages: Sequence[str],
) -> None:
    """A refusal, not a connection failure: commit only the flagged accounts' status
    (nothing else -- no synced_through, no transactions, no other account's status)
    and raise. Never called on a dry run; see ``_sync_one_connection``.
    """
    for mapping, message in inverted:
        mapping.last_status = SYNC_ERROR
        mapping.last_error = message
    session.commit()
    raise SyncError(
        f"Connection {connection.name!r} looks sign-inverted: " + " ".join(messages)
    )


def _write_inserts(
    session: Session,
    connection: SyncConnection,
    today: date,
    insert_buffer: Sequence[_PendingInsert],
    accounts_with_rows: set,
) -> Optional[Import]:
    """Create the Import and its Transaction rows, then run the same post-insert
    pipeline as ``importer.import_csv`` (vendor rules, category rules, transfer
    detection), in the same order. None if there is nothing to insert.
    """
    if not insert_buffer:
        return None

    import_record = Import(
        source_file=f"sync:{connection.name} {today.isoformat()}",
        sync_connection_id=connection.id,
        row_count=len(insert_buffer),
    )
    if len(accounts_with_rows) == 1:
        import_record.account_id = next(iter(accounts_with_rows))
    session.add(import_record)
    session.flush()

    for item in insert_buffer:
        description = item.remote_txn.description
        vendor = (
            importer._get_or_create_vendor(session, description) if description else None
        )
        session.add(
            Transaction(
                account_id=item.local_account.id,
                category_id=None,
                currency_id=item.currency.id,
                import_id=import_record.id,
                vendor_id=vendor.id if vendor else None,
                posted_date=item.txn_date,
                description=description,
                raw_description=description,
                value_minor=item.value_minor,
                category_source="unset",
                import_hash=item.import_hash,
            )
        )

    # Same post-insert sequence as importer.import_csv, and in the same order.
    from .vendors import apply_rules

    apply_rules(session)

    from .categories import apply_category_rules

    apply_category_rules(session)

    from .transfers import detect_transfers

    detect_transfers(session)

    return import_record


def _apply_mapping_outcomes(
    mappings: Sequence[SyncAccount], outcomes: Dict[int, _MappingOutcome]
) -> None:
    """Write every mapping's final synced_through/last_synced_at/status. Called right
    before this connection's own commit/rollback -- never before -- so a dry run's
    rollback discards all of it together.
    """
    for mapping in mappings:
        outcome = outcomes.get(mapping.id)
        if outcome is None:
            continue
        if outcome.synced:
            if outcome.latest_fetched is not None:
                if (
                    mapping.synced_through is None
                    or outcome.latest_fetched > mapping.synced_through
                ):
                    mapping.synced_through = outcome.latest_fetched
            mapping.last_synced_at = datetime.utcnow()
        if outcome.status is not None:
            mapping.last_status, mapping.last_error = outcome.status, outcome.error


def _sync_one_connection(
    session: Session,
    connection: SyncConnection,
    *,
    dry_run: bool,
    today: date,
    load_secret: Callable[[str], Optional[str]],
    fetch: Callable[..., AccountSet],
) -> SyncResult:
    """Sync one connection: fetch its window, classify each mapped account's remote
    transactions, refuse outright if the provider's sign convention looks inverted,
    write whatever is left, and record status -- rolling all of it back together on a
    dry run.
    """
    provider = provider_of(connection)
    result = SyncResult(
        connection_name=connection.name, dry_run=dry_run, provider_label=provider.label
    )

    # Queried before the credentials check (and before the fetch) so a connection-level
    # failure below has something to mark error on before it raises.
    mappings = list(
        session.scalars(select(SyncAccount).where(SyncAccount.connection_id == connection.id))
    )

    secret = load_secret(connection.name)
    if secret is None:
        message = (
            f"No stored credentials for connection {connection.name!r}; run "
            "'budget sync connect' again."
        )
        # Nothing has been written yet (mappings above was only a read), so marking
        # every mapping of this connection error and committing right here is safe --
        # the function never reaches its own commit/rollback once it raises.
        _fail_connection(session, mappings, message, dry_run)
        raise MissingCredentials(message)

    plans = _build_plans(session, mappings, today, provider)
    account_set = _fetch_remote(
        session, connection, provider, secret, mappings, plans, today, fetch, dry_run
    )

    result.errors = [f"{e.code}: {e.msg}" for e in account_set.errors]
    remote_by_id = {a.id: a for a in account_set.accounts}
    result.unmapped = [a.name for a in account_set.accounts if a.id not in plans]

    classification = _classify_accounts(session, plans, remote_by_id, account_set, provider)
    result.accounts = classification.account_results

    if classification.inverted_messages and not dry_run:
        # Nothing else has been written yet -- everything in `classification` only
        # stages Python-side state until applied below.
        _refuse_for_sign_inversion(
            session, connection, classification.inverted, classification.inverted_messages
        )

    import_record = _write_inserts(
        session, connection, today, classification.insert_buffer, classification.accounts_with_rows
    )

    _apply_mapping_outcomes(mappings, classification.outcomes)
    session.flush()

    # Grabbed before the possible rollback below, which expires every ORM attribute.
    result.import_id = import_record.id if (import_record is not None and not dry_run) else None

    if dry_run:
        session.rollback()
    else:
        session.commit()

    return result
