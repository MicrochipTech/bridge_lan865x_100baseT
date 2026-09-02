# CLI Command Reference

Every command the firmware answers to, what it does, and what it does *not*
do. The command set is identical on both consoles — the EDBG virtual COM port
(115200 8N1) and the Telnet server on TCP/23 — and a reply always goes back to
whichever console asked.

Commands are typed bare: there is no group prefix. The groups below exist only
to organise `help` output. Example outputs in this document were captured from
a running board, not written by hand.

- [Conventions](#conventions)
- [Built-in commands](#built-in-commands)
- [`Test` — diagnostics, counters, memory](#test--diagnostics-counters-memory)
- [`env` — persistent configuration](#env--persistent-configuration)
- [`lan` — LAN8651 registers, test modes, PLCA](#lan--lan8651-registers-test-modes-plca)
- [`span` — port mirror and sniffer](#span--port-mirror-and-sniffer)
- [`noip` — raw Ethernet frame test](#noip--raw-ethernet-frame-test)
- [`testserver` — TCP echo server](#testserver--tcp-echo-server)
- [`iperf` — throughput tester](#iperf--throughput-tester)
- [`tcpip` — Harmony stack commands](#tcpip--harmony-stack-commands)
- [What survives a reset](#what-survives-a-reset)

---

## Conventions

| Notation | Meaning |
|---|---|
| `<arg>` | required argument |
| `[arg]` | optional argument |
| `a\|b` | either one |
| *hex* | written `0x…` or bare hex digits, both accepted |

Two distinctions run through the whole command set and cause most of the
surprises:

**Volatile versus persistent.** `setip`, `setgw` and `plca_node <id>` change
the running stack only and are gone after a reset. The `env` group writes to
the emulated EEPROM and survives. See [What survives a reset](#what-survives-a-reset).

**Requested versus confirmed.** Register writes go through an asynchronous
state machine that serves one operation at a time. A command returning
promptly means the request was accepted, not that the hardware took it — which
is why `lan_rmw`, `testmode` and `sniffer` read the register back and report
the result separately.

---

## Built-in commands

| Command | Description |
|---|---|
| `help` | list the command groups |
| `q` | quit the command processor on this console |
| `reset` | reset the host MCU — the board reboots, the console reconnects |

`reset` clears everything volatile; the persisted `env` record is reloaded on
the way back up.

---

## `Test` — diagnostics, counters, memory

### `help`
Lists this group's commands.

### `timestamp`
Prints the build timestamp compiled into the image. This is the only way to
tell from the outside which firmware a board is running — two builds from the
same source differ here and nowhere else visible.

```
Build Timestamp: Sep  2 2026 15:53:35
```

### `uptime`
Time since boot or last reset.

```
uptime: 0d 00:16:05  (965 s since boot/last reset)
```

### `stats`
Per-interface TX/RX counters plus the main-loop rate. `err` on `eth0` climbing
while `qFull` stays at zero points at the driver rejecting frames rather than a
full queue — that is the signature the `TC6_TX_ETH_MAX_SEGMENTS` bug produced.

```
eth0 TX: ok=139 err=0 qFull=0 pend=0
eth0 RX: ok=201 err=0 nobufs=0 pend=0
eth1 TX: ok=239 err=6 qFull=0 pend=0
eth1 RX: ok=138 err=0 nobufs=0 pend=0
main loop: 85540 cycles/s
```

### `meminfo`
Free memory on both heaps. They are separate: the C runtime uses nano-malloc
(no exact free count, hence "largest free block"), while the TCP/IP stack
manages its own pool.

```
C-runtime heap: total=163840  largest free block=64912  (nano-malloc; no exact free count)
TCP/IP heap:    size=98224  free=36352  maxblock=24992  highwater=65200
```

`highwater` is the useful number when sizing the stack heap: it records the
peak, so a value close to `size` means the configuration has no margin left.

### `ipdump [0..3]`
Live dump of received frames: `0` off, `1` eth0, `2` eth1, `3` both. Output
goes to the console that enabled it and can be heavy on a busy segment — the
frames are buffered and printed by the deferred packet log, see `logstat`.

### `logclear` / `logstat`
`logstat` reports the deferred packet log; `logclear` empties it. `overflows`
counting up means frames arrived faster than the console could print them, so
the dump is incomplete — the counters stay honest about it rather than
silently dropping.

```
[LOG] total=0 pending=0 overflows=0 bufsize=64
[LOG] pool_offset=0 pool_size=24288 (16 frames x 1518 bytes)
```

### `dump <addr_hex> <count>`
Hex dump of `count` bytes from `addr_hex`. Reads live RAM — driver descriptors,
config structs, register-backed variables — instead of inferring state from
source.

### `peek <addr_hex> [size=1|2|4]`
Read a single value, default byte width 1.

### `poke <addr_hex> <value_hex> [size=1|2|4]`
Write a single value. **No validation happens.** This writes to any address the
CPU can reach, including peripheral registers and the stack; a wrong address
can hang or reset the board. Useful for experiments, not for routine
configuration — `env` and the `lan` commands exist for that.

---

## `env` — persistent configuration

Backed by a versioned, CRC-protected record in the last 16 KB of flash. Edits
go to a RAM shadow first; nothing is persisted until `saveenv`.

### `showenv`
Shows the RAM shadow, the firmware's own id, and — for the settings that only
take effect at boot — both the persisted and the live value side by side.

```
env (RAM shadow):
  env   id TIBR  version 5  crc ok  |  firmware id TIBR  version 5  tcpip_iperf_lan865x_bridge
  eth0  ip 192.168.0.11  mask 255.255.255.0  gw 192.168.0.1  dns 192.168.0.1
  eth1  ip 192.168.0.12  mask 255.255.255.0  gw 0.0.0.0  dns 192.168.0.1
  eth0  mac 00:04:25:CA:CE:D9
  eth1  mac 00:04:25:CA:CE:DA  (applied at boot)
  plca  id 5  count 8  (eth0/T1S)
  mirror OFF at boot  (now: OFF)
  sniffer OFF at boot  (now: OFF)
```

Run this first whenever a board behaves unexpectedly: a reflash does **not**
erase the emulated EEPROM, so an edited compile-time default can appear to
have no effect until `resetenv` or an explicit `setenv` + `saveenv`.

### `setenv <key> <val>`
Edits the RAM shadow only — no register is written and nothing is persisted.

| Key | Value |
|---|---|
| `ip0` `mask0` `gw0` `dns0` | eth0 (T1S) addressing |
| `ip1` `mask1` `gw1` `dns1` | eth1 (100BASE-TX) addressing |
| `mac0` `mac1` | interface MACs, `XX:XX:XX:XX:XX:XX` |
| `plca_id` | PLCA node id, `0` = coordinator |
| `plca_cnt` | PLCA node count |
| `mirror` `sniffer` | boot state of the capture modes, `0` or `1` |

### `saveenv`
Persists the shadow to EEPROM **and applies it**. This is the command that
actually writes the PLCA register — `setenv plca_id` alone changes nothing on
the PHY. If the register interface is busy at that moment the apply is skipped;
verify with `lan_read 0x0004CA02` (`PLCA_CTRL1`: `NODE_CNT` in bits 15:8,
`NODE_ID` in bits 7:0) rather than with `plca_node`, which reports the driver's
intent rather than the PHY's state.

### `readenv`
Reloads from EEPROM and applies it, discarding unsaved edits.

### `resetenv`
Restores the compiled-in defaults from `configuration.h`, persists them and
applies them.

**IP and PLCA apply live. A MAC change needs a reset**, because the stack reads
the MAC once, at `TCPIP_STACK_Init()`.

---

## `lan` — LAN8651 registers, test modes, PLCA

Register addresses encode their MMS bank in the upper bits:
`address = (MMS << 16) | offset`. MMS0 OA Standard, MMS1 MAC, MMS2 PHY PCS,
MMS3 PHY PMA/PMD, MMS4 PHY Vendor (PLCA lives here), MMS10 Misc.

### `lanhelp`
Lists this group's commands.

### `lan_read <addr_hex>`
Reads one register. The result arrives asynchronously, printed when the
transfer completes.

```
LAN865X Read OK: Addr=0x000308F9 Value=0x00004000
```

### `lan_write <addr_hex> <value_hex>`
Writes one register. Always follow with a `lan_read` — the write itself only
reports that the transaction completed.

### `lan_rmw <addr> <mask> <value>`
Read-modify-write for registers where several control bits share one word,
followed by an automatic masked readback:

```
LAN865X RMW OK: Addr=0x000308F9 Mask=0x00004000 Value=0x00004000 Final=0x00004000
[VERIFY] PASS addr=0x000308F9 masked=0x00004000 (mask 0x00004000)
```

`[VERIFY]` is the actual evidence that the register kept the value. Note that
the driver does not mask `value`, so bits outside `mask` are written too, and
self-clearing bits legitimately report `FAIL`.

### `testmode [0..4] [seconds]`
IEEE 802.3-2022 §147.5.2 transmitter test modes. No argument shows the current
mode, decoded.

| Mode | Purpose | Instrument |
|---|---|---|
| 0 | normal operation | — |
| 1 | output voltage, timing jitter | oscilloscope |
| 2 | output droop | oscilloscope |
| 3 | PSD mask / transmitter distortion | spectrum analyser |
| 4 | transmitter high impedance | measure the bus without this transmitter |

**Modes 1–4 take the T1S link down by design.** The CLI is unaffected — it runs
over EDBG or Telnet, not over T1S. The optional timeout reverts automatically;
use it, because a forgotten test mode later presents as a link that will not
come up. Not to be confused with `sniffer`, which keeps the receiver and the
capture path fully active.

### `plca_node [id]`
Gets or sets the PLCA node id; `0` makes the board the coordinator.

```
[PLCA] current node ID: 5 (NODE_CNT=8)
```

A bare `plca_node <id>` is **volatile** — lost on the next reset. Persist with
`setenv plca_id` + `saveenv`.

### `plca_stat`
Bus health below IP-frame level: link status, the coordinator's observed cycle
length, transmit-opportunity and BEACON counters since the last call, and
events such as an empty PLCA cycle. `PRSSTS.MAXID` reports what the coordinator
actually observes, which is not necessarily this node's configured node count —
a genuinely useful configured-versus-observed comparison.

### `sqi [node|all|off]`, `sqi report <sec>|off`
Continuous Signal Quality Indicator, per node or weighted across all. With no
argument, shows the current reporting state.

---

## `span` — port mirror and sniffer

Copies T1S traffic onto `eth1` so Wireshark on the PC can see the two-wire bus.
Necessary because an ordinary NIC cannot tap a 10BASE-T1S segment, and because
frames addressed to the bridge itself are delivered locally and never forwarded.

| Mode | Forwarding to T1S | Frames mirrored to eth1 | T1S transmitter |
|---|---|---|---|
| bridge (default) | yes | none | on |
| `mirror 1` | yes | eth0 RX addressed to this bridge, plus this bridge's own eth0 TX | on |
| `sniffer 1` | **no**, eth0 TX is disabled | every frame eth0 receives; own TX not mirrored | off |

### `mirror [0|1]`
Bridge-focused capture: only frames addressed to this bridge, plus the bridge's
own transmissions, so a firmware-originated `ping` shows request and reply. The
narrow filter is deliberate — a frame the bridge merely forwards already
reaches the PC natively, and mirroring it again would only duplicate it.

### `sniffer [0|1]`
Whole-bus capture: every frame `eth0` receives, including traffic between two
other nodes. It also disables the LAN8651's own transmitter
(`T1SPMACTL.TXD`), making the bridge a passive tap — receiver and capture path
stay fully active, but **normal PC-to-T1S forwarding stops** while it is on.

The transmitter state is proven, not assumed: the write is retried for up to
three seconds and confirmed by a register readback.

```
eth0(T1S)->eth1 sniffer: ON
  T1S transmitter: not confirmed yet - verifying, watch for [SNIFFER]
[SNIFFER] T1S transmitter disabled - CONFIRMED by readback of T1SPMACTL.TXD
```

If it cannot be placed, an explicit error is printed and the state is reported
as not confirmed — treat anything other than a confirmed answer as a bridge
that may still be transmitting, and check `lan_read 0x000308F9` (bit 14).

**`mirror` and `sniffer` are alternatives, not a combination.** The firmware
refuses to enable one while the other is active and names the command to switch
off first.

Both default to off at every boot unless persisted via `setenv mirror|sniffer`
+ `saveenv`. A persisted `sniffer 1` means the transmitter is never enabled at
all on the next boot, rather than enabled and then switched off.

**Capture limitation:** frames above 1514 bytes are truncated before mirroring,
not dropped — deliberately, to avoid wedging USB-NIC/Npcap captures on the PC.
The count is in the `dbg: truncated=` line. A capture is therefore not
guaranteed byte-complete for oversized frames; this is a protocol-level
diagnostic tool, not a substitute for a calibrated compliance instrument.

### `bigframe <total_len>`
Sends one raw, oversized frame straight out `eth1`, bypassing the stack, T1S,
mirror and sniffer entirely. A targeted tool for exercising the frame-length
handling of the mirror path, unrelated to normal operation.

---

## `noip` — raw Ethernet frame test

### `noip_send <n> [gap_ms]`
Sends `n` raw EtherType `0x88B5` frames out `eth0`, bypassing the TCP/IP stack
completely. Deterministic and independent of any IP configuration, which makes
it a reproducible source for oscilloscope captures and for latency-sensitive
protocol work.

### `noip_stat`
TX/RX counters for that path, independent of any protocol state.

```
[NoIP] TX=0  RX=0
```

---

## `testserver` — TCP echo server

### `testserver [start [port]|stop]`
A small polled TCP echo server, default port `5566`, echoing received bytes
back in fixed 512-byte chunks and tracking byte counters. Intended for
bandwidth-ramp testing from an external tool that just wants an echo endpoint,
distinct from `iperf`'s own protocol. No argument shows the state.

```
testserver: idle
```

---

## `iperf` — throughput tester

The Harmony stack's built-in tester, protocol-compatible with `iperf`/`iperf2`.

| Command | Description |
|---|---|
| `iperf [options]` | start a session, server or client |
| `iperfk` | stop the running session |
| `iperfi <address>` | bind the test to a specific local interface |
| `iperfs <tx\|rx> <bytes>` | set the TX/RX buffer size |

| Option | Meaning | Default |
|---|---|---|
| `-s` | run as server | — |
| `-c <ip>` | run as client toward `<ip>` | — |
| `-u` | UDP instead of TCP | TCP |
| `-b <rate>` | target bandwidth for a UDP client (`K`/`M` suffix) | 10 Mbps |
| `-t <secs>` | duration | 10 s |
| `-p <port>` | port | 5001 |

```
iperf -s                                  # on the board, as a server
iperf -c 192.168.0.220 -u -b 50M -t 20    # from the board, as a client
iperfk                                     # stop it
```

**`iperf` with an unrecognised argument starts a session rather than printing
help** — `iperf help` is not a help command. Use `iperfk` to stop one.

For UDP, never trust the embedded client's own loss figure: it receives no real
feedback from the far end and reports roughly 0% regardless. Read loss from the
receiving side. `scripts/iperf_matrix_test.py` automates that, and measured
results are in [`iperf_matrix_results.md`](iperf_matrix_results.md).

---

## `tcpip` — Harmony stack commands

Registered by the TCP/IP stack itself, not by this project. The ones that
matter here:

| Command | Description |
|---|---|
| `netinfo` | both interfaces: IP, mask, MAC, link status |
| `setip <if> <addr> <mask>` | set an interface address — **volatile** |
| `setgw <if> <addr>` | set an interface gateway — **volatile** |
| `setmac <if> <mac>` | set an interface MAC — **volatile** |
| `ping <addr>` | ICMP echo from the board |
| `arp <if> <op>` | inspect and manipulate the ARP cache |
| `bridge` | MAC bridge status and forwarding database |
| `stack` | stack-wide status |
| `heapinfo` / `heaplist` | TCP/IP heap detail beyond `meminfo` |
| `macinfo` | MAC driver statistics |
| `dhcp`, `dhcps`, `dnsc`, `dnss`, `sntp`, `tftpc`, `http`, … | the corresponding stack services |

`if`, `pktinfo`, `plog`, `udp`, `tcp`, `tcptrace`, `announce`, `vlan`, `miim`
and others are also present; type `help` on the board for the current list,
which follows whatever the stack configuration enables.

---

## What survives a reset

| Set with | Survives a reset | Notes |
|---|---|---|
| `setenv` + `saveenv` | **yes** | the emulated EEPROM, outside the hex image — a reflash does not clear it |
| `resetenv` | **yes** | restores and persists the compiled defaults |
| `setip`, `setgw`, `setmac` | no | running stack only |
| `plca_node <id>` | no | writes `PLCA_CTRL1`, but nothing persists it |
| `mirror`, `sniffer` | no | unless persisted via `setenv` |
| `testmode` | no | and use the timeout argument, so it also reverts without one |
| `lan_write`, `lan_rmw`, `poke` | no | direct register and memory writes |

The rule of thumb: anything typed as a bare runtime command is gone after a
power cycle, and the board comes back to whatever `env` has persisted. Use the
runtime commands to try a value, then `setenv` + `saveenv` to keep it.
