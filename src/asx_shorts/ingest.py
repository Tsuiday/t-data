"""Incrementally load ASIC reports into SQLite.

    uv run asx-shorts-ingest                 # catch up from the last loaded date
    uv run asx-shorts-ingest --since 2010-06-16
    uv run asx-shorts-ingest --from-dir ~/asic   # load already-downloaded files

Weekends are skipped. A 404 on a weekday older than 10 days is recorded as
'none' (public holiday) so it isn't retried; recent 404s are retried next run
because ASIC publishes about four trading days after the position date.
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import asic
from .db import connect

FIRST_REPORT = date(2010, 6, 16)


def store(conn: sqlite3.Connection, d: date, raw: bytes, url: str) -> int:
    rows = asic.parse(raw)
    iso = d.isoformat()
    with conn:
        conn.execute("DELETE FROM positions WHERE position_date = ?", (iso,))
        conn.executemany(
            "INSERT INTO positions (code, position_date, short_positions, shares_on_issue, short_pct) VALUES (?,?,?,?,?)",
            [(p.code, iso, p.short_positions, p.shares_on_issue, p.short_pct) for p in rows],
        )
        conn.executemany(
            """INSERT INTO products (code, name, first_date, last_date) VALUES (?,?,?,?)
               ON CONFLICT(code) DO UPDATE SET
                 name       = CASE WHEN excluded.last_date >= products.last_date THEN excluded.name ELSE products.name END,
                 first_date = min(products.first_date, excluded.first_date),
                 last_date  = max(products.last_date, excluded.last_date)""",
            [(p.code, p.name, iso, iso) for p in rows],
        )
        conn.execute(
            "INSERT OR REPLACE INTO reports (position_date, status, rows, source_url, sha256) VALUES (?, 'ok', ?, ?, ?)",
            (iso, len(rows), url, asic.sha256(raw)),
        )
    return len(rows)


def done_dates(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT position_date FROM reports")}


def run(conn: sqlite3.Connection, since: date, until: date, *, pause: float = 0.2, log=print) -> dict[str, int]:
    have = done_dates(conn)
    stats = {"loaded": 0, "holiday": 0, "pending": 0, "skipped": 0, "unreadable": 0}
    d = since
    while d <= until:
        if d.weekday() >= 5 or d.isoformat() in have:
            stats["skipped"] += d.weekday() < 5
            d += timedelta(days=1)
            continue
        try:
            raw, url = asic.fetch(d)
            n = store(conn, d, raw, url)
            stats["loaded"] += 1
            log(f"{d} {n} products")
        except ValueError as exc:
            # One malformed file must not stop the run; it's retried next time.
            stats["unreadable"] += 1
            log(f"{d} unreadable: {exc}")
        except asic.ReportNotPublished:
            if (until - d).days > 10:
                with conn:
                    conn.execute("INSERT OR REPLACE INTO reports (position_date, status) VALUES (?, 'none')", (d.isoformat(),))
                stats["holiday"] += 1
            else:
                stats["pending"] += 1
        time.sleep(pause)
        d += timedelta(days=1)
    return stats


def load_dir(conn: sqlite3.Connection, folder: Path, log=print) -> int:
    n = 0
    for f in sorted(folder.glob("RR*-001-SSDailyAggShortPos.csv")):
        d = datetime.strptime(f.name[2:10], "%Y%m%d").date()
        store(conn, d, f.read_bytes(), asic.URL.format(d=d))
        n += 1
    log(f"loaded {n} files from {folder}")
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db")
    ap.add_argument("--since", type=date.fromisoformat)
    ap.add_argument("--until", type=date.fromisoformat, default=date.today())
    ap.add_argument("--from-dir", type=Path)
    args = ap.parse_args()
    conn = connect(args.db)
    if args.from_dir:
        load_dir(conn, args.from_dir)
        return
    since = args.since
    if since is None:
        last = conn.execute("SELECT max(position_date) FROM reports WHERE status = 'ok'").fetchone()[0]
        since = date.fromisoformat(last) - timedelta(days=14) if last else FIRST_REPORT
    print(run(conn, since, args.until))


if __name__ == "__main__":
    main()
