"""Minimal client for Hyperliquid's public info API."""

import requests

INFO_URL = "https://api.hyperliquid.xyz/info"
TIMEOUT_S = 10


def get_mid_prices(dex: str = "") -> dict[str, float]:
    resp = requests.post(INFO_URL, json={"type": "allMids", "dex": dex}, timeout=TIMEOUT_S)
    resp.raise_for_status()
    return {coin: float(px) for coin, px in resp.json().items()}


def get_mid_price(coin: str) -> float:
    """Coins on builder-deployed dexes carry a prefix, e.g. 'xyz:GOLD'."""
    dex = coin.split(":", 1)[0] if ":" in coin else ""
    mids = get_mid_prices(dex)
    if coin not in mids:
        raise KeyError(f"{coin} not listed on Hyperliquid")
    return mids[coin]


if __name__ == "__main__":
    print(f"BTC mid: {get_mid_price('BTC'):,.2f}")
