from datetime import datetime
from zoneinfo import ZoneInfo

from asx_shorts.scheduler import SYDNEY, next_run

UTC = ZoneInfo("UTC")


def test_runs_later_the_same_weekday_evening():
    now = datetime(2026, 10, 19, 9, 0, tzinfo=SYDNEY)          # Monday morning
    assert next_run(now, 20) == datetime(2026, 10, 19, 20, 0, tzinfo=SYDNEY)


def test_after_the_hour_moves_to_the_next_day():
    now = datetime(2026, 10, 19, 21, 0, tzinfo=SYDNEY)
    assert next_run(now, 20) == datetime(2026, 10, 20, 20, 0, tzinfo=SYDNEY)


def test_skips_weekends():
    friday_night = datetime(2026, 10, 23, 21, 0, tzinfo=SYDNEY)
    assert next_run(friday_night, 20) == datetime(2026, 10, 26, 20, 0, tzinfo=SYDNEY)   # Monday


def test_uses_sydney_time_whatever_the_server_clock_says():
    # 08:00 UTC on Monday 19 Oct = 19:00 AEDT, so the run is one hour away, same day
    now = datetime(2026, 10, 19, 8, 0, tzinfo=UTC)
    assert next_run(now, 20) == datetime(2026, 10, 19, 20, 0, tzinfo=SYDNEY)
