"""Vendor override management.

An override renames a raw vendor to a readable :class:`VendorName`. Pointing several
raw vendors at the same ``vendor_name`` aggregates them under one display name.

Overrides are set two ways, recorded in ``vendor.vendor_name_source``:

``manual``
    :func:`set_override`, i.e. the ``rename`` command. Never touched by rules.
``rule``
    :func:`apply_rules`, which matches every raw vendor against the glob patterns in
    :class:`VendorRule`. Rules own these rows, so editing or deleting a rule re-points
    or clears them on the next apply.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Transaction, Vendor, VendorName, VendorRule

MANUAL = "manual"
RULE = "rule"


def _get_or_create_vendor_name(session: Session, value: str) -> VendorName:
    value = value.strip()
    vendor_name = session.scalar(select(VendorName).where(VendorName.value == value))
    if vendor_name is None:
        vendor_name = VendorName(value=value)
        session.add(vendor_name)
        session.flush()
    return vendor_name


def set_override(session: Session, raw_name: str, display_name: str) -> bool:
    """Point the vendor whose raw name is ``raw_name`` at ``display_name``.

    Returns ``True`` on success, ``False`` if no vendor matches ``raw_name``.
    """
    vendor = session.scalar(select(Vendor).where(Vendor.name == raw_name.strip()))
    if vendor is None:
        return False
    vendor.vendor_name_id = _get_or_create_vendor_name(session, display_name).id
    vendor.vendor_name_source = MANUAL
    session.commit()
    return True


def clear_override(session: Session, raw_name: str) -> bool:
    """Remove any override on the vendor whose raw name is ``raw_name``."""
    vendor = session.scalar(select(Vendor).where(Vendor.name == raw_name.strip()))
    if vendor is None:
        return False
    vendor.vendor_name_id = None
    vendor.vendor_name_source = None
    session.commit()
    return True


def set_vendor(session: Session, txn_ids: Sequence[int], name: str) -> int:
    """Point the given transactions at the vendor ``name``. Returns rows written.

    Unlike :func:`set_override`, this is a genuinely per-transaction change, for the
    multi-select UI: it get-or-creates a raw :class:`Vendor` named ``name`` and repoints
    each of ``txn_ids`` at it, leaving every other transaction (including others already
    on that vendor) untouched.

    A freshly created ``Vendor`` is pointed at the existing :class:`VendorName` ``name``
    if there is one, so the rows aggregate under that display group rather than starting
    a rival one. An already-existing raw vendor keeps whatever override it already had
    -- set_vendor never touches vendor_name_id on a vendor that already exists, so a
    prior manual rename or rule survives.

    No commit, unlike :func:`set_override`/:func:`clear_override` above -- this follows
    :mod:`.categories`'s convention instead, since the caller (multi-select `sel`
    commands) batches several of these together.
    """
    if not txn_ids:
        return 0
    name = name.strip()
    vendor = session.scalar(select(Vendor).where(Vendor.name == name))
    if vendor is None:
        vendor = Vendor(name=name)
        session.add(vendor)
        session.flush()
        vendor_name = session.scalar(select(VendorName).where(VendorName.value == name))
        if vendor_name is not None:
            vendor.vendor_name_id = vendor_name.id
    written = 0
    for txn in session.scalars(select(Transaction).where(Transaction.id.in_(txn_ids))):
        txn.vendor_id = vendor.id
        written += 1
    session.flush()
    return written


# ------------------------------------------------------------------------- rules

def matches(pattern: str, raw_name: str) -> bool:
    """Case-insensitive shell-style glob match (``*`` and ``?``).

    Note that ``*`` is a wildcard here even though banks often emit it literally, e.g.
    ``Kindle Svcs*BY3UO9RV2`` — which is exactly why ``Kindle Svcs*`` is the natural
    pattern for it.
    """
    return fnmatchcase(raw_name.strip().lower(), pattern.strip().lower())


def add_rule(session: Session, pattern: str, display_name: str) -> VendorRule:
    """Create (or re-target) the rule for ``pattern``. Does not commit."""
    pattern = pattern.strip()
    vendor_name = _get_or_create_vendor_name(session, display_name)
    rule = session.scalar(select(VendorRule).where(VendorRule.pattern == pattern))
    if rule is None:
        rule = VendorRule(pattern=pattern, vendor_name_id=vendor_name.id)
        session.add(rule)
    else:
        rule.vendor_name_id = vendor_name.id
    session.flush()
    return rule


def remove_rule(session: Session, pattern: str) -> bool:
    """Delete the rule for ``pattern``. Does not commit."""
    rule = session.scalar(
        select(VendorRule).where(VendorRule.pattern == pattern.strip())
    )
    if rule is None:
        return False
    session.delete(rule)
    session.flush()
    return True


def list_rules(session: Session) -> List[VendorRule]:
    return list(session.scalars(select(VendorRule).order_by(VendorRule.id)))


def apply_rules(session: Session) -> int:
    """Re-derive every rule-owned override. Returns the number of vendors changed.

    Vendors renamed manually are skipped. A vendor previously set by a rule that no
    longer matches anything is cleared, so deleting a rule undoes it.
    """
    rules = list_rules(session)
    changed = 0
    for vendor in session.scalars(select(Vendor)):
        if vendor.vendor_name_source == MANUAL:
            continue
        match = next((r for r in rules if matches(r.pattern, vendor.name)), None)
        if match is not None:
            target: Optional[int] = match.vendor_name_id
            source: Optional[str] = RULE
        elif vendor.vendor_name_source == RULE:
            target, source = None, None  # its rule is gone; revert to the raw name
        else:
            continue
        if (vendor.vendor_name_id, vendor.vendor_name_source) != (target, source):
            vendor.vendor_name_id = target
            vendor.vendor_name_source = source
            changed += 1
    session.flush()
    return changed
