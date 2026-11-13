# Gold run log

Pinned at 6893b75, 2026-11-10 14:28 UTC. Every code version below reproduces the golden files.

| Start (UTC) | Code | Host | Why |
| --- | --- | --- | --- |
| 2026-11-10 14:28 | c7a2d82 | laptop | pin start, clean files |
| 2026-11-12 08:13 | 6b71c48 | laptop | after a planned stop |
| 2026-11-12 15:26 | 6b71c48 | laptop | after a planned stop |
| 2026-11-13 10:11 | ad67535 | Pi | moved after a laptop reboot; `data/paper` and `data/ticks` copied, open trade resumed |
| 2026-11-13 11:18 | a059dfd | Pi | systemd user service |
| 2026-11-13 11:20 | a059dfd | Pi | deliberate `kill -9`, crash restart test |
| 2026-11-13 11:21 | f4a73a4 | Pi | status lines to `data/paper/live.log` |

## Downtime

From `scripts/uptime.py`, as of 2026-11-13 12:12 UTC:

| Gap (UTC) | Minutes | Cause |
| --- | --- | --- |
| 2026-11-12 05:28 → 08:13 | 165 | laptop off |
| 2026-11-12 10:43 → 15:26 | 283 | laptop off |
| 2026-11-13 09:57 → 10:11 | 14 | laptop reboot, move to the Pi |
| 2026-11-13 11:17 → 11:18 | 1 | switch to the service |

7.7 h of 69.7 h, 88.9%. Allowance at 3 weeks is 25.2 h.

Trades open across the 2026-11-12 gaps had no resting exit while the bot was down and may have
closed later than they would have. Replay sees the same gaps, so parity is unaffected.
