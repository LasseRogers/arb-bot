"""A deterministic price path that exercises entries, exits, reprices, fast markets and a close."""
import random
from datetime import date, datetime, timedelta, timezone

from arbbot.gap.session import Schedule

SCHEDULE = Schedule.standard_fx(date(2026, 11, 1), date(2026, 11, 30))
START = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def run(strategy_cls, quote_cls, steps: int = 40_000) -> list[tuple]:
    rng = random.Random(7)
    s = strategy_cls(schedule=SCHEDULE)
    s.samples = [(START - timedelta(minutes=5 * i), -10.0) for i in range(72, 0, -1)]
    hedge, gap, out = 4000.0, -10.0, []
    for i in range(steps):
        t = START + timedelta(seconds=i)
        hedge += rng.gauss(0, 0.08) + (rng.random() < 0.0005) * rng.choice((-2.0, 2.0))
        gap += -0.002 * (gap + 10.0) + rng.gauss(0, 0.12)
        spread = 0.07 if rng.random() > 0.002 else 0.45
        coin_mid = round((hedge + gap) * 10) / 10
        coin = quote_cls(coin_mid - 0.05, coin_mid + 0.05, t)
        h = quote_cls(round(hedge - spread / 2, 2), round(hedge + spread / 2, 2), t)
        for e in s.step(t, coin, h):
            out.append((e.at.isoformat(), e.name, e.detail))
    out.append(("stats", repr(sorted(s.stats.items())), ""))
    return out
