"""Tests for the SimpleFIN protocol client.

No real network: every test replaces ``urllib.request.urlopen`` with a fake that
returns canned responses and records the request it was given, so assertions can check
the URL/query/headers actually sent as well as the parsed result. The autouse
``_no_network`` fixture in conftest.py already makes the real function raise, which is
exactly what a test forgetting to stub it would hit.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
from datetime import date, timedelta

import pytest

from budget_tracker import simplefin


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _json_response(payload: dict, status: int = 200) -> _FakeResponse:
    return _FakeResponse(json.dumps(payload).encode("utf-8"), status=status)


def _install(monkeypatch, fn):
    """Replace urlopen with ``fn(request, timeout=...)`` and return the call log."""
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        return fn(request, timeout=timeout)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls


def _query(request) -> dict:
    parsed = urllib.parse.urlsplit(request.full_url)
    return urllib.parse.parse_qs(parsed.query)


# --------------------------------------------------------------------- decode_setup_token


def test_decode_setup_token_round_trips_a_claim_url():
    token = base64.b64encode(b"https://bridge.example/claim/abc123").decode("ascii")
    assert simplefin.decode_setup_token(token) == "https://bridge.example/claim/abc123"


def test_decode_setup_token_tolerates_whitespace_and_missing_padding():
    raw = base64.b64encode(b"https://bridge.example/claim/abc123").decode("ascii")
    stripped = raw.rstrip("=")  # drop padding, as a pasted token sometimes loses it
    token = f"  {stripped[:4]}\n{stripped[4:]}  "
    assert simplefin.decode_setup_token(token) == "https://bridge.example/claim/abc123"


def test_decode_setup_token_rejects_invalid_base64():
    with pytest.raises(simplefin.InvalidSetupToken):
        simplefin.decode_setup_token("not-base64!!! ***")


def test_decode_setup_token_rejects_non_url_payload():
    token = base64.b64encode(b"just some text, not a url").decode("ascii")
    with pytest.raises(simplefin.InvalidSetupToken):
        simplefin.decode_setup_token(token)


def test_decode_setup_token_rejects_non_http_scheme():
    token = base64.b64encode(b"ftp://bridge.example/claim/abc123").decode("ascii")
    with pytest.raises(simplefin.InvalidSetupToken):
        simplefin.decode_setup_token(token)


# -------------------------------------------------------------------------------- claim


def test_claim_posts_the_decoded_url_with_an_empty_body_and_returns_the_access_url(monkeypatch):
    claim_url = "https://bridge.example/claim/abc123"
    token = base64.b64encode(claim_url.encode("ascii")).decode("ascii")
    access_url = "https://user:pass@bridge.example/simplefin"
    calls = _install(monkeypatch, lambda req, timeout: _FakeResponse(access_url.encode("ascii")))

    result = simplefin.claim(token)

    assert result == access_url
    assert len(calls) == 1
    assert calls[0].full_url == claim_url
    assert calls[0].get_method() == "POST"
    assert calls[0].data == b""
    # Without its own agent, urllib's default is refused by the Cloudflare firewall in
    # front of SimpleFIN Bridge with a 403 that reads exactly like a used token.
    assert calls[0].get_header("User-agent") == simplefin.USER_AGENT


def test_claim_403_quotes_the_servers_reply(monkeypatch):
    import io

    claim_url = "https://bridge.example/claim/abc123"
    token = base64.b64encode(claim_url.encode("ascii")).decode("ascii")

    def blocked(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url, 403, "Forbidden", None, io.BytesIO(b"error code: 1010\n")
        )

    _install(monkeypatch, blocked)

    with pytest.raises(simplefin.ClaimFailed) as excinfo:
        simplefin.claim(token)
    assert "error code: 1010" in str(excinfo.value)
    assert "abc123" not in str(excinfo.value)


def test_claim_403_raises_claim_failed_without_leaking_the_claim_url_creds(monkeypatch):
    claim_url = "https://tok:secretpart@bridge.example/claim/abc123"
    token = base64.b64encode(claim_url.encode("ascii")).decode("ascii")

    def fail(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", None, None)

    _install(monkeypatch, fail)

    with pytest.raises(simplefin.ClaimFailed) as excinfo:
        simplefin.claim(token)
    assert "secretpart" not in str(excinfo.value)


def test_claim_wraps_transport_failures(monkeypatch):
    claim_url = "https://bridge.example/claim/abc123"
    token = base64.b64encode(claim_url.encode("ascii")).decode("ascii")

    def fail(req, timeout):
        raise OSError("name resolution failed")

    _install(monkeypatch, fail)

    with pytest.raises(simplefin.SimpleFINError) as excinfo:
        simplefin.claim(token)
    # The claim never happened, so the token in the URL's path is still live: the
    # message may name the host, never the path that carries it.
    assert "abc123" not in str(excinfo.value)
    assert "bridge.example" in str(excinfo.value)


def test_claim_rejects_non_200_without_error(monkeypatch):
    claim_url = "https://bridge.example/claim/abc123"
    token = base64.b64encode(claim_url.encode("ascii")).decode("ascii")
    _install(monkeypatch, lambda req, timeout: _FakeResponse(b"", status=202))

    with pytest.raises(simplefin.ClaimFailed):
        simplefin.claim(token)


# ---------------------------------------------------------------------- fetch_accounts


ACCESS_URL = "https://alice:topsecret@bridge.example/simplefin/access/xyz"


def test_fetch_accounts_sends_basic_auth_and_version_but_never_pending(monkeypatch):
    calls = _install(monkeypatch, lambda req, timeout: _json_response({"accounts": []}))

    simplefin.fetch_accounts(ACCESS_URL)

    assert len(calls) == 1
    request = calls[0]
    expected_token = base64.b64encode(b"alice:topsecret").decode("ascii")
    assert request.get_header("Authorization") == f"Basic {expected_token}"
    assert request.get_header("User-agent") == simplefin.USER_AGENT
    query = _query(request)
    assert query["version"] == ["2"]
    assert "pending" not in query
    assert "start-date" not in query
    assert "end-date" not in query
    parsed = urllib.parse.urlsplit(request.full_url)
    assert "topsecret" not in parsed.geturl()
    assert parsed.path == "/simplefin/access/xyz/accounts"


def test_fetch_accounts_converts_start_and_end_to_midnight_utc_epoch(monkeypatch):
    calls = _install(monkeypatch, lambda req, timeout: _json_response({"accounts": []}))

    simplefin.fetch_accounts(ACCESS_URL, start=date(2026, 7, 1), end=date(2026, 7, 10))

    query = _query(calls[0])
    assert query["start-date"] == [str(simplefin._to_epoch(date(2026, 7, 1)))]
    assert query["end-date"] == [str(simplefin._to_epoch(date(2026, 7, 10)))]


def test_fetch_accounts_repeats_the_account_param(monkeypatch):
    calls = _install(monkeypatch, lambda req, timeout: _json_response({"accounts": []}))

    simplefin.fetch_accounts(ACCESS_URL, account_ids=["a1", "a2"])

    query = _query(calls[0])
    assert query["account"] == ["a1", "a2"]


def test_fetch_accounts_parses_accounts_errors_and_connections(monkeypatch):
    payload = {
        "errlist": [{"code": "bad-conn", "msg": "connection down", "conn_id": "c1"}],
        "connections": [{"conn_id": "c1", "name": "My Bank"}],
        "accounts": [
            {
                "id": "acc1",
                "name": "Checking",
                "conn_id": "c1",
                "currency": "USD",
                "balance": "123.45",
                "balance-date": simplefin._to_epoch(date(2026, 8, 1)),
                "transactions": [
                    {
                        "id": "t1",
                        "posted": simplefin._to_epoch(date(2026, 7, 15)),
                        "amount": "-12.34",
                        "description": "COFFEE SHOP",
                        "payee": "Coffee Shop",
                        "memo": "note",
                        "transacted_at": simplefin._to_epoch(date(2026, 7, 14)),
                        "pending": False,
                    },
                    {
                        # No transacted_at, and posted 0 -- a pending row.
                        "id": "t2",
                        "posted": 0,
                        "amount": "-5.00",
                        "description": "PENDING CHARGE",
                        "pending": True,
                    },
                ],
            }
        ],
    }
    _install(monkeypatch, lambda req, timeout: _json_response(payload))

    result = simplefin.fetch_accounts(ACCESS_URL)

    assert len(result.errors) == 1
    error = result.errors[0]
    assert (error.code, error.msg, error.conn_id) == ("bad-conn", "connection down", "c1")

    assert len(result.accounts) == 1
    account = result.accounts[0]
    assert account.id == "acc1"
    assert account.connection_name == "My Bank"
    assert account.balance == simplefin.Decimal("123.45")
    assert account.balance_date == date(2026, 8, 1)

    posted_txn, pending_txn = account.transactions
    assert posted_txn.posted == date(2026, 7, 15)
    assert posted_txn.transacted == date(2026, 7, 14)
    assert posted_txn.amount == simplefin.Decimal("-12.34")
    assert posted_txn.payee == "Coffee Shop"
    assert posted_txn.memo == "note"
    assert posted_txn.pending is False

    assert pending_txn.posted == date(1970, 1, 1)  # epoch 0, since posted is 0
    assert pending_txn.transacted is None  # missing transacted_at
    assert pending_txn.pending is True


def test_fetch_accounts_falls_back_to_legacy_errors(monkeypatch):
    payload = {"errors": ["something went wrong"], "accounts": []}
    _install(monkeypatch, lambda req, timeout: _json_response(payload))

    result = simplefin.fetch_accounts(ACCESS_URL)

    assert result.errors == (simplefin.RemoteError(code="legacy", msg="something went wrong"),)


def test_fetch_accounts_errlist_present_but_empty_is_not_replaced_by_legacy(monkeypatch):
    payload = {"errlist": [], "errors": ["should be ignored"], "accounts": []}
    _install(monkeypatch, lambda req, timeout: _json_response(payload))

    result = simplefin.fetch_accounts(ACCESS_URL)

    assert result.errors == ()


def test_fetch_accounts_keeps_a_url_currency_as_is(monkeypatch):
    payload = {
        "accounts": [
            {
                "id": "acc1",
                "name": "Weird Account",
                "conn_id": "c1",
                "currency": "https://example.org/currency/xyz",
                "balance": "0",
            }
        ]
    }
    _install(monkeypatch, lambda req, timeout: _json_response(payload))

    result = simplefin.fetch_accounts(ACCESS_URL)

    assert result.accounts[0].currency == "https://example.org/currency/xyz"


def test_fetch_accounts_401_raises_auth_failed_without_leaking_credentials(monkeypatch):
    def fail(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", None, None)

    _install(monkeypatch, fail)

    with pytest.raises(simplefin.AuthFailed) as excinfo:
        simplefin.fetch_accounts(ACCESS_URL)
    assert "topsecret" not in str(excinfo.value)


def test_fetch_accounts_403_also_raises_auth_failed(monkeypatch):
    def fail(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", None, None)

    _install(monkeypatch, fail)

    with pytest.raises(simplefin.AuthFailed):
        simplefin.fetch_accounts(ACCESS_URL)


def test_fetch_accounts_other_http_errors_raise_plain_simplefin_error(monkeypatch):
    def fail(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 500, "Server Error", None, None)

    _install(monkeypatch, fail)

    with pytest.raises(simplefin.SimpleFINError) as excinfo:
        simplefin.fetch_accounts(ACCESS_URL)
    assert not isinstance(excinfo.value, simplefin.AuthFailed)


def test_fetch_accounts_malformed_json_raises_simplefin_error(monkeypatch):
    _install(monkeypatch, lambda req, timeout: _FakeResponse(b"not json at all"))

    with pytest.raises(simplefin.SimpleFINError):
        simplefin.fetch_accounts(ACCESS_URL)


def test_fetch_accounts_transport_failure_is_wrapped_and_redacted(monkeypatch):
    def fail(req, timeout):
        raise OSError("connection refused")

    _install(monkeypatch, fail)

    with pytest.raises(simplefin.SimpleFINError) as excinfo:
        simplefin.fetch_accounts(ACCESS_URL)
    assert "topsecret" not in str(excinfo.value)


# ------------------------------------------------------------- window splitting / merging


def test_fetch_accounts_does_not_split_a_window_of_exactly_the_request_limit(monkeypatch):
    calls = _install(monkeypatch, lambda req, timeout: _json_response({"accounts": []}))
    start = date(2026, 1, 1)

    simplefin.fetch_accounts(
        ACCESS_URL, start=start, end=start + timedelta(days=simplefin.REQUEST_WINDOW_DAYS)
    )

    assert len(calls) == 1


def test_fetch_accounts_splits_a_window_over_the_limit_into_consecutive_requests(monkeypatch):
    start = date(2026, 1, 1)
    end = start + timedelta(days=simplefin.REQUEST_WINDOW_DAYS + 1)

    def respond(req, timeout):
        query = _query(req)
        return _json_response(
            {
                "accounts": [
                    {
                        "id": "acc1",
                        "name": "Checking",
                        "conn_id": "c1",
                        "currency": "USD",
                        "balance": query["end-date"][0],  # distinguishes each window
                        "transactions": [],
                    }
                ]
            }
        )

    calls = _install(monkeypatch, respond)

    result = simplefin.fetch_accounts(ACCESS_URL, start=start, end=end)

    assert len(calls) == 2
    first_query = _query(calls[0])
    second_query = _query(calls[1])
    # Consecutive, non-overlapping: first ends exactly where the second starts.
    assert first_query["end-date"] == second_query["start-date"]
    assert first_query["start-date"] == [str(simplefin._to_epoch(start))]
    assert second_query["end-date"] == [str(simplefin._to_epoch(end))]
    # The merged balance is the one from the later (second) window.
    assert result.accounts[0].balance == simplefin.Decimal(second_query["end-date"][0])


def test_fetch_accounts_merges_transactions_across_windows_deduping_by_id(monkeypatch):
    start = date(2026, 1, 1)
    end = start + timedelta(days=simplefin.REQUEST_WINDOW_DAYS + 5)
    windows = []

    def respond(req, timeout):
        query = _query(req)
        windows.append(query["start-date"][0])
        # Every window reports the same transaction id, plus the first window reports
        # a second, distinct one -- exercising both the dedup and the concatenation.
        txns = [
            {
                "id": "shared",
                "posted": simplefin._to_epoch(date(2026, 2, 1)),
                "amount": "-1.00",
                "description": "SHARED",
            }
        ]
        if len(windows) == 1:
            txns.append(
                {
                    "id": "only-first",
                    "posted": simplefin._to_epoch(date(2026, 1, 5)),
                    "amount": "-2.00",
                    "description": "ONLY FIRST",
                }
            )
        return _json_response(
            {
                "accounts": [
                    {
                        "id": "acc1",
                        "name": "Checking",
                        "conn_id": "c1",
                        "currency": "USD",
                        "balance": "10.00",
                        "transactions": txns,
                    }
                ]
            }
        )

    _install(monkeypatch, respond)

    result = simplefin.fetch_accounts(ACCESS_URL, start=start, end=end)

    assert len(windows) == 2
    txn_ids = sorted(t.id for t in result.accounts[0].transactions)
    assert txn_ids == ["only-first", "shared"]  # "shared" appears once, not twice


def test_fetch_accounts_merges_errors_from_every_window(monkeypatch):
    start = date(2026, 1, 1)
    end = start + timedelta(days=simplefin.REQUEST_WINDOW_DAYS + 5)
    seen = []

    def respond(req, timeout):
        seen.append(1)
        return _json_response(
            {
                "errlist": [{"code": "e", "msg": f"window {len(seen)}"}],
                "accounts": [],
            }
        )

    _install(monkeypatch, respond)

    result = simplefin.fetch_accounts(ACCESS_URL, start=start, end=end)

    assert [e.msg for e in result.errors] == ["window 1", "window 2"]


def test_fetch_accounts_with_no_dates_makes_a_single_unsplit_request(monkeypatch):
    calls = _install(monkeypatch, lambda req, timeout: _json_response({"accounts": []}))

    simplefin.fetch_accounts(ACCESS_URL)

    assert len(calls) == 1


# --------------------------------------------------------------------------------- redact


def test_redact_strips_userinfo_but_keeps_the_rest():
    assert simplefin.redact("https://user:pass@host.example/path?a=1") == (
        "https://host.example/path?a=1"
    )


def test_redact_is_a_no_op_without_userinfo():
    assert simplefin.redact("https://host.example/path") == "https://host.example/path"


def test_redact_keeps_the_port():
    assert simplefin.redact("https://user:pass@host.example:8443/path") == (
        "https://host.example:8443/path"
    )


def test_redact_does_not_choke_on_garbage():
    assert simplefin.redact("not a url") == "not a url"


def test_a_split_request_reports_a_repeated_error_once(monkeypatch):
    """A connection-level error comes back on every window of a split request; the
    user saw "con.auth ... Auth required" printed twice."""
    error = {"code": "con.auth", "msg": "Auth required", "conn_id": "MBR-1"}
    calls = _install(
        monkeypatch, lambda req, timeout: _json_response({"accounts": [], "errlist": [error]})
    )
    start = date(2026, 1, 1)
    result = simplefin.fetch_accounts(
        ACCESS_URL, start=start, end=start + timedelta(days=simplefin.REQUEST_WINDOW_DAYS + 5)
    )
    assert len(calls) == 2
    assert [e.msg for e in result.errors] == ["Auth required"]
