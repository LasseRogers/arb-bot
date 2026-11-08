"""Download hourly funding rates for Hyperliquid coins to data/funding/.

Positive rate: longs pay shorts. Hourly payment = position value * rate.
"""

import argparse
import time
from pathlib import Path

import pandas as pd

import hyperliquid
from recorder import slug

HOURS_PER_YEAR = 24 * 365


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("coins", nargs="+", help="e.g. PAXG xyz:GOLD")
    parser.add_argument("--days", type=float, default=20)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    end = int(time.time() * 1000)
    start = end - int(args.days * 24 * 3_600_000)

    for coin in args.coins:
        records = hyperliquid.get_funding_history(coin, start, end)
        if not records:
            print(f"{coin}: no funding data")
            continue
        rates = pd.Series(
            {pd.to_datetime(r["time"], unit="ms", utc=True): float(r["fundingRate"]) for r in records},
            name="rate",
        ).sort_index()
        rates.index.name = "timestamp"

        output = Path("data/funding") / f"{slug('hl:' + coin)}.csv"
        output.parent.mkdir(parents=True, exist_ok=True)
        rates.to_csv(output)

        yearly = rates.mean() * HOURS_PER_YEAR
        print(f"{coin}: {len(rates)} hours, average {yearly:+.1%} per year (longs pay if positive)")
        print(f"  saved {output}")


if __name__ == "__main__":
    main()
