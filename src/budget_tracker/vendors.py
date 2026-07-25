"""Vendor override management.

An override renames a raw vendor to a readable :class:`VendorName`. Pointing several
raw vendors at the same ``vendor_name`` aggregates them under one display name.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Vendor, VendorName


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
    session.commit()
    return True


def clear_override(session: Session, raw_name: str) -> bool:
    """Remove any override on the vendor whose raw name is ``raw_name``."""
    vendor = session.scalar(select(Vendor).where(Vendor.name == raw_name.strip()))
    if vendor is None:
        return False
    vendor.vendor_name_id = None
    session.commit()
    return True
