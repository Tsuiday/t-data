"""US insider transactions (SEC Forms 3, 4, 5).

Two sources, normalised to the same rows so they can overwrite each other safely:

  * bulk:  SEC "Insider Transactions Data Sets", one zip per quarter since 2006
           (TSV tables extracted by the SEC from the filings' XML).
  * daily: EDGAR's daily form index -> each new Form 3/4/5 -> its ownershipDocument
           XML, so data is current within a day instead of a quarter.

A filing is keyed by its accession number; its transactions by (accession,
derivative, seq) where seq is the transaction's position within the filing's
table. Both sources produce identical keys, so a daily row is replaced in place
when the quarterly file arrives.

EDGAR data is US government-published and freely reusable. The SEC asks for at
most 10 requests per second and a User-Agent identifying the requester; set
SEC_USER_AGENT to "name contact@domain" in production.

Deliberately NOT stored: footnotes (bulky legal text) and reporting owners'
street addresses (personal, no trading value).
"""
from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib import error, request

INDEX_PAGE = "https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets"
SEC_BASE = "https://www.sec.gov"
DAILY_INDEX = SEC_BASE + "/Archives/edgar/daily-index/{y}/QTR{q}/form.{d:%Y%m%d}.idx"
FORMS = {"3", "3/A", "4", "4/A", "5", "5/A"}
ATTRIBUTION = "Source: U.S. Securities and Exchange Commission, EDGAR (Forms 3, 4, 5)."

# What each transaction code means, so an assistant can explain a row.
CODES = {
    "P": "open-market or private purchase", "S": "open-market or private sale",
    "A": "grant or award from the company", "D": "disposition back to the company",
    "F": "shares withheld to pay tax or exercise price", "M": "option exercise or conversion",
    "C": "conversion of a derivative", "X": "exercise of an in- or at-the-money derivative",
    "G": "gift", "J": "other acquisition or disposition", "K": "equity swap",
    "W": "acquired or disposed by will or inheritance", "I": "discretionary transaction",
    "L": "small acquisition", "Z": "deposit into or withdrawal from a voting trust",
    "E": "expiration of a short derivative position", "H": "expiration of a long derivative position",
    "O": "exercise of an out-of-the-money derivative", "U": "disposition in a tender of shares",
    "V": "transaction voluntarily reported earlier than required",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS insider_filings (
    accession   TEXT PRIMARY KEY,
    filing_date TEXT NOT NULL,
    period      TEXT,
    form_type   TEXT NOT NULL,
    issuer_cik  INTEGER NOT NULL,
    issuer_name TEXT NOT NULL,
    ticker      TEXT,                 -- as reported by the filer (upper-cased)
    plan_10b5_1 INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL CHECK (source IN ('bulk', 'daily'))
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS insider_filings_ticker ON insider_filings (ticker, filing_date);
CREATE INDEX IF NOT EXISTS insider_filings_issuer ON insider_filings (issuer_cik, filing_date);
CREATE INDEX IF NOT EXISTS insider_filings_date   ON insider_filings (filing_date);

CREATE TABLE IF NOT EXISTS insider_owners (
    accession    TEXT NOT NULL,
    owner_cik    INTEGER NOT NULL,
    owner_name   TEXT NOT NULL,
    relationship TEXT,               -- e.g. "Director,Officer"
    title        TEXT,
    PRIMARY KEY (accession, owner_cik)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS insider_trades (
    accession      TEXT NOT NULL,
    derivative     INTEGER NOT NULL,   -- 0 = shares, 1 = options/warrants/etc.
    seq            INTEGER NOT NULL,   -- position within the filing's table
    security_title TEXT,
    trade_date     TEXT,
    code           TEXT,
    shares         REAL,
    price          REAL,
    acq_disp       TEXT,               -- A = acquired, D = disposed
    shares_after   REAL,
    direct         TEXT,               -- D = direct, I = indirect
    exercise_price REAL,
    underlying     REAL,
    PRIMARY KEY (accession, derivative, seq)
) WITHOUT ROWID;
-- Queries narrow by issuer or filing date first; a (code, trade_date) index made
-- SQLite scan every purchase since 2006 instead, so it is dropped where it exists.
DROP INDEX IF EXISTS insider_trades_code_date;

CREATE TABLE IF NOT EXISTS insider_loads (
    period    TEXT PRIMARY KEY,        -- '2026q2' or a daily date '2026-10-16'
    kind      TEXT NOT NULL CHECK (kind IN ('quarter', 'day')),
    filings   INTEGER NOT NULL,
    loaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@dataclass
class Filing:
    accession: str
    filing_date: str
    period: str | None
    form_type: str
    issuer_cik: int
    issuer_name: str
    ticker: str | None
    plan_10b5_1: bool
    owners: list[tuple] = field(default_factory=list)   # (cik, name, relationship, title)
    trades: list[tuple] = field(default_factory=list)   # rows for insider_trades minus accession


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


# ---- helpers -----------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def iso(value: str | None) -> str | None:
    """'01-APR-2024' or '2024-04-01' (optionally with time/zone) -> '2024-04-01'."""
    if not value:
        return None
    v = value.strip()
    m = re.match(r"^(\d{2})-([A-Z]{3})-(\d{4})", v.upper())
    if m and m.group(2) in _MONTHS:
        return f"{m.group(3)}-{_MONTHS[m.group(2)]:02d}-{m.group(1)}"
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", v)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def num(value: str | None) -> float | None:
    """Numbers are rounded to 2 decimals: the SEC's quarterly data sets are, so
    rounding the daily XML the same way keeps a trade identical when its
    quarterly row later replaces the daily one (e.g. 150.0874 -> 150.09)."""
    if value is None:
        return None
    v = value.strip().replace(",", "")
    try:
        return round(float(v), 2) if v else None
    except ValueError:
        return None


def ticker_of(value: str | None) -> str | None:
    if not value:
        return None
    t = value.strip().upper()
    # Filers type all sorts here ("NONE", "N/A", "AAPL, AAPL.B"); keep the first plausible symbol.
    if t in {"NONE", "NA", "N/A", "N.A.", "N/A.", "-", ""}:
        return None
    t = re.split(r"[\s,;/]+", t)[0]
    return t if re.fullmatch(r"[A-Z0-9.\-]{1,10}", t) and t not in {"NONE", "NA", "N"} else None


def relationship(director: bool, officer: bool, ten_pct: bool, other: bool) -> str:
    parts = [n for n, on in (("Director", director), ("Officer", officer), ("TenPercentOwner", ten_pct), ("Other", other)) if on]
    return ",".join(parts)


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "y", "yes"}


def user_agent() -> str:
    return os.environ.get("SEC_USER_AGENT", "tradingdata-ai research admin@example.org")


class _Pacer:
    """At most `rate` requests per second across threads (SEC asks for <= 10)."""

    def __init__(self, rate: float = 8.0):
        self.interval = 1.0 / rate
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            if now < self.next:
                time.sleep(self.next - now)
            self.next = max(now, self.next) + self.interval


_pacer = _Pacer()


def http_get(url: str, *, retries: int = 4, timeout: float = 60.0) -> bytes:
    last: Exception | None = None
    for attempt in range(retries):
        _pacer.wait()
        try:
            req = request.Request(url, headers={"User-Agent": user_agent(), "Accept-Encoding": "identity"})
            with request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except error.HTTPError as exc:
            if exc.code == 404:
                raise FileNotFoundError(url) from exc
            last = exc
            time.sleep(10 if exc.code in (403, 429) else 2 ** attempt)
        except (error.URLError, TimeoutError) as exc:
            last = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"SEC fetch failed: {url}: {last}")


# ---- bulk quarterly data sets ---------------------------------------------------

def quarter_links(page_html: str) -> dict[str, str]:
    """{'2026q2': 'https://www.sec.gov/files/.../2026q2_form345.zip', ...} from the data-sets page.
    The SEC has moved these files between folders, so links are read, not guessed."""
    out = {}
    for href in re.findall(r'href="([^"]*?(\d{4}q[1-4])_form345\.zip)"', page_html):
        url, q = href
        out[q] = url if url.startswith("http") else SEC_BASE + url
    return out


def parse_bulk(zip_bytes: bytes) -> list[Filing]:
    csv.field_size_limit(10**9)
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))

    def table(name: str):
        # Exact file name: "DERIV_TRANS.tsv" must not match "NONDERIV_TRANS.tsv".
        member = next(n for n in zf.namelist() if n.rsplit("/", 1)[-1].upper() == name.upper() + ".TSV")
        text = io.TextIOWrapper(zf.open(member), encoding="utf-8", errors="replace", newline="")
        return csv.DictReader(text, delimiter="\t", quoting=csv.QUOTE_NONE)

    filings: dict[str, Filing] = {}
    for r in table("SUBMISSION"):
        try:
            cik = int(r["ISSUERCIK"])
        except (TypeError, ValueError):
            continue
        filings[r["ACCESSION_NUMBER"]] = Filing(
            accession=r["ACCESSION_NUMBER"], filing_date=iso(r["FILING_DATE"]) or "",
            period=iso(r.get("PERIOD_OF_REPORT")), form_type=(r.get("DOCUMENT_TYPE") or "").strip(),
            issuer_cik=cik, issuer_name=(r.get("ISSUERNAME") or "").strip(),
            ticker=ticker_of(r.get("ISSUERTRADINGSYMBOL")), plan_10b5_1=truthy(r.get("AFF10B5ONE")),
        )
    for r in table("REPORTINGOWNER"):
        f = filings.get(r["ACCESSION_NUMBER"])
        if f is None:
            continue
        try:
            cik = int(r["RPTOWNERCIK"])
        except (TypeError, ValueError):
            continue
        f.owners.append((cik, (r.get("RPTOWNERNAME") or "").strip(), (r.get("RPTOWNER_RELATIONSHIP") or "").strip(),
                         (r.get("RPTOWNER_TITLE") or "").strip() or None))

    for name, deriv, sk in (("NONDERIV_TRANS", 0, "NONDERIV_TRANS_SK"), ("DERIV_TRANS", 1, "DERIV_TRANS_SK")):
        grouped: dict[str, list[tuple[int, dict]]] = {}
        for r in table(name):
            if r["ACCESSION_NUMBER"] in filings:
                try:
                    grouped.setdefault(r["ACCESSION_NUMBER"], []).append((int(r[sk]), r))
                except (TypeError, ValueError):
                    continue
        for acc, items in grouped.items():
            for seq, (_, r) in enumerate(sorted(items, key=lambda x: x[0]), 1):    # SK order = filing order
                filings[acc].trades.append((
                    deriv, seq, (r.get("SECURITY_TITLE") or "").strip() or None, iso(r.get("TRANS_DATE")),
                    (r.get("TRANS_CODE") or "").strip() or None, num(r.get("TRANS_SHARES")),
                    num(r.get("TRANS_PRICEPERSHARE")), (r.get("TRANS_ACQUIRED_DISP_CD") or "").strip() or None,
                    num(r.get("SHRS_OWND_FOLWNG_TRANS")), (r.get("DIRECT_INDIRECT_OWNERSHIP") or "").strip() or None,
                    num(r.get("CONV_EXERCISE_PRICE")) if deriv else None, num(r.get("UNDLYNG_SEC_SHARES")) if deriv else None,
                ))
    return [f for f in filings.values() if f.filing_date]


# ---- daily filings (ownershipDocument XML) -----------------------------------

def _t(el: ET.Element | None, path: str) -> str | None:
    """Text of `path` or of `path/value` (most numbers are wrapped in <value>)."""
    if el is None:
        return None
    node = el.find(path)
    if node is None:
        return None
    inner = node.find("value")
    text = (inner.text if inner is not None else node.text) or ""
    return text.strip() or None


def parse_ownership_xml(xml: bytes, accession: str, filing_date: str) -> Filing | None:
    m = re.search(rb"<ownershipDocument>.*?</ownershipDocument>", xml, re.S)
    if not m:
        return None
    root = ET.fromstring(m.group(0))
    issuer = root.find("issuer")
    try:
        cik = int(_t(issuer, "issuerCik") or "")
    except ValueError:
        return None
    f = Filing(
        accession=accession, filing_date=filing_date, period=iso(_t(root, "periodOfReport")),
        form_type=(_t(root, "documentType") or "").strip(), issuer_cik=cik,
        issuer_name=(_t(issuer, "issuerName") or "").strip(), ticker=ticker_of(_t(issuer, "issuerTradingSymbol")),
        plan_10b5_1=truthy(_t(root, "aff10b5One")),
    )
    for ro in root.findall("reportingOwner"):
        try:
            ocik = int(_t(ro, "reportingOwnerId/rptOwnerCik") or "")
        except ValueError:
            continue
        rel = ro.find("reportingOwnerRelationship")
        f.owners.append((
            ocik, (_t(ro, "reportingOwnerId/rptOwnerName") or "").strip(),
            relationship(truthy(_t(rel, "isDirector")), truthy(_t(rel, "isOfficer")),
                         truthy(_t(rel, "isTenPercentOwner")), truthy(_t(rel, "isOther"))),
            _t(rel, "officerTitle"),
        ))
    for deriv, table, row in ((0, "nonDerivativeTable", "nonDerivativeTransaction"), (1, "derivativeTable", "derivativeTransaction")):
        for seq, tx in enumerate(root.findall(f"{table}/{row}"), 1):
            f.trades.append((
                deriv, seq, _t(tx, "securityTitle"), iso(_t(tx, "transactionDate")),
                _t(tx, "transactionCoding/transactionCode"), num(_t(tx, "transactionAmounts/transactionShares")),
                num(_t(tx, "transactionAmounts/transactionPricePerShare")),
                _t(tx, "transactionAmounts/transactionAcquiredDisposedCode"),
                num(_t(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
                _t(tx, "ownershipNature/directOrIndirectOwnership"),
                num(_t(tx, "conversionOrExercisePrice")) if deriv else None,
                num(_t(tx, "underlyingSecurity/underlyingSecurityShares")) if deriv else None,
            ))
    return f


def daily_index_entries(idx: bytes) -> list[tuple[str, str, str]]:
    """[(form, accession, archive_path)] for Forms 3/4/5 in an EDGAR form.YYYYMMDD.idx.
    Each filing is listed once per party (issuer and owner), so accessions are deduplicated."""
    seen, out = set(), []
    for line in idx.decode("latin-1").splitlines():
        m = re.match(r"^(\S+(?:/A)?)\s+.*?\s+(\d+)\s+(\d{4}-?\d{2}-?\d{2})\s+(edgar/data/\S+\.txt)\s*$", line)
        if not m or m.group(1) not in FORMS:
            continue
        acc = m.group(4).rsplit("/", 1)[-1].removesuffix(".txt")
        if acc not in seen:
            seen.add(acc)
            out.append((m.group(1), acc, m.group(4)))
    return out


# ---- storage -------------------------------------------------------------------

def store(conn: sqlite3.Connection, filings: list[Filing], source: str) -> int:
    with conn:
        for f in filings:
            conn.execute("DELETE FROM insider_owners WHERE accession = ?", (f.accession,))
            conn.execute("DELETE FROM insider_trades WHERE accession = ?", (f.accession,))
            conn.execute(
                "INSERT OR REPLACE INTO insider_filings VALUES (?,?,?,?,?,?,?,?,?)",
                (f.accession, f.filing_date, f.period, f.form_type, f.issuer_cik, f.issuer_name,
                 f.ticker, int(f.plan_10b5_1), source),
            )
            conn.executemany("INSERT OR REPLACE INTO insider_owners VALUES (?,?,?,?,?)",
                             [(f.accession, *o) for o in f.owners])
            conn.executemany("INSERT OR REPLACE INTO insider_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             [(f.accession, *t) for t in f.trades])
    return len(filings)


def load_quarter(conn: sqlite3.Connection, quarter: str, url: str, log=print) -> int:
    filings = parse_bulk(http_get(url, timeout=300))
    n = store(conn, filings, "bulk")
    with conn:
        conn.execute("INSERT OR REPLACE INTO insider_loads (period, kind, filings) VALUES (?, 'quarter', ?)", (quarter, n))
    log(f"{quarter}: {n} filings")
    return n


def load_day(conn: sqlite3.Connection, d: date, log=print) -> int:
    q = (d.month - 1) // 3 + 1
    try:
        idx = http_get(DAILY_INDEX.format(y=d.year, q=q, d=d))
    except FileNotFoundError:
        return 0                                   # weekend, holiday, or not published yet
    filings = []
    for form, acc, path in daily_index_entries(idx):
        if conn.execute("SELECT 1 FROM insider_filings WHERE accession = ? AND source = 'bulk'", (acc,)).fetchone():
            continue                                # the quarterly file already has it
        try:
            raw = http_get(f"{SEC_BASE}/Archives/{path}")
            f = parse_ownership_xml(raw, acc, d.isoformat())
        except (FileNotFoundError, ET.ParseError):
            continue
        if f:
            filings.append(f)
    n = store(conn, filings, "daily")
    with conn:
        conn.execute("INSERT OR REPLACE INTO insider_loads (period, kind, filings) VALUES (?, 'day', ?)", (d.isoformat(), n))
    log(f"{d}: {n} filings from the daily index")
    return n


def catch_up(conn: sqlite3.Connection, today: date | None = None, log=print) -> dict[str, int | str]:
    """Load any quarterly files not yet loaded, then daily filings since the last quarter."""
    ensure_schema(conn)
    today = today or date.today()
    links = quarter_links(http_get(INDEX_PAGE).decode("utf-8", "replace"))
    have = {r[0] for r in conn.execute("SELECT period FROM insider_loads WHERE kind = 'quarter'")}
    stats = {"quarters": 0, "days": 0, "daily_filings": 0}
    for q in sorted(links):
        if q not in have:
            load_quarter(conn, q, links[q], log)
            stats["quarters"] += 1
    if links:
        last_q = max(links)
        y, n = int(last_q[:4]), int(last_q[-1])
        start = date(y + (n == 4), (n % 4) * 3 + 1, 1)     # first day after the last bulk quarter
        done_days = {r[0] for r in conn.execute("SELECT period FROM insider_loads WHERE kind = 'day'")}
        d = max(start, today - timedelta(days=120))
        while d <= today:
            recent = (today - d).days <= 3              # re-check the last few days for late index updates
            if d.weekday() < 5 and (recent or d.isoformat() not in done_days):
                try:
                    stats["daily_filings"] += load_day(conn, d, log)
                except RuntimeError as exc:             # e.g. a temporary SEC block; days done so far are kept
                    log(f"{d}: stopping, will resume next run: {exc}")
                    stats["stopped_at"] = d.isoformat()
                    break
                stats["days"] += 1
            d += timedelta(days=1)
    return stats


def main() -> None:
    import argparse

    from .db import connect

    ap = argparse.ArgumentParser(description="Load SEC insider transactions (Forms 3/4/5).")
    ap.add_argument("--db")
    ap.add_argument("--quarters", nargs="*", help="only these quarters, e.g. 2026q2 2026q1")
    args = ap.parse_args()
    conn = connect(args.db)
    ensure_schema(conn)
    if args.quarters:
        links = quarter_links(http_get(INDEX_PAGE).decode("utf-8", "replace"))
        for q in args.quarters:
            load_quarter(conn, q, links[q])
    else:
        print(catch_up(conn))


if __name__ == "__main__":
    main()
