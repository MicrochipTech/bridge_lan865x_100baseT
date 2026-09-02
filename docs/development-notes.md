# Development Notes

Technical background for anyone maintaining or extending this project: the hard
rules this codebase follows, the day-to-day build/flash workflow, and a dated
log of MCC-regeneration pitfalls that cost real debugging time the first time
around. For the full narrative behind any entry below, see
[`session-log.md`](session-log.md); for the patch mechanism itself, see
[`mcc-generated-code-patches.md`](mcc-generated-code-patches.md).

---

## 1. Hard rule: MCC-generated code is never touched by hand

Everything under `firmware\src\config\default\` (drivers, `configuration.h`,
`system_config.h`, `definitions.h`, `initialization.c`,
`peripheral\*\plib_*.c/.h`, etc.) as well as
`firmware\tcpip_iperf_lan865x.X\tcpip_iperf_lan865x_default\` (component
YAMLs, `mcc-config.mc4`) is changed **exclusively via MCC + Generate Code** —
never by manual edits, not even as a quick fix. If something is missing or
wrong in the generated code, the fix belongs in the MCC GUI (pins, component
properties), not in the file.

**Sole exception:** `firmware\src\app.c` / `app.h` (and other genuine
user files outside of `config\default\`) — e.g. the
`TCPIP_STACK_InitCallback` stub lives there.

A handful of fixes below have no MCC GUI equivalent at all (pure generator
bugs, or silicon errata workarounds) and are hand-patched as a documented
exception — see [`mcc-generated-code-patches.md`](mcc-generated-code-patches.md)
for the mechanism that makes those survive a regenerate.

---

## 2. Building, flashing, console

```bat
setup.bat                 :: once per machine, after cloning (venv, pyOCD, debug fix, makefiles)
build.bat                 :: incremental (default), TYPE_IMAGE=PRODUCTION
build.bat rebuild         :: clean + full
build.bat clean
flash.bat                 :: pyOCD via EDBG probe
flash.bat --list          :: connected probes
cli.bat "help"             :: send a command over the serial console
cli.bat --port COM8 --read 3 "reset"
```

- `build.bat` additionally copies the resulting HEX to
  `release\bridge_lan865x_100baseT.hex` after every successful build, so a
  fresh clone can be flashed without building first. Only `build.bat` updates
  this copy — a build from inside the MPLAB X IDE leaves `release\` outdated.
  `flash.bat` flashes exactly this `release\` file by default; to flash a
  fresh local build instead, pass the `dist\` path explicitly.
- `scripts\build_summary.py` runs automatically at the end of every
  `build.bat`: flash/RAM usage from `memoryfile.xml`, heap/stack size from the
  `.map`, active interrupt handlers via `xc32-nm` (needs
  `python scripts\setup_compiler.py` to have been run once). Also archives the
  HEX plus the summary text with a timestamp under
  `firmware\...\dist\default\production\image\` (gitignored).
- `cli.py`'s `--read N` deliberately waits *at least* N seconds before
  exiting — wrapping it in an external `timeout M` with `M < N` kills it
  prematurely and can be misread as "the board is hanging" when it isn't.
  Call it without an extra `timeout` wrapper, or give the wrapper a generous
  margin (`M >= N + 15s`).
- Before concluding "the board is stuck" from a missing serial response,
  cross-check via pyOCD instead of trusting a single CLI timeout:
  ```
  pyocd commander -t atsame54p20a -u <probe-id> -M pre-reset --elf <production.elf> -c "reg" -c "exit"
  xc32-addr2line.exe -e <production.elf> -f -C <pc-hex> <lr-hex>
  ```
  `-M pre-reset` resets and halts immediately; comparing PC across repeated
  calls distinguishes "genuinely stuck" (PC never moves) from "running fine,
  the serial line just didn't arrive" (PC changes between calls).
- `cli.py`'s stdout can raise `UnicodeEncodeError` on non-ASCII bytes from the
  board (e.g. the boot log right after `reset`) under the Windows console's
  cp1252 encoding — set `PYTHONIOENCODING=utf-8` first.

---

## 3. Known MCC regenerate pitfalls

- **Generate Code can run incompletely, without an error message.** New
  driver folders and component YAMLs get written, but
  `configuration.h`/`system_config.h`/`initialization.c` sometimes stay
  unchanged. Check `git status`/file mtimes of the core files after every
  Generate — a clean compile is not proof that regeneration actually touched
  everything it should have.
- **Missing `#define DRV_GMAC` turns `gmac_drv_dcpt[]` into a zero-element
  array**, which fails with `-Werror=array-bounds` at compile time. Fix: make
  sure MCC actually generated the GMAC component (see above); don't add the
  define by hand.
- **`TCPIP_STACK_NETWORK_INTERAFCE_COUNT` can stay at `1` after adding a
  second interface**, even though MCC's own Configuration Summary already
  shows "Network Interface: 2" — that summary only mirrors the model, not the
  generated code. Only an actual Generate run (main toolbar, not the TCP/IP
  Configurator's own popup) writes the real value into `configuration.h`.
- **Wiring components in the data-link graph does not set pin mapping.**
  GMAC/MDIO pins had to be assigned separately in MCC's Pins editor; without
  that, the PHY's MDIO line never initializes even though the graph looks
  correct.
- **An undersized TCP/IP heap can fail GMAC init with no obvious symptom
  pointing at the heap.** `TCPIP_STACK_DRAM_SIZE` (MCC: TCPIP CORE → Heap
  Configuration) and the linker's Heap Size (XC32 Global Options → Linker →
  General) both need enough headroom for `DRV_GMAC_Initialize()`'s
  descriptor/buffer allocation; too small and initialization fails even
  though pins, clock, and PHY address are all correct. Both values must be
  raised together and kept in the same rough ratio.
- **Recurring MCC generator bug: `va_start`/`va_end` used without
  `#include <stdarg.h>`** in generated files, causing an "implicit
  declaration" compile error. Observed in the LAN865x driver and in the
  generated Telnet source. No MCC GUI field controls this — the include has
  to be re-added by hand after every Generate run that touches an affected
  file.
- **`TCPIP_STACK_InitCallback` is declared and wired in by generated code,
  but never defined by MCC**, producing a linker error. Solution lives in
  `app.c` (user code): return a persistent pointer to a `static
  TCPIP_STACK_INIT` struct with the same values `initialization.c` already
  builds, and return immediately — no asynchronous wait needed.
- **Bridging is enabled per network-interface component**, not via a
  dedicated bridge component: each network-config component carries a
  boolean "Add to MAC Bridge" field. Enable it on both interfaces, then
  Generate — MCC produces the `TCPIP_STACK_USE_MAC_BRIDGE` block and the
  bridge table/init data on its own.
- **A recorded toolchain version in `configurations.xml` can silently drift
  from the compiler actually used at link time.** When in doubt, read the
  `xc32-gcc.exe` path straight out of the build log rather than trusting that
  field.
- **Silicon errata DS80000748K (FDPLL ratio never clears)** can hang the
  board at boot inside MCC-generated clock init, before any application code
  runs, and the failure can appear or disappear depending on unrelated linker
  address shifts elsewhere in the image. Confirmed via direct register
  access: `DPLLRATIO` and `DPLLSTATUS` both look correct, only
  `DPLLSYNCBUSY` incorrectly stays set. Fix (documented exception, no MCC GUI
  field exists for it): bound the wait loops in `FDPLL0_Initialize()` with a
  count limit instead of polling forever, and reapply after every Generate
  that touches the clock peripheral file. A naive halt-and-inspect debug
  session can show the PC inside C-runtime startup on both a hanging and a
  working build alike — that's a sampling artifact of that attach mode, not a
  real finding; only direct register values, or comparing the PC across
  repeated resets, are reliable here.
- **A module that allocates from the TCP/IP heap during early
  initialization can crash with a bus fault**, because application
  initialization runs synchronously before the stack's own asynchronous heap
  setup has necessarily finished. Fix: move any such allocation into the
  application's later "service tasks" phase, alongside other code that
  already waits for a running stack.
- **The same bug class silently broke Telnet authentication**: registering
  the auth handler too early gets it overwritten back to null once the
  generated Telnet module's own initialization runs later. Every login then
  hits a null handler and is rejected without ever reaching the real check.
  Fix: move the registration to the same later phase as above.
- **A connected terminal program can send Enter as CR+NUL instead of
  CR+LF** (both are valid under RFC 854). The generated command-line editor
  only recognizes CR/LF as a line terminator, so the trailing NUL becomes the
  leading byte of the *next* command buffer — and a string starting with a
  NUL byte reads as empty to standard string functions, so only the first
  command in a session ever appeared to work. Fix: explicitly discard a
  trailing NUL byte in the character editor.
- **Command output sent over a secondary I/O channel (e.g. Telnet) can
  silently go to the wrong place** if application code always prints through
  a single hard-wired console output function instead of the I/O handle the
  active command session actually provides. Fix: route all command replies
  through that handle, and leave truly asynchronous background logging (with
  no command context) on the original console output.
- **A too-small output buffer for a secondary I/O channel silently
  truncates large command output** rather than erroring, if the underlying
  write's return value is discarded instead of checked. Sizing it requires an
  explicit tradeoff against how much heap headroom the channel eats per
  connection.
- **Removing a busy-wait guard while refactoring for a second output
  channel can turn truncation into corruption** instead of just truncating —
  without backpressure, output can be produced faster than the transport
  drains it, interleaving bytes from two different points in the same
  stream. A guard tied to one channel's own readiness check can often be
  reused as-is for the other, since it naturally reports "ready" when that
  channel isn't the bottleneck.
- **A readiness check before a write is not the same guarantee as checking
  the write's actual return value.** In a bare-metal single-superloop
  design, nothing drains a secondary channel's send buffer until the code
  that services it actually runs again — so a bounded retry loop around the
  real return value, actively pumping the relevant service calls in between,
  is what closes a race that a plain pre-check leaves open intermittently.
- **A hand-rolled mutex on a bare-metal build may not actually be a mutex.**
  If the underlying OS abstraction layer implementation is just a flag
  rather than a real interrupt-disabling primitive, a driver's own
  lock/unlock functions built on top of it do not protect against a
  genuinely concurrent hardware interrupt callback — leading to
  non-deterministic data corruption that looks like a timing-dependent
  length mismatch. Fix: wrap the lock with an actual interrupt-disable
  primitive as well.
- **A MAC-layer frame-length convention is easy to get subtly wrong at the
  boundary between generic stack code and application code.** If a receive
  packet's stored segment length means "payload after the link-layer
  header" at the point application code reads it, using it directly as "full
  frame length" undercounts by exactly the header size — invisible for
  small frames, very visible for anything spanning more than one lower-level
  transfer chunk.

---

Every entry above traces back to a fully verified fix in the working tree;
see `session-log.md` for the original derivation, register values, and test
evidence behind each one.
