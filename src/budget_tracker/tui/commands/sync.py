"""``sync`` -- pull new transactions from every connected sync provider."""

from __future__ import annotations

from typing import List

from budget_tracker import sync


class SyncCommands:
    """``sync`` and ``sync preview``."""

    SYNC_USAGE = "Usage: sync | sync preview"

    def _do_sync(self, arg: str) -> None:
        """``sync`` pulls every connected account; ``sync preview``/``sync dry`` only
        reports what it would do. Connecting a server at all needs a hidden prompt for
        the one-time setup token, so that step stays CLI-only (``budget sync
        connect``) -- this command only ever runs an already-connected server.
        """
        arg = arg.strip().lower()
        if arg in ("", "preview", "dry"):
            dry_run = arg != ""
        else:
            self.notify(self.SYNC_USAGE, severity="warning")
            return

        if any(w.group == "sync" and not w.is_finished for w in self.workers):
            self.notify("A sync is already running.", severity="warning")
            return

        with self.session_factory() as session:
            connections = sync.list_connections(session)
        if not connections:
            self.notify(
                "No sync connection yet. Run 'budget sync connect' in a terminal "
                "to add one -- it needs a hidden prompt for the one-time setup "
                "token, so that step stays CLI-only.",
                severity="warning",
            )
            return

        self.notify("Syncing…")

        def runner() -> None:
            try:
                with self.session_factory() as session:
                    results = sync.run_sync(session, dry_run=dry_run)
            except Exception as error:  # noqa: BLE001 - reported, not swallowed
                self.call_from_thread(self._on_sync_error, error)
                return
            self.call_from_thread(self._on_sync_done, results, dry_run)

        self.run_worker(runner, thread=True, group="sync", exit_on_error=False)

    def _on_sync_error(self, error: Exception) -> None:
        """Runs on the UI thread. Covers ``sync.SyncError`` and its subclasses (a
        missing credential, a revoked connection, an inverted-sign provider),
        ``simplefin.SimpleFINError`` (network/protocol trouble) and
        ``credentials.CredentialsUnavailable`` (no usable keychain backend) alike --
        all of them name something the user needs to act on, not a bug to chase.
        """
        self.notify(str(error), title="Sync failed", severity="error", markup=False)

    def _on_sync_done(self, results: List[sync.SyncResult], dry_run: bool) -> None:
        """Runs on the UI thread once a sync worker lands.

        One summary notification covers everything ``run_sync`` reported. Any account
        with a gap warning also gets its own, separate, long-lived warning
        notification -- a hole in coverage is exactly the kind of thing a summary line
        among many is easy to miss.
        """
        lines: List[str] = []
        gap_lines: List[str] = []
        import_ids: List[int] = []

        for result in results:
            lines.append(f"{result.connection_name}:")
            if result.import_id is not None:
                import_ids.append(result.import_id)
            for account in result.accounts:
                lines.append(
                    f"  {account.account_name} ({account.remote_name}): "
                    f"{account.inserted} inserted, {account.matched_existing} "
                    f"matched existing, {account.already_synced} already synced"
                )
                if account.inserted_before_cutoff:
                    # New rows dated inside what a CSV import already covered: either
                    # it missed them, or they are a near-duplicate the match did not
                    # catch. Either way, worth a look.
                    lines.append(
                        f"    {account.inserted_before_cutoff} of those are dated on or "
                        "before your last CSV import -- worth checking"
                    )
                for warning in account.warnings:
                    lines.append(f"    {warning}")
                if account.gap_warning:
                    gap_lines.append(f"{account.account_name}: {account.remote_name}")
                    gap_lines.extend(f"  {warning}" for warning in account.warnings)
            for error in result.errors:
                lines.append(f"  remote error: {error}")
            for remote_name in result.unmapped:
                lines.append(f"  not mapped to a local account: {remote_name}")

        if dry_run:
            lines.append("Preview only -- nothing was written.")

        self.notify(
            "\n".join(lines),
            title="Sync preview" if dry_run else "Sync",
            markup=False,
            timeout=10,
        )

        if gap_lines:
            self.notify(
                "\n".join(gap_lines),
                title="Sync -- possible gap in coverage",
                severity="warning",
                markup=False,
                timeout=20,
            )

        if not dry_run:
            # A real sync always needs a reload even when nothing was inserted: it still
            # wrote each account's sync_status/sync_error (see models.SyncAccount), and
            # the accounts sidebar's colors come from exactly that -- see
            # queries.get_accounts. import_ids only gates the rate fetch below, which
            # has nothing to do once there is no new import.
            self.reload()
            if import_ids:
                self._fetch_rates_after_import(import_ids)
