import json

import anyio
import pytest
from websockets.exceptions import InvalidHandshake

from arbbot import retry
from arbbot.venues import hyperliquid_ws


class FakeWs:
    def __init__(self):
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, msg):
        self.sent.append(msg)

    def __aiter__(self):
        return self._messages()

    async def _messages(self):
        yield json.dumps({"channel": "l2Book", "data": {"levels": [[{"px": "100.0"}], [{"px": "100.1"}]]}})
        await anyio.sleep(3600)


def test_stream_survives_refused_handshakes(monkeypatch):
    attempts = []

    def connect(url):
        attempts.append(url)
        if len(attempts) <= 2:
            raise InvalidHandshake("server rejected WebSocket connection: HTTP 502")
        return FakeWs()

    monkeypatch.setattr(hyperliquid_ws, "connect", connect)
    monkeypatch.setattr(hyperliquid_ws, "RECONNECT_WAIT_S", 0.01)
    quote = {"bid": None, "ask": None, "at": 0.0}

    async def main():
        with anyio.move_on_after(2):
            async with anyio.create_task_group() as tg:
                tg.start_soon(hyperliquid_ws.stream_top_of_book, "PAXG", quote)
                while quote["bid"] is None:
                    await anyio.sleep(0.01)
                tg.cancel_scope.cancel()

    anyio.run(main)
    assert len(attempts) == 3 and quote["bid"] == 100.0 and quote["ask"] == 100.1


def test_startup_waits_out_an_outage(monkeypatch):
    calls = []
    monkeypatch.setattr(retry.time, "sleep", lambda s: calls.append(s))

    def flaky():
        if len(calls) < 3:
            raise ConnectionError("502 Bad Gateway")
        return 42

    assert retry.until_ok("price", flaky) == 42
    assert calls == [5, 10, 20]
