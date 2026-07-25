"""SQLAlchemy models.

This is the minimal slice of the schema in README.md needed to import a CSV:
``currency``, ``account``, ``category``, ``import`` and ``transaction``. The other
tables (budget, rule, tag, transaction_tag, transaction_split, recurring,
balance_snapshot, app_config, exchange_rate) are deferred until their features exist.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Currency(Base):
    __tablename__ = "currency"

    id: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[str] = mapped_column(String, unique=True)  # ISO code, e.g. "USD"
    symbol: Mapped[Optional[str]] = mapped_column(String, default=None)
    decimal_places: Mapped[int] = mapped_column(default=2)


class Account(Base):
    __tablename__ = "account"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    currency_id: Mapped[int] = mapped_column(ForeignKey("currency.id"))
    opening_balance_minor: Mapped[Optional[int]] = mapped_column(default=None)
    opening_date: Mapped[Optional[date]] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    currency: Mapped[Currency] = relationship()


class Category(Base):
    __tablename__ = "category"
    __table_args__ = (
        UniqueConstraint("parent_id", "value", name="uq_category_parent_value"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("category.id"), default=None
    )
    value: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )


class VendorName(Base):
    """Canonical / readable vendor names that overrides point to."""

    __tablename__ = "vendor_name"

    id: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[str] = mapped_column(String, unique=True)


class Vendor(Base):
    """A raw merchant string seen in imports.

    ``vendor_name_id`` is NULL by default (no override), in which case ``name`` is the
    display name. Point it at a :class:`VendorName` to rename and/or aggregate several
    raw vendors under one readable name.
    """

    __tablename__ = "vendor"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    vendor_name_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("vendor_name.id"), default=None
    )

    vendor_name: Mapped[Optional[VendorName]] = relationship()

    @property
    def display_name(self) -> str:
        return self.vendor_name.value if self.vendor_name else self.name


class Import(Base):
    __tablename__ = "import"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("account.id"), default=None
    )
    source_file: Mapped[str] = mapped_column(String)
    row_count: Mapped[int] = mapped_column(default=0)
    imported_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Transaction(Base):
    # "transactions" (plural) sidesteps the SQL reserved word "transaction".
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"))
    category_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("category.id"), default=None
    )
    currency_id: Mapped[int] = mapped_column(ForeignKey("currency.id"))
    import_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("import.id"), default=None
    )
    vendor_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("vendor.id"), default=None
    )
    posted_date: Mapped[date] = mapped_column()
    description: Mapped[str] = mapped_column(String)
    raw_description: Mapped[str] = mapped_column(String)
    value_minor: Mapped[int] = mapped_column()  # signed; negative = outflow
    transfer_group_id: Mapped[Optional[int]] = mapped_column(default=None)
    category_source: Mapped[str] = mapped_column(String, default="unset")
    import_hash: Mapped[str] = mapped_column(String, unique=True)

    account: Mapped[Account] = relationship()
    category: Mapped[Optional[Category]] = relationship()
    currency: Mapped[Currency] = relationship()
    vendor: Mapped[Optional[Vendor]] = relationship()
