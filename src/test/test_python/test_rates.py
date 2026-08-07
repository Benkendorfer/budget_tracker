"""Tests for exchange-rate recording, lookup, and the Frankfurter fetcher."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from budget_tracker import rates
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.models import Currency


def _setup(tmp_path):
    engine = get_engine(tmp_path / "t.db")
    init_db(engine)
    return get_sessionmaker(engine)


def _add_currency(session, code, decimal_places=2):
    currency = Currency(value=code, decimal_places=decimal_places)
    session.add(currency)
    session.flush()
    return currency


# --------------------------------------------------------------------------- rate_on

def test_rate_on_matches_most_recent_on_or_before_across_a_weekend(tmp_path):
    """ECB has no rows for weekends/holidays -- an exact-date lookup would fail for
    every one of those, so the match has to fall back to the last working day."""
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        # Friday 2025-12-05; Saturday and Sunday are simply absent, as they are from
        # the real feed.
        rates.record_rate(session, date(2025, 12, 5), "USD", "CHF", Decimal("0.8"), rates.ECB)
        session.commit()

    with session_factory() as session:
        assert rates.rate_on(session, date(2025, 12, 5), "USD", "CHF") == Decimal("0.8")
        assert rates.rate_on(session, date(2025, 12, 6), "USD", "CHF") == Decimal("0.8")
        assert rates.rate_on(session, date(2025, 12, 7), "USD", "CHF") == Decimal("0.8")
        # Nothing on or before the day before the first recorded rate.
        assert rates.rate_on(session, date(2025, 12, 4), "USD", "CHF") is None


def test_rate_on_source_breaks_a_tie_on_the_same_day(tmp_path):
    """All three sources dated the same day: manual, then observed, then reference."""
    session_factory = _setup(tmp_path)
    day = date(2025, 12, 1)
    with session_factory() as session:
        rates.record_rate(session, day, "USD", "CHF", Decimal("0.70"), rates.ECB)
        session.commit()
    with session_factory() as session:
        assert rates.rate_on(session, day, "USD", "CHF") == Decimal("0.70")
        rates.record_rate(session, day, "USD", "CHF", Decimal("0.75"), rates.OBSERVED)
        session.commit()
    with session_factory() as session:
        # A rate actually paid beats a reference midpoint for the same day.
        assert rates.rate_on(session, day, "USD", "CHF") == Decimal("0.75")
        rates.record_rate(session, day, "USD", "CHF", Decimal("0.90"), rates.MANUAL)
        session.commit()
    with session_factory() as session:
        assert rates.rate_on(session, day, "USD", "CHF") == Decimal("0.90")


def test_a_stale_manual_rate_does_not_shadow_later_daily_rates(tmp_path):
    """Recency outranks source across different days.

    The tempting reading of "automation never overrides a hand-set value" would have one
    rate typed in January standing in for every day after it, however many real daily
    rates were fetched since. That error grows silently with time: a year later every
    converted figure is still using the January rate and nothing on screen says so. A
    hand-set rate is authoritative for the day it was set, which is what setting it meant.
    """
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        rates.record_rate(
            session, date(2025, 1, 1), "USD", "CHF", Decimal("0.90"), rates.MANUAL
        )
        rates.record_rate(
            session, date(2025, 12, 1), "USD", "CHF", Decimal("0.70"), rates.ECB
        )
        session.commit()

    with session_factory() as session:
        # On and after the reference date, the far more recent daily rate is used...
        assert rates.rate_on(session, date(2025, 12, 1), "USD", "CHF") == Decimal("0.70")
        assert rates.rate_on(session, date(2026, 6, 1), "USD", "CHF") == Decimal("0.70")
        # ...but the manual rate still governs the span it was actually set for.
        assert rates.rate_on(session, date(2025, 6, 1), "USD", "CHF") == Decimal("0.90")


def test_rate_on_derives_the_inverse_pair(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        rates.record_rate(session, date(2025, 12, 1), "USD", "CHF", Decimal("0.8"), rates.ECB)
        session.commit()

    with session_factory() as session:
        derived = rates.rate_on(session, date(2025, 12, 1), "CHF", "USD")
        assert derived == Decimal(1) / Decimal("0.8")


def test_rate_on_prefers_the_more_recent_of_direct_or_inverse(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        # A stale direct CHF->USD row, and a fresher USD->CHF row to invert instead.
        rates.record_rate(session, date(2025, 11, 1), "CHF", "USD", Decimal("1.1"), rates.ECB)
        rates.record_rate(session, date(2025, 12, 1), "USD", "CHF", Decimal("0.8"), rates.ECB)
        session.commit()

    with session_factory() as session:
        assert rates.rate_on(session, date(2025, 12, 1), "CHF", "USD") == Decimal(1) / Decimal(
            "0.8"
        )


def test_rate_on_returns_none_when_nothing_is_known(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        assert rates.rate_on(session, date(2025, 12, 1), "USD", "CHF") is None


# ---------------------------------------------------------------------------- convert

def test_convert_same_currency_is_an_exact_identity(tmp_path):
    """No rate lookup and no currency row required -- there is nothing to convert."""
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        assert rates.convert(session, 12345, "USD", "USD", date(2025, 12, 1)) == 12345
        assert rates.convert(session, 0, "USD", "USD", date(2025, 12, 1)) == 0
        assert rates.convert(session, -500, "USD", "USD", date(2025, 12, 1)) == -500


def test_convert_returns_none_for_an_unknown_rate(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        _add_currency(session, "USD")
        _add_currency(session, "CHF")
        session.commit()

    with session_factory() as session:
        assert rates.convert(session, 1000, "USD", "CHF", date(2025, 12, 1)) is None


def test_convert_rounds_half_even_to_the_target_currencys_own_decimal_places(tmp_path):
    """JPY-style: 0 decimal places, not the assumed 2."""
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        _add_currency(session, "USD", decimal_places=2)
        _add_currency(session, "JPY", decimal_places=0)
        rates.record_rate(session, date(2025, 12, 1), "USD", "JPY", Decimal("150.5"), rates.ECB)
        rates.record_rate(session, date(2025, 12, 3), "USD", "JPY", Decimal("149.5"), rates.ECB)
        session.commit()

    with session_factory() as session:
        # 200 minor units = 2.00 USD -> 301.0 JPY exactly -> 301, no rounding needed.
        assert rates.convert(session, 200, "USD", "JPY", date(2025, 12, 1)) == 301
        # A genuine half-even tie: 100 minor units = 1.00 USD -> 150.5 JPY, exactly
        # halfway between 150 and 151 -- half-even rounds down to 150 (the even one).
        assert rates.convert(session, 100, "USD", "JPY", date(2025, 12, 1)) == 150
        # Same tie shape the other way: 149.5 is halfway between 149 and 150 -- rounds
        # up to 150, still the even one, proving this isn't just "round down on ties".
        assert rates.convert(session, 100, "USD", "JPY", date(2025, 12, 3)) == 150


def test_convert_unknown_currency_raises_rather_than_assuming_two_places(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        _add_currency(session, "USD")
        rates.record_rate(session, date(2025, 12, 1), "USD", "XYZ", Decimal("2"), rates.ECB)
        session.commit()

    with session_factory() as session:
        with pytest.raises(ValueError, match="XYZ"):
            rates.convert(session, 100, "USD", "XYZ", date(2025, 12, 1))


# ------------------------------------------------------------------------- recording

def test_record_rate_is_idempotent(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        rates.record_rate(session, date(2025, 12, 1), "USD", "CHF", Decimal("0.80"), rates.ECB)
        rates.record_rate(session, date(2025, 12, 1), "USD", "CHF", Decimal("0.81"), rates.ECB)
        session.commit()

    with session_factory() as session:
        from sqlalchemy import func, select

        from budget_tracker.models import ExchangeRate

        count = session.scalar(select(func.count(ExchangeRate.id)))
        assert count == 1
        assert rates.rate_on(session, date(2025, 12, 1), "USD", "CHF") == Decimal("0.81")


def test_record_observed_takes_plain_tuples(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        written = rates.record_observed(
            session,
            [
                (date(2025, 12, 1), "USD", "CHF", Decimal("0.79")),
                (date(2025, 12, 2), "USD", "EUR", Decimal("0.86")),
            ],
        )
        session.commit()
    assert written == 2

    with session_factory() as session:
        assert rates.rate_on(session, date(2025, 12, 1), "USD", "CHF") == Decimal("0.79")
        assert rates.rate_on(session, date(2025, 12, 2), "USD", "EUR") == Decimal("0.86")


# ------------------------------------------------------------------------- fetching

def _stub_response(day_rates):
    """Build a Frankfurter-shaped time-series payload from {"YYYY-MM-DD": {...}}."""
    return {"amount": 1.0, "base": "USD", "start_date": None, "end_date": None, "rates": day_rates}


def test_fetch_ecb_rates_populates_the_cache_from_a_stub(tmp_path):
    session_factory = _setup(tmp_path)
    calls = []

    def stub_fetch(url):
        calls.append(url)
        return _stub_response(
            {
                "2025-12-01": {"CHF": 0.80472, "EUR": 0.86103},
                "2025-12-02": {"CHF": 0.805, "EUR": 0.861},
                # weekend/holiday gap: 2025-12-06/07 simply absent, as the real feed is.
            }
        )

    with session_factory() as session:
        written = rates.fetch_ecb_rates(
            session,
            date(2025, 12, 1),
            date(2025, 12, 2),
            "USD",
            ["CHF", "EUR"],
            today=date(2025, 12, 10),
            fetch=stub_fetch,
        )
        session.commit()

    assert written == 4
    assert len(calls) == 1
    assert "2025-12-01..2025-12-02" in calls[0]

    with session_factory() as session:
        assert rates.rate_on(session, date(2025, 12, 1), "USD", "CHF") == Decimal("0.80472")
        assert rates.rate_on(session, date(2025, 12, 2), "USD", "EUR") == Decimal("0.861")
        # The weekend gap falls back to Tuesday's rate, not an exact-date miss.
        assert rates.rate_on(session, date(2025, 12, 6), "USD", "CHF") == Decimal("0.805")


def test_fetch_ecb_rates_skips_already_cached_historical_days(tmp_path):
    session_factory = _setup(tmp_path)
    calls = []

    def stub_fetch(url):
        calls.append(url)
        return _stub_response({"2025-12-01": {"CHF": 0.8}, "2025-12-02": {"CHF": 0.81}})

    with session_factory() as session:
        rates.fetch_ecb_rates(
            session,
            date(2025, 12, 1),
            date(2025, 12, 2),
            "USD",
            ["CHF"],
            today=date(2025, 12, 10),
            fetch=stub_fetch,
        )
        session.commit()
    assert len(calls) == 1

    # Asking for the same, fully-historical range again must not touch the network.
    with session_factory() as session:
        written = rates.fetch_ecb_rates(
            session,
            date(2025, 12, 1),
            date(2025, 12, 2),
            "USD",
            ["CHF"],
            today=date(2025, 12, 10),
            fetch=stub_fetch,
        )
        session.commit()
    assert written == 0
    assert len(calls) == 1  # unchanged: no second call


def test_fetch_ecb_rates_still_fetches_a_range_earlier_than_the_cache(tmp_path):
    """Narrowing must not skip a request that reaches further back than the cache.

    Testing only how far the cache *extends* would drop this request entirely: having
    cached December, asking for the previous January would compute a start after the
    cached end, find it past the requested end, and return 0 — leaving that year
    permanently unconvertible with nothing to indicate why.
    """
    session_factory = _setup(tmp_path)
    calls = []

    def stub_fetch(url):
        calls.append(url)
        if "2024" in url:
            return _stub_response({"2024-01-15": {"CHF": 0.9}})
        return _stub_response({"2025-12-01": {"CHF": 0.8}})

    with session_factory() as session:
        rates.fetch_ecb_rates(
            session, date(2025, 12, 1), date(2025, 12, 1), "USD", ["CHF"],
            today=date(2026, 6, 1), fetch=stub_fetch,
        )
        session.commit()
    assert len(calls) == 1

    with session_factory() as session:
        written = rates.fetch_ecb_rates(
            session, date(2024, 1, 1), date(2024, 1, 31), "USD", ["CHF"],
            today=date(2026, 6, 1), fetch=stub_fetch,
        )
        session.commit()

    assert written == 1, "the earlier range was skipped instead of fetched"
    assert len(calls) == 2 and "2024-01-01..2024-01-31" in calls[1]
    with session_factory() as session:
        assert rates.rate_on(session, date(2024, 1, 20), "USD", "CHF") == Decimal("0.9")


def test_fetch_ecb_rates_always_refetches_today(tmp_path):
    """Today's rate may be provisional (ECB publishes ~16:00 CET), so it is
    re-requested even though a row for it already exists from an earlier call."""
    session_factory = _setup(tmp_path)
    today = date(2025, 12, 10)
    calls = []

    def stub_fetch(url):
        calls.append(url)
        # Second call simulates the provisional rate having firmed up.
        rate = 0.80 if len(calls) == 1 else 0.81
        return _stub_response({today.isoformat(): {"CHF": rate}})

    with session_factory() as session:
        rates.fetch_ecb_rates(
            session, today, today, "USD", ["CHF"], today=today, fetch=stub_fetch
        )
        session.commit()
    with session_factory() as session:
        assert rates.rate_on(session, today, "USD", "CHF") == Decimal("0.8")

    with session_factory() as session:
        rates.fetch_ecb_rates(
            session, today, today, "USD", ["CHF"], today=today, fetch=stub_fetch
        )
        session.commit()

    assert len(calls) == 2
    with session_factory() as session:
        assert rates.rate_on(session, today, "USD", "CHF") == Decimal("0.81")


def test_fetch_ecb_rates_handles_a_flat_single_day_response(tmp_path):
    """A range collapsing to one trading day can come back flat instead of nested."""
    session_factory = _setup(tmp_path)

    def stub_fetch(url):
        return {"amount": 1.0, "base": "USD", "date": "2025-12-02", "rates": {"CHF": 0.80472}}

    with session_factory() as session:
        written = rates.fetch_ecb_rates(
            session,
            date(2025, 12, 2),
            date(2025, 12, 2),
            "USD",
            ["CHF"],
            today=date(2025, 12, 10),
            fetch=stub_fetch,
        )
        session.commit()

    assert written == 1
    with session_factory() as session:
        assert rates.rate_on(session, date(2025, 12, 2), "USD", "CHF") == Decimal("0.80472")


def test_fetch_ecb_rates_rejects_a_backwards_range(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        with pytest.raises(ValueError):
            rates.fetch_ecb_rates(
                session,
                date(2025, 12, 5),
                date(2025, 12, 1),
                "USD",
                ["CHF"],
                fetch=lambda url: {},
            )


def test_default_fetch_surfaces_network_failure_as_a_catchable_error(monkeypatch):
    """Exercises the real fetch implementation's error handling without ever making a
    network call: urlopen itself is stubbed to fail immediately."""
    import urllib.request

    from urllib.error import URLError

    def broken_urlopen(request, timeout=None):
        raise URLError("simulated DNS failure")

    monkeypatch.setattr(urllib.request, "urlopen", broken_urlopen)

    with pytest.raises(rates.FrankfurterError):
        rates._default_fetch("https://api.frankfurter.dev/v1/2025-12-01..2025-12-02")


def test_fetch_failure_propagates_through_fetch_ecb_rates(tmp_path):
    session_factory = _setup(tmp_path)

    def failing_fetch(url):
        raise rates.FrankfurterError("simulated failure")

    with session_factory() as session:
        with pytest.raises(rates.FrankfurterError):
            rates.fetch_ecb_rates(
                session,
                date(2025, 12, 1),
                date(2025, 12, 2),
                "USD",
                ["CHF"],
                today=date(2025, 12, 10),
                fetch=failing_fetch,
            )
