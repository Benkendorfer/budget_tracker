"""Tests for tui/imports.py: the import browser (folder navigation, candidates,
past imports, unimport), the setup walkthrough, and the `format` command.
"""

from __future__ import annotations

import asyncio

from textual.widgets import DataTable, Input, Static

from budget_tracker import formats, queries
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.importer import import_csv, read_header_and_rows
from budget_tracker.models import Account, Transaction
from budget_tracker.tui import BudgetApp
from helpers import learn_format

from conftest import CSV, _panel_state, _rows_of, _setup


UNKNOWN_LAYOUT_CSV = "Booking Date,Counterparty,Turnover\n2026-07-23,SOMEONE,-200.00\n"

# Same layout as CSV, but rows that are not in the seeded database yet.


NEW_ROWS_CSV = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2025-09-01,2025-09-02,8207,NEW BAKERY,Dining,5.00,
2025-09-03,2025-09-04,8207,NEW BAKERY,Dining,6.00,
"""

# A Wise transfer log: recognised by its own signature columns (see wise.looks_like_wise),
# not a learned layout, so it never needs the setup walkthrough.


WISE_CSV = (
    "ID,Status,Direction,Created on,Finished on,Source fee amount,Source fee currency,"
    "Target fee amount,Target fee currency,Source name,Source amount (after fees),"
    "Source currency,Target name,Target amount (after fees),Target currency,"
    "Exchange rate,Reference,Batch,Created by,Category,Note\n"
    "BALANCE_TRANSACTION-1,COMPLETED,OUT,2026-03-01 09:00:00,2026-03-02 17:34:31,"
    "0.00,USD,,,,100.00,USD,,100.00,USD,1.0,,,,General,\n"
)


def _inbox(tmp_path, monkeypatch, **files):
    inbox = tmp_path / "to_import"
    inbox.mkdir()
    for name, text in files.items():
        (inbox / f"{name}.csv").write_text(text, encoding="utf-8")
    monkeypatch.setattr("budget_tracker.tui.app.TO_IMPORT_DIR", inbox)
    return inbox


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


def test_enter_on_a_wise_file_imports_it_without_the_setup_walkthrough(tmp_path, monkeypatch):
    """A built-in-reader layout (e.g. a Wise transfer log) has no saved format to look
    up, so 'ready to import' has to mean exactly that — not a crash reaching for one."""
    _setup(tmp_path, monkeypatch)
    _inbox(tmp_path, monkeypatch, wise=WISE_CSV)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            before = len(app._txns)
            app._run_command("import")
            await pilot.pause()
            rows = _rows_of(app, "imports")
            table = app.query_one("#imports", DataTable)
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            messages = [n.message for n in app._notifications]
            return before, len(app._txns), rows, messages, app._panel, app._setup

    before, after, rows, messages, panel, setup = asyncio.run(run())
    assert rows[0][:2] == ["wise.csv", "1"]
    # Ready straight away, not "needs setup" — truncated to the Status column's width.
    assert rows[0][2] == "Wise transfer …"
    assert after == before + 1  # the transfer's one leg landed
    assert any("wise.csv: 1 added, 0 skipped" in m for m in messages)
    assert panel == "imports"  # back on the browser, not stuck mid-walkthrough
    assert setup is None  # no walkthrough was ever started


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
                # A CSV never states its own currency, so the walkthrough always asks.
                "currency": "EUR",
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
        "currency",
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
            for answer in ("euro", "Turnover", "Booking Date", "Counterparty",
                           "no", "EUR", "Acct"):
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
    assert spec.currency == "EUR"  # answered in the walkthrough, not the USD default


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


def test_a_currency_mismatch_is_reported_rather_than_crashing_the_app(tmp_path, monkeypatch):
    """Two of the app's three import call sites had no handler at all, so a refusal the
    user can act on arrived as a traceback. The same shape as the Wise file that took
    the wrong branch and crashed looking up a format that was never a row.
    """
    from budget_tracker import formats
    from budget_tracker.tui.app import IMPORT_PROBLEMS

    # The tuple is the contract: a new refusal must be caught everywhere at once.
    assert formats.AccountCurrencyMismatch in IMPORT_PROBLEMS
    assert formats.AccountRequired in IMPORT_PROBLEMS
    assert formats.UnknownFormat in IMPORT_PROBLEMS

    _setup(tmp_path, monkeypatch)
    _inbox(tmp_path, monkeypatch, known=CSV)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            messages = []
            app.notify = lambda text, **kw: messages.append(text)

            def refuse(*args, **kwargs):
                raise formats.AccountCurrencyMismatch(
                    "Account 'Checking' is in USD but this file is in CHF."
                )

            monkeypatch.setattr("budget_tracker.tui.app.import_csv", refuse)
            app._run_command("import")
            await pilot.pause()
            app._run_command("import all")
            await pilot.pause()
            return messages, app._panel

    messages, panel = asyncio.run(run())
    assert any("USD" in m and "CHF" in m for m in messages), messages
    assert panel in ("imports", "txns")  # still usable, not dead
