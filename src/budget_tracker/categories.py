"""Categorising transactions.

A transaction's category is recorded together with who chose it, in
``transaction.category_source``:

``import``
    The bank's own category, written by :mod:`.importer`.
``transfer``
    :mod:`.transfers`, on both legs of a detected transfer. Rules leave these alone, so
    a paired leg always reads as a transfer; only a manual choice overrides one.
``manual``
    :func:`set_category`, i.e. the user picking a category for a vendor. Outranks
    everything else and is never overwritten.
``rule``
    :func:`apply_category_rules`, which matches every vendor against the glob patterns
    in :class:`CategoryRule`. Rules own these rows, so editing or deleting a rule
    re-categorises or clears them on the next apply.

Nothing here commits; callers own the transaction, as in :mod:`.vendors`.
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Category, CategoryRule, Transaction, Vendor, VendorName
from .transfers import TRANSFER_SOURCE
from .vendors import matches as _matches_raw

MANUAL = "manual"
RULE = "rule"
UNSET = "unset"

# Sources a rule will not overwrite. ``manual`` is the user's explicit choice; the
# ``transfer`` stamp keeps the invariant that a detected transfer reads as one. Without
# the latter, whether a rule won depended on whether the leg was paired before or after
# the rule existed, since detection only ever stamps newly paired legs.
PROTECTED_SOURCES = (MANUAL, TRANSFER_SOURCE)


def get_or_create(session: Session, value: str) -> Category:
    """Fetch the top-level category named ``value``, creating it if new. No commit."""
    value = value.strip()
    category = session.scalar(
        select(Category).where(Category.parent_id.is_(None), Category.value == value)
    )
    if category is None:
        category = Category(value=value)
        session.add(category)
        session.flush()
    return category


def _vendor_ids(session: Session, vendor: str) -> List[int]:
    """Raw vendor ids meant by ``vendor``, a raw name or a display name.

    A display name covers every raw vendor pointed at it, so categorising an override
    group categorises the whole group — the same resolution
    :func:`queries.resolve_vendor_filter` does for filtering.
    """
    vendor = vendor.strip()
    vendor_name_id = session.scalar(
        select(VendorName.id).where(VendorName.value == vendor)
    )
    if vendor_name_id is not None:
        return list(
            session.scalars(
                select(Vendor.id).where(Vendor.vendor_name_id == vendor_name_id)
            )
        )
    vendor_id = session.scalar(select(Vendor.id).where(Vendor.name == vendor))
    return [vendor_id] if vendor_id is not None else []


def _transactions_of(session: Session, vendor_ids: List[int]) -> List[Transaction]:
    if not vendor_ids:
        return []
    return list(
        session.scalars(
            select(Transaction).where(Transaction.vendor_id.in_(vendor_ids))
        )
    )


def set_category(session: Session, vendor: str, value: str) -> int:
    """Categorise every transaction of ``vendor`` by hand. Returns rows written.

    ``vendor`` is either a raw merchant string or a display name, in which case the
    whole override group is categorised. Returns 0 when no such vendor exists; the
    caller reports that.

    The rows are stamped ``manual``, which protects them from rules, later imports and
    transfer detection.
    """
    category = get_or_create(session, value)
    transactions = _transactions_of(session, _vendor_ids(session, vendor))
    for txn in transactions:
        txn.category_id = category.id
        txn.category_source = MANUAL
    session.flush()
    return len(transactions)


def clear_category(session: Session, vendor: str) -> int:
    """Undo :func:`set_category` for ``vendor``. Returns rows cleared.

    Only rows this module set by hand are cleared, so a bank-supplied or rule-owned
    category on the same vendor survives — as with :func:`transfers.clear_transfers`.
    """
    cleared = 0
    for txn in _transactions_of(session, _vendor_ids(session, vendor)):
        if txn.category_source != MANUAL:
            continue
        txn.category_id = None
        txn.category_source = UNSET
        cleared += 1
    session.flush()
    return cleared


# ------------------------------------------------------------------------- rules

def matches(pattern: str, vendor: Vendor) -> bool:
    """Case-insensitive glob match against the raw name *or* the display name.

    Matching both means a rule written against the bank's original text keeps working
    after the vendor is renamed, and one written against a readable name catches every
    raw vendor aggregated under it.
    """
    return _matches_raw(pattern, vendor.name) or _matches_raw(
        pattern, vendor.display_name
    )


def add_rule(session: Session, pattern: str, value: str) -> CategoryRule:
    """Create (or re-target) the rule for ``pattern``. Does not commit."""
    pattern = pattern.strip()
    category = get_or_create(session, value)
    rule = session.scalar(select(CategoryRule).where(CategoryRule.pattern == pattern))
    if rule is None:
        rule = CategoryRule(pattern=pattern, category_id=category.id)
        session.add(rule)
    else:
        rule.category_id = category.id
    session.flush()
    return rule


def remove_rule(session: Session, pattern: str) -> bool:
    """Delete the rule for ``pattern``. Does not commit."""
    rule = session.scalar(
        select(CategoryRule).where(CategoryRule.pattern == pattern.strip())
    )
    if rule is None:
        return False
    session.delete(rule)
    session.flush()
    return True


def list_rules(session: Session) -> List[CategoryRule]:
    return list(session.scalars(select(CategoryRule).order_by(CategoryRule.id)))


def apply_category_rules(session: Session) -> int:
    """Re-derive every rule-owned category. Returns the number of transactions changed.

    Rows categorised by hand, and the legs of a detected transfer, are skipped; anything
    else a rule matches is overwritten, including the bank's own category. A row
    previously set by a rule that no longer matches is cleared, so deleting a rule
    undoes it.
    """
    rules = list_rules(session)
    # Patterns are matched per vendor, not per transaction: vendors are far fewer, and
    # every transaction of one vendor gets the same answer anyway.
    target_by_vendor = {
        vendor.id: next(
            (r.category_id for r in rules if matches(r.pattern, vendor)), None
        )
        for vendor in session.scalars(select(Vendor))
    }

    changed = 0
    for txn in session.scalars(select(Transaction)):
        if txn.category_source in PROTECTED_SOURCES:
            continue
        target: Optional[int] = target_by_vendor.get(txn.vendor_id)
        if target is not None:
            source = RULE
        elif txn.category_source == RULE:
            source = UNSET  # its rule is gone; revert to uncategorised
        else:
            continue
        if (txn.category_id, txn.category_source) != (target, source):
            txn.category_id = target
            txn.category_source = source
            changed += 1
    session.flush()
    return changed
