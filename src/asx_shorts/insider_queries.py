"""Read queries for US insider transactions, shared by REST and MCP.

`floor` is the earliest filing date the caller's plan may see (None = all).
"Open-market buying" means transaction code P (purchase) on shares, not
options. Grants (A), tax withholding (F) and option exercises (M) are not buying
decisions, so the buying screens ignore them.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

from .queries import NotFound
from .sec_insiders import CODES

ROLE_FILTERS = {
    "any": "1=1",
    "officer_or_director": "(o.relationship LIKE '%Officer%' OR o.relationship LIKE '%Director%')",
    "officer": "o.relationship LIKE '%Officer%'",
    "director": "o.relationship LIKE '%Director%'",
    "ten_percent_owner": "o.relationship LIKE '%TenPercentOwner%'",
}


def _since(days: int, floor: str | None) -> str:
    start = (date.today() - timedelta(days=days)).isoformat()
    return max(start, floor) if floor else start


def issuer_ciks(conn: sqlite3.Connection, ticker: str) -> list[int]:
    t = ticker.strip().upper()
    ciks = [r[0] for r in conn.execute(
        "SELECT issuer_cik FROM insider_filings WHERE ticker = ? GROUP BY issuer_cik ORDER BY max(filing_date) DESC", (t,))]
    if not ciks:
        raise NotFound(f"no insider filings for ticker {t}")
    return ciks


def _value(shares, price):
    return round(shares * price, 2) if shares is not None and price is not None else None


def trades(conn: sqlite3.Connection, ticker: str, start: str | None, end: str | None,
           codes: list[str] | None, limit: int, floor: str | None) -> dict[str, Any]:
    ciks = issuer_ciks(conn, ticker)
    where = [f"f.issuer_cik IN ({','.join('?' * len(ciks))})"]
    args: list[Any] = list(ciks)
    lo = max(filter(None, [start, floor]), default=None)
    if lo:
        where.append("f.filing_date >= ?")
        args.append(lo)
    if end:
        where.append("f.filing_date <= ?")
        args.append(end)
    if codes:
        where.append(f"t.code IN ({','.join('?' * len(codes))})")
        args.extend(c.upper() for c in codes)
    args.append(limit)
    rows = conn.execute(
        f"""SELECT f.accession, f.filing_date, f.form_type, f.issuer_name, f.ticker, f.plan_10b5_1,
                   t.derivative, t.security_title, t.trade_date, t.code, t.shares, t.price,
                   t.acq_disp, t.shares_after, t.direct, t.exercise_price, t.underlying,
                   (SELECT group_concat(owner_name, '; ') FROM insider_owners WHERE accession = f.accession) AS insiders,
                   (SELECT group_concat(relationship || coalesce(' (' || title || ')', ''), '; ')
                      FROM insider_owners WHERE accession = f.accession) AS roles
            FROM insider_filings f JOIN insider_trades t USING (accession)
            WHERE {' AND '.join(where)}
            ORDER BY f.filing_date DESC, t.trade_date DESC, t.derivative, t.seq
            LIMIT ?""",
        args,
    ).fetchall()
    data = []
    for r in rows:
        d = dict(r)
        d["plan_10b5_1"] = bool(d["plan_10b5_1"])
        d["derivative"] = bool(d["derivative"])
        d["code_meaning"] = CODES.get(d["code"] or "", "other")
        d["value_usd"] = _value(d["shares"], d["price"])
        data.append(d)
    return {"ticker": ticker.upper(), "issuer_ciks": ciks, "from": lo, "to": end, "count": len(data), "data": data}


def buying(conn: sqlite3.Connection, days: int, min_value: float, role: str, min_insiders: int,
           limit: int, floor: str | None) -> dict[str, Any]:
    """Companies ranked by open-market insider purchases filed in the last `days`."""
    since = _since(days, floor)
    role_sql = ROLE_FILTERS.get(role, ROLE_FILTERS["officer_or_director"])
    # A filing can list several insiders reporting the SAME trade (e.g. a fund and
    # its general partner), so trade values are summed once per trade and the
    # insiders are counted separately — joining owners to trades would double-count.
    rows = conn.execute(
        f"""WITH buys AS (
                SELECT f.accession, f.issuer_cik, f.issuer_name, f.ticker, t.trade_date,
                       t.shares * t.price AS value
                FROM insider_filings f JOIN insider_trades t USING (accession)
                WHERE f.filing_date >= ? AND t.code = 'P' AND t.derivative = 0
                  AND t.shares > 0 AND t.price > 0
                  AND EXISTS (SELECT 1 FROM insider_owners o WHERE o.accession = f.accession AND {role_sql})
            ),
            people AS (
                SELECT b.issuer_cik, count(DISTINCT o.owner_cik) AS insiders,
                       group_concat(DISTINCT o.owner_name) AS who
                FROM (SELECT DISTINCT accession, issuer_cik FROM buys) b
                JOIN insider_owners o ON o.accession = b.accession AND {role_sql}
                GROUP BY b.issuer_cik
            )
            SELECT b.issuer_cik, max(b.ticker) AS ticker, max(b.issuer_name) AS issuer_name,
                   p.insiders, round(sum(b.value), 2) AS total_value_usd, count(*) AS purchases,
                   min(b.trade_date) AS first_trade, max(b.trade_date) AS last_trade, p.who
            FROM buys b JOIN people p USING (issuer_cik)
            GROUP BY b.issuer_cik
            HAVING sum(b.value) >= ? AND p.insiders >= ?
            ORDER BY p.insiders DESC, total_value_usd DESC LIMIT ?""",
        (since, min_value, min_insiders, limit),
    ).fetchall()
    return {"since": since, "role": role, "min_value_usd": min_value, "count": len(rows), "data": [dict(r) for r in rows]}


def summary(conn: sqlite3.Connection, ticker: str, days: int, floor: str | None) -> dict[str, Any]:
    ciks = issuer_ciks(conn, ticker)
    since = _since(days, floor)
    marks = ",".join("?" * len(ciks))
    base = f"""FROM insider_filings f JOIN insider_trades t USING (accession)
               WHERE f.issuer_cik IN ({marks}) AND f.filing_date >= ? AND t.derivative = 0
                 AND t.code IN ('P', 'S') AND t.price > 0"""
    # Values per trade (no owner join, so joint filings aren't double-counted) ...
    r = conn.execute(
        f"""SELECT round(sum(CASE WHEN t.code = 'P' THEN t.shares * t.price END), 2) AS bought_usd,
                   round(sum(CASE WHEN t.code = 'S' THEN t.shares * t.price END), 2) AS sold_usd,
                   sum(t.code = 'S' AND f.plan_10b5_1 = 1) AS planned_sales,
                   sum(t.code = 'S') AS sales, max(f.issuer_name) AS issuer_name {base}""",
        [*ciks, since],
    ).fetchone()
    # ... and people counted separately.
    people = conn.execute(
        f"""SELECT count(DISTINCT CASE WHEN t.code = 'P' THEN o.owner_cik END) AS buyers,
                   count(DISTINCT CASE WHEN t.code = 'S' THEN o.owner_cik END) AS sellers
            FROM insider_filings f JOIN insider_trades t USING (accession)
            JOIN insider_owners o ON o.accession = f.accession
            WHERE f.issuer_cik IN ({marks}) AND f.filing_date >= ? AND t.derivative = 0
              AND t.code IN ('P', 'S') AND t.price > 0""",
        [*ciks, since],
    ).fetchone()
    bought, sold = r["bought_usd"] or 0.0, r["sold_usd"] or 0.0
    return {
        "ticker": ticker.upper(), "issuer_name": r["issuer_name"], "since": since,
        "open_market_bought_usd": bought, "open_market_sold_usd": sold, "net_usd": round(bought - sold, 2),
        "insiders_buying": people["buyers"], "insiders_selling": people["sellers"],
        "sales_under_10b5_1_plan": r["planned_sales"] or 0, "sales": r["sales"] or 0,
        "note": "Open-market purchases (P) and sales (S) of shares only; grants, tax withholding and "
                "option exercises are excluded. Sales under a 10b5-1 plan were scheduled in advance.",
    }


# ---- Form 144: planned sales ---------------------------------------------------

PLANNED_ROLE_FILTERS = {
    "any": "1=1",
    "officer_or_director": "(n.relationship LIKE '%Officer%' OR n.relationship LIKE '%Director%')",
    "officer": "n.relationship LIKE '%Officer%'",
    "director": "n.relationship LIKE '%Director%'",
    "ten_percent_owner": "(n.relationship LIKE '%10%' OR n.relationship LIKE '%Ten Percent%')",
}

# Display ticker: the SEC's current CIK -> ticker list, else the latest Form 4 ticker.
_TICKER = """coalesce((SELECT ticker FROM cik_tickers WHERE cik = n.issuer_cik),
                      (SELECT ticker FROM insider_filings WHERE issuer_cik = n.issuer_cik AND ticker IS NOT NULL
                       ORDER BY filing_date DESC LIMIT 1))"""


def form144_ciks(conn: sqlite3.Connection, ticker: str) -> list[int]:
    t = ticker.strip().upper()
    ciks = {r[0] for r in conn.execute("SELECT cik FROM cik_tickers WHERE ticker = ?", (t,))}
    ciks |= {r[0] for r in conn.execute("SELECT DISTINCT issuer_cik FROM insider_filings WHERE ticker = ?", (t,))}
    if not ciks:
        raise NotFound(f"unknown US ticker {t}")
    return sorted(ciks)


def _planned_row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["under_10b5_1_plan"] = d["plan_adopted"] is not None
    units, out = d.get("units"), d.pop("units_outstanding", None)
    d["pct_of_outstanding"] = round(units / out, 6) if units and out else None
    return d


_PLANNED_COLS = f"""n.accession, n.filing_date, n.form_type, n.issuer_cik, n.issuer_name, {_TICKER} AS ticker,
                    n.seller, n.relationship, n.security_class, n.units, n.market_value, n.units_outstanding,
                    n.approx_sale_date, n.exchange, n.broker, n.acquired_how, n.plan_adopted, n.sold_past_3m_usd"""


def planned_sales(conn: sqlite3.Connection, ticker: str, start: str | None, end: str | None,
                  limit: int, floor: str | None) -> dict[str, Any]:
    """Form 144 notices for one US ticker, newest first."""
    ciks = form144_ciks(conn, ticker)
    where = [f"n.issuer_cik IN ({','.join('?' * len(ciks))})"]
    args: list[Any] = list(ciks)
    lo = max(filter(None, [start, floor]), default=None)
    if lo:
        where.append("n.filing_date >= ?")
        args.append(lo)
    if end:
        where.append("n.filing_date <= ?")
        args.append(end)
    rows = conn.execute(
        f"SELECT {_PLANNED_COLS} FROM form144_notices n WHERE {' AND '.join(where)} "
        "ORDER BY n.filing_date DESC, n.market_value DESC LIMIT ?", [*args, limit]).fetchall()
    data = [_planned_row(r) for r in rows]
    return {"ticker": ticker.upper(), "issuer_ciks": ciks, "from": lo, "to": end, "count": len(data), "data": data,
            "note": "Form 144 is filed before an insider sells; the sale itself is reported later on Form 4. "
                    "A notice is an intention, not a completed sale."}


def planned_sales_recent(conn: sqlite3.Connection, days: int, min_value: float, role: str,
                         limit: int, floor: str | None) -> dict[str, Any]:
    """Largest planned insider sales (Form 144) filed in the last `days`, across the market."""
    since = _since(days, floor)
    role_sql = PLANNED_ROLE_FILTERS.get(role, PLANNED_ROLE_FILTERS["any"])
    rows = conn.execute(
        f"""SELECT {_PLANNED_COLS} FROM form144_notices n
            WHERE n.filing_date >= ? AND coalesce(n.market_value, 0) >= ? AND {role_sql}
            ORDER BY n.market_value DESC LIMIT ?""", (since, min_value, limit)).fetchall()
    data = [_planned_row(r) for r in rows]
    return {"since": since, "role": role, "min_value_usd": min_value, "count": len(data), "data": data,
            "note": "Form 144 notices are intentions to sell, filed before the sale."}
