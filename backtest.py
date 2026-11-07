"""Backtest a simple mean-reversion rule on a gap file.

One trade at a time, one unit per leg:
  normal = rolling median of the gap over the previous --window hours
  enter  when the gap is at least --entry away from normal
  exit   when the gap is back at normal
"""

import argparse
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


@dataclass
class Trade:
    side: str  # "short": gap too high, profits if it falls. "long": the reverse.
    entry_time: pd.Timestamp
    entry_gap: float
    exit_time: pd.Timestamp | None = None
    exit_gap: float | None = None

    @property
    def gross(self) -> float:
        move = self.exit_gap - self.entry_gap
        return -move if self.side == "short" else move


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("csv", type=Path, help="gap file from recorder.py or history.py")
    parser.add_argument("--window", type=float, default=6, help="hours used for 'normal'")
    parser.add_argument("--entry", type=float, default=3, help="distance from normal to enter")
    parser.add_argument("--fee", type=float, default=1.5, help="round-trip cost per trade, both legs")
    return parser.parse_args()


def rolling_normal(gap: pd.Series, window_h: float) -> pd.Series:
    window = pd.Timedelta(hours=window_h)
    # shift(1): each minute only sees the past, never itself or the future.
    normal = gap.rolling(window).median().shift(1)
    normal[gap.index < gap.index[0] + window] = float("nan")  # warm-up
    return normal


def run(gap: pd.Series, normal: pd.Series, entry: float) -> list[Trade]:
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
        elif (open_trade.side == "short" and g <= n) or (open_trade.side == "long" and g >= n):
            open_trade.exit_time, open_trade.exit_gap = ts, g
            trades.append(open_trade)
            open_trade = None

    if open_trade is not None:  # still open when the data ends: close at the last value
        open_trade.exit_time, open_trade.exit_gap = gap.index[-1], gap.iloc[-1]
        trades.append(open_trade)
    return trades


def main() -> None:
    args = parse_args()
    df = pd.read_csv(args.csv, parse_dates=["timestamp"], index_col="timestamp")
    trades = run(df["gap"], rolling_normal(df["gap"], args.window), args.entry)

    if not trades:
        print("no trades")
        return

    out = pd.DataFrame([{**vars(t), "gross": t.gross} for t in trades])
    out["net"] = out["gross"] - args.fee
    out["held"] = out["exit_time"] - out["entry_time"]

    path = Path("data/backtests") / f"{args.csv.stem}__w{args.window:g}_e{args.entry:g}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)

    print(f"trades        {len(out)}")
    print(f"winners       {(out['net'] > 0).sum()}")
    print(f"gross profit  {out['gross'].sum():+.2f}")
    print(f"fees          {-args.fee * len(out):+.2f}")
    print(f"net profit    {out['net'].sum():+.2f}")
    print(f"worst trade   {out['net'].min():+.2f}")
    print(f"typical hold  {out['held'].median()}")
    print(f"longest hold  {out['held'].max()}")
    print(f"saved {path}")


if __name__ == "__main__":
    main()
