# arb-bot

Paper trading of the PAXG (Hyperliquid) / XAUUSD (cTrader) gap. No live orders.
Pass/fail is fixed in [`docs/criteria.md`](docs/criteria.md).

`arbbot/gap/strategy.py` holds the rules; `tests/test_golden.py` fails on any behaviour change.
`arbbot.gap.replay` checks paper fills against recorded ticks.

`.env`: cTrader Open API credentials (see `arbbot/venues/ctrader.py`), `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`.

## Deploy

Runs as a daemon under the systemd user manager with linger, so it survives reboots. Stopped
with SIGINT to flush state. Daily report and 15-minute health check go to Telegram.

```bash
cp deploy/systemd/gold-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now gold-bot.service
bash deploy/install_timers.sh
```

Output goes to `data/paper/live.log`.
