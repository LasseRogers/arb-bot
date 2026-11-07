"""Minimal client for Binance's public spot market data API."""

import requests

BASE_URL = "https://api.binance.com"
TIMEOUT_S = 10


def get_book_ticker(symbol: str) -> tuple[float, float]:
    """Return (best bid, best ask) for a spot symbol such as 'BTCUSDT'."""
    resp = requests.get(
        f"{BASE_URL}/api/v3/ticker/bookTicker",
        params={"symbol": symbol},
        timeout=TIMEOUT_S,
    )
    resp.raise_for_status()
    data = resp.json()
    return float(data["bidPrice"]), float(data["askPrice"])


def get_mid_price(symbol: str) -> float:
    bid, ask = get_book_ticker(symbol)
    return (bid + ask) / 2


if __name__ == "__main__":
    bid, ask = get_book_ticker("BTCUSDT")
    print(f"BTC bid: {bid:,.2f}  ask: {ask:,.2f}  mid: {(bid + ask) / 2:,.2f}")
