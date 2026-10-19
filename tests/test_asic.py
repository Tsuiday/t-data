from pathlib import Path

import pytest

from asx_shorts import asic

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return asic.parse((FIX / f"{name}-001-SSDailyAggShortPos.csv").read_bytes())


def test_parses_pre_2022_utf16_tab_format():
    rows = {p.code: p for p in load("RR20150701")}
    assert set(rows) == {"BHP", "JBH", "FLT", "CBA", "1PG"}
    assert all(code == code.strip() for code in rows)          # padded codes are trimmed
    p = rows["1PG"]
    assert p.short_positions == 410000 and p.shares_on_issue == 77477341
    assert p.short_pct == pytest.approx(410000 / 77477341)


def test_parses_post_2022_utf8_comma_format():
    rows = {p.code: p for p in load("RR20250701")}
    assert set(rows) == {"BHP", "FLT", "PLS", "CBA", "IEL"}
    for p in rows.values():
        assert 0 <= p.short_pct < 1                           # recomputed, never the raw "%" column


def test_percentage_is_recomputed_not_trusted():
    # ASIC's own % column has used both percent and fraction scaling; ours is always a fraction.
    raw = "Product,Product Code,Reported Short Positions,Total Product in Issue,% short\nX LTD,XYZ,50,1000,5.0\n".encode()
    (p,) = asic.parse(raw)
    assert p.short_pct == 0.05


def test_rows_that_do_not_reconcile_are_dropped():
    raw = ("Product,Product Code,Reported Short Positions,Total Product in Issue,%\n"
           "BAD ISSUE,AAA,10,0,0\nMORE THAN ISSUED,BBB,200,100,2\nNOT NUMBER,CCC,abc,100,0\nGOOD,DDD,1,100,0.01\n").encode()
    assert [p.code for p in asic.parse(raw)] == ["DDD"]


def test_rejects_files_that_are_not_reports():
    with pytest.raises(ValueError):
        asic.parse(b"<html>Not found</html>")


def test_empty_200_response_counts_as_not_published(monkeypatch):
    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b""
    monkeypatch.setattr(asic.request, "urlopen", lambda *a, **k: Resp())
    from datetime import date
    with pytest.raises(asic.ReportNotPublished):
        asic.fetch(date(2013, 9, 4))


def test_unreadable_file_does_not_stop_the_run(tmp_path, monkeypatch):
    from datetime import date
    from asx_shorts import db, ingest
    good = (FIX / "RR20250701-001-SSDailyAggShortPos.csv").read_bytes()
    def fake_fetch(d, **k):
        return (b"<html>oops</html>" if d.day == 1 else good), "u"
    monkeypatch.setattr(asic, "fetch", fake_fetch)
    conn = db.connect(tmp_path / "t.sqlite")
    stats = ingest.run(conn, date(2025, 7, 1), date(2025, 7, 2), pause=0, log=lambda *_: None)
    assert stats["unreadable"] == 1 and stats["loaded"] == 1
