"""REST API + remote MCP endpoint.

    uv run asx-shorts-serve --port 8000

Auth: `X-API-Key: asxs_…` (or `Authorization: Bearer asxs_…`). /, /terms, /healthz,
/v1/coverage, POST /v1/keys (self-serve free key) and POST /v1/pro-waitlist are
public. Every data response carries its source's attribution.
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import re
from contextlib import asynccontextmanager, closing
from datetime import date
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from mcp.server.transport_security import TransportSecuritySettings

from . import asic, insider_queries, queries, sec_insiders, signup
from .db import connect
from .keys import PLANS, Plan, count_request, lookup
from .mcp_server import current_plan, mcp
from .scheduler import DailyIngest

# Make the daily-ingest thread's progress visible in `fly logs`.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

CODE = re.compile(r"^[A-Z0-9]{1,6}$")
LANDING = (Path(__file__).parent / "landing.html").read_text(encoding="utf-8")
TERMS = (Path(__file__).parent / "terms.html").read_text(encoding="utf-8")


def _key_from(headers: dict[str, str], query_key: str | None = None) -> str:
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return headers.get("x-api-key", "").strip() or (query_key or "").strip()


def _authorise(key: str, route: str | None = None) -> tuple[Plan, int]:
    """Return (plan, requests used today) or raise 401/429. `route` feeds the anonymous usage counts."""
    with closing(connect()) as conn:
        found = lookup(conn, key)
        if not found:
            raise HTTPException(401, {"error": "invalid_key",
                                      "message": "Send a valid key in X-API-Key. Get a free key at https://tradingdata-ai.fly.dev/"})
        key_hash, plan = found
        used = count_request(conn, key_hash)
        if route:
            signup.record_route(conn, route, plan.name)
    if used > plan.daily_requests:
        raise HTTPException(429, {"error": "daily_limit", "message": f"{plan.name} plan allows {plan.daily_requests} requests per day."})
    return plan, used


def require_key(request: Request, api_key: Annotated[str | None, Query(include_in_schema=False)] = None) -> Plan:
    route = getattr(request.scope.get("route"), "path", request.url.path)
    plan, used = _authorise(_key_from({k.lower(): v for k, v in request.headers.items()}, api_key), route)
    request.state.rate = (plan.daily_requests, max(0, plan.daily_requests - used))
    return plan


def _date(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise HTTPException(400, {"error": "bad_date", "message": f"{name} must be YYYY-MM-DD"})


def _wrap(request: Request, plan: Plan, payload: dict[str, Any], source: str = "asic") -> JSONResponse:
    if source == "sec":
        body = {**payload, "plan": plan.name, "attribution": sec_insiders.ATTRIBUTION}
    else:
        body = {**payload, "plan": plan.name, "attribution": asic.ATTRIBUTION, "licence": asic.LICENCE}
    resp = JSONResponse(body)
    limit, remaining = getattr(request.state, "rate", (0, 0))
    resp.headers["X-RateLimit-Limit"] = str(limit)
    resp.headers["X-RateLimit-Remaining"] = str(remaining)
    return resp


def _run(fn, *args):
    with closing(connect(readonly=True)) as conn:
        try:
            return fn(conn, *args)
        except queries.NotFound as exc:
            raise HTTPException(404, {"error": "not_found", "message": str(exc)})
        except PermissionError as exc:
            raise HTTPException(403, {"error": "outside_plan", "message": str(exc)})


# ---- MCP mounted at /mcp, behind the same keys --------------------------------

class MCPAuth:
    """ASGI wrapper: check the key, apply the plan, then hand over to the MCP app."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        try:
            plan, _ = _authorise(_key_from(headers))
        except HTTPException as exc:
            resp = JSONResponse(exc.detail, status_code=exc.status_code)
            return await resp(scope, receive, send)
        token = current_plan.set(plan)
        try:
            await self.app(scope, receive, send)
        finally:
            current_plan.reset(token)


def allowed_hosts() -> list[str]:
    """Host headers the MCP endpoint accepts (DNS-rebinding protection).
    Set ASX_SHORTS_HOSTS="api.example.com" in production; localhost is always allowed."""
    extra = [h.strip() for h in os.environ.get("ASX_SHORTS_HOSTS", "").split(",") if h.strip()]
    return ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", *extra]


def allowed_origins() -> list[str]:
    return [f"https://{h}" for h in allowed_hosts() if not h.endswith(":*")] + ["http://localhost:*", "http://127.0.0.1:*"]


def create_app() -> FastAPI:
    """Build the app. Each call gets a fresh MCP session manager, which may only run once."""
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=allowed_hosts(), allowed_origins=allowed_origins()),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        daily = DailyIngest()
        daily.start()
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            daily.stop()

    application = FastAPI(
        title="tradingdata-ai",
        version="0.2.0",
        description=("Trading data for AI assistants: daily ASIC short positions for every ASX security since "
                     "June 2010, and US insider trades (SEC Forms 3/4/5) since 2006. " + asic.ATTRIBUTION),
        lifespan=lifespan,
    )
    application.mount("/mcp", MCPAuth(mcp_app))
    application.include_router(router)
    application.add_exception_handler(HTTPException, http_error)
    return application


router = APIRouter()


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def landing() -> str:
    return LANDING


@router.get("/terms", response_class=HTMLResponse, include_in_schema=False)
def terms() -> str:
    return TERMS


def _client_ip(request: Request) -> str:
    # Fly's proxy sets Fly-Client-IP; elsewhere fall back to the socket peer.
    return request.headers.get("fly-client-ip") or (request.client.host if request.client else "unknown")


class KeyRequest(BaseModel):
    accept_terms: bool


class WaitlistRequest(BaseModel):
    email: str = Field(max_length=254)
    use_case: str | None = Field(default=None, max_length=1000)


@router.post("/v1/keys", include_in_schema=False)
def create_key(request: Request, body: KeyRequest):
    """Self-serve free key, shown once."""
    if not body.accept_terms:
        raise HTTPException(400, {"error": "terms", "message": "Please accept the terms to get a key."})
    with closing(connect()) as conn:
        try:
            key = signup.create_free_key(conn, _client_ip(request))
        except signup.SignupLimit as exc:
            raise HTTPException(429, {"error": "signup_limit", "message": str(exc)})
    free = PLANS["free"]
    return {"key": key, "plan": free.name, "requests_per_day": free.daily_requests,
            "history_days": free.history_days, "note": "Shown once. Store it now; it can't be recovered."}


@router.post("/v1/pro-waitlist", include_in_schema=False)
def pro_waitlist(body: WaitlistRequest):
    with closing(connect()) as conn:
        try:
            signup.join_waitlist(conn, body.email, body.use_case)
        except ValueError as exc:
            raise HTTPException(400, {"error": "bad_email", "message": str(exc)})
    return {"ok": True, "message": "Thanks. We'll email you when your Pro beta access is ready."}


@router.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/v1/coverage")
def coverage() -> dict[str, Any]:
    """Public: what's loaded."""
    return {**_run(queries.coverage), "attribution": asic.ATTRIBUTION, "licence": asic.LICENCE}


@router.get("/v1/products")
def products(request: Request, plan: Annotated[Plan, Depends(require_key)],
             q: Annotated[str, Query(min_length=1, max_length=60)], limit: Annotated[int, Query(ge=1, le=50)] = 20):
    """Search securities by code or name."""
    return _wrap(request, plan, {"results": _run(queries.search, q, limit)})


@router.get("/v1/short-positions/{code}")
def short_positions(request: Request, plan: Annotated[Plan, Depends(require_key)], code: str,
                    start: Annotated[str | None, Query(alias="from")] = None,
                    end: Annotated[str | None, Query(alias="to")] = None,
                    format: Annotated[str, Query(pattern="^(json|csv)$")] = "json"):
    """Daily short position history for one code. Free plan: last 90 days."""
    code = code.upper()
    if not CODE.match(code):
        raise HTTPException(400, {"error": "bad_code", "message": "codes are 1-6 letters/digits, e.g. BHP"})
    data = _run(queries.history, code, _date(start, "from"), _date(end, "to"), plan.floor())
    if format == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["# " + asic.ATTRIBUTION + " " + asic.LICENCE])
        w.writerow(["date", "code", "short_positions", "shares_on_issue", "short_pct"])
        for r in data["data"]:
            w.writerow([r["date"], code, r["short_positions"], r["shares_on_issue"], r["short_pct"]])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{code}_short_positions.csv"'})
    return _wrap(request, plan, data)


@router.get("/v1/most-shorted")
def most_shorted(request: Request, plan: Annotated[Plan, Depends(require_key)],
                 on: Annotated[str | None, Query(alias="date")] = None,
                 limit: Annotated[int, Query(ge=1, le=100)] = 20,
                 min_shares_on_issue: Annotated[int, Query(ge=0)] = 0):
    """Most heavily shorted securities on a date (default: latest report)."""
    return _wrap(request, plan, _run(queries.most_shorted, _date(on, "date"), limit, min_shares_on_issue, plan.floor()))


@router.get("/v1/changes")
def changes(request: Request, plan: Annotated[Plan, Depends(require_key)],
            reports_back: Annotated[int, Query(ge=1, le=250)] = 5,
            direction: Annotated[str, Query(pattern="^(up|down)$")] = "up",
            limit: Annotated[int, Query(ge=1, le=100)] = 20):
    """Biggest changes in short % (percentage points) vs N reports earlier."""
    return _wrap(request, plan, _run(queries.changes, reports_back, limit, direction, plan.floor()))


TICKER_US = re.compile(r"^[A-Z0-9.\-]{1,10}$")


@router.get("/v1/us/insiders/buying")
def us_insider_buying(request: Request, plan: Annotated[Plan, Depends(require_key)],
                      days: Annotated[int, Query(ge=1, le=3650)] = 30,
                      min_value: Annotated[float, Query(ge=0)] = 100_000,
                      role: Annotated[str, Query(pattern="^(any|officer_or_director|officer|director|ten_percent_owner)$")] = "officer_or_director",
                      min_insiders: Annotated[int, Query(ge=1, le=50)] = 1,
                      limit: Annotated[int, Query(ge=1, le=200)] = 50):
    """US companies ranked by open-market insider purchases (Form 4, code P) filed in the last N days."""
    return _wrap(request, plan, _run(insider_queries.buying, days, min_value, role, min_insiders, limit, plan.floor()), "sec")


@router.get("/v1/us/insiders/{ticker}/summary")
def us_insider_summary(request: Request, plan: Annotated[Plan, Depends(require_key)], ticker: str,
                       days: Annotated[int, Query(ge=1, le=3650)] = 90):
    """Open-market insider buying vs selling for one US ticker."""
    ticker = ticker.upper()
    if not TICKER_US.match(ticker):
        raise HTTPException(400, {"error": "bad_ticker", "message": "tickers are 1-10 letters, digits, '.' or '-'"})
    return _wrap(request, plan, _run(insider_queries.summary, ticker, days, plan.floor()), "sec")


@router.get("/v1/us/insiders/{ticker}")
def us_insider_trades(request: Request, plan: Annotated[Plan, Depends(require_key)], ticker: str,
                      start: Annotated[str | None, Query(alias="from")] = None,
                      end: Annotated[str | None, Query(alias="to")] = None,
                      codes: Annotated[str | None, Query(pattern="^[A-Za-z](,[A-Za-z])*$")] = None,
                      limit: Annotated[int, Query(ge=1, le=1000)] = 200):
    """Insider transactions (Forms 3/4/5) for one US ticker, newest filings first. codes e.g. P,S."""
    ticker = ticker.upper()
    if not TICKER_US.match(ticker):
        raise HTTPException(400, {"error": "bad_ticker", "message": "tickers are 1-10 letters, digits, '.' or '-'"})
    code_list = codes.split(",") if codes else None
    return _wrap(request, plan, _run(insider_queries.trades, ticker, _date(start, "from"), _date(end, "to"),
                                     code_list, limit, plan.floor()), "sec")


async def http_error(_request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {"error": "error", "message": str(exc.detail)}
    return JSONResponse(detail, status_code=exc.status_code)


app = create_app()


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True)
