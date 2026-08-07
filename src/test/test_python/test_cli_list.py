"""Tests for the CLI twin of the app's ``list`` command.

``cli.main`` is called with an argv list, exactly as the ``budget`` entry point does,
and the database is pointed at a temporary file through ``BUDGET_DB``.
"""

from budget_tracker import categories, cli
from conftest import _setup


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
