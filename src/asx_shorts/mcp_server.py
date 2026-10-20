"""MCP server: lets AI assistants (Claude, Cursor, …) query ASX short positions.

Remote:  mounted at /mcp by the API server; send your key as `Authorization: Bearer asxs_…`.
Local:   `uv run asx-shorts-mcp` speaks stdio against your own database (no key needed).
"""
from __future__ import annotations

import contextvars
from contextlib import closing
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import asic, insider_queries, queries, sec_form144, sec_insiders, signup
from .db import connect
from .keys import PLANS, Plan

# Set per request by the API's auth middleware; stdio runs with full access.
current_plan: contextvars.ContextVar[Plan] = contextvars.ContextVar("current_plan", default=PLANS["pro"])

mcp = MCPServer(
    name="tradingdata-ai",
    title="Trading data for AI (ASX short positions, US insider trades)",
    instructions=(
        "Two markets. (1) US insiders: transactions from SEC Forms 3/4/5 since 2006 "
        "(tools prefixed us_insider_): code P = open-market purchase, S = sale; grants (A), "
        "tax withholding (F) and option exercises (M) are not buying or selling decisions. "
        "Planned sales from SEC Form 144 since April 2023 (tools us_planned_sales*) are filed "
        "before an insider sells, so they can show a sale before it happens. "
        "(2) Daily short positions for ASX-listed securities from ASIC, since June 2010. "
        "short_pct is a fraction of shares on issue (0.05 = 5%). Positions are as at the "
        "position date; ASIC publishes them about four trading days later, so the latest "
        "date is never 'today'. Data: " + asic.ATTRIBUTION
    ),
    version="0.1.0",
)


def _wrap(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "attribution": asic.ATTRIBUTION, "licence": asic.LICENCE, "plan": current_plan.get().name}


def _track(tool: str) -> None:
    """Anonymous per-tool counts (the demand report); never let it break a call."""
    try:
        with closing(connect()) as conn:
            signup.record_route(conn, "mcp:" + tool, current_plan.get().name)
    except Exception:
        pass


def _call(tool: str, fn, *args) -> dict[str, Any]:
    _track(tool)
    with closing(connect(readonly=True)) as conn:
        try:
            return _wrap(fn(conn, *args))
        except queries.NotFound as exc:
            return _wrap({"error": "not_found", "message": str(exc)})
        except PermissionError as exc:
            return _wrap({"error": "outside_plan", "message": str(exc)})


@mcp.tool()
def search_products(query: str, limit: int = 20) -> dict[str, Any]:
    """Find ASX codes by code or company name (e.g. 'BHP', 'lithium', 'flight centre')."""
    _track("search_products")
    with closing(connect(readonly=True)) as conn:
        return _wrap({"results": queries.search(conn, query, max(1, min(limit, 50)))})


@mcp.tool()
def short_history(code: str, start: str | None = None, end: str | None = None) -> dict[str, Any]:
    """Daily short position history for one ASX code. Dates are YYYY-MM-DD; omit for all available."""
    return _call("short_history", queries.history, code, start, end, current_plan.get().floor())


@mcp.tool()
def most_shorted(date: str | None = None, limit: int = 20, min_shares_on_issue: int = 0) -> dict[str, Any]:
    """The most heavily shorted ASX securities on a date (default: latest report)."""
    return _call("most_shorted", queries.most_shorted, date, max(1, min(limit, 100)), max(0, min_shares_on_issue), current_plan.get().floor())


@mcp.tool()
def short_changes(reports_back: int = 5, direction: str = "up", limit: int = 20) -> dict[str, Any]:
    """Largest changes in short % (percentage points) between the latest report and N reports earlier.
    direction: 'up' (shorting increased) or 'down' (shorts covered)."""
    if direction not in ("up", "down"):
        direction = "up"
    return _call("short_changes", queries.changes, max(1, min(reports_back, 250)), max(1, min(limit, 100)), direction, current_plan.get().floor())


@mcp.tool()
def data_coverage() -> dict[str, Any]:
    """First and last report dates, number of reports and securities loaded."""
    return _call("data_coverage", queries.coverage)


def _wrap_sec(payload: dict[str, Any], attribution: str = sec_insiders.ATTRIBUTION) -> dict[str, Any]:
    return {**payload, "attribution": attribution, "plan": current_plan.get().name}


def _call_sec(tool: str, fn, *args, attribution: str = sec_insiders.ATTRIBUTION) -> dict[str, Any]:
    _track(tool)
    with closing(connect(readonly=True)) as conn:
        try:
            return _wrap_sec(fn(conn, *args), attribution)
        except queries.NotFound as exc:
            return _wrap_sec({"error": "not_found", "message": str(exc)}, attribution)


@mcp.tool()
def us_insider_trades(ticker: str, start: str | None = None, end: str | None = None,
                      codes: list[str] | None = None, limit: int = 100) -> dict[str, Any]:
    """Insider transactions for one US ticker from SEC Forms 3/4/5, newest first.
    Dates filter the filing date (YYYY-MM-DD). codes e.g. ["P"] for open-market purchases, ["P","S"]."""
    return _call_sec("us_insider_trades", insider_queries.trades, ticker, start, end, codes, max(1, min(limit, 500)), current_plan.get().floor())


@mcp.tool()
def us_insider_buying(days: int = 30, min_value: float = 100_000, role: str = "officer_or_director",
                      min_insiders: int = 1, limit: int = 25) -> dict[str, Any]:
    """US companies ranked by open-market insider purchases filed in the last N days.
    role: any | officer_or_director | officer | director | ten_percent_owner.
    Use min_insiders >= 2 to find 'cluster buying' by several insiders."""
    if role not in insider_queries.ROLE_FILTERS:
        role = "officer_or_director"
    return _call_sec("us_insider_buying", insider_queries.buying, max(1, min(days, 3650)), max(0.0, min_value), role,
                     max(1, min(min_insiders, 50)), max(1, min(limit, 100)), current_plan.get().floor())


@mcp.tool()
def us_insider_summary(ticker: str, days: int = 90) -> dict[str, Any]:
    """Open-market insider buying vs selling (US$) for one US ticker over the last N days."""
    return _call_sec("us_insider_summary", insider_queries.summary, ticker, max(1, min(days, 3650)), current_plan.get().floor())


@mcp.tool()
def us_planned_sales(ticker: str, start: str | None = None, end: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Planned insider sales for one US ticker: SEC Form 144 notices, filed BEFORE an insider sells
    (the completed sale appears later in us_insider_trades as code S). Dates filter the filing date."""
    return _call_sec("us_planned_sales", insider_queries.planned_sales, ticker, start, end, max(1, min(limit, 500)),
                     current_plan.get().floor(), attribution=sec_form144.ATTRIBUTION)


@mcp.tool()
def us_planned_sales_recent(days: int = 7, min_value: float = 1_000_000, role: str = "any", limit: int = 25) -> dict[str, Any]:
    """Largest planned insider sales (SEC Form 144) filed across the US market in the last N days.
    role: any | officer_or_director | officer | director | ten_percent_owner.
    A notice is an intention to sell, not a completed sale."""
    if role not in insider_queries.PLANNED_ROLE_FILTERS:
        role = "any"
    return _call_sec("us_planned_sales_recent", insider_queries.planned_sales_recent, max(1, min(days, 3650)),
                     max(0.0, min_value), role, max(1, min(limit, 100)), current_plan.get().floor(),
                     attribution=sec_form144.ATTRIBUTION)


def main_stdio() -> None:
    mcp.run("stdio")
