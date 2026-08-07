"""Tests for the TUI's rename shortcut (ctrl+n).

Driven through Textual's ``run_test`` pilot. ``asyncio.run`` wraps each case so the
suite does not need pytest-asyncio.
"""

import asyncio
import datetime
import io

import pytest
from rich.console import Console
from sqlalchemy import text as sql_text
from textual.widgets import DataTable, Input, ListView, Static

from budget_tracker import categories, charts, formats, queries, stats, transfers, vendors
from budget_tracker.db import DuplicateCategoryNamesError, get_engine, get_sessionmaker, init_db
from budget_tracker.importer import import_csv, read_header_and_rows
from budget_tracker.models import Account, Currency, Transaction
from helpers import learn_format
from budget_tracker.tui import FOLD_INDICATOR, TRANSFER_MARK, BudgetApp, _fmt_amount

CSV = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2025-07-01,2025-07-02,8207,COFFEE SHOP A,Dining,3.00,
2025-07-01,2025-07-02,8207,COFFEE SHOP B,Dining,4.00,
2025-07-03,2025-07-04,8207,COFFEE SHOP A,Dining,3.50,
"""


def _setup(tmp_path, monkeypatch):
    """Seed a DB and point the app at it via the BUDGET_DB override."""
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


UNKNOWN_LAYOUT_CSV = "Booking Date,Counterparty,Turnover\n2026-07-23,SOMEONE,-200.00\n"

# Same layout as CSV, but rows that are not in the seeded database yet.
NEW_ROWS_CSV = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2025-09-01,2025-09-02,8207,NEW BAKERY,Dining,5.00,
2025-09-03,2025-09-04,8207,NEW BAKERY,Dining,6.00,
"""


def _inbox(tmp_path, monkeypatch, **files):
    inbox = tmp_path / "to_import"
    inbox.mkdir()
    for name, text in files.items():
        (inbox / f"{name}.csv").write_text(text, encoding="utf-8")
    monkeypatch.setattr("budget_tracker.tui.TO_IMPORT_DIR", inbox)
    return inbox


def _rows_of(app, table_id):
    table = app.query_one(f"#{table_id}", DataTable)
    return [[str(c) for c in table.get_row_at(i)] for i in range(table.row_count)]


def test_import_command_opens_a_file_picker(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _inbox(tmp_path, monkeypatch, known=CSV, unknown=UNKNOWN_LAYOUT_CSV)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("import")
            await pilot.pause()
            return _panel_state(app), _rows_of(app, "imports"), app.focused.id

    state, rows, focused = asyncio.run(run())
    assert state[0] is False  # transactions gave up the panel
    assert focused == "imports"
    # Files sort by name; the row count and what blocks each file are both shown.
    assert rows[0][:2] == ["known.csv", "3"]
    assert rows[0][2] == "test_layout"  # a recognised layout, ready to import
    assert rows[1][0] == "unknown.csv"
    assert rows[1][2] == "needs setup"
    assert "2 file(s), 1 ready" in state[2] and "enter to import" in state[2]


def test_enter_imports_the_highlighted_file(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _inbox(tmp_path, monkeypatch, fresh=NEW_ROWS_CSV)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            before = len(app._txns)
            app._run_command("import")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            messages = [n.message for n in app._notifications]
            return before, len(app._txns), messages, _panel_state(app)

    before, after, messages, state = asyncio.run(run())
    assert after == before + 2  # the two new rows landed
    assert any("fresh.csv: 2 added, 0 skipped" in m for m in messages)
    assert state[1] is False and state[0] is False  # still on the imports panel


def test_enter_on_an_unknown_layout_starts_the_walkthrough(tmp_path, monkeypatch):
    """An unseen layout is set up in the app, not punted to the CLI."""
    _setup(tmp_path, monkeypatch)
    _inbox(tmp_path, monkeypatch, unknown=UNKNOWN_LAYOUT_CSV)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("import")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            return app._panel, app._setup.question, _panel_state(app)[2]

    panel, question, status = asyncio.run(run())
    assert panel == "setup"
    assert question.field == "name"  # names the layout first
    assert "unknown.csv" in status and "escape cancels" in status


def test_walkthrough_defines_the_layout_then_imports(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _inbox(tmp_path, monkeypatch, unknown=UNKNOWN_LAYOUT_CSV)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            before = len(app._txns)
            app._run_command("import")
            await pilot.pause()
            await pilot.press("enter")  # begin setup for unknown.csv
            await pilot.pause()

            asked = []
            # Answer by column name, except one answered by picking a numbered row.
            answers = {
                "name": "euro",
                "amount_column": "Turnover",
                "posted_date_column": "1",  # by number, as shown in the panel
                "description_column": "Counterparty",
                # A single signed amount column, so the format also asks which way its
                # sign points; -200.00 in the sample already matches our convention.
                "invert_amount": "no",
                "__account": "Euro Account",
            }
            for _ in range(8):
                question = app._setup.question if app._setup else None
                if question is None:
                    break
                asked.append(question.field)
                app._answer_setup(answers[question.field])
                await pilot.pause()
            return before, len(app._txns), asked, app._panel

    before, after, asked, panel = asyncio.run(run())
    assert asked == [
        "name",
        "amount_column",
        "posted_date_column",
        "description_column",
        "invert_amount",
        "__account",
    ]
    assert after == before + 1  # the single row imported
    assert panel == "imports"  # back to the file list when done


def test_walkthrough_saves_the_layout_for_next_time(tmp_path, monkeypatch):
    session_factory = _setup(tmp_path, monkeypatch)
    _inbox(tmp_path, monkeypatch, unknown=UNKNOWN_LAYOUT_CSV)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("import")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            for answer in ("euro", "Turnover", "Booking Date", "Counterparty", "Acct"):
                if app._setup is None or app._setup.question is None:
                    break
                app._answer_setup(answer)
                await pilot.pause()

    asyncio.run(run())
    with session_factory() as session:
        spec = formats.get_format(session, "euro")
    assert spec.amount_column == "Turnover"
    assert spec.description_column == "Counterparty"
    assert spec.date_formats == ["%Y-%m-%d"]  # read off the file's own values


def test_escape_cancels_the_walkthrough(tmp_path, monkeypatch):
    session_factory = _setup(tmp_path, monkeypatch)
    _inbox(tmp_path, monkeypatch, unknown=UNKNOWN_LAYOUT_CSV)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("import")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            return app._setup, app._panel

    setup, panel = asyncio.run(run())
    assert setup is None
    assert panel == "imports"  # back to the file list, nothing half-saved
    with session_factory() as session:
        assert [s.name for s in formats.list_formats(session)] == ["test_layout"]


def test_walkthrough_asks_for_an_account_on_a_known_layout(tmp_path, monkeypatch):
    """A layout whose files carry no account still needs one, and the app can ask."""
    session_factory = _setup(tmp_path, monkeypatch)
    inbox = _inbox(tmp_path, monkeypatch, other=UNKNOWN_LAYOUT_CSV)
    with session_factory() as session:
        fieldnames, rows = read_header_and_rows(inbox / "other.csv")
        values = formats.infer("other", fieldnames, rows).values
        values.update(
            posted_date_column="Booking Date",
            description_column="Counterparty",
            amount_column="Turnover",
            amount_style=formats.SIGNED,
            date_formats=["%Y-%m-%d"],
            dedup_columns=["Booking Date", "Counterparty", "Turnover"],
        )
        formats.save_format(session, formats.spec_from_values(values))
        session.commit()

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("import")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            question = app._setup.question
            app._answer_setup("Euro Account")
            await pilot.pause()
            return question, [t.name for t in app._accounts]

    question, accounts = asyncio.run(run())
    assert question.field == "__account"  # asked, not refused
    assert "Euro Account" in accounts


def test_escape_leaves_the_import_picker(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _inbox(tmp_path, monkeypatch, known=CSV)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("import")
            await pilot.pause()
            opened = _panel_state(app)
            await pilot.press("escape")
            await pilot.pause()
            return opened, _panel_state(app), app.focused.id

    opened, closed, focused = asyncio.run(run())
    assert opened[0] is False
    assert closed[0] is True  # transactions are back
    assert focused == "command"


def test_import_all_still_imports_without_browsing(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _inbox(tmp_path, monkeypatch, fresh=NEW_ROWS_CSV, unknown=UNKNOWN_LAYOUT_CSV)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("import all")
            await pilot.pause()
            return [(n.title, n.message) for n in app._notifications], _panel_state(app)

    notifications, state = asyncio.run(run())
    summary = next(m for t, m in notifications if not t)
    warning = next(m for t, m in notifications if t and "not imported" in t)
    assert "Imported 1 file(s): 2 added" in summary
    assert "unknown.csv" in warning
    assert state[0] is True  # bulk import does not open the picker


def _panel_state(app):
    """(transactions visible, rules visible, status line)."""
    return (
        app.query_one("#txns", DataTable).display,
        app.query_one("#rules", DataTable).display,
        str(app.query_one("#status", Static).content),
    )


def test_rules_command_opens_the_main_panel(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            before = _panel_state(app)

            app._run_command("rule COFFEE* = Coffee")
            app._run_command("rule *GROCER* = Groceries")
            await pilot.pause()
            app._run_command("rules")
            await pilot.pause()

            table = app.query_one("#rules", DataTable)
            rows = [
                [str(c) for c in table.get_row_at(i)] for i in range(table.row_count)
            ]
            return before, _panel_state(app), rows, app.focused.id

    before, after, rows, focused = asyncio.run(run())
    assert before == (True, False, before[2])  # transactions own the panel on mount
    assert after[0] is False and after[1] is True
    # COFFEE* names both coffee vendors; *GROCER* matches nothing in this data.
    # The leading cell is the rule's kind, now that category rules share the panel.
    assert rows == [
        ["vendor", "COFFEE*", "Coffee", "2"],
        ["vendor", "*GROCER*", "Groceries", "0"],
    ]
    assert "2 rules" in after[2] and "escape to return" in after[2]
    assert focused == "rules"


def test_escape_returns_to_transactions(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("rule COFFEE* = Coffee")
            await pilot.pause()
            app._run_command("rules")
            await pilot.pause()
            opened = _panel_state(app)

            await pilot.press("escape")
            await pilot.pause()
            return opened, _panel_state(app), app.focused.id

    opened, closed, focused = asyncio.run(run())
    assert opened[1] is True
    assert closed[0] is True and closed[1] is False
    assert "txns" in closed[2]  # the totals line is back
    assert focused == "command"


def test_escape_is_a_noop_in_transaction_view(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            await pilot.press("escape")
            await pilot.pause()
            return _panel_state(app)

    visible_txns, visible_rules, _ = asyncio.run(run())
    assert visible_txns is True and visible_rules is False


def test_bare_rule_command_also_opens_the_panel(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("rule COFFEE* = Coffee")
            await pilot.pause()
            app._run_command("rule")
            await pilot.pause()
            return _panel_state(app), app.query_one("#rules", DataTable).row_count

    state, row_count = asyncio.run(run())
    assert state[1] is True
    assert row_count == 1
    assert "1 rule " in state[2]  # singular


def test_rules_panel_with_no_rules_is_empty_and_notifies(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("rules")
            await pilot.pause()
            messages = [n.message for n in app._notifications]
            return _panel_state(app), app.query_one("#rules", DataTable).row_count, messages

    state, row_count, messages = asyncio.run(run())
    assert state[1] is True  # the panel still opens, just empty
    assert row_count == 0
    assert any("No vendor rules yet" in m for m in messages)


def test_new_rule_appears_in_an_open_panel(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("rules")
            await pilot.pause()
            app._run_command("rule COFFEE* = Coffee")
            await pilot.pause()
            table = app.query_one("#rules", DataTable)
            return _panel_state(app), table.row_count

    state, row_count = asyncio.run(run())
    assert state[1] is True  # still on the rules panel
    assert row_count == 1  # and it picked up the rule that was just added


def test_shortcut_with_no_vendor_selected_does_nothing(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app.query_one("#vendors", ListView).index = 0  # the "— All —" row
            await pilot.press("ctrl+n")
            return app.query_one("#command", Input).value

    assert asyncio.run(run()) == ""


TRANSFER_CSV = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2025-10-01,2025-10-02,9999,MOVE OUT,Transfer,900.00,
"""


def test_transfer_rows_are_marked_in_the_table(tmp_path, monkeypatch):
    """A row absent from the totals must look different, or it reads as a bug."""
    session_factory = _setup(tmp_path, monkeypatch)
    other = tmp_path / "other.csv"
    other.write_text(TRANSFER_CSV, encoding="utf-8")
    with session_factory() as session:
        import_csv(session, other)  # a second account, opposite sign to nothing yet
        currency_id = session.execute(
            __import__("sqlalchemy").text("select id from currency limit 1")
        ).scalar()
        account_id = session.execute(
            __import__("sqlalchemy").text(
                "select id from account where name like '%8207%' limit 1"
            )
        ).scalar()
        session.execute(
            __import__("sqlalchemy").text(
                "insert into transactions (account_id, currency_id, posted_date,"
                " description, raw_description, value_minor, category_source, import_hash)"
                " values (:a, :c, '2025-10-03', 'MOVE IN', 'MOVE IN', 90000, 'unset', 'h1')"
            ),
            {"a": account_id, "c": currency_id},
        )
        session.commit()
        assert transfers.detect_transfers(session) == 1
        session.commit()

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            table = app.query_one("#txns", DataTable)
            rows = {}
            for i in range(table.row_count):
                cells = table.get_row_at(i)
                rows[str(cells[1])] = cells
            return rows

    rows = asyncio.run(run())
    transfer_row = rows["⇄ MOVE IN"]
    assert "⇄" in str(transfer_row[1])  # flagged
    assert "dim" in str(transfer_row[4].style)  # and greyed, not red/green
    ordinary = next(v for k, v in rows.items() if "COFFEE" in k)
    assert "⇄" not in str(ordinary[1])
    assert "dim" not in str(ordinary[4].style)


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


def test_transaction_table_shows_the_account_after_the_amount(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            table = app.query_one("#txns", DataTable)
            headers = [str(c.label) for c in table.columns.values()]
            first = [str(c) for c in table.get_row_at(0)]
            return headers, first

    headers, first = asyncio.run(run())
    assert headers == ["Date", "Description", "Vendor", "Category", "Amount", "Account"]
    assert first[-1] == "8207"  # the account each row belongs to


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

RECENT_ROWS = (
    # (days ago, description, category, debit, credit)
    (3, "CAFE ONE", "Dining", "10.00", ""),
    (6, "CAFE TWO", "Dining", "30.00", ""),
    (9, "RENT PAYMENT", "Housing", "1000.00", ""),
    (5, "PAYCHECK", "Income", "", "2000.00"),
    (100, "OLD SHOP", "Dining", "500.00", ""),  # outside 1m/3m, inside 6m
)


def _recent_csv():
    today = datetime.date.today()
    lines = ["Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit"]
    for days, description, category, debit, credit in RECENT_ROWS:
        day = (today - datetime.timedelta(days=days)).isoformat()
        lines.append(f"{day},{day},8207,{description},{category},{debit},{credit}")
    return "\n".join(lines) + "\n"


def _setup_recent(tmp_path, monkeypatch):
    """A database holding only the relative-dated rows, so window totals are exact."""
    db_path = tmp_path / "recent.db"
    engine = get_engine(db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    csv_path = tmp_path / "recent.csv"
    csv_path.write_text(_recent_csv(), encoding="utf-8")
    with session_factory() as session:
        learn_format(session, csv_path)
        import_csv(session, csv_path)
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def _stats_rows(app):
    return _rows_of(app, "stats_table")


def _stats_state(app):
    """(picker visible, stats visible, status line)."""
    return (
        app.query_one("#periods", DataTable).display,
        app.query_one("#stats").display,
        str(app.query_one("#status", Static).content),
    )


def test_the_prompt_still_belongs_to_the_setup_walkthrough(tmp_path, monkeypatch):
    """The prompt is now shared, so the panel that asked must still get it."""
    _setup(tmp_path, monkeypatch)
    _inbox(tmp_path, monkeypatch, unknown=UNKNOWN_LAYOUT_CSV)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("import")
            await pilot.pause()
            await pilot.press("enter")  # starts the walkthrough for unknown.csv
            await pilot.pause()
            asking = app.query_one("#prompt", Static).display
            await pilot.press("escape")
            await pilot.pause()
            return asking, app.query_one("#prompt", Static).display

    assert asyncio.run(run()) == (True, False)


def test_stats_command_opens_the_period_picker(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats")
            await pilot.pause()
            return _rows_of(app, "periods"), _stats_state(app), app.focused.id

    rows, state, focused = asyncio.run(run())
    assert [row[0] for row in rows] == [
        "1 month", "3 months", "6 months", "1 year", "2 years", "Custom…",
    ]
    # Every preset spells out the dates it resolves to.
    window = stats.resolve("6m")
    assert rows[2][1] == f"{window.start} → {window.end}"
    assert rows[5][1] == "2025-01-01..2025-06-30"  # the format a custom range takes
    assert state[0] is True and state[1] is False
    assert "choose a period" in state[2] and "escape to return" in state[2]
    assert focused == "periods"


def test_enter_on_a_preset_shows_the_stats_panel(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats")
            await pilot.pause()
            await pilot.press("enter")  # the cursor starts on "1 month"
            await pilot.pause()
            return _stats_rows(app), _stats_state(app), app.window, app.focused.id

    rows, state, window, focused = asyncio.run(run())
    assert window.key == "1m"
    assert state[0] is False and state[1] is True
    # Biggest spender first, income last; the 100-day-old row is out of this window.
    # The closing TOTAL row sums the depth-0 rows above it: -1,000 - 40 + 2,000 = 960.
    assert [row[0] for row in rows] == ["Housing", "Dining", "Income", "TOTAL"]
    assert [row[2] for row in rows] == ["-1,000.00", "-40.00", "2,000.00", "960.00"]
    assert [row[4] for row in rows] == ["96.2%", "3.8%", "0.0%", "100.0%"]
    assert rows[-1][1] == "4"  # every transaction in the window, not just the spending
    assert state[2].startswith("1 month ")
    assert "4 txns" in state[2] and "out -1,040.00" in state[2]
    assert "in 2,000.00" in state[2]
    assert focused == "stats_table"


def test_stats_status_line_fits_the_main_panel(tmp_path, monkeypatch):
    """The panel gives the status 92 columns; a truncated line hides the totals.

    Measured against the widest figures the line is designed to hold rather than the
    fixture's tens of dollars: a seeded ``-40.00`` says nothing about whether a real year
    of spending fits. The ceiling below — a million dollars through 99,999 transactions
    over the longest label ("custom") — is well past any plausible personal budget, and
    lands exactly on the budget, so widening any field here will fail this test.
    """
    _setup_recent(tmp_path, monkeypatch)
    window = stats.parse("2024-01-01..2025-12-31")
    big = stats.Report(
        window=window,
        categories=[],
        count=99_999,
        net_minor=0,
        outflow_minor=-99_999_999,  # -999,999.99
        inflow_minor=99_999_999,
        transfer_count=999,
    )

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2y")
            await pilot.pause()
            width = app.query_one("#status", Static).size.width
            real = app._stats_status()
            app._report = big
            return real, app._stats_status(), width

    real, worst_case, width = asyncio.run(run())
    assert len(real) <= width - 2  # padding: 0 1
    assert len(worst_case) <= width - 2
    # The transfer count only appears when there is one to report.
    assert TRANSFER_MARK in worst_case and TRANSFER_MARK not in real


def test_stats_with_a_preset_spec_skips_the_picker(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 6m")
            await pilot.pause()
            six = (_stats_rows(app), _stats_state(app), app.window.key)

            app._run_command("stats 1 year")  # the spelled-out label works too
            await pilot.pause()
            return six, app.window.key

    (rows, state, key), year_key = asyncio.run(run())
    assert key == "6m" and year_key == "1y"
    assert state[0] is False and state[1] is True  # the picker never appeared
    # Six months reaches the 100-day-old row, so Dining gains a transaction.
    assert [row[:3] for row in rows] == [
        ["Housing", "1", "-1,000.00"],
        ["Dining", "3", "-540.00"],
        ["Income", "1", "2,000.00"],
        ["TOTAL", "5", "460.00"],
    ]


def test_stats_with_an_explicit_range_skips_the_picker(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)  # the 2025-07 fixture rows

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 2025-01-01..2025-06-30")
            await pilot.pause()
            empty = (_stats_rows(app), _stats_state(app))

            app._run_command("stats 2025-07-01..2025-07-31")
            await pilot.pause()
            return empty, _stats_rows(app), _stats_state(app), app.window

    (empty_rows, empty_state), rows, state, window = asyncio.run(run())
    assert empty_state[0] is False and empty_state[1] is True
    assert empty_rows == []  # a window with nothing in it is empty, not an error
    assert window.key == "custom"
    # 3.00 + 4.00 + 3.50 over 31 days: 1050 * 30.4375 / 31 = 1030.9 minor units a month.
    # The trailing "" is % parent, blank at depth 0 (identical to % spend there).
    assert rows == [
        ["Dining", "3", "-10.50", "-10.31", "100.0%", ""],
        ["TOTAL", "3", "-10.50", "-10.31", "100.0%", ""],
    ]
    assert state[2].startswith("custom 2025-07-01→2025-07-31 3 txns")


def test_custom_row_prompts_for_a_range_and_applies_it(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats")
            await pilot.pause()
            table = app.query_one("#periods", DataTable)
            table.move_cursor(row=5)  # Custom…
            await pilot.press("enter")
            await pilot.pause()
            prompt = app.query_one("#prompt", Static)
            asked = (
                app._range_pending,
                prompt.display,
                str(prompt.content),
                app.focused.id,
                _stats_state(app)[0],
            )

            app.query_one("#command", Input).value = "2025-07-01..2025-07-31"
            await pilot.press("enter")
            await pilot.pause()
            return (
                asked,
                _stats_rows(app),
                _stats_state(app),
                app._range_pending,
                prompt.display,
            )

    asked, rows, state, pending, prompt_visible = asyncio.run(run())
    assert asked[0] is True and asked[1] is True  # the prompt is up, over the picker
    assert "2025-01-01..2025-06-30" in asked[2]  # and shows the expected format
    assert asked[3] == "command" and asked[4] is True
    assert pending is False and prompt_visible is False  # the question is done with
    # The trailing "" is % parent, blank at depth 0.
    assert state[1] is True and rows == [
        ["Dining", "3", "-10.50", "-10.31", "100.0%", ""],
        ["TOTAL", "3", "-10.50", "-10.31", "100.0%", ""],
    ]


def test_a_bad_spec_notifies_and_leaves_the_panel_alone(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats banana")
            await pilot.pause()
            from_command = (
                [n.message for n in app._notifications],
                _panel_state(app)[0],
                len(app._txns),
                app.window,
            )

            app._run_command("stats")
            await pilot.pause()
            table = app.query_one("#periods", DataTable)
            table.move_cursor(row=5)
            await pilot.press("enter")
            await pilot.pause()
            app.query_one("#command", Input).value = "2025-13-01..nonsense"
            await pilot.press("enter")
            await pilot.pause()
            return from_command, _stats_state(app), app._range_pending

    (messages, txns_visible, txn_count, window), state, pending = asyncio.run(run())
    assert any("banana" in m for m in messages)
    assert txns_visible is True and txn_count == 3  # nowhere confusing, nothing blanked
    assert window is None
    assert state[0] is True and pending is True  # still at the prompt, over the picker


def test_escape_returns_to_transactions_from_the_stats_panel(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 3m")
            await pilot.pause()
            opened = _stats_state(app)
            await pilot.press("escape")
            await pilot.pause()
            return opened, _stats_state(app), _panel_state(app), app.focused.id

    opened, closed, panel, focused = asyncio.run(run())
    assert opened[1] is True
    assert closed[1] is False and panel[0] is True
    assert "txns" in panel[2]  # the totals line is back
    assert focused == "command"


def test_escape_at_the_range_prompt_returns_to_the_picker(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats")
            await pilot.pause()
            app.query_one("#periods", DataTable).move_cursor(row=5)
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            return (
                app._range_pending,
                app.query_one("#prompt", Static).display,
                _stats_state(app),
                app.focused.id,
            )

    pending, prompt_visible, state, focused = asyncio.run(run())
    assert pending is False and prompt_visible is False
    assert state[0] is True and state[1] is False  # back in the picker, not the table
    assert focused == "periods"


def test_stats_panel_rescopes_when_a_filter_changes(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 6m")
            await pilot.pause()
            before = _stats_rows(app)

            app._run_command("filter CAFE")  # runs reload() under the covers
            await pilot.pause()
            filtered = (_stats_rows(app), _stats_state(app))

            category = next(c for c in app._categories if c.name == "Housing")
            app.text_filter = None
            app.category_filter = category.id
            app.reload()
            await pilot.pause()
            return before, filtered, _stats_rows(app), _stats_state(app)

    before, (filtered_rows, filtered_state), housing_rows, housing_state = asyncio.run(run())
    assert len(before) == 4  # three categories plus the closing TOTAL row
    # Averages depend on the length of a window anchored to today, so this case checks
    # the scope; the fixed custom range above pins the arithmetic.
    # The trailing "" is % parent, blank at depth 0.
    assert [row[:3] + row[4:] for row in filtered_rows] == [
        ["Dining", "2", "-40.00", "100.0%", ""],
        ["TOTAL", "2", "-40.00", "100.0%", ""],
    ]
    assert "2 txns" in filtered_state[2] and "out -40.00" in filtered_state[2]
    assert [row[:3] + row[4:] for row in housing_rows] == [
        ["Housing", "1", "-1,000.00", "100.0%", ""],
        ["TOTAL", "1", "-1,000.00", "100.0%", ""],
    ]
    assert housing_state[1] is True  # still on the stats panel throughout


# ------------------------------------------------------------------ categories

def _category_of(app, description):
    """The Category cell the transactions table shows for a description."""
    table = app.query_one("#txns", DataTable)
    for index, txn in enumerate(app._txns):
        if txn.description == description:
            return str(table.get_row_at(index)[3])
    return None


def test_sidebar_is_not_rebuilt_when_only_the_filters_changed(tmp_path, monkeypatch):
    """Rebuilding the sidebar dominated every drill-down: it runs to hundreds of rows,
    and mounting that many widgets costs far more than the query behind it. Nothing a
    filter or a drill does can change it — the sidebar queries take no filter arguments —
    so the rebuild must be skipped, while a real data change must still come through.
    """
    _setup_recent(tmp_path, monkeypatch)
    rebuilt = []
    original_clear = ListView.clear

    def counting_clear(self, *args, **kwargs):
        rebuilt.append(self.id)
        return original_clear(self, *args, **kwargs)

    monkeypatch.setattr(ListView, "clear", counting_clear)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            at_startup = list(rebuilt)

            app._run_command("stats 1m")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.move_cursor(row=0)
            rebuilt.clear()
            await pilot.press("right")  # drill down
            await pilot.pause()
            await pilot.press("left")  # and back
            await pilot.pause()
            after_drill = list(rebuilt)

            app._run_command("filter rent")
            await pilot.pause()
            after_filter = list(rebuilt)

            rebuilt.clear()
            app._run_command("categorize RENT PAYMENT = Housing Costs")
            await pilot.pause()
            sidebar = [
                str(item.children[0].content)
                for item in app.query_one("#categories", ListView).children
            ]
            return at_startup, after_drill, after_filter, sidebar

    at_startup, after_drill, after_filter, sidebar = asyncio.run(run())
    # All three lists are populated once when the app opens.
    assert sorted(at_startup) == ["accounts", "categories", "vendors"]
    assert after_drill == []  # a full round trip rebuilds nothing
    assert after_filter == []
    # ...but a change to the underlying data still reaches the screen.
    assert any("Housing Costs" in label for label in sidebar), sidebar


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


def test_categorize_rule_categorises_matching_vendors(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("categorize rule COFFEE* = Coffee")
            await pilot.pause()
            return (
                [t.category for t in app._txns],
                [n.message for n in app._notifications],
            )

    category_cells, messages = asyncio.run(run())
    assert category_cells == ["Coffee"] * 3  # a rule overwrites the bank's own category
    assert any("3 transactions categorised" in m for m in messages)


def test_manual_category_outranks_a_later_rule(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("categorize COFFEE SHOP A = Treats")
            await pilot.pause()
            app._run_command("categorize rule COFFEE* = Coffee")
            await pilot.pause()
            return _category_of(app, "COFFEE SHOP A"), _category_of(app, "COFFEE SHOP B")

    assert asyncio.run(run()) == ("Treats", "Coffee")


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


def test_rules_panel_lists_both_kinds_of_rule(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("rule COFFEE* = Coffee")
            app._run_command("categorize rule *SHOP A = Treats")
            await pilot.pause()
            app._run_command("categorize rules")
            await pilot.pause()
            named = _panel_state(app)

            await pilot.press("escape")
            await pilot.pause()
            app._run_command("categorize")  # bare, like a bare `rule`
            await pilot.pause()
            return _rows_of(app, "rules"), _panel_state(app), app.focused.id, named

    rows, state, focused, named = asyncio.run(run())
    assert named[1] is True  # `categorize rules` opens the same panel
    assert rows == [
        ["vendor", "COFFEE*", "Coffee", "2"],  # 2 raw vendors named
        ["category", "*SHOP A", "Treats", "2"],  # 2 transactions categorised
    ]
    assert state[1] is True and focused == "rules"
    assert "2 rules" in state[2] and "2 vendors named" in state[2]
    assert "2 txns categorised" in state[2]


def test_a_manual_category_is_not_counted_against_a_rule(tmp_path, monkeypatch):
    """The count is what the rule owns, so rows it could not take are left out."""
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("categorize COFFEE SHOP A = Treats")
            await pilot.pause()
            app._run_command("categorize rule COFFEE* = Coffee")
            await pilot.pause()
            app._run_command("rules")
            await pilot.pause()
            return _rows_of(app, "rules")

    # Three coffee transactions match the pattern, but two are manual, so the rule
    # only owns COFFEE SHOP B's single row.
    assert asyncio.run(run()) == [["category", "COFFEE*", "Coffee", "1"]]


def test_rules_panel_columns_fit_the_main_panel(tmp_path, monkeypatch):
    """A column pushed off the right edge hides the count, which is the point."""
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("rule COFFEE* = Coffee")
            app._run_command("categorize rule COFFEE* = Coffee and cake")
            await pilot.pause()
            app._run_command("rules")
            await pilot.pause()
            table = app.query_one("#rules", DataTable)
            widths = [c.width for c in table.columns.values()]
            return widths, table.size.width

    widths, panel_width = asyncio.run(run())
    assert widths == [9, 26, 18, 7]
    # Two cells of padding per column, plus the panel's own round border.
    assert sum(widths) + 2 * len(widths) + 2 <= panel_width


# ------------------------------------------------------- statistics drill-down
# One row with no category at all, so the Uncategorised row of the report has
# something to drill into.

UNCATEGORISED_ROW = (12, "MYSTERY CHARGE", "", "25.00", "")


def _setup_with_uncategorised(tmp_path, monkeypatch):
    session_factory = _setup_recent(tmp_path, monkeypatch)
    days, description, category, debit, credit = UNCATEGORISED_ROW
    day = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    extra = tmp_path / "extra.csv"
    extra.write_text(
        "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\n"
        f"{day},{day},8207,{description},{category},{debit},{credit}\n",
        encoding="utf-8",
    )
    with session_factory() as session:
        import_csv(session, extra)
    return session_factory


def test_enter_on_a_stats_row_drills_into_that_category(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 1m")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=1)  # Dining, the second-biggest spender
            shown = [str(c) for c in table.get_row_at(1)]
            await pilot.press("enter")
            await pilot.pause()
            return (
                shown,
                app._panel,
                app.category_filter,
                app.date_filter,
                [t.description for t in app._txns],
                _panel_state(app)[2],
            )

    shown, panel, category_filter, date_filter, descriptions, status = asyncio.run(run())
    assert panel == "txns"  # the transactions the row was made of
    assert category_filter is not None
    window = stats.resolve("1m")
    assert date_filter == (window.start, window.end)
    # The row said "2"; the table has to agree, or the drill-down lies.
    assert shown[1] == "2" and len(descriptions) == 2
    assert sorted(descriptions) == ["CAFE ONE", "CAFE TWO"]
    # The 100-day-old CAFE row is Dining too, and is outside the window.
    assert "OLD SHOP" not in descriptions
    assert "[filtered: category," in status and str(window.start) in status


def test_enter_on_the_uncategorised_row_drills_in_too(tmp_path, monkeypatch):
    _setup_with_uncategorised(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 1m")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            row = next(
                i for i in range(table.row_count)
                if str(table.get_row_at(i)[0]) == stats.UNCATEGORISED
            )
            shown = [str(c) for c in table.get_row_at(row)]
            table.focus()
            table.move_cursor(row=row)
            await pilot.press("enter")
            await pilot.pause()
            return shown, app.category_filter, [t.description for t in app._txns]

    shown, category_filter, descriptions = asyncio.run(run())
    assert category_filter == queries.UNCATEGORISED_ID  # the null category, not "none"
    assert shown[1] == "1" and descriptions == ["MYSTERY CHARGE"]


def test_a_custom_window_drill_down_uses_that_window(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)  # the fixed 2025-07 rows

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 2025-07-01..2025-07-02")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            return app.date_filter, [t.posted_date for t in app._txns]

    date_filter, dates = asyncio.run(run())
    assert date_filter == (datetime.date(2025, 7, 1), datetime.date(2025, 7, 2))
    assert dates == ["2025-07-02", "2025-07-02"]  # the 07-04 row is out of the window


def test_clear_filters_also_clears_the_date_filter(tmp_path, monkeypatch):
    """A drill-down must not be sticky, or the table quietly hides later rows."""
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 1m")
            await pilot.pause()
            app.query_one("#stats_table", DataTable).focus()
            await pilot.press("enter")
            await pilot.pause()
            drilled = (app.date_filter, len(app._txns))

            await pilot.press("ctrl+l")
            await pilot.pause()
            cleared = (app.date_filter, app.category_filter, len(app._txns))

            app._run_command("stats 1m")
            await pilot.pause()
            app.query_one("#stats_table", DataTable).focus()
            await pilot.press("enter")
            await pilot.pause()
            app._run_command("all")
            await pilot.pause()
            return drilled, cleared, (app.date_filter, len(app._txns))

    drilled, cleared, after_all = asyncio.run(run())
    assert drilled[0] is not None and drilled[1] == 1  # Housing, one transaction
    assert cleared == (None, None, 5)  # every seeded row is back
    assert after_all == (None, 5)  # the `all` command clears it too


def test_drill_down_status_line_fits_the_main_panel(tmp_path, monkeypatch):
    """The window in the scope label costs columns the line does not really have.

    The panel gives the status 92 columns, two of them padding. A count, a scope list and
    three money figures already fill most of that, so the drill-down's window is written
    ``2025-01-01→12-31`` — the end date's year is noise once the start has given it. This
    pins that: spelling the year out twice pushes the case below over the edge.

    The line is not width-budgeted beyond this. Stacking an account and a vendor filter on
    top, or a text filter of any length, already overflowed it before the window existed;
    that is pre-existing and would need the whole line redesigned.
    """
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            width = app.query_one("#status", Static).size.width
            app._run_command("stats 1m")
            await pilot.pause()
            app.query_one("#stats_table", DataTable).focus()
            await pilot.press("enter")
            await pilot.pause()
            real = str(app.query_one("#status", Static).content)

            # A year of one category, in the hundreds of transactions and thousands of
            # dollars: comfortably past a real personal budget's biggest category.
            app.date_filter = (datetime.date(2025, 1, 1), datetime.date(2025, 12, 31))
            app._set_status(
                queries.Totals(
                    count=999,
                    net_minor=-999_999,
                    outflow_minor=-999_999,
                    inflow_minor=0,
                )
            )
            return real, str(app.query_one("#status", Static).content), width

    real, worst_case, width = asyncio.run(run())
    assert len(real) <= width - 2
    assert len(worst_case) <= width - 2
    assert "2025-01-01→12-31" in worst_case  # the year is given once, not twice


# ------------------------------------------------- arrow-key statistics navigation


def test_right_arrow_on_a_stats_row_drills_down_like_enter(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 1m")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=1)  # Dining, the second-biggest spender
            await pilot.press("right")
            await pilot.pause()
            return (
                app._panel,
                app.category_filter,
                app.date_filter,
                sorted(t.description for t in app._txns),
            )

    panel, category_filter, date_filter, descriptions = asyncio.run(run())
    assert panel == "txns"
    assert category_filter is not None
    window = stats.resolve("1m")
    assert date_filter == (window.start, window.end)
    assert descriptions == ["CAFE ONE", "CAFE TWO"]


def test_left_arrow_returns_to_the_stats_panel_with_the_full_breakdown(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 1m")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=1)  # Dining
            await pilot.press("right")
            await pilot.pause()
            drilled = (app._panel, app.category_filter, app.date_filter)

            await pilot.press("left")
            await pilot.pause()
            back_panel = app._panel
            rows = _stats_rows(app)
            cursor_row = app.query_one("#stats_table", DataTable).cursor_row
            return drilled, back_panel, rows, app.category_filter, app.date_filter, cursor_row

    drilled, back_panel, rows, category_filter, date_filter, cursor_row = asyncio.run(run())
    assert drilled[0] == "txns" and drilled[1] is not None and drilled[2] is not None
    assert back_panel == "stats"
    # The category filter the drill-down set is gone, or this would be one row, not the
    # whole breakdown.
    assert category_filter is None
    assert date_filter is None
    assert len(rows) > 1
    assert [r[0] for r in rows] == ["Housing", "Dining", "Income", "TOTAL"]
    assert cursor_row == 1  # back on the Dining row that was drilled from


def test_left_arrow_restores_filters_set_before_the_drill(tmp_path, monkeypatch):
    """Going back must not blank a filter the user had on purpose before drilling in."""
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            housing = next(c for c in app._categories if c.name == "Housing")
            app.category_filter = housing.id
            app.reload()
            await pilot.pause()

            app._run_command("stats 1m")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=0)  # the only row: Housing, scoped by the filter above
            await pilot.press("right")
            await pilot.pause()
            drilled_category_filter = app.category_filter

            await pilot.press("left")
            await pilot.pause()
            return housing.id, drilled_category_filter, app.category_filter, app._panel

    housing_id, drilled_category_filter, restored_category_filter, panel = asyncio.run(run())
    assert drilled_category_filter == housing_id  # the drill only had one row to pick
    assert restored_category_filter == housing_id  # back to the pre-drill filter, not None
    assert panel == "stats"


def test_left_arrow_does_nothing_without_a_drill_down(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            before = (app._panel, app.focused.id)
            await pilot.press("left")
            await pilot.pause()
            return before, (app._panel, app.focused.id)

    before, after = asyncio.run(run())
    assert before == after == ("txns", "command")


def test_a_new_filter_clears_the_stale_drill_down_flag(tmp_path, monkeypatch):
    """Once the user has changed the view another way, left arrow must not fire later."""
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 1m")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=1)
            await pilot.press("right")
            await pilot.pause()
            drilled = app._drilled_from_stats

            app._run_command("filter cafe")
            await pilot.pause()
            after_filter = app._drilled_from_stats
            panel = app._panel

            await pilot.press("left")
            await pilot.pause()
            return drilled, after_filter, panel, app._panel

    drilled, after_filter, panel_before_left, panel_after_left = asyncio.run(run())
    assert drilled is True
    assert after_filter is False
    assert panel_before_left == "txns"
    assert panel_after_left == "txns"  # left arrow did not send us anywhere


def test_escape_also_clears_the_stale_drill_down_flag(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 1m")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=1)
            await pilot.press("right")
            await pilot.pause()

            await pilot.press("escape")
            await pilot.pause()
            return app._drilled_from_stats

    assert asyncio.run(run()) is False


def test_the_drill_down_keys_are_advertised_in_the_footer(tmp_path, monkeypatch):
    """The stats status line and the drilled-down transactions status line are both
    already pinned to their 92-column budget by other tests (see
    test_stats_status_line_fits_the_main_panel and
    test_drill_down_status_line_fits_the_main_panel), with no room left for a hint. The
    footer advertises the keys instead, and only while they do something: showing a key
    that only works in one context all the time would be its own kind of confusing.
    """
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            await pilot.pause()
            not_drilled = dict(app.active_bindings)

            app._run_command("stats 1m")
            await pilot.pause()
            await pilot.pause()
            on_stats = dict(app.active_bindings)

            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=1)
            await pilot.press("right")
            await pilot.pause()
            await pilot.pause()
            drilled = dict(app.active_bindings)

            return not_drilled, on_stats, drilled

    def action_for(bindings, key):
        """The action bound to a key, if any.

        left/right are also bound by the focused widget itself (an ``Input``'s own
        cursor movement, say), so the key alone is not enough to prove our binding is
        the one showing — the action name is.
        """
        binding = bindings.get(key)
        return binding.binding.action if binding else None

    not_drilled, on_stats, drilled = asyncio.run(run())
    assert action_for(not_drilled, "left") != "drill_up"  # nothing to go back to yet
    assert action_for(not_drilled, "right") != "drill_down"  # not looking at a stats row
    assert action_for(on_stats, "right") == "drill_down"
    assert on_stats["right"].binding.description == "Drill down"
    assert action_for(drilled, "left") == "drill_up"
    assert drilled["left"].binding.description == "Back to stats"


# --------------------------------------------------------------- category hierarchy


def _seed_category_hierarchy(tmp_path, monkeypatch):
    """``Food > Dining > {Restaurants and Fast Casual Spots, Fast Food}``, ``Food > Groceries``.

    Six-figure amounts and a long leaf name, so the width guard below measures
    something real rather than passing vacuously on a handful of seeded dollars (see
    test_stats_status_line_fits_the_main_panel for the same concern on the status line).
    """
    db_path = tmp_path / "deep.db"
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
        restaurants = categories.ensure_path(
            session, "Food > Dining > Restaurants and Fast Casual Spots"
        )
        fast_food = categories.ensure_path(session, "Food > Dining > Fast Food")
        groceries = categories.ensure_path(session, "Food > Groceries")
        session.flush()

        def txn(day, amount, description, category):
            session.add(
                Transaction(
                    account_id=account.id,
                    currency_id=currency.id,
                    posted_date=day,
                    description=description,
                    raw_description=description,
                    value_minor=amount,
                    category_id=category.id,
                    category_source="manual",
                    import_hash=f"deep-{description}-{day}-{amount}",
                )
            )

        txn(datetime.date(2025, 3, 1), -12_345_678, "Big meal", restaurants)
        txn(datetime.date(2025, 3, 2), -2_345_678, "Fast food", fast_food)
        txn(datetime.date(2025, 3, 3), -3_456_789, "Groceries", groceries)
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def test_stats_table_indents_by_depth_and_shows_percent_parent(tmp_path, monkeypatch):
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            return _stats_rows(app)

    rows = asyncio.run(run())
    names = [row[0] for row in rows]
    # Depth-first, each parent immediately followed by its subtree, two spaces of
    # indentation per level.
    assert names[0] == "Food"
    assert names[1] == "  Dining"
    assert names[2].startswith("    Restaurants")  # truncated, but still 3 levels deep
    assert names[3] == "    Fast Food"
    assert names[4] == "  Groceries"
    # % parent is blank at depth 0 (it would just repeat % spend there), and populated
    # — and different from % spend — everywhere below it.
    assert rows[0][4] == "100.0%" and rows[0][5] == ""  # Food
    assert rows[1][4] == "81.0%" and rows[1][5] == "81.0%"  # Dining: Food holds nothing directly
    assert rows[2][4] == "68.0%" and rows[2][5] == "84.0%"  # Restaurants, of Dining's total
    assert rows[3][4] == "12.9%" and rows[3][5] == "16.0%"  # Fast Food, of Dining's total
    assert rows[4][4] == "19.0%" and rows[4][5] == "19.0%"  # Groceries: Food holds nothing directly


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


def test_stats_table_fits_the_main_panel_with_a_deep_hierarchy(tmp_path, monkeypatch):
    """The % parent column and the deepest indentation must not push the table off-panel.

    Measured against the table's own actual size, not the ~92-column estimate in the
    add_column comment, and against a three-level hierarchy with six-figure totals — a
    seeded ``-40.00`` a couple of levels deep would pass this even if the columns
    genuinely did not fit.
    """
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            widths = {str(column.label): column.width for column in table.columns.values()}
            panel_width = table.size.width
            return widths, panel_width, _stats_rows(app)

    widths, panel_width, rows = asyncio.run(run())
    # Six columns plus two cells of padding each have to fit inside the width Textual
    # actually gave the table, not just the comment's estimate.
    padding = 2 * len(widths)
    assert sum(widths.values()) + padding <= panel_width
    # A column too narrow for its own header clips it silently (this caught % parent
    # needing width 8, one more than % spend, since "% parent" is eight characters).
    for label, width in widths.items():
        assert len(label) <= width, f"{label!r} does not fit in width {width}"
    # The indented, truncated leaf name still respects the Category column's budget.
    assert all(len(row[0]) <= widths["Category"] for row in rows)
    # Every row below the top rendered a % parent value; only the root leaves it blank.
    # The closing TOTAL row is not a category and leaves it blank too, so it is excluded
    # here rather than the assertion being weakened to tolerate any blank.
    assert rows[-1][0] == "TOTAL"
    assert rows[0][-1] == ""
    assert all(row[-1].endswith("%") for row in rows[1:-1])


def test_stats_total_row_agrees_with_the_report_and_cannot_be_drilled(
    tmp_path, monkeypatch
):
    """The total must come from the report's own figures, and must not act like a row.

    Re-adding the column would double-count a parent's spending with its children's,
    since every row's money already includes its descendants.
    """
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("stats 1m")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            report = app._report
            table.move_cursor(row=table.row_count - 1)  # the TOTAL row
            await pilot.press("right")  # the drill-down key
            await pilot.pause()
            return _stats_rows(app), report, app._panel, app.category_filter

    rows, report, panel, category_filter = asyncio.run(run())
    total = rows[-1]
    assert total[0] == "TOTAL"
    assert total[1] == str(report.count)
    assert total[2] == _fmt_amount(report.net_minor)
    assert total[3] == _fmt_amount(stats.per_month(report.net_minor, report.window))
    # Summing the depth-0 rows reproduces it; summing every row would not.
    depth0 = [c for c in report.categories if c.depth == 0]
    assert sum(c.total_minor for c in depth0) == report.net_minor
    assert sum(c.count for c in depth0) == report.count
    # Drilling from the total row is a no-op: it is not a category.
    assert panel == "stats" and category_filter is None


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


# ------------------------------------------------------------------------- unimport


def test_imports_panel_lists_past_imports_with_their_id(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)  # already imported one file: in.csv
    _inbox(tmp_path, monkeypatch)  # empty inbox, so only history rows show

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("import")
            await pilot.pause()
            return _rows_of(app, "imports"), app._imports

    rows, history = asyncio.run(run())
    assert len(history) == 1 and history[0].source_file == "in.csv"
    # The empty inbox means the candidate section is empty; the history row (dimmed,
    # not actionable by enter) still carries the id an `unimport` needs.
    assert rows[0] == ["in.csv", "3", "imported", str(history[0].id)]


def test_unimport_asks_for_confirmation_naming_what_it_will_destroy(tmp_path, monkeypatch):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        import_id = queries.get_imports(session)[0].id

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command(f"unimport {import_id}")
            await pilot.pause()
            prompt = app.query_one("#prompt", Static)
            return str(prompt.content), prompt.display, app._pending_unimport is not None

    text, visible, pending = asyncio.run(run())
    assert visible is True and pending is True
    assert f"#{import_id}" in text
    assert "in.csv" in text
    assert "3 transaction(s)" in text


def test_unimport_confirmed_deletes_the_transactions(tmp_path, monkeypatch):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        import_id = queries.get_imports(session)[0].id

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            before = len(app._txns)
            app._run_command(f"unimport {import_id}")
            await pilot.pause()
            app.query_one("#command", Input).value = "yes"
            await pilot.press("enter")
            await pilot.pause()
            messages = [n.message for n in app._notifications]
            return before, len(app._txns), messages, app._pending_unimport

    before, after, messages, pending = asyncio.run(run())
    assert before == 3 and after == 0
    assert pending is None
    assert any(
        f"Deleted import #{import_id}" in m and "3 transaction(s) removed" in m
        for m in messages
    )
    with session_factory() as session:
        assert queries.get_imports(session) == []


def test_unimport_cancelled_by_any_other_answer(tmp_path, monkeypatch):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        import_id = queries.get_imports(session)[0].id

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            before = len(app._txns)
            app._run_command(f"unimport {import_id}")
            await pilot.pause()
            app.query_one("#command", Input).value = "no thanks"
            await pilot.press("enter")
            await pilot.pause()
            messages = [n.message for n in app._notifications]
            return before, len(app._txns), messages, app.query_one("#prompt", Static).display

    before, after, messages, prompt_visible = asyncio.run(run())
    assert before == after == 3  # nothing was touched
    assert any("Unimport cancelled" in m for m in messages)
    assert prompt_visible is False


def test_unimport_escape_cancels(tmp_path, monkeypatch):
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        import_id = queries.get_imports(session)[0].id

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command(f"unimport {import_id}")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            messages = [n.message for n in app._notifications]
            return len(app._txns), messages, app._pending_unimport

    count, messages, pending = asyncio.run(run())
    assert count == 3  # nothing deleted
    assert any("Unimport cancelled" in m for m in messages)
    assert pending is None


def test_unimport_reports_an_unknown_id(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("unimport 999")
            await pilot.pause()
            return [n.message for n in app._notifications], app._pending_unimport

    messages, pending = asyncio.run(run())
    assert any("No import with id 999" in m for m in messages)
    assert pending is None


XFER_OUT_CSV = """Transaction Date,Posted Date,Card No.,Description,Amount
2025-07-01,2025-07-02,CHK,TRANSFER TO CARD,-100.00
"""
XFER_IN_CSV = """Transaction Date,Posted Date,Card No.,Description,Amount
2025-07-01,2025-07-02,CRD,PAYMENT RECEIVED,100.00
"""


def _setup_transfer(tmp_path, monkeypatch):
    """A fresh DB holding one already-paired transfer, split across two imports."""
    db_path = tmp_path / "xfer.db"
    engine = get_engine(db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    out_path = tmp_path / "out.csv"
    out_path.write_text(XFER_OUT_CSV, encoding="utf-8")
    in_path = tmp_path / "in_xfer.csv"
    in_path.write_text(XFER_IN_CSV, encoding="utf-8")
    with session_factory() as session:
        learn_format(session, out_path, name="card")
        import_csv(session, out_path)
        import_csv(session, in_path)  # same signature, auto-detected; pairs as a transfer
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def test_unimport_reports_transfer_pairings_it_would_break(tmp_path, monkeypatch):
    session_factory = _setup_transfer(tmp_path, monkeypatch)
    with session_factory() as session:
        out_import_id = next(
            row.id for row in queries.get_imports(session) if row.source_file == "out.csv"
        )

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command(f"unimport {out_import_id}")
            await pilot.pause()
            prompt_text = str(app.query_one("#prompt", Static).content)

            app.query_one("#command", Input).value = "yes"
            await pilot.press("enter")
            await pilot.pause()
            messages = [n.message for n in app._notifications]
            return prompt_text, messages, app._txns

    prompt_text, messages, txns = asyncio.run(run())
    assert "breaking 1 transfer pairing(s)" in prompt_text
    assert any("1 transfer pairing(s) broken" in m for m in messages)
    assert len(txns) == 1
    assert txns[0].is_transfer is False  # its partner is gone, so it is ordinary again


# --------------------------------------------------------------------------- format


def test_format_command_lists_layouts_and_their_polarity(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("format")
            await pilot.pause()
            return [n.message for n in app._notifications]

    messages = asyncio.run(run())
    assert any("test_layout" in m and "debit_credit" in m for m in messages)


def test_format_invert_flips_polarity(tmp_path, monkeypatch):
    session_factory = _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("format test_layout invert on")
            await pilot.pause()
            return [n.message for n in app._notifications]

    messages = asyncio.run(run())
    assert any("test_layout" in m and "invert on" in m for m in messages)
    with session_factory() as session:
        assert formats.get_format(session, "test_layout").invert_amount is True


def test_format_invert_reports_an_unknown_format(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("format nope invert on")
            await pilot.pause()
            return [n.message for n in app._notifications]

    messages = asyncio.run(run())
    assert any("Unknown format" in m and "nope" in m for m in messages)


# --------------------------------------------------------- statistics fold/unfold


def _seed_deep_foldable_hierarchy(tmp_path, monkeypatch):
    """A foldable category with a long name three levels deep, for the width guard.

    ``Food > Dining > Casual and Fast Dining Establishments > Takeout`` — the third
    level is both foldable (it has one child) and long enough that the fold indicator's
    two extra characters are the difference between fitting the Category column and not.
    """
    db_path = tmp_path / "deep_fold.db"
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
        parent = categories.ensure_path(
            session, "Food > Dining > Casual and Fast Dining Establishments"
        )
        child = categories.ensure_path(
            session,
            "Food > Dining > Casual and Fast Dining Establishments > Takeout",
        )
        session.flush()

        def txn(day, amount, description, category):
            session.add(
                Transaction(
                    account_id=account.id,
                    currency_id=currency.id,
                    posted_date=day,
                    description=description,
                    raw_description=description,
                    value_minor=amount,
                    category_id=category.id,
                    category_source="manual",
                    import_hash=f"deepfold-{description}-{day}-{amount}",
                )
            )

        txn(datetime.date(2025, 3, 1), -12_345_678, "Big meal", parent)
        txn(datetime.date(2025, 3, 2), -2_345_678, "Takeout order", child)
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def test_space_folds_and_unfolds_a_stats_row_with_children(tmp_path, monkeypatch):
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            expanded = _stats_rows(app)

            table.move_cursor(row=1)  # Dining, which has two children
            await pilot.press("space")
            await pilot.pause()
            collapsed = _stats_rows(app)

            await pilot.press("space")  # press it again: back to expanded
            await pilot.pause()
            reexpanded = _stats_rows(app)

            return expanded, collapsed, reexpanded

    expanded, collapsed, reexpanded = asyncio.run(run())
    names_expanded = [row[0] for row in expanded]
    assert names_expanded == [
        "Food",
        "  Dining",
        "    Restaurants and Fast …",
        "    Fast Food",
        "  Groceries",
        "TOTAL",
    ]
    names_collapsed = [row[0] for row in collapsed]
    # Restaurants and Fast Food, Dining's subtree, are gone; Groceries moves up.
    assert names_collapsed == [
        "Food",
        f"  {FOLD_INDICATOR} Dining",
        "  Groceries",
        "TOTAL",
    ]
    assert [row[0] for row in reexpanded] == names_expanded


def test_folding_a_group_hides_its_whole_subtree_recursively(tmp_path, monkeypatch):
    """Collapsing ``Food`` (depth 0) must hide Dining, Groceries, and Dining's own
    children — not just its immediate row."""
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=0)  # Food, the root of everything seeded
            await pilot.press("space")
            await pilot.pause()
            return _stats_rows(app)

    rows = asyncio.run(run())
    assert [row[0] for row in rows] == [f"{FOLD_INDICATOR} Food", "TOTAL"]


def test_space_does_nothing_on_a_leaf_row_or_the_total_row(tmp_path, monkeypatch):
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            before = _stats_rows(app)

            table.move_cursor(row=4)  # Groceries, a leaf
            await pilot.press("space")
            await pilot.pause()
            after_leaf = _stats_rows(app)

            table.move_cursor(row=table.row_count - 1)  # TOTAL
            await pilot.press("space")
            await pilot.pause()
            after_total = _stats_rows(app)

            return before, after_leaf, after_total

    before, after_leaf, after_total = asyncio.run(run())
    assert before == after_leaf == after_total


def test_folding_does_not_change_any_numbers(tmp_path, monkeypatch):
    """Every row's figures already roll up its descendants, so folding — which only
    hides rows — must not edit a single one of them."""
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            before = _stats_rows(app)
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=1)  # Dining
            await pilot.press("space")
            await pilot.pause()
            after = _stats_rows(app)
            return before, after

    before, after = asyncio.run(run())
    assert len(before) == 6 and len(after) == 4
    # Food's row rolls up all of Dining's subtree; that total does not move.
    assert before[0] == after[0]
    # Dining's own figures are unchanged; only its name grows the fold indicator.
    assert before[1][1:] == after[1][1:]
    assert before[1][0] == "  Dining"
    assert after[1][0] == f"  {FOLD_INDICATOR} Dining"
    # Groceries and TOTAL, untouched by the fold, are byte-for-byte identical, just
    # moved up to fill the gap Dining's hidden children left.
    assert before[4] == after[2]
    assert before[5] == after[3]


def test_fold_state_survives_a_window_change_a_filter_and_a_drill_round_trip(
    tmp_path, monkeypatch
):
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=1)  # Dining
            await pilot.press("space")  # collapse
            await pilot.pause()
            after_fold = [row[0] for row in _stats_rows(app)]

            # A new window rebuilds the report from scratch.
            app._run_command("stats 2020-01-01..2025-12-31")
            await pilot.pause()
            after_window = [row[0] for row in _stats_rows(app)]

            # So does a filter.
            app.reload()
            await pilot.pause()
            after_filter = [row[0] for row in _stats_rows(app)]

            # And so does a full drill-in/drill-out round trip.
            table.move_cursor(row=0)  # Food, still visible with Dining collapsed
            await pilot.press("right")
            await pilot.pause()
            await pilot.press("left")
            await pilot.pause()
            after_drill = [row[0] for row in _stats_rows(app)]

            return after_fold, after_window, after_filter, after_drill

    after_fold, after_window, after_filter, after_drill = asyncio.run(run())
    expected = ["Food", f"  {FOLD_INDICATOR} Dining", "  Groceries", "TOTAL"]
    assert after_fold == expected
    assert after_window == expected
    assert after_filter == expected
    assert after_drill == expected


def test_drill_down_maps_to_the_row_actually_clicked_after_a_collapse(
    tmp_path, monkeypatch
):
    """The trap: once rows can be hidden, table row N is no longer report.categories[N].

    A row below a collapsed group has to drill into the category it visibly shows, not
    into whatever used to sit at that table index before the collapse.
    """
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=1)  # Dining
            await pilot.press("space")  # hides Restaurants and Fast Food
            await pilot.pause()
            rows = _stats_rows(app)
            assert rows[2][0] == "  Groceries"  # moved up into what was row 2

            table.move_cursor(row=2)
            await pilot.press("enter")
            await pilot.pause()
            return [t.description for t in app._txns]

    descriptions = asyncio.run(run())
    # Groceries' own transaction, not "Big meal"/"Fast food" from Dining's hidden rows.
    assert descriptions == ["Groceries"]


def test_left_arrow_after_a_collapsed_drill_down_lands_back_on_the_row_clicked(
    tmp_path, monkeypatch
):
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=1)  # Dining
            await pilot.press("space")  # collapse
            await pilot.pause()

            table.move_cursor(row=2)  # Groceries, directly below the collapsed group
            await pilot.press("right")  # drill down
            await pilot.pause()
            await pilot.press("left")  # back
            await pilot.pause()

            rows = _stats_rows(app)
            cursor_row = app.query_one("#stats_table", DataTable).cursor_row
            return rows, cursor_row, app._panel

    rows, cursor_row, panel = asyncio.run(run())
    assert panel == "stats"
    assert cursor_row == 2  # back on Groceries, not shifted by the still-collapsed group
    assert [row[0] for row in rows] == [
        "Food",
        f"  {FOLD_INDICATOR} Dining",
        "  Groceries",
        "TOTAL",
    ]


def test_space_does_nothing_outside_the_stats_table(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            txns = app.query_one("#txns", DataTable)
            txns.focus()
            before_txns = _rows_of(app, "txns")
            await pilot.press("space")
            await pilot.pause()
            after_txns = _rows_of(app, "txns")
            txns_panel = app._panel

            app._run_command("rules")
            await pilot.pause()
            rules = app.query_one("#rules", DataTable)
            rules.focus()
            before_rules = _rows_of(app, "rules")
            await pilot.press("space")
            await pilot.pause()
            after_rules = _rows_of(app, "rules")
            rules_panel = app._panel

            return (
                before_txns,
                after_txns,
                txns_panel,
                before_rules,
                after_rules,
                rules_panel,
            )

    (
        before_txns,
        after_txns,
        txns_panel,
        before_rules,
        after_rules,
        rules_panel,
    ) = asyncio.run(run())
    assert before_txns == after_txns
    assert txns_panel == "txns"
    assert before_rules == after_rules
    assert rules_panel == "rules"


def test_space_still_types_a_literal_space_in_the_command_bar(tmp_path, monkeypatch):
    """The fold binding is deliberately not priority=True, so it must never steal a
    space bar press from the Input that holds focus most of the app's life."""
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            text = "filter multi word search"
            keys = [char if char != " " else "space" for char in text]
            await pilot.press(*keys)
            return app.query_one("#command", Input).value

    assert asyncio.run(run()) == "filter multi word search"


def test_stats_table_fold_indicator_fits_the_main_panel_on_a_deep_row(
    tmp_path, monkeypatch
):
    """Extends test_stats_table_fits_the_main_panel_with_a_deep_hierarchy: the fold
    indicator's two extra characters must not push a deep, long-named, collapsed row
    past the Category column's 26-character budget.
    """
    _seed_deep_foldable_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=2)  # "Casual and Fast Dining Establishments", depth 2
            await pilot.press("space")
            await pilot.pause()
            widths = {str(column.label): column.width for column in table.columns.values()}
            panel_width = table.size.width
            return widths, panel_width, _stats_rows(app)

    widths, panel_width, rows = asyncio.run(run())
    padding = 2 * len(widths)
    assert sum(widths.values()) + padding <= panel_width
    collapsed_row = rows[2]
    assert collapsed_row[0].startswith(f"    {FOLD_INDICATOR} ")
    for label, width in widths.items():
        assert len(label) <= width, f"{label!r} does not fit in width {width}"
    assert all(len(row[0]) <= widths["Category"] for row in rows)


# ----------------------------------------------------- statistics fold/unfold all ("f")


def test_f_folds_and_unfolds_every_group_at_once(tmp_path, monkeypatch):
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            expanded = _stats_rows(app)

            await pilot.press("f")
            await pilot.pause()
            collapsed = _stats_rows(app)

            await pilot.press("f")  # press again: back to fully expanded
            await pilot.pause()
            reexpanded = _stats_rows(app)

            return expanded, collapsed, reexpanded

    expanded, collapsed, reexpanded = asyncio.run(run())
    names_expanded = [row[0] for row in expanded]
    assert names_expanded == [
        "Food",
        "  Dining",
        "    Restaurants and Fast …",
        "    Fast Food",
        "  Groceries",
        "TOTAL",
    ]
    # Food is the only top-level group, and folding it hides its entire subtree —
    # Dining included — even though Dining itself is also independently foldable.
    assert [row[0] for row in collapsed] == [f"{FOLD_INDICATOR} Food", "TOTAL"]
    assert [row[0] for row in reexpanded] == names_expanded


def test_f_always_collapses_when_any_group_is_expanded(tmp_path, monkeypatch):
    """A mix of folded and unfolded groups is not "some folded, some not" after ``f`` —
    the key always does something visible, so a partial state collapses fully rather
    than being read as "some already folded" and unfolding the rest."""
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            table.move_cursor(row=1)  # Dining
            await pilot.press("space")  # fold just Dining by hand
            await pilot.pause()
            partially_folded = _stats_rows(app)

            await pilot.press("f")  # Food is still expanded, so this collapses all
            await pilot.pause()
            fully_collapsed = _stats_rows(app)

            await pilot.press("f")  # and this expands all, Dining included
            await pilot.pause()
            fully_expanded = _stats_rows(app)

            return partially_folded, fully_collapsed, fully_expanded

    partially_folded, fully_collapsed, fully_expanded = asyncio.run(run())
    assert [row[0] for row in partially_folded] == [
        "Food",
        f"  {FOLD_INDICATOR} Dining",
        "  Groceries",
        "TOTAL",
    ]
    assert [row[0] for row in fully_collapsed] == [f"{FOLD_INDICATOR} Food", "TOTAL"]
    assert [row[0] for row in fully_expanded] == [
        "Food",
        "  Dining",
        "    Restaurants and Fast …",
        "    Fast Food",
        "  Groceries",
        "TOTAL",
    ]


def test_f_does_not_change_any_numbers(tmp_path, monkeypatch):
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            before = _stats_rows(app)
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            await pilot.press("f")
            await pilot.pause()
            after = _stats_rows(app)
            return before, after

    before, after = asyncio.run(run())
    # Food's row (rolled up, including everything folded away) is unchanged; only its
    # name grows the fold indicator.
    assert before[0][0] == "Food"
    assert after[0][0] == f"{FOLD_INDICATOR} Food"
    assert before[0][1:] == after[0][1:]
    # TOTAL, unaffected by folding, is byte-for-byte identical.
    assert before[-1] == after[-1]


def test_f_does_nothing_outside_the_stats_table(tmp_path, monkeypatch):
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            txns = app.query_one("#txns", DataTable)
            txns.focus()
            before_txns = _rows_of(app, "txns")
            await pilot.press("f")
            await pilot.pause()
            after_txns = _rows_of(app, "txns")
            txns_panel = app._panel

            app._run_command("rules")
            await pilot.pause()
            rules = app.query_one("#rules", DataTable)
            rules.focus()
            before_rules = _rows_of(app, "rules")
            await pilot.press("f")
            await pilot.pause()
            after_rules = _rows_of(app, "rules")
            rules_panel = app._panel

            return (
                before_txns,
                after_txns,
                txns_panel,
                before_rules,
                after_rules,
                rules_panel,
            )

    (
        before_txns,
        after_txns,
        txns_panel,
        before_rules,
        after_rules,
        rules_panel,
    ) = asyncio.run(run())
    assert before_txns == after_txns
    assert txns_panel == "txns"
    assert before_rules == after_rules
    assert rules_panel == "rules"


def test_f_still_types_a_literal_f_in_the_command_bar(tmp_path, monkeypatch):
    """The fold-all binding is deliberately not priority=True: a plain 'f' typed into a
    command must reach the Input, not get swallowed as a fold-all keypress."""
    _setup_recent(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            text = "filter food"
            await pilot.press(*text)
            return app.query_one("#command", Input).value

    assert asyncio.run(run()) == "filter food"


def _seed_category_hierarchy_with_income_sibling(tmp_path, monkeypatch):
    """``_seed_category_hierarchy``'s Food tree, plus a leaf ``Income`` category beside
    it — so folding every group leaves one visible, non-foldable row (Income) whose
    table index still has to map back to the right category (see
    test_drill_down_after_fold_all_maps_to_the_row_actually_visible).
    """
    db_path = tmp_path / "deep_with_income.db"
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
        restaurants = categories.ensure_path(
            session, "Food > Dining > Restaurants and Fast Casual Spots"
        )
        fast_food = categories.ensure_path(session, "Food > Dining > Fast Food")
        groceries = categories.ensure_path(session, "Food > Groceries")
        income = categories.ensure_path(session, "Income")
        session.flush()

        def txn(day, amount, description, category):
            session.add(
                Transaction(
                    account_id=account.id,
                    currency_id=currency.id,
                    posted_date=day,
                    description=description,
                    raw_description=description,
                    value_minor=amount,
                    category_id=category.id,
                    category_source="manual",
                    import_hash=f"income-{description}-{day}-{amount}",
                )
            )

        txn(datetime.date(2025, 3, 1), -12_345_678, "Big meal", restaurants)
        txn(datetime.date(2025, 3, 2), -2_345_678, "Fast food", fast_food)
        txn(datetime.date(2025, 3, 3), -3_456_789, "Groceries", groceries)
        txn(datetime.date(2025, 3, 4), 500_000, "Paycheck", income)
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def test_drill_down_after_fold_all_maps_to_the_row_actually_visible(
    tmp_path, monkeypatch
):
    """The same trap the per-row fold has: once ``f`` hides Food's whole subtree, the
    next visible row (Income) is table row 1, and a drill-down there must open Income,
    not whatever used to sit at row 1 before everything collapsed.
    """
    _seed_category_hierarchy_with_income_sibling(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("stats 2025-01-01..2025-12-31")
            await pilot.pause()
            table = app.query_one("#stats_table", DataTable)
            table.focus()
            await pilot.press("f")
            await pilot.pause()
            rows = _stats_rows(app)
            assert rows[1][0] == "Income"  # visible, right after collapsed Food

            table.move_cursor(row=1)
            await pilot.press("enter")
            await pilot.pause()
            return [t.description for t in app._txns]

    descriptions = asyncio.run(run())
    assert descriptions == ["Paycheck"]



# ------------------------------------------------------------------------- chart


CHART_ROWS = (
    # (day, minor amount, description, category)
    (datetime.date(2026, 1, 15), -2000, "SHOP A", "Food"),
    (datetime.date(2026, 1, 20), -1000, "SHOP B", "Food"),
    (datetime.date(2026, 2, 10), -500, "SHOP C", None),
    (datetime.date(2026, 3, 5), 300_000, "PAYCHECK", None),
    (datetime.date(2026, 3, 6), -4000, "SHOP D", "Travel"),
)

# The window the chart tests use, chosen so month buckets land on the seeded data
# exactly. Out: January 30.00, February 5.00, March 40.00. In: March 3,000.00.
CHART_WINDOW = "2026-01-01..2026-03-31"

# 13 cells either side of the axis, per charts.DEFAULT_WIDTH.
HALF = 13


def _setup_chart(tmp_path, monkeypatch):
    """Absolute dates, seeded directly, so bucket boundaries are not relative to today."""
    db_path = tmp_path / "chart.db"
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
        for day, amount, description, category_name in CHART_ROWS:
            category = (
                categories.ensure_path(session, category_name)
                if category_name
                else None
            )
            session.add(
                Transaction(
                    account_id=account.id,
                    currency_id=currency.id,
                    category_id=category.id if category is not None else None,
                    posted_date=day,
                    description=description,
                    raw_description=description,
                    value_minor=amount,
                    import_hash=f"{day}-{amount}-{description}",
                )
            )
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def _chart_rows(app):
    return _rows_of(app, "chart")


def _chart_headers(app):
    table = app.query_one("#chart", DataTable)
    return [str(column.label) for column in table.columns.values()]


def test_the_chart_defaults_to_net_drawn_either_side_of_the_axis(tmp_path, monkeypatch):
    """January and February cost money and grow left; March took in far more than it
    spent, so it grows right. That sign is the whole reason net is the default."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            return _chart_rows(app), app._panel, app._measure, app.focused.id

    rows, panel, measure, focused = asyncio.run(run())
    assert (panel, measure, focused) == ("chart", "net", "chart")
    assert [row[0] for row in rows[:3]] == ["2026-01", "2026-02", "2026-03"]
    # March is the peak (+2,960.00) and fills its whole side.
    assert rows[2][1] == " " * HALF + "│" + "█" * HALF
    # January (-30.00) is a rounding sliver on the other side of the axis.
    assert rows[0][1] == " " * (HALF - 1) + "█" + "│" + " " * HALF
    assert [row[2] for row in rows[:3]] == ["-30.00", "-5.00", "2,960.00"]


def test_the_net_axis_lines_up_on_every_row(tmp_path, monkeypatch):
    """A diverging chart whose centre wanders is not readable as a chart."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            return [row[1] for row in _chart_rows(app)[:3]]

    bars = asyncio.run(run())
    assert all(len(bar) == 2 * HALF + 1 for bar in bars)
    assert all(bar[HALF] == "│" for bar in bars)


def test_a_bucket_that_came_out_even_sits_on_the_axis(tmp_path, monkeypatch):
    """200.00 spent and 200.00 refunded is not a heavy month, and must not draw as one."""
    session_factory = _setup_chart(tmp_path, monkeypatch)
    with session_factory() as session:
        currency = session.query(Currency).one()
        account = session.query(Account).one()
        for amount, description in ((-20_000, "BIG BUY"), (20_000, "REFUNDED")):
            session.add(
                Transaction(
                    account_id=account.id,
                    currency_id=currency.id,
                    posted_date=datetime.date(2026, 2, 20),
                    description=description,
                    raw_description=description,
                    value_minor=amount,
                    import_hash=f"even-{description}",
                )
            )
        session.commit()

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month spending")
            await pilot.pause()
            spending = _chart_rows(app)
            app._run_command("chart net")
            await pilot.pause()
            return spending, _chart_rows(app)

    spending, net = asyncio.run(run())
    # Charted as spending, February is now the biggest month of the three.
    assert spending[1][2] == "205.00"
    assert spending[1][1] == "█" * 27
    # Charted as net, it is a sliver: the refund cancelled almost all of it.
    assert net[1][2] == "-5.00"
    assert net[1][1].count("█") == 1


def test_the_measure_key_cycles_net_spending_income(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            app.query_one("#chart", DataTable).focus()
            seen = [(app._measure, _chart_headers(app)[1])]
            for _ in range(3):
                await pilot.press("m")
                await pilot.pause()
                seen.append((app._measure, _chart_headers(app)[1]))
            return seen

    seen = asyncio.run(run())
    assert [measure for measure, _header in seen] == ["net", "spend", "income", "net"]
    # The header names what is being drawn, and the net one explains its own axis.
    assert seen[0][1] == "Net (← out | in →)"
    assert seen[1][1] == "Spending"
    assert seen[2][1] == "Income"


def test_the_figure_columns_follow_the_measure(tmp_path, monkeypatch):
    """The charted measure comes first, then whatever is most worth seeing beside it."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            headers, rows = {}, {}
            for measure in ("net", "spending", "income"):
                app._run_command(f"chart {CHART_WINDOW} month {measure}")
                await pilot.pause()
                headers[measure] = _chart_headers(app)
                rows[measure] = _chart_rows(app)
            return headers, rows

    headers, rows = asyncio.run(run())
    assert headers["net"] == ["Period", "Net (← out | in →)", "Net", "Out", "Txns"]
    assert headers["spending"] == ["Period", "Spending", "Out", "Net", "Txns"]
    assert headers["income"] == ["Period", "Income", "In", "Net", "Txns"]
    # March: 40.00 out, 3,000.00 in, 2,960.00 net — the same three figures, reordered.
    assert rows["net"][2][2:4] == ["2,960.00", "40.00"]
    assert rows["spending"][2][2:4] == ["40.00", "2,960.00"]
    assert rows["income"][2][2:4] == ["3,000.00", "2,960.00"]


def test_spending_and_income_bars_are_left_anchored_with_no_axis(tmp_path, monkeypatch):
    """Only net has a sign to encode, so the other two use the conventional bar and the
    full width of the column rather than half of it."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month spending")
            await pilot.pause()
            spending = _chart_rows(app)
            app._run_command("chart income")
            await pilot.pause()
            return spending, _chart_rows(app)

    spending, income = asyncio.run(run())
    assert all("│" not in row[1] for row in spending[:3])
    # March spends the most, so it fills the whole 27-column width.
    assert spending[2][1] == "█" * 27
    assert spending[0][1] == "█" * 20 + "▎"  # 30.00 of 40.00
    # Income lands only in March, so the other two months are empty.
    assert income[2][1] == "█" * 27
    assert [income[0][1], income[1][1]] == ["", ""]
    assert [income[0][2], income[1][2]] == ["0.00", "0.00"]


def test_a_quiet_bucket_is_drawn_empty_rather_than_dropped(tmp_path, monkeypatch):
    """A month with nothing in it must still occupy a row; a chart that silently skips
    it reads as though the calendar itself had no April."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("chart 2026-01-01..2026-04-30 month spending")
            await pilot.pause()
            return _chart_rows(app)

    rows = asyncio.run(run())
    assert [row[0] for row in rows[:4]] == ["2026-01", "2026-02", "2026-03", "2026-04"]
    assert rows[3][1] == ""  # April: present, empty, and not a zero-width lie
    assert rows[3][2] == "0.00"


def test_chart_total_row_follows_the_measure(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            totals = {}
            for measure in ("net", "spending", "income"):
                app._run_command(f"chart {CHART_WINDOW} month {measure}")
                await pilot.pause()
                totals[measure] = _chart_rows(app)[-1]
            return totals

    totals = asyncio.run(run())
    for measure, row in totals.items():
        assert row[0] == "TOTAL", measure
        assert row[4] == "5", measure
    # 3,000.00 in less 75.00 out, and the average is of whatever is charted.
    assert totals["net"][2:4] == ["2,925.00", "75.00"]
    assert totals["net"][1] == "avg 975.00/month"
    assert totals["spending"][2:4] == ["75.00", "2,925.00"]
    assert totals["spending"][1] == "avg 25.00/month"
    assert totals["income"][2:4] == ["3,000.00", "2,925.00"]
    assert totals["income"][1] == "avg 1,000.00/month"


def test_an_empty_window_charts_no_total_row(tmp_path, monkeypatch):
    """Same rule as the statistics table: a row of zeroes reads as a finding."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("chart 2020-01-01..2020-03-31 month")
            await pilot.pause()
            return _chart_rows(app)

    rows = asyncio.run(run())
    assert [row[0] for row in rows] == ["2020-01", "2020-02", "2020-03"]
    assert all("█" not in row[1] for row in rows)


def test_selecting_a_category_rescopes_the_open_chart(tmp_path, monkeypatch):
    """The headline of the feature: the sidebar is how a category is chosen, so the bars
    have to follow it without the user reissuing the command."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month spending")
            await pilot.pause()
            everything = _chart_rows(app)

            food = next(c for c in app._categories if c.name == "Food")
            app.category_filter = food.id
            app.reload()
            await pilot.pause()
            return everything, _chart_rows(app), str(app.query_one("#status", Static).content)

    everything, food_rows, status = asyncio.run(run())
    assert everything[-1][2] == "75.00"
    # Food is January's 20.00 + 10.00 and nothing else.
    assert [row[2] for row in food_rows[:3]] == ["30.00", "0.00", "0.00"]
    assert food_rows[-1][2] == "30.00"
    assert food_rows[0][1] == "█" * 27  # rescaled: January is now the peak
    # And the status line says which category, or the chart is unreadable.
    assert "Food" in status


def test_the_measure_survives_a_new_period_but_the_bucket_is_rederived(
    tmp_path, monkeypatch
):
    """The measure is a question about the money; the bucket is a property of the range.
    Daily bars chosen for one month are unreadable stretched over a year."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("chart 2026-01-01..2026-01-20 income")
            await pilot.pause()
            first = (app._bucket, app._measure)
            app._run_command("chart 2026-01-01..2026-12-31")
            await pilot.pause()
            return first, (app._bucket, app._measure)

    first, second = asyncio.run(run())
    assert first == ("day", "income")
    assert second == ("month", "income")


def test_the_bucket_key_cycles_day_week_month(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            app.query_one("#chart", DataTable).focus()
            seen = [app._bucket]
            for _ in range(3):
                await pilot.press("b")
                await pilot.pause()
                seen.append(app._bucket)
            return seen, len(_chart_rows(app))

    seen, row_count = asyncio.run(run())
    assert seen == ["month", "day", "week", "month"]
    assert row_count == 4  # back to three months and a total


def test_the_chart_keys_are_inert_outside_the_chart(tmp_path, monkeypatch):
    """'b' and 'm' are plain letters, so they must stay typeable everywhere else."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            app._run_command("stats " + CHART_WINDOW)
            await pilot.pause()
            app.query_one("#stats_table", DataTable).focus()
            await pilot.press("b")
            await pilot.press("m")
            await pilot.pause()
            on_stats = (app._bucket, app._measure)

            app.query_one("#command", Input).focus()
            await pilot.press("b", "m")
            await pilot.pause()
            return on_stats, (app._bucket, app._measure), app.query_one("#command", Input).value

    on_stats, after_typing, typed = asyncio.run(run())
    assert on_stats == ("month", "net") and after_typing == ("month", "net")
    assert typed == "bm"  # both reached the command bar as ordinary characters


def test_the_bucket_defaults_to_the_window_length(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            picked = {}
            for spec in (
                "2026-01-01..2026-01-20",
                "2026-01-01..2026-03-01",
                "2026-01-01..2026-12-31",
            ):
                app._run_command(f"chart {spec}")
                await pilot.pause()
                picked[spec] = app._bucket
            return picked

    picked = asyncio.run(run())
    assert picked["2026-01-01..2026-01-20"] == "day"
    assert picked["2026-01-01..2026-03-01"] == "week"
    assert picked["2026-01-01..2026-12-31"] == "month"


def test_a_bucket_or_measure_on_its_own_redraws_the_open_chart(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            app._run_command("chart week")
            await pilot.pause()
            after_bucket = (app._bucket, app._measure)
            app._run_command("chart income")
            await pilot.pause()
            return after_bucket, (app._bucket, app._measure), app.window.start, app.window.end

    after_bucket, after_measure, start, end = asyncio.run(run())
    assert after_bucket == ("week", "net")
    assert after_measure == ("week", "income")  # the bucket it was already using
    assert (start, end) == (datetime.date(2026, 1, 1), datetime.date(2026, 3, 31))


def test_both_words_can_be_given_at_once_in_either_order(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} week spending")
            await pilot.pause()
            first = (app._bucket, app._measure)
            app._run_command(f"chart {CHART_WINDOW} income day")
            await pilot.pause()
            return first, (app._bucket, app._measure)

    first, second = asyncio.run(run())
    assert first == ("week", "spend")
    assert second == ("day", "income")


def test_a_bucket_with_no_window_yet_says_so(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            messages = []
            app.notify = lambda text, **kw: messages.append(text)
            app._run_command("chart week")
            await pilot.pause()
            return messages, app._panel

    messages, panel = asyncio.run(run())
    assert panel == "txns"  # it did not open an empty chart
    assert "No period yet" in messages[0] and "chart 3m week" in messages[0]


def test_bare_chart_opens_the_picker_and_the_picker_opens_the_chart(tmp_path, monkeypatch):
    """The period picker is shared with `stats`, so it has to remember who asked."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("chart")
            await pilot.pause()
            picker = app._panel
            app.query_one("#periods", DataTable).move_cursor(row=0)  # 1 month
            await pilot.press("enter")
            await pilot.pause()
            return picker, app._panel

    picker, landed = asyncio.run(run())
    assert picker == "periods"
    assert landed == "chart"


def test_the_picker_still_opens_the_statistics_panel_for_stats(tmp_path, monkeypatch):
    """The other half of the same seam: routing the picker must not have stolen it."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("chart")  # leaves the picker pointed at the chart
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            app._run_command("stats")
            await pilot.pause()
            app.query_one("#periods", DataTable).move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            return app._panel

    assert asyncio.run(run()) == "stats"


def test_a_custom_range_typed_into_the_picker_reaches_the_chart(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("chart")
            await pilot.pause()
            table = app.query_one("#periods", DataTable)
            table.move_cursor(row=len(stats.PRESETS))  # Custom…
            await pilot.press("enter")
            await pilot.pause()
            app.query_one("#command", Input).value = CHART_WINDOW
            await pilot.press("enter")
            await pilot.pause()
            return app._panel, app._bucket, _chart_rows(app)

    panel, bucket, rows = asyncio.run(run())
    assert panel == "chart"
    # 90 days, so it buckets by week without being asked — a range typed into the picker
    # goes through the same default as one typed on the command line.
    assert bucket == "week"
    assert rows[-1][0] == "TOTAL" and rows[-1][2] == "2,925.00"


def test_chart_status_line_fits_the_main_panel(tmp_path, monkeypatch):
    """Same 92-column budget as every other status line, with a long category name and
    five-figure money in it."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("chart 2020-01-01..2026-12-31 month income")
            await pilot.pause()
            food = next(c for c in app._categories if c.name == "Food")
            app.category_filter = food.id
            app.reload()
            await pilot.pause()
            return str(app.query_one("#status", Static).content)

    status = asyncio.run(run())
    assert len(status) <= 92, f"{len(status)} columns: {status!r}"
    assert "income/month" in status and "Food" in status and "peak" in status


def test_chart_table_fits_the_main_panel(tmp_path, monkeypatch):
    """Every column, the full-width bar included, has to land inside the panel."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month spending")
            await pilot.pause()
            buffer = io.StringIO()
            Console(file=buffer, width=130).print(app.screen._compositor)
            return buffer.getvalue()

    rendered = asyncio.run(run())
    header = next(line for line in rendered.splitlines() if "Period" in line)
    for column in ("Period", "Spending", "Out", "Net", "Txns"):
        assert column in header, f"{column!r} clipped: {header.strip()!r}"
    # The widest bar plus every other column still leaves the panel's right border on
    # screen, so nothing has been pushed off the edge.
    peak_line = next(line for line in rendered.splitlines() if "█" * 27 in line)
    assert len(peak_line.rstrip()) <= 130


def test_the_net_header_survives_its_own_column_width(tmp_path, monkeypatch):
    """The net header carries the legend for the axis, so it is the longest of the three
    and the one that would clip first."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            buffer = io.StringIO()
            Console(file=buffer, width=130).print(app.screen._compositor)
            return buffer.getvalue()

    rendered = asyncio.run(run())
    assert "Net (← out | in →)" in rendered


def test_footer_shows_the_chart_keys(tmp_path, monkeypatch):
    """The footer truncates mid-word, so a new binding has to be checked on the panel it
    actually appears on — see test_footer_shows_every_shortcut_at_a_normal_width."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            app.query_one("#chart", DataTable).focus()
            await pilot.pause()
            buffer = io.StringIO()
            Console(file=buffer, width=130).print(app.screen._compositor)
            return buffer.getvalue()

    rendered = asyncio.run(run())
    footer = next(line for line in rendered.splitlines() if "palette" in line)
    for label in (
        "Refresh", "Clear", "Rename", "Categorise", "Transactions", "Bucket", "Measure",
    ):
        assert label in footer, f"{label!r} missing or truncated: {footer.strip()!r}"


def test_escape_leaves_the_chart_for_the_transactions(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            return app._panel, app.query_one("#chart", DataTable).display

    panel, chart_visible = asyncio.run(run())
    assert panel == "txns"
    assert chart_visible is False


def test_graph_is_a_synonym_for_chart(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"graph {CHART_WINDOW} month")
            await pilot.pause()
            return app._panel

    assert asyncio.run(run()) == "chart"


def test_a_bad_chart_period_is_reported_not_opened(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            messages = []
            app.notify = lambda text, **kw: messages.append(text)
            app._run_command("chart last tuesday")
            await pilot.pause()
            return messages, app._panel

    messages, panel = asyncio.run(run())
    assert panel == "txns"
    assert "last tuesday" in messages[0]


def test_transfers_left_out_of_the_bars_are_counted_in_the_status(tmp_path, monkeypatch):
    """The bars exclude transfers, as every other figure does. Dropping money between
    your own accounts without saying so reads as missing spending."""
    session_factory = _setup_chart(tmp_path, monkeypatch)
    with session_factory() as session:
        currency = session.query(Currency).one()
        savings = Account(name="Savings", currency_id=currency.id)
        session.add(savings)
        session.flush()
        checking = session.query(Account).filter_by(name="Checking").one()
        for account, amount in ((checking, -50_000), (savings, 50_000)):
            session.add(
                Transaction(
                    account_id=account.id,
                    currency_id=currency.id,
                    posted_date=datetime.date(2026, 2, 14),
                    description="MOVE",
                    raw_description="MOVE",
                    value_minor=amount,
                    import_hash=f"move-{account.id}",
                )
            )
        session.commit()
        transfers.detect_transfers(session)
        session.commit()

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month spending")
            await pilot.pause()
            return _chart_rows(app), str(app.query_one("#status", Static).content)

    rows, status = asyncio.run(run())
    # February's 500.00 transfer is not in the bar...
    assert rows[1][2] == "5.00"
    # ...and the status line says two transactions went missing rather than hiding it.
    assert f"{TRANSFER_MARK} 2" in status


# ------------------------------------------------------- arrow-key chart navigation


def test_right_arrow_on_a_chart_bar_drills_down_like_enter(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            table = app.query_one("#chart", DataTable)
            table.focus()
            table.move_cursor(row=0)  # January
            await pilot.press("right")
            await pilot.pause()
            return (
                app._panel,
                app.date_filter,
                sorted(t.description for t in app._txns),
            )

    panel, date_filter, descriptions = asyncio.run(run())
    assert panel == "txns"
    assert date_filter == (datetime.date(2026, 1, 1), datetime.date(2026, 1, 31))
    assert descriptions == ["SHOP A", "SHOP B"]


def test_left_arrow_returns_to_the_chart_with_the_full_series(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            before = _chart_rows(app)
            table = app.query_one("#chart", DataTable)
            table.focus()
            table.move_cursor(row=2)  # March
            await pilot.press("right")
            await pilot.pause()
            drilled = (app._panel, app.date_filter)

            await pilot.press("left")
            await pilot.pause()
            back_panel = app._panel
            after = _chart_rows(app)
            cursor_row = app.query_one("#chart", DataTable).cursor_row
            return before, drilled, back_panel, after, app.date_filter, cursor_row

    before, drilled, back_panel, after, date_filter, cursor_row = asyncio.run(run())
    assert drilled[0] == "txns" and drilled[1] is not None
    assert back_panel == "chart"
    # The date filter the drill-down set is gone, or the chart would be scoped to March.
    assert date_filter is None
    assert after == before
    assert cursor_row == 2  # back on the March row that was drilled from


def test_a_partial_edge_bucket_drills_into_only_the_days_in_the_window(
    tmp_path, monkeypatch
):
    """The window's own edges, not the calendar month's. Without clamping,
    bucket_date_range would reconstruct January as the whole month and March as the
    whole month, pulling in SHOP A (before the window) and SHOP D (after it) — rows the
    bars themselves never counted, so the drilled-down list would stop summing to them.
    """
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("chart 2026-01-18..2026-03-05 month net")
            await pilot.pause()
            table = app.query_one("#chart", DataTable)
            table.focus()

            table.move_cursor(row=0)  # January, clamped to 01-18..01-31
            await pilot.press("right")
            await pilot.pause()
            january = sorted(t.description for t in app._txns), app.date_filter
            await pilot.press("left")
            await pilot.pause()

            table.move_cursor(row=2)  # March, clamped to 03-01..03-05
            await pilot.press("right")
            await pilot.pause()
            march = sorted(t.description for t in app._txns), app.date_filter

            return january, march

    january, march = asyncio.run(run())
    # SHOP A (2026-01-15) is outside the window; only SHOP B (01-20) counts.
    assert january == (["SHOP B"], (datetime.date(2026, 1, 18), datetime.date(2026, 1, 31)))
    # SHOP D (2026-03-06) is outside the window; only the 03-05 paycheck counts.
    assert march == (["PAYCHECK"], (datetime.date(2026, 3, 1), datetime.date(2026, 3, 5)))


def test_left_arrow_after_a_chart_drill_restores_filters_set_before_it(
    tmp_path, monkeypatch
):
    """A chart drill-down only ever narrows the date; a category filter set before it
    must survive the round trip untouched."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            food = next(c for c in app._categories if c.name == "Food")
            app.category_filter = food.id
            app.reload()
            await pilot.pause()

            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            table = app.query_one("#chart", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("right")
            await pilot.pause()
            drilled_category_filter = app.category_filter

            await pilot.press("left")
            await pilot.pause()
            return food.id, drilled_category_filter, app.category_filter, app._panel

    food_id, drilled_category_filter, restored_category_filter, panel = asyncio.run(run())
    assert drilled_category_filter == food_id
    assert restored_category_filter == food_id
    assert panel == "chart"


def test_a_new_filter_clears_the_stale_chart_drill_flag(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            table = app.query_one("#chart", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("right")
            await pilot.pause()
            drilled = app._drilled_from_chart

            app._run_command("filter shop")
            await pilot.pause()
            after_filter = app._drilled_from_chart
            panel = app._panel

            await pilot.press("left")
            await pilot.pause()
            return drilled, after_filter, panel, app._panel

    drilled, after_filter, panel_before_left, panel_after_left = asyncio.run(run())
    assert drilled is True
    assert after_filter is False
    assert panel_before_left == "txns"
    assert panel_after_left == "txns"  # left arrow did not send us anywhere


def test_escape_clears_the_stale_chart_drill_down_flag(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            table = app.query_one("#chart", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("right")
            await pilot.pause()

            await pilot.press("escape")
            await pilot.pause()
            return app._drilled_from_chart

    assert asyncio.run(run()) is False


def test_the_chart_total_row_cannot_be_drilled(tmp_path, monkeypatch):
    """Mirrors the statistics panel's guard: the closing TOTAL row is not a bar."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            table = app.query_one("#chart", DataTable)
            table.focus()
            table.move_cursor(row=table.row_count - 1)  # the TOTAL row
            await pilot.press("right")
            await pilot.pause()
            return app._panel, app.date_filter

    panel, date_filter = asyncio.run(run())
    assert panel == "chart"
    assert date_filter is None


def test_drilling_from_stats_and_from_the_chart_do_not_confuse_each_others_back_link(
    tmp_path, monkeypatch
):
    """Leaving one drilled-down view for the other panel invalidates the back-link it
    leaves behind, so a stale left arrow never sends you somewhere unrelated."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"stats {CHART_WINDOW}")
            await pilot.pause()
            app.query_one("#stats_table", DataTable).focus()
            await pilot.press("right")
            await pilot.pause()
            drilled_from_stats = app._drilled_from_stats

            # Opening the chart leaves the stats drill behind.
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            after_chart_open = (app._drilled_from_stats, app._drilled_from_chart)

            app.query_one("#chart", DataTable).focus()
            await pilot.press("right")
            await pilot.pause()
            drilled_from_chart = app._drilled_from_chart

            # And the reverse: opening stats leaves the chart drill behind in turn.
            app._run_command(f"stats {CHART_WINDOW}")
            await pilot.pause()
            after_stats_open = (app._drilled_from_stats, app._drilled_from_chart)

            return (
                drilled_from_stats,
                after_chart_open,
                drilled_from_chart,
                after_stats_open,
            )

    (
        drilled_from_stats,
        after_chart_open,
        drilled_from_chart,
        after_stats_open,
    ) = asyncio.run(run())
    assert drilled_from_stats is True
    assert after_chart_open == (False, False)
    assert drilled_from_chart is True
    assert after_stats_open == (False, False)


def test_the_chart_drill_down_keys_are_advertised_in_the_footer(tmp_path, monkeypatch):
    """The chart's twin of test_the_drill_down_keys_are_advertised_in_the_footer."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"chart {CHART_WINDOW} month")
            await pilot.pause()
            app.query_one("#chart", DataTable).focus()
            await pilot.pause()
            on_chart = dict(app.active_bindings)

            table = app.query_one("#chart", DataTable)
            table.move_cursor(row=0)
            await pilot.press("right")
            await pilot.pause()
            drilled = dict(app.active_bindings)

            return on_chart, drilled

    def action_for(bindings, key):
        binding = bindings.get(key)
        return binding.binding.action if binding else None

    on_chart, drilled = asyncio.run(run())
    assert action_for(on_chart, "right") == "drill_down"
    assert on_chart["right"].binding.description == "Drill down"
    assert action_for(drilled, "left") == "drill_up"


# ---------------------------------------------------------- browsing the import inbox


NESTED_CSV = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2025-08-01,2025-08-02,8207,NESTED SHOP,Dining,7.00,
2025-08-03,2025-08-04,8207,NESTED SHOP,Dining,8.00,
"""


def _nested_inbox(tmp_path, monkeypatch):
    """An inbox with one CSV at the top and a month folder holding another."""
    inbox = _inbox(tmp_path, monkeypatch, top=CSV)
    month = inbox / "2026-08"
    month.mkdir()
    (month / "nested.csv").write_text(NESTED_CSV, encoding="utf-8")
    return inbox


def test_a_subdirectory_is_offered_as_a_row_with_its_csv_count(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _nested_inbox(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("import")
            await pilot.pause()
            return _rows_of(app, "imports")

    rows = asyncio.run(run())
    # Folders sort above files, and the count says whether descending is worth it.
    assert rows[0][0] == "▸ 2026-08"
    assert rows[0][1] == "1"
    assert rows[0][2] == "folder"
    assert rows[1][0] == "top.csv"


def test_enter_on_a_folder_descends_into_it(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    inbox = _nested_inbox(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("import")
            await pilot.pause()
            await pilot.press("enter")  # the folder row is first
            await pilot.pause()
            return app._import_dir, _rows_of(app, "imports")

    where, rows = asyncio.run(run())
    assert where == inbox / "2026-08"
    # Now inside: the way back, then the file that lives here. top.csv is not in it.
    assert rows[0][0] == "▸ .." and rows[0][2] == "up"
    assert rows[1][0] == "nested.csv"
    assert "top.csv" not in [row[0] for row in rows]


def test_the_way_back_is_offered_only_below_the_root(tmp_path, monkeypatch):
    """It is the only way up, so stopping at the root is what keeps browsing inside the
    inbox rather than loose in the filesystem."""
    _setup(tmp_path, monkeypatch)
    inbox = _nested_inbox(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("import")
            await pilot.pause()
            at_root = [row[0] for row in _rows_of(app, "imports")]
            app._open_import_dir(inbox / "2026-08")
            await pilot.pause()
            below = [row[0] for row in _rows_of(app, "imports")]
            await pilot.press("enter")  # ".." is the first row down here
            await pilot.pause()
            return at_root, below, app._import_dir

    at_root, below, back = asyncio.run(run())
    assert "▸ .." not in at_root
    assert "▸ .." in below
    assert back == inbox


def test_enter_imports_the_file_the_cursor_is_actually_on(tmp_path, monkeypatch):
    """The navigation rows shift every file down, so a row index has to be offset by
    them — otherwise enter on 'nested.csv' would import whatever used to be at that
    index, or nothing at all."""
    _setup(tmp_path, monkeypatch)
    inbox = _nested_inbox(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("import")
            await pilot.pause()
            app._open_import_dir(inbox / "2026-08")
            await pilot.pause()
            table = app.query_one("#imports", DataTable)
            table.move_cursor(row=1)  # past "..", onto nested.csv
            await pilot.press("enter")
            await pilot.pause()
            app._run_command("all")
            await pilot.pause()
            return [t.description for t in app._txns]

    descriptions = asyncio.run(run())
    assert descriptions.count("NESTED SHOP") == 2


def test_the_status_line_says_which_folder_is_open(tmp_path, monkeypatch):
    """A panel that does not say where it is looking invites importing the wrong month."""
    _setup(tmp_path, monkeypatch)
    inbox = _nested_inbox(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("import")
            await pilot.pause()
            at_root = str(app.query_one("#status", Static).content)
            app._open_import_dir(inbox / "2026-08")
            await pilot.pause()
            return at_root, str(app.query_one("#status", Static).content)

    at_root, below = asyncio.run(run())
    assert at_root.startswith("to_import")  # the inbox itself, named not "."
    assert below.startswith("2026-08")
    assert "1 file(s), 1 ready" in below


def test_import_all_takes_the_open_folder_not_the_whole_tree(tmp_path, monkeypatch):
    """'all' should import what the panel is showing, not quietly reach into folders you
    have not opened."""
    _setup(tmp_path, monkeypatch)
    inbox = _nested_inbox(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("import")
            await pilot.pause()
            app._run_command("import all")
            await pilot.pause()
            at_root = {t.description for t in app._txns}

            app._open_import_dir(inbox / "2026-08")
            await pilot.pause()
            app._run_command("import all")
            await pilot.pause()
            app._run_command("all")
            await pilot.pause()
            return at_root, {t.description for t in app._txns}

    at_root, after_descending = asyncio.run(run())
    assert "NESTED SHOP" not in at_root  # the folder was not reached into
    assert "COFFEE SHOP A" in at_root
    assert "NESTED SHOP" in after_descending


def test_an_empty_folder_says_so_rather_than_looking_broken(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    inbox = _inbox(tmp_path, monkeypatch, top=CSV)
    (inbox / "empty").mkdir()

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            messages = []
            app.notify = lambda text, **kw: messages.append(text)
            app._run_command("import")
            await pilot.pause()
            app._open_import_dir(inbox / "empty")
            await pilot.pause()
            return messages, _rows_of(app, "imports")

    messages, rows = asyncio.run(run())
    assert any("Nothing to import in empty" in m for m in messages)
    assert rows[0][0] == "▸ .."  # still navigable back out


# ------------------------------------------------------------------------- pie


def test_bare_pie_opens_the_picker_and_the_picker_opens_the_pie(tmp_path, monkeypatch):
    """The period picker is shared with stats/chart, so it has to remember who asked."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("pie")
            await pilot.pause()
            picker = app._panel
            app.query_one("#periods", DataTable).move_cursor(row=0)  # 1 month
            await pilot.press("enter")
            await pilot.pause()
            return picker, app._panel

    picker, landed = asyncio.run(run())
    assert picker == "periods"
    assert landed == "pie"


def test_pie_with_a_period_skips_the_picker(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"pie {CHART_WINDOW}")
            await pilot.pause()
            return app._panel, app.window.start, app.window.end

    panel, start, end = asyncio.run(run())
    assert panel == "pie"
    assert (start, end) == (datetime.date(2026, 1, 1), datetime.date(2026, 3, 31))


def test_pie_only_shows_depth_zero_categories_with_net_spend(tmp_path, monkeypatch):
    """Food (depth 0, real net spend) gets a wedge; Dining/Groceries (its descendants,
    already rolled into Food) do not, and neither does Income (depth 0 but net
    positive) — the exact filter stats.Report.categories warns about."""
    _seed_category_hierarchy_with_income_sibling(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("pie 2025-01-01..2025-12-31")
            await pilot.pause()
            return [s.name for s in app._pie.slices]

    names = asyncio.run(run())
    assert names == ["Food"]


def test_a_window_with_no_net_spending_shows_a_message_instead_of_a_blank_pie(
    tmp_path, monkeypatch
):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("pie 2020-01-01..2020-03-31")  # nothing seeded that early
            await pilot.pause()
            return app._pie.slices, str(app.query_one("#pie", Static).content)

    slices, content = asyncio.run(run())
    assert slices == []
    assert "No net spending" in content


def test_escape_leaves_the_pie_for_the_transactions(tmp_path, monkeypatch):
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"pie {CHART_WINDOW}")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            return app._panel, app.query_one("#pie", Static).display

    panel, pie_visible = asyncio.run(run())
    assert panel == "txns"
    assert pie_visible is False


def test_selecting_a_category_rescopes_the_open_pie(tmp_path, monkeypatch):
    """Like the chart, the pie is scoped by whatever filters are active — filtering to
    one category leaves a single, whole-circle wedge behind."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"pie {CHART_WINDOW}")
            await pilot.pause()
            everything = [s.name for s in app._pie.slices]

            food = next(c for c in app._categories if c.name == "Food")
            app.category_filter = food.id
            app.reload()
            await pilot.pause()
            return everything, app._pie.slices

    everything, food_slices = asyncio.run(run())
    assert set(everything) == {"Food", "Travel"}
    assert [s.name for s in food_slices] == ["Food"]
    assert food_slices[0].share == 1.0


def test_pie_status_line_fits_the_main_panel(tmp_path, monkeypatch):
    """Same 92-column budget as every other status line, against a custom range (the
    longest label) and a real year of spending in the hundreds of thousands."""
    _seed_category_hierarchy(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command("pie 2025-01-01..2025-12-31")
            await pilot.pause()
            return str(app.query_one("#status", Static).content)

    status = asyncio.run(run())
    assert len(status) <= 92, f"{len(status)} columns: {status!r}"
    assert "category" in status and "net spend" in status


def test_pie_panel_looks_like_a_circle_and_fits_the_main_panel(tmp_path, monkeypatch):
    """Renders the real compositor rather than assuming the geometry reads as round —
    checks every legend column lands inside the panel, with several categories, a long
    name, and a six-figure amount, the shape most likely to clip."""
    session_factory = _setup_chart(tmp_path, monkeypatch)
    with session_factory() as session:
        currency = session.query(Currency).one()
        account = session.query(Account).one()
        big = categories.ensure_path(
            session, "Restaurants and Fast Casual Spots Somewhere Nearby"
        )
        session.add(
            Transaction(
                account_id=account.id,
                currency_id=currency.id,
                category_id=big.id,
                posted_date=datetime.date(2026, 1, 10),
                description="BIG SPEND",
                raw_description="BIG SPEND",
                value_minor=-123_456_789,
                import_hash="big-spend",
            )
        )
        session.commit()

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"pie {CHART_WINDOW}")
            await pilot.pause()
            buffer = io.StringIO()
            Console(file=buffer, width=130).print(app.screen._compositor)
            return buffer.getvalue(), [s.name for s in app._pie.slices]

    rendered, names = asyncio.run(run())
    assert all(len(line.rstrip()) <= 130 for line in rendered.splitlines())
    assert set(names) == {"Food", "Travel", "Restaurants and Fast Casual Spots Somewhere Nearby"}
    # The pie itself drew something...
    assert charts.BLOCK in rendered
    # ...and the legend beside it names the categories, their shares, and their
    # amounts, none of it clipped off the edge of the panel.
    assert "Food" in rendered and "Travel" in rendered
    assert "Restaurants and Fas" in rendered  # the long name, truncated to fit the column
    assert "1,234,567.89" in rendered  # a six-figure amount, formatted not raw


def test_a_new_filter_does_not_disturb_an_open_pie_until_reload(tmp_path, monkeypatch):
    """Sanity check that the pie panel survives reload() being called for reasons that
    have nothing to do with it (e.g. importing more data) without erroring."""
    _setup_chart(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_command(f"pie {CHART_WINDOW}")
            await pilot.pause()
            app.reload()
            await pilot.pause()
            return app._panel, [s.name for s in app._pie.slices]

    panel, names = asyncio.run(run())
    assert panel == "pie"
    assert set(names) == {"Food", "Travel"}
