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
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Callable, Dict, Iterable, Optional, Sequence, Tuple
from urllib.error import URLError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Currency, ExchangeRate

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

def _most_recent_on_or_before(
    session: Session, day: date, base: str, quote: str, source: str
) -> Optional[ExchangeRate]:
    return session.scalar(
        select(ExchangeRate)
        .where(
            ExchangeRate.source == source,
            ExchangeRate.base == base,
            ExchangeRate.quote == quote,
            ExchangeRate.day <= day,
        )
        .order_by(ExchangeRate.day.desc())
        .limit(1)
    )


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
    if direct is not None and (inverse is None or direct.day >= inverse.day):
        return direct.day, Decimal(direct.rate)
    # Division precision: 28 significant digits (Python decimal's default context) is
    # far more than any currency pair needs — rates run to at most six or seven
    # significant digits — but it keeps this inversion from being the place precision
    # is lost, leaving that entirely to convert()'s final, deliberate rounding step.
    with localcontext() as ctx:
        ctx.prec = 28
        return inverse.day, Decimal(1) / Decimal(inverse.rate)


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


def _decimal_places(session: Session, code: str) -> int:
    places = session.scalar(select(Currency.decimal_places).where(Currency.value == code))
    if places is None:
        raise ValueError(f"Unknown currency {code!r}: no currency row to read decimal_places from.")
    return places


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
_DEFAULT_TIMEOUT = 10  # seconds; just bounds a hang, the API itself is small and fast


def _default_fetch(url: str) -> Dict[str, object]:
    """Real network call to Frankfurter, via stdlib ``urllib`` so this adds no
    dependency.

    Frankfurter sits behind Cloudflare, which returns HTTP 403 to the default
    ``Python-urllib`` User-Agent as a bot-blocking heuristic. Without an explicit
    header this looks exactly like a dead API rather than a blocked request, which is
    why one is set here.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "budget-tracker/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=_DEFAULT_TIMEOUT) as response:
            payload = response.read()
    except (URLError, OSError) as exc:
        raise FrankfurterError(f"Could not reach Frankfurter at {url}: {exc}") from exc
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
