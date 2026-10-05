"""Tests for the ``budget limits ...`` CLI: the monthly budget's tracking and
planning tables, and the ``set``/``clear``/``income`` writers.

``cli.main`` is called with an argv list, exactly as the ``budget`` entry point does,
and the database is pointed at a temporary file through ``BUDGET_DB`` -- same
convention as ``test_cli_categories.py``. Where a test needs a fixed "today" (the
tracking table's pace line, and over/ahead-of-pace marking), ``budget_cmd.date`` is
monkeypatched rather than the real clock faked some other way -- see that module's
``_do_track`` docstring comment for why that specific name.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select

from budget_tracker import cli, categories
from budget_tracker.cli import budget_cmd
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.models import Account, BudgetAmount, Category, Currency, Transaction


def _setup(tmp_path, monkeypatch, category_names=()):
    """Seed a currency, one account, and the named categories, returning plain ids
    (not ORM objects, which would be detached the moment this session closes)."""
    db_path = tmp_path / "t.db"
    engine = get_engine(db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        currency = Currency(value="USD", symbol="$", decimal_places=2)
        session.add(currency)
        session.flush()
        account = Account(name="Checking", currency_id=currency.id)
        session.add(account)
        cats = {}
        for name in category_names:
            category = Category(value=name)
            session.add(category)
            cats[name] = category
        session.flush()
        currency_id, account_id = currency.id, account.id
        cat_ids = {name: category.id for name, category in cats.items()}
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory, currency_id, account_id, cat_ids


def _txn(session_factory, currency_id, account_id, day, amount_minor, description, category_id=None):
    with session_factory() as session:
        txn = Transaction(
            account_id=account_id,
            currency_id=currency_id,
            category_id=category_id,
            posted_date=day,
            description=description,
            raw_description=description,
            value_minor=amount_minor,
            import_hash=f"{day}-{amount_minor}-{description}-{id(object())}",
        )
        session.add(txn)
        session.commit()


class _FixedDate(date):
    """A ``date`` subclass whose ``.today()`` is pinned, for monkeypatching
    ``budget_cmd.date`` -- see the module docstring above."""

    _TODAY = date(2026, 9, 4)

    @classmethod
    def today(cls):
        return cls._TODAY


def _freeze(monkeypatch, today: date) -> None:
    _FixedDate._TODAY = today
    monkeypatch.setattr(budget_cmd, "date", _FixedDate)


# --------------------------------------------------------------------------- set/clear

def test_set_writes_a_budget_amount(tmp_path, monkeypatch, capsys):
    session_factory, *_ , cats = _setup(tmp_path, monkeypatch, category_names=("Food",))

    assert cli.main(["limits", "set", "Food", "600", "--month", "2026-09"]) == 0
    out = capsys.readouterr().out
    assert "Set 'Food' to 600.00/month for 2026-09." in out

    with session_factory() as session:
        row = session.scalar(select(BudgetAmount).where(BudgetAmount.kind == "expense"))
        assert row.amount_minor == 60000
        assert row.month == date(2026, 9, 1)
        assert row.category_id == cats["Food"]


def test_set_parses_a_comma_amount(tmp_path, monkeypatch, capsys):
    session_factory, *_ = _setup(tmp_path, monkeypatch, category_names=("Rent",))

    assert cli.main(["limits", "set", "Rent", "1,200.50", "--month", "2026-09"]) == 0
    with session_factory() as session:
        row = session.scalar(select(BudgetAmount).where(BudgetAmount.kind == "expense"))
        assert row.amount_minor == 120050


def test_set_resolves_a_category_path(tmp_path, monkeypatch):
    session_factory, currency_id, account_id, _ = _setup(tmp_path, monkeypatch, category_names=())
    with session_factory() as session:
        categories.ensure_path(session, "Food > Dining")
        session.commit()

    assert cli.main(["limits", "set", "Food > Dining", "75", "--month", "2026-09"]) == 0
    with session_factory() as session:
        dining = categories.resolve_path(session, "Food > Dining")
        row = session.scalar(select(BudgetAmount).where(BudgetAmount.kind == "expense"))
        assert row.category_id == dining.id
        assert row.amount_minor == 7500


def test_set_unknown_category_is_an_error(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["limits", "set", "Nope", "600"]) == 1
    assert "No category named 'Nope'." in capsys.readouterr().out


def test_set_bad_amount_is_an_error(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, category_names=("Food",))

    assert cli.main(["limits", "set", "Food", "abc"]) == 1
    assert "Could not parse amount 'abc'" in capsys.readouterr().out


def test_set_bad_month_is_an_error(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, category_names=("Food",))

    assert cli.main(["limits", "set", "Food", "600", "--month", "nope"]) == 1
    assert "Could not parse month 'nope'" in capsys.readouterr().out


def test_set_wrong_arity_prints_usage(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, category_names=("Food",))

    assert cli.main(["limits", "set", "Food"]) == 1
    assert "Usage: budget limits set" in capsys.readouterr().out


def test_clear_removes_a_budget_amount(tmp_path, monkeypatch, capsys):
    session_factory, *_ = _setup(tmp_path, monkeypatch, category_names=("Food",))
    cli.main(["limits", "set", "Food", "600", "--month", "2026-09"])
    capsys.readouterr()

    assert cli.main(["limits", "clear", "Food", "--month", "2026-09"]) == 0
    assert "Cleared 'Food' for 2026-09." in capsys.readouterr().out
    with session_factory() as session:
        assert session.scalar(select(BudgetAmount).where(BudgetAmount.kind == "expense")) is None


def test_clear_unknown_category_is_an_error(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["limits", "clear", "Nope"]) == 1
    assert "No category named 'Nope'." in capsys.readouterr().out


# -------------------------------------------------------------------------------- income

def test_income_set_and_clear(tmp_path, monkeypatch, capsys):
    session_factory, *_ = _setup(tmp_path, monkeypatch)

    assert cli.main(["limits", "income", "5000", "--month", "2026-09"]) == 0
    assert "Set the income target to 5,000.00/month for 2026-09." in capsys.readouterr().out
    with session_factory() as session:
        row = session.scalar(select(BudgetAmount).where(BudgetAmount.kind == "income"))
        assert row.amount_minor == 500000
        assert row.category_id is None

    assert cli.main(["limits", "income", "--clear", "--month", "2026-09"]) == 0
    assert "Cleared the income target for 2026-09." in capsys.readouterr().out
    with session_factory() as session:
        assert session.scalar(select(BudgetAmount).where(BudgetAmount.kind == "income")) is None


def test_income_clear_with_an_amount_is_an_error(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["limits", "income", "100", "--clear"]) == 1
    assert "Usage: budget limits income" in capsys.readouterr().out


# ---------------------------------------------------------------------------- tracking

def test_track_shows_the_budget_row_and_totals(tmp_path, monkeypatch, capsys):
    session_factory, currency_id, account_id, cats = _setup(
        tmp_path, monkeypatch, category_names=("Food", "Fun")
    )
    _txn(session_factory, currency_id, account_id, date(2026, 9, 5), -30000, "Dinner", cats["Food"])
    _txn(session_factory, currency_id, account_id, date(2026, 9, 6), -1500, "Movie", cats["Fun"])
    cli.main(["limits", "set", "Food", "600", "--month", "2026-09"])
    capsys.readouterr()

    assert cli.main(["limits", "2026-09"]) == 0
    out = capsys.readouterr().out
    assert "Food" in out
    assert "600.00" in out  # budget
    assert "300.00" in out  # spent
    assert "300.00" in out  # left (budget - spent, happens to match here)
    assert "Total budget 600.00" in out
    assert "Spent 300.00" in out
    assert "Not budgeted 15.00" in out
    assert "Total spending 315.00" in out
    assert "Fun" not in out  # unbudgeted category gets no row of its own


def test_track_notes_a_copied_forward_plan(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, category_names=("Food",))
    cli.main(["limits", "set", "Food", "600", "--month", "2026-09"])
    capsys.readouterr()

    assert cli.main(["limits", "2026-10"]) == 0
    assert "(plan copied forward from 2026-09)" in capsys.readouterr().out


def test_track_income_row_target_vs_actual(tmp_path, monkeypatch, capsys):
    session_factory, currency_id, account_id, _ = _setup(tmp_path, monkeypatch)
    _txn(session_factory, currency_id, account_id, date(2026, 9, 1), 500000, "Paycheck")
    cli.main(["limits", "income", "6000", "--month", "2026-09"])
    capsys.readouterr()

    assert cli.main(["limits", "2026-09"]) == 0
    out = capsys.readouterr().out
    assert "Income" in out
    assert "6,000.00" in out  # target
    assert "5,000.00" in out  # actual


def test_track_current_month_marks_over_and_ahead_of_pace(tmp_path, monkeypatch, capsys):
    session_factory, currency_id, account_id, cats = _setup(
        tmp_path, monkeypatch, category_names=("Food", "Fun")
    )
    _freeze(monkeypatch, date(2026, 9, 4))  # 4 of 30 days (~13%) elapsed
    # Food: budget 100, spent 150 -> over.
    _txn(session_factory, currency_id, account_id, date(2026, 9, 2), -15000, "Big dinner", cats["Food"])
    cli.main(["limits", "set", "Food", "100", "--month", "2026-09"])
    # Fun: budget 1000, spent 200 (20%) -> well past the ~13% elapsed -> ahead, not over.
    _txn(session_factory, currency_id, account_id, date(2026, 9, 3), -20000, "Movies", cats["Fun"])
    cli.main(["limits", "set", "Fun", "1000", "--month", "2026-09"])
    capsys.readouterr()

    assert cli.main(["limits", "2026-09"]) == 0
    out = capsys.readouterr().out
    assert "OVER" in out
    assert "ahead" in out
    assert "Sep 4 of 30 (13% of the month)" in out


def test_track_past_month_shows_no_pace_line(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, category_names=("Food",))
    _freeze(monkeypatch, date(2026, 9, 4))
    cli.main(["limits", "set", "Food", "600", "--month", "2026-08"])
    capsys.readouterr()

    assert cli.main(["limits", "2026-08"]) == 0
    out = capsys.readouterr().out
    assert "of the month" not in out


def test_track_default_month_is_the_current_one(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, category_names=("Food",))
    _freeze(monkeypatch, date(2026, 9, 4))
    cli.main(["limits", "set", "Food", "600", "--month", "2026-09"])
    capsys.readouterr()

    assert cli.main(["limits"]) == 0
    assert "Sep 4 of 30" in capsys.readouterr().out


def test_track_bad_month_is_an_error(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["limits", "2026-99"]) == 1
    assert "Could not parse month" in capsys.readouterr().out


# -------------------------------------------------------------------------------- plan

def test_plan_shows_averages_last_month_and_a_dash_for_no_budget(tmp_path, monkeypatch, capsys):
    session_factory, currency_id, account_id, cats = _setup(
        tmp_path, monkeypatch, category_names=("Food",)
    )
    _txn(session_factory, currency_id, account_id, date(2026, 9, 15), -4000, "Dinner", cats["Food"])
    _txn(session_factory, currency_id, account_id, date(2026, 8, 15), -2000, "Lunch", cats["Food"])
    _txn(session_factory, currency_id, account_id, date(2026, 7, 15), 0, "Nothing", cats["Food"])

    assert cli.main(["limits", "plan", "2026-10", "--months", "3"]) == 0
    out = capsys.readouterr().out
    assert "Food" in out
    assert "20.00" in out  # avg: (40.00 + 20.00 + 0) / 3
    assert "40.00" in out  # last month (September, right before October)
    assert "–" in out  # no budget set yet


def test_plan_shows_the_budget_once_set(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, category_names=("Food",))
    cli.main(["limits", "set", "Food", "500", "--month", "2026-10"])
    capsys.readouterr()

    assert cli.main(["limits", "plan", "2026-10"]) == 0
    out = capsys.readouterr().out
    assert "Food" in out
    assert "500.00" in out


def test_plan_unallocated_against_an_income_target(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, category_names=("Food",))
    cli.main(["limits", "set", "Food", "500", "--month", "2026-10"])
    cli.main(["limits", "income", "5000", "--month", "2026-10"])
    capsys.readouterr()

    assert cli.main(["limits", "plan", "2026-10"]) == 0
    out = capsys.readouterr().out
    assert "Total budgeted 500.00" in out
    assert "Income target 5,000.00" in out
    assert "Unallocated 4,500.00" in out


def test_plan_with_no_income_target_shows_no_unallocated_line(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, category_names=("Food",))
    cli.main(["limits", "set", "Food", "500", "--month", "2026-10"])
    capsys.readouterr()

    assert cli.main(["limits", "plan", "2026-10"]) == 0
    out = capsys.readouterr().out
    assert "Unallocated" not in out


def test_plan_bad_month_is_an_error(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    assert cli.main(["limits", "plan", "not-a-month"]) == 1
    assert "Could not parse month" in capsys.readouterr().out


def test_plan_default_month_is_the_current_one(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, category_names=("Food",))
    _freeze(monkeypatch, date(2026, 9, 4))
    cli.main(["limits", "set", "Food", "600", "--month", "2026-09"])
    capsys.readouterr()

    assert cli.main(["limits", "plan"]) == 0
    out = capsys.readouterr().out
    assert "600.00" in out
