"""Tests for the CLI twin of the app's ``transfers`` command.

``cli.main`` is called with an argv list, exactly as the ``budget`` entry point does,
and the database is pointed at a temporary file through ``BUDGET_DB``.
"""

import datetime

from budget_tracker import cli, queries
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.models import Account, Currency, Transaction


def _seed_same_account_transfer_candidates(tmp_path, monkeypatch):
    """Two legs in one account, same amount and opposite sign, a day apart.

    Only pairable with ``allow_same_account`` — the default rule requires different
    accounts.
    """
    db_path = tmp_path / "same_account.db"
    engine = get_engine(db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        currency = Currency(value="USD", symbol="$", decimal_places=2)
        session.add(currency)
        session.flush()
        account = Account(name="Checking", currency_id=currency.id)
        session.add(account)
        session.flush()
        session.add(
            Transaction(
                account_id=account.id,
                currency_id=currency.id,
                posted_date=datetime.date(2025, 6, 1),
                description="Move out",
                raw_description="Move out",
                value_minor=-50000,
                category_source="unset",
                import_hash="sa-out",
            )
        )
        session.add(
            Transaction(
                account_id=account.id,
                currency_id=currency.id,
                posted_date=datetime.date(2025, 6, 2),
                description="Move in",
                raw_description="Move in",
                value_minor=50000,
                category_source="unset",
                import_hash="sa-in",
            )
        )
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def test_transfers_default_does_not_pair_same_account_legs(tmp_path, monkeypatch, capsys):
    _seed_same_account_transfer_candidates(tmp_path, monkeypatch)

    assert cli.main(["transfers"]) == 0
    out = capsys.readouterr().out
    assert "Found 0 new transfer pair(s)" in out
    assert "0 transaction(s) are now excluded" in out


def test_transfers_same_account_flag_pairs_them(tmp_path, monkeypatch, capsys):
    session_factory = _seed_same_account_transfer_candidates(tmp_path, monkeypatch)

    assert cli.main(["transfers", "--same-account"]) == 0
    out = capsys.readouterr().out
    assert "Found 1 new transfer pair(s)" in out
    assert "(same-account allowed)" in out
    assert "2 transaction(s) are now excluded" in out
    with session_factory() as session:
        assert queries.get_totals(session).transfer_count == 2


def test_transfers_reset_undoes_a_same_account_pairing(tmp_path, monkeypatch, capsys):
    session_factory = _seed_same_account_transfer_candidates(tmp_path, monkeypatch)

    cli.main(["transfers", "--same-account"])
    capsys.readouterr()

    assert cli.main(["transfers", "--reset"]) == 0
    assert "Un-paired 2 transaction(s)." in capsys.readouterr().out
    with session_factory() as session:
        assert queries.get_totals(session).transfer_count == 0
