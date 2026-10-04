"""`sel transfer` / `sel untransfer` in the app. The core is tested in
test_manual_transfers.py; this covers the command, its pop-up, and the guard that both
legs are selected."""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.importer import import_csv
from budget_tracker.models import Transaction
from budget_tracker.tui import BudgetApp

from helpers import learn_format

# Checking sends 1,000.00; Wise receives 995.00 -- a 5.00 fee detection cannot see past.
CSV = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2025-07-01,2025-07-01,1111,TO WISE,Misc,1000.00,
2025-07-02,2025-07-02,2222,FROM CHECKING,Misc,,995.00
2025-07-03,2025-07-03,1111,COFFEE,Dining,4.00,
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


def _ids_of(app, *descriptions):
    return {t.id for t in app._txns if t.description in descriptions}


def test_sel_transfer_splits_the_fee_and_says_how_much(tmp_path, monkeypatch):
    session_factory = _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._selected_ids = _ids_of(app, "TO WISE", "FROM CHECKING")
            app._run_command("sel transfer")
            await pilot.pause()
            return [(n.title, n.message) for n in app._notifications], app._totals

    notes, totals = asyncio.run(run())
    [(title, message)] = [n for n in notes if n[0] == "Manual transfer"]
    assert "-5.00" in message and "Transfer fee" in message
    assert "#manual-transfer" in message
    # The legs drop out of the totals; the fee and the coffee still count.
    assert totals.outflow_minor == -900
    with session_factory() as session:
        fee = session.scalar(select(Transaction).where(Transaction.description == "Transfer fee"))
        assert fee.value_minor == -500 and fee.transfer_group_id is None


def test_sel_transfer_needs_both_legs(tmp_path, monkeypatch):
    session_factory = _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._selected_ids = _ids_of(app, "TO WISE")
            app._run_command("sel transfer")
            await pilot.pause()
            return [n.message for n in app._notifications]

    messages = asyncio.run(run())
    assert any("needs both legs" in m for m in messages)
    with session_factory() as session:
        grouped = session.scalar(
            select(func.count(Transaction.id)).where(Transaction.transfer_group_id.is_not(None))
        )
    assert grouped == 0


def test_sel_untransfer_puts_everything_back(tmp_path, monkeypatch):
    session_factory = _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._selected_ids = _ids_of(app, "TO WISE", "FROM CHECKING")
            app._run_command("sel transfer")
            await pilot.pause()
            app._run_command("sel untransfer")
            await pilot.pause()

    asyncio.run(run())
    with session_factory() as session:
        amounts = sorted(t.value_minor for t in session.scalars(select(Transaction)))
        grouped = [t for t in session.scalars(select(Transaction)) if t.transfer_group_id]
    assert amounts == [-100000, -400, 99500]  # fee folded back, nothing extra left
    assert grouped == []
