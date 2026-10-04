"""Client for the SimpleFIN protocol (https://www.simplefin.org/protocol.html).

SimpleFIN Bridge is a read-only aggregator: the user links their bank there, then hands
this app a one-time *setup token*. Claiming it returns an *access URL* with credentials
embedded, which is all :mod:`.sync` needs from then on. The same protocol is spoken by
other servers (Synci, for UK and European banks), so nothing here names a provider.

This module only speaks the protocol. It knows nothing about the database; turning
remote transactions into local ones is :mod:`.sync`'s job.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class RemoteTransaction:
    id: str
    posted: date
    amount: Decimal  # positive = money in, the same convention as value_minor
    description: str
    payee: Optional[str] = None
    memo: Optional[str] = None
    transacted: Optional[date] = None
    pending: bool = False


@dataclass(frozen=True)
class RemoteAccount:
    id: str
    name: str
    conn_id: str
    currency: str  # ISO code, or a URL for a custom currency
    balance: Decimal
    balance_date: Optional[date] = None
    connection_name: str = ""
    transactions: Tuple[RemoteTransaction, ...] = ()


@dataclass(frozen=True)
class RemoteError:
    code: str
    msg: str
    conn_id: Optional[str] = None
    account_id: Optional[str] = None


@dataclass(frozen=True)
class AccountSet:
    accounts: Tuple[RemoteAccount, ...] = ()
    errors: Tuple[RemoteError, ...] = ()


class SimpleFINError(Exception):
    """Base for everything this module raises. Never carries credentials."""


class InvalidSetupToken(SimpleFINError):
    """The setup token is not base64 of an http(s) URL."""


class ClaimFailed(SimpleFINError):
    """Claiming a setup token returned 403 or another non-200 status.

    A token can be claimed exactly once, so this is also what a reused token produces.
    """


class AuthFailed(SimpleFINError):
    """The access URL's credentials were rejected (401/403) -- access was revoked."""


# How far back SimpleFIN will serve history at all (see sync.py's lookback).
MAX_WINDOW_DAYS = 90

# The longest range one /accounts request asks for. SimpleFIN Bridge answers a longer
# one in full but adds "Requested date range exceeds recommended range of 45 days. In
# the future, this may be capped." to errlist, so longer ranges are split into
# consecutive requests and merged client-side (see fetch_accounts). A routine sync
# spans 10-20 days and stays one request; only a first sync becomes two.
REQUEST_WINDOW_DAYS = 45

# SimpleFIN Bridge sits behind Cloudflare, which refuses urllib's default
# "Python-urllib/3.x" agent with a bare 403 (Cloudflare error 1010) -- indistinguishable,
# by status alone, from a setup token that was already claimed. Every request names the
# app instead.
USER_AGENT = "budget-tracker/0.1"

# fetch_accounts's default read timeout. SimpleFIN Bridge has been observed taking
# close to 60s to answer right after a bank is re-linked (it is itself waiting on the
# aggregator underneath); a 60s timeout looked indistinguishable from a hang. claim()
# stays at 30s -- it posts an empty body and gets a short access URL back, not a
# transaction history, so it has never been seen to be slow.
FETCH_ACCOUNTS_TIMEOUT = 120


def redact(url: str) -> str:
    """Strip ``user:pass@`` userinfo from ``url``, for safe use in error messages.

    ``urllib.error`` exceptions and our own messages sometimes need to name the URL that
    failed; the access URL's userinfo *is* the credential, so nothing that can reach a
    log or a notification may ever include it unredacted.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url  # not parseable as a URL; nothing to strip
    if not parts.hostname:
        return url  # no netloc -> no userinfo to strip either
    netloc = parts.hostname
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def decode_setup_token(token: str) -> str:
    """Decode a SimpleFIN setup token into its claim URL.

    The token is nothing but base64 of the claim URL. Tokens are typically copy-pasted
    from a browser, so stray whitespace (including embedded newlines) and missing
    base64 padding are both tolerated.
    """
    cleaned = "".join(token.split())
    cleaned += "=" * (-len(cleaned) % 4)
    try:
        decoded = base64.b64decode(cleaned, validate=False).decode("utf-8")
    except ValueError as exc:
        raise InvalidSetupToken("setup token is not valid base64") from exc
    parsed = urllib.parse.urlsplit(decoded)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise InvalidSetupToken("setup token does not decode to an http(s) URL")
    return decoded


def _short_reply(error: urllib.error.HTTPError) -> str:
    """``': <first line of the body>'`` for an error message, or nothing.

    Kept short and to one line: it is there to tell a firewall's refusal from the
    server's, not to relay a whole HTML error page.
    """
    try:
        text = error.read().decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001 - a body we cannot read is just left out
        return ""
    first = text.splitlines()[0][:80] if text else ""
    return f": {first}" if first else ""


def claim(setup_token: str, *, timeout: float = 30) -> str:
    """POST the decoded claim URL with an empty body; returns the access URL.

    A token can be claimed once -- an unknown or already-claimed token is a 403, raised
    as :class:`ClaimFailed`.
    """
    claim_url = decode_setup_token(setup_token)
    # Not redact(): the one-time token is the last path segment of this URL, not its
    # userinfo, and a transport failure leaves it unclaimed -- still usable by anyone who
    # reads the message. Name the host and nothing else.
    where = urllib.parse.urlsplit(claim_url).hostname
    request = urllib.request.Request(
        claim_url, data=b"", method="POST", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # Quote the server's own reply: a 403 from the firewall in front of the server
        # ("error code: 1010") and one from the server itself ("was it already
        # claimed?") look identical by status, and only one of them is about the token.
        reply = _short_reply(exc)
        raise ClaimFailed(
            f"setup token was rejected by {where} (HTTP {exc.code}{reply}). It may "
            "already have been used; if so, create a new one."
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise SimpleFINError(f"could not reach {where}: {exc}") from exc
    if status != 200:
        raise ClaimFailed(f"claim failed with HTTP {status} ({where})")
    access_url = body.strip()
    if not access_url:
        raise ClaimFailed(f"claim returned an empty access URL ({where})")
    return access_url


def _to_epoch(d: date) -> int:
    """Midnight UTC of ``d``, as a unix epoch second count."""
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _epoch_to_date(epoch: int) -> date:
    """UTC calendar date of ``epoch``."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).date()


def _basic_auth_header(parsed: urllib.parse.SplitResult) -> str:
    """HTTP Basic ``Authorization`` value from a parsed URL's userinfo.

    ``urllib`` only applies Basic auth from userinfo for ``http.client``'s own
    redirect-following helpers, not a plain ``urlopen`` call, so we build the header
    ourselves from the URL we were handed.
    """
    username = urllib.parse.unquote(parsed.username or "")
    password = urllib.parse.unquote(parsed.password or "")
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _split_window(
    start: Optional[date], end: Optional[date]
) -> List[Tuple[Optional[date], Optional[date]]]:
    """Break a start/end range into consecutive chunks no longer than REQUEST_WINDOW_DAYS.

    Either end being ``None`` means "let the server use its default", which can't be
    split against a range it doesn't know -- passed through as the single window.
    """
    if start is None or end is None:
        return [(start, end)]
    if (end - start).days <= REQUEST_WINDOW_DAYS:
        return [(start, end)]
    windows: List[Tuple[date, date]] = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=REQUEST_WINDOW_DAYS), end)
        windows.append((cursor, chunk_end))
        cursor = chunk_end
    return windows


def _connection_names(payload: dict) -> Dict[str, str]:
    names: Dict[str, str] = {}
    for conn in payload.get("connections") or []:
        conn_id = conn.get("conn_id")
        if conn_id is not None:
            names[conn_id] = conn.get("name") or ""
    return names


def _parse_transaction(raw: dict) -> RemoteTransaction:
    transacted_epoch = raw.get("transacted_at")
    return RemoteTransaction(
        id=raw["id"],
        posted=_epoch_to_date(int(raw.get("posted") or 0)),
        amount=Decimal(str(raw.get("amount", "0"))),
        description=raw.get("description") or "",
        payee=raw.get("payee"),
        memo=raw.get("memo"),
        transacted=_epoch_to_date(int(transacted_epoch)) if transacted_epoch else None,
        pending=bool(raw.get("pending", False)),
    )


def _parse_account(raw: dict, connection_names: Dict[str, str]) -> RemoteAccount:
    balance_date_raw = raw.get("balance-date")
    conn_id = raw.get("conn_id") or ""
    return RemoteAccount(
        id=raw["id"],
        name=raw.get("name") or "",
        conn_id=conn_id,
        currency=raw.get("currency") or "",
        balance=Decimal(str(raw.get("balance", "0"))),
        balance_date=_epoch_to_date(int(balance_date_raw)) if balance_date_raw else None,
        connection_name=connection_names.get(conn_id, ""),
        transactions=tuple(_parse_transaction(t) for t in (raw.get("transactions") or [])),
    )


def _parse_errors(payload: dict) -> Tuple[RemoteError, ...]:
    """``errlist`` if the key is present at all (even empty), else legacy ``errors``."""
    errlist = payload.get("errlist")
    if errlist is not None:
        return tuple(
            RemoteError(
                code=str(e.get("code", "")),
                msg=str(e.get("msg", "")),
                conn_id=e.get("conn_id"),
                account_id=e.get("account_id"),
            )
            for e in errlist
        )
    legacy = payload.get("errors") or []
    return tuple(RemoteError(code="legacy", msg=str(s)) for s in legacy)


def _parse_account_set(payload: dict) -> AccountSet:
    try:
        connection_names = _connection_names(payload)
        accounts = tuple(
            _parse_account(a, connection_names) for a in (payload.get("accounts") or [])
        )
        errors = _parse_errors(payload)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise SimpleFINError("malformed response from SimpleFIN server") from exc
    return AccountSet(accounts=accounts, errors=errors)


def _merge_account_sets(results: Sequence[AccountSet]) -> AccountSet:
    """Merge the per-window responses of a split request.

    Per account: concatenate transactions in window order, dedup by id (a later
    window's copy wins on collision, though ids are never supposed to repeat), and keep
    the most recently fetched balance -- later windows cover more recent dates.
    """
    order: List[str] = []
    latest: Dict[str, RemoteAccount] = {}
    txns_by_id: Dict[str, Dict[str, RemoteTransaction]] = {}
    errors: List[RemoteError] = []
    for account_set in results:
        # A split request repeats a connection-level error once per window; report it
        # once. RemoteError is frozen, so equal entries compare (and hash) equal.
        errors.extend(e for e in account_set.errors if e not in errors)
        for account in account_set.accounts:
            if account.id not in latest:
                order.append(account.id)
                txns_by_id[account.id] = {}
            latest[account.id] = account
            for txn in account.transactions:
                txns_by_id[account.id][txn.id] = txn
    merged = tuple(
        replace(latest[account_id], transactions=tuple(txns_by_id[account_id].values()))
        for account_id in order
    )
    return AccountSet(accounts=merged, errors=tuple(errors))


def _fetch_accounts_once(
    access_url: str,
    *,
    start: Optional[date],
    end: Optional[date],
    account_ids: Sequence[str],
    balances_only: bool,
    timeout: float,
) -> AccountSet:
    parsed = urllib.parse.urlsplit(access_url)
    auth_header = _basic_auth_header(parsed)
    base = redact(access_url).rstrip("/")  # urllib won't send userinfo auth on its own
    query: List[Tuple[str, str]] = [("version", "2")]
    if start is not None:
        query.append(("start-date", str(_to_epoch(start))))
    if end is not None:
        query.append(("end-date", str(_to_epoch(end))))
    for account_id in account_ids:
        query.append(("account", account_id))
    if balances_only:
        query.append(("balances-only", "1"))
    url = f"{base}/accounts?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(
        url, headers={"Authorization": auth_header, "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AuthFailed(
                f"access to {redact(access_url)} was refused (HTTP {exc.code}); "
                "reconnect this account"
            ) from exc
        raise SimpleFINError(
            f"request to {redact(access_url)} failed with HTTP {exc.code}"
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise SimpleFINError(f"could not reach {redact(access_url)}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise SimpleFINError(f"malformed JSON from {redact(access_url)}") from exc
    return _parse_account_set(payload)


def fetch_accounts(
    access_url: str,
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
    account_ids: Sequence[str] = (),
    balances_only: bool = False,
    timeout: float = FETCH_ACCOUNTS_TIMEOUT,
) -> AccountSet:
    """GET ``<access url>/accounts``, auth via Basic from the URL's own userinfo.

    ``start``/``end`` are protocol semantics: start inclusive, end exclusive. A range
    over :data:`REQUEST_WINDOW_DAYS` is split into consecutive requests and merged (see
    :func:`_merge_account_sets`) so callers never have to think about the server's
    90-day limit. Always sends ``version=2``; never ``pending=1`` -- pending rows are
    not wanted here.

    Errors in the response body (``errlist``/legacy ``errors``) come back on the
    returned :class:`AccountSet` rather than being raised: one bad connection must not
    hide the good ones. Only transport failures, auth failures and malformed JSON
    raise.
    """
    results = [
        _fetch_accounts_once(
            access_url,
            start=window_start,
            end=window_end,
            account_ids=account_ids,
            balances_only=balances_only,
            timeout=timeout,
        )
        for window_start, window_end in _split_window(start, end)
    ]
    return _merge_account_sets(results)
