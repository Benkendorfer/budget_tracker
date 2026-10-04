"""Tests for the ``budget sync`` command family (``cli/sync.py``).

The network and the OS keychain are both faked by monkeypatching the module
attributes :mod:`budget_tracker.sync` resolves lazily at call time --
``budget_tracker.simplefin.claim``/``fetch_accounts`` and
``budget_tracker.credentials.store``/``load``/``delete`` -- exactly as
:mod:`test_sync` fakes them for :mod:`budget_tracker.sync` itself. ``getpass.getpass``
stands in for the hidden setup-token prompt, and ``input`` feeds the interactive
mapping walk, the same way ``test_cli_import``'s picker tests feed ``input``.

Dates are built relative to ``date.today()`` (never a hardcoded date) so a run months
from now does not drift the CSV cutoff out of SimpleFIN's 90-day window.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from budget_tracker import cli, queries
from budget_tracker import credentials as credentials_module
from budget_tracker import simplefin as simplefin_module
from budget_tracker import sync as sync_module
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.importer import import_csv
from budget_tracker.models import Account, Currency, Import, SyncAccount, SyncConnection, Transaction
from budget_tracker.simplefin import AccountSet, AuthFailed, RemoteAccount, RemoteError, RemoteTransaction
from helpers import learn_format

TODAY = date.today()
FLOOR_START = TODAY - timedelta(days=89)
# Comfortably inside the 90-day window, with room either side for OVERLAP_DAYS and
# for a definite-gap warning to still be reachable from it.
CSV_POSTED = TODAY - timedelta(days=60)

AMEX_CSV = (
    "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\n"
    f"{(CSV_POSTED - timedelta(days=1)).isoformat()},{CSV_POSTED.isoformat()},"
    "1008,TRADER JOE S,Groceries,42.17,\n"
)

SECRET_TOKEN = "setup-token-should-never-print-8675309"
SECRET_ACCESS_URL = "https://alice:sekrit-pw-998@bridge.example.com/simplefin"


# --------------------------------------------------------------------------- fixtures


def _db(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    engine = get_engine(db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def _import_amex(session_factory, tmp_path, csv_text=AMEX_CSV, account_name="Amex 1008"):
    path = tmp_path / "amex.csv"
    path.write_text(csv_text, encoding="utf-8")
    with session_factory() as session:
        learn_format(session, path, name="amex_cli_test")
        import_csv(session, path, account_name=account_name)


def _fake_credentials(monkeypatch):
    """An in-memory stand-in for the keychain, keyed exactly like the real one."""
    store: dict = {}
    monkeypatch.setattr(credentials_module, "store", lambda name, secret: store.__setitem__(name, secret))
    monkeypatch.setattr(credentials_module, "load", lambda name: store.get(name))
    monkeypatch.setattr(
        credentials_module, "delete", lambda name: store.pop(name, None) is not None
    )
    return store


def _fake_claim(monkeypatch, access_url=SECRET_ACCESS_URL):
    monkeypatch.setattr(simplefin_module, "claim", lambda token, **kwargs: access_url)


def _fake_fetch(monkeypatch, account_set: AccountSet):
    calls = []

    def fetch(access_url, *, start=None, end=None, account_ids=(), balances_only=False, timeout=60):
        calls.append({"access_url": access_url, "start": start, "end": end})
        return account_set

    fetch.calls = calls
    monkeypatch.setattr(simplefin_module, "fetch_accounts", fetch)
    return fetch


def _fake_fetch_raising(monkeypatch, exc):
    def fetch(access_url, **kwargs):
        raise exc

    monkeypatch.setattr(simplefin_module, "fetch_accounts", fetch)


def _answers(monkeypatch, *replies):
    typed = iter(replies)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(typed))


def _racct(remote_id="r-amex", currency="USD", txns=(), name="Amex Card", balance="0"):
    return RemoteAccount(
        id=remote_id,
        name=name,
        conn_id="c1",
        currency=currency,
        balance=Decimal(balance),
        transactions=tuple(txns),
    )


def _rtxn(remote_id, day, amount, description="MERCHANT"):
    return RemoteTransaction(id=remote_id, posted=day, amount=Decimal(amount), description=description)


# --------------------------------------------------------------------------- connect


def test_connect_maps_to_an_existing_account_in_the_same_currency(tmp_path, monkeypatch, capsys):
    session_factory = _db(tmp_path, monkeypatch)
    _import_amex(session_factory, tmp_path)
    # A different-currency account that must never be offered for a USD remote one.
    with session_factory() as session:
        eur = Currency(value="EUR", decimal_places=2)
        session.add(eur)
        session.flush()
        session.add(Account(name="Europe Checking", currency_id=eur.id))
        session.commit()

    _fake_credentials(monkeypatch)
    _fake_claim(monkeypatch)
    _fake_fetch(monkeypatch, AccountSet(accounts=(_racct(),)))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": SECRET_TOKEN)
    _answers(monkeypatch, "1")  # the only same-currency candidate: Amex 1008

    assert cli.main(["sync", "connect"]) == 0
    out = capsys.readouterr().out
    assert "Europe Checking" not in out  # filtered out: wrong currency
    assert "Mapped 'Amex Card' -> 'Amex 1008'." in out
    assert "sync --dry-run" in out

    with session_factory() as session:
        mapping = session.scalar(select(SyncAccount))
        assert mapping.remote_id == "r-amex"
        assert session.get(Account, mapping.account_id).name == "Amex 1008"


def test_connect_maps_to_a_new_account(tmp_path, monkeypatch, capsys):
    session_factory = _db(tmp_path, monkeypatch)

    _fake_credentials(monkeypatch)
    _fake_claim(monkeypatch)
    _fake_fetch(monkeypatch, AccountSet(accounts=(_racct(name="Checking Remote"),)))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": SECRET_TOKEN)
    _answers(monkeypatch, "n", "")  # new account, accept the suggested default name

    assert cli.main(["sync", "connect"]) == 0
    out = capsys.readouterr().out
    assert "Mapped 'Checking Remote' -> 'Checking Remote'." in out

    with session_factory() as session:
        account = session.scalar(select(Account).where(Account.name == "Checking Remote"))
        assert account is not None


def test_connect_can_skip_an_account(tmp_path, monkeypatch, capsys):
    session_factory = _db(tmp_path, monkeypatch)

    _fake_credentials(monkeypatch)
    _fake_claim(monkeypatch)
    _fake_fetch(monkeypatch, AccountSet(accounts=(_racct(),)))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": SECRET_TOKEN)
    _answers(monkeypatch, "s")

    assert cli.main(["sync", "connect"]) == 0
    out = capsys.readouterr().out
    assert "Skipped 'Amex Card'." in out
    with session_factory() as session:
        assert session.scalar(select(SyncAccount)) is None


def test_connect_reprompts_on_an_invalid_choice(tmp_path, monkeypatch, capsys):
    session_factory = _db(tmp_path, monkeypatch)
    _import_amex(session_factory, tmp_path)

    _fake_credentials(monkeypatch)
    _fake_claim(monkeypatch)
    _fake_fetch(monkeypatch, AccountSet(accounts=(_racct(),)))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": SECRET_TOKEN)
    _answers(monkeypatch, "banana", "9", "1")

    assert cli.main(["sync", "connect"]) == 0
    out = capsys.readouterr().out
    assert out.count("Invalid selection.") == 2


def test_connect_refuses_a_name_already_in_use(tmp_path, monkeypatch, capsys):
    session_factory = _db(tmp_path, monkeypatch)
    with session_factory() as session:
        session.add(SyncConnection(name="simplefin"))
        session.commit()

    def claim(token, **kwargs):
        raise AssertionError("must not claim a token for a name already in use")

    _fake_credentials(monkeypatch)
    monkeypatch.setattr(simplefin_module, "claim", claim)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": SECRET_TOKEN)

    assert cli.main(["sync", "connect"]) == 1
    assert "already" in capsys.readouterr().out.lower()


def test_connect_reports_a_post_claim_fetch_failure_without_losing_the_connection(
    tmp_path, monkeypatch, capsys
):
    session_factory = _db(tmp_path, monkeypatch)

    _fake_credentials(monkeypatch)
    _fake_claim(monkeypatch)
    _fake_fetch_raising(monkeypatch, simplefin_module.SimpleFINError("network blip"))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": SECRET_TOKEN)

    assert cli.main(["sync", "connect"]) == 0
    out = capsys.readouterr().out
    assert "connection" in out.lower() and "saved" in out.lower()
    assert "sync map" in out
    with session_factory() as session:
        assert session.scalar(select(SyncConnection)) is not None


# -------------------------------------------------------------------------------- map


def test_map_rerun_shows_and_keeps_the_current_mapping_by_default(tmp_path, monkeypatch, capsys):
    session_factory = _db(tmp_path, monkeypatch)
    _import_amex(session_factory, tmp_path)
    secrets = _fake_credentials(monkeypatch)
    secrets["simplefin"] = SECRET_ACCESS_URL
    with session_factory() as session:
        session.add(SyncConnection(name="simplefin"))
        session.commit()
        sync_module.map_account(session, "simplefin", _racct(), "Amex 1008")

    _fake_fetch(monkeypatch, AccountSet(accounts=(_racct(),)))
    _answers(monkeypatch, "")  # keep the current mapping

    assert cli.main(["sync", "map"]) == 0
    out = capsys.readouterr().out
    assert "Currently mapped to 'Amex 1008'." in out

    with session_factory() as session:
        mapping = session.scalar(select(SyncAccount))
        assert session.get(Account, mapping.account_id).name == "Amex 1008"


def test_map_rerun_can_change_the_mapping(tmp_path, monkeypatch, capsys):
    session_factory = _db(tmp_path, monkeypatch)
    _import_amex(session_factory, tmp_path)
    secrets = _fake_credentials(monkeypatch)
    secrets["simplefin"] = SECRET_ACCESS_URL
    with session_factory() as session:
        usd = session.scalar(select(Currency).where(Currency.value == "USD"))
        session.add(Account(name="New Card", currency_id=usd.id))
        session.add(SyncConnection(name="simplefin"))
        session.commit()
        sync_module.map_account(session, "simplefin", _racct(), "Amex 1008")

    _fake_fetch(monkeypatch, AccountSet(accounts=(_racct(),)))
    # Candidates are listed alphabetically by queries.get_accounts: Amex 1008, New Card.
    _answers(monkeypatch, "2")

    assert cli.main(["sync", "map"]) == 0
    with session_factory() as session:
        mapping = session.scalar(select(SyncAccount))
        assert session.get(Account, mapping.account_id).name == "New Card"


# ------------------------------------------------------------------------------ status


def test_status_with_nothing_connected(tmp_path, monkeypatch, capsys):
    _db(tmp_path, monkeypatch)
    assert cli.main(["sync", "status"]) == 0
    assert "sync connect" in capsys.readouterr().out


def test_status_lists_mappings(tmp_path, monkeypatch, capsys):
    session_factory = _db(tmp_path, monkeypatch)
    _import_amex(session_factory, tmp_path)
    with session_factory() as session:
        session.add(SyncConnection(name="simplefin"))
        session.commit()
        sync_module.map_account(session, "simplefin", _racct(name="Amex Card"), "Amex 1008")

    assert cli.main(["sync", "status"]) == 0
    out = capsys.readouterr().out
    assert "simplefin" in out
    assert "Amex 1008" in out and "Amex Card" in out
    assert "never" in out  # not yet synced


# ------------------------------------------------------------------------------- run


def _connected_amex(session_factory, tmp_path, monkeypatch, csv_text=AMEX_CSV):
    _import_amex(session_factory, tmp_path, csv_text=csv_text)
    secrets = _fake_credentials(monkeypatch)
    secrets["simplefin"] = SECRET_ACCESS_URL
    with session_factory() as session:
        session.add(SyncConnection(name="simplefin"))
        session.commit()
        sync_module.map_account(session, "simplefin", _racct(), "Amex 1008")


def test_run_with_nothing_connected_is_an_error(tmp_path, monkeypatch, capsys):
    _db(tmp_path, monkeypatch)
    assert cli.main(["sync"]) == 1
    assert "sync connect" in capsys.readouterr().out


def test_run_reports_missing_credentials(tmp_path, monkeypatch, capsys):
    session_factory = _db(tmp_path, monkeypatch)
    _import_amex(session_factory, tmp_path)
    _fake_credentials(monkeypatch)  # empty: nothing stored under this name
    with session_factory() as session:
        session.add(SyncConnection(name="simplefin"))
        session.commit()
        sync_module.map_account(session, "simplefin", _racct(), "Amex 1008")

    assert cli.main(["sync"]) == 1
    out = capsys.readouterr().out
    assert "credentials" in out.lower()


def test_dry_run_banner_at_start_and_end_and_writes_nothing(tmp_path, monkeypatch, capsys):
    session_factory = _db(tmp_path, monkeypatch)
    _connected_amex(session_factory, tmp_path, monkeypatch)
    with session_factory() as session:
        before_imports = session.scalar(select(func.count()).select_from(Import))

    # Confirms coverage back to the cutoff, and inserts one new row, so the dry run
    # has a real count to report without writing it.
    confirming = _rtxn("rt-existing", CSV_POSTED, "-42.17", "TRADER JOE S")
    new_txn = _rtxn("rt-new", CSV_POSTED + timedelta(days=1), "-12.34", "NEW SHOP")
    _fake_fetch(
        monkeypatch, AccountSet(accounts=(_racct(txns=[confirming, new_txn]),))
    )

    assert cli.main(["sync", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.count("DRY RUN") >= 2
    assert out.strip().startswith("DRY RUN")
    assert out.strip().endswith("DRY RUN — nothing was written.")
    assert "inserted 1" in out
    assert "matched 1 existing CSV row(s)" in out

    with session_factory() as session:
        after_imports = session.scalar(select(func.count()).select_from(Import))
        assert after_imports == before_imports
        mapping = session.scalar(select(SyncAccount))
        assert mapping.synced_through is None


def test_a_real_runs_output_names_counts_and_start_date(tmp_path, monkeypatch, capsys):
    session_factory = _db(tmp_path, monkeypatch)
    _connected_amex(session_factory, tmp_path, monkeypatch)

    confirming = _rtxn("rt-existing", CSV_POSTED, "-42.17", "TRADER JOE S")
    new_txn = _rtxn("rt-new", CSV_POSTED + timedelta(days=1), "-12.34", "NEW SHOP")
    _fake_fetch(monkeypatch, AccountSet(accounts=(_racct(txns=[confirming, new_txn]),)))

    assert cli.main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "Connection 'simplefin':" in out
    assert "Amex 1008 (remote: 'Amex Card')" in out
    expected_start = CSV_POSTED - timedelta(days=sync_module.OVERLAP_DAYS)
    assert f"start: {expected_start.isoformat()}" in out
    assert "fetched 2, inserted 1, matched 1 existing CSV row(s), already synced 0, pending skipped 0" in out
    assert "DRY RUN" not in out

    with session_factory() as session:
        inserted = session.scalar(select(Transaction).where(Transaction.description == "NEW SHOP"))
        assert inserted is not None


def test_inserted_before_cutoff_gets_its_own_line(tmp_path, monkeypatch, capsys):
    session_factory = _db(tmp_path, monkeypatch)
    _connected_amex(session_factory, tmp_path, monkeypatch)

    # A distinct amount inside the CSV's own range: never matches, always inserted,
    # and dated on/before the cutoff.
    odd = _rtxn("rt1", CSV_POSTED, "-77.77", "ODD SHOP")
    _fake_fetch(monkeypatch, AccountSet(accounts=(_racct(txns=[odd]),)))

    assert cli.main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "1 inserted transaction(s) are dated on or before your last CSV import" in out


def test_remote_errors_and_unmapped_accounts_are_listed(tmp_path, monkeypatch, capsys):
    session_factory = _db(tmp_path, monkeypatch)
    _connected_amex(session_factory, tmp_path, monkeypatch)

    confirming = _rtxn("rt-existing", CSV_POSTED, "-42.17", "TRADER JOE S")
    account_set = AccountSet(
        accounts=(
            _racct(txns=[confirming]),
            _racct(remote_id="r-other", name="Unmapped Checking"),
        ),
        errors=(RemoteError(code="bad-conn", msg="needs attention"),),
    )
    _fake_fetch(monkeypatch, account_set)

    assert cli.main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "bad-conn: needs attention" in out
    assert "Unmapped Checking" in out


def test_gap_warning_block_comes_last(tmp_path, monkeypatch, capsys):
    session_factory = _db(tmp_path, monkeypatch)
    _import_amex(session_factory, tmp_path)
    secrets = _fake_credentials(monkeypatch)
    secrets["simplefin"] = SECRET_ACCESS_URL
    with session_factory() as session:
        session.add(SyncConnection(name="simplefin"))
        session.commit()
        mapping = sync_module.map_account(session, "simplefin", _racct(), "Amex 1008")
        # Far enough in the past that part of the gap is permanently unreachable --
        # see test_sync.test_definite_gap_warning.
        mapping.synced_through = FLOOR_START - timedelta(days=5)
        session.commit()

    _fake_fetch(monkeypatch, AccountSet(accounts=(_racct(txns=()),)))

    assert cli.main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "⚠ POSSIBLE GAP" in out
    gap_index = out.index("⚠ POSSIBLE GAP")
    # Nothing printed afterward reopens another connection's or account's counts --
    # the gap block really is the last thing on screen.
    assert "Connection 'simplefin':" not in out[gap_index + 1 :]
    # Two coverage warnings fire for this account (a definite gap, and the separate
    # "could not confirm" check -- nothing at all came back); the block ends with the
    # last of them, not the counts above it.
    assert out.rstrip().endswith("so there may be a gap.")


def test_sign_inversion_is_refused_and_writes_nothing(tmp_path, monkeypatch, capsys):
    csv_text = (
        "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\n"
        f"{(CSV_POSTED - timedelta(days=2)).isoformat()},{(CSV_POSTED - timedelta(days=2)).isoformat()},1008,SHOP A,Shopping,10.00,\n"
        f"{(CSV_POSTED - timedelta(days=1)).isoformat()},{(CSV_POSTED - timedelta(days=1)).isoformat()},1008,SHOP B,Shopping,20.00,\n"
        f"{CSV_POSTED.isoformat()},{CSV_POSTED.isoformat()},1008,SHOP C,Shopping,30.00,\n"
    )
    session_factory = _db(tmp_path, monkeypatch)
    _connected_amex(session_factory, tmp_path, monkeypatch, csv_text=csv_text)

    inverted = [
        _rtxn("rt1", CSV_POSTED - timedelta(days=2), "10.00", "SHOP A"),
        _rtxn("rt2", CSV_POSTED - timedelta(days=1), "20.00", "SHOP B"),
        _rtxn("rt3", CSV_POSTED, "30.00", "SHOP C"),
    ]
    _fake_fetch(monkeypatch, AccountSet(accounts=(_racct(txns=inverted),)))

    with session_factory() as session:
        before = session.scalar(select(func.count()).select_from(Import))

    assert cli.main(["sync"]) == 1
    out = capsys.readouterr().out
    assert "sign" in out.lower()

    with session_factory() as session:
        after = session.scalar(select(func.count()).select_from(Import))
        assert after == before  # nothing written


def test_auth_failed_is_reported_cleanly(tmp_path, monkeypatch, capsys):
    session_factory = _db(tmp_path, monkeypatch)
    _connected_amex(session_factory, tmp_path, monkeypatch)
    _fake_fetch_raising(monkeypatch, AuthFailed("revoked"))

    assert cli.main(["sync"]) == 1
    out = capsys.readouterr().out
    assert "sync connect" in out


# ------------------------------------------------------------------------ disconnect


def test_disconnect_declined_keeps_the_connection(tmp_path, monkeypatch, capsys):
    session_factory = _db(tmp_path, monkeypatch)
    secrets = _fake_credentials(monkeypatch)
    secrets["simplefin"] = SECRET_ACCESS_URL
    with session_factory() as session:
        session.add(SyncConnection(name="simplefin"))
        session.commit()

    _answers(monkeypatch, "n")
    assert cli.main(["sync", "disconnect"]) == 0
    out = capsys.readouterr().out
    assert "Not disconnected." in out
    with session_factory() as session:
        assert session.scalar(select(SyncConnection)) is not None
    assert secrets == {"simplefin": SECRET_ACCESS_URL}


def test_disconnect_confirmed_removes_connection_and_keychain_entry_but_keeps_transactions(
    tmp_path, monkeypatch, capsys
):
    session_factory = _db(tmp_path, monkeypatch)
    _connected_amex(session_factory, tmp_path, monkeypatch)
    new_txn = _rtxn("rt1", CSV_POSTED + timedelta(days=1), "-12.34", "NEW SHOP")
    _fake_fetch(monkeypatch, AccountSet(accounts=(_racct(txns=[new_txn]),)))
    assert cli.main(["sync"]) == 0
    capsys.readouterr()

    _answers(monkeypatch, "y")
    assert cli.main(["sync", "disconnect"]) == 0
    out = capsys.readouterr().out
    assert "Disconnected 'simplefin'." in out
    assert "kept" in out.lower() or "still here" in out.lower()

    assert credentials_module.load("simplefin") is None  # keychain entry removed
    with session_factory() as session:
        assert session.scalar(select(SyncConnection)) is None
        assert session.scalar(select(SyncAccount)) is None
        assert session.scalar(select(Transaction).where(Transaction.description == "NEW SHOP")) is not None


# --------------------------------------------------------------------------- secrecy


def test_no_secret_is_ever_printed(tmp_path, monkeypatch, capsys):
    session_factory = _db(tmp_path, monkeypatch)
    _import_amex(session_factory, tmp_path)

    _fake_credentials(monkeypatch)
    _fake_claim(monkeypatch)
    _fake_fetch(monkeypatch, AccountSet(accounts=(_racct(),)))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": SECRET_TOKEN)
    _answers(monkeypatch, "1")

    assert cli.main(["sync", "connect"]) == 0
    captured_connect = capsys.readouterr()

    assert cli.main(["sync", "status"]) == 0
    captured_status = capsys.readouterr()

    new_txn = _rtxn("rt1", CSV_POSTED + timedelta(days=1), "-12.34", "NEW SHOP")
    _fake_fetch(monkeypatch, AccountSet(accounts=(_racct(txns=[new_txn]),)))
    assert cli.main(["sync"]) == 0
    captured_run = capsys.readouterr()

    everything = "\n".join(
        c.out + c.err for c in (captured_connect, captured_status, captured_run)
    )
    assert SECRET_TOKEN not in everything
    assert SECRET_ACCESS_URL not in everything
    assert "sekrit-pw-998" not in everything
    assert "alice:sekrit-pw-998" not in everything
