# iperf Throughput Matrix — Results

Full 12-direction `iperf` matrix from `scripts\iperf_matrix_test.py`, run on
2026-09-01 against this bench's own bridge.
Raw, unabridged output (every rate step, every captured device report) is in
[`iperf_matrix_results.log`](iperf_matrix_results.log).

## Bench configuration used

| Node | Role | IP | Control |
|---|---|---|---|
| PC | test driver + real `iperf2` (`jperf-2.0.2\bin\iperf.exe`) | `192.168.0.100` | local |
| Bridge | this firmware | `eth0` (T1S) `192.168.0.11`, `eth1` (100BASE-T) `192.168.0.12` | COM8 |
| FollowerA | T1S follower node | `192.168.0.201` | COM10 |
| FollowerB | T1S follower node | `192.168.0.202` | COM23 |

Test parameters: UDP ascending search over `1, 2, 5, 8, 10, 20, 50, 80` Mbit/s
steps (3 s each), stopping at the first step exceeding 2.0% loss and
reporting the last clean one; TCP a single 5 s measurement. Every result is
read from the **destination's own report** (device `iperf -s` capture or the
PC's real `iperf2` server), never the sending client's self-reported figure
— see the script's docstring for why the client-side number is untrustworthy
for UDP.

## Results matrix

Rows = source, columns = destination. Each cell: UDP max clean rate / TCP.

| Source \ Destination | PC | Bridge (`eth1`) | FollowerA | FollowerB |
|---|---|---|---|---|
| **PC** | — | 79.92 Mbit/s / 20.73 Mbit/s | 7.98 Mbit/s / 5.38 Mbit/s | 7.98 Mbit/s / 5.42 Mbit/s |
| **Bridge** | 73.70 Mbit/s / 11.50 Mbit/s | — | 9.43 Mbit/s / 5.85 Mbit/s | 9.43 Mbit/s / 5.85 Mbit/s |
| **FollowerA** | 9.43 Mbit/s / 3.87 Mbit/s | 9.42 Mbit/s / 5.83 Mbit/s | — | 9.43 Mbit/s / 5.84 Mbit/s |
| **FollowerB** | 9.43 Mbit/s / 3.87 Mbit/s | 9.42 Mbit/s / 5.83 Mbit/s | 9.43 Mbit/s / 5.84 Mbit/s | — |

All UDP results measured at 0.0% loss at their reported rate (the search
either reached the top 80 Mbit/s step cleanly or stopped one step short of
the first step over the 2% loss threshold).

## Observations

- **PC ↔ Bridge (100BASE-T only, no T1S hop) is the fastest path**, as
  expected: UDP up to ~80 Mbit/s, TCP ~11–21 Mbit/s (asymmetric — PC→Bridge
  TCP noticeably outruns Bridge→PC, likely the embedded TCP stack's own
  send-side tuning rather than a bridge issue; not investigated further
  here).
- **Every path that crosses the T1S segment caps out around 9.4 Mbit/s
  UDP**, consistent with 10BASE-T1S's physical line rate and PLCA sharing
  overhead — this ceiling shows up identically for PC↔Follower,
  Bridge↔Follower, and Follower↔Follower.
- **PC → Follower UDP tops out at ~8 Mbit/s, one step lower than the ~9.4
  Mbit/s every other T1S-crossing direction reaches** — the same 10 Mbit/s
  step already shows 5% loss for PC→FollowerA/B, whereas Bridge→Follower and
  Follower→Follower both clear 9.4 Mbit/s at 0% loss. Only extra hop unique
  to the PC→Follower path is the 100BASE-T leg feeding into the bridge
  before the T1S segment; not root-caused here.
- **`Bridge -> FollowerA`/`Bridge -> FollowerB` TCP originally failed outright
  (0.00 Mbit/s)** in the first run, while the same direction's UDP test
  passed cleanly at 9.43 Mbit/s. Root-caused to `TC6_TX_ETH_MAX_SEGMENTS`
  being `1u` in the LAN865x TC6 driver (`tc6-conf.h`) — the bridge's own TCP
  stack links up to 3 buffer segments per packet when it originates real
  payload data (not just an ACK), which the driver rejected outright.
  Forwarded PC↔Follower traffic and bare ACKs (1 segment) were never
  affected — only the bridge acting as a TCP *client* toward a T1S node.
  Fixed 2026-09-01 (`1u → 3u`, matching the value the companion follower
  application already used; full write-up in
  [`mcc-generated-code-patches.md`](mcc-generated-code-patches.md) item 10).
  **Re-verified after rebuild/reflash:** both directions now report
  **5.85 Mbit/s TCP**, in line with every other T1S-crossing TCP direction,
  with eth0 `err=0` in `stats` afterward (was climbing before the fix).

*Full raw log: [`iperf_matrix_results.log`](iperf_matrix_results.log).*
