import gzip
import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from asx_shorts import db, insider_queries, keys, sec_form144 as f144

FIX = Path(__file__).parent / "fixtures"
APPLE = (FIX / "form144_apple.txt").read_bytes()
PREFIXED = (FIX / "form144_prefixed.txt").read_bytes()
PAST_SALES = (FIX / "form144_past_sales.txt").read_bytes()
RECENT = (date.today() - timedelta(days=3)).isoformat()
OLD = (date.today() - timedelta(days=300)).isoformat()


def test_us_dates():
    assert f144.us_date("10/16/2026") == "2026-10-16"
    assert f144.us_date("2/3/2025") == "2025-02-03"
    assert f144.us_date("13/40/2025") is None and f144.us_date("") is None


def test_parses_a_real_notice():
    n = f144.parse_notice(APPLE, "A1", "2026-10-16")
    assert (n.issuer_cik, n.issuer_name, n.seller, n.relationship) == (320193, "Apple Inc.", "KEVAN PAREKH", "Officer")
    assert (n.units, n.market_value, n.approx_sale_date) == (1392.0, 476064.0, "2026-10-16")
    assert n.acquired_how == "Restricted Stock Units" and n.plan_adopted == "2025-11-21"
    assert n.sold_past_3m_usd is None and n.exchange == "NASDAQ"


def test_parses_namespace_prefixed_xml_and_past_sales():
    n = f144.parse_notice(PREFIXED, "A2", "2026-10-16")
    assert n is not None and n.issuer_name == "CrowdStrike Holdings, Inc." and n.relationship == "Director"
    p = f144.parse_notice(PAST_SALES, "A3", "2026-10-16")
    assert p.sold_past_3m_usd and p.sold_past_3m_usd > 0


def test_no_xml_is_skipped():
    assert f144.parse_notice(b"<SEC-DOCUMENT>paper filing</SEC-DOCUMENT>", "x", "2026-01-01") is None


def test_index_entries_from_daily_and_quarterly_indexes():
    daily = (b"144   Apple Inc.   320193   20261016   edgar/data/320193/0001950047-26-010286.txt\n"
             b"4     Apple Inc.   320193   20261016   edgar/data/320193/0000000000-26-000001.txt\n")
    full = b"144/A   Acme   99   2024-01-02   edgar/data/99/0000000099-24-000002.txt\n"
    assert f144._entries(daily) == [("144", "0001950047-26-010286", "edgar/data/320193/0001950047-26-010286.txt", "2026-10-16")]
    assert f144._entries(full)[0][::3] == ("144/A", "2024-01-02")


def test_quarters_newest_first_range():
    assert f144._quarters("2023q2", "2024q1") == ["2023q2", "2023q3", "2023q4", "2024q1"]


class FakeSEC:
    """Serves a tiny EDGAR: one published day in the current quarter, one notice per quarter."""

    def __init__(self, today):
        self.today, self.fetched = today, []
        q = (today.month - 1) // 3 + 1
        self.listing = f"/daily-index/{today.year}/QTR{q}/index.json"
        self.day = today - timedelta(days=1)

    def __call__(self, url, **kw):
        self.fetched.append(url)
        if url.endswith("company_tickers.json"):
            return json.dumps({"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}).encode()
        if url.endswith(self.listing):
            return json.dumps({"directory": {"item": [{"name": f"form.{self.day:%Y%m%d}.idx"}]}}).encode()
        if "/daily-index/" in url:
            return f"144  Apple  320193  {self.day:%Y%m%d}  edgar/data/320193/DAY-1.txt\n".encode()
        if "/full-index/" in url:
            y, q = url.split("/full-index/")[1].split("/")[:2]
            return gzip.compress(f"144  Apple  320193  {y}-01-05  edgar/data/320193/Q-{y}-{q}.txt\n".encode())
        if url.endswith(".txt"):
            return APPLE
        raise FileNotFoundError(url)


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    path = tmp_path / "t.sqlite"
    monkeypatch.setattr(db, "DEFAULT_PATH", path)
    c = db.connect(path)
    yield c
    c.close()


def test_catch_up_loads_newest_first_and_resumes(conn, monkeypatch):
    today = date(2023, 11, 15)
    sec = FakeSEC(today)
    monkeypatch.setattr(f144, "http_get", sec)
    stats = f144.catch_up(conn, today=today, log=lambda *_: None)
    assert stats["days"] == 1 and stats["quarters"] == 2 and "stopped" not in stats
    loaded = [u.rsplit("/", 1)[-1] for u in sec.fetched if u.endswith(".txt")]
    assert loaded == ["DAY-1.txt", "Q-2023-QTR3.txt", "Q-2023-QTR2.txt"]       # current day, then newest quarter first
    assert conn.execute("SELECT count(*) FROM form144_notices").fetchone()[0] == 3
    sec.fetched.clear()
    f144.catch_up(conn, today=today, log=lambda *_: None)                     # nothing new: no notice re-fetched
    assert not [u for u in sec.fetched if u.endswith(".txt") and "/data/" in u]


def test_catch_up_respects_the_time_budget(conn, monkeypatch):
    today = date(2023, 11, 15)
    monkeypatch.setattr(f144, "http_get", FakeSEC(today))
    stats = f144.catch_up(conn, today=today, budget_seconds=0, log=lambda *_: None)
    assert "time budget" in stats["stopped"]
    assert conn.execute("SELECT count(*) FROM form144_loads WHERE kind = 'quarter'").fetchone()[0] == 0


def _seed(conn):
    with conn:
        conn.execute("INSERT INTO cik_tickers (cik, ticker, name) VALUES (320193, 'AAPL', 'Apple Inc.')")
    f144.store(conn, [f144.parse_notice(APPLE, "NEW", RECENT), f144.parse_notice(APPLE, "OLD", OLD),
                      f144.parse_notice(PREFIXED, "CRWD", RECENT)])


def test_planned_sales_queries(conn):
    _seed(conn)
    out = insider_queries.planned_sales(conn, "aapl", None, None, 10, None)
    assert [r["accession"] for r in out["data"]] == ["NEW", "OLD"]
    row = out["data"][0]
    assert row["ticker"] == "AAPL" and row["under_10b5_1_plan"] and row["pct_of_outstanding"] == round(1392 / 14594180000, 6)
    clipped = insider_queries.planned_sales(conn, "AAPL", None, None, 10, (date.today() - timedelta(days=90)).isoformat())
    assert [r["accession"] for r in clipped["data"]] == ["NEW"]
    top = insider_queries.planned_sales_recent(conn, 7, 1_000_000, "any", 10, None)
    assert [r["issuer_name"] for r in top["data"]] == ["CrowdStrike Holdings, Inc."]      # Apple's $476k is below $1M
    assert insider_queries.planned_sales_recent(conn, 7, 0, "officer", 10, None)["data"][0]["seller"] == "KEVAN PAREKH"
    with pytest.raises(insider_queries.NotFound):
        insider_queries.planned_sales(conn, "ZZZZ", None, None, 10, None)


def test_api_and_mcp(conn, monkeypatch):
    _seed(conn)
    monkeypatch.setenv("ASX_SHORTS_HOSTS", "testserver")
    monkeypatch.setenv("ASX_SHORTS_INGEST", "off")
    free, pro = keys.issue(conn, "f", "free"), keys.issue(conn, "p", "pro")
    from asx_shorts.api import create_app
    with TestClient(create_app()) as c:
        r = c.get("/v1/us/insiders/AAPL/planned-sales", headers={"X-API-Key": pro})
        assert r.status_code == 200 and r.json()["count"] == 2 and "Form 144" in r.json()["attribution"]
        assert c.get("/v1/us/insiders/AAPL/planned-sales", headers={"X-API-Key": free}).json()["count"] == 1
        top = c.get("/v1/us/planned-sales?days=7&min_value=0", headers={"X-API-Key": pro}).json()
        assert top["count"] == 2
        assert c.get("/v1/us/planned-sales?role=bogus", headers={"X-API-Key": pro}).status_code == 422
        m = c.post("/mcp/", headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                                     "Authorization": f"Bearer {pro}"},
                   content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": "us_planned_sales", "arguments": {"ticker": "AAPL"}}}))
        res = m.json()["result"]
        out = res.get("structuredContent") or json.loads(res["content"][0]["text"])
        assert out["count"] == 2 and out["data"][0]["seller"] == "KEVAN PAREKH"
