"""Currency conversion: recording rates, looking them up, and fetching ECB references.

Nothing here is wired into any aggregate yet (see :func:`convert`'s docstring) — this
is only the storage and lookup layer, following the same provenance discipline as
``category_source`` in :mod:`.categories`:

``manual``
    A rate the user typed in by hand. Wins over the other two *for the same day*; it
    does not outrank a more recent rate from another source (see :func:`rate_on`).
``observed``
    A rate actually paid, harvested from a real conversion (e.g. a Wise transfer log —
    see ``wise.ObservedRate``, which this module deliberately does not import so it
    stays independent of a reader it does not own). Better evidence than a reference
    rate for that specific day, but still second to a manual correction.
``ecb``
    A daily reference midpoint fetched from Frankfurter (see :func:`fetch_ecb_rates`).
    The fallback when nothing more specific is known.

Nothing here commits; callers own the transaction, as in :mod:`.vendors`.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from bisect import bisect_right
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import URLError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Currency, ExchangeRate, Transaction

MANUAL = "manual"
OBSERVED = "observed"
ECB = "ecb"

# Ranked in this order by rate_on, but only to break a tie between rates dated the
# same day — recency comes first. See rate_on for why source must not outrank date.
_PRECEDENCE = (MANUAL, OBSERVED, ECB)


# ------------------------------------------------------------------------- recording

def record_rate(
    session: Session, day: date, base: str, quote: str, rate: Decimal, source: str
) -> ExchangeRate:
    """Upsert idempotently on the unique key (day, base, quote, source). No commit."""
    row = session.scalar(
        select(ExchangeRate).where(
            ExchangeRate.day == day,
            ExchangeRate.base == base,
            ExchangeRate.quote == quote,
            ExchangeRate.source == source,
        )
    )
    rate_text = str(Decimal(rate))
    if row is None:
        row = ExchangeRate(day=day, base=base, quote=quote, rate=rate_text, source=source)
        session.add(row)
    else:
        row.rate = rate_text
    session.flush()
    _forget_rate_book(session)
    return row


def record_observed(
    session: Session, entries: Iterable[Tuple[date, str, str, Decimal]]
) -> int:
    """Record a batch of witnessed rates as ``OBSERVED``. Returns the count written.

    Takes plain ``(day, base, quote, rate)`` tuples rather than ``wise.ObservedRate``
    directly, so this module stays usable without importing the Wise reader; the
    caller unpacks whatever dataclass it has.
    """
    count = 0
    for day, base, quote, rate in entries:
        record_rate(session, day, base, quote, rate, OBSERVED)
        count += 1
    return count


# --------------------------------------------------------------------------- lookup

# Where the whole rate table is parked for the life of one session. Sessions here are
# short (``with self.session_factory() as session:``), so this is scoped exactly right:
# it cannot go stale across a commit, and it dies with the session.
_BOOK_KEY = "_rate_book"


def _load_rate_book(session: Session) -> Dict[Tuple[str, str, str], Tuple[List[date], List[str]]]:
    """Every rate, indexed by ``(source, base, quote)`` and sorted by day.

    One query for the lot. Looking each rate up individually costs six queries -- three
    sources, direct and inverse -- and an aggregate asks once per (currency, day) group,
    so a couple of years of two currencies came to **6,356 queries for a single
    get_totals** and eighteen thousand for one keypress. The whole table is a few
    hundred rows and is read far more often than it is written, so holding it is both
    cheaper and simpler than making the per-lookup query smarter.
    """
    book: Dict[Tuple[str, str, str], Tuple[List[date], List[str]]] = {}
    for row in session.scalars(select(ExchangeRate).order_by(ExchangeRate.day)):
        days, values = book.setdefault((row.source, row.base, row.quote), ([], []))
        days.append(row.day)
        values.append(row.rate)
    return book


def _rate_book(session: Session):
    book = session.info.get(_BOOK_KEY)
    if book is None:
        book = _load_rate_book(session)
        session.info[_BOOK_KEY] = book
    return book


def _forget_rate_book(session: Session) -> None:
    """Drop the cached table so a rate written in this session is seen by the next read."""
    session.info.pop(_BOOK_KEY, None)


def _most_recent_on_or_before(
    session: Session, day: date, base: str, quote: str, source: str
) -> Optional[Tuple[date, str]]:
    """``(day, rate)`` of the newest rate on or before ``day``, or None.

    Resolved against the in-memory book by binary search rather than by a query per
    lookup. The ordering and the on-or-before rule are exactly what the SQL did.
    """
    entry = _rate_book(session).get((source, base, quote))
    if entry is None:
        return None
    days, values = entry
    index = bisect_right(days, day)
    if index == 0:
        return None
    return days[index - 1], values[index - 1]


def _best_for_source(
    session: Session, day: date, base: str, quote: str, source: str
) -> Optional[Tuple[date, Decimal]]:
    """``(day, rate)`` for one source: most recent on or before ``day``, direct or
    derived from the inverse pair, whichever is more recent.

    The day comes back with the rate because :func:`rate_on` ranks across sources by how
    close each one lands to the day asked for, and cannot do that from the rate alone.
    """
    direct = _most_recent_on_or_before(session, day, base, quote, source)
    inverse = _most_recent_on_or_before(session, day, quote, base, source)
    if direct is None and inverse is None:
        return None
    # A tie on date prefers the direct row: no division, so no precision to lose.
    if direct is not None and (inverse is None or direct[0] >= inverse[0]):
        return direct[0], Decimal(direct[1])
    # Division precision: 28 significant digits (Python decimal's default context) is
    # far more than any currency pair needs — rates run to at most six or seven
    # significant digits — but it keeps this inversion from being the place precision
    # is lost, leaving that entirely to convert()'s final, deliberate rounding step.
    with localcontext() as ctx:
        ctx.prec = 28
        return inverse[0], Decimal(1) / Decimal(inverse[1])


def rate_on(session: Session, day: date, base: str, quote: str) -> Optional[Decimal]:
    """The exchange rate on ``day`` (1 ``base`` buys this many ``quote``), or ``None``.

    **The closest rate on or before ``day`` wins; source only breaks a tie on the same
    day**, in :data:`_PRECEDENCE` order (manual, then observed, then reference).

    Source does *not* outrank recency, which is the tempting reading of "a hand-set
    value is never overridden by automation" but the wrong one here. That rule protects
    one row from being rewritten; it does not mean a single manual rate should stand in
    for every later day. Letting it would mean one rate typed for January 2025 silently
    converting every transaction of 2026 too, no matter how many real daily rates had
    been fetched since — an error that grows quietly with time and shows up nowhere.
    Ranking by date keeps a hand-set rate authoritative for the day it was set, which is
    what setting it actually meant.

    Matching is on-or-before, not exact: ECB only publishes on working days, so an
    exact-date lookup would fail for every weekend and holiday.

    Never guesses. A missing rate is ``None``, not 1.0 and not an average of anything.
    """
    best_key = None
    best_rate = None
    for index, source in enumerate(_PRECEDENCE):
        found = _best_for_source(session, day, base, quote, source)
        if found is None:
            continue
        found_day, rate = found
        # Later day wins; on the same day the earlier entry in _PRECEDENCE wins, so its
        # index is negated to sort the same direction as the date.
        key = (found_day, -index)
        if best_key is None or key > best_key:
            best_key, best_rate = key, rate
    return best_rate


def currency_known(session: Session, code: str) -> bool:
    """Whether ``code`` has a :class:`Currency` row at all.

    For an aggregate (see ``queries.get_totals``) to check *before* calling
    :func:`convert`, so it can treat "this database has never had a row for the
    currency it converts into" as just another way of not being able to convert --
    left out and counted, never raised -- the same answer as a missing rate.

    This is deliberately not folded into :func:`convert` itself: a caller handing
    :func:`convert` a currency code that does not exist is a programming error (a
    typo, a code that was never real), and that must stay loud. The distinction is
    which side is asking -- an aggregate checking its *target* currency ahead of time
    degrades gracefully; :func:`convert` handed a bad code directly still raises.
    """
    return session.scalar(select(Currency.id).where(Currency.value == code)) is not None


_PLACES_KEY = "_decimal_places"


def _decimal_places(session: Session, code: str) -> int:
    """How many minor units a currency has, cached for the life of the session.

    A handful of rows, asked once per conversion -- nearly five thousand times for a
    single keypress before this. Currencies are effectively immutable once created, so
    caching them for a short-lived session costs nothing in freshness.
    """
    cache = session.info.setdefault(_PLACES_KEY, {})
    if code not in cache:
        places = session.scalar(
            select(Currency.decimal_places).where(Currency.value == code)
        )
        if places is None:
            raise ValueError(
                f"Unknown currency {code!r}: no currency row to read decimal_places from."
            )
        cache[code] = places
    return cache[code]


def convert(
    session: Session,
    amount_minor: int,
    from_currency: str,
    to_currency: str,
    day: date,
) -> Optional[int]:
    """Convert integer minor units of ``from_currency`` to integer minor units of
    ``to_currency``, at the rate :func:`rate_on` finds for ``day``.

    Same currency in and out is an exact identity — returned unchanged, without ever
    looking up a rate or a currency row, so it can't pick up a rounding error from a
    trip through Decimal it never needed to take.

    Rounds half-even to ``to_currency``'s own ``decimal_places`` (not assumed to be 2).
    Returns ``None`` when the rate is unknown, for the same reason as :func:`rate_on`:
    a wrong figure is worse than a visibly missing one.

    Not wired into any aggregate yet — ``get_totals`` and friends still deal in a
    single currency. That wiring is a later step.
    """
    if from_currency == to_currency:
        return amount_minor

    rate = rate_on(session, day, from_currency, to_currency)
    if rate is None:
        return None

    from_places = _decimal_places(session, from_currency)
    to_places = _decimal_places(session, to_currency)

    major = Decimal(amount_minor).scaleb(-from_places)
    converted_major = major * rate
    quantum = Decimal(1).scaleb(-to_places)
    rounded = converted_major.quantize(quantum, rounding=ROUND_HALF_EVEN)
    return int(rounded.scaleb(to_places))


# -------------------------------------------------------------------------- fetching

class FrankfurterError(RuntimeError):
    """Frankfurter could not be reached, or its response could not be read."""


BASE_URL = "https://api.frankfurter.dev/v1"

# Generous on purpose. Measured against the live API, response time is erratic and has
# little to do with how much is being asked for: six months of two currencies came back
# in 0.7s while one month of the same two took 22s. A tight bound turns that ordinary
# variance into a failed fetch, and the payload is small enough that waiting is cheap.
_DEFAULT_TIMEOUT = 40  # seconds

# One retry, because the slow responses above are transient rather than a broken request:
# the same URL that times out usually answers immediately on a second attempt. More than
# one adds minutes of waiting to a genuine outage without improving the odds.
_ATTEMPTS = 2


def _default_fetch(url: str) -> Dict[str, object]:
    """Real network call to Frankfurter, via stdlib ``urllib`` so this adds no
    dependency.

    Frankfurter sits behind Cloudflare, which returns HTTP 403 to the default
    ``Python-urllib`` User-Agent as a bot-blocking heuristic. Without an explicit
    header this looks exactly like a dead API rather than a blocked request, which is
    why one is set here.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "budget-tracker/1.0"})
    last: Optional[Exception] = None
    for attempt in range(_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=_DEFAULT_TIMEOUT) as response:
                payload = response.read()
            break
        except (URLError, OSError) as exc:
            last = exc
    else:
        raise FrankfurterError(
            f"Could not reach Frankfurter at {url} after {_ATTEMPTS} attempts: {last}"
        ) from last
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise FrankfurterError(f"Frankfurter returned unparsable JSON: {exc}") from exc


def fetch_ecb_rates(
    session: Session,
    start: date,
    end: date,
    base: str,
    quotes: Sequence[str],
    *,
    today: Optional[date] = None,
    fetch: Callable[[str], Dict[str, object]] = _default_fetch,
) -> int:
    """Cache ECB reference rates for ``base`` against each of ``quotes`` over
    ``[start, end]``. Returns the number of (day, quote) rows written.

    One time-series call per base currency for the whole range, not one call per day
    — the caller typically passes the min and max date of the transactions being
    converted.

    Historical days already cached are never re-fetched (ECB's published rates don't
    change), so the request actually sent only covers the gap after whatever is
    already on file. ``today`` is the deliberate exception: ECB publishes around
    16:00 CET, so a rate recorded for today earlier in the run may still be
    provisional, and today is therefore always re-requested even if a row for it
    already exists.

    ``fetch`` takes a URL and returns the parsed JSON body; it defaults to the real
    HTTP call so tests can substitute a stub and never touch the network. Raises
    :class:`FrankfurterError` on any network or parse failure rather than returning
    zero rates for a range that genuinely failed to fetch.
    """
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    if today is None:
        today = date.today()
    quotes = list(quotes)
    if not quotes:
        return 0

    spans = [
        session.execute(
            select(func.min(ExchangeRate.day), func.max(ExchangeRate.day)).where(
                ExchangeRate.base == base,
                ExchangeRate.quote == quote,
                ExchangeRate.source == ECB,
                ExchangeRate.day < today,  # today never counts as "already cached"
            )
        ).one()
        for quote in quotes
    ]
    fetch_start = start
    if all(first is not None for first, _last in spans):
        cached_from = max(first for first, _last in spans)
        cached_through = min(last for _first, last in spans)
        # Only skip ahead when the cache actually *covers* the start of the request.
        # Testing the end alone would silently drop a request that reaches further back
        # than anything on file — asking for 2024 after caching 2025-2026 would fetch
        # nothing and leave 2024 permanently unconvertible. Re-fetching an overlap is
        # harmless (record_rate upserts, and published rates never change), so erring
        # towards the wider range costs one request and cannot lose a year.
        if cached_from <= start <= cached_through:
            fetch_start = cached_through + timedelta(days=1)
    if fetch_start > end:
        return 0  # everything requested is already on file

    date_range = f"{fetch_start.isoformat()}..{end.isoformat()}"
    url = f"{BASE_URL}/{date_range}?base={base}&symbols={','.join(quotes)}"
    payload = fetch(url)

    rates_field = payload.get("rates", {}) or {}
    if rates_field and all(isinstance(v, dict) for v in rates_field.values()):
        day_map = rates_field  # multi-day time-series shape: {"2025-12-01": {...}, ...}
    else:
        # Defensive only. A range URL always answers in the nested shape — verified
        # against the live API, including a range covering a single trading day and one
        # covering a weekend (which answers with the preceding trading day rather than
        # nothing). This branch would only be reached by a single-date URL, which this
        # function never builds.
        day_map = {payload.get("date", fetch_start.isoformat()): rates_field}

    written = 0
    for day_str, day_rates in day_map.items():
        day = date.fromisoformat(day_str)
        for quote, value in day_rates.items():
            if quote not in quotes:
                continue
            # str(value) first: Decimal(a_float) would capture the binary
            # approximation underneath the float, not the decimal digits Frankfurter
            # actually sent in the JSON text.
            record_rate(session, day, base, quote, Decimal(str(value)), ECB)
            written += 1
    return written


# --------------------------------------------------------- fetching after an import

def import_currency_span(
    session: Session, import_id: int, home_currency: str
) -> Optional[Tuple[date, date, List[str]]]:
    """``(start, end, quotes)`` an import needs rates for, or ``None`` if it needs none.

    Reads back the rows :func:`.importer.import_csv` (or ``import_wise_csv``) just
    wrote for ``import_id``: every currency among them other than ``home_currency``,
    and the min/max ``posted_date`` across *those* rows only -- not the whole import,
    since a mixed-currency file's home-currency rows need no rate and should not widen
    the span asked for. ``None`` when every row is already in ``home_currency``, so a
    caller need not special-case "nothing foreign" itself.
    """
    rows = session.execute(
        select(
            Currency.value,
            func.min(Transaction.posted_date),
            func.max(Transaction.posted_date),
        )
        .select_from(Transaction)
        .join(Currency, Currency.id == Transaction.currency_id)
        .where(Transaction.import_id == import_id, Currency.value != home_currency)
        .group_by(Currency.value)
    ).all()
    if not rows:
        return None
    quotes = sorted(row[0] for row in rows)
    start = min(row[1] for row in rows)
    end = max(row[2] for row in rows)
    return start, end, quotes


@dataclass(frozen=True)
class ImportRatesOutcome:
    """What came of trying to fetch rates for an import. Never raised -- see
    :func:`fetch_rates_for_import`.
    """

    attempted: bool  # False when the import held no currency but home_currency
    quotes: Tuple[str, ...] = field(default_factory=tuple)
    written: int = 0
    error: Optional[str] = None  # set on a FrankfurterError; `written` is 0 alongside it


def fetch_rates_for_import(
    session: Session,
    import_id: int,
    home_currency: str,
    *,
    today: Optional[date] = None,
    fetch: Callable[[str], Dict[str, object]] = _default_fetch,
) -> ImportRatesOutcome:
    """Cache whatever ECB rates ``import_id`` needs against ``home_currency``, if any.

    This is the whole "which currencies and what span does this import need, and
    fetch just that" decision in one place, so ``importer.import_csv``'s callers --
    the CLI directly, the TUI from a worker -- only ever have to call this one
    function and render what it reports, never reimplement the question themselves.

    Only asks for what :func:`import_currency_span` says is missing, and
    :func:`fetch_ecb_rates` narrows that further to whatever span is not already
    cached, so a file re-imported into an already-stocked database triggers no
    request at all.

    Never raises: a :class:`FrankfurterError` (offline, or the service unreachable)
    comes back as ``.error`` instead, so a caller can report it without having to wrap
    every call site in its own try/except -- an import must succeed even when the rate
    service does not. Does not commit; the caller owns the transaction, same as every
    other function here.
    """
    span = import_currency_span(session, import_id, home_currency)
    if span is None:
        return ImportRatesOutcome(attempted=False)
    start, end, quotes = span
    try:
        written = fetch_ecb_rates(
            session, start, end, home_currency, quotes, today=today, fetch=fetch
        )
    except FrankfurterError as error:
        return ImportRatesOutcome(attempted=True, quotes=tuple(quotes), error=str(error))
    return ImportRatesOutcome(attempted=True, quotes=tuple(quotes), written=written)
