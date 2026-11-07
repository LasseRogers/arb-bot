"""Compare BTC prices between Hyperliquid and Binance."""

import binance
import hyperliquid


def main() -> None:
    hl = hyperliquid.get_mid_price("BTC")
    bn = binance.get_mid_price("BTCUSDT")
    spread = hl - bn

    print(f"Hyperliquid: {hl:>12,.2f}")
    print(f"Binance:     {bn:>12,.2f}")
    print(f"Spread:      {spread:>+12,.2f}  ({spread / bn:+.4%})")


if __name__ == "__main__":
    main()
