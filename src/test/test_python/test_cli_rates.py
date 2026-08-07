"""Tests for the CLI twin of the app's ``rates`` command.

``cli.main`` is called with an argv list, exactly as the ``budget`` entry point does,
and the database is pointed at a temporary file through ``BUDGET_DB``.
"""

import datetime
from decimal import Decimal

from budget_tracker import cli, queries
from budget_tracker import rates as rates_module
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.models import Account, Currency, Transaction
from conftest import _setup


def _seed_multi_currency_accounts(tmp_path, monkeypatch):
    """A USD and a CHF account, each with one transaction, ten days apart — so a
    derived fetch range and a two-currency pair both have something to find."""
    db_path = tmp_path / "rates.db"
    engine = get_engine(db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        usd = Currency(value="USD", symbol="$", decimal_places=2)
        chf = Currency(value="CHF", symbol="CHF", decimal_places=2)
        session.add_all([usd, chf])
        session.flush()
        checking = Account(name="Checking", currency_id=usd.id)
        wise_chf = Account(name="Wise CHF", currency_id=chf.id)
        session.add_all([checking, wise_chf])
        session.flush()
        session.add_all(
            [
                Transaction(
                    account_id=checking.id,
                    currency_id=usd.id,
                    posted_date=datetime.date(2025, 6, 1),
                    description="A",
                    raw_description="A",
                    value_minor=-1000,
                    import_hash="a",
                ),
                Transaction(
                    account_id=wise_chf.id,
                    currency_id=chf.id,
                    posted_date=datetime.date(2025, 6, 10),
                    description="B",
                    raw_description="B",
                    value_minor=-2000,
                    import_hash="b",
                ),
            ]
        )
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def test_rates_fetch_derives_its_range_and_pair_from_what_is_on_file(
    tmp_path, monkeypatch, capsys
):
    _seed_multi_currency_accounts(tmp_path, monkeypatch)
    calls = []

    def stub(session, start, end, base, quotes):
        calls.append((start, end, base, tuple(quotes)))
        return 1

    monkeypatch.setattr("budget_tracker.rates.fetch_ecb_rates", stub)

    assert cli.main(["rates", "fetch"]) == 0
    out = capsys.readouterr().out
    assert "Range: 2025-06-01..2025-06-10" in out
    assert "Wrote 1 rate(s) for USD -> CHF." in out
    assert calls == [
        (datetime.date(2025, 6, 1), datetime.date(2025, 6, 10), "USD", ("CHF",))
    ]


def test_rates_fetch_with_explicit_dates_skips_the_derived_range(
    tmp_path, monkeypatch, capsys
):
    _seed_multi_currency_accounts(tmp_path, monkeypatch)
    calls = []

    def stub(session, start, end, base, quotes):
        calls.append((start, end))
        return 3

    monkeypatch.setattr("budget_tracker.rates.fetch_ecb_rates", stub)

    assert cli.main(
        ["rates", "fetch", "--start", "2020-01-01", "--end", "2020-01-31"]
    ) == 0
    out = capsys.readouterr().out
    assert "Range: 2020-01-01..2020-01-31" in out
    assert "Wrote 3 rate(s)" in out
    assert calls == [(datetime.date(2020, 1, 1), datetime.date(2020, 1, 31))]


def test_rates_fetch_reports_a_frankfurter_failure_without_a_traceback(
    tmp_path, monkeypatch, capsys
):
    """Never a traceback, and never a silent success that wrote nothing."""
    _seed_multi_currency_accounts(tmp_path, monkeypatch)

    def failing(session, start, end, base, quotes, **kwargs):
        raise rates_module.FrankfurterError("Could not reach Frankfurter at ...")

    monkeypatch.setattr("budget_tracker.rates.fetch_ecb_rates", failing)

    assert cli.main(["rates", "fetch"]) == 1
    out = capsys.readouterr().out
    assert "Could not fetch ECB rates: Could not reach Frankfurter" in out


def test_rates_fetch_with_one_currency_on_file_fetches_nothing(
    tmp_path, monkeypatch, capsys
):
    _setup(tmp_path, monkeypatch)  # single-currency CSV fixture

    assert cli.main(["rates", "fetch"]) == 0
    assert "nothing to fetch" in capsys.readouterr().out


def test_rates_fetch_with_no_transactions_and_no_dates_is_refused(
    tmp_path, monkeypatch, capsys
):
    db_path = tmp_path / "empty.db"
    init_db(get_engine(db_path))
    monkeypatch.setenv("BUDGET_DB", str(db_path))

    assert cli.main(["rates", "fetch"]) == 1
    assert "No transactions" in capsys.readouterr().out


def test_rates_set_records_a_manual_rate_that_wins_for_that_day(
    tmp_path, monkeypatch, capsys
):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        rates_module.record_rate(
            session, datetime.date(2025, 7, 2), "USD", "CHF", Decimal("0.85"),
            rates_module.ECB,
        )
        session.commit()

    assert cli.main(["rates", "set", "USD", "CHF", "0.90", "--on", "2025-07-02"]) == 0
    out = capsys.readouterr().out
    assert "Set USD -> CHF = 0.90 on 2025-07-02 (manual)." in out

    with session_factory() as session:
        assert rates_module.rate_on(
            session, datetime.date(2025, 7, 2), "USD", "CHF"
        ) == Decimal("0.90")


def test_rates_set_a_bad_rate_is_reported_without_a_traceback(
    tmp_path, monkeypatch, capsys
):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["rates", "set", "USD", "CHF", "not-a-number"]) == 1
    assert "Could not read 'not-a-number' as a rate" in capsys.readouterr().out


def test_rates_list_shows_pair_source_and_span(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        rates_module.record_rate(
            session, datetime.date(2025, 7, 1), "USD", "CHF", Decimal("0.88"),
            rates_module.ECB,
        )
        rates_module.record_rate(
            session, datetime.date(2025, 7, 5), "USD", "CHF", Decimal("0.89"),
            rates_module.ECB,
        )
        session.commit()

    assert cli.main(["rates"]) == 0
    out = capsys.readouterr().out
    assert "USD -> CHF" in out
    assert "2025-07-01..2025-07-05" in out
    assert "2 rates" in out


def test_rates_list_with_nothing_cached_says_so(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["rates"]) == 0
    assert "No exchange rates cached yet." in capsys.readouterr().out


def test_rates_fetch_bases_on_the_reporting_currency(tmp_path, monkeypatch, capsys):
    """Every conversion the aggregates perform is X -> home, and rate_on can invert a
    cached pair but cannot chain two together. Basing the fetch on anything other than
    the reporting currency would leave the conversions that actually matter unreachable.
    """
    session_factory = _setup(tmp_path, monkeypatch)
    from budget_tracker import queries, rates
    from budget_tracker.models import Account, Currency

    with session_factory() as session:
        for code in ("CHF", "EUR"):
            currency = Currency(value=code, symbol=code, decimal_places=2)
            session.add(currency)
            session.flush()
            session.add(Account(name=f"Wise {code}", currency_id=currency.id))
        session.commit()

    seen = {}

    def fake_fetch(session, start, end, base, quotes, **kwargs):
        seen["base"] = base
        seen["quotes"] = sorted(quotes)
        return 0

    monkeypatch.setattr(rates, "fetch_ecb_rates", fake_fetch)
    assert cli.main(["rates", "fetch", "--start", "2026-01-01", "--end", "2026-01-31"]) == 0

    assert seen["base"] == queries.HOME_CURRENCY
    # The home currency is never fetched against itself, and every other one is covered.
    assert queries.HOME_CURRENCY not in seen["quotes"]
    assert seen["quotes"] == ["CHF", "EUR"]
