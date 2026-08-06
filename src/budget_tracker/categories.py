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

# Separator for a printable category path, e.g. "Food > Dining".
PATH_SEPARATOR = ">"


class CategoryError(ValueError):
    """A category path is malformed, or a move would create a cycle or a collision."""


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


# --------------------------------------------------------------------------- paths
#
# Names are unique only among siblings (``UniqueConstraint("parent_id", "value")``), so
# "Food > Other" and "Travel > Other" can coexist. Every lookup below is therefore
# path-aware; nothing here does a bare-name match that could silently pick one of several.


def parse_path(text: str) -> List[str]:
    """Split ``"Food > Dining"`` into ``["Food", "Dining"]``.

    Each segment is stripped; an empty segment (leading/trailing/doubled separator) is
    rejected rather than silently dropped, since a typo there would otherwise create a
    category no one meant.
    """
    parts = [part.strip() for part in text.split(PATH_SEPARATOR)]
    if not parts or any(not part for part in parts):
        raise CategoryError(
            f"Bad category path {text!r}; expected e.g. 'Food > Dining'."
        )
    return parts


def format_path(session: Session, category: Category) -> str:
    """``category``'s full path, root first, e.g. ``"Food > Dining"``."""
    parts = [category.value]
    node = category
    while node.parent_id is not None:
        node = session.get(Category, node.parent_id)
        parts.append(node.value)
    parts.reverse()
    return f" {PATH_SEPARATOR} ".join(parts)


def _sibling(session: Session, parent_id: Optional[int], value: str) -> Optional[Category]:
    clause = (
        Category.parent_id.is_(None) if parent_id is None else Category.parent_id == parent_id
    )
    return session.scalar(select(Category).where(clause, Category.value == value))


def resolve_path(session: Session, path: str) -> Optional[Category]:
    """Exact path lookup, e.g. ``"Food > Dining"``.

    A bare name (no separator) is also accepted, but only when it is unambiguous — when
    exactly one category anywhere in the tree carries it. That keeps existing callers and
    typed commands working now that names repeat across branches; an ambiguous bare name
    returns ``None`` rather than guessing which one was meant.
    """
    parts = parse_path(path)
    if len(parts) == 1:
        matches = list(session.scalars(select(Category).where(Category.value == parts[0])))
        return matches[0] if len(matches) == 1 else None
    parent_id: Optional[int] = None
    category: Optional[Category] = None
    for part in parts:
        category = _sibling(session, parent_id, part)
        if category is None:
            return None
        parent_id = category.id
    return category


def children(session: Session, category_id: int) -> List[Category]:
    """Direct children of ``category_id``, alphabetically."""
    return list(
        session.scalars(
            select(Category).where(Category.parent_id == category_id).order_by(Category.value)
        )
    )


def descendant_ids(session: Session, category_id: int) -> List[int]:
    """``category_id`` and every id below it, in no particular order."""
    ids = [category_id]
    frontier = [category_id]
    while frontier:
        found = list(session.scalars(select(Category.id).where(Category.parent_id.in_(frontier))))
        ids.extend(found)
        frontier = found
    return ids


def set_parent(session: Session, child: Category, parent: Optional[Category]) -> Category:
    """Move ``child`` under ``parent`` (``None`` for the top level). No commit.

    Refuses a move that would make ``child`` its own ancestor, and a move that would
    collide with an existing sibling of the same name under the destination — the unique
    constraint would otherwise reject it at flush with a far less helpful error.
    """
    parent_id = parent.id if parent is not None else None
    if parent_id is not None and parent_id in descendant_ids(session, child.id):
        reason = "itself" if parent_id == child.id else f"its own descendant {parent.value!r}"
        raise CategoryError(f"Cannot move {child.value!r} under {reason}; that is a cycle.")
    collision = _sibling(session, parent_id, child.value)
    if collision is not None and collision.id != child.id:
        location = format_path(session, parent) if parent is not None else "the top level"
        raise CategoryError(
            f"{child.value!r} already exists under {location}; cannot move it there too."
        )
    child.parent_id = parent_id
    session.flush()
    return child


def _create(session: Session, value: str, parent: Optional[Category]) -> Category:
    category = Category(value=value, parent_id=parent.id if parent is not None else None)
    session.add(category)
    session.flush()
    return category


def _step(session: Session, parent: Optional[Category], name: str) -> Category:
    """One element of a path: reuse ``name`` under ``parent`` if it is already there.

    Otherwise, rescue a *stray top-level* category of the same name into this slot rather
    than forking a same-named duplicate — someone building "Food > Dining" after already
    having a bare "Dining" almost always means the same category. A name nested somewhere
    else in the tree is left alone: only an explicit bare-name lookup (:func:`resolve_path`,
    or the one-element case below) reaches across branches like that, since silently
    abducting an unrelated same-named category out of another branch while building an
    unrelated path would be a surprising, destructive guess.
    """
    parent_id = parent.id if parent is not None else None
    existing = _sibling(session, parent_id, name)
    if existing is not None:
        return existing
    if parent_id is not None:
        stray = _sibling(session, None, name)
        if stray is not None:
            return set_parent(session, stray, parent)
    return _create(session, name, parent)


def ensure_path(session: Session, path: str) -> Category:
    """Create or complete ``path``, re-parenting its last element under the one before it.

    Each element is created if missing, reusing an already-correctly-placed category, or
    (for a non-top-level slot) rescuing a stray top-level category of the same name into
    place — see :func:`_step`. A one-element path is a special case: rather than restrict
    the search to stray top-level categories, it moves an existing category to the top
    level from *anywhere* in the tree, as long as the bare name is unambiguous; ambiguous
    or absent, it creates one fresh at the top level. Does not commit.
    """
    parts = parse_path(path)
    if len(parts) == 1:
        name = parts[0]
        existing = _sibling(session, None, name)
        if existing is not None:
            return existing
        elsewhere = resolve_path(session, name)
        return set_parent(session, elsewhere, None) if elsewhere is not None else _create(
            session, name, None
        )

    node: Optional[Category] = None
    for part in parts:
        node = _step(session, node, part)
    return node


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
