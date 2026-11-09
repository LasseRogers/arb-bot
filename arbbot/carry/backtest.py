"""Backtest the funding carry against docs/criteria.md: spot long + equal perp short.

Each non-overlapping 8-week window is a fresh run: enter at its start, exit at its end,
judged by the same pass/fail rules as the paper test. Per hour:

  - funding is paid on the short's value at that hour (positive rate: we receive)
  - margin = deposit + short P&L + funding received - perp fees
  - every hour's high is tested for liquidation (maintenance margin is half the
    initial margin at max leverage) and against entry (or last rebalance) x 1.25
  - at each check (--check-every hours, as the paper test is run by hand): below the
    rebalance level, spot is sold and the same amount of perp bought back, and the
    cash tops margin up to the deposit again, fees paid on both legs

Spot and perp use the perp price, so hedge P&L is zero here; the paper test measures it.

Caveat: the coin was picked for its past funding, so windows before the pick flatter it.
"""

import argparse
import math
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from arbbot.carry.paper import PERP_MAKER, SPOT_MAKER
from arbbot.util import slug
from arbbot.venues import hyperliquid

HOURS_PER_YEAR = 24 * 365


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("coin", help="Hyperliquid perp, e.g. HYPE")
    parser.add_argument("--capital", type=float, default=1000, help="USD of spot bought at entry")
    parser.add_argument("--margin", type=float, default=300, help="USD deposited for the short")
    parser.add_argument("--rebalance-below", type=float, default=150)
    parser.add_argument("--weeks", type=int, default=8, help="window length")
    parser.add_argument("--check-every", type=int, default=24,
                        help="hours between manual checks, where margin is read and rebalanced")
    parser.add_argument("--exit-below", type=float, default=None,
                        help="optional: step out when trailing 72h funding falls below this %%/yr")
    parser.add_argument("--enter-above", type=float, default=None,
                        help="optional: step back in when trailing 72h funding rises above this %%/yr")
    return parser.parse_args()


@dataclass
class Run:
    hourly: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))  # net cash per hour
    funding: float = 0.0
    gross_funding: float = 0.0
    fees: float = 0.0
    rebalances: int = 0
    rebalance_cost: float = 0.0
    min_margin: float = math.inf
    high_breaches: int = 0
    liquidated: str = ""
    hours_out: int = 0


def candles(coin: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Hourly close and high, indexed by the hour the candle ends (when funding is paid)."""
    rows = hyperliquid.get_candles(coin, "1h", int(start.timestamp() * 1000), int(end.timestamp() * 1000))
    df = pd.DataFrame({
        "close": [float(c["c"]) for c in rows],
        "high": [float(c["h"]) for c in rows],
    }, index=pd.to_datetime([c["t"] for c in rows], unit="ms", utc=True) + pd.Timedelta(hours=1))
    return df.sort_index()


def simulate(rates: pd.Series, px: pd.DataFrame, args: argparse.Namespace, mmr: float) -> Run:
    run = Run()
    trailing = rates.rolling(72, min_periods=72).mean() * HOURS_PER_YEAR * 100
    cash: dict[pd.Timestamp, float] = {}
    units = margin = mark = anchor = 0.0  # anchor: price at entry or last rebalance
    holding = False

    def enter(price: float) -> float:
        nonlocal units, margin, mark, anchor, holding
        units, mark, anchor, holding = args.capital / price, price, price, True
        margin = args.margin - args.capital * PERP_MAKER
        fee = args.capital * (SPOT_MAKER + PERP_MAKER)
        run.fees += fee
        return fee

    def leave(price: float) -> float:
        nonlocal holding
        holding = False
        fee = units * price * (SPOT_MAKER + PERP_MAKER)
        run.fees += fee
        return fee

    for i, (ts, rate) in enumerate(rates.items()):
        close, high = px.at[ts, "close"], px.at[ts, "high"]
        net = -enter(close) if i == 0 else 0.0
        if not holding:
            run.hours_out += 1
            if args.enter_above is not None and trailing.get(ts, -math.inf) > args.enter_above:
                net -= enter(close)
            cash[ts] = net
            continue

        if margin + units * (mark - high) < mmr * units * high:
            # Liquidated at the high: the short is closed and its maintenance margin kept by
            # the exchange. Spot gained what the short lost, so that is the loss, plus selling spot.
            loss = mmr * units * high + units * high * SPOT_MAKER
            run.fees += units * high * SPOT_MAKER
            run.liquidated, holding = f"{ts:%Y-%m-%d %H:%M}", False
            cash[ts] = net - loss
            break
        if high >= anchor * 1.25:
            run.high_breaches += 1
            anchor = math.inf  # count each breach once until the next rebalance

        received = units * close * rate
        run.funding += received
        run.gross_funding += max(received, 0.0)
        net += received
        margin += received + units * (mark - close)
        mark = close

        if i % args.check_every == 0:
            run.min_margin = min(run.min_margin, margin)
            if margin < args.rebalance_below:
                sell = (args.margin - margin) / close
                cost = sell * close * (SPOT_MAKER + PERP_MAKER)
                units -= sell
                margin = args.margin - sell * close * PERP_MAKER
                anchor = close
                run.rebalances += 1
                run.rebalance_cost += cost
                run.fees += cost
                net -= cost
            if args.exit_below is not None and trailing.get(ts, math.inf) < args.exit_below:
                net -= leave(close)
        cash[ts] = net

    if holding:
        last = rates.index[-1]
        cash[last] -= leave(px.at[last, "close"])
    run.hourly = pd.Series(cash).sort_index()
    return run


def verdict(run: Run, args: argparse.Namespace) -> tuple[str, list[str]]:
    put_in = args.capital + args.margin
    net = run.hourly.sum()
    yearly = net / put_in * 365 / (args.weeks * 7) * 100
    weekly = run.hourly.resample("W").sum()
    worst_hours = -run.hourly.nsmallest(5).clip(upper=0).sum()
    fails = []
    if run.liquidated:
        fails.append(f"liquidated {run.liquidated}")
    if run.min_margin < 90:
        fails.append(f"margin {run.min_margin:.0f} < 90")
    if run.high_breaches:
        fails.append(f"high ≥ entry × 1.25 ({run.high_breaches}x)")
    if run.rebalances > 3:
        fails.append(f"{run.rebalances} rebalances")
    if run.funding > 0 and run.rebalance_cost > 0.25 * run.funding:
        fails.append("rebalance cost > 25% of funding")
    if yearly < 5:
        fails.append(f"{yearly:.1f}%/yr < 5")
    if fails:
        return "FAIL", fails
    passes = [
        yearly >= 10,
        (weekly > 0).sum() >= math.ceil(len(weekly) * 6 / 8),
        weekly.min() >= -5,
        run.gross_funding > 0 and worst_hours <= 0.3 * run.gross_funding,
    ]
    return ("PASS" if all(passes) else "EXTEND"), []


def main() -> None:
    args = parse_args()
    path = Path("data/funding") / f"{slug('hl:' + args.coin)}.csv"
    if not path.exists():
        raise SystemExit(f"missing {path}: run python -m arbbot.carry.funding {args.coin} --days 180 first")
    rates = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")["rate"]
    rates.index = rates.index.floor("h")
    rates = rates[~rates.index.duplicated()]

    px = candles(args.coin, rates.index[0] - pd.Timedelta(hours=1), rates.index[-1])
    px = px.reindex(rates.index).ffill().bfill()
    mmr = 1 / (2 * hyperliquid.get_max_leverage(args.coin))
    put_in = args.capital + args.margin
    span = pd.Timedelta(weeks=args.weeks)

    print(f"{args.coin}: ${args.capital:,.0f} spot + ${args.margin:,.0f} margin, rebalance below "
          f"${args.rebalance_below:,.0f}, maintenance margin {mmr:.1%}, {args.weeks}-week windows")
    if args.exit_below is not None:
        print(f"exit below {args.exit_below:g}%/yr, re-enter above {args.enter_above}%/yr (trailing 72h)")
    print(f"\n{'window':<23} {'funding':>8} {'fees':>6} {'net':>7} {'%/yr':>6} {'+wk':>4} {'worst wk':>8} "
          f"{'min mgn':>7} {'rebal':>5} {'out h':>5}  verdict")

    start, results = rates.index[0], []
    while start + span <= rates.index[-1] + pd.Timedelta(hours=1):
        window = rates[(rates.index >= start) & (rates.index < start + span)]
        run = simulate(window, px, args, mmr)
        net = run.hourly.sum()
        weekly = run.hourly.resample("W").sum()
        result, why = verdict(run, args)
        results.append(result)
        print(f"{start:%Y-%m-%d} – {(start + span):%m-%d}  {run.funding:>+8.2f} {run.fees:>6.2f} {net:>+7.2f} "
              f"{net / put_in * 365 / (args.weeks * 7) * 100:>+6.1f} {(weekly > 0).sum():>2}/{len(weekly):<2} "
              f"{weekly.min():>+8.2f} {run.min_margin:>7.0f} {run.rebalances:>5} {run.hours_out:>5}  "
              f"{result}{': ' + '; '.join(why) if why else ''}")
        start += span

    print(f"\n{results.count('PASS')} pass, {results.count('EXTEND')} extend, {results.count('FAIL')} fail "
          f"of {len(results)} windows")


if __name__ == "__main__":
    main()
