import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from asx_shorts import db, ingest, keys

FIX = Path(__file__).parent / "fixtures"
TODAY = date.today()
OLD = TODAY - timedelta(days=200)      # outside the free plan's 90-day window
RECENT = TODAY - timedelta(days=10)
NEWEST = TODAY - timedelta(days=5)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    path = tmp_path / "t.sqlite"
    monkeypatch.setattr(db, "DEFAULT_PATH", path)
    monkeypatch.setenv("ASX_SHORTS_HOSTS", "testserver")
    monkeypatch.setenv("ASX_SHORTS_INGEST", "off")
    conn = db.connect(path)
    old = (FIX / "RR20150701-001-SSDailyAggShortPos.csv").read_bytes()
    new = (FIX / "RR20250701-001-SSDailyAggShortPos.csv").read_bytes()
    ingest.store(conn, OLD, old, "fixture-old")
    ingest.store(conn, RECENT, old, "fixture-recent")
    ingest.store(conn, NEWEST, new, "fixture-newest")
    free, pro = keys.issue(conn, "free user", "free"), keys.issue(conn, "pro user", "pro")
    conn.close()
    from asx_shorts.api import create_app
    with TestClient(create_app()) as c:
        c.free = {"X-API-Key": free}
        c.pro = {"X-API-Key": pro}
        c.pro_bearer = {"Authorization": f"Bearer {pro}"}
        c.free_bearer = {"Authorization": f"Bearer {free}"}
        yield c


def test_coverage_is_public_and_attributed(client):
    r = client.get("/v1/coverage")
    assert r.status_code == 200
    body = r.json()
    assert body["reports"] == 3 and body["last_date"] == NEWEST.isoformat()
    assert "Australian Securities & Investments Commission" in body["attribution"]


def test_data_needs_a_valid_key(client):
    assert client.get("/v1/most-shorted").status_code == 401
    assert client.get("/v1/most-shorted", headers={"X-API-Key": "asxs_nope"}).status_code == 401
    assert client.get("/v1/most-shorted", headers=client.free).status_code == 200


def test_free_plan_history_is_clipped_to_90_days(client):
    free = client.get("/v1/short-positions/BHP", headers=client.free).json()
    pro = client.get("/v1/short-positions/BHP", headers=client.pro).json()
    assert [r["date"] for r in pro["data"]] == [OLD.isoformat(), RECENT.isoformat(), NEWEST.isoformat()]
    assert OLD.isoformat() not in [r["date"] for r in free["data"]]
    assert free["plan"] == "free" and pro["plan"] == "pro"


def test_free_plan_cannot_ask_for_an_old_snapshot(client):
    r = client.get(f"/v1/most-shorted?date={OLD.isoformat()}", headers=client.free)
    assert r.status_code == 403 and r.json()["error"] == "outside_plan"
    assert client.get(f"/v1/most-shorted?date={OLD.isoformat()}", headers=client.pro).status_code == 200


def test_most_shorted_is_sorted_and_explained(client):
    body = client.get("/v1/most-shorted?limit=3", headers=client.pro).json()
    pcts = [r["short_pct"] for r in body["data"]]
    assert pcts == sorted(pcts, reverse=True) and len(pcts) == 3
    assert body["data"][0]["short_pct_display"].endswith("%")
    assert body["date"] == NEWEST.isoformat()


def test_changes_compare_latest_with_n_reports_back(client):
    body = client.get("/v1/changes?reports_back=1&direction=up&limit=10", headers=client.pro).json()
    assert body["from"] == RECENT.isoformat() and body["to"] == NEWEST.isoformat()
    moves = [r["change_pct_points"] for r in body["data"]]
    assert moves == sorted(moves, reverse=True)


def test_csv_export_carries_attribution(client):
    r = client.get("/v1/short-positions/flt?format=csv", headers=client.pro)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    first, header = r.text.splitlines()[:2]
    assert "Australian Securities" in first and header.startswith("date,code")


def test_bad_input_is_rejected_cleanly(client):
    assert client.get("/v1/short-positions/NOT-A-CODE", headers=client.pro).status_code == 400
    assert client.get("/v1/short-positions/ZZZZ", headers=client.pro).status_code == 404
    assert client.get("/v1/most-shorted?date=yesterday", headers=client.pro).status_code == 400
    assert client.get("/v1/most-shorted?limit=1000", headers=client.pro).status_code == 422


def test_daily_limit_returns_429(client, monkeypatch):
    monkeypatch.setitem(keys.PLANS, "free", keys.Plan("free", daily_requests=2, history_days=90))
    assert client.get("/v1/most-shorted", headers=client.free).status_code == 200
    assert client.get("/v1/most-shorted", headers=client.free).status_code == 200
    r = client.get("/v1/most-shorted", headers=client.free)
    assert r.status_code == 429 and r.json()["error"] == "daily_limit"


# ---- MCP over HTTP ----------------------------------------------------------

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(client, headers, method, params, id_=1):
    r = client.post("/mcp/", headers={**MCP_HEADERS, **headers},
                    content=json.dumps({"jsonrpc": "2.0", "id": id_, "method": method, "params": params}))
    return r


def call_tool(client, headers, name, args):
    r = rpc(client, headers, "tools/call", {"name": name, "arguments": args}, 2)
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


def test_mcp_requires_a_key(client):
    r = rpc(client, {}, "tools/list", {})
    assert r.status_code == 401


def test_mcp_lists_tools(client):
    init = rpc(client, client.pro_bearer, "initialize",
               {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
    assert init.status_code == 200, init.text
    r = rpc(client, client.pro_bearer, "tools/list", {}, 2)
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert {"search_products", "short_history", "most_shorted", "short_changes", "data_coverage"} <= names


def test_mcp_tools_return_data_with_attribution(client):
    out = call_tool(client, client.pro_bearer, "most_shorted", {"limit": 2})
    assert len(out["data"]) == 2 and "Australian Securities" in out["attribution"]
    found = call_tool(client, client.pro_bearer, "search_products", {"query": "flt"})
    assert found["results"][0]["code"] == "FLT"


def test_mcp_applies_the_callers_plan(client):
    free = call_tool(client, client.free_bearer, "short_history", {"code": "BHP"})
    pro = call_tool(client, client.pro_bearer, "short_history", {"code": "BHP"})
    assert free["plan"] == "free" and len(free["data"]) < len(pro["data"])


def test_mcp_rejects_unknown_host_header(client):
    r = client.post("/mcp/", headers={**MCP_HEADERS, **client.pro_bearer, "Host": "evil.example"},
                    content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}))
    assert r.status_code == 421
