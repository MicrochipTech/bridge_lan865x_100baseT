# pyOCD Debugging — Practical Recipes for an AI Agent

Practical, tested recipes for debugging this board live with pyOCD, written from
recipes actually used and verified against real hardware on this bench (2026-09-06/07).
This is not a pyOCD tutorial — it's what actually worked on *this* project, with the
gotchas that cost real time the first time around.

**Why pyOCD and not GDB:** there is no ARM GDB in this project's XC32 install
(confirmed by searching the toolchain directory) — pyOCD is the only debugger
available on this bench, driven either via its CLI (`pyocd flash`/`pyocd reset`/
`pyocd commander`, already used by `flash.bat`) or its Python API for anything
more interactive (breakpoints, live register/memory reads).

---

## 1. Environment specifics for this project

- Target part: `atsame54p20a`.
- CMSIS pack: the real Microchip `SAME54_DFP` pack (pyOCD's own public pack index
  only has an outdated third-party mirror missing RAM/flash region metadata, which
  fails with `"CMSIS-Pack device ATSAME54P20A has no default RAM defined"`).
  `scripts/flash_same54.py` finds it automatically — reuse its logic (or just call
  the script) rather than guessing a path. Typical location once resolved to a
  `.pack` zip:
  `%LOCALAPPDATA%\Temp\Microchip.SAME54_DFP.3.11.261.pack` (as prepared by
  `flash.bat`/`flash_same54.py` — a temp zip built from the unpacked pack MPLAB X
  installs). Pass it explicitly with `--pack <path>` to every `pyocd` invocation;
  without it pyOCD falls back to its broken public index.
- Probe selection: this bench can have **multiple boards connected at once**.
  `pyocd -m` list probes with `python -m pyocd list`, or use
  `flash.bat --list` (same thing, wrapped). Pick one with `-u <probe-serial>`.
  `json/bench.json` records the probe serial-to-board mapping this project already
  uses (`"selected"` is the default `flash.bat` picks with no `--probe`) — read it
  before assuming which physical board a bare command without `-u` will hit.
- Python environment: use this project's own `.venv`
  (`.venv\Scripts\python.exe -m pyocd ...` or `.venv\Scripts\pyocd.exe ...`), not a
  bare system Python — pyOCD is a project dependency, not guaranteed globally
  installed.
- Always build `-t atsame54p20a -f 2000000 --pack <pack>` into every invocation;
  `2000000` (2 MHz SWD clock) is the frequency this project's own scripts use and
  is confirmed to work reliably on this hardware.

**Known noise, not errors** — these appear on almost every pyOCD invocation on this
bench and are safe to ignore:
```
W Overlapping memory regions in file ...Microchip.SAME54_DFP...pack (ATSAME54N19A); deleting outer region.
E Error probing AP#2: Memory transfer fault (SWD/JTAG communication failure (FAULT ACK))
E Error probing AP#3: ...
E Error probing AP#4: ...
```
The AP#2–4 errors are pyOCD probing access ports this part doesn't populate; AP#0
(the real Cortex-M4 core) still works fine. Do not treat these as a sign the probe
or target is broken.

---

## 2. Quick, non-interactive checks (CLI)

```bash
# List every connected probe (which board is which)
.venv/Scripts/python.exe -m pyocd list

# Reset only — does not touch flash. Safe to run any time a board seems wedged
# (e.g. TCP/Telnet stopped responding but ping still works — see session-log.md,
# 2026-09-06/07 DoS findings). This is a recovery action, not a flash.
.venv/Scripts/python.exe -m pyocd reset -t atsame54p20a -f 2000000 --pack <pack> -u <probe-serial>

# Read core registers without a full interactive session (development-notes.md's
# own recipe, useful to cross-check a CLI timeout instead of blindly trusting it):
.venv/Scripts/python.exe -m pyocd commander -t atsame54p20a -u <probe-serial> \
    -M pre-reset --elf <path-to-production.elf> -c "reg" -c "exit"
```

Use the **actual linked ELF** for this exact build
(`firmware/tcpip_iperf_lan865x.X/dist/default/production/tcpip_iperf_lan865x.X.production.elf`)
whenever symbols/addresses matter — a stale or generic ELF gives wrong addresses
silently.

---

## 3. HardFault/BusFault postmortem (no live debugger needed)

This firmware already captures fault state itself (`crashlog.c`) — check this
*before* reaching for pyOCD at all:

```
> faultlog
=== Last BusFault ===
...
PC = 0x0001a3f4
LR = 0x0001a2c1
...
```

Resolve the addresses with the toolchain's own `addr2line` against the real ELF —
no debug session or probe needed:

```bash
xc32-addr2line -e <path-to-production.elf> 0x0001a3f4
```

This is exactly how the BusFault in `EncGlueClient_Close()` (missing NULL-guard on
an unopened connection) was root-caused live on 2026-09-06 — faster and less
invasive than attaching a debugger, and it works even after the board has already
recovered from the fault (the log survives until the next reset or an explicit
`faultlog clear`).

---

## 4. Interactive breakpoint debugging (Python API)

For "does function X actually get called, and how often" questions that can't be
answered from the console or a fault log — e.g. confirming a suspected dead code
path, or that a specific ISR never fires (this is exactly how the SPI-DMA
`spiBusy` deadlock was root-caused in `docs/cpuload-profiling-report.md` §9).

**The one gotcha that wastes the most time:** a breakpoint set on a *running* core
never fires — silently, with no error — even for a function that is provably
called constantly. **Halt the core before arming any breakpoint.** This is not
optional and is not documented anywhere obvious; it was discovered by getting a
false negative on `SYS_Tasks()` itself (entered ~58,000×/s) before halting first.

```python
import time
from pyocd.core.helpers import ConnectHelper

PACK = r"C:\path\to\Microchip.SAME54_DFP.3.11.261.pack"
PROBE = "ATML3264031800001049"          # from bench.json / `pyocd list`
ELF = r"firmware\tcpip_iperf_lan865x.X\dist\default\production\tcpip_iperf_lan865x.X.production.elf"

with ConnectHelper.session_with_chosen_probe(
        unique_id=PROBE, target_override="atsame54p20a", pack=PACK) as session:
    target = session.board.target
    target.elf = ELF                     # lets pyOCD resolve symbols by name

    target.halt()                        # REQUIRED before setting breakpoints
    bp_addrs = [target.elf.symbol_decoder.get_symbol_for_name(name).address
                for name in ("SERCOM0_SPI_InterruptHandler",
                              "DRV_SPI_WriteReadTransferAdd")]
    for addr in bp_addrs:
        target.set_breakpoint(addr)
    target.resume()

    # Now trigger the condition under test over the serial/Telnet console
    # (a separate process/socket — pyOCD does not block that), then poll:
    deadline = time.time() + 10
    hit = None
    while time.time() < deadline:
        if target.get_state() == target.State.HALTED:
            hit = target.read_core_register("pc")
            break
        time.sleep(0.05)

    for addr in bp_addrs:
        target.remove_breakpoint(addr)
    target.resume()                      # ALWAYS resume before disconnecting - see §5
    print("hit" if hit else "never fired", hex(hit) if hit else "")
```

Notes learned the hard way on this hardware:
- `session_with_chosen_probe(unique_id=...)` is how to pick a specific probe when
  more than one board is connected — matches the `-u` CLI flag.
- Watch several candidate functions in one pass rather than one at a time when
  tracing a call chain — this found in one run that a suspected intermediate
  function was never reached while its own caller *and* callee both were,
  pinpointing the exact broken link.
- Breakpoint scripts like this are disposable, one-off diagnostics — write them to
  the scratchpad, not into the repo, unless the technique itself needs to be
  reusable.

---

## 5. The one safety rule: a halted core is a dead board on the network

**Halting the target via pyOCD stops the entire firmware, including the TCP/IP
stack and every open TCP connection (Telnet, the MQTT client, the bridge itself).**
If you are also controlling or observing the board over the network in the same
session, that connection will go silent the instant you `halt()` and only comes
back after `resume()`. Always:
- `resume()` before the script exits, in a `finally` block — a script that raises
  between `halt()` and `resume()` leaves the board hung until something else
  resets it.
- Never leave an interactive pyOCD session halted and walk away from it — nothing
  else on the bench (ping, Telnet, another agent's own commands) will work until
  it's resumed.
- Prefer the passive options first (§2's `commander -c "reg"`, or the firmware's
  own `faultlog`/`meminfo`/`stats` console commands) when they can answer the
  question — they don't require halting anything.

---

## 6. pyOCD vs. the network `bootload` path

pyOCD/SWD is for debugging and for recovering a board that has stopped responding
over the network (a plain `pyocd reset`, not a flash). For actually **updating
firmware** on a network-reachable board, prefer the project's own OTA mechanism —
see [`bootload-agent-flashing-guide.md`](bootload-agent-flashing-guide.md). Reach
for `flash.bat`/pyOCD flashing only when the board cannot be reached over the
network at all (bricked, never provisioned, or the network stack itself is what's
being debugged).
