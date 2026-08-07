"""Tests for the CLI twins of the app's categorisation commands.

``cli.main`` is called with an argv list, exactly as the ``budget`` entry point does,
and the database is pointed at a temporary file through ``BUDGET_DB``.
"""

import datetime
from decimal import Decimal

from budget_tracker import categories, cli, formats, queries
from budget_tracker import rates as rates_module
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.importer import import_csv
from budget_tracker.models import Account, Currency, Transaction
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


# ------------------------------------------------------------------------ transfers


def _seed_same_account_transfer_candidates(tmp_path, monkeypatch):
    """Two legs in one account, same amount and opposite sign, a day apart.

    Only pairable with ``allow_same_account`` — the default rule requires different
    accounts.
    """
    db_path = tmp_path / "same_account.db"
    engine = get_engine(db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        currency = Currency(value="USD", symbol="$", decimal_places=2)
        session.add(currency)
        session.flush()
        account = Account(name="Checking", currency_id=currency.id)
        session.add(account)
        session.flush()
        session.add(
            Transaction(
                account_id=account.id,
                currency_id=currency.id,
                posted_date=datetime.date(2025, 6, 1),
                description="Move out",
                raw_description="Move out",
                value_minor=-50000,
                category_source="unset",
                import_hash="sa-out",
            )
        )
        session.add(
            Transaction(
                account_id=account.id,
                currency_id=currency.id,
                posted_date=datetime.date(2025, 6, 2),
                description="Move in",
                raw_description="Move in",
                value_minor=50000,
                category_source="unset",
                import_hash="sa-in",
            )
        )
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def test_transfers_default_does_not_pair_same_account_legs(tmp_path, monkeypatch, capsys):
    _seed_same_account_transfer_candidates(tmp_path, monkeypatch)

    assert cli.main(["transfers"]) == 0
    out = capsys.readouterr().out
    assert "Found 0 new transfer pair(s)" in out
    assert "0 transaction(s) are now excluded" in out


def test_transfers_same_account_flag_pairs_them(tmp_path, monkeypatch, capsys):
    session_factory = _seed_same_account_transfer_candidates(tmp_path, monkeypatch)

    assert cli.main(["transfers", "--same-account"]) == 0
    out = capsys.readouterr().out
    assert "Found 1 new transfer pair(s)" in out
    assert "(same-account allowed)" in out
    assert "2 transaction(s) are now excluded" in out
    with session_factory() as session:
        assert queries.get_totals(session).transfer_count == 2


def test_transfers_reset_undoes_a_same_account_pairing(tmp_path, monkeypatch, capsys):
    session_factory = _seed_same_account_transfer_candidates(tmp_path, monkeypatch)

    cli.main(["transfers", "--same-account"])
    capsys.readouterr()

    assert cli.main(["transfers", "--reset"]) == 0
    assert "Un-paired 2 transaction(s)." in capsys.readouterr().out
    with session_factory() as session:
        assert queries.get_totals(session).transfer_count == 0


# ------------------------------------------------------- the interactive file picker


def _picker_inbox(tmp_path, monkeypatch):
    """An inbox with a file at the top and one inside a folder."""
    inbox = tmp_path / "to_import"
    (inbox / "2026-08").mkdir(parents=True)
    (inbox / "top.csv").write_text(CSV, encoding="utf-8")
    (inbox / "2026-08" / "nested.csv").write_text(CSV, encoding="utf-8")
    monkeypatch.setattr("budget_tracker.cli.TO_IMPORT_DIR", inbox)
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
    monkeypatch.setattr("budget_tracker.cli.TO_IMPORT_DIR", inbox)

    assert cli._select_csv_interactively() is None
    assert "Nothing to import in" in capsys.readouterr().out


def _seed_multi_currency_accounts(tmp_path, monkeypatch):
    """A USD and a CHF account, each with one transaction, ten days apart — so a
    derived fetch range and a two-currency pair both have something to find."""
    db_path = tmp_path / "rates.db"
    engine = get_engine(db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        usd = Currency(value="USD", symbol="$", decimal_places=2)
        chf = Currency(value="CHF", symbol="CHF", decimal_places=2)
        session.add_all([usd, chf])
        session.flush()
        checking = Account(name="Checking", currency_id=usd.id)
        wise_chf = Account(name="Wise CHF", currency_id=chf.id)
        session.add_all([checking, wise_chf])
        session.flush()
        session.add_all(
            [
                Transaction(
                    account_id=checking.id,
                    currency_id=usd.id,
                    posted_date=datetime.date(2025, 6, 1),
                    description="A",
                    raw_description="A",
                    value_minor=-1000,
                    import_hash="a",
                ),
                Transaction(
                    account_id=wise_chf.id,
                    currency_id=chf.id,
                    posted_date=datetime.date(2025, 6, 10),
                    description="B",
                    raw_description="B",
                    value_minor=-2000,
                    import_hash="b",
                ),
            ]
        )
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def test_rates_fetch_derives_its_range_and_pair_from_what_is_on_file(
    tmp_path, monkeypatch, capsys
):
    _seed_multi_currency_accounts(tmp_path, monkeypatch)
    calls = []

    def stub(session, start, end, base, quotes):
        calls.append((start, end, base, tuple(quotes)))
        return 1

    monkeypatch.setattr("budget_tracker.rates.fetch_ecb_rates", stub)

    assert cli.main(["rates", "fetch"]) == 0
    out = capsys.readouterr().out
    assert "Range: 2025-06-01..2025-06-10" in out
    assert "Wrote 1 rate(s) for USD -> CHF." in out
    assert calls == [
        (datetime.date(2025, 6, 1), datetime.date(2025, 6, 10), "USD", ("CHF",))
    ]


def test_rates_fetch_with_explicit_dates_skips_the_derived_range(
    tmp_path, monkeypatch, capsys
):
    _seed_multi_currency_accounts(tmp_path, monkeypatch)
    calls = []

    def stub(session, start, end, base, quotes):
        calls.append((start, end))
        return 3

    monkeypatch.setattr("budget_tracker.rates.fetch_ecb_rates", stub)

    assert cli.main(
        ["rates", "fetch", "--start", "2020-01-01", "--end", "2020-01-31"]
    ) == 0
    out = capsys.readouterr().out
    assert "Range: 2020-01-01..2020-01-31" in out
    assert "Wrote 3 rate(s)" in out
    assert calls == [(datetime.date(2020, 1, 1), datetime.date(2020, 1, 31))]


def test_rates_fetch_reports_a_frankfurter_failure_without_a_traceback(
    tmp_path, monkeypatch, capsys
):
    """Never a traceback, and never a silent success that wrote nothing."""
    _seed_multi_currency_accounts(tmp_path, monkeypatch)

    def failing(session, start, end, base, quotes):
        raise rates_module.FrankfurterError("Could not reach Frankfurter at ...")

    monkeypatch.setattr("budget_tracker.rates.fetch_ecb_rates", failing)

    assert cli.main(["rates", "fetch"]) == 1
    out = capsys.readouterr().out
    assert "Could not fetch ECB rates: Could not reach Frankfurter" in out


def test_rates_fetch_with_one_currency_on_file_fetches_nothing(
    tmp_path, monkeypatch, capsys
):
    session_factory = _setup(tmp_path, monkeypatch)  # single-currency CSV fixture

    assert cli.main(["rates", "fetch"]) == 0
    assert "nothing to fetch" in capsys.readouterr().out


def test_rates_fetch_with_no_transactions_and_no_dates_is_refused(
    tmp_path, monkeypatch, capsys
):
    db_path = tmp_path / "empty.db"
    init_db(get_engine(db_path))
    monkeypatch.setenv("BUDGET_DB", str(db_path))

    assert cli.main(["rates", "fetch"]) == 1
    assert "No transactions" in capsys.readouterr().out


def test_rates_set_records_a_manual_rate_that_wins_for_that_day(
    tmp_path, monkeypatch, capsys
):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        rates_module.record_rate(
            session, datetime.date(2025, 7, 2), "USD", "CHF", Decimal("0.85"),
            rates_module.ECB,
        )
        session.commit()

    assert cli.main(["rates", "set", "USD", "CHF", "0.90", "--on", "2025-07-02"]) == 0
    out = capsys.readouterr().out
    assert "Set USD -> CHF = 0.90 on 2025-07-02 (manual)." in out

    with session_factory() as session:
        assert rates_module.rate_on(
            session, datetime.date(2025, 7, 2), "USD", "CHF"
        ) == Decimal("0.90")


def test_rates_set_a_bad_rate_is_reported_without_a_traceback(
    tmp_path, monkeypatch, capsys
):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["rates", "set", "USD", "CHF", "not-a-number"]) == 1
    assert "Could not read 'not-a-number' as a rate" in capsys.readouterr().out


def test_rates_list_shows_pair_source_and_span(tmp_path, monkeypatch, capsys):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        rates_module.record_rate(
            session, datetime.date(2025, 7, 1), "USD", "CHF", Decimal("0.88"),
            rates_module.ECB,
        )
        rates_module.record_rate(
            session, datetime.date(2025, 7, 5), "USD", "CHF", Decimal("0.89"),
            rates_module.ECB,
        )
        session.commit()

    assert cli.main(["rates"]) == 0
    out = capsys.readouterr().out
    assert "USD -> CHF" in out
    assert "2025-07-01..2025-07-05" in out
    assert "2 rates" in out


def test_rates_list_with_nothing_cached_says_so(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["rates"]) == 0
    assert "No exchange rates cached yet." in capsys.readouterr().out


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
