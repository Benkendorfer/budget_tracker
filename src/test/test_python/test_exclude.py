"""`sel exclude` / `sel include`: leave rows out of every income and spending figure by
hand -- for an in-kind ACATS move, which no transfer pairing can describe."""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from budget_tracker import importer, queries, transfers
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.importer import import_csv
from budget_tracker.models import Category, Transaction
from budget_tracker.tui import BudgetApp

from helpers import learn_format

CSV = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2025-07-01,2025-07-01,1111,Full Outgoing ACATS Transfer,Transfer,3000.00,
2025-07-02,2025-07-02,2222,VANGUARD FTSE DEVELOPED ETF,Transfer,,3012.50
2025-07-03,2025-07-03,1111,COFFEE,Dining,4.00,
2025-07-04,2025-07-04,1111,TO SAVINGS,Misc,50.00,
2025-07-04,2025-07-04,2222,FROM CHECKING,Misc,,50.00
"""


def _setup(tmp_path, monkeypatch=None):
    db_path = tmp_path / "t.db"
    engine = get_engine(db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    csv_path = tmp_path / "in.csv"
    csv_path.write_text(CSV, encoding="utf-8")
    with session_factory() as session:
        learn_format(session, csv_path)
        import_csv(session, csv_path)  # pairs the 50.00 transfer on its way in
    if monkeypatch is not None:
        monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def _id(session, description):
    return session.scalar(select(Transaction.id).where(Transaction.description == description))


def _acats_ids(session):
    return [
        _id(session, "Full Outgoing ACATS Transfer"),
        _id(session, "VANGUARD FTSE DEVELOPED ETF"),
    ]


def test_excluded_rows_leave_the_totals_and_keep_their_category(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        assert transfers.exclude(session, _acats_ids(session)) == 2
        session.commit()
    with session_factory() as session:
        totals = queries.get_totals(session)
        assert totals.outflow_minor == -400  # only the coffee is spending
        assert totals.inflow_minor == 0
        out = session.get(Transaction, _id(session, "Full Outgoing ACATS Transfer"))
        assert out.transfer_group_id == out.id  # a group of one, like a conversion
        assert session.get(Category, out.category_id).value == "Transfer"  # untouched
        [row] = [r for r in queries.get_transactions(session) if r.id == out.id]
        assert row.is_transfer  # greyed out like any transfer
        assert transfers.EXCLUDED_TAG in row.tags


def test_include_restores_the_rows(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        ids = _acats_ids(session)
        transfers.exclude(session, ids)
        assert transfers.include(session, ids) == 2
        session.commit()
    with session_factory() as session:
        assert all(session.get(Transaction, i).transfer_group_id is None for i in ids)
        assert queries.get_totals(session).outflow_minor == -300400
        [row] = [r for r in queries.get_transactions(session) if r.id == ids[0]]
        assert transfers.EXCLUDED_TAG not in row.tags


def test_exclude_and_include_leave_real_transfers_alone(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        paired = [_id(session, "TO SAVINGS"), _id(session, "FROM CHECKING")]
        group = session.get(Transaction, paired[0]).transfer_group_id
        assert group is not None
        assert transfers.exclude(session, paired) == 0
        assert transfers.include(session, paired) == 0
        assert {session.get(Transaction, i).transfer_group_id for i in paired} == {group}


def test_transfers_reset_and_detection_leave_exclusions_alone(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        ids = _acats_ids(session)
        transfers.exclude(session, ids)
        transfers.clear_transfers(session)
        transfers.detect_transfers(session)
        assert all(session.get(Transaction, i).transfer_group_id == i for i in ids)


def test_unimport_of_an_excluded_row_works(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        transfers.exclude(session, _acats_ids(session))
        import_id = session.get(Transaction, _acats_ids(session)[0]).import_id
        session.commit()
    with session_factory() as session:
        assert importer.delete_import(session, import_id).transactions_deleted == 5


def test_sel_exclude_and_include_in_the_app(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(213, 40)) as pilot:
            app._selected_ids = {
                t.id for t in app._txns if "ACATS" in t.description or "VANGUARD" in t.description
            }
            app._run_command("sel exclude")
            await pilot.pause()
            excluded = (app._totals.outflow_minor, app._totals.inflow_minor)
            app._run_command("sel include")
            await pilot.pause()
            return excluded, (app._totals.outflow_minor, app._totals.inflow_minor)

    excluded, included = asyncio.run(run())
    assert excluded == (-400, 0)
    assert included == (-300400, 301250)
