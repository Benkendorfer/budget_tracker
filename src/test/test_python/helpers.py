"""Shared test helpers.

Formats live in the database, so a test that imports a CSV has to teach the database
that CSV's layout first — the same inference the interactive setup uses.
"""

from budget_tracker import formats
from budget_tracker.importer import read_header_and_rows


def learn_format(session, path, name="test_layout"):
    """Infer and save the format for ``path``, as interactive setup would."""
    fieldnames, rows = read_header_and_rows(path)
    inference = formats.infer(name, fieldnames, rows)
    assert inference.complete, f"unexpected questions: {inference.questions}"
    spec = formats.spec_from_values(inference.values)
    formats.save_format(session, spec)
    session.commit()
    return spec
