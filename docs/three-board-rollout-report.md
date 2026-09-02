# One Firmware on Three Boards — Rollout and Test Report

**Date:** 2026-09-02 · **Firmware:** build `Sep  2 2026 21:53:50`, branch
`survive-missing-phy` · **Bench:** three SAM E54 Curiosity Ultra boards, one
T1S segment, one PC

The same bridge firmware image was deployed to all three boards on the bench —
including one that has no 100BASE-TX PHY at all — and the resulting setup was
tested for reachability, throughput and capture completeness. This documents
what was configured, what was measured, and the three defects the run exposed.

---

## 1. Why this was possible at all

Two of the three boards had been running a separate single-interface follower
firmware. The third, populated with only the LAN8651 T1S MAC-PHY, could not run
the bridge firmware before: a MAC/PHY that is not physically fitted aborted the
entire TCP/IP stack, taking the healthy T1S side down with it. That is fixed by
`patches/tcpip_manager.patch` — see item 11 in
[`mcc-generated-code-patches.md`](mcc-generated-code-patches.md).

Only the third board needs the patch. A board whose PHY is present but whose
cable is unplugged was never affected: the PHY is detected, the interface comes
up, and only the link stays down.

---

## 2. Configuration

| Board | Probe | eth0 (T1S) | eth1 (100BASE-TX) | PLCA id | 100BASE-TX hardware |
|---|---|---|---|---|---|
| COM8 | `ATML3264031800001049` | `192.168.0.11` | `192.168.0.12` | 7 | PHY + cable |
| COM10 | `ATML3264031800001290` | `192.168.0.201` | `192.168.0.210` | **0** (coordinator) | PHY, no cable |
| COM23 | `ATML3264031800001103` | `192.168.0.202` | `192.168.0.220` | 1 | **no PHY** |

PC on `192.168.0.100`, wired to COM8's RJ45. PLCA node count 8 on all three,
exactly one coordinator.

Each board was flashed and then configured over its own serial CLI
(`setenv ip0/ip1/plca_id` + `saveenv`), one board at a time. That order matters:
a freshly flashed board finds no matching EEPROM record and falls back to the
compiled defaults — `192.168.0.11` and PLCA id 5 — so flashing all three first
would put three identical nodes on the bus at once.

### Boot output

COM8 and COM10 report both interfaces:

```
TCP/IP Stack: Initialization Ended - success
eth0 (10BASE-T1S) : up
eth1 (100BASE-TX) : up
[PLCA] node ID set to 7 (NODE_CNT=8, reg=0x00000807)
```

COM23, with no 100BASE-TX PHY, comes up on one interface and says so:

```
DRV_PHY operation error: -1
DRV PHY init failed: -1
TCP/IP Stack: Initialization Ended - success
eth0 (10BASE-T1S) : up
eth1 (100BASE-TX) : NOT AVAILABLE
Bridging is DISABLED - it needs both interfaces.
Continuing on the surviving interface; check the PHY/daughter board.
```

Without the patch the third line reads
`Interface Initialization failed: 0x1 - Aborting!` and nothing else works.

---

## 3. Reachability

### From the PC (`192.168.0.100`)

| Target | Result |
|---|---|
| `.11` COM8 eth0 | reachable |
| `.12` COM8 eth1 | reachable |
| `.201` COM10 eth0 | reachable |
| `.210` COM10 eth1 | **reachable**, although no cable is plugged into it |
| `.202` COM23 eth0 | reachable |
| `.220` COM23 eth1 | **no reply** — the interface does not exist |

Five of six. The one failure is exactly the interface with no hardware behind
it. `.210` answering is not a fluke: COM10 bridges its own eth0 and eth1, so a
packet arriving over T1S for its eth1 address is delivered locally.

### Between boards

Every board reaches every other board's T1S address, and both followers reach
the PC through COM8's bridge. Two directions fail reproducibly (measured three
times each):

| Direction | Result |
|---|---|
| `COM8 → 192.168.0.100` (the PC) | 0 of 4 — although COM10 and COM23 reach the PC *through* COM8 |
| `COM23 → 192.168.0.12` (COM8 eth1) | 0 of 4 — although `COM23 → .11` works and COM10 reaches `.12` |

Both senders have two interfaces in one `/24`, and in both cases the failing
target sits on the other interface's side. This is the same class of problem
`iperf_matrix_test.py` already documents for the bridge's iperf client, which
picks "the default interface" unless pinned with `iperfi`. **Stated as an
observation, not a proven cause** — no interface-selection trace was taken.

---

## 4. Throughput matrix

`scripts/iperf_matrix_test.py`, UDP rate search over 1–80 Mbit/s with a 2 %
loss threshold, then one TCP measurement. UDP loss always read from the
receiving side.

| Direction | UDP max | TCP |
|---|---|---|
| PC → Bridge | 79.46 Mbit/s, 0.0 % | 19.79 Mbit/s |
| Bridge → PC | 72.80 Mbit/s, 0.0 % | 11.70 Mbit/s |
| PC → FollowerA | 7.96 Mbit/s, 0.0 % | 5.70 Mbit/s |
| PC → FollowerB | 8.00 Mbit/s, 0.0 % | 5.56 Mbit/s |
| Bridge → FollowerA | 9.42 Mbit/s, 0.0 % | 5.84 Mbit/s |
| Bridge → FollowerB | 9.44 Mbit/s, 0.0 % | 5.85 Mbit/s |
| FollowerA → PC | 9.44 Mbit/s, 0.0 % | 3.90 Mbit/s |
| FollowerA → Bridge | 9.42 Mbit/s, 0.0 % | 5.84 Mbit/s |
| FollowerA → FollowerB | 9.43 Mbit/s, 0.0 % | 5.85 Mbit/s |
| FollowerB → PC | 9.44 Mbit/s, 0.0 % | 3.89 Mbit/s |
| FollowerB → FollowerA | 9.42 Mbit/s, 0.0 % | 5.83 Mbit/s |
| FollowerB → Bridge | **FAIL** | **FAIL** |

Eleven of twelve directions pass, with zero UDP loss throughout. Every path
that crosses the T1S segment lands at 9.4 Mbit/s, the physical ceiling of
10BASE-T1S minus PLCA overhead — identical whether the traffic involves the
bridge or runs directly between two followers, and identical for the board
without a 100BASE-TX PHY.

The PC → Follower direction tops out one step lower, at ~8 Mbit/s. The only
hop unique to that path is the extra 100BASE-TX leg feeding into the bridge;
not investigated further.

`FollowerB → Bridge` is the genuine failure and matches the ping result: COM23
cannot reach `192.168.0.12`. `FollowerB → FollowerA` initially reported FAIL
too, but as a **follow-on effect** — see defect 2 below; re-run in isolation it
delivers the 9.42 Mbit/s in the table.

---

## 5. Sniffer capture validation

`scripts/sniffer_capture_test.py --udp-rate 10 --duration 5`: `sniffer` enabled
on COM8, real traffic driven **directly between COM10 and COM23**, tshark
recording on the PC's NIC. With sniffer on, COM8's own T1S transmitter is
disabled — it is a passive tap and not a participant in this traffic.

| Test | Verdict |
|---|---|
| UDP FollowerA → FollowerB | **COMPLETE** — every sent sequence id present |
| UDP FollowerB → FollowerA | **COMPLETE** — 4024 datagrams captured, 4023 sent |
| TCP FollowerA → FollowerB | **COMPLETE** — 99.9 % of expected bytes |
| TCP FollowerB → FollowerA | **COMPLETE** — 100.2 % of expected bytes |

The UDP runs asked for 10 Mbit/s and the link delivered 9.44 Mbit/s at 0 %
loss, i.e. the mirror path was exercised at the segment's ceiling, not at a
comfortable fraction of it.

Mirror counters on COM8 for the whole run:

```
dbg: rx_hook=18081 passed_filter=18081 pool_empty=0 no_eth1=0 tx_submitted=18081
dbg: ack_ok=18081 ack_fail=0 last_ack_res=0 max_len_submitted=1514 max_len_ok=1514
dbg: truncated=13044 (frames cut to 1514 bytes before mirroring)
```

18081 frames seen, 18081 mirrored, 18081 confirmed sent by the GMAC. No buffer
exhaustion, no missing destination interface, no failed transmit.

13044 frames were cut to `MIRROR_SAFE_FRAME_LEN`, yet no datagram went missing
and no captured frame was shorter than its own IP/UDP header claimed. Consistent
with the truncation removing only trailing bytes past the IP payload rather than
payload itself — plausible but not separately proven here.

Turning sniffer off restored the transmitter with the readback confirmation the
firmware now performs:

```
[SNIFFER] T1S transmitter enabled - CONFIRMED by readback of T1SPMACTL.TXD
```

---

## 6. Defects found

**1. A host with two interfaces in one subnet can pick the wrong one.**
`COM8 → 192.168.0.100` and `COM23 → 192.168.0.12` fail reproducibly while the
reverse direction and every other node work. Both failing targets sit on the
sender's *other* interface side. Impact: one of twelve throughput directions
cannot be measured at all. Already known for the iperf client, where
`iperfi` exists as the workaround; no equivalent for `ping`.

**2. A hung iperf session cannot be cleared, and poisons the next test.**
`FollowerB → Bridge` targets `.12`, which COM23 cannot reach, so the session
waits forever on an ARP that never resolves — the failure mode
`iperf_matrix_test.py` warns about in its own docstring. `iperfk` then answers
`trying to stop iperf instance 0...` and never completes, and the following
test aborts with `All instances busy. Retry later!`. Only a board reset clears
it. That is why two directions showed FAIL when only one is genuinely broken.

**3. `iperf_matrix_test.py` assumes only the bridge has two interfaces.**
Its `bind_ip` is set for the `Bridge` source only, with the comment "only the
Bridge has more than one interface". After this rollout all three nodes have
two, so follower sources are never pinned with `iperfi`. Not proven to have
caused a failure here — after a reset, COM23 chose eth0 correctly on its own —
but the assumption the script documents is no longer true of this bench.

---

## 7. What this rollout does and does not buy

The same image now runs on all three boards, which removes a whole class of
"which firmware is on this board" confusion, and the T1S side behaves
identically on all three — 9.4 Mbit/s UDP, 0 % loss, in every direction that
crosses the segment.

What the board without a 100BASE-TX PHY does **not** get is bridging, or the
mirror/sniffer feature, both of which need the second port. What it does keep
is everything that made the abort so costly: the stack, the console, counters,
Telnet, and LAN865x register access including PLCA and the IEEE test modes.

Two boards also lost a capability in the swap: they previously ran a PTP
follower firmware that steers a wall clock from Sync/Follow_Up messages. The
bridge firmware has no equivalent. Restoring it means reflashing
`T1S_Follower.hex` and re-entering that board's `env`.
