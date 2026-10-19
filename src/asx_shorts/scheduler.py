"""In-process daily ASIC update.

On Fly a volume attaches to exactly one machine, so a separate scheduled
machine can't reach the database. Instead the API process runs the catch-up
itself: every weekday at ASX_SHORTS_INGEST_HOUR (default 20:00) Sydney time,
plus once shortly after start-up so a fresh deploy catches up immediately.
Set ASX_SHORTS_INGEST=off to disable (tests, local dev).
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import ingest, sec_insiders
from .db import connect

log = logging.getLogger("asx_shorts.scheduler")
SYDNEY = ZoneInfo("Australia/Sydney")


def next_run(now: datetime, hour: int) -> datetime:
    """Next weekday at `hour`:00 Sydney time strictly after `now` (any timezone)."""
    local = now.astimezone(SYDNEY)
    candidate = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def catch_up() -> dict[str, int]:
    conn = connect()
    try:
        last = conn.execute("SELECT max(position_date) FROM reports WHERE status = 'ok'").fetchone()[0]
        since = date.fromisoformat(last) - timedelta(days=14) if last else ingest.FIRST_REPORT
        today = datetime.now(SYDNEY).date()
        stats = ingest.run(conn, since, today, log=log.info)
        log.info("ingest finished: %s", stats)
        return stats
    finally:
        conn.close()


class DailyIngest:
    def __init__(self, hour: int | None = None, startup_delay: float = 30.0):
        self.hour = hour if hour is not None else int(os.environ.get("ASX_SHORTS_INGEST_HOUR", "20"))
        self.startup_delay = startup_delay
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if os.environ.get("ASX_SHORTS_INGEST", "on").lower() == "off":
            log.info("daily ingest disabled")
            return
        self._thread = threading.Thread(target=self._loop, name="daily-ingest", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        if self._stop.wait(self.startup_delay):
            return
        while not self._stop.is_set():
            try:
                catch_up()
            except Exception:                         # never let one bad night kill the thread
                log.exception("daily ingest failed")
            try:
                conn = connect()
                try:
                    log.info("SEC insider catch-up finished: %s", sec_insiders.catch_up(conn, log=log.info))
                finally:
                    conn.close()
            except Exception:
                log.exception("SEC insider catch-up failed")
            wake = next_run(datetime.now(SYDNEY), self.hour)
            log.info("next ingest at %s", wake.isoformat())
            if self._stop.wait((wake - datetime.now(SYDNEY)).total_seconds()):
                return
