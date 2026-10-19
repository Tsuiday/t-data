import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from asx_shorts import db, ingest, signup

FIX = Path(__file__).parent / "fixtures"
RECENT = date.today() - timedelta(days=10)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    path = tmp_path / "t.sqlite"
    monkeypatch.setattr(db, "DEFAULT_PATH", path)
    monkeypatch.setenv("ASX_SHORTS_HOSTS", "testserver")
    monkeypatch.setenv("ASX_SHORTS_INGEST", "off")
    conn = db.connect(path)
    ingest.store(conn, RECENT, (FIX / "RR20250701-001-SSDailyAggShortPos.csv").read_bytes(), "fixture")
    conn.close()
    from asx_shorts.api import create_app
    with TestClient(create_app()) as c:
        c.db_path = path
        yield c


def _key(client, ip="203.0.113.7"):
    return client.post("/v1/keys", json={"accept_terms": True}, headers={"Fly-Client-IP": ip})


def test_self_serve_key_works_straight_away(client):
    r = _key(client)
    assert r.status_code == 200
    key = r.json()["key"]
    assert key.startswith("asxs_") and r.json()["plan"] == "free"
    assert client.get("/v1/most-shorted", headers={"X-API-Key": key}).status_code == 200


def test_terms_must_be_accepted(client):
    assert client.post("/v1/keys", json={"accept_terms": False}).status_code == 400
    assert client.post("/v1/keys", json={}).status_code == 422


def test_per_ip_and_global_signup_limits(client, monkeypatch):
    for _ in range(signup.PER_IP_DAILY):
        assert _key(client).status_code == 200
    r = _key(client)
    assert r.status_code == 429 and "per day" in r.json()["message"]
    assert _key(client, ip="198.51.100.1").status_code == 200          # other networks unaffected
    monkeypatch.setattr(signup, "GLOBAL_DAILY", 0)
    assert _key(client, ip="192.0.2.99").status_code == 429


def test_ips_are_hashed_and_old_rows_purged(client):
    _key(client, ip="203.0.113.50")
    with db.connect(client.db_path) as conn:
        rows = conn.execute("SELECT day, ip_hash FROM signups").fetchall()
        assert rows and all("203.0.113" not in r["ip_hash"] for r in rows)
        conn.execute("INSERT INTO signups VALUES ('2000-01-01', 'old', 1)")
        conn.commit()
        signup.create_free_key(conn, "203.0.113.51")
        assert conn.execute("SELECT count(*) FROM signups WHERE day = '2000-01-01'").fetchone()[0] == 0


def test_waitlist(client):
    assert client.post("/v1/pro-waitlist", json={"email": "not-an-email"}).status_code == 400
    ok = client.post("/v1/pro-waitlist", json={"email": " Quant@Example.com ", "use_case": "backtests"})
    assert ok.status_code == 200
    again = client.post("/v1/pro-waitlist", json={"email": "quant@example.com"})   # no duplicate, use case kept
    assert again.status_code == 200
    with db.connect(client.db_path) as conn:
        assert [tuple(r) for r in conn.execute("SELECT email, use_case FROM waitlist")] == [("quant@example.com", "backtests")]


def test_usage_is_counted_per_endpoint_and_mcp_tool(client):
    key = _key(client).json()["key"]
    for _ in range(2):
        client.get("/v1/short-positions/BHP", headers={"X-API-Key": key})
    r = client.post("/mcp/", headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                                      "Authorization": f"Bearer {key}"},
                    content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": "most_shorted", "arguments": {}}}))
    assert r.status_code == 200
    with db.connect(client.db_path) as conn:
        s = signup.stats(conn, 7)
    routes = {(route, tier): n for route, tier, n in s["requests_by_route"]}
    assert routes[("/v1/short-positions/{code}", "free")] == 2      # the template, not the raw path
    assert routes[("mcp:most_shorted", "free")] == 1
    assert s["signups_by_day"] == [(date.today().isoformat(), 1)]


def test_terms_page_and_landing_signup(client):
    assert "Not financial advice" in client.get("/terms").text
    page = client.get("/").text
    assert 'id="get-key"' in page and "/v1/pro-waitlist" in page and 'href="/terms"' in page
