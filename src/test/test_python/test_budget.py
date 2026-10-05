"""Tests for the monthly budget core: storage, copy-forward, plan averaging, and
tracking against the same actuals :mod:`budget_tracker.stats` reports.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from budget_tracker import budget, categories, rates, stats, transfers
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.models import Account, Category, Currency, Transaction


def _session_factory(tmp_path, name="t.db"):
    engine = get_engine(tmp_path / name)
    init_db(engine)
    return get_sessionmaker(engine)


def _seed(session, account_names=("Checking",), category_names=()):
    currency = Currency(value="USD", symbol="$", decimal_places=2)
    session.add(currency)
    session.flush()
    accounts = {}
    for name in account_names:
        account = Account(name=name, currency_id=currency.id)
        session.add(account)
        accounts[name] = account
    cats = {}
    for name in category_names:
        category = Category(value=name)
        session.add(category)
        cats[name] = category
    session.flush()
    return currency, accounts, cats


def _txn(session, currency, account, day, amount, description="X", category=None):
    txn = Transaction(
        account_id=account.id,
        currency_id=currency.id,
        category_id=category.id if category is not None else None,
        posted_date=day,
        description=description,
        raw_description=description,
        value_minor=amount,
        import_hash=f"{account.id}-{day}-{amount}-{description}-{id(object())}",
    )
    session.add(txn)
    session.flush()
    return txn


# ----------------------------------------------------------------------- month math

def test_month_start_takes_the_first_of_the_month():
    assert budget.month_start(date(2026, 10, 17)) == date(2026, 10, 1)
    assert budget.month_start(date(2026, 1, 1)) == date(2026, 1, 1)


def test_add_months_rolls_over_the_year_both_directions():
    assert budget._add_months(date(2026, 1, 1), -1) == date(2025, 12, 1)
    assert budget._add_months(date(2026, 1, 1), 11) == date(2026, 12, 1)
    assert budget._add_months(date(2026, 1, 1), 12) == date(2027, 1, 1)


# ------------------------------------------------------------- effective_month / get_plan

def test_effective_month_is_none_before_anything_is_stored(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        assert budget.effective_month(session, date(2026, 10, 1)) is None
        plan = budget.get_plan(session, date(2026, 10, 1))
        assert plan.source_month is None
        assert plan.income_target_minor is None
        assert plan.expense == {}


def test_effective_month_copies_forward_from_the_latest_earlier_month(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed(session, category_names=("Food",))
        budget.set_amount(session, date(2026, 8, 1), "Food", 50000)
        session.commit()

    with session_factory() as session:
        # September and October have nothing of their own; both resolve to August.
        assert budget.effective_month(session, date(2026, 9, 15)) == date(2026, 8, 1)
        assert budget.effective_month(session, date(2026, 10, 1)) == date(2026, 8, 1)
        # A month before anything was ever set still resolves to nothing.
        assert budget.effective_month(session, date(2026, 7, 31)) is None

        plan = budget.get_plan(session, date(2026, 10, 1))
        food = categories.resolve_path(session, "Food")
        assert plan.source_month == date(2026, 8, 1)
        assert plan.expense == {food.id: 50000}


def test_set_amount_unknown_category_raises(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed(session)
        with pytest.raises(ValueError, match="No category named"):
            budget.set_amount(session, date(2026, 10, 1), "Nope", 1000)


def test_set_amount_income_rejects_a_category(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed(session)
        with pytest.raises(ValueError, match="no category"):
            budget.set_amount(
                session, date(2026, 10, 1), "Food", 1000, kind=budget.INCOME
            )


def test_set_amount_expense_requires_a_category(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed(session)
        with pytest.raises(ValueError, match="needs a category"):
            budget.set_amount(session, date(2026, 10, 1), None, 1000, kind=budget.EXPENSE)


def test_set_amount_resolves_a_full_path(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed(session)
        categories.ensure_path(session, "Food > Dining")
        budget.set_amount(session, date(2026, 10, 1), "Food > Dining", 20000)
        session.commit()

    with session_factory() as session:
        dining = categories.resolve_path(session, "Food > Dining")
        plan = budget.get_plan(session, date(2026, 10, 1))
        assert plan.expense == {dining.id: 20000}


# ----------------------------------------------------------------------- copy-forward

def test_editing_an_unplanned_month_copies_forward_then_edits(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed(session, category_names=("Food", "Rent"))
        budget.set_amount(session, date(2026, 8, 1), "Food", 50000)
        session.commit()

    with session_factory() as session:
        # October is unplanned; setting Rent here must copy August's Food in too,
        # and the copy must land as October's own rows (not still pointing at August).
        budget.set_amount(session, date(2026, 10, 1), "Rent", 150000)
        session.commit()

    with session_factory() as session:
        food = categories.resolve_path(session, "Food")
        rent = categories.resolve_path(session, "Rent")
        plan = budget.get_plan(session, date(2026, 10, 1))
        assert plan.source_month == date(2026, 10, 1)
        assert plan.expense == {food.id: 50000, rent.id: 150000}
        # August itself is untouched by the copy.
        august = budget.get_plan(session, date(2026, 8, 1))
        assert august.expense == {food.id: 50000}


def test_clearing_in_a_later_month_sticks(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed(session, category_names=("Food", "Rent"))
        budget.set_amount(session, date(2026, 8, 1), "Food", 50000)
        budget.set_amount(session, date(2026, 8, 1), "Rent", 150000)
        session.commit()

    with session_factory() as session:
        # October copies forward (Food + Rent) then clears Food.
        budget.set_amount(session, date(2026, 10, 1), "Food", None)
        session.commit()

    with session_factory() as session:
        rent = categories.resolve_path(session, "Rent")
        october = budget.get_plan(session, date(2026, 10, 1))
        assert october.expense == {rent.id: 150000}

        # November is unplanned and must inherit October's plan -- Food stays gone,
        # not resurrected from August.
        november = budget.get_plan(session, date(2026, 11, 1))
        assert november.source_month == date(2026, 10, 1)
        assert november.expense == {rent.id: 150000}


def test_set_amount_never_touches_another_months_rows(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed(session, category_names=("Food",))
        budget.set_amount(session, date(2026, 8, 1), "Food", 50000)
        budget.set_amount(session, date(2026, 10, 1), "Food", 99900)  # unplanned; copies first
        session.commit()

    with session_factory() as session:
        food = categories.resolve_path(session, "Food")
        august = budget.get_plan(session, date(2026, 8, 1))
        october = budget.get_plan(session, date(2026, 10, 1))
        assert august.expense == {food.id: 50000}
        assert october.expense == {food.id: 99900}


# ------------------------------------------------------------------------ income target

def test_income_target_set_and_clear(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed(session)
        budget.set_amount(session, date(2026, 10, 1), None, 500000, kind=budget.INCOME)
        session.commit()

    with session_factory() as session:
        plan = budget.get_plan(session, date(2026, 10, 1))
        assert plan.income_target_minor == 500000

        budget.set_amount(session, date(2026, 10, 1), None, None, kind=budget.INCOME)
        session.commit()

    with session_factory() as session:
        plan = budget.get_plan(session, date(2026, 10, 1))
        assert plan.income_target_minor is None


# ------------------------------------------------------------------- category deletion

def test_deleting_a_category_cascades_its_budget_rows(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed(session, category_names=("Food",))
        budget.set_amount(session, date(2026, 10, 1), "Food", 50000)
        session.commit()

    with session_factory() as session:
        food = categories.resolve_path(session, "Food")
        session.delete(food)
        session.commit()

    with session_factory() as session:
        from budget_tracker.models import BudgetAmount

        assert session.scalar(select(BudgetAmount)) is None


# --------------------------------------------------------------------------- track()

def test_track_matches_stats_build_report_for_the_same_month(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, cats = _seed(session, category_names=("Food", "Rent"))
        _txn(session, currency, accounts["Checking"], date(2026, 9, 5), -3000, "Lunch", cats["Food"])
        _txn(session, currency, accounts["Checking"], date(2026, 9, 10), -150000, "Rent", cats["Rent"])
        budget.set_amount(session, date(2026, 9, 1), "Food", 50000)
        session.commit()

    with session_factory() as session:
        window = budget._month_window(date(2026, 9, 1))
        report = stats.build_report(session, window)
        spent_by_name = {c.name: -min(0, c.total_minor) for c in report.categories}

        view = budget.track(session, date(2026, 9, 1), today=date(2026, 9, 30))
        food_row = next(r for r in view.rows if r.name == "Food")
        assert food_row.spent_minor == spent_by_name["Food"] == 3000
        assert view.total_spending_minor == -report.net_spend_minor


def test_track_parent_and_child_budgeted_do_not_double_count(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, _ = _seed(session, category_names=())
        categories.ensure_path(session, "Food > Dining")
        food = categories.resolve_path(session, "Food")
        dining = categories.resolve_path(session, "Food > Dining")
        _txn(session, currency, accounts["Checking"], date(2026, 9, 1), -2000, "Groceries", food)
        _txn(session, currency, accounts["Checking"], date(2026, 9, 2), -1000, "Pizza", dining)
        budget.set_amount(session, date(2026, 9, 1), "Food", 10000)
        budget.set_amount(session, date(2026, 9, 1), "Food > Dining", 2000)
        session.commit()

    with session_factory() as session:
        view = budget.track(session, date(2026, 9, 1), today=date(2026, 9, 30))
        # Food's own actual already rolls up Dining's (3000 total), so the combined
        # total must count it once, not add Dining's spend/budget a second time.
        assert view.total_budget_minor == 10000
        assert view.total_spent_minor == 3000
        assert len(view.rows) == 2  # both still shown, just not double-counted in totals


def test_track_unbudgeted_category_counts_toward_not_budgeted(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, cats = _seed(session, category_names=("Food", "Fun"))
        _txn(session, currency, accounts["Checking"], date(2026, 9, 1), -5000, "Dinner", cats["Food"])
        _txn(session, currency, accounts["Checking"], date(2026, 9, 2), -1500, "Movie", cats["Fun"])
        budget.set_amount(session, date(2026, 9, 1), "Food", 10000)
        session.commit()

    with session_factory() as session:
        view = budget.track(session, date(2026, 9, 1), today=date(2026, 9, 30))
        assert view.total_spending_minor == 6500
        assert view.total_spent_minor == 5000  # only Food is budgeted
        assert view.not_budgeted_minor == 1500  # Fun's spend, uncounted above
        assert [r.name for r in view.rows] == ["Food"]  # Fun has no budget, no row


def test_track_refunds_net_against_spend(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, cats = _seed(session, category_names=("Food",))
        _txn(session, currency, accounts["Checking"], date(2026, 9, 1), -10000, "Dinner", cats["Food"])
        _txn(session, currency, accounts["Checking"], date(2026, 9, 2), 4000, "Refund", cats["Food"])
        budget.set_amount(session, date(2026, 9, 1), "Food", 10000)
        session.commit()

    with session_factory() as session:
        view = budget.track(session, date(2026, 9, 1), today=date(2026, 9, 30))
        food_row = next(r for r in view.rows if r.name == "Food")
        assert food_row.spent_minor == 6000  # 10000 - 4000, not 10000
        assert food_row.left_minor == 4000


def test_track_transfers_and_excluded_rows_dont_count(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, cats = _seed(
            session, account_names=("Checking", "Savings"), category_names=("Food",)
        )
        _txn(session, currency, accounts["Checking"], date(2026, 9, 1), -50000, "Xfer To")
        _txn(session, currency, accounts["Savings"], date(2026, 9, 2), 50000, "Xfer From")
        _txn(session, currency, accounts["Checking"], date(2026, 9, 3), -2000, "Snack", cats["Food"])
        excluded = _txn(
            session, currency, accounts["Checking"], date(2026, 9, 4), -99999, "ACATS", cats["Food"]
        )
        transfers.detect_transfers(session)
        transfers.exclude(session, [excluded.id])
        budget.set_amount(session, date(2026, 9, 1), "Food", 10000)
        session.commit()

    with session_factory() as session:
        view = budget.track(session, date(2026, 9, 1), today=date(2026, 9, 30))
        food_row = next(r for r in view.rows if r.name == "Food")
        assert food_row.spent_minor == 2000  # the transfer and the excluded row are out
        assert view.total_spending_minor == 2000


def test_track_multi_currency_converts_to_home_currency(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        usd = Currency(value="USD", symbol="$", decimal_places=2)
        chf = Currency(value="CHF", symbol="Fr", decimal_places=2)
        session.add(usd)
        session.add(chf)
        session.flush()
        checking = Account(name="Checking", currency_id=usd.id)
        card = Account(name="Card CHF", currency_id=chf.id)
        session.add(checking)
        session.add(card)
        food = Category(value="Food")
        session.add(food)
        session.flush()

        day = date(2026, 9, 5)
        rates.record_rate(session, day, "CHF", "USD", Decimal("1.10"), rates.ECB)
        _txn(session, chf, card, day, -10000, "Fondue", food)  # -100.00 CHF
        budget.set_amount(session, date(2026, 9, 1), "Food", 20000)
        session.commit()

    with session_factory() as session:
        view = budget.track(session, date(2026, 9, 1), today=date(2026, 9, 30))
        food_row = next(r for r in view.rows if r.name == "Food")
        assert food_row.spent_minor == 11000  # 100.00 CHF * 1.10 = 110.00 USD


def test_track_past_month_is_fully_elapsed_and_never_ahead_of_pace(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed(session, category_names=("Food",))
        budget.set_amount(session, date(2026, 1, 1), "Food", 10000)
        session.commit()

    with session_factory() as session:
        view = budget.track(session, date(2026, 1, 1), today=date(2026, 9, 30))
        assert view.is_current is False
        assert view.elapsed_fraction == 1.0
        assert all(not r.ahead_of_pace for r in view.rows)


def test_track_current_month_pace_with_injected_today(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, cats = _seed(session, category_names=("Food",))
        # 4 of 30 days elapsed (~13%); 2000 of a 10000 budget spent (20%) is ahead.
        _txn(session, currency, accounts["Checking"], date(2026, 9, 2), -2000, "Early", cats["Food"])
        budget.set_amount(session, date(2026, 9, 1), "Food", 10000)
        session.commit()

    with session_factory() as session:
        view = budget.track(session, date(2026, 9, 1), today=date(2026, 9, 4))
        assert view.is_current is True
        assert view.elapsed_fraction == pytest.approx(4 / 30)
        food_row = next(r for r in view.rows if r.name == "Food")
        assert food_row.used == pytest.approx(0.2)
        assert food_row.ahead_of_pace is True
        assert food_row.over is False


def test_track_income_target_vs_actual(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, _ = _seed(session)
        _txn(session, currency, accounts["Checking"], date(2026, 9, 1), 500000, "Paycheck")
        budget.set_amount(session, date(2026, 9, 1), None, 600000, kind=budget.INCOME)
        session.commit()

    with session_factory() as session:
        view = budget.track(session, date(2026, 9, 1), today=date(2026, 9, 30))
        assert view.income_target_minor == 600000
        assert view.income_actual_minor == 500000


# ---------------------------------------------------------------------- plan_rows()

def test_plan_rows_averages_over_m_months_dividing_by_m_even_with_zero_months(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, cats = _seed(session, category_names=("Food",))
        # Spend in only one of the six months before October.
        _txn(session, currency, accounts["Checking"], date(2026, 7, 10), -60000, "Big dinner", cats["Food"])
        session.commit()

    with session_factory() as session:
        view = budget.plan_rows(session, date(2026, 10, 1), averaging_months=6)
        food_row = next(r for r in view.rows if r.name == "Food")
        assert food_row.avg_minor == 10000  # 60000 / 6, not / 1


def test_plan_rows_last_month_is_the_month_right_before_the_plan_month(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, cats = _seed(session, category_names=("Food",))
        _txn(session, currency, accounts["Checking"], date(2026, 9, 15), -4000, "Dinner", cats["Food"])
        _txn(session, currency, accounts["Checking"], date(2026, 8, 15), -1000, "Older", cats["Food"])
        session.commit()

    with session_factory() as session:
        view = budget.plan_rows(session, date(2026, 10, 1), averaging_months=6)
        food_row = next(r for r in view.rows if r.name == "Food")
        assert food_row.last_month_minor == 4000


def test_plan_rows_first_ever_month_with_no_history_averages_zero(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed(session)
        session.commit()

    with session_factory() as session:
        view = budget.plan_rows(session, date(2026, 10, 1), averaging_months=6)
        assert view.rows == []
        assert view.income_avg_minor == 0
        assert view.income_last_month_minor == 0


def test_plan_rows_shows_a_budget_only_category_with_zero_history(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed(session, category_names=("Food",))
        budget.set_amount(session, date(2026, 10, 1), "Food", 50000)
        session.commit()

    with session_factory() as session:
        view = budget.plan_rows(session, date(2026, 10, 1), averaging_months=6)
        food_row = next(r for r in view.rows if r.name == "Food")
        assert food_row.avg_minor == 0
        assert food_row.last_month_minor == 0
        assert food_row.budget_minor == 50000


def test_plan_rows_includes_an_ancestor_of_a_budget_only_category_for_indentation(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed(session)
        categories.ensure_path(session, "Food > Dining")
        budget.set_amount(session, date(2026, 10, 1), "Food > Dining", 20000)
        session.commit()

    with session_factory() as session:
        view = budget.plan_rows(session, date(2026, 10, 1), averaging_months=6)
        names = {r.name: r for r in view.rows}
        assert "Food" in names  # shown for indentation even though it has no budget
        # No budget of its own, so it carries its only subcategory's, marked as derived.
        assert names["Food"].budget_minor == names["Dining"].budget_minor
        assert names["Food"].budget_derived and not names["Dining"].budget_derived
        assert names["Food"].depth == 0
        assert names["Dining"].depth == 1
        assert names["Dining"].parent_id == names["Food"].category_id


def test_plan_rows_uncategorized_appears_only_when_it_has_spend(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, _ = _seed(session)
        session.commit()

    with session_factory() as session:
        view = budget.plan_rows(session, date(2026, 10, 1), averaging_months=6)
        assert all(r.category_id != -1 for r in view.rows)

    with session_factory() as session:
        currency = session.scalar(select(Currency))
        account = session.scalar(select(Account).where(Account.name == "Checking"))
        _txn(session, currency, account, date(2026, 9, 1), -1500, "Mystery")
        session.commit()

    with session_factory() as session:
        view = budget.plan_rows(session, date(2026, 10, 1), averaging_months=6)
        uncategorized = next(r for r in view.rows if r.category_id == -1)
        assert uncategorized.name == stats.UNCATEGORIZED
        assert uncategorized.budget_minor is None
        assert uncategorized.last_month_minor == 1500


def test_plan_rows_income_average_and_target_unallocated(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        currency, accounts, cats = _seed(session, category_names=("Food",))
        _txn(session, currency, accounts["Checking"], date(2026, 9, 1), 500000, "Paycheck")
        budget.set_amount(session, date(2026, 10, 1), None, 600000, kind=budget.INCOME)
        budget.set_amount(session, date(2026, 10, 1), "Food", 50000)
        session.commit()

    with session_factory() as session:
        view = budget.plan_rows(session, date(2026, 10, 1), averaging_months=6)
        assert view.income_target_minor == 600000
        assert view.income_last_month_minor == 500000
        assert view.income_avg_minor == 500000 // 6
        assert view.total_budget_minor == 50000
        assert view.unallocated_minor == 600000 - 50000


def test_plan_rows_total_budget_does_not_double_count_parent_and_child(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed(session)
        categories.ensure_path(session, "Food > Dining")
        budget.set_amount(session, date(2026, 10, 1), "Food", 50000)
        budget.set_amount(session, date(2026, 10, 1), "Food > Dining", 10000)
        session.commit()

    with session_factory() as session:
        view = budget.plan_rows(session, date(2026, 10, 1), averaging_months=6)
        assert view.total_budget_minor == 50000  # not 60000


def test_a_parent_budget_smaller_than_its_subcategories_is_overcommitted(tmp_path):
    """Food 1,000 with Dining 900 + Groceries 400 beneath it: the parent cannot hold
    1,300. A grandchild under a budgeted child counts once, in that child."""
    from budget_tracker import categories

    session_factory = _session_factory(tmp_path)
    month = date(2026, 10, 1)
    with session_factory() as session:
        categories.ensure_path(session, "Food > Dining > Sushi")
        categories.ensure_path(session, "Food > Groceries")
        budget.set_amount(session, month, "Food", 100000)
        budget.set_amount(session, month, "Dining", 90000)
        budget.set_amount(session, month, "Sushi", 50000)   # under Dining, not Food
        budget.set_amount(session, month, "Groceries", 40000)
        session.commit()
        rows = {r.name: r for r in budget.plan_rows(session, month).rows}

    assert rows["Food"].subcategory_budget_minor == 130000
    assert rows["Food"].overcommitted
    assert rows["Dining"].subcategory_budget_minor == 50000
    assert not rows["Dining"].overcommitted
    assert not rows["Groceries"].overcommitted


def test_an_unbudgeted_parent_carries_the_sum_of_its_subcategories(tmp_path):
    """Food has no budget: it gets Dining (with Sushi inside it) + Groceries, derived.
    Typing its own amount replaces the derived one; totals never count it twice."""
    from budget_tracker import categories

    session_factory = _session_factory(tmp_path)
    month = date(2026, 10, 1)
    with session_factory() as session:
        categories.ensure_path(session, "Food > Dining > Sushi")
        categories.ensure_path(session, "Food > Groceries")
        budget.set_amount(session, month, "Dining", 90000)
        budget.set_amount(session, month, "Sushi", 50000)   # held by Dining
        budget.set_amount(session, month, "Groceries", 40000)
        session.commit()
        plan = {r.name: r for r in budget.plan_rows(session, month).rows}
        tracked = budget.track(session, month, today=date(2026, 10, 15))
        food_track = next(r for r in tracked.rows if r.name == "Food")

        assert (plan["Food"].budget_minor, plan["Food"].budget_derived) == (130000, True)
        assert not plan["Food"].overcommitted
        assert (food_track.budget_minor, food_track.budget_derived) == (130000, True)
        assert tracked.total_budget_minor == 130000  # Dining + Groceries, not doubled

        budget.set_amount(session, month, "Food", 150000)
        session.commit()
        plan = {r.name: r for r in budget.plan_rows(session, month).rows}
        assert (plan["Food"].budget_minor, plan["Food"].budget_derived) == (150000, False)
