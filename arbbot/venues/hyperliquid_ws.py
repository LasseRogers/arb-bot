"""Hyperliquid websocket feed: keep the latest best bid and ask for a coin."""

import json
import time

import anyio
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

WS_URL = "wss://api.hyperliquid.xyz/ws"
PING_EVERY_S = 30  # Hyperliquid drops connections that stay silent for 60s
RECONNECT_WAIT_S = 2


async def _keepalive(ws) -> None:
    while True:
        await anyio.sleep(PING_EVERY_S)
        await ws.send(json.dumps({"method": "ping"}))


async def stream_top_of_book(coin: str, quote: dict) -> None:
    """Keep quote['bid'], quote['ask'] and quote['at'] (monotonic) current, forever.

    Reconnects after network errors or when the server closes the connection.
    """
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
                    tg.cancel_scope.cancel()
        except* (OSError, ConnectionClosed) as group:
            # anyio task groups raise exception groups; network errors just mean reconnect.
            print(f"  hyperliquid stream dropped ({group.exceptions[0]!r}), reconnecting")
        await anyio.sleep(RECONNECT_WAIT_S)
