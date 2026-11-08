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
