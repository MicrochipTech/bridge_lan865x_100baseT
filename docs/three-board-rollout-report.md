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
seeds on its own, and re-addressed the boards into a tidier block. The
throughput and capture measurements (sections 4 and 5) are from phase 1, the
reachability matrix (section 3) from phase 2. Only the labels differ — the
topology and the hardware are the same throughout.

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

Captured in phase 1, hence the PLCA id 7 — the message format is the same in
both phases. COM8 and COM10 report both interfaces:

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

## 3. Reachability — full ping matrix

Measured in phase 2, four sources against all seven addresses. `Sent 0` means
the board produced no packet at all; a plain cross means it transmitted and got
nothing back.

| from ↓ / to → | `.100` PC | `.11` | `.12` | `.21` | `.22` | `.31` | `.32` |
|---|---|---|---|---|---|---|---|
| **PC** | — | no | **yes** | **yes** | no | **yes** | no |
| **COM8** (bridge) | no | yes | yes | yes | yes | yes | no |
| **COM10** (A) | yes | yes | yes | yes | `Sent 0` | yes | no |
| **COM23** (B) | yes | yes | `Sent 0` | yes | yes | yes | no |

**Board to board is essentially complete: 19 of 21 combinations work**,
including every path across the T1S segment and straight through both bridges.

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

### `COM8 → PC` is operator error, not a defect

The bridge's `ping` needs to be told which interface to use. Left to itself it
picks eth0, the T1S side, where the PC is not:

```
ping 192.168.0.100              Sent 4 requests, received 0 replies
ping 192.168.0.100 i eth1       Sent 4 requests, received 4 replies
ping 192.168.0.100 i eth0       Sent 4 requests, received 0 replies
```

So every `COM8 → PC` failure in this report — three in phase 1, one in the
matrix above — is the missing `i eth1`. On a node with two interfaces in one
subnet the interface argument is mandatory, exactly as `iperf` needs `iperfi`.

The same default egress plausibly explains why each board answers the PC on
only one address: a reply sourced from `.11` would be sent out eth0 as well.
That connection is untested — the reply path cannot be pinned from the CLI.

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

Measured in **phase 1**, so the node names below map to the phase-1 addresses:
Bridge `.12`, FollowerA `.201`, FollowerB `.202`.

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

`FollowerB → Bridge` is the genuine failure, and it is structural rather than a
throughput limit: the script addresses the bridge at `192.168.0.12`, which
COM23 believes is one of its own addresses — see [defect 1](#6-defects-found).
Addressing the bridge at `.11` instead would make this direction measurable.

`FollowerB → FollowerA` initially reported FAIL as well, but only as a
**follow-on effect** — see defect 2. Re-run in isolation it delivers the
9.42 Mbit/s in the table.

---

## 5. Sniffer capture validation

Measured in **phase 1** as well.
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
eth1 address, so one of twelve throughput directions is structurally
impossible. Workarounds, in order of preference: address the bridge by its
eth0 address `.11` from the T1S side, give the bridge's eth1 an address other
than the compiled default, or change the default in `configuration.h`.

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
