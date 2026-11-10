"""Replay recorded quotes through the shared strategy core.

  parity   same rules and settings as the paper run; compare events, trades and counters
  ideal    every entry fills at its order price the moment it is placed, hedge with no
           delay: the best case the capture ratio is measured against
  latency  the paper settings with slower hedges, slower order changes, late hedge
           quotes, per-tick stepping and random delays; only the 0.5 s and 2 s hedge
           cases are judged (docs/criteria.md), the rest are informational

Inputs come from the paper run: data/ticks/<pair>/ (quotes per venue, daily schedule,
seed samples per start) and data/paper/gold_events.csv (start times and settings).
A start without a saved seed falls back to 5-minute history, so its first window_h
hours are reported separately. Funding is not replayed; trades compare before funding.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from arbbot.gap.session import Schedule
from arbbot.gap.strategy import Event, Order, Params, Quote, Strategy, Trade

TICKS = Path("data/ticks")
EVENTS = Path("data/paper/gold_events.csv")
TRADES = Path("data/paper/gold_trades.csv")
STATE = Path("data/paper/gold_state.json")
TIME_TOLERANCE_S = 1.5  # paper events are logged to the second
NET_TOLERANCE = 0.10
FLAT_EVENTS = {"closed", "entry cancelled", "entry missed"}
COMPARED = {"entry order", "entry filled", "entry hedged", "entry cancelled", "entry missed",
            "exit order", "exit filled", "exit cancelled", "closed", "carried over close"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["parity", "ideal", "latency"])
    parser.add_argument("--pair", default="paxg-xauusd")
    parser.add_argument("--step", type=float, default=0.5, help="seconds between steps")
    return parser.parse_args()


# --- inputs ----------------------------------------------------------------------

@dataclass
class Start:
    at: datetime
    params: Params
    seed: list[tuple[datetime, float]] | None


@dataclass
class Tape:
    """Quotes per venue as sorted arrays of receive time (epoch seconds), bid, ask."""
    t: dict[str, np.ndarray]
    bid: dict[str, np.ndarray]
    ask: dict[str, np.ndarray]

    def shifted(self, venue: str, seconds: float) -> Tape:
        t = dict(self.t)
        t[venue] = self.t[venue] + seconds
        return Tape(t, self.bid, self.ask)


def params_from(detail: str) -> Params:
    body = detail[detail.index("Params(") + 7:]
    kw = {k: float(v) for k, v in re.findall(r"(\w+)=([-+\d.e]+)", body)}
    fields = Params.__dataclass_fields__
    return Params(**{k: (int(v) if fields[k].type in ("int", int) else v) for k, v in kw.items() if k in fields})


def load_starts(pair: str) -> list[Start]:
    events = pd.read_csv(EVENTS)
    seeds = {}
    for path in sorted((TICKS / pair).glob("seed-*.json")):
        data = json.loads(path.read_text())
        seeds[datetime.fromisoformat(data["started"])] = [(datetime.fromisoformat(ts), g) for ts, g in data["samples"]]
    starts = []
    for row in events[events.event == "start"].itertuples():
        at = datetime.fromisoformat(row.time)
        seed = next((s for t, s in seeds.items() if abs((t - at).total_seconds()) < 600), None)
        starts.append(Start(at, params_from(row.detail), seed))
    return starts


def load_swaps() -> list[tuple[datetime, float, float]]:
    events = pd.read_csv(EVENTS)
    out = []
    for row in events[events.event == "schedule refreshed"].itertuples():
        kw = dict(re.findall(r"(\w+)=([-+\d.e]+)", row.detail))
        out.append((datetime.fromisoformat(row.time), float(kw["swap_long"]), float(kw["swap_short"])))
    return out


def load_tape(pair: str) -> Tape:
    frames = [pd.read_csv(p) for p in sorted((TICKS / pair).glob("????-??-??.csv"))]
    df = pd.concat(frames)
    df["t"] = pd.to_datetime(df.timestamp, utc=True, format="ISO8601").astype("int64") / 1e9
    df = df.sort_values("t", kind="stable")
    t, bid, ask = {}, {}, {}
    for venue, g in df.groupby("venue"):
        t[venue], bid[venue], ask[venue] = g.t.to_numpy(), g.bid.to_numpy(), g.ask.to_numpy()
    return Tape(t, bid, ask)


def load_schedules(pair: str) -> dict:
    return {p.stem.removeprefix("schedule-"): Schedule.from_dict(json.loads(p.read_text()))
            for p in sorted((TICKS / pair).glob("schedule-*.json"))}


def fallback_seed(at: datetime, window_h: float) -> list[tuple[datetime, float]]:
    from arbbot.gap import history  # network; only when a start has no saved seed
    days = (datetime.now(timezone.utc) - at).total_seconds() / 86_400 + max(window_h / 24 * 2, 3)
    a = history.fetch_closes("hl:PAXG", days, "5m")
    b = history.fetch_closes("ctrader:XAUUSD", days, "5m")
    gap = (pd.concat([a, b], axis=1, join="inner").pipe(lambda d: d.iloc[:, 0] - d.iloc[:, 1]))
    return [(ts.to_pydatetime(), float(g)) for ts, g in gap.items() if ts.to_pydatetime() < at]


# --- strategies for the modes --------------------------------------------------------

class IdealStrategy(Strategy):
    def _fill_price(self, order: Order, coin: Quote, now: datetime) -> float | None:
        if order.purpose == "entry":
            return order.price
        return super()._fill_price(order, coin, now)


@dataclass
class RandomDelayStrategy(Strategy):
    low: float = 0.2
    high: float = 1.0
    rng: random.Random = field(default_factory=lambda: random.Random(1))

    def step(self, now, coin, hedge):
        self.params = replace(self.params, hedge_delay_s=self.rng.uniform(self.low, self.high),
                              order_delay_s=self.rng.uniform(self.low, self.high))
        return super().step(now, coin, hedge)


# --- the replay ----------------------------------------------------------------------

@dataclass
class Run:
    events: list[Event] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    forced_flat: list[datetime] = field(default_factory=list)


def replay(tape: Tape, starts: list[Start], schedules: dict, swaps: list, *, cls=Strategy,
           overrides: dict | None = None, step_s: float = 0.5, per_tick: bool = False,
           resync: list[datetime] | None = None, seeds: dict | None = None) -> Run:
    run = Run()
    end = max(float(a[-1]) for a in tape.t.values())
    strat: Strategy | None = None
    resync = sorted(resync or [])
    days = sorted(schedules)

    for i, st in enumerate(starts):
        seg_end = starts[i + 1].at.timestamp() if i + 1 < len(starts) else end
        params = replace(st.params, **(overrides or {}))
        samples = list(seeds[st.at])
        if strat is None:
            strat = cls(params=params)
        else:  # a restart: resting orders are dropped, the normal re-seeded
            strat.params = params
            if strat.state == "entry_resting":
                strat.state, strat.order = "flat", None
            elif strat.state == "exit_resting":
                strat.state, strat.order = "open", None
        strat.samples = samples

        if per_tick:
            times = np.unique(np.concatenate([a[(a >= st.at.timestamp()) & (a < seg_end)] for a in tape.t.values()]))
        else:
            times = np.arange(st.at.timestamp(), seg_end, step_s)
        day, sw = None, 0
        for ts in times:
            now = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            key = now.strftime("%Y-%m-%d")
            if key != day:
                day = key
                k = bisect_right(days, key) - 1
                strat.schedule = schedules[days[max(k, 0)]]
            while sw < len(swaps) and swaps[sw][0] <= now:
                strat.params = replace(strat.params, swap_long=swaps[sw][1], swap_short=swaps[sw][2])
                sw += 1
            while resync and resync[0] <= now:
                resync.pop(0)
                if strat.state != "flat":
                    run.forced_flat.append(now)
                    strat.state, strat.order, strat.trade, strat.hedge_due = "flat", None, None, None
            quotes = {}
            for v in tape.t:
                j = int(np.searchsorted(tape.t[v], ts, side="right"))
                if j == 0:
                    break
                received = datetime.fromtimestamp(float(tape.t[v][j - 1]), tz=timezone.utc)
                quotes[v] = Quote(float(tape.bid[v][j - 1]), float(tape.ask[v][j - 1]), received)
            if len(quotes) < 2:
                continue
            for ev in strat.step(now, quotes["hyperliquid"], quotes["ctrader"]):
                run.events.append(ev)
                if ev.trade is not None:
                    run.trades.append(ev.trade)
    run.stats = dict(strat.stats) if strat else {}
    return run


# --- comparison ----------------------------------------------------------------------

def match_events(paper: pd.DataFrame, events: list[Event]) -> tuple[int, int, str]:
    """Greedy in-order match on name and time. Returns (matched, total, first divergence)."""
    ours = [(e.at.timestamp(), e.name) for e in events if e.name in COMPARED]
    j, matched, first = 0, 0, ""
    for row in paper.itertuples():
        t = datetime.fromisoformat(row.time).timestamp()
        k = j
        while k < len(ours) and ours[k][0] < t - TIME_TOLERANCE_S:
            k += 1
        hit = next((m for m in range(k, min(k + 5, len(ours)))
                    if ours[m][1] == row.event and abs(ours[m][0] - t) <= TIME_TOLERANCE_S), None)
        if hit is None:
            first = first or f"{row.time} {row.event}: {row.detail}"
            continue
        matched += 1
        j = hit + 1
    return matched, len(paper), first


def match_trades(paper: pd.DataFrame, trades: list[Trade], params: Params) -> tuple[int, int, float, float]:
    left = list(trades)
    matched = 0
    for row in paper.itertuples():
        opened = datetime.fromisoformat(row.opened).timestamp()
        hit = next((t for t in left if t.direction == row.direction
                    and abs(t.opened.timestamp() - opened) <= 2
                    and abs(t.net(params) - float(row.net_before_funding)) <= NET_TOLERANCE), None)
        if hit:
            matched += 1
            left.remove(hit)
    return matched, len(paper), float(paper.net_before_funding.sum()) if len(paper) else 0.0, \
        sum(t.net(params) for t in trades)


def summary(run: Run, params: Params) -> str:
    n = len(run.trades)
    net = sum(t.net(params) for t in run.trades)
    entry = [t.entry_slippage for t in run.trades]
    exits = [t.exit_slippage for t in run.trades if t.exit_reason != "closure"]
    mean = lambda xs: f"{np.mean(xs):+.2f}" if xs else "  n/a"
    return (f"{n:>6} {net:>+9.2f} {net / n if n else 0:>+8.2f} {mean(entry):>8} {mean(exits):>8} "
            f"{run.stats.get('requests', 0):>8} {run.stats.get('volume', 0):>10.0f}")


def main() -> None:
    args = parse_args()
    starts = load_starts(args.pair)
    if not starts:
        raise SystemExit(f"no start events in {EVENTS}")
    tape, schedules, swaps = load_tape(args.pair), load_schedules(args.pair), load_swaps()
    seeds = {st.at: st.seed or fallback_seed(st.at, st.params.window_h) for st in starts}
    params = starts[-1].params
    print(f"{len(starts)} start(s) from {starts[0].at:%Y-%m-%d %H:%M} UTC; "
          f"{sum(len(a) for a in tape.t.values()):,} quotes; seeds saved for "
          f"{sum(st.seed is not None for st in starts)}/{len(starts)} starts\n")

    if args.mode == "parity":
        paper = pd.read_csv(EVENTS)
        paper = paper[paper.event.isin(COMPARED)]
        flat_times = [datetime.fromisoformat(t) + timedelta(seconds=1)
                      for t in paper[paper.event.isin(FLAT_EVENTS)].time]
        run = replay(tape, starts, schedules, swaps, step_s=args.step, resync=flat_times, seeds=seeds)
        warm = [st.at + timedelta(hours=st.params.window_h) for st in starts if st.seed is None]
        settled = paper[[not any(st.at <= datetime.fromisoformat(t) < w for st, w in
                             zip([s for s in starts if s.seed is None], warm)) for t in paper.time]]
        m, total, first = match_events(paper, run.events)
        ms, totals, _ = match_events(settled, run.events)
        trades = pd.read_csv(TRADES) if TRADES.exists() else pd.DataFrame(columns=["opened", "direction", "net_before_funding"])
        tm, tt, paper_net, our_net = match_trades(trades, run.trades, params)
        pct = lambda a, b: f"{a / b:.0%}" if b else "n/a"
        print(f"events matched    {m}/{total} ({pct(m, total)}); after warm-up {ms}/{totals} ({pct(ms, totals)})")
        print(f"trades matched    {tm}/{tt}")
        print(f"net before funding paper {paper_net:+.2f}, replay {our_net:+.2f}")
        print(f"re-synchronised   {len(run.forced_flat)} time(s)")
        print(f"first divergence  {first or 'none'}")
        if STATE.exists():
            live = json.loads(STATE.read_text())["stats"]
            print("\ncounter        paper    replay")
            for k in ("placed", "filled", "missed", "cancelled", "closed", "requests", "volume", "budget_peak"):
                print(f"{k:<12} {live.get(k, 0):>8.0f} {run.stats.get(k, 0):>9.0f}")
        net_ok = paper_net == 0 and our_net == 0 or abs(our_net - paper_net) <= 0.05 * abs(paper_net)
        ok = total == 0 or (m / total >= 0.95 and tm == tt and net_ok)
        print(f"\nparity: {'PASS' if ok else 'FAIL'} (≥ 95% events, all trades, net within 5%)")
        return

    header = f"{'case':<28}{'trades':>6} {'net':>9} {'per tr':>8} {'in slip':>8} {'out slip':>8} {'requests':>8} {'volume':>10}"
    if args.mode == "ideal":
        run = replay(tape, starts, schedules, swaps, cls=IdealStrategy, overrides={"hedge_delay_s": 0.0},
                     step_s=args.step, seeds=seeds)
        print(header)
        print(f"{'ideal':<28}{summary(run, params)}")
        if TRADES.exists():
            paper_net = float(pd.read_csv(TRADES).net_before_funding.sum())
            ideal = sum(t.net(params) for t in run.trades)
            print(f"\ncapture ratio: paper {paper_net:+.2f} / ideal {ideal:+.2f} = "
                  f"{paper_net / ideal:.0%}" if ideal > 0 else "\ncapture ratio: n/a (ideal net ≤ 0)")
        return

    cases = [(f"hedge {d:g}s" + ("  [judged]" if d in (0.5, 2) else ""), {"hedge_delay_s": d}, {})
             for d in (0, 0.25, 0.5, 1, 2, 5)]
    cases += [(f"order change {d:g}s", {"order_delay_s": d}, {}) for d in (0.25, 0.5, 1)]
    cases += [(f"cTrader quotes {ms}ms late", {}, {"shift": ms / 1000}) for ms in (100, 200, 300)]
    cases += [("step every tick", {}, {"per_tick": True})]
    cases += [("random 0.2–1s per action", {}, {"cls": RandomDelayStrategy})]
    print(header)
    for name, overrides, opts in cases:
        t = tape.shifted("ctrader", opts["shift"]) if "shift" in opts else tape
        run = replay(t, starts, schedules, swaps, cls=opts.get("cls", Strategy), overrides=overrides,
                     step_s=args.step, per_tick=opts.get("per_tick", False), seeds=seeds)
        print(f"{name:<28}{summary(run, replace(params, **overrides))}", flush=True)


if __name__ == "__main__":
    main()
