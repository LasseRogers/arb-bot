"""Stream live quotes for a Hyperliquid coin and a cTrader symbol and print the gap.

Hyperliquid is polled; cTrader pushes spot updates over the open connection.
"""

import argparse
from datetime import datetime, timezone

import anyio
import anyio.to_thread
from ctrader_api_client.events.types import SpotEvent

from arbbot.venues import ctrader, hyperliquid


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hl", default="PAXG", help="Hyperliquid coin")
    parser.add_argument("--ct", default="XAUUSD", help="cTrader symbol")
    parser.add_argument("--interval", type=float, default=5, help="seconds between lines")
    return parser.parse_args()


async def run(hl_coin: str, ct_symbol: str, interval: float) -> None:
    async with ctrader.connect() as (client, account_id):
        symbol_id = await ctrader.find_symbol_id(client, account_id, ct_symbol)
        ct = {"bid": None, "ask": None}

        @client.on(SpotEvent, symbol_id=symbol_id)
        async def on_spot(event: SpotEvent) -> None:
            # cTrader only sends the side that changed; keep the other one.
            if event.bid is not None:
                ct["bid"] = float(event.bid)
            if event.ask is not None:
                ct["ask"] = float(event.ask)

        await client.market_data.subscribe_spots(account_id, [symbol_id])
        print(f"streaming {ct_symbol} from cTrader, polling {hl_coin} on Hyperliquid. Ctrl+C to stop.")

        while True:
            bids, asks = await anyio.to_thread.run_sync(hyperliquid.get_book, hl_coin)
            hl_bid, hl_ask = bids[0][0], asks[0][0]
            now = datetime.now(timezone.utc).strftime("%H:%M:%S")
            if ct["bid"] is None or ct["ask"] is None:
                print(f"{now}  waiting for first {ct_symbol} price...")
            else:
                gap = (hl_bid + hl_ask) / 2 - (ct["bid"] + ct["ask"]) / 2
                print(f"{now}  {hl_coin} {hl_bid:.2f}/{hl_ask:.2f}   "
                      f"{ct_symbol} {ct['bid']:.2f}/{ct['ask']:.2f}   gap {gap:+.2f}")
            await anyio.sleep(interval)


def main() -> None:
    args = parse_args()
    try:
        anyio.run(run, args.hl, args.ct, args.interval)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
