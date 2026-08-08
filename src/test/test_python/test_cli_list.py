"""Tests for the CLI twin of the app's ``list`` command.

``cli.main`` is called with an argv list, exactly as the ``budget`` entry point does,
and the database is pointed at a temporary file through ``BUDGET_DB``.
"""

from sqlalchemy import select

from budget_tracker import categories, cli, tags
from budget_tracker.models import Transaction
from conftest import _setup


def _txn_ids(session_factory, description=None):
    with session_factory() as session:
        query = select(Transaction.id)
        if description is not None:
            query = query.where(Transaction.description == description)
        return list(session.scalars(query))


def test_list_dash_dash_category_resolves_a_full_path(tmp_path, monkeypatch, capsys):
    """``--category`` is routed through categories.resolve_path, so a nested category
    is reachable by its full path, not just a bare top-level name."""
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        categories.ensure_path(session, "Food > Dining", confirm_relocation=True)
        session.commit()

    assert cli.main(["list", "--category", "Food > Dining"]) == 0
    out = capsys.readouterr().out
    assert "3 txns" in out


def test_list_dash_dash_category_reports_an_unknown_name(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["list", "--category", "Nope"]) == 1
    assert "No category named 'Nope'" in capsys.readouterr().out


def test_list_dash_dash_tag_filters_to_the_tagged_rows(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    ids = _txn_ids(session_factory, "COFFEE SHOP A")  # two of the three rows
    with session_factory() as session:
        tags.add_tag(session, ids, "reimbursable")
        session.commit()

    assert cli.main(["list", "--tag", "reimbursable"]) == 0
    out = capsys.readouterr().out
    assert "2 txns" in out
    assert "COFFEE SHOP B" not in out


def test_list_dash_dash_tag_reports_an_unknown_name(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["list", "--tag", "nope"]) == 1
    assert "No tag named 'nope'" in capsys.readouterr().out


def test_list_dash_dash_trip_filters_to_the_trip(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    ids = _txn_ids(session_factory, "COFFEE SHOP B")  # one row
    with session_factory() as session:
        tags.set_trip(session, ids, "Japan 2026")
        session.commit()

    assert cli.main(["list", "--trip", "Japan 2026"]) == 0
    out = capsys.readouterr().out
    assert "1 txns" in out
    assert "COFFEE SHOP B" in out


def test_list_dash_dash_trip_reports_an_unknown_name(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["list", "--trip", "nope"]) == 1
    assert "No trip named 'nope'" in capsys.readouterr().out


def test_list_tag_and_trip_combine_with_and(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    a_ids = _txn_ids(session_factory, "COFFEE SHOP A")
    b_ids = _txn_ids(session_factory, "COFFEE SHOP B")
    with session_factory() as session:
        tags.add_tag(session, a_ids + b_ids, "reimbursable")
        tags.set_trip(session, a_ids, "Japan 2026")
        session.commit()

    assert cli.main(["list", "--tag", "reimbursable", "--trip", "Japan 2026"]) == 0
    out = capsys.readouterr().out
    assert "2 txns" in out
    assert "COFFEE SHOP B" not in out
