"""Minimal client for Hyperliquid's public info API."""

import math
import requests

INFO_URL = "https://api.hyperliquid.xyz/info"
TIMEOUT_S = 10


def _post(payload: dict) -> list | dict:
    resp = requests.post(INFO_URL, json=payload, timeout=TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()


def get_mid_prices(dex: str = "") -> dict[str, float]:
    return {coin: float(px) for coin, px in _post({"type": "allMids", "dex": dex}).items()}


def get_mid_price(coin: str) -> float:
    """Coins on builder-deployed dexes carry a prefix, e.g. 'xyz:GOLD'."""
    dex = coin.split(":", 1)[0] if ":" in coin else ""
    mids = get_mid_prices(dex)
    if coin not in mids:
        raise KeyError(f"{coin} not listed on Hyperliquid")
    return mids[coin]


def get_candles(coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    """OHLCV candles between two epoch-ms timestamps. At most 5000 per call."""
    return _post({
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms},
    })


def get_book(coin: str) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Order book as (bids, asks), each a list of (price, size), best first."""
    bids, asks = _post({"type": "l2Book", "coin": coin})["levels"]
    return (
        [(float(lvl["px"]), float(lvl["sz"])) for lvl in bids],
        [(float(lvl["px"]), float(lvl["sz"])) for lvl in asks],
    )


def get_funding_history(coin: str, start_ms: int, end_ms: int) -> list[dict]:
    """Hourly funding records between two epoch-ms timestamps, oldest first.

    Positive fundingRate means longs pay shorts.
    """
    records: list[dict] = []
    while start_ms < end_ms:
        batch = _post(
            {"type": "fundingHistory", "coin": coin, "startTime": start_ms, "endTime": end_ms}
        )
        if not batch:
            break
        records.extend(batch)
        last = batch[-1]["time"]
        if last <= start_ms:
            break
        start_ms = last + 1
    return records


def get_perp_dexs() -> list[str]:
    """Names of builder-deployed perp dexes, e.g. ['xyz', 'flx', ...]."""
    return [d["name"] for d in _post({"type": "perpDexs"}) if d]


def get_markets(dex: str = "") -> list[dict]:
    """Every market on a dex with its current funding, price and daily volume."""
    meta, ctxs = _post({"type": "metaAndAssetCtxs", "dex": dex})
    markets = []
    for asset, ctx in zip(meta["universe"], ctxs):
        if asset.get("isDelisted"):
            continue
        markets.append({
            "coin": asset["name"],
            "funding": float(ctx["funding"]),
            "mark": float(ctx["markPx"]),
            "volume": float(ctx["dayNtlVlm"]),
        })
    return markets


def get_spot_markets() -> dict[str, dict]:
    """USDC-quoted spot pairs keyed by base token name, with mid price and 24h volume."""
    meta, ctxs = _post({"type": "spotMetaAndAssetCtxs"})
    tokens = {t["index"]: t["name"] for t in meta["tokens"]}
    ctx_by_pair = {c["coin"]: c for c in ctxs}
    markets = {}
    for pair in meta["universe"]:
        base, quote = (tokens[i] for i in pair["tokens"])
        ctx = ctx_by_pair.get(pair["name"])
        if quote != "USDC" or ctx is None:
            continue
        markets[base] = {
            "pair": pair["name"],
            "mid": float(ctx.get("midPx") or ctx["markPx"]),
            "volume": float(ctx["dayNtlVlm"]),
        }
    return markets


if __name__ == "__main__":
    print(f"BTC mid: {get_mid_price('BTC'):,.2f}")


def price_tick(coin: str, price: float) -> float:
    """Smallest valid price step for a main-dex perp near `price`.

    Hyperliquid allows 5 significant figures and at most 6 - szDecimals decimals.
    """
    universe = _post({"type": "meta"})["universe"]
    sz_decimals = next(a["szDecimals"] for a in universe if a["name"] == coin)
    significant = 10.0 ** (math.floor(math.log10(price)) - 4)
    return max(significant, 10.0 ** -(6 - sz_decimals))
