"""US planned insider sales (SEC Form 144).

An affiliate (officer, director, large holder) must file Form 144 *before*
selling restricted or control shares, so it shows sales that are about to
happen; Form 4 later reports the sale itself. Electronic, structured filing has
been mandatory since 13 April 2023 (earlier notices were mostly on paper).

Sources:
  * current quarter: EDGAR's daily form index, day by day;
  * earlier quarters: EDGAR's quarterly full index (one file per quarter).
Each notice is then fetched once (its .txt submission holds the XML).

There are ~20,000 notices a quarter, so the full history is ~9 hours of polite
downloading. catch_up() therefore loads the newest data first and backfills
history within a time budget per run; the history fills in over a few daily
runs without delaying the other updates.

A notice names the issuer by CIK only (no ticker): tickers come from the SEC's
company_tickers.json, falling back to the latest ticker in the Form 4 data.
"""
from __future__ import annotations

import gzip
import json
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .sec_insiders import (DAILY_INDEX, DAILY_LISTING, SEC_BASE, daily_index_entries, http_get,
                           num, published_days, ticker_of)

FORMS = {"144", "144/A"}
FIRST_QUARTER = "2023q2"                      # electronic filing mandatory from 13 April 2023
FULL_INDEX = SEC_BASE + "/Archives/edgar/full-index/{y}/QTR{q}/form.gz"
TICKERS_URL = SEC_BASE + "/files/company_tickers.json"
ATTRIBUTION = "Source: U.S. Securities and Exchange Commission, EDGAR (Form 144)."

SCHEMA = """
CREATE TABLE IF NOT EXISTS form144_notices (
    accession        TEXT PRIMARY KEY,
    filing_date      TEXT NOT NULL,
    form_type        TEXT NOT NULL,          -- 144 or 144/A
    issuer_cik       INTEGER NOT NULL,
    issuer_name      TEXT,
    seller           TEXT,                   -- person for whose account the shares are sold
    relationship     TEXT,                   -- e.g. 'Officer' or 'Director,Officer'
    security_class   TEXT,
    units            REAL,                   -- shares to be sold (summed over lines)
    market_value     REAL,                   -- their aggregate market value, US$
    units_outstanding REAL,
    approx_sale_date TEXT,
    exchange         TEXT,
    broker           TEXT,
    acquired_how     TEXT,                   -- how the shares were acquired, e.g. 'Restricted Stock Units'
    plan_adopted     TEXT,                   -- 10b5-1 plan adoption date, if the sale is under a plan
    sold_past_3m_usd REAL                    -- gross proceeds of the seller's sales in the past 3 months
);
CREATE INDEX IF NOT EXISTS form144_issuer ON form144_notices (issuer_cik, filing_date);
CREATE INDEX IF NOT EXISTS form144_date   ON form144_notices (filing_date);
CREATE TABLE IF NOT EXISTS form144_loads (
    period    TEXT PRIMARY KEY,              -- '2026q3' or a day '2026-10-16'
    kind      TEXT NOT NULL CHECK (kind IN ('quarter', 'day')),
    filings   INTEGER NOT NULL,
    loaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS cik_tickers (
    cik        INTEGER PRIMARY KEY,
    ticker     TEXT NOT NULL,
    name       TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS cik_tickers_ticker ON cik_tickers (ticker);
"""


@dataclass
class Notice:
    accession: str
    filing_date: str
    form_type: str
    issuer_cik: int
    issuer_name: str | None
    seller: str | None
    relationship: str | None
    security_class: str | None
    units: float | None
    market_value: float | None
    units_outstanding: float | None
    approx_sale_date: str | None
    exchange: str | None
    broker: str | None
    acquired_how: str | None
    plan_adopted: str | None
    sold_past_3m_usd: float | None


def us_date(value: str | None) -> str | None:
    """'10/16/2026' -> '2026-10-16'; ISO dates pass through."""
    if not value:
        return None
    v = value.strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", v)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", v)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        el.tag = el.tag.rsplit("}", 1)[-1]
    return root


def _text(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    found = el.find(path)
    return found.text.strip() if found is not None and found.text and found.text.strip() else None


def _sum(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals), 2) if vals else None


def _join(values: list[str | None]) -> str | None:
    out = list(dict.fromkeys(v for v in values if v))          # unique, in order
    return "; ".join(out) if out else None


def parse_notice(raw: bytes, accession: str, filing_date: str) -> Notice | None:
    """Parse the Form 144 XML inside an EDGAR .txt submission (None if there is none)."""
    m = re.search(rb"<(?:\w+:)?edgarSubmission\b.*?</(?:\w+:)?edgarSubmission>", raw, re.S)
    if not m:
        return None
    try:
        root = _strip_ns(ET.fromstring(m.group(0)))
    except ET.ParseError:
        return None
    form = root.find("formData")
    issuer = form.find("issuerInfo") if form is not None else None
    try:
        cik = int(_text(issuer, "issuerCik") or "")
    except ValueError:
        return None
    lines = form.findall("securitiesInformation")
    rels = [r.text.strip() for r in issuer.findall("relationshipsToIssuer/relationshipToIssuer") if r.text and r.text.strip()]
    plans = sorted(filter(None, (us_date(p.text) for p in form.findall("noticeSignature/planAdoptionDates/planAdoptionDate"))))
    return Notice(
        accession=accession,
        filing_date=filing_date,
        form_type=_text(root, "headerData/submissionType") or "144",
        issuer_cik=cik,
        issuer_name=_text(issuer, "issuerName"),
        seller=_text(issuer, "nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold"),
        relationship=",".join(sorted(set(rels))) or None,
        security_class=_join([_text(s, "securitiesClassTitle") for s in lines]),
        units=_sum([num(_text(s, "noOfUnitsSold")) for s in lines]),
        market_value=_sum([num(_text(s, "aggregateMarketValue")) for s in lines]),
        units_outstanding=max(filter(None, (num(_text(s, "noOfUnitsOutstanding")) for s in lines)), default=None),
        approx_sale_date=min(filter(None, (us_date(_text(s, "approxSaleDate")) for s in lines)), default=None),
        exchange=_join([_text(s, "securitiesExchangeName") for s in lines]),
        broker=_join([_text(s, "brokerOrMarketmakerDetails/name") for s in lines]),
        acquired_how=_join([_text(a, "natureOfAcquisitionTransaction") for a in form.findall("securitiesToBeSold")]),
        plan_adopted=plans[-1] if plans else None,
        sold_past_3m_usd=_sum([num(_text(p, "grossProceeds")) for p in form.findall("securitiesSoldInPast3Months")]),
    )


# ---- storage -------------------------------------------------------------------

def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def store(conn: sqlite3.Connection, notices: list[Notice]) -> int:
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO form144_notices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [tuple(vars(n).values()) for n in notices])
    return len(notices)


def refresh_tickers(conn: sqlite3.Connection, max_age_hours: float = 20) -> int:
    """Reload the SEC's CIK -> ticker list if it's older than `max_age_hours`."""
    last = conn.execute("SELECT max(updated_at) FROM cik_tickers").fetchone()[0]
    now = datetime.now(timezone.utc).replace(tzinfo=None)          # SQLite's datetime('now') is naive UTC
    if last and now - datetime.fromisoformat(last) < timedelta(hours=max_age_hours):
        return 0
    rows: dict[int, tuple[str, str]] = {}
    for item in json.loads(http_get(TICKERS_URL)).values():
        t = ticker_of(str(item.get("ticker", "")))
        if t and int(item["cik_str"]) not in rows:       # first listing = primary class
            rows[int(item["cik_str"])] = (t, item.get("title"))
    with conn:
        conn.execute("DELETE FROM cik_tickers")
        conn.executemany("INSERT INTO cik_tickers (cik, ticker, name) VALUES (?,?,?)",
                         [(c, t, n) for c, (t, n) in rows.items()])
    return len(rows)


# ---- loading ---------------------------------------------------------------------

def _quarter(d: date) -> str:
    return f"{d.year}q{(d.month - 1) // 3 + 1}"


def _quarters(first: str, last: str) -> list[str]:
    y, q = int(first[:4]), int(first[-1])
    out = []
    while f"{y}q{q}" <= last:
        out.append(f"{y}q{q}")
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)
    return out


class _OutOfTime(Exception):
    pass


WORKERS = 4      # parallel fetches; the shared pacer still caps the total at 8 requests/s


def _fetch(entry) -> Notice | None:
    _form, acc, path, filed = entry
    try:
        return parse_notice(http_get(f"{SEC_BASE}/Archives/{path}"), acc, filed)
    except FileNotFoundError:
        return None


def _load_entries(conn, entries, deadline: float | None, log) -> int:
    """Fetch and store notices not already stored, WORKERS at a time (one request is mostly
    waiting on the network). Stores in chunks so progress survives a stop."""
    todo = [e for e in entries
            if not conn.execute("SELECT 1 FROM form144_notices WHERE accession = ?", (e[1],)).fetchone()]
    n = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for i in range(0, len(todo), 200):
            if deadline is not None and time.monotonic() > deadline:
                raise _OutOfTime(n)
            n += store(conn, [x for x in pool.map(_fetch, todo[i:i + 200]) if x])
    return n


def _entries(idx: bytes) -> list[tuple[str, str, str, str]]:
    """(form, accession, path, filing date) for Form 144 lines of an EDGAR form index."""
    out = []
    dates = {}
    for line in idx.decode("latin-1").splitlines():
        m = re.match(r"^(144(?:/A)?)\s+.*?\s+\d+\s+(\d{4})-?(\d{2})-?(\d{2})\s+(edgar/data/\S+\.txt)\s*$", line)
        if m:
            dates[m.group(5).rsplit("/", 1)[-1].removesuffix(".txt")] = f"{m.group(2)}-{m.group(3)}-{m.group(4)}"
    for form, acc, path in daily_index_entries(idx, FORMS):
        out.append((form, acc, path, dates[acc]))
    return out


def load_day(conn: sqlite3.Connection, d: date, deadline: float | None = None, log=print) -> int:
    idx = http_get(DAILY_INDEX.format(y=d.year, q=(d.month - 1) // 3 + 1, d=d))
    n = _load_entries(conn, _entries(idx), deadline, log)
    with conn:
        conn.execute("INSERT OR REPLACE INTO form144_loads (period, kind, filings) VALUES (?, 'day', ?)", (d.isoformat(), n))
    log(f"{d}: {n} Form 144 notices")
    return n


def load_quarter(conn: sqlite3.Connection, quarter: str, deadline: float | None = None, log=print) -> int:
    y, q = int(quarter[:4]), int(quarter[-1])
    raw = gzip.decompress(http_get(FULL_INDEX.format(y=y, q=q)))      # ~50 MB, every form type
    # Keep only the Form 144 lines before decoding: splitting the whole index into
    # Python strings would need a few hundred MB on a 512 MB machine.
    entries = _entries(b"\n".join(re.findall(rb"^144(?:/A)?\s.*$", raw, re.M)))
    del raw
    log(f"{quarter}: {len(entries)} Form 144 notices listed")
    n = _load_entries(conn, entries, deadline, log)
    with conn:
        conn.execute("INSERT OR REPLACE INTO form144_loads (period, kind, filings) VALUES (?, 'quarter', ?)",
                     (quarter, len(entries)))
    log(f"{quarter}: done ({n} new)")
    return n


def catch_up(conn: sqlite3.Connection, today: date | None = None, budget_seconds: float = 2700,
             log=print) -> dict[str, int | str]:
    """Current quarter day by day (newest data first), then earlier quarters newest-first,
    until `budget_seconds` runs out. Unfinished work resumes on the next run."""
    ensure_schema(conn)
    today = today or date.today()
    deadline = time.monotonic() + budget_seconds
    stats: dict[str, int | str] = {"tickers": 0, "days": 0, "quarters": 0, "notices": 0}
    try:
        stats["tickers"] = refresh_tickers(conn)
    except Exception as exc:                         # a stale ticker list is better than no update
        log(f"ticker list not refreshed: {exc}")
    done = {r[0]: r[1] for r in conn.execute("SELECT period, kind FROM form144_loads")}
    this_q = _quarter(today)
    try:
        days = sorted(published_days(http_get(DAILY_LISTING.format(y=today.year, q=(today.month - 1) // 3 + 1))))
    except FileNotFoundError:
        days = []
    try:
        for d in days:
            recent = (today - d).days <= 3
            if d <= today and (recent or d.isoformat() not in done):
                stats["notices"] += load_day(conn, d, deadline, log)
                stats["days"] += 1
        for quarter in reversed(_quarters(FIRST_QUARTER, this_q)[:-1]):
            if done.get(quarter) != "quarter":
                stats["notices"] += load_quarter(conn, quarter, deadline, log)
                stats["quarters"] += 1
    except _OutOfTime as stop:
        stats["notices"] += stop.args[0]
        stats["stopped"] = "time budget reached; resumes next run"
        log("Form 144: time budget reached, will resume next run")
    except RuntimeError as exc:                      # e.g. SEC unavailable; resume next run
        stats["stopped"] = str(exc)
        log(f"Form 144: stopping, will resume next run: {exc}")
    return stats


def main() -> None:
    import argparse

    from .db import connect

    ap = argparse.ArgumentParser(description="Load SEC Form 144 notices (planned insider sales).")
    ap.add_argument("--db")
    ap.add_argument("--budget", type=float, default=2700, help="seconds to spend on this run")
    ap.add_argument("--quarters", nargs="*", help="only these quarters, e.g. 2026q3")
    args = ap.parse_args()
    conn = connect(args.db)
    ensure_schema(conn)
    if args.quarters:
        refresh_tickers(conn)
        for q in args.quarters:
            load_quarter(conn, q)
    else:
        print(catch_up(conn, budget_seconds=args.budget))


if __name__ == "__main__":
    main()
