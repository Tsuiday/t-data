import io
import json
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from asx_shorts import db, insider_queries, keys, sec_insiders as si

FIX = Path(__file__).parent / "fixtures"
XML = (FIX / "form4_joint_purchase.xml").read_bytes()
ACC = (FIX / "form4_joint_purchase.meta").read_text().split()[0]
# Real filing: one purchase of 34,315,465 shares at $0.08 reported jointly by four insiders.
ONE_PURCHASE = round(34_315_465 * 0.08, 2)
RECENT = (date.today() - timedelta(days=10)).isoformat()     # inside the free plan's 90 days
OLD = (date.today() - timedelta(days=400)).isoformat()


def test_date_and_ticker_normalisation():
    assert si.iso("01-APR-2024") == "2024-04-01"
    assert si.iso("2024-04-01-05:00") == "2024-04-01"
    assert si.iso("garbage") is None
    assert si.ticker_of(" aapl ") == "AAPL"
    assert si.ticker_of("BRK.B") == "BRK.B"
    assert si.ticker_of("NONE") is None and si.ticker_of("N/A") is None
    assert si.ticker_of("GOOG, GOOGL") == "GOOG"


def test_numbers_are_rounded_like_the_sec_data_sets():
    assert si.num("150.0874") == 150.09
    assert si.num("1,234.5") == 1234.5
    assert si.num("") is None and si.num("n/a") is None


def test_parses_a_real_joint_form4():
    f = si.parse_ownership_xml(XML, ACC, RECENT)
    assert f.form_type == "4" and f.ticker == "IBATF"
    assert len(f.owners) == 4
    rels = {name: rel for _, name, rel, _ in f.owners}
    assert rels["WARNOCK JACOB AARON"] == "Director,TenPercentOwner"
    shares = [t for t in f.trades if t[0] == 0]
    assert shares == [(0, 1, "Common Stock, no par value", "2026-04-29", "P", 34315465.0, 0.08, "A", 143100443.0, "I", None, None)]


def test_daily_index_dedupes_and_keeps_only_forms_345():
    idx = (
        "Form Type   Company Name   CIK   Date Filed  File Name\n"
        "------------------------------------------------\n"
        "4                Acme Corp                         1111      20261016    edgar/data/1111/0000000001-26-000001.txt\n"
        "4                Jane Insider                      2222      20261016    edgar/data/2222/0000000001-26-000001.txt\n"
        "4/A              Acme Corp                         1111      20261016    edgar/data/1111/0000000001-26-000002.txt\n"
        "8-K              Acme Corp                         1111      20261016    edgar/data/1111/0000000001-26-000003.txt\n"
        "SC 13G           Acme Corp                         1111      20261016    edgar/data/1111/0000000001-26-000004.txt\n"
    ).encode()
    assert [(f, a) for f, a, _ in si.daily_index_entries(idx)] == [("4", "0000000001-26-000001"), ("4/A", "0000000001-26-000002")]


def _tsv(header, rows):
    return "\t".join(header) + "\n" + "".join("\t".join(r) + "\n" for r in rows)


def test_bulk_tables_keep_filing_order_via_surrogate_keys():
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("SUBMISSION.tsv", _tsv(
            ["ACCESSION_NUMBER", "FILING_DATE", "PERIOD_OF_REPORT", "DOCUMENT_TYPE", "ISSUERCIK", "ISSUERNAME", "ISSUERTRADINGSYMBOL", "AFF10B5ONE"],
            [["A-1", "02-JAN-2025", "31-DEC-2024", "4", "99", "ACME INC", "acme", "1"]]))
        zf.writestr("REPORTINGOWNER.tsv", _tsv(
            ["ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNERNAME", "RPTOWNER_RELATIONSHIP", "RPTOWNER_TITLE"],
            [["A-1", "7", "DOE JANE", "Officer", "CFO"]]))
        trans_cols = ["ACCESSION_NUMBER", "NONDERIV_TRANS_SK", "SECURITY_TITLE", "TRANS_DATE", "TRANS_CODE", "TRANS_SHARES",
                      "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD", "SHRS_OWND_FOLWNG_TRANS", "DIRECT_INDIRECT_OWNERSHIP"]
        zf.writestr("NONDERIV_TRANS.tsv", _tsv(trans_cols, [
            ["A-1", "52", "Common", "31-DEC-2024", "S", "10", "5.00", "D", "90", "D"],   # second in the filing
            ["A-1", "51", "Common", "30-DEC-2024", "P", "100", "4.00", "A", "100", "D"], # first in the filing
        ]))
        zf.writestr("DERIV_TRANS.tsv", _tsv(["ACCESSION_NUMBER", "DERIV_TRANS_SK"], []))
    (f,) = si.parse_bulk(z.getvalue())
    assert f.filing_date == "2025-01-02" and f.ticker == "ACME" and f.plan_10b5_1
    assert [(t[1], t[4]) for t in f.trades] == [(1, "P"), (2, "S")]
    assert f.owners == [(7, "DOE JANE", "Officer", "CFO")]


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    path = tmp_path / "t.sqlite"
    monkeypatch.setattr(db, "DEFAULT_PATH", path)
    c = db.connect(path)
    joint = si.parse_ownership_xml(XML, ACC, RECENT)
    old = si.parse_ownership_xml(XML, "OLD-ACC", OLD)                # same trade, filed long ago
    si.store(c, [joint, old], "daily")
    yield c
    c.close()


def test_joint_filings_are_not_double_counted(conn):
    out = insider_queries.buying(conn, 30, 0, "any", 1, 10, None)
    (row,) = out["data"]
    assert row["total_value_usd"] == ONE_PURCHASE           # not 4x for four owners
    assert row["insiders"] == 4 and row["purchases"] == 1


def test_role_filter_counts_only_matching_insiders(conn):
    (row,) = insider_queries.buying(conn, 30, 0, "officer_or_director", 1, 10, None)["data"]
    assert row["insiders"] == 1 and row["total_value_usd"] == ONE_PURCHASE
    assert insider_queries.buying(conn, 30, 0, "officer", 1, 10, None)["data"] == []


def test_cluster_threshold(conn):
    assert insider_queries.buying(conn, 30, 0, "any", 5, 10, None)["data"] == []


def test_summary_counts_value_once(conn):
    s = insider_queries.summary(conn, "IBATF", 30, None)
    assert s["open_market_bought_usd"] == ONE_PURCHASE and s["open_market_sold_usd"] == 0
    assert s["insiders_buying"] == 4


def test_trades_explain_codes_and_respect_the_plan_floor(conn):
    full = insider_queries.trades(conn, "ibatf", None, None, None, 100, None)
    assert {r["accession"] for r in full["data"]} == {ACC, "OLD-ACC"}
    assert full["data"][0]["code_meaning"] == "open-market or private purchase"
    floor = (date.today() - timedelta(days=90)).isoformat()
    clipped = insider_queries.trades(conn, "IBATF", None, None, None, 100, floor)
    assert {r["accession"] for r in clipped["data"]} == {ACC}


def test_unknown_ticker_is_not_found(conn):
    with pytest.raises(Exception):
        insider_queries.trades(conn, "ZZZZZZ", None, None, None, 10, None)


# ---- through the API and MCP ---------------------------------------------------

@pytest.fixture()
def client(conn, monkeypatch):
    monkeypatch.setenv("ASX_SHORTS_HOSTS", "testserver")
    monkeypatch.setenv("ASX_SHORTS_INGEST", "off")
    free, pro = keys.issue(conn, "free", "free"), keys.issue(conn, "pro", "pro")
    from asx_shorts.api import create_app
    with TestClient(create_app()) as c:
        c.free, c.pro = {"X-API-Key": free}, {"X-API-Key": pro}
        c.pro_bearer = {"Authorization": f"Bearer {pro}"}
        yield c


def test_rest_endpoints_carry_sec_attribution(client):
    r = client.get("/v1/us/insiders/buying?days=30&min_value=0&role=any", headers=client.pro)
    assert r.status_code == 200
    body = r.json()
    assert body["data"][0]["ticker"] == "IBATF" and "Securities and Exchange Commission" in body["attribution"]
    assert "licence" not in body                            # ASIC's licence line doesn't apply to SEC data
    s = client.get("/v1/us/insiders/IBATF/summary?days=30", headers=client.pro).json()
    assert s["open_market_bought_usd"] == ONE_PURCHASE
    t = client.get("/v1/us/insiders/IBATF?codes=P", headers=client.pro).json()
    assert t["count"] >= 1 and all(r["code"] == "P" for r in t["data"])


def test_rest_rejects_bad_input_and_needs_a_key(client):
    assert client.get("/v1/us/insiders/buying").status_code == 401
    assert client.get("/v1/us/insiders/NOT A TICKER!", headers=client.pro).status_code in (400, 404)
    assert client.get("/v1/us/insiders/IBATF?codes=P;DROP", headers=client.pro).status_code == 422
    assert client.get("/v1/us/insiders/ZZZZZZ", headers=client.pro).status_code == 404


def test_free_plan_sees_only_90_days(client):
    free = client.get("/v1/us/insiders/IBATF", headers=client.free).json()
    pro = client.get("/v1/us/insiders/IBATF", headers=client.pro).json()
    assert len(free["data"]) < len(pro["data"])


def test_mcp_insider_tool(client):
    r = client.post("/mcp/", headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                                      **client.pro_bearer},
                    content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": "us_insider_buying", "arguments": {"days": 30, "min_value": 0, "role": "any"}}}))
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    out = res.get("structuredContent") or json.loads(res["content"][0]["text"])
    assert out["data"][0]["total_value_usd"] == ONE_PURCHASE


def test_catch_up_stops_on_a_block_and_keeps_progress(tmp_path, monkeypatch):
    c = db.connect(tmp_path / "c.sqlite")
    c.execute("INSERT INTO insider_loads (kind, period, filings) VALUES ('quarter', '2026q3', 0)")
    listing = json.dumps({"directory": {"item": [{"name": f"form.202610{d:02d}.idx"} for d in (1, 2, 5, 6, 7, 8, 9)]}})
    monkeypatch.setattr(si, "http_get", lambda url, **kw: listing.encode() if url.endswith("index.json") else b"")
    monkeypatch.setattr(si, "quarter_links", lambda html: {"2026q3": "x"})
    loaded = []

    def fake_day(conn, d, log=print):
        if d == date(2026, 10, 7):
            raise RuntimeError("HTTP Error 403: Forbidden")
        loaded.append(d)
        return 1

    monkeypatch.setattr(si, "load_day", fake_day)
    stats = si.catch_up(c, today=date(2026, 10, 9), log=lambda *_: None)
    assert loaded == [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 6)]
    assert stats["stopped_at"] == "2026-10-07" and stats["days"] == 4


def test_catch_up_skips_days_the_sec_did_not_publish(tmp_path, monkeypatch):
    c = db.connect(tmp_path / "h.sqlite")
    c.execute("INSERT INTO insider_loads (kind, period, filings) VALUES ('quarter', '2026q3', 0)")
    # 12 Oct 2026 (Columbus Day) has no index; asking for it would get a 403.
    listing = {"directory": {"item": [{"name": "form.20261009.idx"}, {"name": "form.20261013.idx"},
                                      {"name": "company.20261013.idx"}, {"name": "sitemap.quarterlyindex1.xml"}]}}
    monkeypatch.setattr(si, "http_get", lambda url, **kw: json.dumps(listing).encode() if url.endswith("index.json") else b"")
    monkeypatch.setattr(si, "quarter_links", lambda html: {"2026q3": "x"})
    loaded = []
    monkeypatch.setattr(si, "load_day", lambda conn, d, log=print: loaded.append(d) or 1)
    stats = si.catch_up(c, today=date(2026, 10, 13), log=lambda *_: None)
    assert loaded == [date(2026, 10, 9), date(2026, 10, 13)] and "stopped_at" not in stats
