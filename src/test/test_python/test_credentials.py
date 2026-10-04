"""credentials.py never touches the real keychain: an in-memory backend stands in."""

from __future__ import annotations

import sys

import pytest
import keyring
import keyring.errors
from keyring.backend import KeyringBackend

from budget_tracker import credentials


class _InMemoryKeyring(KeyringBackend):
    """Minimal backend that keeps secrets in a dict, standing in for the OS keychain."""

    priority = 1

    def __init__(self):
        super().__init__()
        self._store = {}

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def get_password(self, service, username):
        return self._store.get((service, username))

    def delete_password(self, service, username):
        try:
            del self._store[(service, username)]
        except KeyError:
            raise keyring.errors.PasswordDeleteError("not found")


class _NoBackendKeyring(KeyringBackend):
    """Stands in for keyring finding no usable backend (the 'fail' backend)."""

    priority = 1

    def set_password(self, service, username, password):
        raise keyring.errors.NoKeyringError("no backend")

    def get_password(self, service, username):
        raise keyring.errors.NoKeyringError("no backend")

    def delete_password(self, service, username):
        raise keyring.errors.NoKeyringError("no backend")


@pytest.fixture
def fake_keyring():
    previous = keyring.get_keyring()
    keyring.set_keyring(_InMemoryKeyring())
    try:
        yield
    finally:
        keyring.set_keyring(previous)


@pytest.fixture
def no_backend_keyring():
    previous = keyring.get_keyring()
    keyring.set_keyring(_NoBackendKeyring())
    try:
        yield
    finally:
        keyring.set_keyring(previous)


def test_round_trip(fake_keyring):
    credentials.store("simplefin", "https://user:pass@host/simplefin")
    assert credentials.load("simplefin") == "https://user:pass@host/simplefin"


def test_load_missing_returns_none(fake_keyring):
    assert credentials.load("nothing-here") is None


def test_delete_true_when_stored(fake_keyring):
    credentials.store("simplefin", "secret")
    assert credentials.delete("simplefin") is True
    assert credentials.load("simplefin") is None


def test_delete_false_when_nothing_stored(fake_keyring):
    assert credentials.delete("never-stored") is False


def test_namespaced_by_name(fake_keyring):
    credentials.store("amex", "secret-a")
    credentials.store("checking", "secret-b")
    assert credentials.load("amex") == "secret-a"
    assert credentials.load("checking") == "secret-b"
    assert credentials.delete("amex") is True
    # deleting one connection's credentials must not touch another's
    assert credentials.load("checking") == "secret-b"


def test_unavailable_when_keyring_not_installed(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", None)
    with pytest.raises(credentials.CredentialsUnavailable):
        credentials.load("simplefin")


def test_unavailable_when_no_backend(no_backend_keyring):
    with pytest.raises(credentials.CredentialsUnavailable):
        credentials.store("simplefin", "secret")
    with pytest.raises(credentials.CredentialsUnavailable):
        credentials.load("simplefin")
    with pytest.raises(credentials.CredentialsUnavailable):
        credentials.delete("simplefin")
