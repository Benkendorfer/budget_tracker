"""Category rules re-run after anything that changes what they would match.

A category rule matches a vendor's raw text *or* its display name, so a rename, a
vendor rule, or clearing a manual category can each put rows under a rule. Before this,
only an import or a category-rule command re-ran the rules, so those rows sat
uncategorized until the next one -- e.g. after `categorize Spotify =`, which the README
promises "hands it back to the rules".
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from budget_tracker import categories, cli
from budget_tracker.models import Category, Transaction
from budget_tracker.tui import BudgetApp

from conftest import _setup


def _categories_of(session_factory):
    with session_factory() as session:
        return sorted(
            (session.get(Category, t.category_id).value if t.category_id else "-")
            for t in session.scalars(select(Transaction))
        )


def test_a_vendor_rule_lets_a_display_name_category_rule_fire(tmp_path, monkeypatch):
    """Category rule first (against a name nothing has yet), vendor rule second."""
    session_factory = _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("rule categorize Coffee = Treats")
            await pilot.pause()
            app._run_command("rule COFFEE SHOP* = Coffee")
            await pilot.pause()

    asyncio.run(run())
    assert _categories_of(session_factory) == ["Treats"] * 3


def test_a_rename_lets_a_display_name_category_rule_fire(tmp_path, monkeypatch):
    session_factory = _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("rule categorize Beans = Treats")
            await pilot.pause()
            app._run_command("rename COFFEE SHOP B = Beans")
            await pilot.pause()

    asyncio.run(run())
    # Only SHOP B's one row was renamed; the two SHOP A rows keep the bank's Dining.
    assert _categories_of(session_factory) == ["Dining", "Dining", "Treats"]


def test_clearing_a_manual_category_hands_the_rows_to_the_rules(tmp_path, monkeypatch):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        categories.set_category(session, "COFFEE SHOP A", "Hand Picked")
        categories.add_rule(session, "COFFEE*", "Treats")
        categories.apply_category_rules(session)
        session.commit()
    # The manual category outranks the rule...
    assert _categories_of(session_factory) == ["Hand Picked", "Hand Picked", "Treats"]

    with session_factory() as session:
        categories.clear_category(session, "COFFEE SHOP A")
        session.commit()
    # ...and clearing it gives the rows to the rule at once, not at the next import.
    assert _categories_of(session_factory) == ["Treats"] * 3


def test_cli_rule_add_lets_a_display_name_category_rule_fire(tmp_path, monkeypatch):
    session_factory = _setup(tmp_path, monkeypatch)
    assert cli.main(["category-rule", "add", "Coffee", "Treats"]) == 0
    assert cli.main(["rule", "add", "COFFEE SHOP*", "Coffee"]) == 0
    assert _categories_of(session_factory) == ["Treats"] * 3
