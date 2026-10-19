"""Download and parse ASIC's daily aggregated short-position reports.

Source: https://asic.gov.au/regulatory-resources/markets/short-selling/short-position-reports-table/
Licence: ASIC publishes short selling position reporting data under CC BY 4.0
(reproduction allowed, including for sale) with the attribution in ATTRIBUTION.

Each file is named by its POSITION date (RRyyyymmdd); ASIC publishes it about
four trading days later. Two layouts exist:
  * to mid-2022: UTF-16 with a BOM, tab-separated, product codes padded with spaces
  * since:       UTF-8, comma-separated
The "% short" column has used different scaling over time, so the percentage is
always recomputed here from the share counts.
"""
from __future__ import annotations

import csv
import hashlib
import time
from dataclasses import dataclass
from datetime import date
from urllib import error, request

URL = "https://download.asic.gov.au/short-selling/RR{d:%Y%m%d}-001-SSDailyAggShortPos.csv"
ATTRIBUTION = "© Australian Securities & Investments Commission: Reproduced with permission."
LICENCE = "CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/)"
USER_AGENT = "asx-shorts/0.1 (+https://github.com/eent412)"


@dataclass(frozen=True)
class Position:
    code: str
    name: str
    short_positions: int
    shares_on_issue: int

    @property
    def short_pct(self) -> float:
        return self.short_positions / self.shares_on_issue if self.shares_on_issue else 0.0


class ReportNotPublished(Exception):
    """No report exists for that date (weekend, public holiday, or not yet published)."""


def parse(raw: bytes) -> list[Position]:
    """Parse one report in either layout. Rows that don't reconcile are dropped."""
    utf16 = raw[:2] in (b"\xff\xfe", b"\xfe\xff")
    text = raw.decode("utf-16" if utf16 else "utf-8-sig", errors="replace")
    reader = csv.reader(text.splitlines(), delimiter="\t" if utf16 else ",")
    header = next(reader, None)
    if not header or "product" not in header[0].lower():
        raise ValueError("not an ASIC short-position report")
    out: dict[str, Position] = {}
    for row in reader:
        if len(row) < 4:
            continue
        name, code = row[0].strip(), row[1].strip().upper()
        try:
            short, issued = int(float(row[2])), int(float(row[3]))
        except ValueError:
            continue
        if not code or issued <= 0 or short < 0 or short > issued:
            continue
        out[code] = Position(code, name, short, issued)   # last row wins on duplicates
    return list(out.values())


def fetch(d: date, *, retries: int = 3, timeout: float = 30.0) -> tuple[bytes, str]:
    """Return (raw bytes, url). Raises ReportNotPublished on 404."""
    url = URL.format(d=d)
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = request.Request(url, headers={"User-Agent": USER_AGENT})
            with request.urlopen(req, timeout=timeout) as resp:
                return resp.read(), url
        except error.HTTPError as exc:
            if exc.code == 404:
                raise ReportNotPublished(d.isoformat()) from exc
            last = exc
        except (error.URLError, TimeoutError) as exc:
            last = exc
        time.sleep(2 ** attempt)
    raise RuntimeError(f"ASIC fetch failed for {d}: {last}")


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
