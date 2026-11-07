"""Record the price gap for one asset between Hyperliquid and Binance to a CSV file."""

import argparse
import csv
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import binance
import hyperliquid

FIELDS = ["timestamp", "hyperliquid", "binance", "gap"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hl_coin", help="Hyperliquid coin, e.g. BTC or PAXG")
    parser.add_argument("binance_symbol", help="Binance spot symbol, e.g. BTCUSDT or PAXGUSDT")
    parser.add_argument("--interval", type=float, default=5, help="seconds between samples")
    return parser.parse_args()


def sample(hl_coin: str, binance_symbol: str) -> dict[str, float | str]:
    hl = hyperliquid.get_mid_price(hl_coin)
    bn = binance.get_mid_price(binance_symbol)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hyperliquid": hl,
        "binance": bn,
        "gap": round(hl - bn, 4),
    }


def main() -> None:
    args = parse_args()
    output = Path("data") / f"{args.hl_coin.lower()}_gap.csv"
    output.parent.mkdir(exist_ok=True)
    is_new = not output.exists()

    with output.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new:
            writer.writeheader()

        print(f"Writing to {output} every {args.interval}s. Ctrl+C to stop.")
        while True:
            try:
                row = sample(args.hl_coin, args.binance_symbol)
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
