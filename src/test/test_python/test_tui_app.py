"""Tests for BudgetApp's own state and command dispatch: the rename/categorize
shortcuts, transfers, filter, categorize/category commands, the category hierarchy,
and duplicate-category-name startup handling.

Panel *rendering* lives in its own ``test_tui_<panel>.py`` file; this one covers what
stays on ``BudgetApp`` itself per ``tui/app.py``.
"""

from __future__ import annotations

import asyncio
import datetime
import io
from decimal import Decimal

import pytest
from rich.console import Console
from sqlalchemy import text as sql_text
from textual.widgets import DataTable, Input, ListView, Static

from budget_tracker import categories, queries, rates, stats, transfers, vendors
from budget_tracker.db import DuplicateCategoryNamesError, get_engine, get_sessionmaker, init_db
from budget_tracker.importer import import_csv
from budget_tracker.models import Account, Currency, Transaction
from budget_tracker.tui import BudgetApp

from conftest import CSV, _category_of, _panel_state, _seed_category_hierarchy, _setup, _setup_recent
from helpers import learn_format


def test_shortcut_prefills_highlighted_raw_vendor(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            # Vendors sort by descending count, so index 1 is COFFEE SHOP A (2 txns).
            app.query_one("#vendors", ListView).index = 1
            await pilot.press("ctrl+n")
            return app.query_one("#command", Input).value

    assert asyncio.run(run()) == "rename COFFEE SHOP A = "


def test_shortcut_prefills_active_vendor_filter(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            raw = next(v for v in app._vendors if v.name == "COFFEE SHOP B")
            app.vendor_filter = (raw.kind, raw.id)
            # Highlight a different row to prove the filter wins over the cursor.
            app.query_one("#vendors", ListView).index = 1
            await pilot.press("ctrl+n")
            return app.query_one("#command", Input).value

    assert asyncio.run(run()) == "rename COFFEE SHOP B = "


def test_shortcut_on_override_group_prefills_verb_only(tmp_path, monkeypatch):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        vendors.set_override(session, "COFFEE SHOP A", "Coffee")
        vendors.set_override(session, "COFFEE SHOP B", "Coffee")

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            group = next(v for v in app._vendors if v.kind == "name")
            app.vendor_filter = (group.kind, group.id)
            await pilot.press("ctrl+n")
            return app.query_one("#command", Input).value

    assert asyncio.run(run()) == "rename "


def test_shortcut_uses_focused_transactions_cursor(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            table = app.query_one("#txns", DataTable)
            table.focus()
            # Transactions sort newest first, so row 0 is the 2025-07-03 COFFEE SHOP A.
            table.move_cursor(row=0)
            await pilot.press("ctrl+n")
            first = app.query_one("#command", Input).value

            table.focus()
            table.move_cursor(row=1)  # 2025-07-01 COFFEE SHOP B
            await pilot.press("ctrl+n")
            second = app.query_one("#command", Input).value
            return first, second

    assert asyncio.run(run()) == (
        "rename COFFEE SHOP A = ",
        "rename COFFEE SHOP B = ",
    )


def test_transaction_shortcut_uses_raw_name_not_display_name(tmp_path, monkeypatch):
    """A grouped vendor is still renameable from the table, which knows the raw string."""
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        vendors.set_override(session, "COFFEE SHOP A", "Coffee")

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            table = app.query_one("#txns", DataTable)
            row = next(i for i, t in enumerate(app._txns) if t.vendor == "Coffee")
            table.focus()
            table.move_cursor(row=row)
            await pilot.press("ctrl+n")
            return app.query_one("#command", Input).value

    # The sidebar would only know "Coffee", which set_override cannot match.
    assert asyncio.run(run()) == "rename COFFEE SHOP A = "


def test_sidebar_still_used_when_table_not_focused(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            # The command bar holds focus on mount; the table cursor must not win.
            app.query_one("#vendors", ListView).index = 2  # COFFEE SHOP B
            await pilot.press("ctrl+n")
            return app.query_one("#command", Input).value

    assert asyncio.run(run()) == "rename COFFEE SHOP B = "


def test_shortcut_with_no_vendor_selected_does_nothing(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app.query_one("#vendors", ListView).index = 0  # the "— All —" row
            await pilot.press("ctrl+n")
            return app.query_one("#command", Input).value

    assert asyncio.run(run()) == ""


# ------------------------------------------------------------- vendor sidebar cap


def _setup_many_vendors(tmp_path, monkeypatch, count):
    """A database with ``count`` distinct one-off vendors, "VENDOR 0000".."VENDOR NNNN".

    Every vendor has exactly one transaction, so queries.get_vendors' (-count,
    name.lower()) sort falls back to name order, and the zero-padding keeps that the
    same as numeric order -- a test can predict exactly which vendor sits at which
    sidebar row. Dated today so a ``stats 1m`` window (built relative to
    ``datetime.date.today()``, never hardcoded) picks all of them up.
    """
    db_path = tmp_path / "many_vendors.db"
    engine = get_engine(db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    today = datetime.date.today().isoformat()
    lines = ["Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit"]
    for i in range(count):
        lines.append(f"{today},{today},1234,VENDOR {i:04d},Dining,1.00,")
    csv_path = tmp_path / "many_vendors.csv"
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with session_factory() as session:
        learn_format(session, csv_path)
        import_csv(session, csv_path)
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def _vendor_sidebar_labels(app):
    return [
        str(item.children[0].content)
        for item in app.query_one("#vendors", ListView).children
    ]


def test_vendor_sidebar_below_the_cap_shows_every_vendor(tmp_path, monkeypatch):
    """Right at BudgetApp.VENDOR_SIDEBAR_CAP, nothing is hidden and there is no "more" row."""
    _setup_many_vendors(tmp_path, monkeypatch, BudgetApp.VENDOR_SIDEBAR_CAP)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            return _vendor_sidebar_labels(app)

    labels = asyncio.run(run())
    # "— All —" plus one row per vendor, and nothing else.
    assert len(labels) == BudgetApp.VENDOR_SIDEBAR_CAP + 1
    assert labels[1] == "VENDOR 0000 (1)"
    assert labels[-1] == f"VENDOR {BudgetApp.VENDOR_SIDEBAR_CAP - 1:04d} (1)"
    assert not any("more" in label for label in labels)


def test_vendor_sidebar_over_the_cap_truncates_and_notes_the_rest(tmp_path, monkeypatch):
    total = BudgetApp.VENDOR_SIDEBAR_CAP + 21
    _setup_many_vendors(tmp_path, monkeypatch, total)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            return _vendor_sidebar_labels(app), len(app._vendors)

    labels, vendor_count = asyncio.run(run())
    assert vendor_count == total
    # "— All —" plus the capped vendors plus one trailing summary row.
    assert len(labels) == BudgetApp.VENDOR_SIDEBAR_CAP + 2
    # The highest-traffic (here: alphabetically first, since all counts tie) vendors
    # are the ones kept, not an arbitrary or reshuffled subset.
    assert labels[1] == "VENDOR 0000 (1)"
    assert labels[BudgetApp.VENDOR_SIDEBAR_CAP] == (
        f"VENDOR {BudgetApp.VENDOR_SIDEBAR_CAP - 1:04d} (1)"
    )
    last = labels[-1]
    assert "21 more" in last
    assert "filter vendor:" in last


def test_vendor_sidebar_more_row_fits_the_sidebar_width(tmp_path, monkeypatch):
    """The summary row must not run past the sidebar's own item width, or Textual
    hard-clips it mid-word with no ellipsis -- see formatting._truncate."""
    _setup_many_vendors(tmp_path, monkeypatch, BudgetApp.VENDOR_SIDEBAR_CAP + 8000)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            item = app.query_one("#vendors", ListView).children[-1]
            label = str(item.children[0].content)
            width = item.content_size.width
            return label, width

    label, width = asyncio.run(run())
    assert "8000 more" in label
    assert len(label) <= width


def test_clicking_the_vendor_sidebar_more_row_does_not_filter(tmp_path, monkeypatch):
    _setup_many_vendors(tmp_path, monkeypatch, BudgetApp.VENDOR_SIDEBAR_CAP + 5)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            vendors_list = app.query_one("#vendors", ListView)
            vendors_list.focus()
            vendors_list.index = len(vendors_list.children) - 1  # the "N more" row
            await pilot.press("enter")
            await pilot.pause()
            return app.vendor_filter, [n.message for n in app._notifications]

    vendor_filter, messages = asyncio.run(run())
    assert vendor_filter is None
    assert any("filter vendor:" in message for message in messages)


def test_shortcut_on_the_vendor_sidebar_more_row_falls_back(tmp_path, monkeypatch):
    """ctrl+n on the summary row must not resolve to whatever real vendor happens to
    sit at that row's index -- see BudgetApp._selected_vendor."""
    _setup_many_vendors(tmp_path, monkeypatch, BudgetApp.VENDOR_SIDEBAR_CAP + 5)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            vendors_list = app.query_one("#vendors", ListView)
            vendors_list.index = len(vendors_list.children) - 1  # the "N more" row
            await pilot.press("ctrl+n")
            return app.query_one("#command", Input).value

    assert asyncio.run(run()) == ""


def test_vendor_sidebar_survives_a_drill_round_trip_over_the_cap(tmp_path, monkeypatch):
    """A statistics drill-down and return must leave the capped list exactly as it
    was, not stale and not rebuilt into something wrong."""
    _setup_many_vendors(tmp_path, monkeypatch, BudgetApp.VENDOR_SIDEBAR_CAP + 5)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 1m")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.move_cursor(row=0)
            before = _vendor_sidebar_labels(app)
            await pilot.press("right")
            await pilot.pause()
            await pilot.press("left")
            await pilot.pause()
            after = _vendor_sidebar_labels(app)
            return before, after

    before, after = asyncio.run(run())
    assert before == after
    assert len(after) == BudgetApp.VENDOR_SIDEBAR_CAP + 2
    assert "5 more" in after[-1]


def test_vendor_sidebar_more_count_updates_when_new_vendors_cross_the_cap(
    tmp_path, monkeypatch
):
    """The trailing row's own count depends on the hidden total, so a real change in
    that total (a fresh import, here) must still reach the screen -- _fill_list's
    cache guard compares the *rendered* labels, and the summary text is one of them."""
    session_factory = _setup_many_vendors(tmp_path, monkeypatch, BudgetApp.VENDOR_SIDEBAR_CAP)
    today = datetime.date.today().isoformat()
    extra_csv = tmp_path / "extra.csv"
    extra_csv.write_text(
        "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\n"
        f"{today},{today},1234,VENDOR EXTRA,Dining,1.00,\n",
        encoding="utf-8",
    )

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            before = _vendor_sidebar_labels(app)
            with session_factory() as session:
                import_csv(session, extra_csv)
                session.commit()
            app.reload()
            await pilot.pause()
            after = _vendor_sidebar_labels(app)
            return before, after

    before, after = asyncio.run(run())
    assert not any("more" in label for label in before)
    assert "1 more" in after[-1]


def _seed_same_account_transfer_candidates(tmp_path, monkeypatch):
    """Two legs in one account, same amount and opposite sign, a day apart.

    Only pairable with ``allow_same_account`` — the default rule requires different
    accounts, so this fixture stays unpaired until ``transfers same-account`` runs.
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


def test_transfers_default_does_not_pair_same_account_legs(tmp_path, monkeypatch):
    _seed_same_account_transfer_candidates(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("transfers")
            await pilot.pause()
            messages = [n.message for n in app._notifications]
            return messages, app._totals.transfer_count

    messages, transfer_count = asyncio.run(run())
    assert any("Found 0 new transfer pair(s)" in m for m in messages)
    assert transfer_count == 0


def test_transfers_same_account_command_pairs_them(tmp_path, monkeypatch):
    _seed_same_account_transfer_candidates(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("transfers same-account")
            await pilot.pause()
            messages = [n.message for n in app._notifications]
            return messages, app._totals.transfer_count

    messages, transfer_count = asyncio.run(run())
    assert any(
        "Found 1 new transfer pair(s) (same-account allowed)" in m for m in messages
    )
    assert transfer_count == 2


def test_transfers_reset_undoes_a_same_account_pairing(tmp_path, monkeypatch):
    _seed_same_account_transfer_candidates(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("transfers same-account")
            await pilot.pause()
            paired_count = app._totals.transfer_count

            app._run_command("transfers reset")
            await pilot.pause()
            messages = [n.message for n in app._notifications]
            return paired_count, messages, app._totals.transfer_count

    paired_count, messages, reset_count = asyncio.run(run())
    assert paired_count == 2
    assert any("Un-paired 2 transaction(s)." in m for m in messages)
    assert reset_count == 0


def test_transfers_unknown_option_warns_and_changes_nothing(tmp_path, monkeypatch):
    _seed_same_account_transfer_candidates(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("transfers bogus")
            await pilot.pause()
            messages = [n.message for n in app._notifications]
            return messages, app._totals.transfer_count

    messages, transfer_count = asyncio.run(run())
    assert any("Unknown transfers option" in m and "bogus" in m for m in messages)
    assert transfer_count == 0


def test_filter_command_searches_all_three_fields(tmp_path, monkeypatch):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        vendors.set_override(session, "COFFEE SHOP A", "Beanery")

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("filter Beanery")
            await pilot.pause()
            matched = [t.description for t in app._txns]
            status = _panel_state(app)[2]

            app._run_command("filter")  # bare filter clears it
            await pilot.pause()
            return matched, status, len(app._txns), app.text_filter

    matched, status, cleared_count, text_filter = asyncio.run(run())
    assert matched == ["COFFEE SHOP A", "COFFEE SHOP A"]  # found via display name
    assert 'all~"Beanery"' in status  # the filter is visible in the status line
    assert cleared_count == 3 and text_filter is None


def test_filter_command_can_target_one_field(tmp_path, monkeypatch):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        vendors.set_override(session, "COFFEE SHOP A", "Beanery")

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            results = {}
            for command in ("vendor:Beanery", "raw:Beanery", "description:SHOP B"):
                app._run_command(f"filter {command}")
                await pilot.pause()
                results[command] = [t.description for t in app._txns]
            return results

    results = asyncio.run(run())
    assert results["vendor:Beanery"] == ["COFFEE SHOP A", "COFFEE SHOP A"]
    assert results["raw:Beanery"] == []  # the raw string is not "Beanery"
    assert results["description:SHOP B"] == ["COFFEE SHOP B"]


def test_unprefixed_text_with_a_colon_is_searched_literally(tmp_path, monkeypatch):
    """`filter POS-: MTA` must not be read as a field prefix."""
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("filter nonsense:SHOP A")
            await pilot.pause()
            return [t.description for t in app._txns], app.text_filter

    matched, text_filter = asyncio.run(run())
    assert text_filter.field == "all"
    assert text_filter.text == "nonsense:SHOP A"  # kept whole
    assert matched == []


def test_clear_filters_also_clears_the_text_filter(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("filter SHOP B")
            await pilot.pause()
            filtered = len(app._txns)
            await pilot.press("ctrl+l")
            await pilot.pause()
            return filtered, len(app._txns), app.text_filter

    filtered, after, text_filter = asyncio.run(run())
    assert filtered == 1
    assert after == 3 and text_filter is None


# ------------------------------------------------------------------- statistics
# Windows anchor to today, so anything a preset window has to contain is dated
# relative to today rather than written into a fixture.


def test_categorize_command_categorises_a_vendor(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("categorize COFFEE SHOP A = Coffee")
            await pilot.pause()
            sidebar = [
                str(item.children[0].content)
                for item in app.query_one("#categories", ListView).children
            ]
            return (
                _category_of(app, "COFFEE SHOP A"),
                _category_of(app, "COFFEE SHOP B"),
                sidebar,
                [n.message for n in app._notifications],
            )

    shop_a, shop_b, sidebar, messages = asyncio.run(run())
    assert shop_a == "Coffee"  # both of that vendor's rows, reloaded into the table
    assert shop_b == "Dining"  # the bank's own category, untouched
    assert any("Coffee (2)" in label for label in sidebar)
    assert any("2 transactions categorised" in m for m in messages)


def test_categorize_with_a_blank_category_clears_it(tmp_path, monkeypatch):
    """The undo mirrors a bare `filter`: nothing on the right of the `=`."""
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("categorize COFFEE SHOP A = Coffee")
            await pilot.pause()
            before = _category_of(app, "COFFEE SHOP A")

            app._run_command("categorize COFFEE SHOP A =")
            await pilot.pause()
            return before, _category_of(app, "COFFEE SHOP A"), [
                n.message for n in app._notifications
            ]

    before, after, messages = asyncio.run(run())
    assert before == "Coffee"
    assert after == ""  # back to uncategorised, not left on the old category
    assert any("cleared the category on 2 transactions" in m for m in messages)


def test_categorize_accepts_a_display_name(tmp_path, monkeypatch):
    """A display name covers the whole override group, as filtering does."""
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        vendors.set_override(session, "COFFEE SHOP A", "Coffee")
        vendors.set_override(session, "COFFEE SHOP B", "Coffee")

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("cat Coffee = Dining out")  # `cat` is the short alias
            await pilot.pause()
            return [t.category for t in app._txns]

    assert asyncio.run(run()) == ["Dining out"] * 3


def test_categorize_an_unknown_vendor_notifies(tmp_path, monkeypatch):
    """0 rows means "no such vendor", which is not the same as "nothing to do"."""
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("categorize NOT A VENDOR = Coffee")
            await pilot.pause()
            return [(n.message, n.severity) for n in app._notifications], [
                t.category for t in app._txns
            ]

    notifications, category_cells = asyncio.run(run())
    message, severity = notifications[-1]
    assert "No vendor named 'NOT A VENDOR'" in message
    assert severity == "error"
    assert "categorised" not in message  # not reported as a successful 0
    assert category_cells == ["Dining"] * 3


def test_categorize_without_an_equals_sign_warns(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("categorize COFFEE SHOP A Coffee")
            app._run_command("categorize rule COFFEE*")
            await pilot.pause()
            return [(n.message, n.severity) for n in app._notifications]

    notifications = asyncio.run(run())
    assert notifications[0] == (
        "Usage: categorize <vendor> = <category>   (blank category undoes it)",
        "warning",
    )
    assert notifications[1] == (
        "Usage: categorize rule <pattern> = <category>",
        "warning",
    )


def test_categorize_shortcut_prefills_from_the_transaction_cursor(tmp_path, monkeypatch):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        vendors.set_override(session, "COFFEE SHOP A", "Coffee")

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            table = app.query_one("#txns", DataTable)
            row = next(i for i, t in enumerate(app._txns) if t.vendor == "Coffee")
            table.focus()
            table.move_cursor(row=row)
            await pilot.press("ctrl+t")
            return app.query_one("#command", Input).value

    # The raw string, as ctrl+n does: the row remembers it even once grouped.
    assert asyncio.run(run()) == "categorize COFFEE SHOP A = "


def test_categorize_shortcut_falls_back_to_the_sidebar(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            # The command bar holds focus on mount, so the table cursor must not win.
            app.query_one("#vendors", ListView).index = 2  # COFFEE SHOP B
            await pilot.press("ctrl+t")
            highlighted = app.query_one("#command", Input).value

            raw = next(v for v in app._vendors if v.name == "COFFEE SHOP A")
            app.vendor_filter = (raw.kind, raw.id)
            await pilot.press("ctrl+t")
            return highlighted, app.query_one("#command", Input).value

    assert asyncio.run(run()) == (
        "categorize COFFEE SHOP B = ",
        "categorize COFFEE SHOP A = ",  # the active filter wins over the cursor
    )


def test_categorize_shortcut_on_an_override_group_prefills_the_verb(tmp_path, monkeypatch):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        vendors.set_override(session, "COFFEE SHOP A", "Coffee")
        vendors.set_override(session, "COFFEE SHOP B", "Coffee")

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            group = next(v for v in app._vendors if v.kind == "name")
            app.vendor_filter = (group.kind, group.id)
            await pilot.press("ctrl+t")
            return app.query_one("#command", Input).value

    assert asyncio.run(run()) == "categorize "


def test_footer_shows_every_shortcut_at_a_normal_width(tmp_path, monkeypatch):
    """Textual's Footer truncates mid-word instead of dropping whole entries, so one
    verbose label silently hides every binding after it — which is how "Fold/unfold"
    became "F" and the fold-all key never appeared at all. The last binding is the
    canary: if it is whole, nothing earlier was cut.
    """
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 1m")
            await pilot.pause()
            buffer = io.StringIO()
            Console(file=buffer, width=130).print(app.screen._compositor)
            return buffer.getvalue()

    rendered = asyncio.run(run())
    footer = next(line for line in rendered.splitlines() if "palette" in line)
    # Every shortcut the statistics panel offers, the last one included.
    for label in ("Refresh", "Clear", "Rename", "Categorise", "Transactions", "Fold all"):
        assert label in footer, f"{label!r} missing or truncated: {footer.strip()!r}"


def test_categories_sidebar_indents_by_depth(tmp_path, monkeypatch):
    """A parent's rolled-up count must read as nested, not as a flat, mysteriously
    large category next to its own children."""
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            return [
                str(item.children[0].content)
                for item in app.query_one("#categories", ListView).children
            ]

    sidebar = asyncio.run(run())
    food = next(label for label in sidebar if label.startswith("Food ("))
    dining = next(label for label in sidebar if "Dining (" in label)
    groceries = next(label for label in sidebar if "Groceries (" in label)
    fast_food = next(label for label in sidebar if "Fast Food (" in label)
    assert food.startswith("Food (")  # depth 0: no indentation
    assert dining.startswith("  Dining (")  # depth 1
    assert groceries.startswith("  Groceries (")  # depth 1
    assert fast_food.startswith("    Fast Food (")  # depth 2


def test_category_filter_matches_the_whole_subtree(tmp_path, monkeypatch):
    """Filtering by a parent category has to pull in every descendant's transactions.

    _txn_query's recursive CTE is supposed to guarantee this already; this pins it at
    the level the app actually exercises it, rather than trusting the claim.
    """
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            food = next(c for c in app._categories if c.name == "Food")
            app.category_filter = food.id
            app.reload()
            await pilot.pause()
            return sorted(t.description for t in app._txns)

    descriptions = asyncio.run(run())
    # Food itself holds nothing directly; all three transactions live under its
    # descendants (Dining, Restaurants, Fast Food, Groceries).
    assert descriptions == ["Big meal", "Fast food", "Groceries"]


def test_category_command_asks_for_confirmation_before_a_relocation(tmp_path, monkeypatch):
    """``Dining`` already exists (top-level, from the CSV's bank-supplied category) and
    has transactions, so nesting it under a new ``Food`` is a relocation of that whole
    category — names are unique across the whole tree, so it cannot mean a second one —
    and has to be confirmed before it happens."""
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("category Food > Dining > Restaurants")
            await pilot.pause()
            prompt = app.query_one("#prompt", Static)
            depths = {c.name: c.depth for c in app._categories}
            return str(prompt.content), prompt.display, app._pending_category, depths

    text, visible, pending, depths = asyncio.run(run())
    assert visible is True and pending == "Food > Dining > Restaurants"
    assert "'Dining'" in text and "3 transaction(s)" in text
    assert "distinct name" in text
    assert depths["Dining"] == 0  # nothing moved yet


def test_category_command_confirmed_relocates_the_existing_category(tmp_path, monkeypatch):
    """``ensure_path`` rescues ``Dining`` into place under the new ``Food`` rather than
    forking a duplicate, once the relocation is confirmed."""
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("category Food > Dining > Restaurants")
            await pilot.pause()
            app.query_one("#command", Input).value = "yes"
            await pilot.press("enter")
            await pilot.pause()
            sidebar = [
                str(item.children[0].content)
                for item in app.query_one("#categories", ListView).children
            ]
            depths = {c.name: c.depth for c in app._categories}
            return [n.message for n in app._notifications], sidebar, depths, app._pending_category

    messages, sidebar, depths, pending = asyncio.run(run())
    assert pending is None
    assert any("Food > Dining > Restaurants" in m for m in messages)
    assert depths["Food"] == 0
    assert depths["Dining"] == 1  # rescued under Food, still carrying its 3 transactions
    # Restaurants has no transactions of its own yet, so get_categories (rolled up)
    # correctly omits it — this just proves the sidebar still renders, indented.
    assert any(label.startswith("Food (") for label in sidebar)
    assert any(label.startswith("  Dining (") for label in sidebar)


def test_category_command_relocation_cancelled_leaves_it_in_place(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("category Food > Dining > Restaurants")
            await pilot.pause()
            app.query_one("#command", Input).value = "no thanks"
            await pilot.press("enter")
            await pilot.pause()
            messages = [n.message for n in app._notifications]
            depths = {c.name: c.depth for c in app._categories}
            return messages, depths, app._pending_category, app.query_one(
                "#prompt", Static
            ).display

    messages, depths, pending, prompt_visible = asyncio.run(run())
    assert any("Category move cancelled" in m for m in messages)
    assert pending is None
    assert prompt_visible is False
    assert depths["Dining"] == 0  # untouched: still top-level


def test_category_command_relocation_escape_cancels(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("category Food > Dining > Restaurants")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            messages = [n.message for n in app._notifications]
            depths = {c.name: c.depth for c in app._categories}
            return messages, depths, app._pending_category

    messages, depths, pending = asyncio.run(run())
    assert any("Category move cancelled" in m for m in messages)
    assert pending is None
    assert depths["Dining"] == 0


def test_category_command_one_element_moves_to_top_level(tmp_path, monkeypatch):
    # The fixture CSV's rows import with the bank's own top-level "Dining" category;
    # nesting it under "Food" rescues that existing (and populated) category into place
    # rather than creating an unrelated new one (see categories.ensure_path).
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        categories.ensure_path(session, "Food > Dining", confirm_relocation=True)
        session.commit()

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            before = next(c.depth for c in app._categories if c.name == "Dining")
            app._run_command("category Dining")  # also a relocation: back to the top
            await pilot.pause()
            app.query_one("#command", Input).value = "yes"
            await pilot.press("enter")
            await pilot.pause()
            after = next(c.depth for c in app._categories if c.name == "Dining")
            return before, after

    before, after = asyncio.run(run())
    assert before == 1  # nested under Food
    assert after == 0  # moved to the top level


def test_category_command_bare_shows_the_tree(tmp_path, monkeypatch):
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("category")
            await pilot.pause()
            list_messages = [n.message for n in app._notifications]

            app._run_command("category list")
            await pilot.pause()
            return list_messages, [n.message for n in app._notifications]

    bare_messages, list_messages = asyncio.run(run())
    assert any("Food" in m and "Dining" in m for m in bare_messages)
    assert any("Food" in m and "Dining" in m for m in list_messages)


def test_category_command_reports_a_cycle_without_crashing(tmp_path, monkeypatch):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        categories.ensure_path(session, "Food > Dining", confirm_relocation=True)
        session.commit()

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            # Food already has Dining as a child; asking to move Food under Dining
            # (still under Food) makes Food its own descendant's descendant — a cycle.
            # Food itself relocating (top level -> under Food > Dining) is confirmed
            # first; the cycle check underneath that is what actually rejects it.
            app._run_command("category Food > Dining > Food")
            await pilot.pause()
            app.query_one("#command", Input).value = "yes"
            await pilot.press("enter")
            await pilot.pause()
            return (
                [n.message for n in app._notifications],
                app._panel,
                next(c.depth for c in app._categories if c.name == "Food"),
            )

    messages, panel, food_depth = asyncio.run(run())
    assert any("cycle" in m for m in messages)
    assert panel == "txns"  # the app is still up, on the panel it started on
    assert food_depth == 0  # the rejected move left Food where it was


# ------------------------------------------------------------- category hierarchy (merge)


def test_category_merge_asks_for_confirmation_naming_what_will_move(tmp_path, monkeypatch):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        categories.ensure_path(session, "Snacks")
        session.commit()

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("category merge Dining = Snacks")
            await pilot.pause()
            prompt = app.query_one("#prompt", Static)
            return str(prompt.content), prompt.display, app._pending_category_merge

    text, visible, pending = asyncio.run(run())
    assert visible is True and pending == ("Dining", "Snacks")
    assert "'Dining'" in text and "'Snacks'" in text
    assert "3 transaction(s)" in text


def test_category_merge_confirmed_moves_everything_and_deletes_the_source(
    tmp_path, monkeypatch
):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        categories.ensure_path(session, "Snacks")
        session.commit()

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("category merge Dining = Snacks")
            await pilot.pause()
            app.query_one("#command", Input).value = "yes"
            await pilot.press("enter")
            await pilot.pause()
            messages = [n.message for n in app._notifications]
            return messages, app._pending_category_merge

    messages, pending = asyncio.run(run())
    assert pending is None
    assert any(
        "Merged 'Dining' into 'Snacks'" in m and "3 transaction(s)" in m for m in messages
    )
    with session_factory() as session:
        assert categories.resolve_path(session, "Dining") is None
        assert queries.resolve_category(session, "Snacks") is not None


def test_category_merge_cancelled_moves_nothing(tmp_path, monkeypatch):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        categories.ensure_path(session, "Snacks")
        session.commit()

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("category merge Dining = Snacks")
            await pilot.pause()
            app.query_one("#command", Input).value = "no"
            await pilot.press("enter")
            await pilot.pause()
            messages = [n.message for n in app._notifications]
            return messages, app._pending_category_merge

    messages, pending = asyncio.run(run())
    assert pending is None
    assert any("Merge cancelled" in m for m in messages)
    with session_factory() as session:
        assert categories.resolve_path(session, "Dining") is not None


def test_category_merge_reports_an_unknown_category(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("category merge Nope = Dining")
            await pilot.pause()
            return [n.message for n in app._notifications], app._pending_category_merge

    messages, pending = asyncio.run(run())
    assert any("No category named 'Nope'" in m for m in messages)
    assert pending is None


# ------------------------------------------------------ duplicate category names (startup)


def _legacy_duplicate_category_db(tmp_path):
    """A database predating unique category names, with one name used twice — the
    shape :func:`db.init_db` refuses to open (see test_db.py, which owns that module)."""
    db_path = tmp_path / "legacy.db"
    engine = get_engine(db_path)
    with engine.begin() as connection:
        connection.execute(
            sql_text(
                "CREATE TABLE category ("
                "id INTEGER PRIMARY KEY, parent_id INTEGER, value VARCHAR, "
                "created_at TIMESTAMP, updated_at TIMESTAMP, "
                "CONSTRAINT uq_category_parent_value UNIQUE (parent_id, value))"
            )
        )
        connection.execute(sql_text("INSERT INTO category (value) VALUES ('Airfare')"))
        connection.execute(sql_text("INSERT INTO category (value) VALUES ('Airfare')"))
    return db_path


def test_duplicate_category_names_block_startup_with_a_pointer_to_merge(
    tmp_path, monkeypatch
):
    """Today this is a raw traceback with no path forward; it has to at least name the
    duplicate and point at the command that fixes it."""
    db_path = _legacy_duplicate_category_db(tmp_path)
    monkeypatch.setenv("BUDGET_DB", str(db_path))

    with pytest.raises(DuplicateCategoryNamesError) as exc_info:
        BudgetApp()

    message = str(exc_info.value)
    assert "Airfare" in message
    assert "category merge" in message


# --------------------------------------------------------------------------- rates

def _setup_multi_currency_for_rates(tmp_path, monkeypatch):
    """A USD and a CHF account, one transaction each, with no rate cached -- so `rates
    fetch` has something real to do and the transactions status line has something
    genuinely unconverted to report before it runs."""
    db_path = tmp_path / "rates_multi.db"
    engine = get_engine(db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        usd = Currency(value="USD", symbol="$", decimal_places=2)
        chf = Currency(value="CHF", symbol="CHF", decimal_places=2)
        session.add_all([usd, chf])
        session.flush()
        checking = Account(name="Checking", currency_id=usd.id)
        swiss = Account(name="Swiss", currency_id=chf.id)
        session.add_all([checking, swiss])
        session.flush()
        session.add(
            Transaction(
                account_id=checking.id, currency_id=usd.id,
                posted_date=datetime.date(2025, 6, 1), description="US",
                raw_description="US", value_minor=-1000, import_hash="rm-us",
            )
        )
        session.add(
            Transaction(
                account_id=swiss.id, currency_id=chf.id,
                posted_date=datetime.date(2025, 6, 2), description="CH",
                raw_description="CH", value_minor=-2000, import_hash="rm-ch",
            )
        )
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def test_rates_command_with_nothing_cached_says_so(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("rates")
            await pilot.pause()
            return [n.message for n in app._notifications]

    messages = asyncio.run(run())
    assert any("No exchange rates cached yet" in m for m in messages)


def test_rates_command_lists_what_is_cached(tmp_path, monkeypatch):
    """Reuses queries.get_exchange_rates -- the same aggregation 'budget rates' prints
    -- so the two never drift."""
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        rates.record_rate(
            session, datetime.date(2025, 7, 1), "USD", "CHF", Decimal("0.88"), rates.ECB
        )
        rates.record_rate(
            session, datetime.date(2025, 7, 5), "USD", "CHF", Decimal("0.89"), rates.ECB
        )
        session.commit()

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("rates")
            await pilot.pause()
            return [n.message for n in app._notifications]

    messages = asyncio.run(run())
    assert any(
        "USD -> CHF" in m and "2025-07-01..2025-07-05" in m and "2 rates" in m
        for m in messages
    )


def test_rates_fetch_command_writes_what_the_stub_returns_and_refreshes(
    tmp_path, monkeypatch
):
    """Runs in a worker (never blocks the event loop) and reloads once it lands, so a
    figure the fetch just fixed shows up without a separate 'refresh'."""
    session_factory = _setup_multi_currency_for_rates(tmp_path, monkeypatch)

    def stub(session, start, end, base, quotes, **kwargs):
        written = 0
        for quote in quotes:
            rates.record_rate(session, start, base, quote, Decimal("0.9"), rates.ECB)
            written += 1
        return written

    monkeypatch.setattr(rates, "fetch_ecb_rates", stub)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            before = str(app.query_one("#status", Static).content)
            app._run_command("rates fetch")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            after = str(app.query_one("#status", Static).content)
            return before, after, [n.message for n in app._notifications]

    before, after, messages = asyncio.run(run())
    assert "unconverted" in before
    assert "unconverted" not in after
    assert any("Fetched 1 rate(s) for USD -> CHF" in m for m in messages)


def test_rates_fetch_command_with_only_one_currency_reports_nothing_to_fetch(
    tmp_path, monkeypatch
):
    _setup(tmp_path, monkeypatch)  # single-currency fixture

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("rates fetch")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            return [n.message for n in app._notifications]

    messages = asyncio.run(run())
    assert any("nothing to fetch" in m for m in messages)


def test_rates_unknown_subcommand_warns(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("rates bogus")
            await pilot.pause()
            return [(n.message, n.severity) for n in app._notifications]

    messages = asyncio.run(run())
    assert any("Usage: rates" in m and severity == "warning" for m, severity in messages)


# ------------------------------------------------------------------------- unimport
