"""Tests for tui/transactions.py: the transactions table's transfer marks, columns,
and per-currency formatting."""

from __future__ import annotations

import asyncio
import datetime
import io

from rich.console import Console
from textual.widgets import DataTable

from budget_tracker import transfers
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.importer import import_csv
from budget_tracker.models import Account, Currency, Transaction
from budget_tracker.tui import BudgetApp, _fmt_amount

from conftest import _rows_of, _setup


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


def _seed_jpy_account(tmp_path, monkeypatch):
    """A single JPY transaction. JPY has no minor unit (Currency.decimal_places=0), so
    100 minor units is 100 yen — a formatter that assumed two decimal places, the way
    _fmt_amount's default does, would render it as 1.00.
    """
    db_path = tmp_path / "jpy.db"
    engine = get_engine(db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        jpy = Currency(value="JPY", symbol="¥", decimal_places=0)
        session.add(jpy)
        session.flush()
        account = Account(name="Yen Wallet", currency_id=jpy.id)
        session.add(account)
        session.flush()
        session.add(
            Transaction(
                account_id=account.id,
                currency_id=jpy.id,
                posted_date=datetime.date(2025, 3, 1),
                description="Yen snack",
                raw_description="Yen snack",
                value_minor=100,
                import_hash="jpy-100",
            )
        )
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def test_zero_decimal_currency_shows_whole_units_not_cents(tmp_path, monkeypatch):
    _seed_jpy_account(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            return _rows_of(app, "txns")

    rows = asyncio.run(run())
    assert len(rows) == 1
    # Not "1.00" — JPY has no minor unit, so 100 minor units is 100 yen.
    assert rows[0][4] == "¥100"


def _seed_multi_currency(tmp_path, monkeypatch):
    """Three accounts, one per currency: USD (2dp, one-character symbol), JPY (0dp, no
    minor units), and CHF (2dp, three-character symbol) — so the transactions table has
    to format each row in its own currency rather than assuming two decimal places or a
    one-character symbol. The CHF amount is six figures, the shape most likely to clip
    once a symbol is added to the column (see
    test_txns_amount_column_fits_a_symbol_and_a_six_figure_amount).
    """
    db_path = tmp_path / "multi.db"
    engine = get_engine(db_path)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        usd = Currency(value="USD", symbol="$", decimal_places=2)
        jpy = Currency(value="JPY", symbol="¥", decimal_places=0)
        chf = Currency(value="CHF", symbol="CHF", decimal_places=2)
        session.add_all([usd, jpy, chf])
        session.flush()
        checking = Account(name="Checking", currency_id=usd.id)
        yen = Account(name="Yen Wallet", currency_id=jpy.id)
        swiss = Account(name="Swiss", currency_id=chf.id)
        session.add_all([checking, yen, swiss])
        session.flush()

        def txn(account, currency, day, amount, description):
            session.add(
                Transaction(
                    account_id=account.id,
                    currency_id=currency.id,
                    posted_date=day,
                    description=description,
                    raw_description=description,
                    value_minor=amount,
                    import_hash=f"multi-{description}",
                )
            )

        txn(checking, usd, datetime.date(2025, 3, 3), -4200, "US coffee")
        txn(yen, jpy, datetime.date(2025, 3, 2), -1500, "Japan lunch")
        # A six-figure, signed CHF amount: the widest realistic case for a
        # three-character symbol beside the Amount column.
        txn(swiss, chf, datetime.date(2025, 3, 1), -99_999_999, "Swiss rent")
        session.commit()
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    return session_factory


def test_txns_table_formats_each_row_in_its_own_currency(tmp_path, monkeypatch):
    _seed_multi_currency(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            return _rows_of(app, "txns")

    rows = asyncio.run(run())
    by_description = {row[1]: row[4] for row in rows}
    assert by_description["US coffee"] == "$-42.00"
    # JPY has no minor unit: 1,500 minor units is 1,500 yen, not 15.00.
    assert by_description["Japan lunch"] == "¥-1,500"
    assert by_description["Swiss rent"] == "CHF-999,999.99"


def test_txns_amount_column_fits_a_symbol_and_a_six_figure_amount(tmp_path, monkeypatch):
    """Renders the real compositor at 130 columns with a three-character symbol (CHF)
    beside a signed six-figure amount — the widest realistic value the widened Amount
    column has to hold without clipping the digits (see test_txns_table_formats_...
    above for the value itself; this checks it actually reaches the screen intact).
    """
    _seed_multi_currency(tmp_path, monkeypatch)

    async def run():
        app = BudgetApp()
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            buffer = io.StringIO()
            Console(file=buffer, width=130).print(app.screen._compositor)
            return buffer.getvalue()

    rendered = asyncio.run(run())
    assert "CHF-999,999.99" in rendered
