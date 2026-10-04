"""``merge`` (accounts) and ``transfers`` (automatic detection across accounts)."""

from __future__ import annotations

from budget_tracker import accounts, transfers


class TransferCommands:
    """``merge`` and ``transfers``."""

    def _do_merge(self, arg: str) -> None:
        if "=" not in arg:
            self.notify("Usage: merge <source account> = <target account>", severity="warning")
            return
        source, target = (part.strip() for part in arg.split("=", 1))
        if not source or not target:
            self.notify("Usage: merge <source account> = <target account>", severity="warning")
            return
        with self.session_factory() as session:
            try:
                result = accounts.merge_accounts(session, source, target)
            except accounts.AccountError as error:
                self.notify(str(error), severity="error", markup=False)
                return
            session.commit()
        self.reload()
        message = (
            f"Merged {result.source!r} into {result.target!r}: "
            f"{result.moved_transactions} transactions moved."
        )
        if result.unpaired_transfers:
            message += f"\n{result.unpaired_transfers} same-account transfer legs un-paired."
        self.notify(message, markup=False, timeout=8)

    def _do_transfers(self, arg: str) -> None:
        arg = arg.strip()
        with self.session_factory() as session:
            if arg in {"reset", "clear"}:
                reset = transfers.clear_transfers(session)
                session.commit()
                message = f"Un-paired {reset} transaction(s)."
            elif arg == "same-account":
                # Opt-in only: see transfers.detect_transfers for why this is not the
                # default (a false same-account pairing silently drops two real
                # transactions from the totals).
                pairs = transfers.detect_transfers(session, allow_same_account=True)
                session.commit()
                message = f"Found {pairs} new transfer pair(s) (same-account allowed)."
            elif arg:
                self.notify(
                    f"Unknown transfers option: {arg!r}. Try 'transfers', "
                    "'transfers same-account', or 'transfers reset'.",
                    severity="warning",
                    markup=False,
                )
                return
            else:
                pairs = transfers.detect_transfers(session)
                session.commit()
                message = f"Found {pairs} new transfer pair(s)."
        self.reload()
        self.notify(message)
