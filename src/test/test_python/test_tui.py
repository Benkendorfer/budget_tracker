"""Tests for the TUI's rename shortcut (ctrl+n).

Driven through Textual's ``run_test`` pilot. ``asyncio.run`` wraps each case so the
suite does not need pytest-asyncio.
"""

import asyncio
import datetime

from textual.widgets import DataTable, Input, ListView, Static

from budget_tracker import categories, formats, queries, stats, transfers, vendors
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.importer import import_csv, read_header_and_rows
from budget_tracker.models import Account, Currency, Transaction
from helpers import learn_format
from budget_tracker.tui import TRANSFER_MARK, BudgetApp

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
    assert [row[0] for row in rows] == ["Housing", "Dining", "Income"]
    assert [row[2] for row in rows] == ["-1,000.00", "-40.00", "2,000.00"]
    assert [row[4] for row in rows] == ["96.2%", "3.8%", "0.0%"]
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
    assert rows == [["Dining", "3", "-10.50", "-10.31", "100.0%", ""]]
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
    assert state[1] is True and rows == [["Dining", "3", "-10.50", "-10.31", "100.0%", ""]]


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
    assert len(before) == 3
    # Averages depend on the length of a window anchored to today, so this case checks
    # the scope; the fixed custom range above pins the arithmetic.
    # The trailing "" is % parent, blank at depth 0.
    assert [row[:3] + row[4:] for row in filtered_rows] == [
        ["Dining", "2", "-40.00", "100.0%", ""]
    ]
    assert "2 txns" in filtered_state[2] and "out -40.00" in filtered_state[2]
    assert [row[:3] + row[4:] for row in housing_rows] == [
        ["Housing", "1", "-1,000.00", "100.0%", ""]
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
    assert [r[0] for r in rows] == ["Housing", "Dining", "Income"]
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
    assert rows[0][-1] == ""
    assert all(row[-1].endswith("%") for row in rows[1:])


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


def test_category_command_builds_a_nested_path(tmp_path, monkeypatch):
    """``Dining`` already exists (top-level, from the CSV's bank-supplied category) and
    has transactions; ``ensure_path`` rescues it into place under the new ``Food``
    rather than forking a duplicate (see categories._step)."""
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            app._run_command("category Food > Dining > Restaurants")
            await pilot.pause()
            sidebar = [
                str(item.children[0].content)
                for item in app.query_one("#categories", ListView).children
            ]
            depths = {c.name: c.depth for c in app._categories}
            return [n.message for n in app._notifications], sidebar, depths

    messages, sidebar, depths = asyncio.run(run())
    assert any("Food > Dining > Restaurants" in m for m in messages)
    assert depths["Food"] == 0
    assert depths["Dining"] == 1  # rescued under Food, still carrying its 3 transactions
    # Restaurants has no transactions of its own yet, so get_categories (rolled up)
    # correctly omits it — this just proves the sidebar still renders, indented.
    assert any(label.startswith("Food (") for label in sidebar)
    assert any(label.startswith("  Dining (") for label in sidebar)


def test_category_command_one_element_moves_to_top_level(tmp_path, monkeypatch):
    # The fixture CSV's rows import with the bank's own top-level "Dining" category;
    # nesting it under "Food" rescues that existing (and populated) category into place
    # rather than creating an unrelated new one (see categories.ensure_path).
    session_factory = _setup(tmp_path, monkeypatch)
    with session_factory() as session:
        categories.ensure_path(session, "Food > Dining")
        session.commit()

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            before = next(c.depth for c in app._categories if c.name == "Dining")
            app._run_command("category Dining")
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
        categories.ensure_path(session, "Food > Dining")
        session.commit()

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            # Food already has Dining as a child; asking to move Food under Dining
            # (still under Food) makes Food its own descendant's descendant — a cycle.
            app._run_command("category Food > Dining > Food")
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
