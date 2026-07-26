"""Account housekeeping: renaming, and merging two accounts into one.

Accounts are created by name during import, so one real-world account can end up split
across two rows — most often after changing the prefix a layout derives names with. This
module puts the pieces back together.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Account, Import, Transaction
from .transfers import TRANSFER_SOURCE


class AccountError(ValueError):
    """The requested accounts cannot be renamed or merged as asked."""


@dataclass
class MergeResult:
    source: str
    target: str
    moved_transactions: int
    moved_imports: int
    unpaired_transfers: int


def _require(session: Session, name: str) -> Account:
    account = session.scalar(select(Account).where(Account.name == name.strip()))
    if account is None:
        known = ", ".join(
            a.name for a in session.scalars(select(Account).order_by(Account.name))
        )
        raise AccountError(f"No account named {name!r}. Known accounts: {known}.")
    return account


def rename_account(session: Session, old_name: str, new_name: str) -> None:
    """Rename an account. Does not commit."""
    account = _require(session, old_name)
    new_name = new_name.strip()
    if not new_name:
        raise AccountError("The new account name cannot be empty.")
    clash = session.scalar(select(Account).where(Account.name == new_name))
    if clash is not None and clash.id != account.id:
        raise AccountError(
            f"An account named {new_name!r} already exists; merge them instead."
        )
    account.name = new_name
    session.flush()


def _unpair_same_account_transfers(session: Session) -> int:
    """Drop transfer pairs whose legs now sit in the same account.

    Merging can leave a pair that used to span two accounts entirely inside one, which
    is no longer a transfer by any definition.
    """
    paired = list(
        session.scalars(
            select(Transaction).where(Transaction.transfer_group_id.is_not(None))
        )
    )
    groups = defaultdict(list)
    for txn in paired:
        groups[txn.transfer_group_id].append(txn)

    unpaired = 0
    for legs in groups.values():
        if len({leg.account_id for leg in legs}) > 1:
            continue
        for leg in legs:
            leg.transfer_group_id = None
            if leg.category_source == TRANSFER_SOURCE:
                leg.category_id = None
                leg.category_source = "unset"
            unpaired += 1
    session.flush()
    return unpaired


def merge_accounts(session: Session, source_name: str, target_name: str) -> MergeResult:
    """Move everything from ``source_name`` into ``target_name``, then delete the source.

    Does not commit. Refuses to merge accounts held in different currencies, since their
    amounts are not comparable.
    """
    source = _require(session, source_name)
    target = _require(session, target_name)
    if source.id == target.id:
        raise AccountError("An account cannot be merged into itself.")
    if source.currency_id != target.currency_id:
        raise AccountError(
            f"{source.name!r} and {target.name!r} are held in different currencies; "
            "merging them would mix units."
        )

    moved_transactions = 0
    for txn in session.scalars(
        select(Transaction).where(Transaction.account_id == source.id)
    ):
        txn.account_id = target.id
        moved_transactions += 1

    moved_imports = 0
    for record in session.scalars(select(Import).where(Import.account_id == source.id)):
        record.account_id = target.id
        moved_imports += 1

    session.flush()
    unpaired = _unpair_same_account_transfers(session)

    session.delete(source)
    session.flush()
    return MergeResult(
        source=source_name.strip(),
        target=target.name,
        moved_transactions=moved_transactions,
        moved_imports=moved_imports,
        unpaired_transfers=unpaired,
    )
