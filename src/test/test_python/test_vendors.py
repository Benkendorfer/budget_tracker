"""Integration tests for vendor creation, overrides, and aggregation."""

from budget_tracker import queries, vendors
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.importer import import_csv

CSV = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2025-07-01,2025-07-02,8207,COFFEE SHOP A,Dining,3.00,
2025-07-01,2025-07-02,8207,COFFEE SHOP B,Dining,4.00,
2025-07-03,2025-07-04,8207,COFFEE SHOP A,Dining,3.50,
"""


def _setup(tmp_path):
    engine = get_engine(tmp_path / "t.db")
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    csv_path = tmp_path / "in.csv"
    csv_path.write_text(CSV, encoding="utf-8")
    with session_factory() as session:
        import_csv(session, csv_path)
    return session_factory


def test_vendors_created_from_descriptions(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        rows = queries.get_vendors(session)
    counts = {r.name: r.count for r in rows}
    assert counts == {"COFFEE SHOP A": 2, "COFFEE SHOP B": 1}
    assert all(r.kind == "raw" for r in rows)


def test_override_aggregates_vendors(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        assert vendors.set_override(session, "COFFEE SHOP A", "Coffee")
        assert vendors.set_override(session, "COFFEE SHOP B", "Coffee")

    with session_factory() as session:
        rows = queries.get_vendors(session)
        coffee = [r for r in rows if r.name == "Coffee"]
        assert len(coffee) == 1
        assert coffee[0].kind == "name"
        assert coffee[0].count == 3  # both raw vendors aggregate

        vendor_filter = queries.resolve_vendor_filter(session, "Coffee")
        assert vendor_filter[0] == "name"
        txns = queries.get_transactions(session, vendor_filter=vendor_filter)
        assert len(txns) == 3
        assert all(t.vendor == "Coffee" for t in txns)


def test_override_of_unknown_vendor_fails(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        assert vendors.set_override(session, "DOES NOT EXIST", "X") is False
