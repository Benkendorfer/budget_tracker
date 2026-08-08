"""The ``sel`` verbs: editing several transactions at once.

Selecting rows and rendering the select column live in ``test_tui_transactions.py``
and ``test_tui_app.py``; this file covers what the selection is *for* — the writes
that key off the rows the user picked rather than off a vendor, which is what makes
them different from every other write in the app.
"""

from __future__ import annotations

import asyncio

from textual.widgets import DataTable

from budget_tracker import queries, tags
from budget_tracker.db import get_engine, get_sessionmaker
from budget_tracker.tui.app import BudgetApp

from conftest import _rows_of, _setup


def _select(app, *descriptions):
    """Select the rows whose description is one of ``descriptions``."""
    app._selected_ids = {
        txn.id for txn in app._txns if txn.description in descriptions
    }
    app._render_selection()


def _sessions(tmp_path):
    return get_sessionmaker(get_engine(tmp_path / "t.db"))


def _run(tmp_path, monkeypatch, body):
    """Boot the app against the seeded database and run ``body(app)`` inside it."""
    _setup(tmp_path, monkeypatch)

    async def go():
        app = BudgetApp()
        async with app.run_test() as pilot:
            return await body(app, pilot)

    return asyncio.run(go())


# --- categories ------------------------------------------------------------------


def test_sel_category_only_touches_the_selected_rows(tmp_path, monkeypatch):
    """The point of the feature: two rows share a vendor, only one is selected."""

    async def body(app, pilot):
        _select(app, "COFFEE SHOP A")  # two rows, same vendor, both selected
        app._run_command("sel category = Coffee")
        await pilot.pause()
        return {txn.description: txn.category for txn in app._txns}

    categories = _run(tmp_path, monkeypatch, body)
    assert categories["COFFEE SHOP A"] == "Coffee"
    assert categories["COFFEE SHOP B"] == "Dining"


def test_sel_category_with_a_blank_value_clears_it(tmp_path, monkeypatch):
    async def body(app, pilot):
        _select(app, "COFFEE SHOP B")
        app._run_command("sel category = Coffee")
        await pilot.pause()
        app._run_command("sel category =")
        await pilot.pause()
        return {txn.description: txn.category for txn in app._txns}

    categories = _run(tmp_path, monkeypatch, body)
    assert categories["COFFEE SHOP B"] == ""


# --- vendors ---------------------------------------------------------------------


def test_sel_vendor_repoints_only_the_selected_transaction(tmp_path, monkeypatch):
    """A per-row vendor change, unlike ``rename``, which moves a whole vendor."""

    async def body(app, pilot):
        first = next(t for t in app._txns if t.description == "COFFEE SHOP A")
        app._selected_ids = {first.id}
        app._render_selection()
        app._run_command("sel vendor = Blue Bottle")
        await pilot.pause()
        return [(txn.description, txn.vendor) for txn in app._txns]

    rows = _run(tmp_path, monkeypatch, body)
    moved = [vendor for description, vendor in rows if vendor == "Blue Bottle"]
    assert len(moved) == 1, rows
    # The other COFFEE SHOP A row keeps its original vendor.
    assert ("COFFEE SHOP A", "COFFEE SHOP A") in rows


# --- tags and trips --------------------------------------------------------------


def test_sel_tag_then_untag(tmp_path, monkeypatch):
    async def body(app, pilot):
        _select(app, "COFFEE SHOP A")
        app._run_command("sel tag = reimbursable")
        await pilot.pause()
        tagged = {txn.description: txn.tags for txn in app._txns}
        app._run_command("sel untag = reimbursable")
        await pilot.pause()
        return tagged, {txn.description: txn.tags for txn in app._txns}

    tagged, untagged = _run(tmp_path, monkeypatch, body)
    assert tagged["COFFEE SHOP A"] == ("reimbursable",)
    assert tagged["COFFEE SHOP B"] == ()
    assert untagged["COFFEE SHOP A"] == ()


def test_sel_trip_replaces_an_earlier_trip(tmp_path, monkeypatch):
    """At most one trip per transaction — the rule the travel analysis depends on."""

    async def body(app, pilot):
        _select(app, "COFFEE SHOP A", "COFFEE SHOP B")
        app._run_command("sel trip = Japan 2026")
        await pilot.pause()
        _select(app, "COFFEE SHOP B")
        app._run_command("sel trip = Peru 2025")
        await pilot.pause()
        return {txn.description: txn.trip for txn in app._txns}

    trips = _run(tmp_path, monkeypatch, body)
    assert trips["COFFEE SHOP A"] == "Japan 2026"
    assert trips["COFFEE SHOP B"] == "Peru 2025"


def test_sel_untrip_leaves_ordinary_tags_alone(tmp_path, monkeypatch):
    async def body(app, pilot):
        _select(app, "COFFEE SHOP B")
        app._run_command("sel tag = reimbursable")
        await pilot.pause()
        app._run_command("sel trip = Japan 2026")
        await pilot.pause()
        app._run_command("sel untrip")
        await pilot.pause()
        row = next(t for t in app._txns if t.description == "COFFEE SHOP B")
        return row.trip, row.tags

    trip, tag_names = _run(tmp_path, monkeypatch, body)
    assert trip is None
    assert tag_names == ("reimbursable",)


def test_the_tags_column_shows_the_trip_first(tmp_path, monkeypatch):
    async def body(app, pilot):
        _select(app, "COFFEE SHOP B")
        app._run_command("sel tag = reimbursable")
        await pilot.pause()
        app._run_command("sel trip = Japan 2026")
        await pilot.pause()
        index = next(
            i for i, t in enumerate(app._txns) if t.description == "COFFEE SHOP B"
        )
        return _rows_of(app, "txns")[index][-1]

    cell = _run(tmp_path, monkeypatch, body)
    assert cell.startswith("✈Japan 2026")
    assert "#reimbursable" in cell


# --- clicking the Sel column -----------------------------------------------------
#
# Textual's DataTable only posts a selection message when a click lands on the row the
# cursor is already on, and dispatches _on_click to every class in the MRO. Both bite
# here, so these guard the click path specifically rather than the toggle behind it.


def test_one_click_on_the_sel_column_selects_that_row(tmp_path, monkeypatch):
    """First click, not second — a checkbox that needs two clicks is not a checkbox."""

    async def body(app, pilot):
        await pilot.click("#txns", offset=(2, 2))  # Sel column, first data row
        await pilot.pause()
        return set(app._selected_ids), app._txns[0].id

    selected, first_id = _run(tmp_path, monkeypatch, body)
    assert selected == {first_id}


def test_clicking_the_sel_column_again_deselects(tmp_path, monkeypatch):
    async def body(app, pilot):
        await pilot.click("#txns", offset=(2, 2))
        await pilot.pause()
        await pilot.click("#txns", offset=(2, 2))
        await pilot.pause()
        return set(app._selected_ids)

    assert _run(tmp_path, monkeypatch, body) == set()


def test_clicking_another_column_moves_the_cursor_without_selecting(
    tmp_path, monkeypatch
):
    """Otherwise the table could not be navigated by mouse without selecting."""

    async def body(app, pilot):
        await pilot.click("#txns", offset=(40, 3))  # Description column, second row
        await pilot.pause()
        table = app.query_one("#txns", DataTable)
        return set(app._selected_ids), table.cursor_row

    selected, cursor_row = _run(tmp_path, monkeypatch, body)
    assert selected == set()
    assert cursor_row == 1


def test_clicking_a_row_the_cursor_is_already_on_still_selects(tmp_path, monkeypatch):
    """DataTable's own click-again-to-select keeps working outside the Sel column."""

    async def body(app, pilot):
        await pilot.click("#txns", offset=(40, 3))
        await pilot.pause()
        await pilot.click("#txns", offset=(40, 3))
        await pilot.pause()
        return set(app._selected_ids), app._txns[1].id

    selected, second_id = _run(tmp_path, monkeypatch, body)
    assert selected == {second_id}


# --- the selection itself --------------------------------------------------------


def test_the_selection_survives_an_edit(tmp_path, monkeypatch):
    """So a category and then a tag can be set on the same rows without reselecting."""

    async def body(app, pilot):
        _select(app, "COFFEE SHOP A")
        app._run_command("sel category = Coffee")
        await pilot.pause()
        kept = set(app._selected_ids)
        app._run_command("sel tag = reimbursable")
        await pilot.pause()
        return len(kept), {txn.description: txn.tags for txn in app._txns}

    kept, tag_names = _run(tmp_path, monkeypatch, body)
    assert kept == 2
    assert tag_names["COFFEE SHOP A"] == ("reimbursable",)


def test_every_sel_verb_refuses_an_empty_selection(tmp_path, monkeypatch):
    async def body(app, pilot):
        messages = []
        for command in (
            "sel category = Coffee",
            "sel vendor = Blue Bottle",
            "sel tag = x",
            "sel untag = x",
            "sel trip = Japan 2026",
            "sel untrip",
        ):
            app._selected_ids = set()
            app._run_command(command)
            await pilot.pause()
            messages.append(app._notifications and list(app._notifications)[-1].message)
        return messages, [txn.category for txn in app._txns]

    messages, categories = _run(tmp_path, monkeypatch, body)
    assert messages == ["Nothing selected."] * 6
    assert categories == ["Dining"] * 3  # nothing was written


def test_a_sel_verb_without_a_value_is_a_usage_error_not_a_write(tmp_path, monkeypatch):
    """Blanking a vendor or a tag has no meaning; only `sel category =` undoes."""

    async def body(app, pilot):
        _select(app, "COFFEE SHOP A")
        app._run_command("sel tag =")
        await pilot.pause()
        return (
            list(app._notifications)[-1].message,
            {txn.description: txn.tags for txn in app._txns},
        )

    message, tag_names = _run(tmp_path, monkeypatch, body)
    assert "Usage:" in message
    assert tag_names["COFFEE SHOP A"] == ()


def test_an_unknown_sel_subcommand_warns_without_crashing(tmp_path, monkeypatch):
    async def body(app, pilot):
        app._run_command("sel colour = blue")
        await pilot.pause()
        return list(app._notifications)[-1]

    note = _run(tmp_path, monkeypatch, body)
    assert "Unknown 'sel' command" in note.message
    assert note.severity == "warning"


def test_a_bulk_edit_writes_are_visible_to_a_fresh_session(tmp_path, monkeypatch):
    """The core modules do not commit, so the app must — otherwise nothing persists."""

    async def body(app, pilot):
        _select(app, "COFFEE SHOP A")
        app._run_command("sel trip = Japan 2026")
        await pilot.pause()
        return None

    _run(tmp_path, monkeypatch, body)
    with _sessions(tmp_path)() as session:
        trip_id = queries.resolve_tag(session, "Japan 2026", tags.TRIP)
        assert trip_id is not None
        rows = queries.get_transactions(session, filters=queries.Filters(trip_id=trip_id))
    assert [row.description for row in rows] == ["COFFEE SHOP A", "COFFEE SHOP A"]
