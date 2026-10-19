"""MCP server: lets AI assistants (Claude, Cursor, …) query ASX short positions.

Remote:  mounted at /mcp by the API server; send your key as `Authorization: Bearer asxs_…`.
Local:   `uv run asx-shorts-mcp` speaks stdio against your own database (no key needed).
"""
from __future__ import annotations

import contextvars
from contextlib import closing
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import asic, queries
from .db import connect
from .keys import PLANS, Plan

# Set per request by the API's auth middleware; stdio runs with full access.
current_plan: contextvars.ContextVar[Plan] = contextvars.ContextVar("current_plan", default=PLANS["pro"])

mcp = MCPServer(
    name="asx-short-positions",
    title="ASX Short Positions",
    instructions=(
        "Daily short positions for ASX-listed securities from ASIC, since June 2010. "
        "short_pct is a fraction of shares on issue (0.05 = 5%). Positions are as at the "
        "position date; ASIC publishes them about four trading days later, so the latest "
        "date is never 'today'. Data: " + asic.ATTRIBUTION
    ),
    version="0.1.0",
)


def _wrap(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "attribution": asic.ATTRIBUTION, "licence": asic.LICENCE, "plan": current_plan.get().name}


def _call(fn, *args) -> dict[str, Any]:
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
    with closing(connect(readonly=True)) as conn:
        return _wrap({"results": queries.search(conn, query, max(1, min(limit, 50)))})


@mcp.tool()
def short_history(code: str, start: str | None = None, end: str | None = None) -> dict[str, Any]:
    """Daily short position history for one ASX code. Dates are YYYY-MM-DD; omit for all available."""
    return _call(queries.history, code, start, end, current_plan.get().floor())


@mcp.tool()
def most_shorted(date: str | None = None, limit: int = 20, min_shares_on_issue: int = 0) -> dict[str, Any]:
    """The most heavily shorted ASX securities on a date (default: latest report)."""
    return _call(queries.most_shorted, date, max(1, min(limit, 100)), max(0, min_shares_on_issue), current_plan.get().floor())


@mcp.tool()
def short_changes(reports_back: int = 5, direction: str = "up", limit: int = 20) -> dict[str, Any]:
    """Largest changes in short % (percentage points) between the latest report and N reports earlier.
    direction: 'up' (shorting increased) or 'down' (shorts covered)."""
    if direction not in ("up", "down"):
        direction = "up"
    return _call(queries.changes, max(1, min(reports_back, 250)), max(1, min(limit, 100)), direction, current_plan.get().floor())


@mcp.tool()
def data_coverage() -> dict[str, Any]:
    """First and last report dates, number of reports and securities loaded."""
    return _call(queries.coverage)


def main_stdio() -> None:
    mcp.run("stdio")
