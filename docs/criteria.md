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

Stopped 2026-11-09, restart 1 of 2. The frozen design can't pass: backtest cdf77a7 gives
6–8%/yr on $1,300 at 10.6% funding, and daily manual checks get liquidated on 2026-05-21.
The paper position stays open as a rehearsal (basis, funding vs model) and doesn't count.

Before any new clock:
- Gate: the walk-forward backtest passes ≥ 2 of 3 windows with no hard fail.
- With automated, price-triggered rebalancing the count limit goes; cost ≤ 25% of funding
  stays, budgeted at taker fees. Margin < $90 and liquidation stay hard fails.
- The 10%/yr pass is unchanged.

## Carry walk-forward scan

Fixed before any run. One primary configuration decides the gate; sensitivities
(2-week lookback, top 1, top 3) are reported, never picked afterwards.

| Setting | Value |
| --- | --- |
| Universe | HL perps with a USDC spot market on HL, wrapped names included |
| Volume | 28-day median: perp ≥ $10M/day, spot ≥ $1M/day |
| Listing | Funding history for the full lookback |
| Lookback | 28 days of funding, ≥ 70% of hours positive |
| Hold | 4 weeks, then re-pick |
| Switch | Challenger must beat the incumbent's score by 5 points/yr |
| Floor | Best score < 10%/yr: hold cash |
| Coins | Top 2, equal weight |
| Costs | Taker on every rotation and rebalance |

Every filter as of the pick date, from data available then.

Margin per coin: D = max(20%, 4 × trailing 28-day daily volatility);
m = maintenance + (1 + maintenance) × D; excluded if m > 50%. Automated rebalance when price
is D/2 above the last anchor. Score = trailing funding ÷ (1 + m), the return on total capital.

History: everything available; 4-hour candles for price and highs where hourly don't reach.
6–9 non-overlapping 8-week windows. Report eligible coins per window.

Gate for a new paper clock: ≥ 2/3 of windows pass, no liquidation in any. Must beat both
baselines: always HYPE, and equal weight across the universe.

Auto-deleveraging is not modelled: windows with a market-wide crash are flagged as optimistic.

## Both

- Evaluated independently, unless a shared cause (shared code, platform outage, ADL, fee change).
- Top 3 trades ≤ 50% of net; no session or weekday > 60%. Excluding a losing bucket requires a new test.
- Uptime ≥ 95%; zero unexplained state errors or lost trades.

## Clock

Gold count starts at bfb24c2 (2026-11-09 21:22 UTC), clean paper files, size 1 oz (hedge minimum).
Rule changes after this reset it. Reprice throttle and fast-move guard are rules: `Params` at that commit.

## Definitions

Adverse selection per leg, sign +1 long, −1 short, positive is a cost:
entry sign × (entry_gap − target_gap), exit sign × (exit_target − exit_gap).
$0.75 applies to each leg's mean separately. Closure exits are left out of the exit mean;
they still count in net and worst trade.

Request budget. Requests are places, modifies and cancels; volume is the coin leg, entry + exit.
- Sustainability: total requests ≤ 50% of volume. Fail above 100%.
- Path: `budget_peak` (max of requests − volume at any point) ≤ 5,000. Fail above 10,000.
Paper undercounts (rejected post-only orders, retries); compare with `userRateLimit` once live.
The HYPE carry runs on a separate sub-account so the budgets don't mix.

Net per trade is the `net` column of gold_trades.csv; `stats["net"]` is display only.
