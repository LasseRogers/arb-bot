"""Backtest a simple mean-reversion rule on a gap file.

One trade at a time, one unit per leg:
  normal = rolling median of the gap over the previous --window hours
  enter  when the gap is at least --entry away from normal
  exit   when the gap is back at normal, or earlier if a stop triggers:
           --stop      gap moved this far further against the trade
           --max-hold  trade has been open this many hours
With --funding, hourly funding from data/funding/ is charged or credited per leg.
"""

import argparse
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from arbbot.util import slug


@dataclass
class Trade:
    side: str  # "short": gap too high, profits if it falls. "long": the reverse.
    entry_time: pd.Timestamp
    entry_gap: float
    exit_time: pd.Timestamp | None = None
    exit_gap: float | None = None
    reason: str | None = None  # "normal", "stop", "time" or "end"

    @property
    def gross(self) -> float:
        move = self.exit_gap - self.entry_gap
        return -move if self.side == "short" else move

    @property
    def sign_a(self) -> int:
        """+1 if long the first market, -1 if short it. The second leg is the opposite."""
        return -1 if self.side == "short" else 1

    def adverse_move(self, gap: float) -> float:
        """How far the gap has moved against the trade since entry."""
        return gap - self.entry_gap if self.side == "short" else self.entry_gap - gap


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("csv", type=Path, help="gap file from recorder.py or history.py")
    parser.add_argument("--window", type=float, default=6, help="hours used for 'normal'")
    parser.add_argument("--entry", type=float, default=3, help="distance from normal to enter")
    parser.add_argument("--fee", type=float, default=1.5, help="round-trip fees per trade, both legs")
    parser.add_argument(
        "--slippage", type=float, default=0, help="round-trip price slippage per trade, both legs"
    )
    parser.add_argument("--stop", type=float, help="close if the gap moves this far against the trade")
    parser.add_argument("--max-hold", type=float, help="close after this many hours")
    parser.add_argument("--funding", action="store_true", help="include hourly funding per leg")
    return parser.parse_args()


def rolling_normal(gap: pd.Series, window_h: float) -> pd.Series:
    window = pd.Timedelta(hours=window_h)
    # shift(1): each row only sees the past, never itself or the future.
    normal = gap.rolling(window).median().shift(1)
    normal[gap.index < gap.index[0] + window] = float("nan")  # warm-up
    return normal


def exit_reason(trade: Trade, ts: pd.Timestamp, g: float, n: float,
                stop: float | None, max_hold: pd.Timedelta | None) -> str | None:
    if stop is not None and trade.adverse_move(g) >= stop:
        return "stop"
    if max_hold is not None and ts - trade.entry_time >= max_hold:
        return "time"
    if (trade.side == "short" and g <= n) or (trade.side == "long" and g >= n):
        return "normal"
    return None


def run(gap: pd.Series, normal: pd.Series, entry: float,
        stop: float | None, max_hold_h: float | None) -> list[Trade]:
    max_hold = pd.Timedelta(hours=max_hold_h) if max_hold_h is not None else None
    trades: list[Trade] = []
    open_trade: Trade | None = None

    for ts, g, n in zip(gap.index, gap, normal):
        if pd.isna(n):
            continue
        if open_trade is None:
            if g >= n + entry:
                open_trade = Trade("short", ts, g)
            elif g <= n - entry:
                open_trade = Trade("long", ts, g)
            continue
        reason = exit_reason(open_trade, ts, g, n, stop, max_hold)
        if reason:
            open_trade.exit_time, open_trade.exit_gap, open_trade.reason = ts, g, reason
            trades.append(open_trade)
            open_trade = None

    if open_trade is not None:
        open_trade.exit_time, open_trade.exit_gap = gap.index[-1], gap.iloc[-1]
        open_trade.reason = "end"
        trades.append(open_trade)
    return trades


def load_funding(market: str) -> pd.Series:
    path = Path("data/funding") / f"{slug(market)}.csv"
    if not path.exists():
        raise SystemExit(f"missing {path}: run python -m arbbot.carry.funding for {market} first")
    return pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")["rate"]


def funding_cash(trade: Trade, prices: pd.DataFrame, rates: dict[str, pd.Series]) -> float:
    """Funding received (+) or paid (-) over the life of a trade, both legs."""
    a, b = prices.columns[:2]
    total = 0.0
    for market, sign in ((a, trade.sign_a), (b, -trade.sign_a)):
        r = rates[market]
        hours = r[(r.index > trade.entry_time) & (r.index <= trade.exit_time)]
        if hours.empty:
            continue
        px = prices[market].reindex(hours.index, method="ffill")
        # Positive rate: longs (sign +1) pay, shorts (sign -1) receive.
        total += float((-sign * hours * px).sum())
    return total


def fmt(value: float | None) -> str:
    return "none" if value is None else f"{value:g}"


def main() -> None:
    args = parse_args()
    df = pd.read_csv(args.csv, parse_dates=["timestamp"], index_col="timestamp")
    normal = rolling_normal(df["gap"], args.window)
    trades = run(df["gap"], normal, args.entry, args.stop, args.max_hold)

    if not trades:
        print("no trades")
        return

    cost = args.fee + args.slippage
    out = pd.DataFrame([{**vars(t), "gross": t.gross} for t in trades])
    out["funding"] = 0.0
    if args.funding:
        prices = df.drop(columns="gap")
        rates = {m: load_funding(m) for m in prices.columns[:2]}
        out["funding"] = [funding_cash(t, prices, rates) for t in trades]
    out["net"] = out["gross"] - cost + out["funding"]
    out["held"] = out["exit_time"] - out["entry_time"]

    name = (
        f"{args.csv.stem}__w{args.window:g}_e{args.entry:g}"
        f"_f{args.fee:g}_s{args.slippage:g}_st{fmt(args.stop)}_mh{fmt(args.max_hold)}"
        f"{'_fund' if args.funding else ''}.csv"
    )
    path = Path("data/backtests") / name
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)

    reasons = out["reason"].value_counts().to_dict()
    print(f"trades        {len(out)}")
    print(f"winners       {(out['net'] > 0).sum()}")
    print(f"closed by     {reasons}")
    print(f"gross profit  {out['gross'].sum():+.2f}")
    print(f"fees          {-args.fee * len(out):+.2f}")
    print(f"slippage      {-args.slippage * len(out):+.2f}")
    print(f"funding       {out['funding'].sum():+.2f}")
    print(f"net profit    {out['net'].sum():+.2f}")
    print(f"worst trade   {out['net'].min():+.2f}")
    print(f"typical hold  {out['held'].median()}")
    print(f"longest hold  {out['held'].max()}")
    print(f"saved {path}")


if __name__ == "__main__":
    main()
