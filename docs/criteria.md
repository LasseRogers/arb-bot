# Pass/fail criteria

Frozen 2026-11-09. Changing a threshold or a strategy rule resets that strategy's count;
max two restarts each. Results between pass and fail: extend or change one rule and restart.
Re-evaluate on after-tax numbers once SKAT treatment is confirmed.

## Gold gap (hl:PAXG vs ctrader:XAUUSD)

| Metric | Pass | Fail |
| --- | --- | --- |
| Sample | ≥ 50 closed trades, ≥ 3 weeks | < 25 trades after 6 weeks |
| Entry fill rate | ≥ 40% | < 25% |
| Mean adverse selection (signal gap − fill gap) | ≤ $0.75 | > $1.50 |
| Mean net per trade (fees, swap, funding) | ≥ $1.00/oz, t ≥ 2 | ≤ $0.50/oz or t < 1 |
| Mean net with +$0.50/trade cost | > 0 | ≤ 0 |
| Worst trade | ≥ −$10/oz | < −$15/oz |
| Max drawdown | ≤ $20/oz and ≤ 50% of final net | either exceeded |
| Trades open > 72h | ≤ 10% | > 20% |
| Positive time-thirds | ≥ 2/3 | 1/3 |

Re-estimate n = (2·sd / mean)² after 20 trades.

Backtest parity, same dates and parameters:

- Signal count within ±20% of paper entry orders; otherwise fix the implementation first.
- Capture ratio (paper net / backtest net): pass ≥ 50%, stop < 30%, else rework order placement.

Latency replay: net/trade at 0.5 s hedge delay ≥ 70% of zero-delay; > 0 at 2 s.

## HYPE carry (spot long + perp short, $1,000 + $300 margin)

Min 8 weeks; no annualised figures before week 4. Weekly totals only.

| Metric | Pass | Fail |
| --- | --- | --- |
| Net return on $1,300 | ≥ 10%/yr (≥ $20 / 8 wk) | < 5%/yr (< $10 / 8 wk) |
| Positive weeks | ≥ 6/8 | |
| Worst week | ≥ −$5 | |
| Five worst hours vs gross funding | ≤ 30% | |
| Hedge P&L (spot + perp) | within ±$5 | |

In between: extend to 12 weeks. Report funding and hedge P&L separately.

Rebalance when margin < $150, fees included. Hard fails regardless of funding:

- margin < $90 at any check
- hourly perp high ≥ entry × 1.25 between checks without a rebalance
- > 3 rebalances, or rebalance cost > 25% of funding

## Both

- Evaluated independently, unless a shared cause (shared code, platform outage, ADL, fee change).
- Top 3 trades ≤ 50% of net; no session or weekday > 60%. Excluding a losing bucket requires a new test.
- Uptime ≥ 95%; zero unexplained state errors or lost trades.

## Clock

Gold count starts at 5504dc6 (2026-11-09 21:09 UTC). Rule changes after this reset it.

Adverse selection per leg: sign × (entry_gap − target_gap) on entry, sign × (exit_target − exit_gap)
on exit; sign +1 long, −1 short. Positive is a cost. Logged as entry_slippage / exit_slippage.
