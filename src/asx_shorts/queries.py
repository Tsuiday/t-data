"""Read queries shared by the REST API and the MCP tools.

`floor` is the earliest date the caller's plan may see (None = everything).
Percentages are returned both as a fraction (short_pct) and in percent
(short_pct_display) because people misread one or the other constantly.
"""
from __future__ import annotations

import sqlite3
from typing import Any


class NotFound(Exception):
    pass


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    if "short_pct" in d and d["short_pct"] is not None:
        d["short_pct"] = round(d["short_pct"], 6)
        d["short_pct_display"] = f"{d['short_pct'] * 100:.2f}%"
    return d


def coverage(conn: sqlite3.Connection) -> dict[str, Any]:
    r = conn.execute(
        "SELECT min(position_date) AS first, max(position_date) AS last, count(*) AS reports "
        "FROM reports WHERE status = 'ok'"
    ).fetchone()
    products = conn.execute("SELECT count(*) FROM products").fetchone()[0]
    return {"first_date": r["first"], "last_date": r["last"], "reports": r["reports"], "products": products}


def latest_date(conn: sqlite3.Connection) -> str:
    d = conn.execute("SELECT max(position_date) FROM reports WHERE status = 'ok'").fetchone()[0]
    if not d:
        raise NotFound("no data loaded yet")
    return d


def _resolve_date(conn: sqlite3.Connection, on: str | None, floor: str | None) -> str:
    """Latest report on or before `on` (or the latest overall), respecting the plan floor."""
    if on:
        d = conn.execute(
            "SELECT max(position_date) FROM reports WHERE status = 'ok' AND position_date <= ?", (on,)
        ).fetchone()[0]
    else:
        d = latest_date(conn)
    if not d:
        raise NotFound(f"no report on or before {on}")
    if floor and d < floor:
        raise PermissionError(f"{d} is outside your plan's history window (from {floor})")
    return d


def product(conn: sqlite3.Connection, code: str) -> dict[str, Any]:
    r = conn.execute("SELECT code, name, first_date, last_date FROM products WHERE code = ?", (code.upper(),)).fetchone()
    if not r:
        raise NotFound(f"unknown code {code.upper()}")
    return dict(r)


def search(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[dict[str, Any]]:
    q = query.strip().upper()
    rows = conn.execute(
        """SELECT code, name, first_date, last_date FROM products
           WHERE code = ? OR code LIKE ? OR upper(name) LIKE ?
           ORDER BY (code = ?) DESC, last_date DESC, code LIMIT ?""",
        (q, f"{q}%", f"%{q}%", q, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def history(conn: sqlite3.Connection, code: str, start: str | None, end: str | None, floor: str | None) -> dict[str, Any]:
    meta = product(conn, code)
    lo = max(filter(None, [start, floor]), default=None)
    sql = "SELECT position_date AS date, short_positions, shares_on_issue, short_pct FROM positions WHERE code = ?"
    args: list[Any] = [meta["code"]]
    if lo:
        sql += " AND position_date >= ?"
        args.append(lo)
    if end:
        sql += " AND position_date <= ?"
        args.append(end)
    rows = [_row(r) for r in conn.execute(sql + " ORDER BY position_date", args)]
    return {"product": meta, "from": lo, "to": end, "count": len(rows), "data": rows}


def most_shorted(conn: sqlite3.Connection, on: str | None, limit: int, min_shares_on_issue: int, floor: str | None) -> dict[str, Any]:
    d = _resolve_date(conn, on, floor)
    rows = conn.execute(
        """SELECT p.code, pr.name, p.short_positions, p.shares_on_issue, p.short_pct
           FROM positions p JOIN products pr USING (code)
           WHERE p.position_date = ? AND p.shares_on_issue >= ?
           ORDER BY p.short_pct DESC LIMIT ?""",
        (d, min_shares_on_issue, limit),
    ).fetchall()
    return {"date": d, "count": len(rows), "data": [_row(r) for r in rows]}


def changes(conn: sqlite3.Connection, reports_back: int, limit: int, direction: str, floor: str | None) -> dict[str, Any]:
    """Biggest moves in short % (percentage points) between the latest report and N reports earlier."""
    dates = [r[0] for r in conn.execute(
        "SELECT position_date FROM reports WHERE status = 'ok' ORDER BY position_date DESC LIMIT ?", (reports_back + 1,)
    )]
    if len(dates) <= reports_back:
        raise NotFound("not enough history loaded")
    new, old = dates[0], dates[reports_back]
    if floor and old < floor:
        raise PermissionError(f"{old} is outside your plan's history window (from {floor})")
    order = "DESC" if direction == "up" else "ASC"
    rows = conn.execute(
        f"""SELECT n.code, pr.name, o.short_pct AS short_pct_before, n.short_pct AS short_pct,
                   (n.short_pct - o.short_pct) AS change_pct_points
            FROM positions n
            JOIN positions o ON o.code = n.code AND o.position_date = ?
            JOIN products pr ON pr.code = n.code
            WHERE n.position_date = ?
            ORDER BY change_pct_points {order} LIMIT ?""",
        (old, new, limit),
    ).fetchall()
    data = []
    for r in rows:
        d = _row(r)
        d["short_pct_before"] = round(d["short_pct_before"], 6)
        d["change_pct_points"] = round(d["change_pct_points"] * 100, 3)
        data.append(d)
    return {"from": old, "to": new, "direction": direction, "count": len(data), "data": data}
