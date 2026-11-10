"""Deterministic price paths for the golden test, written against the pinned API only
(Strategy(schedule=), .samples, .step, .stats, to_dict/from_dict), so they can be run
at the pinned commit to produce the golden files: python tests/make_golden.py.
"""
import random
from datetime import date, datetime, timedelta, timezone

from arbbot.gap.session import Schedule

SCHEDULE = Schedule.standard_fx(date(2026, 11, 1), date(2026, 11, 30))
FRI_15 = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)
SUN_REOPEN = datetime(2026, 11, 15, 23, 5, tzinfo=timezone.utc)
PATHS = {
    # Monday 12:00 UTC for 11 h: entries, exits, reprices, fast markets, the nightly break.
    "weekday": dict(start=datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc), steps=40_000, seed=7),
    # Friday: a gap jump at 19:43 opens a trade that is still open at 20:57, so it is closed
    # at the quote before the weekend. Stale hedge quotes pull its exit; a restart from saved
    # state drops a resting order. Then Sunday's reopen.
    "weekend": dict(start=FRI_15, steps=26_000, seed=11, shock=(17_000, -5.0), hold=(17_000, 24_000),
                    stale=(17_400, 17_440), restart=17_100, jump_to=(24_000, SUN_REOPEN)),
    # Friday again, but cTrader goes quiet from 20:50: no fresh hedge quote, so no flatten; the
    # trade is carried over the weekend and flagged at the reopen.
    "outage": dict(start=FRI_15, steps=26_000, seed=11, shock=(17_000, -5.0), hold=(17_000, 24_000),
                   stale=(21_000, 24_000), jump_to=(24_000, SUN_REOPEN)),
    # Monday: a gap jump into a market that then stands still, so the entry times out.
    "missed": dict(start=datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc), steps=3_000, seed=5,
                   shock=(100, -5.0), freeze=(100, 500)),
}


def run(strategy_cls, quote_cls, path: str = "weekday") -> list[tuple]:
    cfg = PATHS[path]
    rng = random.Random(cfg["seed"])
    start = cfg["start"]
    s = strategy_cls(schedule=SCHEDULE)
    s.samples = [(start - timedelta(minutes=5 * i), -10.0) for i in range(72, 0, -1)]
    hedge, gap, out = 4000.0, -10.0, []
    hedge_at = start
    jump_step, jump_time = cfg.get("jump_to", (None, None))
    for i in range(cfg["steps"]):
        t = start + timedelta(seconds=i)
        if jump_step is not None and i >= jump_step:
            t = jump_time + timedelta(seconds=i - jump_step)
            s.samples = [(t - timedelta(minutes=5 * k), -10.0) for k in range(72, 0, -1)] if i == jump_step else s.samples
        if i == cfg.get("restart"):
            restored = strategy_cls.from_dict(s.to_dict(), s.params, SCHEDULE)
            if restored.state == "entry_resting":
                restored.state, restored.order = "flat", None
            elif restored.state == "exit_resting":
                restored.state, restored.order = "open", None
            restored.samples = s.samples
            s = restored
        frozen = cfg.get("freeze", (0, 0))[0] < i < cfg.get("freeze", (0, 0))[1]
        if frozen:
            rng.random()  # keep the random stream aligned with unfrozen steps
        else:
            hedge += rng.gauss(0, 0.08) + (rng.random() < 0.0005) * rng.choice((-2.0, 2.0))
        if i == cfg.get("shock", (None, 0))[0]:
            gap += cfg["shock"][1]
        held = cfg.get("hold", (0, 0))[0] <= i < cfg.get("hold", (0, 0))[1]
        if not frozen:
            gap += (0 if held else -0.002 * (gap + 10.0)) + rng.gauss(0, 0.03 if held else 0.15)
        spread = 0.07 if frozen or rng.random() > 0.002 else 0.45
        lo, hi = cfg.get("stale", (0, 0))
        open_ = SCHEDULE.is_open(t)
        if open_ and not lo <= i < hi:
            hedge_at = t
        coin_mid = round((hedge + gap) * 10) / 10
        coin = quote_cls(coin_mid - 0.05, coin_mid + 0.05, t)
        h = quote_cls(round(hedge - spread / 2, 2), round(hedge + spread / 2, 2), hedge_at)
        for e in s.step(t, coin, h):
            out.append((e.at.isoformat(), e.name, e.detail))
    out.append(("stats", repr(sorted(s.stats.items())), ""))
    return out
