"""Tests for the CLI twin of the app's ``format`` command.

``cli.main`` is called with an argv list, exactly as the ``budget`` entry point does,
and the database is pointed at a temporary file through ``BUDGET_DB``.
"""

from budget_tracker import cli, formats
from conftest import _setup

# --------------------------------------------------------------------------- format


def test_format_list_shows_polarity(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["format"]) == 0
    out = capsys.readouterr().out
    assert "test_layout" in out and "debit_credit" in out


def test_format_invert_flips_and_is_reflected_in_list(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)

    assert cli.main(["format", "invert", "test_layout", "on"]) == 0
    assert "invert on" in capsys.readouterr().out
    with session_factory() as session:
        assert formats.get_format(session, "test_layout").invert_amount is True

    assert cli.main(["format", "invert", "test_layout", "off"]) == 0
    assert "invert off" in capsys.readouterr().out
    with session_factory() as session:
        assert formats.get_format(session, "test_layout").invert_amount is False


def test_format_invert_reports_an_unknown_format(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["format", "invert", "nope", "on"]) == 1
    assert "Unknown format" in capsys.readouterr().out
