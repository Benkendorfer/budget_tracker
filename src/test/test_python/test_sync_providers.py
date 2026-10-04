"""Synci as a second SimpleFIN-protocol provider: `budget sync connect --provider synci`.

Same protocol and code path as SimpleFIN Bridge; what differs is the name in every
message and how much history the server keeps (60 days on Synci's Basic plan, against
SimpleFIN's 90), which is what the gap warnings measure against.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from budget_tracker import cli
from budget_tracker import sync as sync_module
from budget_tracker.models import SyncConnection
from budget_tracker.simplefin import AccountSet, RemoteAccount, RemoteTransaction

from test_cli_sync import (
    SECRET_ACCESS_URL,
    _answers,
    _db,
    _fake_claim,
    _fake_credentials,
    _fake_fetch,
    _import_amex,
)


def _gbp_account(txns=()):
    return RemoteAccount(
        id="r-monzo", name="Current Account", conn_id="c1", currency="GBP",
        balance=Decimal("0"), connection_name="Monzo", transactions=tuple(txns),
    )


def test_connect_with_synci_names_it_and_stores_the_provider(tmp_path, monkeypatch, capsys):
    session_factory = _db(tmp_path, monkeypatch)
    _fake_credentials(monkeypatch)
    _fake_claim(monkeypatch)
    _fake_fetch(monkeypatch, AccountSet(accounts=(_gbp_account(),)))
    prompts = []
    monkeypatch.setattr(
        "getpass.getpass", lambda prompt="": prompts.append(prompt) or "token"
    )
    _answers(monkeypatch, "s")  # skip mapping; only the connection matters here

    assert cli.main(["sync", "connect", "--provider", "synci"]) == 0
    out = capsys.readouterr().out
    assert "Synci setup token" in prompts[0]
    assert "Contacting Synci" in out and "Contacting SimpleFIN" not in out
    assert "Connected as 'synci'." in out  # named after its provider by default
    with session_factory() as session:
        connection = session.scalar(select(SyncConnection))
        assert (connection.name, connection.provider) == ("synci", "synci")


def test_synci_gap_warning_uses_its_60_days(tmp_path, monkeypatch, capsys):
    """An anchor 70 days back is inside SimpleFIN's 90 days but outside Synci's 60."""
    session_factory = _db(tmp_path, monkeypatch)
    _import_amex(session_factory, tmp_path)  # any account with CSV history will do
    secrets = _fake_credentials(monkeypatch)
    secrets["synci"] = SECRET_ACCESS_URL
    today = date.today()
    remote = _gbp_account().__class__(
        id="r-amex", name="Card", conn_id="c1", currency="USD", balance=Decimal("0"),
        transactions=(RemoteTransaction("t1", today, Decimal("-5"), "SHOP"),),
    )
    with session_factory() as session:
        session.add(SyncConnection(name="synci", provider="synci"))
        session.commit()
        mapping = sync_module.map_account(session, "synci", remote, "Amex 1008")
        mapping.synced_through = today - timedelta(days=70)
        session.commit()
    _fake_fetch(monkeypatch, AccountSet(accounts=(remote,)))

    assert cli.main(["sync", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Contacting Synci" in out
    assert "older than Synci's 60-day history" in out
    assert "⚠ POSSIBLE GAP" in out


def test_simplefin_stays_the_default(tmp_path, monkeypatch, capsys):
    session_factory = _db(tmp_path, monkeypatch)
    _fake_credentials(monkeypatch)
    _fake_claim(monkeypatch)
    _fake_fetch(monkeypatch, AccountSet())
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "token")

    assert cli.main(["sync", "connect"]) == 0
    with session_factory() as session:
        connection = session.scalar(select(SyncConnection))
        assert (connection.name, connection.provider) == ("simplefin", "simplefin")


def test_an_unknown_provider_is_refused_before_claiming(tmp_path, monkeypatch):
    session_factory = _db(tmp_path, monkeypatch)
    claimed = []
    with session_factory() as session:
        try:
            sync_module.connect(
                session, "token", "x", provider="plaid",
                claim=lambda t: claimed.append(t) or "url",
                store_secret=lambda n, s: None, load_secret=lambda n: None,
                fetch=lambda *a, **k: AccountSet(),
            )
        except sync_module.SyncError as error:
            assert "plaid" in str(error)
        else:
            raise AssertionError("an unknown provider was accepted")
    assert claimed == []  # the one-time token was not burned


# --------------------------------------------------------------------- --clipboard


def test_connect_can_read_the_token_from_the_clipboard(tmp_path, monkeypatch, capsys):
    import subprocess

    _db(tmp_path, monkeypatch)
    _fake_credentials(monkeypatch)
    claimed = []
    monkeypatch.setattr(
        "budget_tracker.simplefin.claim",
        lambda token, **kw: claimed.append(token) or SECRET_ACCESS_URL,
    )
    _fake_fetch(monkeypatch, AccountSet())
    token = "c2V0dXAtdG9rZW4tdGhhdC1pcy1sb25n" * 40  # ~1,300 chars: past the prompt limit
    wrapped = "\n".join(token[i:i + 64] for i in range(0, len(token), 64)) + "\n"
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=wrapped),
    )

    def no_prompt(prompt=""):
        raise AssertionError("--clipboard must not prompt")

    monkeypatch.setattr("getpass.getpass", no_prompt)

    assert cli.main(["sync", "connect", "--provider", "synci", "--clipboard"]) == 0
    out = capsys.readouterr().out
    assert claimed == [token]  # whole, with the wrapping removed
    assert f"Read a {len(token)}-character setup token." in out
    assert token[:40] not in out  # the token itself is never shown


def test_clipboard_failure_is_reported_without_claiming(tmp_path, monkeypatch, capsys):
    import subprocess

    _db(tmp_path, monkeypatch)
    _fake_credentials(monkeypatch)
    claimed = []
    monkeypatch.setattr(
        "budget_tracker.simplefin.claim", lambda token, **kw: claimed.append(token) or "x"
    )

    def broken(cmd, **kw):
        raise OSError("pbpaste not found")

    monkeypatch.setattr(subprocess, "run", broken)

    assert cli.main(["sync", "connect", "--clipboard"]) == 1
    assert "Could not read the clipboard" in capsys.readouterr().out
    assert claimed == []
