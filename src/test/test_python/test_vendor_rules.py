"""Tests for pattern-based vendor rename rules."""

import sqlalchemy

from budget_tracker import queries, vendors
from budget_tracker.db import get_engine, get_sessionmaker, init_db
from budget_tracker.importer import import_csv

# The literal "*" in "Kindle Svcs*<ref>" is what the bank emits, not a wildcard.
CSV = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2025-07-01,2025-07-02,8207,Kindle Svcs*BY3UO9RV2,Merchandise,9.99,
2025-07-03,2025-07-04,8207,Kindle Svcs*BS4XF2Z70,Merchandise,4.99,
2025-07-05,2025-07-06,8207,GROCERY STORE,Groceries,20.00,
"""

LATER_CSV = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2025-08-01,2025-08-02,8207,Kindle Svcs*RH3P00223,Merchandise,7.99,
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


def _vendor_counts(session):
    return {r.name: r.count for r in queries.get_vendors(session)}


def test_matches_is_case_insensitive_glob():
    assert vendors.matches("Kindle Svcs*", "Kindle Svcs*BY3UO9RV2")
    assert vendors.matches("kindle svcs*", "KINDLE SVCS*BY3UO9RV2")
    assert vendors.matches("*CAVA*", "10078 CAVA KENDALL SQR")
    assert not vendors.matches("Kindle Svcs*", "GROCERY STORE")
    # Without a wildcard the pattern is an exact match, not a substring.
    assert not vendors.matches("Kindle", "Kindle Svcs*BY3UO9RV2")
    assert vendors.matches("Kindle Svcs*BY3UO9RV2", "Kindle Svcs*BY3UO9RV2")


def test_rule_renames_and_aggregates_existing_vendors(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        assert _vendor_counts(session) == {
            "Kindle Svcs*BY3UO9RV2": 1,
            "Kindle Svcs*BS4XF2Z70": 1,
            "GROCERY STORE": 1,
        }
        vendors.add_rule(session, "Kindle Svcs*", "Kindle")
        assert vendors.apply_rules(session) == 2
        session.commit()

    with session_factory() as session:
        assert _vendor_counts(session) == {"Kindle": 2, "GROCERY STORE": 1}
        vendor_filter = queries.resolve_vendor_filter(session, "Kindle")
        assert vendor_filter[0] == "name"
        assert len(queries.get_transactions(session, vendor_filter=vendor_filter)) == 2


def test_rule_applies_to_vendors_from_later_imports(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        vendors.add_rule(session, "Kindle Svcs*", "Kindle")
        vendors.apply_rules(session)
        session.commit()

    later = tmp_path / "later.csv"
    later.write_text(LATER_CSV, encoding="utf-8")
    with session_factory() as session:
        assert import_csv(session, later).inserted == 1

    with session_factory() as session:
        # The new raw vendor was folded in by the importer, with no manual step.
        assert _vendor_counts(session) == {"Kindle": 3, "GROCERY STORE": 1}


def test_manual_rename_is_not_clobbered_by_a_rule(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        assert vendors.set_override(session, "Kindle Svcs*BY3UO9RV2", "My Kindle")
        vendors.add_rule(session, "Kindle Svcs*", "Kindle")
        vendors.apply_rules(session)
        session.commit()

    with session_factory() as session:
        # The manually renamed one keeps its name; only the other follows the rule.
        assert _vendor_counts(session) == {
            "My Kindle": 1,
            "Kindle": 1,
            "GROCERY STORE": 1,
        }


def test_retargeting_a_rule_updates_vendors_it_owns(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        vendors.add_rule(session, "Kindle Svcs*", "Kindle")
        vendors.apply_rules(session)
        session.commit()

    with session_factory() as session:
        vendors.add_rule(session, "Kindle Svcs*", "Amazon Kindle")
        assert vendors.apply_rules(session) == 2
        session.commit()

    with session_factory() as session:
        assert _vendor_counts(session) == {"Amazon Kindle": 2, "GROCERY STORE": 1}
        assert len(vendors.list_rules(session)) == 1  # re-targeted, not duplicated


def test_removing_a_rule_reverts_the_vendors_it_named(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        vendors.add_rule(session, "Kindle Svcs*", "Kindle")
        vendors.apply_rules(session)
        session.commit()

    with session_factory() as session:
        assert vendors.remove_rule(session, "Kindle Svcs*")
        assert vendors.apply_rules(session) == 2
        session.commit()

    with session_factory() as session:
        assert _vendor_counts(session) == {
            "Kindle Svcs*BY3UO9RV2": 1,
            "Kindle Svcs*BS4XF2Z70": 1,
            "GROCERY STORE": 1,
        }


def test_removing_a_rule_leaves_manual_renames_alone(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        vendors.set_override(session, "Kindle Svcs*BY3UO9RV2", "My Kindle")
        vendors.add_rule(session, "Kindle Svcs*", "Kindle")
        vendors.apply_rules(session)
        session.commit()

    with session_factory() as session:
        vendors.remove_rule(session, "Kindle Svcs*")
        vendors.apply_rules(session)
        session.commit()

    with session_factory() as session:
        counts = _vendor_counts(session)
        assert counts["My Kindle"] == 1
        assert "Kindle" not in counts


def test_apply_rules_is_idempotent(tmp_path):
    session_factory = _setup(tmp_path)
    with session_factory() as session:
        vendors.add_rule(session, "Kindle Svcs*", "Kindle")
        assert vendors.apply_rules(session) == 2
        assert vendors.apply_rules(session) == 0  # nothing left to change
        session.commit()


def test_init_db_adds_the_source_column_to_a_preexisting_vendor_table(tmp_path):
    """Databases created before rules existed must survive init_db()."""
    db_path = tmp_path / "old.db"
    engine = get_engine(db_path)
    with engine.begin() as connection:
        connection.execute(sqlalchemy.text("CREATE TABLE vendor_name (id INTEGER PRIMARY KEY, value VARCHAR)"))
        connection.execute(
            sqlalchemy.text(
                "CREATE TABLE vendor (id INTEGER PRIMARY KEY, name VARCHAR, "
                "vendor_name_id INTEGER REFERENCES vendor_name(id))"
            )
        )
        connection.execute(
            sqlalchemy.text("INSERT INTO vendor (name) VALUES ('Kindle Svcs*BY3UO9RV2')")
        )

    init_db(engine)

    with engine.begin() as connection:
        columns = {
            row[1]
            for row in connection.execute(sqlalchemy.text("PRAGMA table_info(vendor)"))
        }
    assert "vendor_name_source" in columns

    # The pre-existing row is intact and rules work against it.
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        vendors.add_rule(session, "Kindle Svcs*", "Kindle")
        assert vendors.apply_rules(session) == 1
        session.commit()
    with session_factory() as session:
        assert [v.name for v in queries.get_vendors(session)] == []  # no transactions
        assert session.scalar(
            sqlalchemy.select(sqlalchemy.func.count()).select_from(
                sqlalchemy.text("vendor")
            )
        ) == 1
