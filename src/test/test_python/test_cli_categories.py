"""Tests for the CLI twins of the app's categorisation commands.

``cli.main`` is called with an argv list, exactly as the ``budget`` entry point does,
and the database is pointed at a temporary file through ``BUDGET_DB``.
"""

from budget_tracker import categories, cli
from conftest import _setup


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


def test_category_add_refuses_a_relocation_without_yes(tmp_path, monkeypatch, capsys):
    """"Dining" already exists (the bank's own top-level category, with the CSV's three
    transactions), so nesting it under "Food" is a relocation of that whole category —
    names are unique across the whole tree, so it cannot mean creating a second one."""
    _setup(tmp_path, monkeypatch)

    assert cli.main(["category", "add", "Food > Dining > Restaurants"]) == 1
    out = capsys.readouterr().out
    assert "relocate" in out
    assert "'Dining'" in out and "3 transaction(s)" in out
    assert "distinct name" in out


def test_category_add_confirmed_relocates_and_builds_the_path(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["category", "add", "Food > Dining > Restaurants", "--yes"]) == 0
    assert "'Food > Dining > Restaurants' ready." in capsys.readouterr().out


def test_category_list_shows_the_tree_indented(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        # "Dining" already exists (the bank's own top-level category, with the CSV's
        # three transactions); nesting it rescues it into place rather than forking.
        # Called directly on the core, so it is confirmed up front like any other
        # test fixture setup — it is not what this test is exercising.
        categories.ensure_path(session, "Food > Dining", confirm_relocation=True)
        session.commit()

    assert cli.main(["category"]) == 0  # bare command defaults to "list"
    out = capsys.readouterr().out
    assert "Food (3)" in out
    assert "  Dining (3)" in out  # indented one level under Food


def test_category_add_reports_a_cycle_without_crashing(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        categories.ensure_path(session, "Food > Dining", confirm_relocation=True)
        session.commit()

    # Food already has Dining as a child; moving Food under Dining (still under Food)
    # makes Food its own descendant's descendant. --yes confirms the relocation half of
    # this path (Food itself moving under Food > Dining) so the cycle check underneath
    # it is what actually rejects this.
    assert cli.main(["category", "add", "Food > Dining > Food", "--yes"]) == 1
    assert "cycle" in capsys.readouterr().out


# --------------------------------------------------------- category hierarchy (merge)


def test_category_merge_without_yes_only_previews(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        categories.ensure_path(session, "Snacks")
        session.commit()

    assert cli.main(["category", "merge", "Dining", "Snacks"]) == 1
    out = capsys.readouterr().out
    assert "Would merge 'Dining' into 'Snacks'" in out
    assert "3 transaction(s)" in out
    # Nothing actually moved: the trial run inside the preview was never committed.
    assert _categories_of(session_factory) == ["Dining", "Dining", "Dining"]


def test_category_merge_confirmed_moves_everything_and_deletes_the_source(
    tmp_path, monkeypatch, capsys
):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        categories.ensure_path(session, "Snacks")
        session.commit()

    assert cli.main(["category", "merge", "Dining", "Snacks", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Merged 'Dining' into 'Snacks'" in out
    assert "3 transaction(s)" in out
    assert _categories_of(session_factory) == ["Snacks", "Snacks", "Snacks"]
    with session_factory() as session:
        assert categories.resolve_path(session, "Dining") is None


def test_category_merge_reports_an_unknown_category(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["category", "merge", "Nope", "Dining"]) == 1
    assert "No category named 'Nope'" in capsys.readouterr().out
