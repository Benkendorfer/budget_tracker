"""``rates`` and ``rates fetch`` -- cached exchange rates, and topping them up.

Shares its background-worker plumbing (``_run_rate_fetch``) with the post-import
auto-fetch, which other families (currently only ``ImportCommands`` and
``SyncCommands``) call through ``self._fetch_rates_after_import``.
"""

from __future__ import annotations

from typing import Callable, List, Optional

from sqlalchemy.orm import Session

from budget_tracker import queries, rates


class RatesCommands:
    """``rates``, including the background fetch shared with post-import/post-sync."""

    RATES_USAGE = "Usage: rates | rates fetch"

    def _run_rate_fetch(self, job: Callable[[Session], Optional[str]]) -> None:
        """Run ``job(session)`` off the event loop and report whatever it returns.

        ``fetch_ecb_rates`` alone can take up to 40 seconds per attempt, twice over —
        see its own docstring — so nothing that might call it runs on the UI thread.
        This is the one place that talks to a worker thread for it, shared by the
        post-import auto-fetch (``_fetch_rates_after_import``) and the ``rates fetch``
        command (``_do_rates_fetch``), so neither has to repeat the plumbing.

        ``job`` gets its own session (worker threads do not share one with the main
        thread) and returns the message to show, or ``None`` to say nothing — an import
        that turned out to need no foreign currency at all has nothing worth a
        notification. Never raises past this point: a database or network hiccup here
        must not take the whole app down, only report as "did not work".
        """

        def runner() -> None:
            try:
                with self.session_factory() as session:
                    message = job(session)
                    session.commit()
            except Exception as error:  # noqa: BLE001 - reported, not swallowed
                message = f"Rate fetch failed: {error}"
            if message:
                self.call_from_thread(self._on_rate_fetch_done, message)

        self.run_worker(runner, thread=True, group="rates", exit_on_error=False)

    def _on_rate_fetch_done(self, message: str) -> None:
        """Runs on the UI thread (via call_from_thread) once a rate fetch worker lands."""
        self.notify(message, markup=False)
        self.reload()

    def _fetch_rates_after_import(self, import_ids: List[int]) -> None:
        """Cache whatever ECB rates the import(s) that just finished need, if any.

        The decision -- which currencies, what span, whether anything is even missing
        -- is entirely rates.fetch_rates_for_import's; this only loops over the ids and
        turns its answer into a line of text. Offline or unreachable is reported, never
        raised: the import this follows has already committed and succeeded.
        """

        def job(session: Session) -> Optional[str]:
            messages = []
            for import_id in import_ids:
                outcome = rates.fetch_rates_for_import(session, import_id, queries.HOME_CURRENCY)
                if not outcome.attempted:
                    continue  # nothing but home_currency in this import
                quotes = ", ".join(outcome.quotes)
                if outcome.error is not None:
                    messages.append(
                        f"Could not fetch {queries.HOME_CURRENCY} -> {quotes} rates: "
                        f"{outcome.error} Run 'rates fetch' later."
                    )
                else:
                    messages.append(
                        f"Fetched {outcome.written} rate(s) for "
                        f"{queries.HOME_CURRENCY} -> {quotes}."
                    )
            return "\n".join(messages) if messages else None

        self._run_rate_fetch(job)

    def _do_rates(self, arg: str) -> None:
        """Bare ``rates`` lists what is cached; ``rates fetch`` caches what is missing."""
        arg = arg.strip().lower()
        if arg in ("", "list"):
            self._notify_rates()
            return
        if arg == "fetch":
            self._do_rates_fetch()
            return
        self.notify(self.RATES_USAGE, severity="warning")

    def _notify_rates(self) -> None:
        with self.session_factory() as session:
            rows = queries.get_exchange_rates(session)
        if not rows:
            self.notify("No exchange rates cached yet. Run: rates fetch")
            return
        lines = []
        for row in rows:
            span = (
                row.first_day if row.first_day == row.last_day
                else f"{row.first_day}..{row.last_day}"
            )
            plural = "" if row.count == 1 else "s"
            lines.append(
                f"{row.base} -> {row.quote}   {row.source:<8} {span:<23} "
                f"{row.count} rate{plural}"
            )
        self.notify("\n".join(lines), title="Exchange rates", markup=False, timeout=8)

    def _do_rates_fetch(self) -> None:
        """Fetch ECB rates for every foreign currency on file, over its whole range —
        the same derivation ``budget rates fetch`` uses (queries.default_rate_fetch_span),
        so the two never drift.
        """

        def job(session: Session) -> str:
            derived = queries.default_rate_fetch_span(session, queries.HOME_CURRENCY)
            if derived is None:
                return (
                    "No transactions in the database to derive a date range from."
                )
            start, end, quotes = derived
            if not quotes:
                return f"Only one currency on file ({queries.HOME_CURRENCY}); nothing to fetch."
            try:
                written = rates.fetch_ecb_rates(
                    session, start, end, queries.HOME_CURRENCY, quotes
                )
            except rates.FrankfurterError as error:
                return f"Could not fetch ECB rates: {error}"
            return (
                f"Fetched {written} rate(s) for "
                f"{queries.HOME_CURRENCY} -> {', '.join(quotes)}."
            )

        self.notify("Fetching exchange rates…")
        self._run_rate_fetch(job)
