# What TLS, the certificate machinery and MQTT actually cost

Flash, RAM, heap and CPU of this firmware, broken down by module, with the TLS
work called out separately - the question this whole branch exists to answer.

Everything here is measured on the image that is in `release\`, not estimated:

| Source | What it gives |
|---|---|
| `firmware\tcpip_iperf_lan865x.X\dist\default\production\*.map` (build of 2026-09-07, the `release\` HEX) | per-object `.text`/`.rodata`/`.data`/`.bss` |
| the same build's `.elf`, DWARF | exact `sizeof()` of the wolfSSL structures |
| `heapinfo` over the board's own console | the TCP/IP stack's heap, live |
| `cpuload` over the console | per-slot cycle counts, live |
| pyOCD attached in `attach` mode (no halt) | `_sbrk` state, RAM layout, live |

Bench: the bridge board at 192.168.0.12, SAME54P20A at 120 MHz, measured
2026-09-08. Where a number is derived rather than measured it says so.

---

## 1. Flash

Linker regions: `rom 0x0..0x7c000` (496 KB - one of the two OTA banks, see the
`bootload` design), `ram 0x20000000..0x20040000` (256 KB).

**Image: 369,992 bytes = 73 % of the 496 KB bank.** By module:

| Module group | ROM | share | of which `.rodata` |
|---|---:|---:|---:|
| **wolfSSL** (`third_party/wolfssl`) | **147,002** | **39.7 %** | 4,502 |
| drivers (LAN865x/TC6, GMAC, PHY, SPI, MIIM) | 82,262 | 22.2 % | 46,829 |
| TCP/IP stack (Harmony) | 71,620 | 19.4 % | 1,275 |
| application (`firmware/src/*.c`) | 35,741 | 9.7 % | 3,963 |
| toolchain / libc / runtime | 13,869 | 3.7 % | 339 |
| Harmony system + PLIBs | 9,842 | 2.7 % | 172 |
| NET_PRES + the TLS glue | 4,384 | 1.2 % | 104 |
| emulated EEPROM | 1,880 | 0.5 % | 0 |
| Paho MQTT packet codec | 1,516 | 0.4 % | 0 |
| rest (startup, init tables, exceptions) | 1,876 | 0.5 % | 1,118 |
| **total** | **369,992** | | 58,302 |

Two numbers deserve a second look:

- **wolfSSL is the single largest item in the image, at ~40 %.** Inside it:
  `internal.o` 34.4 K (the TLS state machine), `sp_cortexm.o` 19.7 K (the
  single-precision RSA/ECC assembly - this is what makes a handshake 1.2 s
  instead of many seconds), `asn.o` 16.3 K (X.509/ASN.1 parsing), `tfm.o`
  12.2 K (fast-math bignum), `ecc.o` 10.0 K, `aes.o` 9.5 K, `ssl.o` 9.1 K,
  `tls.o` 5.2 K, `sha.o` 4.5 K, `rsa.o` 4.3 K, `des3.o` 3.7 K.
- **`drv_ethphy.o` carries 43,961 bytes of `.rodata`** - a single table,
  `.rodata.F_DRV_PHY_SMITr`, of 43,600 bytes. That is 8.7 % of the whole flash
  bank spent by the PHY driver on a static table, more than the entire
  application. Not TLS's doing, but it is the cheapest 40 KB anyone will ever
  find in this image if flash gets tight.

**What the TLS/cert/MQTT feature itself costs in flash:** wolfSSL 147.0 K +
NET_PRES/glue 4.4 K + `cert_provision.o` 5.7 K + `app_mqtt.o` 2.7 K + the Paho
codec 1.5 K = **~161 KB, 44 % of the image**. `ecc.o`, `des3.o` and parts of
`asn.o` are linked but unreachable for the cipher suites actually negotiated -
a first, safe reduction if flash ever becomes the constraint (see §7).

## 2. Static RAM

**60,716 bytes of `.data`+`.bss`**, 23 % of the 256 KB. Layout, verified
against the live `_minbrk`/`_maxbrk` symbols:

```
0x20000000  +-------------------------------+
            | .data + .bss   60,720 bytes   |  60,716 measured + 4 padding
0x2000ED30  +-------------------------------+  _minbrk
            | C heap        163,840 bytes   |  (see chapter 3)
0x20036D30  +-------------------------------+  _maxbrk
            | stack          37,584 bytes   |  grows down from the top
0x20040000  +-------------------------------+
```

Per module, and the individual objects that dominate:

| Module group | static RAM | largest single objects |
|---|---:|---|
| application | 42,407 | `frame_data_pool` 24,292 (`app.o`), `s_slots` 6,432 (`cpuload.o`), `s_staged` 3,864 + `s_recv_buf` 1,280 (`cert_provision.o`), `pkt_log` 2,576 |
| TCP/IP stack | 6,207 | `icmpPingBuff` 2,000, `txfer_buffer` 1,600 (iperf) |
| drivers | 5,769 | `m_tc6` 4,560 (the LAN865x/TC6 driver) |
| Harmony system + PLIB | 4,333 | two 1,024-byte SERCOM1 UART buffers, `printBuff` 1,024 |
| NET_PRES + TLS glue | 968 | `net_pres.o` 932 |
| **wolfSSL** | **112** | - |
| toolchain / libc | 148 | - |

**wolfSSL's static footprint is 112 bytes.** That is the headline of this
chapter and the reason chapter 3 exists: wolfSSL puts essentially everything
on the heap, so its real RAM cost is invisible here and has to be looked for
there.

The application's 42 KB is not TLS either - three quarters of it is
`frame_data_pool`, the sniffer/mirror capture pool. `cert_provision.o`'s
5.1 KB (the staged certificate store plus its receive buffer) is the
certificate machinery's own static cost, and it is genuinely needed: a staged
identity has to survive in RAM between `cert_arm` and `cert_save`.

## 3. The heap

### 3.1 How big it is, and who takes it

The heap is **163,840 bytes (160 KB)**, set as `heap-size` in
`nbproject/configurations.xml` and visible in the map as
`_min_heap_size = 0x28000`. XC32 links the *lite* musl allocator
(`liblmalloc-musl.a`: `lite_malloc.o`, `lite_free.o`, `lite_calloc.o`), and
`_sbrk` hands the whole region out in one go - measured live right after a
reset, the break already equals `_maxbrk`. So "the heap" is one fixed 160 KB
arena from the first `malloc()` onwards; there is no growth to observe from
outside.

What lives in it:

| Consumer | Bytes | When |
|---|---:|---|
| **TCP/IP stack's internal heap** (`TCPIP_STACK_DRAM_SIZE`) | **98,304** | one `malloc()` at stack init, never released |
| wolfSSL: contexts, sessions, certificate parsing, bignums | see §3.3 | per TLS connection, freed on close |
| everything else (`crypto.c`'s `RsaKey`/`ecc_key` holders, misc) | small | on demand |
| **free for all of the above** | **65,536** | 160 KB - 96 KB |

The 96 KB figure is not a guess: `TCPIP_STACK_USE_INTERNAL_HEAP` with
`TCPIP_STACK_MALLOC_FUNC = malloc` means the stack asks the C heap for one
`TCPIP_STACK_DRAM_SIZE` block and then sub-allocates inside it. So **wolfSSL
and the TCP/IP stack share one 160 KB heap, and the stack takes 60 % of it
before the first packet moves.**

### 3.2 What the TCP/IP stack does with its 96 KB

Live, on the bridge board after ~1 h of uptime, both interfaces up, MQTT
client running:

```
heapinfo
Heap type: internal. Initial created heap size: 98224 Bytes
Allocable block heap size: 20480 Bytes
All available heap size: 36544 Bytes, high watermark: 71664
```

- 98,224 usable of the 98,304 requested (80 bytes of heap header).
- **61,680 bytes in use** right now, 36,544 free.
- **71,664 bytes peak** since boot - i.e. it has been within 27 KB of running
  out, and there are 26,544 bytes of margin left at the peak.

The bulk of that is packet buffers and socket queues:
`TCPIP_TCP_MAX_SOCKETS = 10` sockets × (512 TX + 512 RX) = 10,240 bytes of
socket buffers alone, plus the MAC driver's RX/TX descriptors and the packet
pool for two interfaces, plus ARP/DHCP/DNS caches.

### 3.3 What a TLS handshake needs from the heap

wolfSSL 5.4.0 with `USE_FAST_MATH`, no static-memory pool, no custom
allocators - so every one of these is a `malloc()` from the same 160 KB arena.
Sizes read from the build's own DWARF, so they are this build's numbers, not
upstream defaults:

| Structure | Bytes | Lifetime |
|---|---:|---|
| `RsaKey` | **4,516** | one per key in play: the board's own private key, *plus* one per peer certificate being verified |
| `ecc_key` | 2,308 | only if an ECC suite/cert is used (not on this bench) |
| `DecodedCert` | 864 | during peer-certificate parsing, per certificate |
| `WOLFSSL` (the session) | 640 | whole connection |
| `Arrays` (handshake secrets, randoms, pre-master) | 492 | handshake only, freed on completion |
| `Aes` | 428 | whole connection, ×2 (encrypt/decrypt) |
| `Hmac` | 380 | whole connection, ×2 |
| `WOLFSSL_CTX` | 264 | one per role, shared by all its connections |
| `Keys` | 220 | whole connection |
| `wc_Sha256` / `wc_Sha` | 124 / 112 | handshake hashes |
| `WOLFSSL_SESSION` | 180 | (`NO_SESSION_CACHE` is set, so not cached) |

`RsaKey` being 4.5 KB is a direct consequence of `USE_FAST_MATH`: an `fp_int`
is **552 bytes** in this build and `RsaKey` holds eight of them inline. That is
the single biggest per-connection allocation, and it is why a handshake's heap
demand is dominated by key material rather than by buffers.

On top of the structures come wolfSSL's I/O buffers: the input and output
buffers start small and are grown by `GrowInputBuffer`/`CheckAvailableSize` to
whatever the largest incoming record needs. A handshake that carries a
2048-bit certificate chain pushes them into the low kilobytes each; steady
state afterwards is much smaller. Note that both peers send a certificate here
- this is mutual TLS - so the *verifying* side allocates a `DecodedCert` plus
an `RsaKey` for the peer on top of its own.

**Order-of-magnitude for one handshake: ~12-16 KB** (own `RsaKey` 4.5 K +
peer `RsaKey` 4.5 K + `DecodedCert` 0.9 K + session/arrays/hashes ~2 K + I/O
buffers). Against 65 KB of non-TCP/IP heap that is comfortable for the *one*
Telnet session this firmware allows (`TCPIP_TELNET_MAX_CONNECTIONS = 1`) plus
the bootload, cert-provisioning and MQTT-client sessions - but it is also why
running several handshakes at once against one board has never worked well on
this bench, and why `discover.py` gave up on the parallel /24 sweep.

> **What could not be measured, and how to measure it properly.** The XC32
> lite allocator keeps no high-water counter, and since `_sbrk` hands out the
> whole arena at once there is nothing observable from the debug probe either:
> reading the break before and after handshakes gives 163,840 both times.
> Every number in this section is therefore *derived* from exact struct sizes,
> not weighed on the board. To weigh it, the cheap route is wolfSSL's own
> accounting: build with `WOLFSSL_TRACK_MEMORY` and read
> `wolfSSL_GetMemStats()` (peak, current, total allocations) from a console
> command; the general route is a `malloc`/`free` wrapper in the application
> that keeps a running total and a maximum. Either is a small, contained
> change - worth doing before anyone tries to shrink the heap.

### 3.4 Does the heap have to be 160 KB?

Split the question, because the two halves have different answers.

**The 96 KB TCP/IP part: no, but not by much.** Its own peak is 71,664 bytes,
so ~26 KB above the observed peak is dead weight *for the traffic this bench
has seen*. Cutting `TCPIP_STACK_DRAM_SIZE` to, say, 80 KB would still leave
headroom - but the peak was measured with one Telnet session, one MQTT client
and light traffic. An iperf run across the bridge, or the sniffer/mirror
active, moves far more packet buffers; §8 of the throughput report is the
relevant workload, and the number to re-measure with `heapinfo` before
trusting any reduction. This is the one knob where "measure, then cut" is
straightforward, because the stack reports its own watermark.

**The remaining 64 KB for wolfSSL: yes, probably, but not blindly.** Nothing
in this build reports how much of it is actually used. The estimate in §3.3
says one handshake needs 12-16 KB and the sessions this firmware can have open
at once are few (Telnet 1, bootload 1, cert 1, MQTT client 1), so 64 KB looks
generous by roughly a factor of two. But that is arithmetic on struct sizes,
not a measurement, and the failure mode of getting it wrong is a failed TLS
handshake under load rather than a clean error. Instrument first (see the box
above), then cut.

**What the heap size actually buys:** 160 KB heap + 60.7 KB static + 37.6 KB
stack = 262,140 bytes, i.e. the 256 KB of RAM is fully committed. There is no
spare RAM to grow the heap *into* - any increase has to come out of the stack
or out of static buffers such as `frame_data_pool` (24 KB) or the cpuload
slots (6.4 KB). The stack side is not measured either; 37.6 KB is what is left
over rather than a designed figure, and RSA math with 552-byte `fp_int`s on
the stack is the deepest consumer in the system.

## 4. CPU load

`cpuload` counts DWT cycles per main-loop slot (the round-robin `SYS_Tasks()`
calls) and per interrupt. This is a bare-metal spin loop, so "CPU load" is
always 100 % by construction - the useful quantities are **cycles per pass**
and **where a pass spends them**.

### 4.1 Idle: bridge up, no TLS, no traffic

```
  slot        n          min        max       mean     median  (cycles)
  sys_cmd     553969     404        167831    423      413
  miim        553969     189        3560      198      194
  tcpip       553969     695        12161     828      767
  net_pres    553969     181        1236      247      255
  app         553969     767        40699     887      888
  TOTAL       553969     2631       180567    2984     2909
  isr_tc0     28683      477        920       687      696
TOTAL: mean 2984 cycles (24 us) per pass -> avg 40214 loops/s
CPU load: interrupts 1%, tasks 99%
```

Idle the loop runs **40,200 passes/s at 24 µs each**, spread almost evenly
over the TCP/IP stack (828 cycles), the application (887, mostly the bridge's
own forwarding and the discovery responder) and the console (423). Interrupts
are 1 % - the 1 kHz `SYS_TIME` tick dominates them at 687 cycles a shot.

### 4.2 A TLS handshake

Measured by resetting the counters, closing the console (a held Telnet session
occupies the *only* Telnet slot and makes further handshakes fail - which is
itself worth knowing), running N handshakes from the PC, then reading the
counters back:

| handshakes in the window | `net_pres` cycles | total cycles | longest single `net_pres` call |
|---:|---:|---:|---:|
| 0 (+1 from reopening the console) | 151.0 M | 404.0 M | 89,353,780 = **745 ms** |
| 2 (+1) | 437.0 M | 911.8 M | 89,364,048 = 745 ms |
| 5 (+1) | 847.0 M | 1,584.8 M | 89,879,320 = 749 ms |

Fitting those: **one mutual-TLS handshake costs ~139 million cycles = 1.16 s of
CPU at 120 MHz**, and it lands almost entirely in the `net_pres` slot. That
matches the 1.15-1.2 s wall-clock a lone handshake takes (`discover.py`'s
`TLS_TIMEOUT` comment) - i.e. the handshake is not waiting on the network, it
is *computing*, essentially flat out.

**The number that matters operationally is the 745 ms one.** That is a single
uninterruptible call inside one main-loop pass: the RSA-2048 private-key
operation. For three quarters of a second the round-robin loop does not turn -
no `TCPIP_STACK_Task`, no bridge forwarding pass, no console. Traffic does not
stop dead (RX is interrupt-driven into buffers), but nothing is drained for
745 ms, and everything with a timeout is running one.

That single fact explains a whole string of earlier bench observations:
- a full-/24 TLS sweep needing a 6 s timeout and still losing boards
  (`discover.py`, `MAX_WORKERS`/`TLS_TIMEOUT`);
- 2 of 5 rapid handshakes against one board failing outright in this very
  measurement;
- why the certificate/bootload data ports each get their own connection rather
  than sharing one.

### 4.3 Bulk data through an established TLS connection

Measured with the console's own `dump <addr> <count>`, which turns N bytes of
memory into ~4.94 N bytes of hex text and pushes all of it through the
established mutual-TLS Telnet connection. Three sizes, so the fixed cost of
opening the connection and reading the counters cancels in the slope:

| `dump` | bytes over TLS | wall clock | throughput | `sys_cmd` cycles |
|---:|---:|---:|---:|---:|
| 4,000 | 19,810 | 1.86 s | 10.4 KB/s | 69.2 M |
| 16,000 | 79,062 | 2.50 s | 30.8 KB/s | 143.8 M |
| 32,000 | 158,062 | 3.30 s | 46.8 KB/s | 242.6 M |

Slope between the first and the last, i.e. the marginal cost of 138,252 more
bytes:

| slot | marginal cycles | per byte |
|---|---:|---:|
| **`sys_cmd`** | **173.5 M** | **1,255** |
| `tcpip` | 1.3 M | 9.6 |
| `net_pres` | 0.3 M | 1.8 |
| `app` | -0.4 M | -2.9 |
| sum | 174.7 M | **1,263** |

**Marginal throughput: 94 KB/s, and the CPU is saturated while it happens** -
94 KB/s × 1,263 cycles/byte = ~121 M cycles/s against a 120 MHz core. Bulk
transfer over this connection is CPU-bound end to end, not network-bound.

Two things about *where* those cycles are billed:

- Everything lands in `sys_cmd`, and `tcpip`/`net_pres` stay flat at their
  idle rate. That is not because the network stack does no work - it is
  because a Telnet-issued command pumps the TCP/IP stack **from inside**
  `SYS_CMD_Tasks()`: `F_Telnet_MSG()` (telnet.c hand-patch) calls the stack's
  own tasks directly so a socket can drain while the command is still
  running (see the long comment above `CmdDumpMem()` in `app.c` for why).
  So TLS encryption, TCP processing and the drain-retry loop are all inside
  the `sys_cmd` measurement here.
- 1,263 cycles per byte is **not** the price of encryption. Per ~78-byte
  output line that is 98,500 cycles, 821 µs - a full round of "format the
  line, hand it to the console API, encrypt it as its own TLS record, write
  it to the socket, spin the pump until the socket has room". The per-byte
  crypto part of that is small: AES-128-CBC plus HMAC-SHA-256 in wolfSSL's C
  code on a Cortex-M4 is tens of cycles per byte, i.e. low single-digit
  percent of what is measured here. For a bracket from this very bench:
  plaintext TCP with bulk buffers and no console in the path (iperf,
  Bridge → PC, `iperf_matrix_results.md`) runs at 11.5 Mbit/s = 1.37 MB/s,
  which is ~84 cycles/byte for the whole TCP/IP path.

So the honest reading is: **the console-over-TLS path costs ~1,255 cycles per
byte and tops out around 94 KB/s, dominated by per-line record and print
overhead rather than by the cipher.** A payload path that writes in large
blocks instead of 78-byte lines - `bootload`'s data port on 5567, for instance
- would pay a small fraction of this per byte, which is exactly why the
firmware update uses its own binary port instead of the console.

> Isolating the cipher's own share exactly would need either a plaintext
> Telnet build to difference against, or a counter around wolfSSL's record
> encryption. Neither exists today; the numbers above are what can be measured
> without changing the firmware, and they are an upper bound on the crypto
> cost, not the crypto cost itself.

### 4.4 So where does the compute actually go?

**Per TLS connection: ~1.16 s of CPU, concentrated in a single 745 ms blocking
RSA operation at setup.** That is the dominant cost by a wide margin and it is
asymmetric - the board pays it, the PC pays almost nothing.

**Per byte afterwards: the cipher is cheap, but this firmware's console path
around it is not** - 1,255 cycles/byte, CPU-bound at ~94 KB/s, almost all of it
per-line framing and flow control rather than cryptography.

**Steady state with no TLS traffic:** the cycles go to the bridge data path and
the TCP/IP stack (828 + 887 cycles per 2,984-cycle pass), not to anything
security-related.

## 5. The crypto hardware this build does not use

The measurements above are all of *software* crypto. The part underneath has
four crypto blocks, and this firmware touches none of them. From the device
header (`packs/ATSAME54P20A_DFP/same54p20a.h`):

| Block | Where | What it is |
|---|---|---|
| **AES** | `0x42002400`, IRQ 130 | AES-128/192/256 in hardware |
| **TRNG** | `0x42002800`, IRQ 131 | true random number generator |
| **ICM** | `0x42002c00`, IRQ 132 | Integrity Check Monitor - SHA over memory regions |
| **PUKCC** | IRQ 133, `ID_PUKCC = 76` | Public Key Cryptography Controller |

PUKCC has no `PUKCC_REGS` in the header on purpose: it is not driven through
user registers but through the **PUKCL library, which sits in ROM inside the
device**, with its parameters passed in a dedicated 4 KB Crypto RAM. Per the
data sheet, [§43.1](https://onlinedocs.microchip.com/oxy/GUID-F5813793-E016-46F5-A9E2-718D8BCED496-en-US-15/GUID-85A335EF-0029-47C3-B3B1-05A040D0F82D.html),
it implements RSA/DSA modular exponentiation **with CRT up to 7168 bits**
(without CRT up to 5376), prime generation, and ECDSA over GF(p) up to
521 bits - RSA-2048 with CRT is comfortably inside that range.
[§43.3.1](https://onlinedocs.microchip.com/oxy/GUID-F5813793-E016-46F5-A9E2-718D8BCED496-en-US-15/GUID-8D532BF9-2B97-4A52-AEB7-33EDA9490E02.html)
says in as many words that the library "can be used in conjunction with a SSL
software stack to improve performance and helps to reduce the RAM usage and
time taken to perform different cryptographic functions".

**Verified unused here:** outside the pack headers, the only references to
`AES_REGS`, `ICM_REGS`, `TRNG_REGS` or `PUKCC` in the whole source tree are the
interrupt vector tables (`device_vectors.h`, `interrupts.c`) - i.e. empty
handler slots. The 745 ms of §4.2 is wolfSSL's software RSA
(`sp_cortexm.o`, the 19.7 KB of hand-written Cortex-M assembly from §1) doing
the modular exponentiation on the core.

**The plumbing is half-built already**, which is why this is worth writing
down rather than filing away: `WOLF_CRYPTO_CB` (wolfSSL's crypto-callback
framework) is already defined in `configuration.h`, and the vendored wolfSSL
carries the PUKCC port - `third_party/wolfssl/wolfssl/wolfcrypt/port/pic32/CryptoLib_*.h`
is exactly the PUKCL interface.

Three caveats before anyone reads this as a free win:

1. **PUKCL is not a DMA-style offload.**
   [§43.3.8](https://onlinedocs.microchip.com/oxy/GUID-F5813793-E016-46F5-A9E2-718D8BCED496-en-US-15/GUID-2CF0C526-FE9E-4BE9-BEA2-C192956C7507.html):
   "This library is using the main core to execute its computations, and
   therefore is also sharing some resources with the application." The
   handshake would get faster; the blocking call would get *shorter*, not
   disappear. Every timeout tuned around the current 745 ms would want
   re-checking rather than removing.
2. **It has its own setup requirements**: a wait for the Crypto RAM clear
   (`PUKCCSR & BIT_PUKCCSR_CLRRAM_BUSY`) and a mandatory SelfTest at init
   ([§43.3.3.1](https://onlinedocs.microchip.com/oxy/GUID-F5813793-E016-46F5-A9E2-718D8BCED496-en-US-15/GUID-7C82F552-E5A2-4284-AC7B-EB861635D1B4.html)),
   parameters that must live in the 4 KB Crypto RAM, and stack of its own
   (ExpMod 200 bytes, CRT 304 bytes - and see §3.4 on how little stack
   headroom is designed rather than left over).
3. **The data sheet's own numbers are the thing to look up first.**
   [§43.3.8](https://onlinedocs.microchip.com/oxy/GUID-F5813793-E016-46F5-A9E2-718D8BCED496-en-US-15/GUID-2CF0C526-FE9E-4BE9-BEA2-C192956C7507.html)
   carries a "Service Timing for RSA" table with estimated performance at
   120 MHz. Its values are deliberately **not** reproduced here - they could
   not be extracted reliably at the time of writing, and a guess is worse
   than a pointer. That table against the measured 745 ms is the decision.

AES and ICM are the smaller prizes. Per §4.3 the per-byte cost of an
established connection is dominated by the console's per-line framing, not by
the cipher, so hardware AES would move a few percent of a number that is
already not the problem. TRNG is the one that may matter for correctness
rather than speed if wolfSSL's entropy source is ever revisited.

## 6. Summary

| Resource | Total | TLS/cert/MQTT share |
|---|---:|---|
| Flash | 369,992 / 507,904 (73 %) | ~161 KB (44 % of the image) |
| Static RAM | 60,716 / 262,144 (23 %) | ~5.2 KB (`cert_provision`) + 112 B (wolfSSL) |
| Heap | 163,840 committed | ~12-16 KB per handshake, derived; 96 KB is the TCP/IP stack's |
| CPU, per connection | 40,200 loop passes/s idle | 1.16 s per handshake, 745 ms of it in one blocking call |
| CPU, per byte | plaintext TCP ~84 cycles/byte (iperf) | console over TLS 1,255 cycles/byte, CPU-bound at ~94 KB/s |

TLS is a **flash-and-latency** cost on this part, not a RAM cost. It is the
largest single thing in the image, it blocks the main loop for three quarters
of a second per connection, and it fits into RAM without difficulty at the
connection counts this firmware allows. The per-byte cost of an established
connection is dominated by the console path around the cipher, not by the
cipher.

## 7. If any of this has to come down

Roughly in order of return per unit of risk:

1. **`.rodata.F_DRV_PHY_SMITr`, 43,600 bytes** in `drv_ethphy.o` - nothing to
   do with TLS, 8.7 % of the flash bank, and worth a look at whether this
   build needs that table at all.
2. **Unreachable wolfSSL code**: `ecc.o` (10.0 K) and `des3.o` (3.7 K) are
   linked for cipher suites this bench never negotiates. Trimming the suite
   list in `configuration.h` and rebuilding is a compile-time change with a
   measurable, easily verified effect.
3. **`TCPIP_STACK_DRAM_SIZE` 98,304 -> ~80 K**, after re-measuring `heapinfo`'s
   watermark under the heaviest workload that matters (iperf across the
   bridge, sniffer on). The stack reports its own peak, so this one can be
   done honestly.
4. **`frame_data_pool` 24,292 bytes** of static RAM - the capture pool, only
   useful when the mirror/sniffer is in use.
5. **The heap's non-TCP/IP 64 KB** - probably twice what is needed, but
   instrument wolfSSL's allocations first (§3.3 box). Do not cut this one on
   arithmetic alone.
6. **The 745 ms handshake stall** is not a memory problem but it is the
   sharpest edge here. In order of expected return: hand the modular
   exponentiation to the **PUKCC** the part already has and this build
   ignores (§5) - the wolfSSL crypto-callback framework and the PUKCL port are
   both already in the tree; or use a smaller key (ECDSA P-256 instead of
   RSA-2048 - `ecc.o` is already linked, and PUKCC does ECDSA too); or accept
   it and keep every timeout in the system above it, which is what the
   host-side tooling does today.

## Related

- `docs/tls-poc-report.md` - what was built and why, §3.1 for the flash budget
  as it stood earlier.
- `docs/cpuload-profiling-report.md` - how `cpuload` works and how to measure
  with it properly.
- `docs/pki-clean-start.md` - the certificate procedure whose runtime cost is
  measured here.
