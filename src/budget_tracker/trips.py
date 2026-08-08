"""The travel-bucket map: which of the seven travel buckets a category counts toward.

A trip (see :mod:`.tags`, ``kind == TRIP``) is a tag naming a journey; this module is
the separate question of how its spending breaks down for display -- not into the
user's real categories, but into a fixed, coarser vocabulary a bar chart can show at a
glance. The map is seeded with a starting guess (:func:`seed_default_buckets`) and
edited from there (:func:`set_bucket`); it is data, not a hardcoded rule, so a user
whose categories don't match the seed's names can fix it in one command.

Nothing here commits; callers own the transaction, as in :mod:`.categories`.
"""

from __future__ import annotations

from typing import Dict, List, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from .categories import format_path, resolve_path
from .models import Category, TripBucket

AIRFARE, RAIL, CAR, HOTEL, FOOD, TOURISM, SHOPPING, MISC = (
    "airfare", "rail", "car", "hotel", "food", "tourism", "shopping", "misc"
)
# Roughly the order a trip is booked in, with the catch-all last. The seven real
# buckets are exactly the seven hues in tui/formatting.PIE_COLORS, and MISC takes the
# neutral OTHER_COLOR gray already reserved there for a catch-all standing in for
# several things -- so every bucket has a color and none of them has to share.
BUCKETS = (AIRFARE, RAIL, CAR, HOTEL, FOOD, TOURISM, SHOPPING, MISC)

# The seed's starting guess, matched case-insensitively against a category's own name
# (not its path) -- see seed_default_buckets. MISC gets no entries: it is what a
# category gets by falling through everything else, in resolve_buckets.
_DEFAULT_SEED: Dict[str, Sequence[str]] = {
    AIRFARE: ("Airfare", "Flights"),
    RAIL: ("Rail Travel", "Rail", "Train"),
    CAR: ("Car", "Car Rental", "Gas/Automotive", "Auto & Transport"),
    HOTEL: ("Hotel", "Lodging", "Accommodation"),
    FOOD: ("Food", "Food & Dining"),
    TOURISM: ("Tourism", "Sightseeing", "Attractions"),
    # Seeding the parent covers Clothing, Books, Electronics, Merchandise and the rest
    # of that subtree from one row. "Sporting Goods" is named separately because it
    # lives under Fitness, not Shopping. Mapping it costs nothing elsewhere: a bucket
    # only ever applies to a transaction that is on a trip, so a gym purchase made at
    # home is not reached by this and never appears in the trips panel at all.
    SHOPPING: ("Shopping", "Sporting Goods"),
}


def seed_default_buckets(session: Session) -> int:
    """Seed the bucket map with a starting guess. Returns rows written (0 if already
    seeded). No commit.

    This is a first guess, not a rule the user is stuck with -- reassign anything with
    :func:`set_bucket`. Runs only when the table is empty, checked on the table itself
    rather than on the individual names, so once the user has edited the map at all we
    never seed on top of them again. Only categories that already exist are mapped;
    none are created. ``Transport > Taxi`` and ``Transport > Public Transit`` are
    deliberately absent from the seed -- both are genuinely ambiguous, and guessing
    wrong is worse than an honest ``misc`` the user reassigns themselves.
    """
    if session.scalar(select(TripBucket.category_id).limit(1)) is not None:
        return 0
    by_lower_name = {c.value.lower(): c for c in session.scalars(select(Category))}
    written = 0
    for bucket, names in _DEFAULT_SEED.items():
        for name in names:
            category = by_lower_name.get(name.lower())
            if category is None:
                continue
            session.add(TripBucket(category_id=category.id, bucket=bucket))
            written += 1
    session.flush()
    return written


def _resolve_all(session: Session, categories: Sequence[str]) -> List[Category]:
    """Every name in ``categories`` resolved to a :class:`Category`, or raise naming
    every one that failed to resolve -- shared by :func:`set_bucket` and
    :func:`clear_bucket` so both refuse a partial edit the same way.
    """
    resolved: List[Category] = []
    missing: List[str] = []
    for name in categories:
        category = resolve_path(session, name)
        if category is None:
            missing.append(name)
        else:
            resolved.append(category)
    if missing:
        raise ValueError(f"No category found for {', '.join(repr(m) for m in missing)}.")
    return resolved


def set_bucket(session: Session, categories: Sequence[str], bucket: str) -> int:
    """Put every category in ``categories`` into ``bucket``. Returns rows written.

    Category on the left, bucket on the right, matching every other ``=`` command in
    this app (``categorize <vendor> = <category>``, ``sel category = <name>``). The map
    is keyed by category, so this is additive and per-category by construction: setting
    one category's bucket never touches another category already mapped to that bucket,
    or to any other.

    Each name is a bare category name or a full ``Food > Dining`` path, resolved with
    :func:`.categories.resolve_path` -- the same rule ``budget list --category``
    already uses. If any name fails to resolve, nothing is written at all and the
    ``ValueError`` names every one that failed: a half-applied mapping the user has to
    diff against what they typed is worse than a refusal. No commit.
    """
    if bucket not in BUCKETS:
        raise ValueError(f"Unknown bucket {bucket!r}; expected one of {list(BUCKETS)}.")
    resolved = _resolve_all(session, categories)

    for category in resolved:
        row = session.get(TripBucket, category.id)
        if row is None:
            session.add(TripBucket(category_id=category.id, bucket=bucket))
        else:
            row.bucket = bucket
    session.flush()
    return len(resolved)


def clear_bucket(session: Session, categories: Sequence[str]) -> int:
    """Unmap every category in ``categories`` (``trip bucket <name> =``). Returns rows
    removed.

    Leaves the category with no row at all, rather than writing :data:`MISC` --
    :func:`resolve_buckets` already falls through to ``misc`` for anything unmapped, and
    leaving no row means a mapped ancestor, or a later re-seed, can still reach it. Same
    all-or-nothing name resolution as :func:`set_bucket`. No commit.
    """
    resolved = _resolve_all(session, categories)
    removed = 0
    for category in resolved:
        row = session.get(TripBucket, category.id)
        if row is not None:
            session.delete(row)
            removed += 1
    session.flush()
    return removed


def bucket_map(session: Session) -> Dict[int, str]:
    """The mapping exactly as stored: ``category_id -> bucket``, no inheritance."""
    return dict(session.execute(select(TripBucket.category_id, TripBucket.bucket)).all())


def resolve_buckets(session: Session) -> Dict[int, str]:
    """Every category id's travel bucket, inherited from the nearest mapped ancestor,
    or :data:`MISC` if nothing above it is mapped either.

    Computed in one pass over the whole tree -- each category resolved once and
    memoized, so a child looks its already-resolved parent up rather than walking to
    the root itself -- rather than walking ancestors per category. This feeds a panel
    that redraws on every reload, so a correct-but-quadratic tree walk would be a real
    cost, not just an inelegance.
    """
    mapped = bucket_map(session)
    by_id = {c.id: c for c in session.scalars(select(Category))}
    resolved: Dict[int, str] = {}

    def resolve(category_id: int) -> str:
        if category_id in resolved:
            return resolved[category_id]
        if category_id in mapped:
            bucket = mapped[category_id]
        else:
            parent_id = by_id[category_id].parent_id
            bucket = resolve(parent_id) if parent_id is not None else MISC
        resolved[category_id] = bucket
        return bucket

    for category_id in by_id:
        resolve(category_id)
    return resolved


def list_buckets(session: Session) -> Dict[str, List[str]]:
    """The stored map, keyed by bucket and valued with sorted category paths -- so
    ``trip buckets`` can print it the way the user thinks about it, one heading per
    bucket with its categories underneath.
    """
    result: Dict[str, List[str]] = {bucket: [] for bucket in BUCKETS}
    for category_id, bucket in bucket_map(session).items():
        category = session.get(Category, category_id)
        if category is None:
            continue  # stale row; ondelete=CASCADE keeps this from happening in practice
        result[bucket].append(format_path(session, category))
    for paths in result.values():
        paths.sort()
    return result
