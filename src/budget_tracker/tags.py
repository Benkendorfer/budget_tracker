"""Tags and trips.

A ``Tag`` labels a transaction; ``kind`` splits the namespace into two independent
sets that happen to share one table (see :class:`.models.Tag`):

``TAG``
    Free-form, and a transaction may carry any number at once (:func:`add_tag`,
    :func:`remove_tag`).
``TRIP``
    Names a journey. A transaction carries at most one, so travel spending can be
    summed without double-counting overlapping trips. That rule is enforced here, in
    :func:`set_trip`, by deleting any existing trip link before adding the new one --
    not by a database constraint, since SQLite cannot write a partial unique index
    whose predicate reads another table (``kind == 'trip'``).

Nothing here commits; callers own the transaction, as in :mod:`.categories`. Every
function here edits transactions per-row (a list of ids), unlike :mod:`.vendors` and
:mod:`.categories`, which mostly key off a vendor.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from .models import Tag, TransactionTag

TAG = "tag"
TRIP = "trip"
KINDS = (TAG, TRIP)


def get_or_create(session: Session, name: str, kind: str = TAG) -> Tag:
    """The tag named ``name`` of the given ``kind``, creating it if needed. No commit.

    ``(name, kind)`` is the unique key (see :class:`.models.Tag`), so a trip and a
    tag can share a name without colliding.
    """
    name = name.strip()
    tag = session.scalar(select(Tag).where(Tag.name == name, Tag.kind == kind))
    if tag is None:
        tag = Tag(name=name, kind=kind)
        session.add(tag)
        session.flush()
    return tag


def resolve(session: Session, name: str, kind: str = TAG) -> Optional[Tag]:
    return session.scalar(
        select(Tag).where(Tag.name == name.strip(), Tag.kind == kind)
    )


def list_tags(session: Session, kind: Optional[str] = None) -> List[Tag]:
    query = select(Tag).order_by(Tag.name)
    if kind is not None:
        query = query.where(Tag.kind == kind)
    return list(session.scalars(query))


def add_tag(session: Session, txn_ids: Sequence[int], name: str) -> int:
    """Give every transaction in ``txn_ids`` the tag ``name``. Returns rows added.

    Idempotent: the transactions that already carry the tag are read back first and
    left alone, so a repeat call neither double-counts nor raises on the composite
    primary key.
    """
    if not txn_ids:
        return 0
    tag = get_or_create(session, name, TAG)
    already = set(
        session.scalars(
            select(TransactionTag.transaction_id).where(
                TransactionTag.tag_id == tag.id,
                TransactionTag.transaction_id.in_(txn_ids),
            )
        )
    )
    to_add = [txn_id for txn_id in set(txn_ids) if txn_id not in already]
    if not to_add:
        return 0
    session.execute(
        insert(TransactionTag),
        [{"transaction_id": txn_id, "tag_id": tag.id} for txn_id in to_add],
    )
    session.flush()
    return len(to_add)


def remove_tag(session: Session, txn_ids: Sequence[int], name: str) -> int:
    """Take the tag ``name`` off every transaction in ``txn_ids``. Returns rows removed."""
    if not txn_ids:
        return 0
    tag = resolve(session, name, TAG)
    if tag is None:
        return 0
    result = session.execute(
        delete(TransactionTag).where(
            TransactionTag.tag_id == tag.id,
            TransactionTag.transaction_id.in_(txn_ids),
        )
    )
    session.flush()
    return result.rowcount or 0


def set_trip(session: Session, txn_ids: Sequence[int], name: str) -> int:
    """Put every transaction in ``txn_ids`` on trip ``name``, replacing any other trip.

    At most one trip per transaction is enforced here: the transaction's existing
    trip link (if any) is deleted first, then the new one is inserted. Returns rows
    written -- every id in ``txn_ids``, once each.
    """
    if not txn_ids:
        return 0
    trip = get_or_create(session, name, TRIP)
    ids = list(set(txn_ids))
    _clear_trip_links(session, ids)
    session.execute(
        insert(TransactionTag),
        [{"transaction_id": txn_id, "tag_id": trip.id} for txn_id in ids],
    )
    session.flush()
    return len(ids)


def clear_trip(session: Session, txn_ids: Sequence[int]) -> int:
    """Take every transaction in ``txn_ids`` off whatever trip it was on."""
    if not txn_ids:
        return 0
    return _clear_trip_links(session, list(set(txn_ids)))


def _clear_trip_links(session: Session, txn_ids: List[int]) -> int:
    """Delete every (transaction, trip-kind tag) link among ``txn_ids``. No commit."""
    trip_ids = select(Tag.id).where(Tag.kind == TRIP)
    result = session.execute(
        delete(TransactionTag).where(
            TransactionTag.transaction_id.in_(txn_ids),
            TransactionTag.tag_id.in_(trip_ids),
        )
    )
    session.flush()
    return result.rowcount or 0


def tags_for(session: Session, txn_ids: Sequence[int]) -> Dict[int, List[Tag]]:
    """Every tag on each of ``txn_ids``, keyed by transaction id. One query, not N."""
    result: Dict[int, List[Tag]] = {txn_id: [] for txn_id in txn_ids}
    if not txn_ids:
        return result
    rows = session.execute(
        select(TransactionTag.transaction_id, Tag)
        .join(Tag, Tag.id == TransactionTag.tag_id)
        .where(TransactionTag.transaction_id.in_(txn_ids))
    ).all()
    for txn_id, tag in rows:
        result[txn_id].append(tag)
    return result


def rename_tag(session: Session, name: str, new_name: str, kind: str = TAG) -> bool:
    """Rename a tag in place. Returns ``False`` if no tag ``name``/``kind`` exists."""
    tag = resolve(session, name, kind)
    if tag is None:
        return False
    tag.name = new_name.strip()
    session.flush()
    return True


def delete_tag(session: Session, name: str, kind: str = TAG) -> int:
    """Delete the tag and unlink it everywhere. Returns transactions it came off."""
    tag = resolve(session, name, kind)
    if tag is None:
        return 0
    result = session.execute(
        delete(TransactionTag).where(TransactionTag.tag_id == tag.id)
    )
    session.delete(tag)
    session.flush()
    return result.rowcount or 0
