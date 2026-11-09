"""Trading schedule of the hedge venue: when entries are allowed, when to flatten, when
resting orders must come off, and how many swap rollovers a holding period crosses.

Pure. Build it from the broker's trading hours with from_trading_hours(), and snapshot it
with to_dict() so a replay sees the schedule that was live at the time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

LONG_CLOSURE = timedelta(hours=6)  # longer than the nightly break: weekend or holiday
WEEKDAYS = {"MONDAY": 0, "TUESDAY": 1, "WEDNESDAY": 2, "THURSDAY": 3, "FRIDAY": 4,
            "SATURDAY": 5, "SUNDAY": 6}

Session = tuple[datetime, datetime]


@dataclass(frozen=True)
class SessionRules:
    nightly_entry_block: timedelta = timedelta(minutes=30)
    long_entry_block: timedelta = timedelta(minutes=120)
    after_open_block: timedelta = timedelta(minutes=15)
    flatten_lead: timedelta = timedelta(minutes=60)
    quote_lead: timedelta = timedelta(minutes=5)


@dataclass
class Schedule:
    sessions: list[Session]  # UTC, merged, sorted
    triple_weekday: int = 2
    tz: str = "America/New_York"
    rules: SessionRules = field(default_factory=SessionRules)

    def _index(self, t: datetime) -> int | None:
        for i, (start, end) in enumerate(self.sessions):
            if start <= t < end:
                return i
        return None

    def _closure_after(self, i: int) -> timedelta:
        if i + 1 >= len(self.sessions):
            return LONG_CLOSURE  # unknown future: treat as long, stay on the safe side
        return self.sessions[i + 1][0] - self.sessions[i][1]

    def is_open(self, t: datetime) -> bool:
        return self._index(t) is not None

    def entries_allowed(self, t: datetime) -> bool:
        i = self._index(t)
        if i is None:
            return False
        start, end = self.sessions[i]
        if t - start < self.rules.after_open_block:
            return False
        long_close = self._closure_after(i) >= LONG_CLOSURE
        lead = self.rules.long_entry_block if long_close else self.rules.nightly_entry_block
        return end - t > lead

    def quoting_allowed(self, t: datetime) -> bool:
        i = self._index(t)
        return i is not None and self.sessions[i][1] - t > self.rules.quote_lead

    def must_flatten(self, t: datetime) -> bool:
        i = self._index(t)
        return (i is not None and self._closure_after(i) >= LONG_CLOSURE
                and self.sessions[i][1] - t <= self.rules.flatten_lead)

    def long_closure_between(self, a: datetime, b: datetime) -> bool:
        return any(a < end <= b and self._closure_after(i) >= LONG_CLOSURE
                   for i, (_, end) in enumerate(self.sessions))

    def rollovers(self, a: datetime, b: datetime) -> int:
        """Swap periods charged between a and b: one per session close, triple on the triple day."""
        zone = ZoneInfo(self.tz)
        return sum(3 if end.astimezone(zone).weekday() == self.triple_weekday else 1
                   for _, end in self.sessions if a < end <= b)

    def to_dict(self) -> dict:
        return {
            "sessions": [[s.isoformat(), e.isoformat()] for s, e in self.sessions],
            "triple_weekday": self.triple_weekday,
            "tz": self.tz,
        }

    @classmethod
    def from_dict(cls, data: dict, rules: SessionRules | None = None) -> Schedule:
        sessions = [(datetime.fromisoformat(s), datetime.fromisoformat(e)) for s, e in data["sessions"]]
        return cls(sessions, data["triple_weekday"], data["tz"], rules or SessionRules())

    @classmethod
    def standard_fx(cls, first: date, last: date, tz: str = "America/New_York") -> Schedule:
        """Sunday 18:02 to Friday 16:57 local, with a daily 16:59-18:02 break. For tests."""
        zone, raw = ZoneInfo(tz), []
        d = first
        while d <= last:
            wd = d.weekday()
            if wd < 4:
                raw.append((datetime.combine(d, time(0, 0), zone), datetime.combine(d, time(16, 59), zone)))
            if wd == 4:
                raw.append((datetime.combine(d, time(0, 0), zone), datetime.combine(d, time(16, 57), zone)))
            if wd in (0, 1, 2, 3, 6):
                raw.append((datetime.combine(d, time(18, 2), zone),
                            datetime.combine(d + timedelta(days=1), time(0, 0), zone)))
            d += timedelta(days=1)
        return cls(merge(raw), tz=tz)


def merge(raw: list[Session]) -> list[Session]:
    """Sort, convert to UTC and join sessions that touch (the broker splits them at midnight)."""
    out: list[Session] = []
    for start, end in sorted((s.astimezone(timezone.utc), e.astimezone(timezone.utc)) for s, e in raw):
        if out and start <= out[-1][1] + timedelta(minutes=1):
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def subtract(sessions: list[Session], window: Session) -> list[Session]:
    ws, we = (t.astimezone(timezone.utc) for t in window)
    out: list[Session] = []
    for start, end in sessions:
        if we <= start or ws >= end:
            out.append((start, end))
            continue
        if ws > start:
            out.append((start, ws))
        if we < end:
            out.append((we, end))
    return out


def from_trading_hours(th, triple_day, first: date, last: date,
                       rules: SessionRules | None = None) -> Schedule:
    """Build from the broker's TradingHours (sessions_on, holidays_on, timezone)."""
    raw, closed = [], []
    d = first
    while d <= last:
        raw.extend((s.start, s.end) for s in th.sessions_on(d))
        closed.extend(h.closed_window(d) for h in th.holidays_on(d))
        d += timedelta(days=1)
    sessions = merge(raw)
    for window in closed:
        sessions = subtract(sessions, window)
    name = getattr(triple_day, "name", str(triple_day)).upper()
    return Schedule(sessions, WEEKDAYS[name], str(th.timezone), rules or SessionRules())
