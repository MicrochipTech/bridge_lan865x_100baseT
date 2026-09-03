# Per-Task CPU-Load Profiling — How It Works, How It Was Tested, What It Found

Added and measured 2026-09-03. Covers the new `cpuload` command: what it
measures, how the mechanism works, the exact test procedure run on the bench
(including a real bug the test itself found and a bench-configuration mixup
that invalidated the first "worst case" run), the resulting numbers, and —
added the same day — `cpuload live`, a once-a-second, redraw-in-place view
of the same table ([§6](#6-addendum--cpuload-live)), then a real per-interrupt
breakdown, a live-mode units toggle, and a second stale-timer bug the testing
caught ([§7](#7-addendum--per-interrupt-breakdown-live-mode-units-and-a-second-stale-timer-bug)),
and finally a scroll-safe rewrite of the live redraw itself plus a CPU-load
percentage ([§8](#8-addendum--live-mode-stopped-after-one-frame-in-a-real-terminal-and-a-cpu-load-)),
then a third corruption bug, an explicit 3-number CPU load, and the
`isr_spi` question resolved for real with a real debugger and the user's own
DMA hunch
([§9](#9-addendum--a-third-corruption-bug-an-explicit-3-number-cpu-load-and-an-open-question-on-isr_spi)),
and finally a fourth live-mode bug — an 80-column line-wrap the fixed
cursor-up math didn't know about
([§10](#10-addendum--a-fourth-live-mode-bug-lines-wider-than-the-terminal)).

---

## 1. Background

The starting question was simple: how much of the board's CPU time is
actually used, and can that be measured without a debugger attached? The
firmware already had one answer — `s_idle_cycle_count`/`s_idle_cycles_per_sec`
in `app.c`, incremented once per `APP_STATE_IDLE` pass and printed by `stats`
as `main loop: N cycles/s` (the name is historical — it counts loop
iterations, not clock cycles). That is a single overall rate: it tells you
the loop is spinning slower under load, not *which* of the loop's five polled
calls got more expensive.

`cpuload` is the finer-grained companion: cycle-accurate timing of each call
`SYS_Tasks()` makes per pass, using the Cortex-M4's built-in DWT cycle
counter, opt-in from the CLI and off by default.

## 2. How it works

### 2.1 The thing being measured

`SYS_Tasks()` (`firmware/src/config/default/tasks.c`, MCC-generated) is the
bare-metal build's main loop — a plain, unconditional, non-blocking sequence
of five calls, run back to back forever:

```
SYS_CMD_Tasks()                          - console command processing
DRV_MIIM_OBJECT_BASE_Default.miim_Tasks()- eth1 PHY management (MDIO)
TCPIP_STACK_Task()                       - the TCP/IP stack, both interfaces
NET_PRES_Tasks()                         - presentation/socket layer
APP_Tasks()                              - this project's own state machine
```

There is no RTOS here and no separate idle task — every one of those five
calls runs to completion before the next one starts, so "CPU load" can only
mean "how many cycles did each call take, and how does that shift under
traffic."

### 2.2 Timing source

The Cortex-M4 has a free-running 32-bit cycle counter, `DWT->CYCCNT`, that
increments once per core clock (120 MHz on this board — 8.33 ns/cycle). It is
disabled by default; `cpuload on` arms it:

```c
CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
DWT->CYCCNT = 0u;
DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
```

Reading it is a single register access, and a 32-bit unsigned subtraction
(`DWT->CYCCNT - entry`) gives the correct elapsed cycle count even across one
counter rollover (~35.8 s at 120 MHz), because unsigned wraparound arithmetic
does the right thing here.

### 2.3 Six slots

One per call above, plus a `TOTAL` slot wrapping the whole `SYS_Tasks()` pass
(not a sixth call):

```c
CPULOAD_SLOT_SYS_CMD, CPULOAD_SLOT_MIIM, CPULOAD_SLOT_TCPIP,
CPULOAD_SLOT_NET_PRES, CPULOAD_SLOT_APP, CPULOAD_SLOT_TOTAL
```

Each of the five real calls, and the pass as a whole, is bracketed with
`CPULOAD_Enter(slot)` / `CPULOAD_Exit(slot)` — added to `tasks.c` as a
documented hand-patch (`docs/mcc-generated-code-patches.md` item 12), since
that file is MCC-owned and gets reverted on every `Generate Code` run.
`Enter`/`Exit` are a single `if (!enabled) return;` each when `cpuload` is
off, so leaving the instrumentation compiled in costs nothing until armed.

### 2.4 What gets recorded per slot

- **min / max / sum / count** — updated in O(1) on every sample, exact and
  unwindowed: they cover everything since the last `on`/`reset`.
- **median** — over only the most recent ≤128 samples (a small ring buffer),
  sorted with `qsort` on demand when `cpuload stats` is typed. Cheap to keep
  at 120 MHz, but it will miss a spike old enough to have scrolled out of
  that 128-sample window — `min`/`max`/`mean` are the ones with full history.

### 2.5 CLI

```
cpuload on      - reset all counters, arm the DWT counter, start recording
cpuload off     - stop recording; last values stay readable
cpuload reset   - clear counters, keep the current on/off state
cpuload stats   - print the table (also the default with no argument)
```

Full syntax and a sample table are in `docs/cli-reference.md`.

## 3. Test procedure

Bench: `Bridge` (this firmware, COM8, `eth0` 10BASE-T1S / `eth1` 100BASE-TX),
`FollowerA` and `FollowerB` (T1S-only follower firmware, COM10 / COM23),
all commanded over their EDBG serial CLI via `scripts/cli.py`.

### 3.1 Build and flash

```
python patches/apply_patches.py --check   # confirm all hand-patches present, including the new tasks.c one
build.bat                                  # MPLAB X's own make, CONF=default, TYPE_IMAGE=PRODUCTION
flash.bat                                  # pyOCD, probe selected via json/bench.json
```

Adding `cpuload.c` as a new source file needed one extra one-time step beyond
the code itself: MPLAB X derives `nbproject/Makefile-*.mk` from
`nbproject/configurations.xml`, and the Makefile fragments are gitignored
(machine-specific absolute paths). The new file was registered the proper way
— `<itemPath>../src/cpuload.c</itemPath>` added to `configurations.xml`, then
`batch/genmk.bat firmware/tcpip_iperf_lan865x.X` regenerated the Makefiles
headlessly (no IDE needed) — rather than hand-editing the generated Makefile,
which was tried first and reverted once the proper path was clear.

### 3.2 Phase A — quiet bus (baseline)

```
cli.py --port COM8 "cpuload on"            # arm, stats reset
                                            # ~8 s idle, no CLI/network traffic
cli.py --port COM8 "cpuload stats"
```

### 3.3 Phase B — sniffer mode + two followers at full 10 Mbit UDP (worst case)

```
cli.py --port COM8 "cpuload reset" "sniffer 1"       # reset counters, arm the T1S tap
cli.py --port COM23 "iperf -s -u"                     # FollowerB: UDP server
cli.py --port COM10 "iperf -c <FollowerB-ip> -u -t 30"# FollowerA: UDP client, default 10 Mbit
                                                        # ~12-20 s running, then sample:
cli.py --port COM8 "cpuload stats" "stats"
cli.py --port COM8 "sniffer"                           # cross-check: mirror/sniffer frame counters
```

Cleanup: `iperfk` on both followers, `sniffer 0` and `cpuload off` on the
bridge, `netinfo` to confirm both links back up and forwarding restored.

### 3.4 Two problems the test itself surfaced

**A real firmware bug, found on the first run.** The first Phase B sample
showed `sys_cmd` and `TOTAL` `max` in the billions of cycles — an obviously
bogus ~31 s "call". Root cause: `cpuload reset` is itself a console command,
so it executes *from inside* `CPULOAD_SLOT_SYS_CMD`'s own open
`Enter`/`Exit` bracket. The original code stored each slot's in-flight start
timestamp in the same struct `ResetStats()` cleared, so `reset` zeroed the
timestamp of the very call it was running inside of — the next `Exit()` then
computed `DWT->CYCCNT - 0`, a huge number. Fixed by moving the in-flight
timestamps into a separate array that `ResetStats()` never touches. Rebuilt,
reflashed, reproduced clean before trusting any further numbers — see the
comment above `s_entry` in `cpuload.c` for the full writeup.

**A stale bench assumption, found on the corrected first "worst case" run.**
With the bug fixed, the first Phase B run still showed almost no captured
traffic (`sniffer`'s `rx_hook=3`) despite the iperf client reporting a normal
start. `netinfo` on both followers showed why: FollowerA's own `eth0` address
is `192.168.0.31` and FollowerB's is `192.168.0.21` — the opposite of what
`scripts/iperf_matrix_test.py`'s `DEVICES` table records (`FollowerA=.21`,
`FollowerB=.31`). The client had been pointed at itself. Re-run against the
correct address produced a real flood (10,141 frames captured in under 10 s).
This is a bench-state drift, not a firmware issue — worth a look if that
script is still relied on for its recorded IPs.

## 4. Results

### 4.1 Quiet bus (baseline)

```
cpuload: ENABLED (DWT cycle counter, 120.0MHz core clock)
  slot        n          min        max       mean     median  (all in cycles)
  sys_cmd     848274     200        3225      206      205
  miim        848274     185        3544      204      202
  tcpip       848274     688        23268     762      718
  net_pres    848274     168        999       170      168
  app         848274     317        1249      337      335
  TOTAL       848274     1935       24714     2039     1984
TOTAL: mean 2039 cycles (16 us) per pass -> avg 58852 loops/s
```

Cross-checked against the existing coarse counter: `stats` reported
`main loop: 56939 cycles/s` at the same time — same order of magnitude, as
expected (it counts a different, narrower loop state).

### 4.2 Sniffer + genuine full 10 Mbit UDP load (worst case)

```
cpuload: ENABLED (DWT cycle counter, 120.0MHz core clock)
  slot        n          min        max       mean     median  (all in cycles)
  sys_cmd     1569558    200        57661     212      205
  miim        1569558    178        4198      208      204
  tcpip       1569558    689        38155     1436     713
  net_pres    1569558    168        1382      173      168
  app         1569558    313        1940      343      335
  TOTAL       1569558    1935       91467     2737     1979
TOTAL: mean 2737 cycles (22 us) per pass -> avg 43843 loops/s
```

`stats` at the same moment: `eth0 RX: ok=24049`, `main loop: 56927 cycles/s`
(the coarse counter samples over its own trailing 1 s window and moves more
slowly than the finer per-call breakdown above).

### 4.3 Baseline vs. worst case

| Slot | Mean, idle | Mean, worst case | Change | Median, idle | Median, worst case |
|---|---|---|---|---|---|
| sys_cmd | 206 | 212 | +3% | 205 | 205 |
| miim | 204 | 208 | +2% | 202 | 204 |
| **tcpip** | **762** | **1436** | **+88%** | 718 | 713 |
| net_pres | 170 | 173 | +2% | 168 | 168 |
| app | 337 | 343 | +2% | 335 | 335 |
| **TOTAL** | **2039** | **2737** | **+34%** | 1984 | 1979 |
| **loops/s (derived)** | **58852** | **43843** | **−25%** | — | — |

## 5. Summary

- `cpuload` gives exact, per-task cycle timing of the bare-metal main loop,
  opt-in and free when off, complementing the existing coarse loop-rate
  counter rather than replacing it.
- Under a quiet bus, the loop spins at ~58,850 passes/s, ~2,039 cycles
  (~17 µs) each, dominated by `TCPIP_STACK_Task()` (~760 cycles) even with
  nothing to forward.
- Under the intended worst case — bridge in sniffer mode, two follower nodes
  saturating the shared T1S segment with 10 Mbit/s UDP — the loop rate drops
  by about a quarter (~43,850/s) and `TCPIP_STACK_Task()`'s **mean** cost
  nearly doubles. Its **median** barely moves, which means the extra cost is
  concentrated in a fraction of passes (the ones actually handling a freshly
  mirrored frame), not spread evenly across all of them — the common-case
  path stays cheap; the sniffer/mirror path is where the load actually goes.
- Even at this worst case, the board is nowhere near saturated: over
  40,000 loop passes per second remain, so there is no evidence the 10BASE-T1S
  segment's own ~9.4 Mbit/s ceiling (`docs/iperf_matrix_results.md`) is a CPU
  limitation on this board.
- Two things worth carrying forward beyond the numbers themselves: the fix in
  `cpuload.c` (never let a stats-clearing command touch an in-flight
  timestamp it might itself be running inside of), and the bench IP mismatch
  against `scripts/iperf_matrix_test.py`'s `DEVICES` table, which would
  silently mis-target any test run against the addresses recorded there.

## 6. Addendum — `cpuload live`

Added the same day, once the numbers above were in: a live view of the same
table, redrawn in place once a second instead of scrolling.

**Mechanism.** `cpuload live` cannot block inside its own command handler —
that would freeze the whole bridge for as long as someone watches the screen
— so it is a *mode*, not a blocking call. A new `CPULOAD_LivePoll()` runs as
the first statement of every `SYS_Tasks()` pass (before `SYS_CMD_Tasks()`),
and while a console is live it:

- reads that console's pending bytes itself, via the same generic
  `SYS_CMD_DEVICE_NODE{ pCmdApi; cmdIoParam }` vtable every command handler
  already uses (`sys_command.h`) — `isRdy()`/`getc_t()`, non-blocking, one
  byte at a time. Running before `SYS_CMD_Tasks()` lets it consume `r`/`q`
  before the normal line editor ever sees them, on the same pass, so neither
  key needs Enter and nothing races the command parser.
- redraws once a second using the same `SYS_TIME_Counter64Get()`-deadline
  idiom already used elsewhere in this codebase (`app.c`'s
  `s_banner_deadline`, `lan865x_diag.c`'s `s_revert_tick`), via `\x1b[s`
  (save cursor, once) then `\x1b[u\x1b[J` (jump back, erase to end of screen)
  before each repeated print of the same `CPULOAD_PrintStats()` table.

**Verified 2026-09-03**, over the board's raw serial stream (not `cli.py`,
which frames every send as a full line — a throwaway script instead, sending
single un-terminated bytes for `r`/`q`):

- `\x1b[s`, `\x1b[u`, `\x1b[J` all present in the captured stream.
- 5 table redraws captured in 4.5 s of `cpuload live` — the ~1 Hz cadence.
- A bare `r` (no CR/LF) reset the counters immediately (`n` dropped from
  207,012 to 25,430 and kept growing from there).
- A bare `q` (no CR/LF) stopped the live view (`cpuload live: stopped`)
  without a trailing keystroke, and normal command handling resumed cleanly
  on the same console right after (`netinfo` answered normally).

Not independently verified: the actual *visual* in-place redraw in a real
VT100 terminal (TeraTerm) — the byte-level check above confirms the right
escape sequences go out at the right times, but seeing "no scrolling" is
inherently something to look at, not parse. Left for a TeraTerm session.

**Known limitation, documented rather than solved:** if a Telnet session
starts `cpuload live` and disconnects without pressing `q`, the board keeps
treating that session as "the live console" (harmless — it just blocks a
*new* `cpuload live` elsewhere until that one is reclaimed or the board
resets). Not building full Telnet session-lifecycle tracking to close this
gap; see the comment above `s_livePCmdIO` in `cpuload.c`.

## 7. Addendum — per-interrupt breakdown, live-mode units, and a second stale-timer bug

Added 2026-09-03, same day again: interrupts run invisibly to everything
above — worse, since the DWT counter never stops for an interrupt, ISR time
was always silently folded into whichever main-loop slot happened to be open
when it fired. The user asked for a real per-interrupt breakdown, toggled by
the same overall on/off, plus a live-mode key to switch displayed units
between cycles and microseconds.

**Mechanism.** This board has 6 real interrupt handlers (`interrupts.c`'s
vector table + `interrupts.h`): `DMAC_0`, `DMAC_1`, the LAN865x/TC6 SPI
driver (`SERCOM0`, 4 vectors → 1 handler), the console UART (`SERCOM1`, same
shape), `GMAC` (eth1), and the `SYS_TIME` tick (`TC0`). Rather than editing
all 5 peripheral-driver files that implement them — finding and preserving
every early-return path inside each — `interrupts.c`'s vector table already
indirects every ISR through one designated-initializer struct, so 6 tiny
pass-through wrappers in `cpuload.c` (`Enter(ISR slot)` / real handler /
`Exit(ISR slot)`) go in the vector table instead
(`docs/mcc-generated-code-patches.md` item 13). The 5 driver files stay
completely untouched. Same shared `cpuload on`/`off` arms both sections.
`TOTAL` still means only the whole `SYS_Tasks()` pass — the interrupt slots
are a separate section, not folded into it, and (documented, not solved)
interrupt time that preempts an open main-loop bracket is still silently
counted inside that bracket too; nothing subtracts it back out.

The live-mode unit toggle (`t` = microseconds, `c` = cycles) reuses the same
table-printing code with a `showUs` parameter and forces an immediate redraw
on press, the same as `r` now also does — no more waiting out the rest of a
second to see the effect.

**A second real bug, found the same way as the first.** With profiling
armed, disabled, then re-armed (`cpuload on` → `off` → `on`), `sys_cmd`/
`TOTAL` showed another huge bogus sample (~2.7×10⁹ cycles) — same symptom as
the `cpuload reset` bug in [§3.4](#34-two-problems-the-test-itself-surfaced),
different cause. `CPULOAD_Enable(true)`
resets `DWT->CYCCNT` to 0, and it runs from *inside* `SYS_CMD`'s own open
bracket (it's the body of the `cpuload on` command). At that bracket's own
`Enter()` earlier in the same pass, `s_enabled` was still `false` — a no-op,
so no fresh timestamp was recorded — but by the time that bracket's `Exit()`
ran, `on` had already flipped `s_enabled` true, so `Exit()` fired for real
against whatever stale value `s_entry[SYS_CMD]` happened to hold from a much
earlier session (deliberately never cleared by `ResetStats()`, per the first
fix). Fixed by tracking, per slot, whether `Enter()` actually recorded a
fresh timestamp for the *currently open* bracket (`s_entryValid[]`) —
`Exit()` now trusts that instead of just "is profiling enabled right now".
Reproduced the exact `on`/`off`/`on` sequence after the fix: clean. See the
comment above `s_entryValid` in `cpuload.c` for the full writeup.

**Verified on hardware, 2026-09-03:**
- All 6 interrupt rows show sane, nonzero counts except `isr_spi`, which
  stayed at "no samples yet" across every run — not a bug: this driver's SPI
  transfers complete via the paired DMA-channel interrupts (`isr_dmac0`/
  `isr_dmac1`, equal counts each run), not the SERCOM0 interrupt itself.
- `t` shrank every number into the µs range and swapped the column header
  (e.g. `tcpip` mean 718 cycles → 5 µs, matching 718 ÷ 120); `c` switched
  back; both redrew immediately rather than waiting up to a second.
- `q` still stopped cleanly; normal command handling and network forwarding
  (`netinfo`, both links up) were unaffected by profiling being armed —
  the one new thing genuinely sitting in a hot path, the wrapper indirection
  on `SERCOM0_SPI`/`GMAC`, showed no observable regression.

## 8. Addendum — live mode stopped after one frame in a real terminal, and a CPU-load %

Reported by the user immediately after §7 shipped: `cpuload live` in an
actual terminal (TeraTerm) drew one frame and then never updated again — it
had worked fine before the interrupt section existed. The byte-level checks
in §6/§7 couldn't have caught this: they confirm the right escape bytes go
out, not what a terminal does with them.

**Root cause.** The redraw used `\x1b[s` (save cursor) once, then
`\x1b[u` (restore) before every later frame. Both are *absolute* — save
records a `(row, col)` on the screen as it exists at that instant. The table
grew from ~10 lines (§6) to 19 once the interrupt section (§7) was added.
Once that no longer fits inside the terminal's visible height, printing it
scrolls the terminal — and after even one scroll, restoring the old
`(row, col)` lands on whatever now happens to be at that position, not on
the table. First frame: no scroll yet, drew correctly. Every frame after:
the terminal had already scrolled during the first frame's own printing, so
`\x1b[u]` was already stale — silent failure, indistinguishable from "stopped
updating" to someone watching.

**Fix:** move the cursor up by a *fixed, known* line count instead
(`\x1b[<N>A` then `\r\x1b[J`), relative to wherever the cursor currently is
— this keeps working no matter how many times the terminal has scrolled,
since it never depends on an absolute position. Made every line in
`PrintStatsTable()` unconditional (a "(no samples yet)" placeholder for
anything with zero samples, including the `TOTAL` and new `CPU load` summary
lines) specifically so the frame height is a true compile-time constant
(`CPULOAD_LIVE_FRAME_LINES` = slot count + 7 fixed lines = 19) — no frame is
ever a different height than the last, so a single fixed cursor-up always
lands in the right place. See the comment above the `CPULOAD_LivePoll()`
redraw call in `cpuload.c`.

**Verified on hardware, 2026-09-03** (raw byte capture, 8 s of `cpuload
live`): 8 table redraws, matching 8 `CPU load:` lines, every cursor-up
sequence exactly `\x1b[19A` (7 of them — one per frame after the first), no
`\x1b[s`/`\x1b[u` anywhere in the stream, and `TOTAL`'s sample count strictly
increasing across all 8 frames — proof each one is a fresh live redraw, not
a frozen repeat. The one thing still not verified by this project the way
everything else here is — actually watching it redraw in place in a real
terminal — is now up to the user to confirm in TeraTerm; the mechanism is a
standard, scroll-immune technique, so this should hold regardless of window
size, unlike the save/restore approach it replaced.

**CPU-load percentage, added the same fix.** The table now ends with a
plain 2-way split of every cycle measured so far — interrupts vs. the main
loop, always summing to 100% — e.g. `CPU load: 1% in interrupts, 99% in the
main loop` at idle. There is no idle task in this bare-metal loop to compare
against for a classic "busy vs. idle %"; the interrupt/main-loop split is
the closest honest equivalent, and directly answers "how loaded is the CPU"
using exactly the two halves (§3, §7) this feature already measures. Formula:
`isrCycles = Σ(interrupt slots' summed cycles)`, `wallCycles = TOTAL`'s
summed cycles (every measured pass, which already includes any interrupt
time that preempted it — see the caveat in §7), `pct = 100 × isrCycles ÷
wallCycles`, clamped to 100 as a defensive measure against the one gap in
that accounting: `CPULOAD_LivePoll()` itself runs just outside any `TOTAL`
bracket (`tasks.c`), so an interrupt landing in that narrow gap is not
reflected in `wallCycles` — negligible against thousands of measured passes.

## 9. Addendum — a third corruption bug, an explicit 3-number CPU load, and an open question on `isr_spi`

Two more things the same day: the user asked for `isr_spi`'s "no samples"
result to actually be explained rather than assumed, and for the CPU-load
line to show three explicit numbers (interrupts %, tasks %, and their sum)
rather than implying the second from the first.

**Third corruption bug, root-caused properly this time.** Investigating
`isr_spi` surfaced *another* instance of the same billions-of-cycles symptom
from §3.4/§7 — this time from typing `cpuload on` while it was *already* on.
Root cause, finally the real one: `CPULOAD_Enable(true)` reset
`DWT->CYCCNT` to 0 unconditionally, including when called from inside
`SYS_CMD`'s own bracket while that bracket's `Enter()` had *already* recorded
a real, valid, pre-reset timestamp earlier in the same pass (a redundant
`on` doesn't hit the "Enter ran while disabled" case §7's `s_entryValid` fix
covers — it hits a different one: entry valid, but the counter it was taken
from gets zeroed out from under it moments later, in the same bracket).
`Exit()` then subtracted the old, large entry from the freshly-zeroed
counter and underflowed. Fixed at the actual root instead of patching around
it again: `CPULOAD_Enable(true)` no longer resets `DWT->CYCCNT` at all —
`Enter()`/`Exit()` only ever use the delta between two reads, which is
correct via unsigned wraparound regardless of the counter's absolute value,
so the reset was never load-bearing, only cosmetic. Reproduced the exact
"on, then on again" sequence after the fix: clean, twice.

**CPU load, three explicit numbers.** `CPU load: interrupts 1%, tasks 99%,
total 100%` — same underlying computation as §8 (`taskPct = 100 - isrPct`,
so the two always add to exactly the third rather than being left for the
reader to add up themselves).

**`isr_spi`: investigated properly, not resolved.** The claim in §7/the
`cli-reference.md` example — that this driver's SPI transfers complete via
the DMA-channel interrupts instead of the SERCOM0 one — was wrong, and
worth recording as a wrong turn rather than quietly fixing. What was
actually checked, in order:

1. Read the driver chain end to end: `TC6_CB_OnSpiTransaction()`
   (`drv_lan865x_api.c`, the *one* user callback the TC6 library uses for
   **every** SPI transfer, control commands and data chunks alike, per its
   own doc comment) always calls `DRV_SPI_WriteReadTransferAdd()`, which
   calls the registered PLIB's `writeRead` — `SERCOM0_SPI_WriteRead()` in
   `plib_sercom0_spi_master.c`. That function's own comment: "Start the
   first write here itself, rest will happen in ISR context" — confirming
   any transfer beyond a single byte, and even a single byte's completion
   signal, needs `SERCOM0_SPI_InterruptHandler()` to run. No DMA anywhere in
   this file.
2. Confirmed `NVIC_EnableIRQ()` is called for all 4 SERCOM0 vectors
   (`SERCOM0_0/1/2/OTHER_IRQn`, `peripheral/nvic/plib_nvic.c`).
3. Re-read `interrupts.c`'s actual vector table struct (`exception_table`,
   `__attribute__((section(".vectors"), used))` — confirmed by its own
   declaration to be the real, linked, hardware vector table, not a
   secondary/unused one) and confirmed via `xc32-nm` on the built ELF that
   `.pfnSERCOM0_0/1/2/OTHER_Handler` really do resolve to the wrapper's
   address, not the original handler's.
4. Triggered one **guaranteed** SPI transaction from the CLI (`lan_read`,
   which returned a real value — the transfer definitely completed over
   real hardware) and checked `cpuload stats` immediately before and after:
   `isr_spi` stayed at "no samples yet" both times.
5. Added a temporary counter (`g_spiIsrHitCount`, tagged `TEMP DIAG`,
   incremented unconditionally at the very top of `CPULOAD_ISR_SPI()`,
   independent of `cpuload`'s own enable/disable logic entirely) to rule out
   a bug in `CPULOAD_Enter()`/`Exit()` specifically. Peeked its address
   (from `xc32-nm`) before and after the same `lan_read`: stayed at 0 both
   times.

Step 5 is the decisive one: it proves the wrapper function itself is never
entered by hardware, for a real SPI transaction that demonstrably completes
successfully — independent of anything `cpuload`'s own logic does. Every
mechanism-level check (vector table content, NVIC enable, linked symbol
address) came back correct, which rules out the obvious explanations without
finding the real one.

**Continued with a real debugger, as suggested by the user** (`pyOCD`'s own
Python API — the same tool `flash.bat` already uses, just driven
interactively instead of via its CLI wrapper). First attempt gave a false
negative: a breakpoint set on a *running* core without halting it first
never fired, on **anything**, including `SYS_Tasks()` itself (the round-robin
loop entered ~58,000×/s) — proving the methodology was broken, not the
firmware. Fixed by halting before arming each breakpoint (confirmed against
that same `SYS_Tasks()` breakpoint, which then fired within milliseconds of
resuming), and redid every check:

- `SERCOM0_SPI_InterruptHandler` — confirmed, reliably this time: never hit,
  across 8s and a guaranteed `lan_read`.
- Traced upward through the whole intended chain
  (`DRV_LAN865X_ReadRegister` → `TC6_ReadRegister` → `TC6_Service` →
  `TC6_CB_OnSpiTransaction` → `DRV_SPI_WriteReadTransferAdd`) with a
  breakpoint on every link at once. `TC6_CB_OnSpiTransaction` fired
  constantly (20 hits in well under a second — this is the continuous TC6
  chunk-exchange heartbeat, unrelated to the specific `lan_read`) —
  confirming SPI activity is genuinely happening continuously. **None of the
  other four ever fired**, `DRV_SPI_WriteReadTransferAdd` included.

**Why, most likely:** `TC6_CB_OnSpiTransaction`'s own body
(`drv_lan865x_api.c:1492`) only calls `DRV_SPI_WriteReadTransferAdd()` inside
`if (!pDrvInst->spiBusy)`; `spiBusy` is set `true` right before that call and
is only ever set back to `false` in `_EventHandlerSPI()`
(`drv_lan865x_api.c:2169`) — the `DRV_SPI` transfer-complete callback, itself
only reachable through `SERCOM0_SPI_InterruptHandler`, which the check above
shows never runs. If the very first SPI transfer this driver ever attempted
(back at boot, well before this debug session attached) got queued
(`spiBusy = true`) and then could never complete for the same reason, every
`TC6_CB_OnSpiTransaction` call since — all 20+ observed, and presumably
every one since boot — would hit that guard and silently return without
ever re-attempting the real transfer.

**Resolved — the user's own suggestion was the answer.** Asked directly
whether DMA might be involved. `firmware/src/config/default/initialization.c`
configures the `DRV_SPI` instance this SPI bus (SERCOM0, the LAN865x's SPI)
uses with **real DMA channels**:

```c
.dmaChannelTransmit = DRV_SPI_XMIT_DMA_CH_IDX0,
.dmaChannelReceive  = DRV_SPI_RCV_DMA_CH_IDX0,
```

(not `SYS_DMA_CHANNEL_NONE`), plus `DMAC_0_IRQn`/`DMAC_1_IRQn` as the
driver's own recorded DMA interrupt lines. `drv_spi.c`'s
`DRV_SPI_WriteReadTransferAdd()` branches on exactly this: DMA channels
configured → `lDRV_SPI_StartDMATransfer()`; otherwise → the PLIB's own
interrupt-driven `writeRead()`. This instance always takes the DMA branch,
so `SERCOM0_SPI_WriteRead()`'s interrupt-arming code (the one read from
`plib_sercom0_spi_master.c` earlier in this section) is present in the
binary but never reached for this SPI bus — dead code for this project's
configuration, not a bug. `SERCOM0_DMAC_ID_TX`/`_RX` (`packs/.../sercom0.h`)
confirm `DMAC_CHCTRLA_TRIGSRC(5)`/`(4)` on DMA channels 0/1
(`peripheral/dmac/plib_dmac.c`) are exactly SERCOM0 TX/RX — the two channels
`cpuload`'s `isr_dmac0`/`isr_dmac1` already track. The DMA completion
handlers (`lDRV_SPI_TX/RX_DMA_CallbackHandler`, `drv_spi.c`) call the same
`_EventHandlerSPI()` the PLIB-interrupt path would have, just via
`DMAC_0/1_InterruptHandler` instead of `SERCOM0_SPI_InterruptHandler` — and
can re-arm further DMA transfers directly from within the callback (the
"dummy data" continuation branches), which is almost certainly why
`TC6_CB_OnSpiTransaction`'s own call into `DRV_SPI_WriteReadTransferAdd` -
confirmed present via the same breakpoint technique, address
`0x00018674` - didn't have to fire again during this session's 8s
observation window for DMA activity to keep showing up: most of what
`isr_dmac0`/`isr_dmac1` were catching was very likely this self-renewing
continuation, not fresh top-level transfer requests.

**Conclusion for `cli-reference.md`/the earlier claim in this section:**
"SPI transfers complete via the DMA-channel interrupts, not the SERCOM0
interrupt" was the *right* explanation after all - reached correctly the
first time from the `isr_dmac0`/`isr_dmac1` correlation, wrongly retracted
after reading only the PLIB file in isolation without checking the DMA
branch one layer up in `drv_spi.c`/`initialization.c`. `isr_spi` reading "no
samples yet" is confirmed-correct behavior for this board's configuration,
not an open question — `cpuload.c`'s comment on `CPULOAD_ISR_SPI()` records
the short version. The temporary `g_spiIsrHitCount` counter (`TEMP DIAG` tag)
has been removed now that the question is settled, matching this project's
own convention for this kind of instrumentation
(`docs/mcc-generated-code-patches.md`'s "Temporary diagnostic
instrumentation" section). The pyOCD debug technique that settled this
(halt, set breakpoint(s), resume, trigger over the serial CLI, poll
`get_state()` via `pyocd.core.helpers.ConnectHelper`) is recorded here in
case a similar question comes up for a different peripheral later; the
breakpoint scripts themselves were disposable one-offs, not committed.

## 10. Addendum — a fourth live-mode bug: lines wider than the terminal

Reported right after §8 shipped, on real hardware in TeraTerm this time (not
the raw byte-level checks that had validated everything up to here): the
live view drew one frame, then stopped updating again — the exact symptom
§8 was supposed to have already fixed.

**Root cause.** The §8 fix moves the cursor up by a *fixed* line count
(`CPULOAD_LIVE_FRAME_LINES`, computed from how many `CMD_PRINT` calls
`PrintStatsTable()` makes) — one physical terminal row per printed line,
*assuming* every line fits the terminal's width. It didn't: the `median`
footer line was 85 columns, and the microseconds-mode column header was 84
— both over the 80 columns a standard terminal window uses, so both
silently wrapped onto a second physical row. `median` is printed
unconditionally, on *every* frame, so every single redraw undershot the
real number of physical rows to move up by one — a small, constant drift
that compounds every second, which looks exactly like "stopped repeating"
once it drifts far enough to no longer land anywhere near the actual
previous frame. The byte-level checks in §6–§9 never caught this because
they only ever verified which bytes went out and in what order, never how
many *columns* wide a rendered line was — the same category of blind spot
§8 already flagged for "watching it redraw" itself, just one layer deeper.

**Fix:** shortened the two offending lines and confirmed every line
`PrintStatsTable()` can print — including the worst case, microseconds mode
— is ≤80 columns, by measuring actual printed output (not by eyeballing
format strings):

| Line | Before | After |
|---|---|---|
| column header (µs mode) | 84 | 67 |
| `median` footer | 85 | 64 |

**Verified on hardware, 2026-09-03:** captured one live frame in each unit
mode (`t` mid-capture), stripped the ANSI escapes, and checked every
resulting line's length directly — zero lines over 80 columns in either
mode. Re-ran the same redraw-cadence check from §8 (8s of `cpuload live`):
still 8 clean redraws, still a consistent `\x1b[19A` each time, `TOTAL`'s
count still strictly increasing across every frame — the mechanism itself
was never wrong, only two of the lines it was measuring against were.

**A pattern worth naming, since this is the second time it bit the same
feature:** anything that redraws a terminal screen by counting *logical*
print calls has to independently guarantee those logical lines never exceed
the terminal's physical width, or the two counts silently diverge. Worth
remembering if `cpuload`'s table grows a column later.
