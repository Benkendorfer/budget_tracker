"""Tests for the TUI's rename shortcut (ctrl+n).

Driven through Textual's ``run_test`` pilot. ``asyncio.run`` wraps each case so the
suite does not need pytest-asyncio.
"""

import asyncio

from textual.widgets import DataTable, Input, ListView, Static

from budget_tracker import formats, transfers, vendors
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.importer import import_csv, read_header_and_rows
from helpers import learn_format
from budget_tracker.tui import BudgetApp

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
    assert rows == [["COFFEE*", "Coffee", "2"], ["*GROCER*", "Groceries", "0"]]
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


def test_filter_flag_controls_case_sensitivity(tmp_path, monkeypatch):
    session_factory = _setup(tmp_path, monkeypatch)
    lower = tmp_path / "lower.csv"
    lower.write_text(
        "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\n"
        "2025-07-05,2025-07-06,8207,coffee shop a,Dining,1.00,\n",
        encoding="utf-8",
    )
    with session_factory() as session:
        import_csv(session, lower)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            results = {}
            for command in ("COFFEE SHOP A", "-c COFFEE SHOP A", "-c raw:coffee shop a"):
                app._run_command(f"filter {command}")
                await pilot.pause()
                results[command] = (
                    sorted(t.description for t in app._txns),
                    app.text_filter.case_sensitive,
                    _panel_state(app)[2],
                )
            return results

    results = asyncio.run(run())
    insensitive, flag, status = results["COFFEE SHOP A"]
    assert insensitive == ["COFFEE SHOP A", "COFFEE SHOP A", "coffee shop a"]
    assert flag is False and 'all~"COFFEE SHOP A"' in status

    sensitive, flag, status = results["-c COFFEE SHOP A"]
    assert sensitive == ["COFFEE SHOP A", "COFFEE SHOP A"]
    assert flag is True
    assert 'all=="COFFEE SHOP A"' in status  # the status line shows which mode is on

    # The flag combines with a field prefix.
    scoped, flag, _ = results["-c raw:coffee shop a"]
    assert scoped == ["coffee shop a"] and flag is True
