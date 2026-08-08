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

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from sqlalchemy import func, select
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
    """The category named ``value``, from anywhere in the tree, creating a new
    top-level one only if nothing anywhere has that name. No commit.

    Category names are unique across the whole tree now (see :class:`.models.Category`),
    so a flat string — a bank's CSV category column, or what a user types into
    ``categorize``/``category-rule`` — matches at most one category, wherever it is
    nested. This is the one lookup :func:`set_category`, :func:`add_rule`, and
    :func:`.importer._get_or_create_category` all share; before this, each did its own
    top-level-only lookup, so an import naming an already-nested category (say
    "Airfare" under "Travel") forked a rival top-level "Airfare" instead of binding to
    the existing one.

    Never *moves* an existing category — :func:`ensure_path` does that, and only with
    confirmation.
    """
    value = value.strip()
    category = session.scalar(select(Category).where(Category.value == value))
    if category is None:
        category = Category(value=value)
        session.add(category)
        session.flush()
    return category


# --------------------------------------------------------------------------- paths
#
# A name is unique across the whole tree (``UniqueConstraint("value")``), so a bare
# name matches at most one category and a full path is just a placement, not an
# identity. The lookups below reflect that: nothing here needs to hedge against several
# same-named categories any more.


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


def _by_name(session: Session, name: str) -> Optional[Category]:
    """The one category named ``name``, anywhere in the tree, or ``None``."""
    return session.scalar(select(Category).where(Category.value == name))


def resolve_path(session: Session, path: str) -> Optional[Category]:
    """Exact path lookup, e.g. ``"Food > Dining"``; a bare name is a plain lookup too.

    A bare name matches at most one category now that names are unique across the
    whole tree, so there is nothing left to disambiguate.
    """
    parts = parse_path(path)
    if len(parts) == 1:
        return _by_name(session, parts[0])
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

    Refuses a move that would make ``child`` its own ancestor. A destination-sibling
    name collision can no longer happen — names are unique across the whole tree, so
    nothing else can already be sitting under ``parent`` with ``child``'s name; see
    :func:`ensure_path`, which is what decides whether a same-named category should
    move here at all.
    """
    parent_id = parent.id if parent is not None else None
    if parent_id is not None and parent_id in descendant_ids(session, child.id):
        reason = "itself" if parent_id == child.id else f"its own descendant {parent.value!r}"
        raise CategoryError(f"Cannot move {child.value!r} under {reason}; that is a cycle.")
    child.parent_id = parent_id
    session.flush()
    return child


def _create(session: Session, value: str, parent: Optional[Category]) -> Category:
    category = Category(value=value, parent_id=parent.id if parent is not None else None)
    session.add(category)
    session.flush()
    return category


@dataclass
class Relocation:
    """An existing category :func:`ensure_path` would move to make room for a path."""

    name: str
    from_parent: Optional[str]  # current parent's path, or None for the top level
    to_parent: Optional[str]  # destination parent's path, or None for the top level
    transaction_count: int  # rolled up over the whole subtree, not just this category


@dataclass
class PathPreview:
    """What applying a path would do, per :func:`preview_path`. Nothing is written."""

    creates: List[str] = field(default_factory=list)  # levels with no existing category
    relocations: List[Relocation] = field(default_factory=list)  # existing ones that move


# A sentinel expected-parent-id that no real category id (nor None, the top level) can
# ever equal — used by preview_path while walking through a level that does not exist
# yet, so a same-named category found further down is still correctly reported as
# relocating into a not-yet-created parent, rather than looking like it is already
# in place.
_NOT_YET_BUILT = object()


def preview_path(session: Session, path: str) -> PathPreview:
    """What :func:`ensure_path` would do to ``path``, without writing anything.

    Walks the path level by level: a name with nothing anywhere in the tree is a
    level that would be created; a name that exists but under a different parent than
    this path puts it is a relocation, reported with its current and destination
    parent and the transaction count rolled up over its *whole* subtree — a category
    with children must not be reported as moving fewer rows than it actually would.
    """
    parts = parse_path(path)
    creates: List[str] = []
    relocations: List[Relocation] = []
    expected_parent_id: object = None  # None means "the top level"
    parent_path = ""
    for part in parts:
        found = _by_name(session, part)
        if found is None:
            creates.append(part)
            expected_parent_id = _NOT_YET_BUILT
        else:
            if found.parent_id != expected_parent_id:
                current_parent = (
                    session.get(Category, found.parent_id)
                    if found.parent_id is not None
                    else None
                )
                relocations.append(
                    Relocation(
                        name=found.value,
                        from_parent=(
                            format_path(session, current_parent)
                            if current_parent is not None
                            else None
                        ),
                        to_parent=parent_path or None,
                        transaction_count=_subtree_txn_count(session, found.id),
                    )
                )
            expected_parent_id = found.id
        parent_path = f"{parent_path} {PATH_SEPARATOR} {part}" if parent_path else part
    return PathPreview(creates=creates, relocations=relocations)


def _subtree_txn_count(session: Session, category_id: int) -> int:
    ids = descendant_ids(session, category_id)
    return session.scalar(
        select(func.count(Transaction.id)).where(Transaction.category_id.in_(ids))
    ) or 0


def ensure_path(
    session: Session, path: str, confirm_relocation: bool = False
) -> Category:
    """Create or complete ``path``, moving an existing category into place if needed.

    Each level is created if nothing anywhere has that name yet, or reused as-is if it
    is already exactly where this path puts it. A level that already exists somewhere
    *else* in the tree is a relocation of that whole category — including everything
    nested under it — and is refused unless ``confirm_relocation`` is set, raising
    :class:`CategoryError` naming what it would have moved. Call :func:`preview_path`
    first to show the user what that is before setting it; a caller that has not been
    updated to confirm therefore cannot silently relocate anything, top-level
    promotion (a one-element path) included. Does not commit.
    """
    preview = preview_path(session, path)
    if preview.relocations and not confirm_relocation:
        moved = "; ".join(
            f"{r.name!r} from {r.from_parent or 'the top level'} to "
            f"{r.to_parent or 'the top level'} ({r.transaction_count} transactions)"
            for r in preview.relocations
        )
        raise CategoryError(
            f"{path!r} would relocate {moved}. Pass confirm_relocation=True to do it."
        )

    parts = parse_path(path)
    parent: Optional[Category] = None
    for part in parts:
        parent_id = parent.id if parent is not None else None
        found = _by_name(session, part)
        if found is None:
            found = _create(session, part, parent)
        elif found.parent_id != parent_id:
            found = set_parent(session, found, parent)
        parent = found
    return parent


# ------------------------------------------------------------------------- merging

@dataclass
class MergeResult:
    source: str
    target: str
    moved_transactions: int
    moved_rules: int
    moved_children: int


def _find_for_merge(session: Session, text_: str) -> Category:
    """Locate exactly one category by name or path, for :func:`merge_category`.

    Unlike every other lookup in this module, this one has to work on a database that
    does *not* yet have unique names — merging duplicates is how such a database gets
    there in the first place (see :func:`.db.init_db`'s migration). A full (multi-segment)
    path is always safe, since ``(parent_id, value)`` was already unique before this
    change. A bare name is safe too once names are unique, which is the common case;
    while they are not, it is only unambiguous when at most one *top-level* category
    carries it — a top-level category's own path is just its bare value, so that is the
    one thing left for the bare form to mean once the name also exists, nested, under
    some other parent. Anything left ambiguous after that refuses to guess.
    """
    parts = parse_path(text_)
    if len(parts) > 1:
        parent_id: Optional[int] = None
        category: Optional[Category] = None
        for part in parts:
            category = _sibling(session, parent_id, part)
            if category is None:
                raise CategoryError(f"No category at path {text_!r}.")
            parent_id = category.id
        return category

    name = parts[0]
    matches = list(session.scalars(select(Category).where(Category.value == name)))
    if not matches:
        raise CategoryError(f"No category named {text_!r}.")
    if len(matches) == 1:
        return matches[0]
    top_level = [m for m in matches if m.parent_id is None]
    if len(top_level) == 1:
        return top_level[0]
    raise CategoryError(
        f"{text_!r} matches {len(matches)} categories; give a full path "
        f"(e.g. 'Parent > {name}') to say which one."
    )


def merge_category(session: Session, source: str, target: str) -> MergeResult:
    """Fold ``source`` into ``target``: repoint its transactions, its category rules,
    and any children onto ``target``, then delete it. No commit.

    ``source``/``target`` are a bare name or a full path, resolved by
    :func:`_find_for_merge` rather than :func:`get_or_create` since the whole point of
    this function is cleaning up a database where names are not unique yet.

    Refuses a merge that would make ``target`` its own ancestor — merging a category
    into one of its own descendants, which would leave nothing above the moved
    subtree to be the descendant *of*.
    """
    src = _find_for_merge(session, source)
    tgt = _find_for_merge(session, target)
    if src.id == tgt.id:
        raise CategoryError(f"Cannot merge {src.value!r} into itself.")
    if tgt.id in descendant_ids(session, src.id):
        raise CategoryError(
            f"Cannot merge {src.value!r} into its own descendant {tgt.value!r}; "
            "that would make it its own ancestor."
        )

    moved_transactions = 0
    for txn in session.scalars(select(Transaction).where(Transaction.category_id == src.id)):
        txn.category_id = tgt.id
        moved_transactions += 1

    moved_rules = 0
    for rule in session.scalars(select(CategoryRule).where(CategoryRule.category_id == src.id)):
        rule.category_id = tgt.id
        moved_rules += 1

    moved_children = 0
    for child in children(session, src.id):
        child.parent_id = tgt.id
        moved_children += 1

    session.delete(src)
    session.flush()
    return MergeResult(
        source=src.value,
        target=tgt.value,
        moved_transactions=moved_transactions,
        moved_rules=moved_rules,
        moved_children=moved_children,
    )


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


def set_category_for(session: Session, txn_ids: Sequence[int], value: str) -> int:
    """Categorise the given transactions by hand. Returns rows written.

    Per-row form of :func:`set_category`, for the multi-select UI, which picks
    transactions rather than a vendor. An empty ``txn_ids`` is a no-op returning 0 --
    never a query with ``IN ()``.
    """
    if not txn_ids:
        return 0
    category = get_or_create(session, value)
    transactions = list(
        session.scalars(select(Transaction).where(Transaction.id.in_(txn_ids)))
    )
    for txn in transactions:
        txn.category_id = category.id
        txn.category_source = MANUAL
    session.flush()
    return len(transactions)


def clear_category_for(session: Session, txn_ids: Sequence[int]) -> int:
    """Undo :func:`set_category_for` on the given transactions. Returns rows cleared.

    Only rows this module set by hand are cleared, the same restriction
    :func:`clear_category` makes.
    """
    if not txn_ids:
        return 0
    cleared = 0
    for txn in session.scalars(select(Transaction).where(Transaction.id.in_(txn_ids))):
        if txn.category_source != MANUAL:
            continue
        txn.category_id = None
        txn.category_source = UNSET
        cleared += 1
    session.flush()
    return cleared


def set_category(session: Session, vendor: str, value: str) -> int:
    """Categorise every transaction of ``vendor`` by hand. Returns rows written.

    ``vendor`` is either a raw merchant string or a display name, in which case the
    whole override group is categorised. Returns 0 when no such vendor exists; the
    caller reports that.

    The rows are stamped ``manual``, which protects them from rules, later imports and
    transfer detection.
    """
    txn_ids = [t.id for t in _transactions_of(session, _vendor_ids(session, vendor))]
    return set_category_for(session, txn_ids, value)


def clear_category(session: Session, vendor: str) -> int:
    """Undo :func:`set_category` for ``vendor``. Returns rows cleared.

    Only rows this module set by hand are cleared, so a bank-supplied or rule-owned
    category on the same vendor survives — as with :func:`transfers.clear_transfers`.
    """
    txn_ids = [t.id for t in _transactions_of(session, _vendor_ids(session, vendor))]
    return clear_category_for(session, txn_ids)


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
