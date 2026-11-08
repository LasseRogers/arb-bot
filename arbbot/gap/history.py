"""Download past closes for two Hyperliquid markets and save the gap.

The output has the same layout as recorder.py, so plot.py and backtest.py work on it.
Hyperliquid serves roughly the latest 5000 candles per interval, so coarser
intervals reach further back.
"""

import argparse
import time
from pathlib import Path

import pandas as pd

from arbbot.venues import hyperliquid
from arbbot.util import slug

INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}
MAX_CANDLES = 5000


def fetch_closes(coin: str, days: float, interval: str) -> pd.Series:
    step_ms = INTERVAL_MINUTES[interval] * 60_000
    end = int(time.time() * 1000)
    start = end - int(days * 24 * 60 * 60_000)

    closes: dict[pd.Timestamp, float] = {}
    while start < end:
        chunk_end = min(start + MAX_CANDLES * step_ms, end)
        for candle in hyperliquid.get_candles(coin, interval, start, chunk_end):
            closes[pd.to_datetime(candle["t"], unit="ms", utc=True)] = float(candle["c"])
        start = chunk_end

    if not closes:
        raise SystemExit(f"no candles returned for {coin}")
    return pd.Series(closes, name=f"hl:{coin}").sort_index()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("a", help="first Hyperliquid coin, e.g. PAXG")
    parser.add_argument("b", help="second Hyperliquid coin, e.g. xyz:GOLD")
    parser.add_argument("--days", type=float, default=3, help="how far back to go")
    parser.add_argument("--interval", choices=INTERVAL_MINUTES, default="1m")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    a = fetch_closes(args.a, args.days, args.interval)
    b = fetch_closes(args.b, args.days, args.interval)

    # Keep only timestamps where both markets have a candle.
    df = pd.concat([a, b], axis=1, join="inner")
    df.index.name = "timestamp"
    df["gap"] = (df[a.name] - df[b.name]).round(4)

    output = Path("data/history") / f"{slug(a.name)}__{slug(b.name)}__{args.interval}.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output)

    print(f"{a.name}: {len(a)} candles, {b.name}: {len(b)} candles, {len(df)} matched")
    print(f"from {df.index[0]} to {df.index[-1]}")
    print(f"saved {output}")


if __name__ == "__main__":
    main()
