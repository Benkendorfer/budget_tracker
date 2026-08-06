"""Tests for the CLI twins of the app's categorisation commands.

``cli.main`` is called with an argv list, exactly as the ``budget`` entry point does,
and the database is pointed at a temporary file through ``BUDGET_DB``.
"""

from budget_tracker import categories, cli, formats, queries
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.importer import import_csv
from helpers import learn_format

CSV = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2025-07-01,2025-07-02,8207,COFFEE SHOP A,Dining,3.00,
2025-07-01,2025-07-02,8207,COFFEE SHOP B,Dining,4.00,
2025-07-03,2025-07-04,8207,COFFEE SHOP A,Dining,3.50,
"""


def _setup(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    engine = get_engine(db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    csv_path = tmp_path / "in.csv"
    csv_path.write_text(CSV, encoding="utf-8")
    with session_factory() as session:
        learn_format(session, csv_path)
        import_csv(session, csv_path)
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def _categories_of(session_factory):
    with session_factory() as session:
        from sqlalchemy import text

        return sorted(
            row[0] or ""
            for row in session.execute(
                text(
                    "select c.value from transactions t "
                    "left join category c on c.id = t.category_id"
                )
            )
        )


def test_categorize_sets_a_category(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)

    assert cli.main(["categorize", "COFFEE SHOP A", "Coffee"]) == 0
    assert "Categorised 2 transaction(s)" in capsys.readouterr().out
    assert _categories_of(session_factory) == ["Coffee", "Coffee", "Dining"]


def test_categorize_clear_undoes_it(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)

    cli.main(["categorize", "COFFEE SHOP A", "Coffee"])
    capsys.readouterr()
    assert cli.main(["categorize", "COFFEE SHOP A", "--clear"]) == 0
    assert "Cleared the category on 2 transaction(s)" in capsys.readouterr().out
    # The bank's own category on the other vendor survives; only manual rows go.
    assert _categories_of(session_factory) == ["", "", "Dining"]


def test_categorize_reports_an_unknown_vendor(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["categorize", "NOT A VENDOR", "Coffee"]) == 1
    assert "No vendor named 'NOT A VENDOR'" in capsys.readouterr().out


def test_categorize_without_a_category_is_refused(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["categorize", "COFFEE SHOP A"]) == 1
    assert "A category is required" in capsys.readouterr().out


def test_category_rule_add_list_remove(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)

    assert cli.main(["category-rule", "add", "COFFEE*", "Coffee"]) == 0
    assert "3 transactions updated" in capsys.readouterr().out
    assert _categories_of(session_factory) == ["Coffee"] * 3

    assert cli.main(["category-rule", "list"]) == 0
    assert "COFFEE*  ->  Coffee" in capsys.readouterr().out

    assert cli.main(["category-rule", "remove", "COFFEE*"]) == 0
    assert "Removed 'COFFEE*'" in capsys.readouterr().out
    # Removing a rule reverts the rows it owned, as apply_category_rules promises.
    assert _categories_of(session_factory) == ["", "", ""]


def test_category_rule_list_is_the_default(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["category-rule"]) == 0
    assert "No category rules defined." in capsys.readouterr().out


def test_category_rule_remove_reports_an_unknown_pattern(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["category-rule", "remove", "NOPE*"]) == 1
    assert "No category rule with pattern 'NOPE*'" in capsys.readouterr().out


def test_category_rule_apply_re_runs_every_rule(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        categories.add_rule(session, "COFFEE SHOP B", "Coffee")
        session.commit()  # added behind the CLI's back, so nothing is applied yet

    assert cli.main(["category-rule", "apply"]) == 0
    assert "Applied 1 rules; 1 transactions updated" in capsys.readouterr().out
    assert _categories_of(session_factory) == ["Coffee", "Dining", "Dining"]


# ------------------------------------------------------ category hierarchy (nesting)


def test_category_add_builds_a_nested_path(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["category", "add", "Food > Dining > Restaurants"]) == 0
    assert "'Food > Dining > Restaurants' ready." in capsys.readouterr().out


def test_category_list_shows_the_tree_indented(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        # "Dining" already exists (the bank's own top-level category, with the CSV's
        # three transactions); nesting it rescues it into place rather than forking.
        categories.ensure_path(session, "Food > Dining")
        session.commit()

    assert cli.main(["category"]) == 0  # bare command defaults to "list"
    out = capsys.readouterr().out
    assert "Food (3)" in out
    assert "  Dining (3)" in out  # indented one level under Food


def test_category_add_reports_a_cycle_without_crashing(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        categories.ensure_path(session, "Food > Dining")
        session.commit()

    # Food already has Dining as a child; moving Food under Dining (still under Food)
    # makes Food its own descendant's descendant.
    assert cli.main(["category", "add", "Food > Dining > Food"]) == 1
    assert "cycle" in capsys.readouterr().out


def test_list_dash_dash_category_resolves_a_full_path(tmp_path, monkeypatch, capsys):
    """``--category`` is routed through categories.resolve_path, so a nested category
    is reachable by its full path, not just a bare top-level name."""
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        categories.ensure_path(session, "Food > Dining")
        session.commit()

    assert cli.main(["list", "--category", "Food > Dining"]) == 0
    out = capsys.readouterr().out
    assert "3 txns" in out


def test_list_dash_dash_category_reports_an_unknown_name(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["list", "--category", "Nope"]) == 1
    assert "No category named 'Nope'" in capsys.readouterr().out


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
