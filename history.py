"""Download past 1-minute closes for two Hyperliquid markets and save the gap.

The output has the same layout as recorder.py, so plot.py works on it.
"""

import argparse
import time
from pathlib import Path

import pandas as pd

import hyperliquid
from recorder import slug

MINUTE_MS = 60_000
MAX_CANDLES = 5000


def fetch_closes(coin: str, days: float) -> pd.Series:
    end = int(time.time() * 1000)
    start = end - int(days * 24 * 60 * MINUTE_MS)

    closes: dict[pd.Timestamp, float] = {}
    while start < end:
        chunk_end = min(start + MAX_CANDLES * MINUTE_MS, end)
        for candle in hyperliquid.get_candles(coin, "1m", start, chunk_end):
            closes[pd.to_datetime(candle["t"], unit="ms", utc=True)] = float(candle["c"])
        start = chunk_end

    if not closes:
        raise SystemExit(f"no candles returned for {coin}")
    return pd.Series(closes, name=f"hl:{coin}").sort_index()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("a", help="first Hyperliquid coin, e.g. PAXG")
    parser.add_argument("b", help="second Hyperliquid coin, e.g. xyz:GOLD")
    parser.add_argument("--days", type=float, default=3, help="how far back to go")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    a = fetch_closes(args.a, args.days)
    b = fetch_closes(args.b, args.days)

    # Keep only minutes where both markets traded.
    df = pd.concat([a, b], axis=1, join="inner")
    df.index.name = "timestamp"
    df["gap"] = (df[a.name] - df[b.name]).round(4)

    output = Path("data/history") / f"{slug(a.name)}__{slug(b.name)}.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output)

    print(f"{a.name}: {len(a)} candles, {b.name}: {len(b)} candles, {len(df)} matched")
    print(f"from {df.index[0]} to {df.index[-1]}")
    print(f"saved {output}")


if __name__ == "__main__":
    main()
