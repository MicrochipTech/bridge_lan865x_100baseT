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

### `cpuload on|off|stats|reset|live`
Cycle-accurate CPU-load profiling using the Cortex-M4 DWT cycle counter, in
two sections: the bare-metal round-robin main loop (`SYS_Tasks()` in
`tasks.c`, one slot per polled call) and every one of this board's 6 actual
interrupt handlers (`DMAC_0`, `DMAC_1`, the LAN865x/TC6 SPI driver, the
console UART, `GMAC`/eth1, the `SYS_TIME` tick — `interrupts.c`'s vector
table is repointed at wrappers around each real handler, see
`docs/mcc-generated-code-patches.md` item 13). One shared on/off arms and
disables both sections together. Off by default — arming it costs a per-call
flag check on the main loop and one small fixed indirection per interrupt,
nothing while disabled. This is a finer-grained companion to `stats`' "main
loop: N cycles/s" counter, not a replacement for it.

- `cpuload on` — resets all counters, arms the DWT cycle counter, starts
  recording.
- `cpuload off` — stops recording; the counters keep their last values so they
  can still be read.
- `cpuload reset` — clears counters without changing on/off state.
- `cpuload stats` (or no argument) — prints the table below, always in cycles.
- `cpuload live` — the same table, redrawn in place once a second: the cursor
  jumps up by the table's own fixed height (`\x1b[<N>A`, then `\r\x1b[J`) and
  overwrites it — no scrolling, needs a real terminal such as TeraTerm, not a
  line-oriented tool. (An earlier version used VT100 "save/restore cursor"
  instead, which only works until the terminal scrolls once — it broke the
  moment the table grew past one screenful. The fixed-height relative move
  used now keeps working regardless of how much the terminal has scrolled —
  but only if every printed line actually fits the terminal's width: a line
  that wraps eats a second physical row the fixed cursor-up count doesn't
  know about, drifting a little further every frame. Every line here is now
  deliberately kept to ≤80 columns for exactly that reason (the `median`
  line and the microseconds column header were the two that didn't,
  originally — see `docs/cpuload-profiling-report.md` §10).
  Auto-enables sampling if it wasn't already on. While live: `r` resets the
  counters, `t`/`c` switch the displayed units between microseconds and
  cycles, `q` stops the live view — all take effect immediately, no Enter
  needed. Only one console can be live at a time. Console-agnostic (works the
  same over Telnet), with one caveat: if a Telnet session starts
  `cpuload live` and disconnects without pressing `q`, the board keeps that
  console "live" (harmlessly — it just means nothing else can start a new
  live view until the board is reset) until something else takes it over.

```
cpuload: ENABLED (DWT cycle counter, 120.0MHz core clock)
  slot        n          min        max       mean     median  (cycles)
-- main loop --
  sys_cmd     564879     235        1586      246      242    
  miim        564880     177        3532      186      184    
  tcpip       564880     657        24332     723      674    
  net_pres    564880     172        1184      179      177    
  app         564880     302        1488      319      314    
  TOTAL       564879     1937       25880     2042     1976   
-- interrupts --
  isr_dmac0   56         144        145       144      145    
  isr_dmac1   56         530        583       562      567    
  isr_spi     (no samples yet)
  isr_usart   175        146        231       191      189    
  isr_gmac    46         237        237       237      237    
  isr_tc0     20387      523        898       652      671    
TOTAL: mean 2042 cycles (17 us) per pass -> avg 58765 loops/s
Load vs fastest pass: 6% (mean 2042 vs min 1937 cycles)
CPU load: interrupts 1%, tasks 99%, total 100%
median: last <=128 samples/slot - min/max/mean: since last reset
```

`min`/`max`/`mean` are exact and cover every sample since the last `on`/
`reset`. `median` is only over the most recent ≤128 samples per slot (a ring
buffer, sorted on demand) — cheap to keep, but it will not reflect a brief
spike that happened long enough ago to have scrolled out of that window.
`TOTAL` wraps the whole `SYS_Tasks()` pass, not a sixth call, and does **not**
include interrupt time — the interrupt section is a separate breakdown, not
folded into it.

**`isr_spi` reading "(no samples yet)" is confirmed correct, not a bug** —
this SPI bus (SERCOM0, the LAN865x) is configured with real DMA channels
(`initialization.c`: `.dmaChannelTransmit = DRV_SPI_XMIT_DMA_CH_IDX0`,
`.dmaChannelReceive = DRV_SPI_RCV_DMA_CH_IDX0`, not `SYS_DMA_CHANNEL_NONE`),
so `drv_spi.c` always takes its DMA transfer path for this instance — the
PLIB's own interrupt-driven `SERCOM0_SPI_WriteRead()` is present in the
binary but never reached. The transfers really do complete via
`isr_dmac0`/`isr_dmac1` (confirmed structurally: `SERCOM0_DMAC_ID_TX`/`_RX`
match the `DMAC_CHCTRLA_TRIGSRC` values configured on those two channels).
Verified two ways: reading the config source, and — since a real hardware
breakpoint settles this kind of question outright — using `pyOCD`'s Python
API interactively (the same tool `flash.bat` already uses) to confirm
`SERCOM0_SPI_InterruptHandler` genuinely never runs. See
`docs/cpuload-profiling-report.md` §9 for the full trail, including a
methodology pitfall worth remembering: a breakpoint set on a *running* core
without halting first silently never fires, on anything.

**A caveat worth stating plainly:** since the DWT cycle counter never stops
for an interrupt, time spent in one of the 6 interrupts above while a
main-loop slot's bracket happens to be open is *also* still counted inside
that main-loop slot's own number — nothing subtracts it back out. The
interrupt section tells you roughly how much that could be; it is not netted
against the main-loop figures automatically.

**`CPU load: interrupts X%, tasks Y%, total Z%`** is a time **breakdown**,
not a utilization figure — the bare-metal loop never sleeps, so it is always
100% "busy" by construction, and there is no idle task to measure spare
capacity against. It is instead a plain 2-way split of every cycle measured
so far — interrupts vs. everything else — always summing to 100%. A rising
interrupt share is the clearest single signal that the board is under load,
but the number itself only ever says *where* the time went, never *how much
headroom is left*.

**`Load vs fastest pass: N%`** is the closer attempt at an actual
utilization-shaped number, self-calibrating instead of needing a real idle
task: `TOTAL.min`, the single fastest pass seen since the last reset, is the
closest thing this system has to "idle" — a pass where whatever the loop
polled found nothing extra to do. `N%` is how much slower the *average*
pass is than that fastest one, so it is genuinely 0 at the theoretical floor
and grows as real work piles on.

**Important: this is not a 0–100% saturation gauge like a classic OS load
percentage — it is an unbounded ratio.** `100%` means "the average pass now
takes twice as long as the fastest one ever seen", `300%` means four times
as long, and so on with no ceiling; there is no fixed number that means "the
CPU can no longer keep up". That is a real architectural difference from a
system with a fixed control period or hard deadlines: this round-robin loop
just gets slower as more piles into each pass, it does not hit a wall at a
particular percentage. What an actual problem looks like here is not a
number crossing some threshold, but concrete symptoms elsewhere — PLCA/T1S
timing violated, `stats`' `qFull`/`err` counters climbing, the console or
Telnet visibly lagging. Treat `Load vs fastest pass` as a trend to watch,
not a limit to compare against.

Also worth knowing: `TOTAL.min` is not literally "zero work" either — even
the fastest pass still polls all five main-loop calls and pays the
`cpuload` instrumentation's own overhead, just with nothing extra for any
of them to do. It is the loop's fixed baseline cost, not an absence of
cost — `N%` measures the *variable* load stacked on top of that baseline.
It can also only get better (a new fastest pass lowers the baseline) or
reset to unknown (`cpuload reset`) — never silently drift worse on its own
— and a short observation window may simply not have caught the board's
true best case yet, so treat an early reading as provisional.

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
