"""Estimate what instant trading costs, from live Hyperliquid order books.

A round trip on one market (buy now, sell now) costs about
average buy price - average sell price. A gap trade does that on both
markets, so its cost is the sum. Figures are per one unit of the asset.
"""

import argparse

import hyperliquid


def fill_price(levels: list[tuple[float, float]], size: float) -> float | None:
    """Average price to fill `size` by sweeping the book, or None if it's too thin."""
    remaining, cost = size, 0.0
    for px, sz in levels:
        take = min(remaining, sz)
        cost += take * px
        remaining -= take
        if remaining <= 1e-12:
            return cost / size
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("coins", nargs="+", help="e.g. PAXG xyz:GOLD")
    parser.add_argument("--sizes", nargs="+", type=float, default=[0.1, 1], help="units to trade")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    books = {coin: hyperliquid.get_book(coin) for coin in args.coins}

    for size in args.sizes:
        print(f"\nsize {size:g}")
        total = 0.0
        for coin, (bids, asks) in books.items():
            buy, sell = fill_price(asks, size), fill_price(bids, size)
            if buy is None or sell is None:
                print(f"  {coin:<10} book too thin for this size")
                total = float("nan")
                continue
            print(
                f"  {coin:<10} best bid {bids[0][0]:.2f}  best ask {asks[0][0]:.2f}"
                f"  instant in+out {buy - sell:.2f}"
            )
            total += buy - sell
        print(f"  both markets   {total:.2f} per unit")


if __name__ == "__main__":
    main()
