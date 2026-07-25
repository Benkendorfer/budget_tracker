"""Integration tests for vendor creation, overrides, and aggregation."""

from budget_tracker import queries, vendors
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.importer import import_csv
from helpers import learn_format

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
        learn_format(session, csv_path)
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


LATER_CSV = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2025-08-01,2025-08-02,8207,COFFEE SHOP A,Dining,5.00,
2025-08-05,2025-08-06,8207,COFFEE SHOP B,Dining,6.00,
"""


def test_override_survives_reimport(tmp_path):
    """Re-importing must not clobber a rename, and new rows must join the group."""
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        vendors.set_override(session, "COFFEE SHOP A", "Coffee")
        vendors.set_override(session, "COFFEE SHOP B", "Coffee")

    # Re-import the original file (all duplicates) plus a file with new rows.
    later = tmp_path / "later.csv"
    later.write_text(LATER_CSV, encoding="utf-8")
    with session_factory() as session:
        again = import_csv(session, tmp_path / "in.csv")
        assert (again.inserted, again.skipped_duplicates) == (0, 3)
        assert import_csv(session, later).inserted == 2

    with session_factory() as session:
        rows = queries.get_vendors(session)
        assert [(r.kind, r.name, r.count) for r in rows] == [("name", "Coffee", 5)]

        vendor_filter = queries.resolve_vendor_filter(session, "Coffee")
        txns = queries.get_transactions(session, vendor_filter=vendor_filter)
        assert len(txns) == 5
        assert all(t.vendor == "Coffee" for t in txns)
