"""Walk-forward test of the funding carry: pick coins from past data only, measure what
they paid afterwards. Settings are frozen in docs/criteria.md ("Carry walk-forward scan").

Each 8-week window is a fresh run with two picks, at its start and after 4 weeks. A pick
sees only the 28 days before it. Per position, on 4-hour bars:

  - rebalance (taker, both legs) whenever the high reaches the anchor x (1 + D/2):
    sell spot, buy back the same perp, cash tops margin back up to m x notional
  - liquidation if the high still takes margin below maintenance
  - funding for the bar's hours, paid on the bar close
Spot and perp share the perp price, so hedge P&L is zero; fees, rebalances and
liquidations are what separate net from funding.

Data is cached under data/scan/. Limits worth knowing:
  - max leverage is today's, not the historical one
  - delisted perps are included when Hyperliquid still lists them; delisted spot pairs aren't
  - 4-hour highs throughout, also where hourly candles exist: conservative for liquidation
  - auto-deleveraging is not modelled; windows with a BTC day of -15% or worse are flagged
"""

from __future__ import annotations

import argparse
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from arbbot.fees import HL_PERP_TAKER, HL_SPOT_TAKER
from arbbot.util import slug
from arbbot.venues import hyperliquid

HOURS_PER_YEAR = 24 * 365
CACHE = Path("data/scan")
TAKER = HL_SPOT_TAKER + HL_PERP_TAKER
WINDOW = pd.Timedelta(weeks=8)
HALF = pd.Timedelta(weeks=4)
CRASH_DAY = -0.15


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--windows", type=int, default=9)
    parser.add_argument("--notional", type=float, default=1000, help="USD of spot across all slots")
    parser.add_argument("--top", type=int, default=2)
    parser.add_argument("--lookback-days", type=int, default=28)
    parser.add_argument("--min-positive", type=float, default=0.70)
    parser.add_argument("--min-perp-volume", type=float, default=10e6)
    parser.add_argument("--min-spot-volume", type=float, default=1e6)
    parser.add_argument("--switch-margin", type=float, default=0.05, help="score edge a challenger needs")
    parser.add_argument("--floor", type=float, default=0.10, help="best score below this: hold cash")
    parser.add_argument("--max-margin", type=float, default=0.50)
    parser.add_argument("--refresh", action="store_true", help="re-download cached data")
    return parser.parse_args()


# --- data ----------------------------------------------------------------------

def _retry(fn, *a):
    for attempt in range(6):
        try:
            return fn(*a)
        except Exception as e:  # rate limits and network blips
            if attempt == 5:
                raise
            wait = 2 ** attempt
            print(f"\n  {fn.__name__} failed ({e!r}), retrying in {wait}s")
            time.sleep(wait)


def _cached(path: Path, refresh: bool, fetch) -> pd.DataFrame:
    if path.exists() and not refresh:
        return pd.read_csv(path, index_col=0, parse_dates=[0])
    df = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path)
    return df


def _candles(coin: str, interval: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    rows = _retry(hyperliquid.get_candles, coin, interval, int(start.timestamp() * 1000), int(end.timestamp() * 1000))
    if not rows:
        return pd.DataFrame(columns=["close", "high", "low", "usd_volume"])
    df = pd.DataFrame({
        "close": [float(c["c"]) for c in rows],
        "high": [float(c["h"]) for c in rows],
        "low": [float(c["l"]) for c in rows],
        "usd_volume": [float(c["v"]) * float(c["c"]) for c in rows],
    }, index=pd.to_datetime([c["t"] for c in rows], unit="ms", utc=True))
    return df[~df.index.duplicated()].sort_index()


def _funding(coin: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    rows = _retry(hyperliquid.get_funding_history, coin, int(start.timestamp() * 1000), int(end.timestamp() * 1000))
    idx = pd.to_datetime([r["time"] for r in rows], unit="ms", utc=True).floor("h")
    df = pd.DataFrame({"rate": [float(r["fundingRate"]) for r in rows]}, index=idx)
    return df[~df.index.duplicated()].sort_index()


@dataclass
class Market:
    coin: str
    spot_pair: str
    maintenance: float
    funding: pd.Series  # hourly rate, index = hour
    bars: pd.DataFrame  # 4h close/high, index = bar end
    daily: pd.DataFrame  # perp close/low/usd_volume, index = day start
    spot_daily: pd.DataFrame  # spot usd_volume


def load_markets(start: pd.Timestamp, end: pd.Timestamp, refresh: bool) -> dict[str, Market]:
    perps = {p["name"]: p for p in _retry(hyperliquid.get_perp_universe)}
    spot = _retry(hyperliquid.get_spot_pairs)
    pairs = {}
    for base, pair in spot.items():
        coin = base if base in perps else base[1:] if base.startswith("U") and base[1:] in perps else None
        if coin and coin not in pairs:
            pairs[coin] = pair
    print(f"{len(pairs)} perps with a USDC spot market on Hyperliquid; loading history")

    markets = {}
    for i, (coin, pair) in enumerate(sorted(pairs.items()), 1):
        print(f"\r  {i}/{len(pairs)} {coin:<10}", end="", flush=True)
        name = slug(coin)
        funding = _cached(CACHE / "funding" / f"{name}.csv", refresh, lambda: _funding(coin, start, end))
        if funding.empty:
            continue
        bars = _cached(CACHE / "bars" / f"{name}.csv", refresh, lambda: _candles(coin, "4h", start, end))
        daily = _cached(CACHE / "daily" / f"{name}.csv", refresh, lambda: _candles(coin, "1d", start, end))
        spot_daily = _cached(CACHE / "spot" / f"{name}.csv", refresh, lambda: _candles(pair, "1d", start, end))
        if bars.empty or daily.empty or spot_daily.empty:
            continue
        for df in (funding, bars, daily, spot_daily):
            df.index = pd.to_datetime(df.index, utc=True)
        bars.index = bars.index + pd.Timedelta(hours=4)
        markets[coin] = Market(coin, pair, 1 / (2 * perps[coin]["maxLeverage"]),
                               funding["rate"], bars[["close", "high"]], daily, spot_daily)
    print()
    return markets


# --- selection -----------------------------------------------------------------

def candidates(markets: dict[str, Market], t: pd.Timestamp, args: argparse.Namespace) -> pd.DataFrame:
    """Every coin that passes the universe filters at t, scored from the lookback only."""
    lb_start = t - pd.Timedelta(days=args.lookback_days)
    rows = []
    for m in markets.values():
        f = m.funding[(m.funding.index >= lb_start) & (m.funding.index < t)]
        if len(f) < 0.95 * args.lookback_days * 24 or m.funding.index[0] > lb_start:
            continue
        d = m.daily[(m.daily.index >= lb_start) & (m.daily.index < t)]
        s = m.spot_daily[(m.spot_daily.index >= lb_start) & (m.spot_daily.index < t)]
        if len(d) < args.lookback_days * 0.7 or len(s) < args.lookback_days * 0.7:
            continue
        if d.usd_volume.median() < args.min_perp_volume or s.usd_volume.median() < args.min_spot_volume:
            continue
        sigma = np.log(d.close).diff().std()
        distance = max(0.20, 4 * sigma)
        margin = m.maintenance + (1 + m.maintenance) * distance
        funding = f.mean() * HOURS_PER_YEAR
        rows.append({"coin": m.coin, "funding": funding, "positive": (f > 0).mean(),
                     "distance": distance, "margin": margin, "score": funding / (1 + margin)})
    df = pd.DataFrame(rows, columns=["coin", "funding", "positive", "distance", "margin", "score"])
    return df.set_index("coin").sort_values("score", ascending=False)


def pick(ranked: pd.DataFrame, held: list[str], args: argparse.Namespace) -> list[str]:
    """Top coins by score; incumbents stay unless a challenger beats them by the switch margin."""
    ok = ranked[(ranked.positive >= args.min_positive) & (ranked.margin <= args.max_margin)
                & (ranked.score >= args.floor)]
    keep = [c for c in held if c in ok.index]
    challengers = [c for c in ok.index if c not in keep]
    while len(keep) < args.top and challengers:
        keep.append(challengers.pop(0))
    for inc in sorted(keep, key=lambda c: ok.at[c, "score"]):
        if challengers and ok.at[challengers[0], "score"] >= ok.at[inc, "score"] + args.switch_margin:
            keep[keep.index(inc)] = challengers.pop(0)
    return keep[:args.top]


# --- simulation ----------------------------------------------------------------

@dataclass
class Position:
    coin: str
    notional: float
    margin_frac: float
    distance: float
    start: pd.Timestamp
    end: pd.Timestamp


@dataclass
class Result:
    cash: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))  # net per bar
    funding: float = 0.0
    fees: float = 0.0
    rebalances: int = 0
    liquidated: list[str] = field(default_factory=list)


def simulate(pos: Position, market: Market) -> Result:
    res = Result()
    bars = market.bars[(market.bars.index > pos.start) & (market.bars.index <= pos.end)]
    if bars.empty:
        return res
    mm = market.maintenance
    price = market.bars.close[market.bars.index <= pos.start]
    price = price.iloc[-1] if len(price) else bars.close.iloc[0]
    units = pos.notional / price
    margin = pos.margin_frac * pos.notional
    mark = anchor = price
    cash = {}
    entry_fee = pos.notional * TAKER
    res.fees += entry_fee
    hourly = market.funding
    prev = pos.start

    for ts, bar in bars.iterrows():
        net = -entry_fee if prev == pos.start else 0.0
        # Price-triggered rebalances on the way up.
        trigger = anchor * (1 + pos.distance / 2)
        while bar.high >= trigger:
            margin += units * (mark - trigger)
            mark = anchor = trigger
            sell = (pos.margin_frac * units * trigger - margin) / (trigger * (1 + pos.margin_frac))
            if sell > 0:
                cost = sell * trigger * TAKER
                units -= sell
                margin += sell * trigger - sell * trigger * HL_PERP_TAKER
                res.fees += cost
                net -= cost
                res.rebalances += 1
            trigger = anchor * (1 + pos.distance / 2)
        if margin + units * (mark - bar.high) < mm * units * bar.high:
            loss = mm * units * bar.high + units * bar.high * HL_SPOT_TAKER
            res.liquidated.append(f"{market.coin} {ts:%Y-%m-%d}")
            cash[ts] = net - loss
            res.fees += units * bar.high * HL_SPOT_TAKER
            res.cash = pd.Series(cash)
            return res
        rate = hourly[(hourly.index >= prev) & (hourly.index < ts)].sum()
        received = units * bar.close * rate
        res.funding += received
        margin += received + units * (mark - bar.close)
        mark = bar.close
        cash[ts] = net + received
        prev = ts

    exit_fee = units * mark * TAKER
    res.fees += exit_fee
    cash[bars.index[-1]] -= exit_fee
    res.cash = pd.Series(cash)
    return res


def run_window(markets: dict[str, Market], start: pd.Timestamp, choose, args: argparse.Namespace):
    """choose(t, held) -> list of (coin, notional, margin_frac, distance) for the half from t."""
    first = choose(start, [])
    second = choose(start + HALF, [c for c, *_ in first])
    positions = []
    by_coin_second = {c: rest for c, *rest in second}
    for coin, notional, m, d in first:
        end = start + WINDOW if coin in by_coin_second else start + HALF
        positions.append(Position(coin, notional, m, d, start, end))
    for coin, notional, m, d in second:
        if coin not in {c for c, *_ in first}:
            positions.append(Position(coin, notional, m, d, start + HALF, start + WINDOW))

    capital = max(sum(n * (1 + m) for _, n, m, _ in half) for half in (first, second)) if (first or second) else 0
    total = Result()
    parts = []
    for pos in positions:
        r = simulate(pos, markets[pos.coin])
        total.funding += r.funding
        total.fees += r.fees
        total.rebalances += r.rebalances
        total.liquidated += r.liquidated
        parts.append(r.cash)
    total.cash = pd.concat(parts).groupby(level=0).sum() if parts else pd.Series(dtype=float)
    return total, capital or args.notional, [c for c, *_ in first], [c for c, *_ in second]


def yearly(net: float, capital: float) -> float:
    return net / capital * 365 / WINDOW.days * 100


def main() -> None:
    args = parse_args()
    end = pd.Timestamp.now(tz="UTC").floor("D")
    first_window = end - args.windows * WINDOW
    data_start = first_window - pd.Timedelta(days=args.lookback_days + 2)
    markets = load_markets(data_start, end, args.refresh)

    def scan(t, held):
        ranked = candidates(markets, t, args)
        coins = pick(ranked, held, args)
        size = args.notional / args.top
        return [(c, size, ranked.at[c, "margin"], ranked.at[c, "distance"]) for c in coins]

    def universe(t, held):
        ranked = candidates(markets, t, args)
        if ranked.empty:
            return []
        size = args.notional / len(ranked)
        return [(c, size, r.margin, r.distance) for c, r in ranked.iterrows()]

    def hype(t, held):
        if "HYPE" not in markets:
            return []
        m = markets["HYPE"]
        d = m.daily[(m.daily.index >= t - pd.Timedelta(days=args.lookback_days)) & (m.daily.index < t)]
        distance = max(0.20, 4 * np.log(d.close).diff().std())
        return [("HYPE", args.notional, m.maintenance + (1 + m.maintenance) * distance, distance)]

    btc = markets["BTC"].daily if "BTC" in markets else None
    print(f"\n{args.windows} windows of 8 weeks, picks at week 0 and 4, top {args.top}, "
          f"lookback {args.lookback_days}d, floor {args.floor:.0%}/yr\n")
    print(f"{'window':<17}{'elig':>5}  {'picks (wk 0 | wk 4)':<26}{'funding':>8}{'fees':>7}{'net':>8}"
          f"{'%/yr':>7}{'+wk':>6}{'worst wk':>9}{'rebal':>6}  {'HYPE':>6}{'all':>7}  verdict")
    verdicts, liquidations, ours, base_hype, base_all = [], 0, [], [], []
    for i in range(args.windows):
        start = first_window + i * WINDOW
        res, capital, p1, p2 = run_window(markets, start, scan, args)
        net = res.cash.sum()
        pct = yearly(net, capital)
        weekly = res.cash.resample("W").sum() if not res.cash.empty else pd.Series([0.0])
        h, hcap, *_ = run_window(markets, start, hype, args)
        a, acap, *_ = run_window(markets, start, universe, args)
        hp, ap = yearly(h.cash.sum(), hcap), yearly(a.cash.sum(), acap)
        crash = btc is not None and ((btc.low / btc.close.shift(1) - 1)[
            (btc.index >= start) & (btc.index < start + WINDOW)] <= CRASH_DAY).any()

        if res.liquidated:
            verdict = "FAIL: liquidated " + ", ".join(res.liquidated)
        elif pct < 5:
            verdict = "FAIL"
        elif pct >= 10:
            verdict = "PASS"
        else:
            verdict = "EXTEND"
        verdicts.append(verdict.split(":")[0])
        liquidations += bool(res.liquidated)
        ours.append(pct)
        base_hype.append(hp)
        base_all.append(ap)
        picks = f"{','.join(p1) or 'cash'} | {','.join(p2) or 'cash'}"
        eligible = len(candidates(markets, start, args))
        print(f"{start:%Y-%m-%d} –{(start + WINDOW):%m-%d}{eligible:>5}  {picks:<26}{res.funding:>+8.2f}"
              f"{res.fees:>7.2f}{net:>+8.2f}{pct:>+7.1f}{(weekly > 0).sum():>3}/{len(weekly):<2}"
              f"{weekly.min():>+9.2f}{res.rebalances:>6}  {hp:>+6.1f}{ap:>+7.1f}  {verdict}"
              f"{'  [crash: optimistic]' if crash else ''}")

    passes = verdicts.count("PASS")
    gate = passes >= math.ceil(len(verdicts) * 2 / 3) and not liquidations
    print(f"\n{passes} pass, {verdicts.count('EXTEND')} extend, {verdicts.count('FAIL')} fail of {len(verdicts)}")
    print(f"mean %/yr: scan {np.mean(ours):+.1f}, always HYPE {np.mean(base_hype):+.1f}, "
          f"equal weight {np.mean(base_all):+.1f}")
    beats = np.mean(ours) > max(np.mean(base_hype), np.mean(base_all))
    print(f"gate (≥ 2/3 pass, no liquidation, beats both baselines): {'PASSED' if gate and beats else 'NOT PASSED'}")


if __name__ == "__main__":
    main()
