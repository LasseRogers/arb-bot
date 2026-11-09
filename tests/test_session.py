from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from arbbot.gap.session import Schedule, from_trading_hours, merge, subtract

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
SCHED = Schedule.standard_fx(date(2026, 11, 1), date(2026, 11, 30))


def ny(day: int, h: int, m: int = 0) -> datetime:
    return datetime(2026, 11, day, h, m, tzinfo=NY)


def test_midnight_split_is_merged():
    # Monday 9 Nov: one session from Sunday 18:02 to Monday 16:59.
    assert (ny(8, 18, 2), ny(9, 16, 59)) in SCHED.sessions


@pytest.mark.parametrize("when, allowed", [
    (ny(9, 10), True),
    (ny(9, 16, 30), False),   # inside the 30 min nightly block
    (ny(9, 16, 28), True),
    (ny(9, 17, 30), False),   # nightly break
    (ny(9, 18, 10), False),   # first 15 min after reopen
    (ny(9, 18, 20), True),
    (ny(13, 14, 56), True),   # Friday, just over 120 min before the weekend
    (ny(13, 15, 0), False),
    (ny(14, 12), False),      # Saturday
])
def test_entries_allowed(when, allowed):
    assert SCHED.entries_allowed(when) is allowed


def test_quoting_stops_shortly_before_any_close():
    assert SCHED.quoting_allowed(ny(9, 16, 53))
    assert not SCHED.quoting_allowed(ny(9, 16, 55))


def test_flatten_only_before_long_closures():
    assert not SCHED.must_flatten(ny(9, 16, 30))  # nightly break
    assert not SCHED.must_flatten(ny(13, 15, 56))
    assert SCHED.must_flatten(ny(13, 15, 57))     # 60 min before Friday close


@pytest.mark.parametrize("start, end, n", [
    (ny(9, 10), ny(10, 10), 1),   # Monday close
    (ny(10, 10), ny(12, 10), 4),  # Tuesday + Wednesday triple
    (ny(13, 10), ny(16, 10), 1),  # Friday close only
    (ny(9, 10), ny(9, 16), 0),
])
def test_rollovers(start, end, n):
    assert SCHED.rollovers(start, end) == n


def test_rollovers_follow_new_york_across_dst():
    # 1 Nov is the US switch to winter time; the close is still 16:59 New York.
    s = Schedule.standard_fx(date(2026, 10, 25), date(2026, 11, 7))
    closes = [e.astimezone(NY) for _, e in s.sessions if e.astimezone(NY).weekday() < 5]
    assert {(c.hour, c.minute) for c in closes} <= {(16, 59), (16, 57)}
    assert {c.utcoffset() for c in closes} == {timedelta(hours=-4), timedelta(hours=-5)}


def test_long_closure_between():
    assert SCHED.long_closure_between(ny(13, 16), ny(15, 19))
    assert not SCHED.long_closure_between(ny(9, 16), ny(9, 19))


def test_unknown_future_counts_as_long_closure():
    short = Schedule.standard_fx(date(2026, 11, 9), date(2026, 11, 10))
    assert short.must_flatten(short.sessions[-1][1] - timedelta(minutes=30))


def test_holiday_early_close_cut_out():
    sessions = merge([(ny(25, 18, 2), ny(26, 16, 59)), (ny(26, 18, 2), ny(27, 16, 59))])
    cut = subtract(sessions, (ny(26, 14, 30), ny(26, 18, 2)))
    assert cut[0] == (ny(25, 18, 2).astimezone(UTC), ny(26, 14, 30).astimezone(UTC))


def test_from_trading_hours():
    sessions = {d: [SimpleNamespace(start=datetime.combine(d, datetime.min.time(), NY),
                                    end=datetime.combine(d, datetime.min.time(), NY) + timedelta(hours=16, minutes=59)),
                    SimpleNamespace(start=datetime.combine(d, datetime.min.time(), NY) + timedelta(hours=18, minutes=2),
                                    end=datetime.combine(d + timedelta(days=1), datetime.min.time(), NY))]
                for d in (date(2026, 11, 25), date(2026, 11, 26))}
    holiday = SimpleNamespace(closed_window=lambda d: (ny(26, 14, 30), ny(26, 18, 2)))
    th = SimpleNamespace(
        timezone=NY,
        sessions_on=lambda d: sessions.get(d, []),
        holidays_on=lambda d: [holiday] if d == date(2026, 11, 26) else [],
    )
    s = from_trading_hours(th, SimpleNamespace(name="WEDNESDAY"), date(2026, 11, 25), date(2026, 11, 26))
    assert s.triple_weekday == 2 and s.tz == "America/New_York"
    assert not s.is_open(ny(26, 15))
    assert s.is_open(ny(26, 14))


def test_dict_round_trip():
    assert Schedule.from_dict(SCHED.to_dict()).sessions == SCHED.sessions


def test_holiday_split_does_not_add_a_rollover():
    s = Schedule(subtract(SCHED.sessions, (ny(10, 12), ny(10, 13))))
    assert s.rollovers(ny(10, 10), ny(11, 10)) == 1
