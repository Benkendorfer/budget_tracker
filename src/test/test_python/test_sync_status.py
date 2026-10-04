"""Tests for per-account sync status (``models.SyncAccount.last_status``/``last_error``).

See ``sync.py``'s ``_sync_one_connection`` for where it is set, and ``cli/sync.py``'s
``budget sync status`` for where it is shown. Fakes follow the same pattern as
``test_sync.py``: ``fetch``/``load_secret`` are injected directly into ``run_sync``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from budget_tracker import cli, sync
from budget_tracker import credentials as credentials_module
from budget_tracker import simplefin as simplefin_module
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.importer import import_csv
from budget_tracker.models import SYNC_ERROR, SYNC_OK, SyncAccount, SyncConnection, Transaction
from budget_tracker.simplefin import AccountSet, AuthFailed, RemoteAccount, RemoteError, RemoteTransaction
from helpers import learn_format

TODAY = date(2026, 10, 3)
SECRET = "https://u:p@host/simplefin"

AMEX_CSV = (
    "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\n"
    "2026-07-21,2026-07-22,1008,SHOP A,Shopping,10.00,\n"
    "2026-07-22,2026-07-23,1008,SHOP B,Shopping,20.00,\n"
    "2026-07-23,2026-07-24,1008,SHOP C,Shopping,30.00,\n"
)


# --------------------------------------------------------------------------- fixtures


def _db(tmp_path):
    engine = get_engine(tmp_path / "t.db")
    init_db(engine)
    return get_sessionmaker(engine)


def _connection(session, name="simplefin") -> SyncConnection:
    connection = SyncConnection(name=name)
    session.add(connection)
    session.commit()
    return connection


def _racct(remote_id="r1", currency="USD", txns=(), name="Card", conn_id="c1"):
    return RemoteAccount(
        id=remote_id,
        name=name,
        conn_id=conn_id,
        currency=currency,
        balance=Decimal("0"),
        transactions=tuple(txns),
    )


def _rtxn(remote_id, day, amount, description="MERCHANT"):
    return RemoteTransaction(id=remote_id, posted=day, amount=Decimal(amount), description=description)


def _fetch_returning(account_set: AccountSet):
    def fetch(access_url, *, start=None, end=None, account_ids=(), balances_only=False, timeout=60):
        return account_set

    return fetch


def _fetch_raising(exc):
    def fetch(access_url, **kwargs):
        raise exc

    return fetch


def _loader(secrets):
    return lambda name: secrets.get(name)


def _mapped(session, connection_name="simplefin", remote_id="r1", account_name="Checking", currency="USD"):
    _connection(session, connection_name)
    remote = _racct(remote_id=remote_id, currency=currency)
    return sync.map_account(session, connection_name, remote, account_name)


def _import_csv_file(session, tmp_path, csv_text, account_name):
    path = tmp_path / "amex.csv"
    path.write_text(csv_text, encoding="utf-8")
    learn_format(session, path, name="amex_status_test")
    return import_csv(session, path, account_name=account_name)


# ---------------------------------------------------------------------------- basics


def test_ok_status_after_a_clean_sync(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _mapped(session)
        session.commit()

    account_set = AccountSet(accounts=(_racct(),))
    with session_factory() as session:
        sync.run_sync(
            session,
            "simplefin",
            today=TODAY,
            load_secret=_loader({"simplefin": SECRET}),
            fetch=_fetch_returning(account_set),
        )

    with session_factory() as session:
        mapping = session.scalar(select(SyncAccount))
        assert mapping.last_status == SYNC_OK
        assert mapping.last_error is None


def test_account_missing_from_response_is_an_error(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _mapped(session)
        session.commit()

    with session_factory() as session:
        sync.run_sync(
            session,
            "simplefin",
            today=TODAY,
            load_secret=_loader({"simplefin": SECRET}),
            fetch=_fetch_returning(AccountSet()),  # r1 never shows up
        )

    with session_factory() as session:
        mapping = session.scalar(select(SyncAccount))
        assert mapping.last_status == SYNC_ERROR
        assert mapping.last_error is not None
        assert "not returned" in mapping.last_error.lower()


def test_conn_level_errlist_on_a_returned_account_is_still_an_error(tmp_path):
    """The real case this models: SimpleFIN still returns a stale account alongside
    ``con.auth: ... Auth required`` for its connection -- presence alone is not
    success."""
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _mapped(session)
        session.commit()

    account_set = AccountSet(
        accounts=(_racct(conn_id="c1"),),
        errors=(
            RemoteError(
                code="con.auth",
                msg="Connection to Interactive Brokers may need attention. Auth required",
                conn_id="c1",
            ),
        ),
    )
    with session_factory() as session:
        sync.run_sync(
            session,
            "simplefin",
            today=TODAY,
            load_secret=_loader({"simplefin": SECRET}),
            fetch=_fetch_returning(account_set),
        )

    with session_factory() as session:
        mapping = session.scalar(select(SyncAccount))
        assert mapping.last_status == SYNC_ERROR
        assert "con.auth" in mapping.last_error
        assert "Auth required" in mapping.last_error


def test_account_level_errlist_is_an_error(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _mapped(session)
        session.commit()

    account_set = AccountSet(
        accounts=(_racct(),),
        errors=(RemoteError(code="bad-acct", msg="account needs attention", account_id="r1"),),
    )
    with session_factory() as session:
        sync.run_sync(
            session,
            "simplefin",
            today=TODAY,
            load_secret=_loader({"simplefin": SECRET}),
            fetch=_fetch_returning(account_set),
        )

    with session_factory() as session:
        mapping = session.scalar(select(SyncAccount))
        assert mapping.last_status == SYNC_ERROR
        assert "bad-acct" in mapping.last_error
        assert "account needs attention" in mapping.last_error


def test_currency_mismatch_skip_is_an_error(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _mapped(session, currency="USD")
        session.commit()

    # The remote account now reports a currency that disagrees with the local one.
    account_set = AccountSet(accounts=(_racct(currency="EUR"),))
    with session_factory() as session:
        sync.run_sync(
            session,
            "simplefin",
            today=TODAY,
            load_secret=_loader({"simplefin": SECRET}),
            fetch=_fetch_returning(account_set),
        )

    with session_factory() as session:
        mapping = session.scalar(select(SyncAccount))
        assert mapping.last_status == SYNC_ERROR
        assert "skipped" in mapping.last_error


# ------------------------------------------------------------------- connection-level


def test_missing_credentials_marks_every_mapping_error_and_still_raises(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _mapped(session, remote_id="r1", account_name="Checking")
        sync.map_account(session, "simplefin", _racct(remote_id="r2", name="Savings"), "Savings")
        session.commit()

    with session_factory() as session:
        with pytest.raises(sync.MissingCredentials):
            sync.run_sync(
                session,
                "simplefin",
                today=TODAY,
                load_secret=lambda n: None,
                fetch=_fetch_returning(AccountSet()),
            )

    with session_factory() as session:
        mappings = session.scalars(select(SyncAccount)).all()
        assert len(mappings) == 2
        for mapping in mappings:
            assert mapping.last_status == SYNC_ERROR
            assert "credentials" in mapping.last_error.lower()
            # Nothing else is written on a connection-level failure.
            assert mapping.synced_through is None
            assert mapping.last_synced_at is None


def test_auth_failed_marks_every_mapping_error_and_still_raises(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _mapped(session)
        session.commit()

    with session_factory() as session:
        with pytest.raises(sync.SyncError):
            sync.run_sync(
                session,
                "simplefin",
                today=TODAY,
                load_secret=_loader({"simplefin": SECRET}),
                fetch=_fetch_raising(AuthFailed("revoked")),
            )

    with session_factory() as session:
        mapping = session.scalar(select(SyncAccount))
        assert mapping.last_status == SYNC_ERROR
        assert mapping.last_error is not None


def test_sign_inversion_marks_error_and_writes_no_transactions(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _import_csv_file(session, tmp_path, AMEX_CSV, "Amex 1008")
        _connection(session, "simplefin")
        sync.map_account(session, "simplefin", _racct(remote_id="r-amex", currency="USD"), "Amex 1008")
        session.commit()

    inverted = [
        _rtxn("rt1", date(2026, 7, 22), "10.00", "SHOP A"),
        _rtxn("rt2", date(2026, 7, 23), "20.00", "SHOP B"),
        _rtxn("rt3", date(2026, 7, 24), "30.00", "SHOP C"),
    ]
    account_set = AccountSet(accounts=(_racct(remote_id="r-amex", currency="USD", txns=inverted),))

    with session_factory() as session:
        before_txns = session.scalar(select(func.count()).select_from(Transaction))

    with session_factory() as session:
        with pytest.raises(sync.SyncError, match="sign"):
            sync.run_sync(
                session,
                "simplefin",
                today=TODAY,
                load_secret=_loader({"simplefin": SECRET}),
                fetch=_fetch_returning(account_set),
            )

    with session_factory() as session:
        after_txns = session.scalar(select(func.count()).select_from(Transaction))
        assert after_txns == before_txns  # nothing written besides the status itself
        mapping = session.scalar(select(SyncAccount))
        assert mapping.last_status == SYNC_ERROR
        assert "sign" in mapping.last_error.lower()
        assert mapping.synced_through is None


# -------------------------------------------------------------------------- dry runs


def test_dry_run_leaves_status_unchanged(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _mapped(session)
        session.commit()

    # A connection-level failure would normally mark every mapping error; a dry run
    # must still raise (the exception names something the user needs to act on) but
    # not touch the status.
    with session_factory() as session:
        with pytest.raises(sync.MissingCredentials):
            sync.run_sync(
                session,
                "simplefin",
                dry_run=True,
                today=TODAY,
                load_secret=lambda n: None,
                fetch=_fetch_returning(AccountSet()),
            )
    with session_factory() as session:
        mapping = session.scalar(select(SyncAccount))
        assert mapping.last_status is None

    # Nor does an otherwise-clean dry run.
    with session_factory() as session:
        sync.run_sync(
            session,
            "simplefin",
            dry_run=True,
            today=TODAY,
            load_secret=_loader({"simplefin": SECRET}),
            fetch=_fetch_returning(AccountSet(accounts=(_racct(),))),
        )
    with session_factory() as session:
        mapping = session.scalar(select(SyncAccount))
        assert mapping.last_status is None


def test_a_later_successful_sync_flips_error_to_ok_and_clears_the_message(tmp_path):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _mapped(session)
        session.commit()

    with session_factory() as session:
        sync.run_sync(
            session,
            "simplefin",
            today=TODAY,
            load_secret=_loader({"simplefin": SECRET}),
            fetch=_fetch_returning(AccountSet()),  # missing -> error
        )
    with session_factory() as session:
        mapping = session.scalar(select(SyncAccount))
        assert mapping.last_status == SYNC_ERROR
        assert mapping.last_error is not None

    with session_factory() as session:
        sync.run_sync(
            session,
            "simplefin",
            today=TODAY,
            load_secret=_loader({"simplefin": SECRET}),
            fetch=_fetch_returning(AccountSet(accounts=(_racct(),))),  # clean this time
        )
    with session_factory() as session:
        mapping = session.scalar(select(SyncAccount))
        assert mapping.last_status == SYNC_OK
        assert mapping.last_error is None


# ------------------------------------------------------------------------------- cli


def test_cli_sync_status_shows_ok_and_error(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "t.db"
    engine = get_engine(db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    monkeypatch.setenv("BUDGET_DB", str(db_path))

    secrets: dict = {"simplefin": SECRET}
    monkeypatch.setattr(credentials_module, "store", lambda name, secret: secrets.__setitem__(name, secret))
    monkeypatch.setattr(credentials_module, "load", lambda name: secrets.get(name))
    monkeypatch.setattr(credentials_module, "delete", lambda name: secrets.pop(name, None) is not None)

    with session_factory() as session:
        session.add(SyncConnection(name="simplefin"))
        session.commit()
        sync.map_account(session, "simplefin", _racct(remote_id="r-ok", name="Good"), "Good Acct")
        sync.map_account(session, "simplefin", _racct(remote_id="r-bad", name="Bad"), "Bad Acct")

    # r-bad never comes back in the response -> error; r-ok is clean -> ok.
    account_set = AccountSet(accounts=(_racct(remote_id="r-ok", name="Good"),))
    monkeypatch.setattr(simplefin_module, "fetch_accounts", lambda *a, **k: account_set)

    assert cli.main(["sync"]) == 0
    capsys.readouterr()

    assert cli.main(["sync", "status"]) == 0
    out = capsys.readouterr().out
    # A table row per account; a long error may wrap onto following lines within its
    # cell, so the reason is checked on the whitespace-collapsed output.
    good_line = next(line for line in out.splitlines() if "Good Acct" in line)
    bad_line = next(line for line in out.splitlines() if "Bad Acct" in line)
    assert good_line.rstrip().endswith("ok")
    assert "ERROR" in bad_line and "ERROR" not in good_line
    assert "not returned" in " ".join(out.split()).lower()


def _status_after(tmp_path, account_conn_id, error_conn_id):
    session_factory = _db(tmp_path)
    with session_factory() as session:
        _mapped(session)
        session.commit()
    account_set = AccountSet(
        accounts=(_racct(conn_id=account_conn_id),),
        errors=(RemoteError(code="con.auth", msg="Auth required", conn_id=error_conn_id),),
    )
    with session_factory() as session:
        sync.run_sync(
            session, "simplefin", today=TODAY,
            load_secret=_loader({"simplefin": SECRET}),
            fetch=_fetch_returning(account_set),
        )
    with session_factory() as session:
        return session.scalar(select(SyncAccount)).last_status


def test_conn_error_matches_despite_simplefins_prefix_mismatch(tmp_path):
    """Real SimpleFIN ids: the account says MX-MBR-<uuid>, its errlist entry MBR-<uuid>.
    Exact matching left the user's IBKR account green under "Auth required"."""
    uuid = "10911cc2-daad-4f3c-8fb7-e5d310a6b57e"
    assert _status_after(tmp_path, f"MX-MBR-{uuid}", f"MBR-{uuid}") == SYNC_ERROR


def test_another_connections_error_does_not_turn_this_account_red(tmp_path):
    assert (
        _status_after(
            tmp_path,
            "MX-MBR-f7306810-6ca7-4fa4-8c92-bc92fb013760",
            "MBR-10911cc2-daad-4f3c-8fb7-e5d310a6b57e",
        )
        == SYNC_OK
    )
