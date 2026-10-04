"""Store a sync connection's SimpleFIN access URL in the OS keychain.

The access URL embeds a long-lived credential to the user's bank transactions
(``https://user:pass@host/simplefin``). It belongs nowhere this repo or its
(gitignored) database could leak it, so it never touches a file or a table —
only the OS keychain, via the ``keyring`` package. Import ``keyring`` lazily
inside each function so the rest of the app still imports if it is not
installed or has no usable backend.
"""

from __future__ import annotations

from typing import Optional

SERVICE = "budget-tracker"


class CredentialsUnavailable(RuntimeError):
    """No keyring backend is usable (package missing, or no OS keychain)."""


def _key(name: str) -> str:
    return f"sync:{name}"


def _keyring():
    try:
        import keyring
    except ImportError as exc:
        raise CredentialsUnavailable(
            "the 'keyring' package is not installed; run `pip install keyring` "
            "to store sync credentials"
        ) from exc
    return keyring


def store(name: str, secret: str) -> None:
    keyring = _keyring()
    try:
        keyring.set_password(SERVICE, _key(name), secret)
    except keyring.errors.NoKeyringError as exc:
        raise CredentialsUnavailable(
            "no OS keychain backend is available to store sync credentials"
        ) from exc


def load(name: str) -> Optional[str]:
    keyring = _keyring()
    try:
        return keyring.get_password(SERVICE, _key(name))
    except keyring.errors.NoKeyringError as exc:
        raise CredentialsUnavailable(
            "no OS keychain backend is available to load sync credentials"
        ) from exc


def delete(name: str) -> bool:
    keyring = _keyring()
    try:
        keyring.delete_password(SERVICE, _key(name))
        return True
    except keyring.errors.PasswordDeleteError:
        return False
    except keyring.errors.NoKeyringError as exc:
        raise CredentialsUnavailable(
            "no OS keychain backend is available to delete sync credentials"
        ) from exc
