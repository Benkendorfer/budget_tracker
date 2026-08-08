"""Tests for the CLI twin of the tags/trips feature.

``cli.main`` is called with an argv list, exactly as the ``budget`` entry point does,
and the database is pointed at a temporary file through ``BUDGET_DB``. The CLI has no
way to select individual transactions, so every test here tags the fixture rows through
the core :mod:`budget_tracker.tags` module directly, then exercises the CLI on top of
that -- exactly the situation the real app leaves the CLI in after a TUI multi-select.
"""

from sqlalchemy import select

from budget_tracker import cli, tags
from budget_tracker.models import Transaction
from conftest import _setup


def _txn_ids(session_factory, description=None):
    with session_factory() as session:
        query = select(Transaction.id)
        if description is not None:
            query = query.where(Transaction.description == description)
        return list(session.scalars(query))


def _tag_fixture(session_factory, ids, name, kind=tags.TAG):
    with session_factory() as session:
        if kind == tags.TRIP:
            tags.set_trip(session, ids, name)
        else:
            tags.add_tag(session, ids, name)
        session.commit()


# ------------------------------------------------------------------------------- list


def test_tags_list_shows_name_kind_count_and_total(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    ids = _txn_ids(session_factory, "COFFEE SHOP A")  # two rows, -300 and -350
    _tag_fixture(session_factory, ids, "reimbursable")

    assert cli.main(["tags"]) == 0  # bare command defaults to "list"
    out = capsys.readouterr().out
    assert "reimbursable" in out
    assert "tag" in out
    assert "2 txns" in out
    assert "-6.50" in out


def test_tags_list_shows_a_tag_with_no_transactions(tmp_path, monkeypatch, capsys):
    """An empty tag/trip still appears -- there is no other way to make it selectable."""
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        tags.get_or_create(session, "Japan 2026", tags.TRIP)
        session.commit()

    assert cli.main(["tags"]) == 0
    out = capsys.readouterr().out
    assert "Japan 2026" in out
    assert "0 txns" in out


def test_tags_list_filters_by_kind(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    ids = _txn_ids(session_factory)
    _tag_fixture(session_factory, [ids[0]], "reimbursable")
    _tag_fixture(session_factory, [ids[1]], "Japan 2026", tags.TRIP)

    assert cli.main(["tags", "list", "--kind", "trip"]) == 0
    out = capsys.readouterr().out
    assert "Japan 2026" in out
    assert "reimbursable" not in out

    assert cli.main(["tags", "list", "--kind", "tag"]) == 0
    out = capsys.readouterr().out
    assert "reimbursable" in out
    assert "Japan 2026" not in out


def test_tags_list_with_no_tags(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["tags"]) == 0
    assert "No tags yet." in capsys.readouterr().out


# ----------------------------------------------------------------------------- rename


def test_tags_rename(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    ids = _txn_ids(session_factory, "COFFEE SHOP A")
    _tag_fixture(session_factory, ids, "reimbursable")

    assert cli.main(["tags", "rename", "reimbursable", "work expense"]) == 0
    assert "Renamed 'reimbursable' -> 'work expense'." in capsys.readouterr().out

    with session_factory() as session:
        assert tags.resolve(session, "reimbursable") is None
        assert tags.resolve(session, "work expense") is not None


def test_tags_rename_a_trip_needs_the_kind_flag(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    ids = _txn_ids(session_factory, "COFFEE SHOP A")
    _tag_fixture(session_factory, ids, "Japan 2026", tags.TRIP)

    # Without --kind, rename defaults to TAG and cannot see the trip.
    assert cli.main(["tags", "rename", "Japan 2026", "Italy 2027"]) == 1
    assert "No tag named 'Japan 2026'" in capsys.readouterr().out

    assert cli.main(
        ["tags", "rename", "Japan 2026", "Italy 2027", "--kind", "trip"]
    ) == 0
    assert "Renamed 'Japan 2026' -> 'Italy 2027'." in capsys.readouterr().out
    with session_factory() as session:
        assert tags.resolve(session, "Japan 2026", tags.TRIP) is None
        assert tags.resolve(session, "Italy 2027", tags.TRIP) is not None


def test_tags_rename_reports_an_unknown_name(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["tags", "rename", "nope", "still nope"]) == 1
    assert "No tag named 'nope'." in capsys.readouterr().out


# ----------------------------------------------------------------------------- delete


def test_tags_delete_without_yes_only_previews(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    ids = _txn_ids(session_factory, "COFFEE SHOP A")
    _tag_fixture(session_factory, ids, "reimbursable")

    assert cli.main(["tags", "delete", "reimbursable"]) == 1
    out = capsys.readouterr().out
    assert "Would remove 'reimbursable' from 2 transaction(s)" in out
    assert "Re-run with --yes" in out

    with session_factory() as session:
        # Nothing actually changed: the trial run inside the preview was rolled back.
        assert tags.resolve(session, "reimbursable") is not None
        by_txn = tags.tags_for(session, ids)
        assert all(t.name == "reimbursable" for tag_list in by_txn.values() for t in tag_list)


def test_tags_delete_confirmed_removes_it_everywhere(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    ids = _txn_ids(session_factory, "COFFEE SHOP A")
    _tag_fixture(session_factory, ids, "reimbursable")

    assert cli.main(["tags", "delete", "reimbursable", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Deleted 'reimbursable'; removed from 2 transaction(s)." in out

    with session_factory() as session:
        assert tags.resolve(session, "reimbursable") is None
        by_txn = tags.tags_for(session, ids)
        assert by_txn == {ids[0]: [], ids[1]: []}


def test_tags_delete_reports_an_unknown_name(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["tags", "delete", "nope", "--yes"]) == 1
    assert "No tag named 'nope'." in capsys.readouterr().out
