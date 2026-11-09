"""Minimal cTrader Open API access for one account, configured from .env."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import anyio
from dotenv import load_dotenv

from ctrader_api_client import ClientConfig, CTraderClient
from ctrader_api_client.auth.credentials import AccountCredentials
from ctrader_api_client.enums import TrendbarPeriod
from ctrader_api_client.exceptions import APIError

DEMO_HOST = "demo.ctraderapi.com"
LIVE_HOST = "live.ctraderapi.com"


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing {name} in .env")
    return value


@asynccontextmanager
async def connect(live: bool = False) -> AsyncIterator[tuple[CTraderClient, int]]:
    """Yield an authorised client and the account id for CTRADER_TRADER_LOGIN."""
    load_dotenv()
    config = ClientConfig(
        host=LIVE_HOST if live else DEMO_HOST,
        client_id=_env("CTRADER_CLIENT_ID"),
        client_secret=_env("CTRADER_CLIENT_SECRET"),
    )
    access_token = _env("CTRADER_ACCESS_TOKEN")

    client = CTraderClient(config)
    async with client:
        account_id = await client.accounts.resolve_account_id(
            access_token, trader_login=int(_env("CTRADER_TRADER_LOGIN"))
        )
        await client.auth.authenticate_trader(
            AccountCredentials(
                account_id=account_id,
                access_token=access_token,
                refresh_token=_env("CTRADER_REFRESH_TOKEN"),
                expires_at=int(_env("CTRADER_TOKEN_EXPIRES_AT")),
            )
        )
        yield client, account_id


async def find_symbol_id(client: CTraderClient, account_id: int, name: str) -> int:
    for info in await client.symbols.search(account_id, name):
        if info.name == name:
            return info.symbol_id
    raise KeyError(f"{name} not found on this account")


PERIODS = {
    "1m": TrendbarPeriod.M1,
    "5m": TrendbarPeriod.M5,
    "15m": TrendbarPeriod.M15,
    "1h": TrendbarPeriod.H1,
}
REQUEST_SPAN = timedelta(days=2)
REQUEST_PAUSE_S = 0.5
RATE_LIMIT_RETRIES = 5


async def _trendbars_with_retry(client, account_id, symbol_id, period, start, end):
    """Historical data is rate limited; back off and retry when told to slow down."""
    for attempt in range(RATE_LIMIT_RETRIES):
        try:
            return await client.market_data.get_trendbars(account_id, symbol_id, period, start, end)
        except APIError as exc:
            if not exc.is_rate_limited() or attempt == RATE_LIMIT_RETRIES - 1:
                raise
            await anyio.sleep(2 ** attempt)


async def fetch_closes(symbol: str, start: datetime, end: datetime, interval: str) -> dict[datetime, float]:
    """Close of every bar between start and end, keyed by bar open time (UTC)."""
    async with connect() as (client, account_id):
        symbol_id = await find_symbol_id(client, account_id, symbol)
        closes: dict[datetime, float] = {}
        t = start
        while t < end:
            bars = await _trendbars_with_retry(
                client, account_id, symbol_id, PERIODS[interval], t, min(t + REQUEST_SPAN, end)
            )
            for bar in bars:
                closes[bar.timestamp] = float(bar.close)
            t += REQUEST_SPAN
            await anyio.sleep(REQUEST_PAUSE_S)
        return closes


def get_closes(symbol: str, start: datetime, end: datetime, interval: str) -> dict[datetime, float]:
    return anyio.run(fetch_closes, symbol, start, end, interval)


async def main() -> None:
    async with connect() as (client, account_id):
        account = await client.accounts.get_trader(account_id)
        print(f"account {account.trader_login}  broker {account.broker_name}  balance {account.balance}")

        symbol_id = await find_symbol_id(client, account_id, "XAUUSD")
        symbol = await client.symbols.get_by_id(account_id, symbol_id)
        print(f"XAUUSD id {symbol_id}  lot size {symbol.lot_size}  digits {symbol.digits}")
        print(f"commission {symbol.commission}")
        print(f"swap {symbol.swap}")
        print(f"open now: {symbol.trading_hours.is_open()}")

        end = datetime.now(timezone.utc)
        bars = await client.market_data.get_trendbars(
            account_id, symbol_id, TrendbarPeriod.M1, end - timedelta(days=3), end
        )
        print(f"\n{len(bars)} one-minute bars, last five:")
        for bar in bars[-5:]:
            print(f"  {bar.timestamp}  close {bar.close}")


if __name__ == "__main__":
    anyio.run(main)
