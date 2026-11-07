"""Record the price gap between two markets to a CSV file.

Markets are written as venue:symbol, e.g. hl:PAXG, hl:xyz:GOLD, binance:BTCUSDT.
The gap is first minus second.
"""

import argparse
import csv
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import requests

import binance
import hyperliquid

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


def slug(spec: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", spec.lower()).strip("-")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("a", type=market, help="first market, e.g. hl:PAXG")
    parser.add_argument("b", type=market, help="second market, e.g. hl:xyz:GOLD")
    parser.add_argument("--interval", type=float, default=5, help="seconds between samples")
    return parser.parse_args()


def sample(a: str, b: str) -> dict[str, float | str]:
    price_a = get_price(a)
    price_b = get_price(b)
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

    with output.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", args.a, args.b, "gap"])
        if is_new:
            writer.writeheader()

        print(f"Writing to {output} every {args.interval}s. Ctrl+C to stop.")
        while True:
            try:
                row = sample(args.a, args.b)
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
