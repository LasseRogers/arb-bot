"""Backtest a funding carry: hold spot long and an equal perp short, collect funding.

The number of coins is fixed at entry, so hourly funding is paid on the
position's value at that hour. Fees are charged once for entry and exit.
"""

import argparse
import time
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

import hyperliquid
from recorder import slug


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("coin", help="Hyperliquid perp, e.g. HYPE")
    parser.add_argument("--capital", type=float, default=1000, help="USD of spot bought at entry")
    parser.add_argument("--fees", type=float, default=0.0011,
                        help="round-trip fee rate, both legs (0.0011 = 0.11%%)")
    return parser.parse_args()


def hourly_closes(coin: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    candles = hyperliquid.get_candles(coin, "1h", int(start.timestamp() * 1000), int(end.timestamp() * 1000))
    return pd.Series(
        {pd.to_datetime(c["t"], unit="ms", utc=True): float(c["c"]) for c in candles}
    ).sort_index()


def main() -> None:
    args = parse_args()
    path = Path("data/funding") / f"{slug('hl:' + args.coin)}.csv"
    if not path.exists():
        raise SystemExit(f"missing {path}: run funding.py {args.coin} --days 180 first")
    rates = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")["rate"]

    prices = hourly_closes(args.coin, rates.index[0], rates.index[-1] + pd.Timedelta(hours=1))
    px = prices.reindex(rates.index, method="ffill").bfill()
    units = args.capital / px.iloc[0]

    # Short perp: positive funding is received, negative is paid.
    cash = units * px * rates
    fees = args.capital * args.fees
    days = (rates.index[-1] - rates.index[0]).total_seconds() / 86_400
    total = cash.sum() - fees

    monthly = cash.resample("ME").sum()
    weekly = cash.resample("W").sum()

    print(f"{args.coin}: {days:.0f} days, ${args.capital:,.0f} position, fees ${fees:.2f}\n")
    print("month       funding")
    for ts, v in monthly.items():
        print(f"{ts:%Y-%m}  {v:>+9.2f}")
    print(f"\nweeks             {len(weekly)}")
    print(f"weeks negative    {(weekly < 0).sum()}")
    print(f"worst week        {weekly.min():+.2f}")
    print(f"best week         {weekly.max():+.2f}")
    print(f"total after fees  {total:+.2f}  ({total / args.capital:+.1%}, "
          f"{total / args.capital * 365 / days:+.1%} per year)")

    fig, (top, bottom) = plt.subplots(2, 1, figsize=(11, 6), sharex=True, height_ratios=[2, 1])
    top.plot((cash.cumsum() - fees).index, cash.cumsum() - fees, color="tab:green")
    top.axhline(0, color="grey", lw=0.8)
    top.set_ylabel("earned so far ($)")
    top.set_title(f"{args.coin} carry: ${args.capital:,.0f} spot long + perp short")
    bottom.bar(weekly.index, weekly, width=5,
               color=["tab:green" if v >= 0 else "tab:red" for v in weekly])
    bottom.set_ylabel("per week ($)")
    fig.autofmt_xdate()
    fig.tight_layout()
    out = Path("charts") / f"carry_{slug(args.coin)}.png"
    out.parent.mkdir(exist_ok=True)
    fig.savefig(out, dpi=120)
    print(f"\nsaved {out}")


if __name__ == "__main__":
    main()
