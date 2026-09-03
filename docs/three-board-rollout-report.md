# One Firmware on Three Boards — Rollout and Test Report

**Dates:** 2026-09-02 (phase 1) and 2026-09-03 (phase 2) · **Firmware:** build
`Sep  2 2026 21:53:50`, branch `survive-missing-phy` · **Bench:** three SAM E54
Curiosity Ultra boards, one T1S segment, one PC

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

A board whose PHY is present but whose cable is unplugged was never affected:
the PHY is detected, the interface comes up, and only the link stays down.

**The patch covers either PHY going missing, and both cases are measured.**
Pulling the LAN8651 Click board off the bridge produces the mirror image of the
100BASE-TX case — the SPI bus reads back all ones, because nothing is answering:

```
TCP/IP Stack: Initialization Started
Invalid MACPHY, oui=0x3FFFFF, model=0x3FF
TCP/IP Stack: Initialization Ended - success
eth0 (10BASE-T1S) : NOT AVAILABLE
eth1 (100BASE-TX) : up
Bridging is DISABLED - it needs both interfaces.
```

The board then ran as a plain 100BASE-TX node: `PC → .12` 3 of 3, `COM8 → PC`
4 of 4, console and Telnet unaffected. `.11`, the address of the interface that
is gone, is unreachable, and LAN865x register access answers `result=-5` —
that driver is torn down with its interface, the same limitation the
100BASE-TX case shows in reverse. With the T1S PHY removed the other two boards
also lose their only path to the PC, as expected.

This matters because the two failures travel through different drivers —
DRV_GMAC plus DRV_ETHPHY for eth1, DRV_LAN865X over SPI/TC6 for eth0 — and
report through the same `MAC_Status` contract. The single assignment in the
patch is enough for both.

---

## 2. Configuration

The bench was set up twice. Phase 1 kept the addresses the boards had carried
as followers; phase 2 started from fully erased MCUs to see what the firmware
seeds on its own, and re-addressed the boards into a tidier block. **Every
measurement reported below — sections 3, 4 and 5 — is from phase 2**; phase 1 is
kept here because the erase-and-reseed step in between is itself a result
(see [below](#what-a-fully-erased-board-writes-into-the-eeprom-by-itself)), and
because section 4 compares the two runs where they disagree. Only the labels
differ — the topology and the hardware are the same throughout.

**Phase 1**

| Board | Probe | eth0 (T1S) | eth1 (100BASE-TX) | PLCA id | 100BASE-TX hardware |
|---|---|---|---|---|---|
| COM8 | `ATML3264031800001049` | `192.168.0.11` | `192.168.0.12` | 7 | PHY + cable |
| COM10 | `ATML3264031800001290` | `192.168.0.201` | `192.168.0.210` | **0** (coordinator) | PHY, no cable |
| COM23 | `ATML3264031800001103` | `192.168.0.202` | `192.168.0.220` | 1 | **no PHY** |

**Phase 2** — after a full chip erase of all three (`flash_same54.py --erase`,
which wipes the emulated EEPROM as well):

| Board | Role | eth0 (T1S) | eth1 (100BASE-TX) | PLCA id |
|---|---|---|---|---|
| COM8 | bridge | `192.168.0.11` | `192.168.0.12` | 5 (seeded default) |
| COM10 | **A** | `192.168.0.21` | `192.168.0.22` | **0** (coordinator) |
| COM23 | **B** | `192.168.0.31` | `192.168.0.32` | **1** |

PC on `192.168.0.100`, wired to COM8's RJ45. PLCA node count 8 on all three,
exactly one coordinator.

Each board was flashed and then configured over its own serial CLI
(`setenv ip0/ip1/plca_id` + `saveenv`), one board at a time. That order matters:
a freshly flashed board finds no matching EEPROM record and falls back to the
compiled defaults — `192.168.0.11` and PLCA id 5 — so flashing all three first
would put three identical nodes on the bus at once.

### What a fully erased board writes into the EEPROM by itself

Phase 2 confirmed on hardware what `env.c` promises in code: `ENV_Init()`
formats the blank region and immediately persists a record, with no `saveenv`
needed. Read back from COM8 on its first boot after the erase:

| Field | eth0 | eth1 |
|---|---|---|
| IP | `192.168.0.11` | `192.168.0.12` |
| Mask | `255.255.255.0` | `255.255.255.0` |
| Gateway | `192.168.0.1` | **`0.0.0.0`** |
| DNS | `192.168.0.1` | `192.168.0.1` |

Plus PLCA id 5 / count 8, `mirror` and `sniffer` off, magic `TIBR`, version 5.
The MAC does not come from `configuration.h` at all but from
`SAME54_SERIAL_WORD0` — OUI `00:04:25` plus three serial bytes, eth1 being eth0
with the last byte incremented. That is why three freshly erased boards do not
collide on MAC addresses, only on everything else.

The eth1 gateway becoming `0.0.0.0` rather than `192.168.0.1` is the visible
effect of the known typo in `TCPIP_NETWORK_DEFAULT_GATEWAY_IDX1`
(`"192.168.0..1"`, a double dot): the string fails to parse and the field keeps
the zero it was initialised with. The typo is not cosmetic.

### Boot output

Captured on 2026-09-03. Each interface line now names the part fitted on it and
its silicon revision, read from the hardware at boot rather than taken from the
configuration — `DEVID` over the TC6 SPI link for eth0, the clause-22 PHY ID
over MDIO for eth1. COM8 and COM10 report both:

```
TCP/IP Stack: Initialization Ended - success
Build Timestamp   : Sep  3 2026 17:48:07
eth0 (10BASE-T1S) : up    LAN8651 rev 1  (DEVID 0x00086511)
eth1 (100BASE-TX) : up    Microchip LAN8740A model 0x11 rev 1  (OUI 0x0001F0, PHYID 0007:C111)
[PLCA] node ID set to 5 (NODE_CNT=8, reg=0x00000805)
```

COM23, with no 100BASE-TX PHY, comes up on one interface and says so:

```
DRV_PHY operation error: -1
DRV PHY init failed: -1
TCP/IP Stack: Initialization Ended - success
Build Timestamp   : Sep  3 2026 17:48:07
eth0 (10BASE-T1S) : up    LAN8651 rev 1  (DEVID 0x00086511)
eth1 (100BASE-TX) : NOT AVAILABLE
Bridging is DISABLED - it needs both interfaces.
Continuing on the surviving interface; check the PHY/daughter board.
```

Without the patch the line after the build timestamp reads
`Interface Initialization failed: 0x1 - Aborting!` and nothing else works.

All three boards report the same LAN8651 revision, and the two that have a
100BASE-TX card report the same PHY ID, so the parts are identical across the
bench. COM10 is the instructive case: its PHY answers with its ID even though no
cable is plugged in, because MDIO does not depend on link — which is what
separates "PHY missing" from "cable missing" at a glance.

---

## 3. Reachability — full ping matrix

Measured in phase 2, four sources against all seven addresses. `Sent 0` means
the board produced no packet at all; a plain cross means it transmitted and got
nothing back.

| from ↓ / to → | `.100` PC | `.11` | `.12` | `.21` | `.22` | `.31` | `.32` |
|---|---|---|---|---|---|---|---|
| **PC** | — | no | **yes** | **yes** | no | **yes** | no |
| **COM8** (bridge) | yes* | yes | yes | yes* | yes* | yes* | no |
| **COM10** (A) | yes | yes | yes | yes | `Sent 0` | yes | no |
| **COM23** (B) | yes | yes | `Sent 0` | yes | yes | yes | no |

`*` marks a target the bridge only reaches when the interface is named — see
below; the earlier reading of this row left that out and understated it.

**Board to board is essentially complete: 16 of 21 combinations answer**, and
every one that does not is accounted for below. Every path across the T1S
segment and straight through both bridges works.

### The two `Sent 0` cells are the interesting ones

`COM23 → .12` produces no packet, while `COM23 → .32` does transmit (and gets no
answer). That pair is the proof for [defect 1](#6-defects-found): COM23 holds
`.12` as one of *its own* addresses — the compiled default its eth1 was given at
initialisation and can never leave — whereas the `.32` it was configured with
exists only in the `env` record and never reached the stack.

`COM10 → .22` behaves the same way for the same reason at one remove: `.22` is
its own eth1 address, and that interface has a PHY but no cable, so there is no
link to send on. Where the interface does have a link, pinging one's own address
works — `COM8 → .12` answers 4 of 4. The rule is: a ping to a local address
succeeds only if that interface is linked.

### From the PC, each board answers on one address only

`.12` for COM8, `.21` for A, `.31` for B — in each case the interface the
request arrives through. The counterparts `.11` and `.22` stay silent even
though ARP resolves for them; the PC's ARP table holds the correct MACs. Two
runs in the same session gave identical results, but phase 1 with its different
addressing *did* reach the equivalent of `.11`, so this is reproducible within a
configuration and not across configurations. Not root-caused.

### Naming the interface is mandatory on the bridge, and the default is not stable

The bridge's `ping` has to be told which interface to use, because both of its
interfaces sit in one subnet. That much was clear from the start. What the
re-test on 2026-09-03 added is that **which** interface it picks by default is
not fixed:

| | reaching the PC (`.100`) | reaching a T1S peer (`.21`) |
|---|---|---|
| earlier run | only with `i eth1` | worked unqualified |
| 2026-09-03 | only with `i eth1` | **only with `i eth0`** |

Same firmware source, a full clean rebuild in between, nothing changed in the
configuration. On the later run an unqualified `ping 192.168.0.21` returned
`Sent 4 requests, received 0 replies` even in a freshly opened session, while
`ping 192.168.0.21 i eth0` answered 4 of 4 — with `eth0 err=0` throughout, so
nothing was failing to transmit. Forwarding was unaffected either way: in the
same run the PC reached `.21` and `.31` straight through the bridge.

So the rule is stronger than "remember `i eth1` for the PC": **always name the
interface when pinging from the bridge's own console**, in either direction.
`iperf` is already immune, because `iperf_matrix_test.py` pins every client's
source address per destination instead of trusting a default.

That the default can flip also weakens, rather than supports, the earlier guess
that default egress explains why each board answers the PC on only one address.
The reply path still cannot be pinned from the CLI, so that stays untested.

It does **not** explain `COM23 → .12`, which stays `Sent 0` even with
`i eth0` given explicitly. That one is [defect 1](#6-defects-found) and has a
different cause.

### A measurement trap worth recording

Windows `ping.exe` counts an ICMP *Destination host unreachable* as a received
packet, so `Received = 3` and `0% loss` can both appear for an address that is
not reachable at all:

```
Pinging 192.168.0.32 from 192.168.0.100 with 32 bytes of data:
Reply from 192.168.0.100: Destination host unreachable.
Packets: Sent = 2, Received = 2, Lost = 0 (0% loss)
```

The first pass of this matrix was scored that way and reported `.32` as
reachable. Only `Reply from <target>: bytes=` lines count. The PC also has a
second adapter in the same `/24` (Wi-Fi on `192.168.0.78`), so every PC-side
ping here pins the source with `-S 192.168.0.100`.

---

## 4. Throughput matrix

Re-measured on **2026-09-03** after a full clean rebuild, on the addressing in
section 2, with all three boards flashed from that one image — each of them
confirms the same build timestamp at boot. `scripts/iperf_matrix_test.py`, UDP rate
search over 1-80 Mbit/s with a 2 % loss threshold, then one TCP measurement.
UDP loss always read from the receiving side.

| Direction | UDP max | TCP |
|---|---|---|
| PC → Bridge | 79.25 Mbit/s, 0.0 % | 15.72 Mbit/s |
| Bridge → PC | 72.90 Mbit/s, 0.0 % | 11.70 Mbit/s |
| PC → FollowerA | 7.99 Mbit/s, 0.0 % | 5.55 Mbit/s |
| PC → FollowerB | 8.00 Mbit/s, 0.0 % | 5.56 Mbit/s |
| Bridge → FollowerA | 9.42 Mbit/s, 0.0 % | 5.84 Mbit/s |
| Bridge → FollowerB | 9.43 Mbit/s, 0.0 % | 5.85 Mbit/s |
| FollowerA → PC | 9.43 Mbit/s, 0.0 % | 3.89 Mbit/s |
| FollowerA → Bridge | 9.42 Mbit/s, 0.0 % | 5.84 Mbit/s |
| FollowerA → FollowerB | 9.43 Mbit/s, 0.0 % | 5.85 Mbit/s |
| FollowerB → PC | 9.44 Mbit/s, 0.0 % | 3.75 Mbit/s |
| FollowerB → Bridge | 9.42 Mbit/s, 0.0 % | 5.83 Mbit/s |
| FollowerB → FollowerA | 9.42 Mbit/s, 0.0 % | 5.83 Mbit/s |

**All twelve directions now produce a measurement.** Every path crossing the
T1S segment lands at 9.42-9.46 Mbit/s with zero loss — the physical ceiling of
10BASE-T1S minus PLCA overhead, and identical whether the traffic involves the
bridge, runs directly between two followers, or terminates on the board that has
no 100BASE-TX PHY at all. Bridge-originated TCP toward a follower stays at
5.84/5.85 Mbit/s, so the `TC6_TX_ETH_MAX_SEGMENTS` fix still holds; without it
this direction reads 0.00 Mbit/s.

### `FollowerB → Bridge` no longer fails

The earlier run reported FAIL here because the script addressed the bridge at
`192.168.0.12`, which COM23 holds as one of its own addresses
([defect 1](#6-defects-found)). It now addresses a board's **eth0**, the T1S
side, whenever the source is another board — which is both the correct thing to
measure for a T1S peer and enough to sidestep the defect.
`FollowerB → FollowerA` also came in clean, so the follow-on effect from
defect 2 did not recur.

### The one figure that does not reproduce

`PC → Follower` has now been measured three times: 7.96 / 8.00 Mbit/s, then
2.00 / 4.92, then 7.99 / 8.00 on the clean rebuild. Every other direction
reproduces to within 0.04 Mbit/s across all three. Same firmware, same path,
same script settings; the rate search uses coarse steps (1, 2, 5, 8, ...), so
the middle run disagrees by one to two steps rather than marginally — and with
two runs either side of it, it now looks like the outlier rather than the rule.

This is the only direction whose bottleneck is the bridge forwarding *from* the
fast segment *into* the slow one, so it depends on buffer availability at that
moment rather than on a sender that is itself the limit. That makes run-to-run
variation plausible, but it is not established — treat the PC→follower figure
as indicative and the T1S-limited figures as solid.

---

## 5. Sniffer capture validation

Re-measured on **2026-09-03**.
`scripts/sniffer_capture_test.py --udp-rate 10 --duration 5`: `sniffer` enabled
on COM8, real traffic driven **directly between COM10 and COM23**, tshark
recording on the PC's NIC. With sniffer on, COM8's own T1S transmitter is
disabled — it is a passive tap and not a participant in this traffic.

| Test | Verdict |
|---|---|
| UDP FollowerA → FollowerB | **COMPLETE** — 4018 captured, 4017 sent |
| UDP FollowerB → FollowerA | **COMPLETE** — 4023 captured, 4022 sent |
| TCP FollowerA → FollowerB | **COMPLETE** — 3 647 080 bytes / 2503 segments, 99.8 % of expected |
| TCP FollowerB → FollowerA | **COMPLETE** — 3 639 780 bytes / 2498 segments, 99.9 % of expected |

The first attempt of this run produced one empty capture: the
`FollowerA → FollowerB` UDP file held nothing but its 496-byte header, and the
script duly reported `INCOMPLETE`. The mirror counters from that same attempt
read `rx_hook=18045 tx_submitted=18045 ack_ok=18045`, so the frames were
mirrored and handed to the GMAC — the gap was on the capture side, most likely
the session's first `tshark` start missing its window. An immediate re-run, with
nothing changed, produced the four verdicts above. Worth keeping in mind as a
harness caveat rather than a firmware finding: a single empty capture is not
evidence against the mirror path, and the counters are what tell the two apart.

The UDP runs asked for 10 Mbit/s and the link delivered 9.31 Mbit/s at 0 % loss
by the sender's own count, i.e. the mirror path was exercised at the segment's
ceiling rather than at a comfortable fraction of it.

Mirror counters on COM8 for the whole run:

```
dbg: rx_hook=18045 passed_filter=18045 pool_empty=0 no_eth1=0 tx_submitted=18045
dbg: ack_ok=18045 ack_fail=0 last_ack_res=0 max_len_submitted=1514 max_len_ok=1514
dbg: truncated=13032 (frames cut to 1514 bytes before mirroring)
```

18 045 frames seen, 18 045 mirrored, 18 045 confirmed sent by the GMAC. No
buffer exhaustion, no missing destination interface, no failed transmit.

### What the truncation counter actually does — now measured, not assumed

13 032 of those frames were clamped to `MIRROR_SAFE_FRAME_LEN`, which an earlier
version of this report could only call harmless as a plausible inference.
Checked directly against the captures instead:

```
tcp_FollowerA_to_FollowerB.pcapng: lost_segment=0  retransmission=0
tcp_FollowerB_to_FollowerA.pcapng: lost_segment=0  retransmission=0
```

Wireshark finds **no gap at all** in either TCP stream, and the frame lengths
explain why. Data segments capture at 1514 bytes and pure ACKs at 64, with
nothing in between; all 4018 captured UDP datagrams read
`frame.len=1514  ip.len=1498  udp.length=1478`, without a single exception.

Those numbers say the mirrored copies are, if anything, slightly **too long**
rather than short: a minimum-size ACK is 60 bytes on the wire but captures as
64, and a full-size frame is 1512 bytes on the wire but captures as 1514. The
extra bytes are the 4-byte Ethernet FCS, which the LAN865x hands up and nothing
strips; for a full-size frame the 1514 clamp happens to cut two of those four
away again. Wireshark parses by the IP and UDP header lengths and ignores the
remainder, which is why the captures are byte-exact for analysis. This also
explains, from the opposite direction, the consistent "4 bytes over" reading
recorded in [`session-log.md`](session-log.md) for small frames.

So the clamp never removes payload. What it removes is FCS padding the capture
had no use for.

One caveat on this run: the destination's own iperf report failed to parse for
both UDP directions (`destination report: NO RESULT`), so receive-side loss was
not independently confirmed and completeness is measured against the sender's
own sequence counter. The TCP directions did report from the destination.

Turning sniffer off restored the transmitter with the readback confirmation the
firmware now performs:

```
[SNIFFER] T1S transmitter enabled - CONFIRMED by readback of T1SPMACTL.TXD
```

---

## 6. Defects found

**1. A board with a missing PHY is stuck on the compiled default IP for that
interface — and that default is the bridge's own address.**

`configuration.h` gives eth1 the default `192.168.0.12`. On COM23 that address
is applied at initialization and then can never be changed, because the
interface is torn down when its PHY is not found:

```
setip eth1 192.168.0.99 255.255.255.0
No such interface is up
```

`setenv ip1` + `saveenv` hit the same wall silently: `showenv` reports the
value that was asked for (`192.168.0.220`), while the running stack keeps
`192.168.0.12`. Confirmed across a reset, and confirmed again after setting
eth1 to a different subnet entirely — the change never reaches the stack.

The consequence is that COM23 treats `192.168.0.12` as **its own** address, on
an interface with no link, and refuses to put anything on the wire:

```
ping 192.168.0.12
Ping: done. Sent 0 requests, received 0 replies.
```

Phase 2 supplies the clean counter-check, because there the board was
configured for `.32` instead of `.220`: `COM23 → .12` still reports `Sent 0`,
while `COM23 → .32` reports `Sent 4 requests, received 0 replies`. The address
it was *told* to use leaves the board and finds nobody; the address it was
*never* told to use is the one it defends as local. That is the compiled default
`configuration.h` gives eth1, surviving both a full chip erase and every
reconfiguration attempt.

`Sent 0` is the signature, and it is not specific to this case: `COM10 → .210`,
its own eth1 address on a link-less interface, answers exactly the same way,
whereas `COM23 → .202` — its own eth0 address, with link — replies 4 of 4.
Forcing the interface (`ping 192.168.0.12 i eth0`) changes nothing, and a
sniffer capture of the T1S segment shows no ARP request for `.12` ever being
sent, while requests for `.201` from the same board are there.

Impact: any board without its 100BASE-TX PHY can never reach the real bridge's
eth1 address. Workarounds, in order of preference: address the bridge by its
eth0 address `.11` from the T1S side, give the bridge's eth1 an address other
than the compiled default, or change the default in `configuration.h`.

*Status: present, not fixed.* The throughput matrix in section 4 takes the
first workaround, which is why the direction is measurable there — the defect
itself is untouched.

**2. A hung iperf session cannot be cleared, and poisons the next test.**
`FollowerB → Bridge` used to target `.12`, which COM23 cannot reach, so the
session waited forever on an ARP that never resolves — the failure mode
`iperf_matrix_test.py` warns about in its own docstring. `iperfk` then answers
`trying to stop iperf instance 0...` and never completes, and the following
test aborts with `All instances busy. Retry later!`. Only a board reset clears
it. That is why two directions once showed FAIL when only one was genuinely
broken.

*Status: no longer triggered, not fixed.* With defect 1 routed around, nothing
in the matrix reaches an unresolvable address any more, and the phase-2 run
completed all twelve directions without a single stuck session. The inability
to kill a session that is already hung is untouched.

**3. `iperf_matrix_test.py` assumed only the bridge has two interfaces.**
Its `bind_ip` was set for the `Bridge` source only, with the comment "only the
Bridge has more than one interface". After this rollout all three nodes have
two, so follower sources were never pinned with `iperfi`.

*Status: fixed.* Interface selection is no longer a special case for one node:
each entry in `DEVICES` now carries its own `eth0`/`eth1` addresses plus a
`toward_pc` field naming the interface that faces the PC — still `eth0` for a
follower, which reaches the PC through the T1S segment rather than on its own
eth1. Two helpers use it: `bind_addr_for()` pins the client's outgoing
interface per destination, and `dest_addr_for()` makes board-to-board traffic
address the destination's eth0. `sniffer_capture_test.py` imports both instead
of reading `["ip"]` directly.

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

---

## 8. Verification status

What was tested against which board state, so nobody has to infer it from the
sections above. Everything here — the ping matrix, the throughput matrix and the
sniffer validation — was measured on build `Sep  3 2026 17:48:07`, produced by a
full clean rebuild and flashed to all three boards, each of which reports that
timestamp itself at boot. The phase-2 addressing applies throughout.

| Board state | Ping | iperf | Sniffer capture |
|---|---|---|---|
| Both PHYs fitted | yes | yes | yes |
| **100BASE-TX PHY missing** | yes | yes, as an endpoint — 9.42 / 9.44 Mbit/s, 0 % loss | yes, as a traffic endpoint; the feature itself needs eth1 and cannot run *on* such a board |
| **T1S MAC-PHY missing** | yes | not pursued | not pursued |

The board without its 100BASE-TX PHY was not a bystander in the throughput and
capture runs — it was `FollowerB`, one of the two endpoints, and the sniffer
test's four verdicts were `COMPLETE` with it in the path. Regression on a
healthy board is covered by the same runs, since the bridge carrying them had
both PHYs.

Coverage for the missing-T1S-PHY case stops at ping **by decision**, not by
oversight. On such a board T1S throughput and the mirror path are structurally
impossible — there is no eth0 to measure or to mirror from — and the remaining
possibility, iperf over eth1 against the PC, was judged not worth the bench
time.

Explicitly **not** verified:

- **`env_apply()` writes configuration into both interfaces.** Defect 1 shows
  that this path fails silently for a downed interface. Which other fields are
  affected the same way was not investigated.
- **Whether a reply's egress interface explains the PC-side asymmetry.**
  Plausible, and consistent with `ping` defaulting to eth0, but the reply path
  cannot be pinned from the CLI.
- **Why `PC → Follower` throughput does not reproduce** between the two runs
  (section 4). Every other direction repeats to within 0.04 Mbit/s; this one
  moved by one to two steps of the rate search.
- **Receive-side loss for the two UDP sniffer directions.** The destination's
  own report did not parse on this run, so completeness rests on the sender's
  sequence counter. The capture analysis is independent of that and found no
  gaps.
