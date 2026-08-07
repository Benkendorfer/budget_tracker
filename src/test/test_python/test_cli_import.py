"""Tests for the CLI twins of the app's import/imports/unimport commands.

``cli.main`` is called with an argv list, exactly as the ``budget`` entry point does,
and the database is pointed at a temporary file through ``BUDGET_DB``.
"""

from budget_tracker import cli, formats, queries
from budget_tracker import rates as rates_module
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.importer import import_csv
from conftest import CSV, _setup
from helpers import learn_format


def _empty_db(tmp_path, monkeypatch):
    """A database with no transactions yet, for tests that need a clean account."""
    db_path = tmp_path / "t.db"
    engine = get_engine(db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


# ------------------------------------------------------------------------- unimport


def test_imports_command_lists_past_imports(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        import_id = queries.get_imports(session)[0].id

    assert cli.main(["imports"]) == 0
    out = capsys.readouterr().out
    assert f"[{import_id:>4}]" in out
    assert "in.csv" in out
    assert "3 txns" in out


def test_unimport_without_yes_only_previews(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        import_id = queries.get_imports(session)[0].id

    assert cli.main(["unimport", str(import_id)]) == 1
    out = capsys.readouterr().out
    assert "Would delete" in out and "3 transaction(s)" in out
    assert "--yes" in out
    with session_factory() as session:
        assert len(queries.get_transactions(session)) == 3  # untouched


def test_unimport_with_yes_deletes(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        import_id = queries.get_imports(session)[0].id

    assert cli.main(["unimport", str(import_id), "--yes"]) == 0
    out = capsys.readouterr().out
    assert f"Deleted import #{import_id}" in out
    assert "3 transaction(s) removed" in out
    with session_factory() as session:
        assert queries.get_transactions(session) == []


def test_unimport_reports_an_unknown_id(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["unimport", "999", "--yes"]) == 1
    assert "No import with id 999" in capsys.readouterr().out


# ------------------------------------------------------- the interactive file picker


def _picker_inbox(tmp_path, monkeypatch):
    """An inbox with a file at the top and one inside a folder."""
    inbox = tmp_path / "to_import"
    (inbox / "2026-08").mkdir(parents=True)
    (inbox / "top.csv").write_text(CSV, encoding="utf-8")
    (inbox / "2026-08" / "nested.csv").write_text(CSV, encoding="utf-8")
    monkeypatch.setattr("budget_tracker.cli.import_cmds.TO_IMPORT_DIR", inbox)
    return inbox


def _answers(monkeypatch, *replies):
    """Feed the picker a fixed sequence of typed answers."""
    typed = iter(replies)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(typed))


def test_the_picker_lists_folders_above_files(tmp_path, monkeypatch, capsys):
    _picker_inbox(tmp_path, monkeypatch)
    _answers(monkeypatch, "q")

    assert cli._select_csv_interactively() is None
    listed = capsys.readouterr().out
    assert "[1] 2026-08/  (1 CSV)" in listed
    assert "[2] top.csv" in listed


def test_the_picker_descends_into_a_folder_and_back_out(tmp_path, monkeypatch, capsys):
    """Sub-directories are ordinary numbered choices, so reaching a nested file needs no
    path typing — and '../' gets you back."""
    inbox = _picker_inbox(tmp_path, monkeypatch)
    # 1: into 2026-08/   1: back out via ../   2: top.csv
    _answers(monkeypatch, "1", "1", "2")

    chosen = cli._select_csv_interactively()
    assert chosen == inbox / "top.csv"
    listed = capsys.readouterr().out
    assert "[1] ../" in listed  # offered only once inside the folder
    assert "[2] nested.csv" in listed


def test_the_picker_returns_a_nested_file(tmp_path, monkeypatch):
    inbox = _picker_inbox(tmp_path, monkeypatch)
    _answers(monkeypatch, "1", "2")  # into the folder, then the file below "../"

    assert cli._select_csv_interactively() == inbox / "2026-08" / "nested.csv"


def test_the_picker_has_no_way_up_from_the_top(tmp_path, monkeypatch, capsys):
    """The only way up is the '../' entry, so withholding it at the root is what keeps
    the picker inside the inbox rather than loose in the filesystem."""
    _picker_inbox(tmp_path, monkeypatch)
    _answers(monkeypatch, "q")

    cli._select_csv_interactively()
    assert "../" not in capsys.readouterr().out


def test_a_mistyped_number_reprompts_rather_than_giving_up(tmp_path, monkeypatch, capsys):
    """Quitting the whole import over a slip means walking back down the tree again."""
    inbox = _picker_inbox(tmp_path, monkeypatch)
    _answers(monkeypatch, "9", "banana", "2")

    assert cli._select_csv_interactively() == inbox / "top.csv"
    assert capsys.readouterr().out.count("Invalid selection.") == 2


def test_an_empty_inbox_says_so_and_stops(tmp_path, monkeypatch, capsys):
    inbox = tmp_path / "to_import"
    inbox.mkdir()
    monkeypatch.setattr("budget_tracker.cli.import_cmds.TO_IMPORT_DIR", inbox)

    assert cli._select_csv_interactively() is None
    assert "Nothing to import in" in capsys.readouterr().out


def test_the_import_summary_names_the_database_it_actually_wrote(tmp_path, monkeypatch, capsys):
    """It used to print DEFAULT_DB_PATH unconditionally, so with BUDGET_DB set it named
    a file it had not touched. Saying you wrote to one database while writing to another
    is worse than saying nothing at all."""
    _setup(tmp_path, monkeypatch)
    csv_path = tmp_path / "again.csv"
    csv_path.write_text(CSV, encoding="utf-8")

    assert cli.main(["import", str(csv_path)]) == 0
    printed = capsys.readouterr().out
    assert f"Database: {tmp_path / 't.db'}" in printed
    assert "data/budget.db" not in printed


def test_import_command_uses_the_formats_currency_with_no_currency_flag(
    tmp_path, monkeypatch, capsys
):
    """The bug this fixes: ``--currency`` used to default to USD and was passed on
    every import regardless, silently overriding whatever currency the format itself
    was in."""
    session_factory = _empty_db(tmp_path, monkeypatch)
    csv_path = tmp_path / "swiss.csv"
    csv_path.write_text(CSV, encoding="utf-8")
    with session_factory() as session:
        spec = learn_format(session, csv_path, name="swiss")
        formats.save_format(
            session, formats.FormatSpec(**{**formats.to_dict(spec), "currency": "CHF"})
        )
        session.commit()

    assert cli.main(["import", str(csv_path)]) == 0
    with session_factory() as session:
        assert {a.currency for a in queries.get_accounts(session)} == {"CHF"}


def test_import_command_currency_flag_still_overrides_the_format(
    tmp_path, monkeypatch, capsys
):
    session_factory = _empty_db(tmp_path, monkeypatch)
    csv_path = tmp_path / "swiss.csv"
    csv_path.write_text(CSV, encoding="utf-8")
    with session_factory() as session:
        spec = learn_format(session, csv_path, name="swiss")
        formats.save_format(
            session, formats.FormatSpec(**{**formats.to_dict(spec), "currency": "CHF"})
        )
        session.commit()

    assert cli.main(["import", str(csv_path), "--currency", "EUR"]) == 0
    with session_factory() as session:
        assert {a.currency for a in queries.get_accounts(session)} == {"EUR"}


def test_import_command_fetches_rates_for_a_foreign_currency_import(
    tmp_path, monkeypatch, capsys
):
    """The bug this fixes: a CHF import into a database with no CHF rates used to leave
    every money figure a silent zero until someone thought to run 'rates fetch' by
    hand. An import now fetches what it needs on its own."""
    session_factory = _empty_db(tmp_path, monkeypatch)
    csv_path = tmp_path / "swiss.csv"
    csv_path.write_text(CSV, encoding="utf-8")
    with session_factory() as session:
        spec = learn_format(session, csv_path, name="swiss")
        formats.save_format(
            session, formats.FormatSpec(**{**formats.to_dict(spec), "currency": "CHF"})
        )
        session.commit()

    calls = []

    def stub(session, start, end, base, quotes, **kwargs):
        calls.append((base, tuple(quotes)))
        return 3

    monkeypatch.setattr(rates_module, "fetch_ecb_rates", stub)

    assert cli.main(["import", str(csv_path)]) == 0
    out = capsys.readouterr().out
    assert calls == [(queries.HOME_CURRENCY, ("CHF",))]
    assert f"Fetched 3 rate(s) for {queries.HOME_CURRENCY} -> CHF." in out


def test_import_command_does_not_fetch_rates_for_a_home_currency_import(
    tmp_path, monkeypatch, capsys
):
    """Only when a foreign currency is actually present -- a plain USD import asks
    fetch_ecb_rates for nothing at all."""
    session_factory = _empty_db(tmp_path, monkeypatch)
    csv_path = tmp_path / "in.csv"
    csv_path.write_text(CSV, encoding="utf-8")
    with session_factory() as session:
        learn_format(session, csv_path)
        session.commit()

    calls = []
    monkeypatch.setattr(
        rates_module, "fetch_ecb_rates", lambda *a, **kw: calls.append(1) or 0
    )

    assert cli.main(["import", str(csv_path)]) == 0
    out = capsys.readouterr().out
    assert calls == []
    assert "Fetched" not in out


def test_import_command_reports_a_rate_fetch_failure_without_failing_the_import(
    tmp_path, monkeypatch, capsys
):
    """Offline must mean 'imported N; could not reach the rate service', never a failed
    or rolled-back import."""
    session_factory = _empty_db(tmp_path, monkeypatch)
    csv_path = tmp_path / "swiss.csv"
    csv_path.write_text(CSV, encoding="utf-8")
    with session_factory() as session:
        spec = learn_format(session, csv_path, name="swiss")
        formats.save_format(
            session, formats.FormatSpec(**{**formats.to_dict(spec), "currency": "CHF"})
        )
        session.commit()

    def failing(session, start, end, base, quotes, **kwargs):
        raise rates_module.FrankfurterError("Could not reach Frankfurter at ...")

    monkeypatch.setattr(rates_module, "fetch_ecb_rates", failing)

    assert cli.main(["import", str(csv_path)]) == 0  # the import itself still succeeds
    out = capsys.readouterr().out
    assert "3 added" in out
    assert "Could not fetch" in out and "Could not reach Frankfurter" in out
    assert "rates fetch" in out
    with session_factory() as session:
        assert {a.currency for a in queries.get_accounts(session)} == {"CHF"}


def test_import_command_reports_a_currency_mismatch(tmp_path, monkeypatch, capsys):
    """Caught alongside AccountRequired/UnknownFormat: a clear message and exit 1,
    not a traceback, and nothing from the second file is written."""
    session_factory = _empty_db(tmp_path, monkeypatch)
    csv_path = tmp_path / "in.csv"
    csv_path.write_text(CSV, encoding="utf-8")
    with session_factory() as session:
        learn_format(session, csv_path, name="cards")
        import_csv(session, csv_path)  # USD, the format's default

    other_path = tmp_path / "other.csv"
    other_path.write_text(
        "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\n"
        "2025-07-05,2025-07-06,8207,GROCERIES,Food,10.00,\n",
        encoding="utf-8",
    )

    assert cli.main(["import", str(other_path), "--currency", "CHF"]) == 1
    printed = capsys.readouterr().out
    assert "8207" in printed and "USD" in printed and "CHF" in printed
    with session_factory() as session:
        assert len(queries.get_transactions(session)) == 3  # only the first file's rows
