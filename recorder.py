"""Record the BTC price gap between Hyperliquid and Binance to a CSV file."""

import csv
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import binance
import hyperliquid

OUTPUT = Path("data/btc_gap.csv")
INTERVAL_S = 5
FIELDS = ["timestamp", "hyperliquid", "binance", "gap"]


def sample() -> dict[str, float | str]:
    hl = hyperliquid.get_mid_price("BTC")
    bn = binance.get_mid_price("BTCUSDT")
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hyperliquid": hl,
        "binance": bn,
        "gap": round(hl - bn, 2),
    }


def main() -> None:
    OUTPUT.parent.mkdir(exist_ok=True)
    is_new = not OUTPUT.exists()

    with OUTPUT.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new:
            writer.writeheader()

        print(f"Writing to {OUTPUT} every {INTERVAL_S}s. Ctrl+C to stop.")
        while True:
            try:
                row = sample()
            except requests.RequestException as exc:
                print(f"skipped: {exc}")
            else:
                writer.writerow(row)
                f.flush()
                print(f"{row['timestamp']}  gap {row['gap']:+.2f}")
            time.sleep(INTERVAL_S)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")
