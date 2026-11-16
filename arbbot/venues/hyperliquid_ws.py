"""Hyperliquid websocket feed: keep the latest best bid and ask for a coin."""

import json
import time
from collections.abc import Callable

import anyio
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

WS_URL = "wss://api.hyperliquid.xyz/ws"
PING_EVERY_S = 30  # Hyperliquid drops connections that stay silent for 60s
RECONNECT_WAIT_S = 2
RECONNECT_MAX_S = 60


async def _keepalive(ws) -> None:
    while True:
        await anyio.sleep(PING_EVERY_S)
        await ws.send(json.dumps({"method": "ping"}))


async def stream_top_of_book(coin: str, quote: dict,
                             on_quote: Callable[[float, float], None] | None = None) -> None:
    """Keep quote['bid'], quote['ask'] and quote['at'] (monotonic) current, forever.

    on_quote(bid, ask) is called on every book update. Reconnects after any network or
    protocol error (closed connection, refused handshake such as a 502, timeout), waiting
    longer after each failure until a book update arrives again.
    """
    wait = RECONNECT_WAIT_S
    subscribe = json.dumps({"method": "subscribe", "subscription": {"type": "l2Book", "coin": coin}})
    while True:
        try:
            async with connect(WS_URL) as ws:
                await ws.send(subscribe)
                async with anyio.create_task_group() as tg:
                    tg.start_soon(_keepalive, ws)
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("channel") != "l2Book":
                            continue
                        bids, asks = msg["data"]["levels"]
                        if bids and asks:
                            quote["bid"] = float(bids[0]["px"])
                            quote["ask"] = float(asks[0]["px"])
                            quote["at"] = time.monotonic()
                            wait = RECONNECT_WAIT_S
                            if on_quote is not None:
                                on_quote(quote["bid"], quote["ask"])
                    tg.cancel_scope.cancel()
        except* (OSError, TimeoutError, WebSocketException) as group:
            # anyio task groups raise exception groups; network errors just mean reconnect.
            print(f"  hyperliquid stream dropped ({group.exceptions[0]!r}), reconnecting in {wait:g}s")
        await anyio.sleep(wait)
        wait = min(wait * 2, RECONNECT_MAX_S)
