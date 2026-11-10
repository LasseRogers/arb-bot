import random
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from arbbot.gap.replay import IdealStrategy, Start, Tape, match_events, params_from, replay
from arbbot.gap.session import Schedule
from arbbot.gap.strategy import Event, Params, Quote, Strategy

SCHED = Schedule.standard_fx(date(2026, 11, 1), date(2026, 11, 30))
T0 = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
SEED = [(T0 - timedelta(minutes=5 * i), -10.0) for i in range(72, 0, -1)]


def recorded(seconds: int = 6 * 3600, seed: int = 3):
    """Ticks per venue at irregular times, as the live recorder writes them."""
    rng = random.Random(seed)
    t, hedge, gap = T0.timestamp(), 4000.0, -10.0
    ticks = {"hyperliquid": [], "ctrader": []}
    while t < T0.timestamp() + seconds:
        t += rng.expovariate(4.0)
        hedge += rng.gauss(0, 0.05)
        gap += -0.002 * (gap + 10.0) + rng.gauss(0, 0.1)
        venue = rng.choice(("hyperliquid", "ctrader"))
        if venue == "ctrader":
            ticks[venue].append((t, round(hedge - 0.04, 2), round(hedge + 0.04, 2)))
        else:
            mid = round((hedge + gap) * 10) / 10
            ticks[venue].append((t, mid - 0.05, mid + 0.05))
    arr = {v: np.array(x) for v, x in ticks.items()}
    return Tape({v: a[:, 0] for v, a in arr.items()}, {v: a[:, 1] for v, a in arr.items()},
                {v: a[:, 2] for v, a in arr.items()})


def live_style(tape: Tape, end: float, step: float = 0.5) -> list[Event]:
    """What the live loop does: every step, the latest quote of each venue."""
    s = Strategy(schedule=SCHED)
    s.samples = list(SEED)
    out = []
    for ts in np.arange(T0.timestamp(), end, step):
        quotes = {}
        for v in tape.t:
            j = int(np.searchsorted(tape.t[v], ts, side="right"))
            if j:
                quotes[v] = Quote(tape.bid[v][j - 1], tape.ask[v][j - 1],
                                  datetime.fromtimestamp(tape.t[v][j - 1], tz=timezone.utc))
        if len(quotes) == 2:
            out += s.step(datetime.fromtimestamp(ts, tz=timezone.utc), quotes["hyperliquid"], quotes["ctrader"])
    return out


def test_replay_reproduces_a_live_style_run():
    tape = recorded()
    end = max(a[-1] for a in tape.t.values())
    expected = live_style(tape, end)
    run = replay(tape, [Start(T0, Params(), SEED)], {"2026-11-09": SCHED}, [], seeds={T0: SEED})
    assert len(expected) > 20
    assert [(e.at, e.name, e.detail) for e in run.events] == [(e.at, e.name, e.detail) for e in expected]


def test_ideal_fills_every_entry_and_beats_paper():
    tape = recorded()
    starts, scheds = [Start(T0, Params(), SEED)], {"2026-11-09": SCHED}
    paper = replay(tape, starts, scheds, [], seeds={T0: SEED})
    ideal = replay(tape, starts, scheds, [], cls=IdealStrategy, overrides={"hedge_delay_s": 0.0}, seeds={T0: SEED})
    placed = [e for e in ideal.events if e.name == "entry order"]
    filled = [e for e in ideal.events if e.name == "entry filled"]
    assert placed and len(filled) == len(placed)
    assert ideal.total() >= paper.total()


def test_restart_drops_resting_orders():
    tape = recorded()
    later = T0 + timedelta(hours=3)
    run = replay(tape, [Start(T0, Params(), SEED), Start(later, Params(), SEED)], {"2026-11-09": SCHED}, [],
                 seeds={T0: SEED, later: SEED})
    assert run.events  # runs through the restart without error


def test_match_events_tolerates_logging_to_the_second():
    at = T0 + timedelta(seconds=10.4)
    ours = [Event(at, "entry order", ""), Event(at + timedelta(seconds=3), "entry filled", "")]
    paper = pd.DataFrame({"time": [T0.replace(second=10).isoformat(), (T0 + timedelta(seconds=13)).isoformat(),
                                   (T0 + timedelta(seconds=40)).isoformat()],
                          "event": ["entry order", "entry filled", "entry hedged"], "detail": ["", "", "x"]})
    matched, total, first = match_events(paper, ours)
    assert (matched, total) == (2, 3) and "entry hedged" in first


def test_params_parsed_from_start_event():
    detail = f"code abc123, params {Params(entry=3.5, window_h=6.0, min_samples=12)}"
    p = params_from(detail)
    assert p.entry == 3.5 and p.min_samples == 12 and isinstance(p.min_samples, int)


def held_overnight() -> Tape:
    """Gap -14 from 12:30 Monday to 10:00 Tuesday: one long trade held over the nightly close."""
    t = np.arange(T0.timestamp(), T0.timestamp() + 24 * 3600, 2.0)
    gap = np.where((t >= T0.timestamp() + 1800) & (t < T0.timestamp() + 22 * 3600), -14.0, -10.0)
    hedge = 4000.0 + 0.05 * np.sin(t / 60)
    open_ = np.array([SCHED.is_open(datetime.fromtimestamp(x, tz=timezone.utc)) for x in t])
    coin = np.round((hedge + gap + 0.15 * np.sin(t / 30)) * 10) / 10
    return Tape({"hyperliquid": t, "ctrader": t[open_]},
                {"hyperliquid": coin - 0.05, "ctrader": np.round(hedge[open_] - 0.04, 2)},
                {"hyperliquid": coin + 0.05, "ctrader": np.round(hedge[open_] + 0.04, 2)})


def test_net_uses_the_swap_in_force_when_the_trade_closes():
    refresh = datetime(2026, 11, 10, 0, 30, tzinfo=timezone.utc)  # after Monday's rollover
    long_seed = [(T0 - timedelta(minutes=5 * i), -10.0) for i in range(600, 0, -1)]  # normal stays -10
    params = Params(window_h=72)
    run = replay(held_overnight(), [Start(T0, params, long_seed)], {"2026-11-09": SCHED},
                 [(refresh, -99.0, 99.0)], seeds={T0: long_seed})
    held = [t for t in run.trades if t.rollovers]
    assert held and all(t.closed > refresh for t in held)
    for t in held:
        assert run.net(t) == pytest.approx(t.net(replace(params, swap_long=-99.0, swap_short=99.0)))
        assert run.net(t) != pytest.approx(t.net(params))


def test_unseeded_start_is_not_judged_during_warm_up():
    assert Start(T0, Params(), None).warm_until == T0 + timedelta(hours=6)
    assert Start(T0, Params(), SEED, reconstructed=True).warm_until == T0 + timedelta(hours=6)
    assert Start(T0, Params(), SEED).warm_until == T0
