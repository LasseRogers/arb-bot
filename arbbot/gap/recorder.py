"""Record the price gap between two markets to a CSV file.

Markets are written as venue:symbol, e.g. hl:PAXG, hl:xyz:GOLD, binance:BTCUSDT.
The gap is first minus second.
"""

import argparse
import csv
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

from arbbot.util import slug
from arbbot.venues import binance
from arbbot.venues import hyperliquid

VENUES: dict[str, Callable[[str], float]] = {
    "hl": hyperliquid.get_mid_price,
    "binance": binance.get_mid_price,
}


def market(spec: str) -> str:
    venue, _, symbol = spec.partition(":")
    if venue not in VENUES or not symbol:
        raise argparse.ArgumentTypeError(
            f"bad market {spec!r}: expected venue:symbol, venue one of {sorted(VENUES)}"
        )
    return spec


def get_price(spec: str) -> float:
    venue, _, symbol = spec.partition(":")
    return VENUES[venue](symbol)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("a", type=market, help="first market, e.g. hl:PAXG")
    parser.add_argument("b", type=market, help="second market, e.g. hl:xyz:GOLD")
    parser.add_argument("--interval", type=float, default=5, help="seconds between samples")
    return parser.parse_args()


def sample(a: str, b: str, pool: ThreadPoolExecutor) -> dict[str, float | str]:
    # Fetch both legs concurrently so the two quotes are as close in time as possible.
    future_a = pool.submit(get_price, a)
    future_b = pool.submit(get_price, b)
    price_a, price_b = future_a.result(), future_b.result()
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        a: price_a,
        b: price_b,
        "gap": round(price_a - price_b, 4),
    }


def main() -> None:
    args = parse_args()
    output = Path("data") / f"{slug(args.a)}__{slug(args.b)}.csv"
    output.parent.mkdir(exist_ok=True)
    is_new = not output.exists()

    with output.open("a", newline="") as f, ThreadPoolExecutor(max_workers=2) as pool:
        writer = csv.DictWriter(f, fieldnames=["timestamp", args.a, args.b, "gap"])
        if is_new:
            writer.writeheader()

        print(f"Writing to {output} every {args.interval}s. Ctrl+C to stop.")
        while True:
            try:
                row = sample(args.a, args.b, pool)
            except requests.RequestException as exc:
                print(f"skipped: {exc}")
            else:
                writer.writerow(row)
                f.flush()
                print(f"{row['timestamp']}  gap {row['gap']:+.2f}")
            time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")
