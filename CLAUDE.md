# tcpip_iperf_lan865x (net_10base_t1s_bridge) — Working Instructions

This app in the fork `github.com/zabooh/net_10base_t1s_bridge.git` (`origin`, branch `master`,
`upstream` = the public Microchip content repo `Microchip-MPLAB-Harmony/net_10base_t1s`)
is being extended into a 10BASE-T1S ↔ 100BASE-T layer-2 bridge on the ATSAME54P20A — analogous to
the sister project `C:\work\t1s_bridge\bridge\t1s_100baset_bridge` (own repo, own `CLAUDE.md`
there), which already works and serves as a reference for bridge configuration, PHY assignment,
and pin mapping.

**All tools and docs for this bridge work live here in `apps\tcpip_iperf_lan865x\`**, not in the
repo root — the rest of `net_10base_t1s` is an unmodified Microchip content repo with many other
apps that are irrelevant here. New Markdown files (docs, test logs, etc., not this `CLAUDE.md`)
belong under **`docs\`** in this folder.

---

## 0. Language rules and documentation

- **All Markdown files under `docs\` must be written in English** — regardless of which language
  the session console is running in. This `CLAUDE.md` file itself is written in English as well
  (translated from German on 2026-09-01 so it benefits contributors who don't read German — see
  section 5).
- **All code (C or Python) must be entirely in English** — identifiers, comments,
  log/console output (`SYS_CONSOLE_PRINT` etc.), docstrings, error messages. Applies equally to
  new code and to changes to existing code.
- The session console (chat with the user) runs independently of this in whatever language the
  user is currently using (currently German) — this rule concerns only files, not the
  conversation.
- **Session log:** Continuously, throughout the whole session, record which actions were taken
  and what results/insights they produced — file `docs\session-log.md`, chronological, in
  English. Append after each completed work step (not only at the end of the session), so nothing
  is lost even if the session is interrupted. Goal: being able to reconstruct afterwards which
  actions achieved what. **For executed commands (CLI tests, builds, script invocations, etc.)
  always record the exact invocation with all parameters verbatim** (not just a summarized
  description) — including failed attempts/dead ends, so it stays traceable afterwards what
  exactly worked and what didn't.
- **Configuration manual:** Build up a document in parallel to the session that explains how the
  bridge is configured (MCC components, pin mapping, bridge activation, etc.) — file
  `docs\bridge-configuration-manual.md`, English, intended for readers who want to rebuild/
  configure the bridge themselves (unlike the session log, which records the history of this
  work).
- **When the user refers to "screenshots"** (e.g. "look at the screenshot", "copy the
  screenshots over") without naming a specific filename/path: **always use the newest
  screenshot(s)** in `C:\Users\M91221\OneDrive - Microchip Technology Inc\Pictures\Screenshots`
  (sorted by `LastWriteTime`). These get copied to `docs\images\` with a **meaningful,
  descriptive English filename** (not the original `Screenshot YYYY-MM-DD HHMMSS.png` name) —
  name it based on the actual image content.

---

## 1. Hard rule: MCC-generated code is NEVER touched by hand

Everything under `firmware\src\config\default\` (drivers, `configuration.h`, `system_config.h`,
`definitions.h`, `initialization.c`, `peripheral\*\plib_*.c/.h` etc.) as well as
`firmware\tcpip_iperf_lan865x.X\tcpip_iperf_lan865x_default\` (component YAMLs,
`mcc-config.mc4`) is changed **exclusively via MCC + Generate Code** — **never** by manual
edits, not even as a quick fix. If something is missing or wrong in the generated code, the fix
belongs in the MCC GUI (pins, component properties), not in the file.

**Sole exception:** `firmware\src\app.c` / `app.h` (and other genuine user files outside of
`config\default\`) — e.g. the `TCPIP_STACK_InitCallback` stub lives there (see section 3).

**Before diagnosing any build/runtime error in generated code:** diff against the sister project
(`t1s_100baset_bridge`, same hardware family, verified working) before speculating. Has paid off
multiple times — see section 4.

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

- **The user builds themselves in MPLAB X** (not `build.bat`) — don't proactively call
  `make`/`build.bat` to "prove" a fix. Only build/flash/test when asked.
- **`build.bat`/`flash.bat`/`cli.bat` live directly here**, `scripts\cli.py` and
  `scripts\flash_same54.py` underneath — ported from the sister project
  `t1s_100baset_bridge\build.bat`/`flash.bat`/`scripts\cli.py`/`scripts\flash_same54.py`,
  paths adapted relative to `firmware\tcpip_iperf_lan865x.X`.
- **Own `.venv`** (since 2026-08-31, `setup.bat`/`batch\setup_venv.bat`,
  `scripts\requirements.txt`) — `flash.bat`/`cli.bat`/`run_gui.bat`/`run_gui_telnet.bat`/
  `run_term.bat` use `%~dp0.venv\Scripts\python(w).exe`, falling back to the global `python`.
  Previously these scripts pointed hard at the sister project's `.venv` — it worked, but was
  fragile if the sister project got moved/cleaned up. The entire `setup.bat`/`install.bat`
  mechanism (venv, pyOCD/probe selection via `bench.json`, SAME54_DFP debug fix, `genmk.bat` for
  headless makefiles) was ported 1:1 from the sister project, including
  `setup_compiler.py`/`setup_compiler.config` (added 2026-08-31, since this machine has two
  XC32 versions installed — `v4.60`, `v5.10`). **Important:** here too this step is only a
  note, not a build control — `build.bat` does not read `setup_compiler.config`, exactly like
  in the sister project (there, the value only feeds `build_summary.py`'s `xc32-nm`, which
  doesn't exist here). Which XC32 actually builds is recorded in
  `nbproject\Makefile-local-default.mk` (written by MPLAB X itself) — controllable only via the
  IDE, not via this script.
- **`genmk.bat` (headless `nbproject\Makefile-*.mk` generation) works here**, contrary to the
  older note in the cross-project MCC knowledge (which said: tried multiple times, never
  worked, only "open once in the GUI and build" worked — that referred to the sister project,
  not this one; both experiences remain documented, just measured on different projects).
  Tested against this project on 2026-08-31, with a real stumbling block along the way: the
  version ported from the sister project had the MPLAB X version hard-coded as a list
  (`v6.25 v6.20 ...`) — this machine has both `v6.25` **and** `v6.35` installed, the IDE uses
  `v6.35`, but the list only knew up to `v6.25` and thus silently picked the wrong (older)
  version. Result: `rc=0`, no error message, but `Makefile-local-default.mk` pointed to a
  different DFP pack path and a different Java version than the IDE build — exactly the kind of
  "silently wrong" the script's own header comment actually warns about for `xc32-bin2hex`, just
  one level up. **Fix:** the same dynamic directory detection as in `build.bat`
  (`dir /b /ad /o-n "...\MPLABX\v*"`, newest first) instead of the hard-coded list. After that
  the generator picked `v6.35`, and `Makefile-local-default.mk` came out **byte-identical** to
  the IDE-generated original; `Makefile-default.mk` differed only in the flag hash suffixes
  (harmless noise, the same kind as with MCC Generate Code diffs, see above) — no more path/
  compiler deviation. Verified by direct diff against a copy of the IDE-generated files saved
  before the test, not by building (no unsolicited `build.bat` run, see the rule above).
- **Board COM port: `COM8`** (EDBG serial number `...001049`) — confirmed on 2026-08-30.
  Other probes connected at this bench: `COM10` (`...001290`), `COM23` (`...001103`), belong to
  other boards.
- **HEX output:** `firmware\tcpip_iperf_lan865x.X\dist\default\production\tcpip_iperf_lan865x.X.production.hex`
  (`TYPE_IMAGE=PRODUCTION`, not the `debug` directory).
- **`build.bat` additionally copies the hex to `release\bridge_lan865x_100baseT.hex` after
  every successful build** (since 2026-08-31, same as in the sister project, checked in there —
  so a fresh clone can be flashed without building first). **Only `build.bat` updates this
  copy** — a build directly from the MPLAB X IDE leaves `release\` outdated.
  **`flash.bat` flashes exactly this `release\` file by default** (since 2026-08-31, previously
  `dist\`) — to flash a fresh local build instead, pass the `dist\` path explicitly:
  `flash.bat firmware\tcpip_iperf_lan865x.X\dist\default\production\tcpip_iperf_lan865x.X.production.hex`.
- **`scripts\build_summary.py` (since 2026-09-01, ported 1:1 from the sister project)** runs
  automatically at the end of every `build.bat` run: flash/RAM usage from `memoryfile.xml`,
  heap/stack size from the `.map` (heap found, `_min_stack_size` **not** present in the `.map`
  in this project — the script cleanly shows "-- not found in map --" for that, not a bug),
  active interrupt handlers via `xc32-nm` (stays empty as long as `setup_compiler.config`
  doesn't exist — run `python scripts\setup_compiler.py` once, exactly the same dependency as
  in the sister project). Also archives HEX + summary text with a timestamp under
  `firmware\tcpip_iperf_lan865x.X\dist\default\production\image\` (gitignored, like the whole
  `dist\` tree).
- **Call `.bat` files with an absolute path from Git Bash** (otherwise "not recognized"):
  `MSYS_NO_PATHCONV=1 cmd /c "C:\work\t1s_bridge\bridge\harmony\net_10base_t1s\apps\tcpip_iperf_lan865x\flash.bat --list" < /dev/null`.
- **CLI responses are asynchronous** — wait for the response line after a command
  (`cli.bat --read N "..."`), don't send the next one immediately.
- **`cli.py --read N` deliberately waits *at least* N seconds before exiting** (`drain()` has a
  fixed lower time bound). An outer Bash `timeout M` wrapper with `M < N` therefore guaranteed
  kills the process prematurely — regardless of whether the board had actually responded.
  **Actually happened on 2026-08-30:** `timeout 15 ... cli.py --read 20 "reset"` returned exit
  code 124 and was wrongly interpreted as "board is hanging", even though the board had booted
  cleanly — the same call without the `timeout` wrapper (or with `M > N`) immediately showed the
  correct boot messages. Rule: **always call `cli.py` without an additional `timeout` wrapper**
  (it terminates deterministically on its own after `--read` seconds); if an outer safety net is
  still needed, set `M` to at least `N + 15s`. Before diagnosing a "board is hanging" case,
  additionally cross-check via pyOCD instead of relying on a single CLI timeout — recipe:
  ```
  pyocd commander -t atsame54p20a -u <probe-id> -M pre-reset --elf <production.elf> -c "reg" -c "exit"
  xc32-addr2line.exe -e <production.elf> -f -C <pc-hex> <lr-hex>
  ```
  `-M pre-reset` resets and halts immediately; `reg` shows PC/LR, `addr2line` resolves them to
  function+line. Considerably more reliable than the serial console for distinguishing "board is
  genuinely stuck" (PC stays identical across repeated calls/after waiting) from "running
  normally, the serial output just didn't arrive" (PC is somewhere in the main program, changes
  between two calls).
- `cli.py`'s stdout encoding can crash on non-ASCII bytes from the board (e.g. boot log right
  after `reset`) on Windows (`UnicodeEncodeError`, cp1252 console) — work around it with
  `PYTHONIOENCODING=utf-8` beforehand.

---

## 3. Known MCC regenerate pitfalls (this project, 2026-08-29/30)

- **Generate Code can run incompletely, without an error message.** Observed multiple times: new
  driver folders (`driver\gmac\`, `driver\ethphy\`) and component YAMLs get written, but
  `configuration.h`/`system_config.h`/`initialization.c` remain unchanged (check mtimes!).
  **After every Generate: check `git status`/mtimes of the core files**, not just build success
  — a clean compile does not mean everything was actually regenerated.
- **Missing `#define DRV_GMAC` → `gmac_drv_dcpt[]` becomes a zero-element array** →
  `-Werror=array-bounds` at compile time (`drv_gmac.c`, `gmac_drv_dcpt[macIndex]`). Fix: make
  sure in MCC that the GMAC component was actually generated (see above), don't insert the line
  by hand.
- **`TCPIP_STACK_NETWORK_INTERAFCE_COUNT` stayed at `1` after adding GMAC**, even though MCC's
  own "Configuration Summary" (Overview → Config Summary) already correctly showed "Network
  Interface: 2" — the summary view only mirrors the model, not the generated code. Only an
  actual Generate run (main window toolbar, not the TCP/IP Configurator popup) writes it into
  `configuration.h`.
- **Wiring GMAC/PHY components in the data-link graph does NOT automatically set the pin
  mapping.** The ten RMII+MDIO pins had to be assigned separately in the MCC **Pins** editor
  (its own window, not the TCP/IP Configurator) for the GMAC function:
  `PA12, PA13, PA14, PA15, PA17, PA18, PA19, PC20, PC22, PC23`. Without that, the MDIO line
  never initializes the physical PHY. **Caught up on 2026-08-30** — `peripheral\port\plib_port.c`
  was then checked 1:1 against the (verified working) version in the sister project: all ten
  pins now identical. **Still the same boot error nonetheless:**
  `TCP/IP Stack: GMAC MAC initialization failed` / `Initialization failed 9 - Aborting!` — so
  the pin mapping was necessary but apparently not sufficient. Clock configuration
  (`peripheral\clock\plib_clock.c`) and all GMAC/MIIM/PHY macros in `configuration.h` were also
  checked against the sister project and match.
  **SOLVED (2026-08-30):** the actual cause was **none** of the above, but an undersized heap —
  `TCPIP_STACK_DRAM_SIZE` (MCC: `TCPIP CORE` → "Heap Configuration" → "TCP/IP Stack Dynamic RAM
  Size") was set here to `39250` instead of the `65536`/generated `131072` in the sister
  project, and the linker `heap-size` (System → Project Configuration → XC32 Global Options →
  Linker → General → Heap Size) to `44960` instead of `163840` — both undersized by the same
  factor (~3.6×). Since `TCPIP_STACK_HEAP_TYPE_INTERNAL` with `malloc_fnc = malloc` obtains the
  entire TCP/IP heap from a single `malloc(TCPIP_STACK_DRAM_SIZE)` out of the linker heap, there
  was practically no room left for `DRV_GMAC_Initialize()`'s descriptor/buffer allocation
  (`F_DRV_GMAC_RxCreate`/`TxCreate`, `drv_gmac.c`) — hence the failure despite correct pins/
  clock/PHY address. After raising both values (linker `heap-size` → `163840`,
  `TCPIP_STACK_DRAM_SIZE` → `65535`, followed by Generate Code) both interfaces come up cleanly,
  ping to another T1S node (`192.168.0.202`) successful. Full history including diff evidence:
  `docs\session-log.md`. `drv_miim.c`/`drv_ethphy.c` (package version difference `net v3.14.5`
  vs. the sister project's `v3.11.1`) were checked line by line via agent as a precaution — no
  behavioral differences found, only MISRA style. The orphaned `.ctu-info` remnants of
  `drv_extphy_lan8742a.*` are clean now (current state consistently generates only LAN8742A
  files, no more LAN8740 remnants, see session log).
- **`TCPIP_STACK_DRAM_SIZE` raised to `98304` (96K) — 2026-08-31, hand edit,
  `configuration.h` (previously `65535`).** Motivation: documented side finding from earlier
  (see the telnet buffer entry above) — the free TCP/IP heap drops from ~17 KB to ~3.8 KB after
  a single telnet connection and stays fragmented. Linker `heap-size` deliberately **not**
  raised along with it (stays `163840`) — the math was checked beforehand: remaining headroom
  for everything else (C runtime heap, GMAC/LAN865x buffers, wolfSSL) drops from
  `163840-65535=98305` to `163840-98304=65536` (64K), still far above the documented failure
  threshold of ~`5710` from the GMAC init failure above. Build successful (`BUILD SUCCESSFUL`,
  `release\bridge_lan865x_100baseT.hex` updated) — **not yet tested on hardware.** **Must also
  be set in the MCC GUI** (`TCPIP CORE` → "Heap Configuration" → "TCP/IP Stack Dynamic RAM
  Size" → `98304`), otherwise the value silently falls back to whatever was last stored in the
  model on the next Generate Code — exactly the same pattern as the telnet buffer above.
- **Recurring MCC generator bug: `#include <stdarg.h>` missing in generated files that use
  `va_start`/`va_end`** → compile error `implicit declaration of function 'va_start'`. Observed
  so far in two different generated files:
  - `drv_lan865x_api.c` (`PrintRateLimited()`) — got removed during a regenerate.
  - `library\tcpip\src\telnet.c` (`F_Telnet_PRINT()`) — was missing right after adding the
    telnet server component via MCC, **2026-08-30**, same error signature.
  No MCC GUI field exists for this — fix so far is by directly adding the include to the
  affected generated file (exception to the hard rule above, since it's a pure generator bug
  with no configuration equivalent). **After every Generate run that touches an affected file,
  check whether the include is still there** — MCC removes it again on the next regenerate.
- **`TCPIP_STACK_InitCallback` is declared `extern` by `initialization.c` and wired into
  `TCPIP_STACK_Init()`, but MCC generates no definition** → linker error
  `undefined reference to 'TCPIP_STACK_InitCallback'`. Solution implemented **in `app.c`**
  (user code, see rule 1): returns a persistent pointer to a `static TCPIP_STACK_INIT` struct
  holding the same values that `initialization.c` builds locally anyway, and returns `0`
  immediately (no asynchronous wait needed).
- **Activating the bridge is done via a checkbox per network interface component**, not via a
  dedicated MCC component and not by manually adding `TCPIP_STACK_USE_MAC_BRIDGE` & co.: in the
  MCC component model, each `tcpipNetConfig_N` component (NETCONFIG-0/NETCONFIG-1 in the
  data-link graph) carries a boolean field `TCPIP_NETWORK_MACBRIDGE_ADD_IDXn` — set to `true`
  for both interfaces in the sister project (`Add to MAC Bridge` in the properties). Enable it
  on NETCONFIG-0 and NETCONFIG-1 there, then Generate — MCC automatically produces the
  `TCPIP_STACK_USE_MAC_BRIDGE` block in `configuration.h` as well as `tcpipMacbridgeTable`/
  `tcpipBridgeInitData` and the `{TCPIP_MODULE_MAC_BRIDGE, ...}` entry in `initialization.c`.
  **Activated since 2026-08-30 and confirmed as a fully working end-to-end bridge** (ping
  matrix, `bridge status/stats/fdb`, see `docs\session-log.md`).
- **`nbproject\configurations.xml`'s `languageToolchainVersion`** can differ from the compiler
  actually used at link time (in our case `4.60` was recorded, but the real link used `v5.10`,
  visible from the `xc32-gcc.exe` path in the build log) — when in doubt, check the path in the
  build log, not just this field.
- **Silicon errata: `OSCCTRL_DPLLSYNCBUSY.DPLLRATIO` never clears**, even though the DPLL locks
  correctly — Microchip Silicon Errata **DS80000748K** ("SAM D5x/E5x Family Silicon Errata and
  Data Sheet Clarification"), item **2.13.2 "FDPLL Ratio in DPLLnRATIO"**, affects both silicon
  revisions (A and D), so this board too. The MCC-generated, unmodified `FDPLL0_Initialize()`
  code in `peripheral\clock\plib_clock.c` waits there with an unbounded `while(...)` loop →
  **complete boot hang, before any app code even runs**, reproducible depending on seemingly
  unrelated linker address shifts elsewhere in the image (bisected for hours on 2026-08-30/31,
  see `docs\session-log.md`). Confirmed via direct register access: `DPLLRATIO` (`0x40001034`)
  takes the value correctly, `DPLLSTATUS` (`0x40001040`) shows `LOCK|CLKRDY` — only
  `DPLLSYNCBUSY` (`0x4000103C`) incorrectly stays stuck. **Fix (documented exception, no MCC GUI
  field for this):** in `FDPLL0_Initialize()`, all three wait loops (`DPLLSYNCBUSY.DPLLRATIO`,
  `.ENABLE`, `DPLLSTATUS.LOCK|CLKRDY`) were given a count limit
  (`CLOCK_DPLL0_SYNC_TIMEOUT = 2000`) instead of polling forever — reapply after every Generate
  run that touches `plib_clock.c`. **Caution when debugging this file:** a naive
  `pyocd commander -M attach -c halt` snapshot showed the PC in `__dinit_clear`/C runtime
  startup — both on the hanging build AND on a known-good build (sampling artifact of this
  attach mode, not a real finding). Only direct register values
  (`DPLLSTATUS`/`DPLLRATIO`/`DPLLSYNCBUSY`) or `-M pre-reset` with PC comparison across multiple
  calls are reliable.
- **App bug (not an MCC topic, but the same boot hang masked it):** `MIRROR_Initialize()`
  (`port_mirror.c`) immediately allocates 8 packet buffers from the TCP/IP heap
  (`TCPIP_PKT_PacketAlloc()`). If it's called — as initially ported — directly from
  `APP_Initialize()`, it crashes with a genuine bus fault (wild pointer, `BFAR` outside
  flash/RAM), because `APP_Initialize()` still runs synchronously inside `SYS_Initialize()`
  (`initialization.c`, right after `TCPIP_STACK_Init()`), but at that point the TCP/IP heap
  isn't necessarily fully set up yet (`TCPIP_STACK_Init()` only kicks off the actual,
  asynchronous stack initialization). **Fix:** `MIRROR_Initialize()` moved to `app.c`'s already
  existing `APP_STATE_SERVICE_TASKS` phase (the same place where packet handler registration
  and `env_apply()` already wait for a running stack). The other three ported modules
  (`lan865x_diag`/`noip_test`/`testserver`) only register CLI commands in their `_Initialize()`
  (no heap access) and are unaffected by this.
- **Telnet login always showed "Access denied" — fixed 2026-08-31, the same bug class as
  `MIRROR_Initialize()` above.** `TCPIP_TELNET_AuthenticationRegister()` was likewise called from
  `APP_Initialize()` — the registration reported success, but was silently overwritten shortly
  after by `TCPIP_TELNET_Initialize()` (`telnet.c`, MCC-generated, line ~317:
  `telnetAuthHandler = NULL;`), because that module init only runs later as part of
  `TCPIP_STACK_Init()`'s asynchronous initialization. Every real login attempt therefore hit
  `telnetAuthHandler == NULL` and was rejected without ever calling our handler. **Fix:**
  registration moved after `APP_STATE_SERVICE_TASKS`, right behind `MIRROR_Initialize()`.
  **Verified** via a raw Python socket test plus a parallel `tshark` capture on `tcp port 23`:
  `Logged in successfully` instead of `Access denied`.
- **Telnet commands were never recognized ("Please type in a command" on every line) — fixed
  2026-08-31.** TeraTerm sends each character individually and Enter as **`0d 00`
  (CR NUL)**, not CR LF (confirmed via a `tshark` capture on `tcp port 23`, RFC 854 allows
  both). `sys_command.c`'s (MCC-generated) character editor (`RunCmdTask()`) only recognizes
  `\r`/`\n` as line terminators — the following NUL byte fell into the generic "insert
  character" branch and ended up as the leading byte of the NEXT command buffer.
  `strncpy()`/string functions treat a string starting with `\0` as empty, even if real text
  follows afterwards — which is why each session's first command works, but every subsequent
  one fails. **Fix (documented exception, `sys_command.c`):** new `else if (newCh == '\0')`
  branch right after the `\r`/`\n` handling, which simply discards the byte.
  `patches/sys_command.patch` newly created (the tool now covers 5 files).
  **Verified:** two consecutive character-by-character `"help"` inputs over a raw socket as
  well as live in TeraTerm — both now work reliably.
- **All own commands sent their replies into the void over telnet — fixed 2026-08-31.** Direct
  consequence of the login fix: commands were now parsed, but their output always ended up on
  the serial console, never in the telnet client. Cause: all six own module files (`env.c`,
  `app.c`, `port_mirror.c`, `lan865x_diag.c`, `noip_test.c`, `testserver.c`) used
  `SYS_CONSOLE_PRINT()` (hard-wired to `SYS_CONSOLE_DEFAULT_INSTANCE`, i.e. always serial)
  instead of the `pCmdIO` that every `SYS_CMD_FNC` handler receives — 231 occurrences. **Fix:**
  new header `firmware/src/cmd_print.h` with `CMD_PRINT(pCmdIO, ...)`/`CMD_MSG(pCmdIO, str)`
  (wrappers around `pCmdIO->pCmdApi->print/msg`), all command replies in all six files
  converted; boot/background logs (no command context, e.g. `APP_Tasks()`'s packet log drain,
  `TESTSERVER_Tasks()`'s connect/disconnect messages) deliberately left on `SYS_CONSOLE_PRINT`
  unchanged. For `lan865x_diag.c`'s asynchronous register operations (result only arrives later
  from `LAN865X_DIAG_Tasks()`, after the command handler has returned) additionally
  `CMD_PRINT_OR_CONSOLE(pCmdIO, ...)` plus a remembered `s_diag_pCmdIO` (safe, because the
  module only ever allows one operation at a time anyway, `LAN865X_DIAG_Busy()`). **Verified**
  via a real telnet socket: `showenv`/`stats`/`meminfo`/`mirror`/`lanhelp` AND the two
  asynchronous cases `lan_read`/`plca_stat` (including a chained RMW + multi-step read
  sequence) now correctly deliver over telnet.
- **Telnet output buffer too small for larger command outputs (e.g. `dump`/`netinfo`) —
  2026-08-31, a real MCC configuration field, no hand patch.**
  `TCPIP_TELNET_SKT_TX_BUFF_SIZE` was set to `0` (= framework default), noticeably too small
  (`F_Telnet_MSG()` in `telnet.c` discards the return value of `NET_PRES_SocketWrite()` — what
  doesn't fit into the buffer is silently lost). Test series with `dump <addr> <size>` (size
  freely chosen) plus `netinfo` over a real telnet socket: 0 already truncates at 200 bytes;
  2048 covers 200, not 500/800; **3072 fully covers 200 and 500** (not 800); 4096 covers all
  three, but pushes the largest free TCP/IP heap block down to only ~720 bytes after a
  connect/dump/disconnect cycle — too tight given previous heap-exhaustion bugs in this
  project. **Set to 3072** as a middle ground (currently a hand edit in `configuration.h` for
  the test series — must also be set via MCC's telnet server component before the next Generate
  Code, otherwise it silently falls back to 0 on regenerate). Side finding, independent of
  buffer size: the free TCP/IP heap drops from ~17 KB (fresh boot) to ~3.8 KB after a single
  telnet connection and stays fragmented (largest block only ~1.6 KB) — not investigated
  further yet.
- **`dump` produced broken/corrupted output for larger byte counts (e.g. 500) — self-inflicted
  from the fix above, resolved 2026-08-31.** When splitting `DumpMem()` into a `pCmdIO`-capable
  `CmdDumpMem()` (for the `dump` command), the original's busy-wait guard
  (`SYS_CONSOLE_WriteFreeBufferCountGet()`, serial-specific — there's no equivalent for telnet
  via `pCmdIO`) got lost. `CmdDumpMem()` printed lines unthrottled at CPU speed, thereby
  outrunning both the serial 1024-byte ring buffer (`SERCOM1_USART_Write()` silently discards
  what doesn't fit) and telnet's `F_Telnet_MSG()` (likewise discards
  `NET_PRES_SocketWrite()`'s return value) — result: not just truncation, but **corrupted,
  interleaved bytes** in the middle of the output.
  **First fix (discarded):** a fixed 10ms pacing pause after every line — worked, but throttled
  EVERY dump unnecessarily, even far too-small ones. **Better fix (user's idea: "the
  backpressure had already worked in the sister project"):** deliberately reused the ORIGINAL
  `SYS_CONSOLE_WriteFreeBufferCountGet()` busy-wait from `DumpMem()`, with no device detection
  at all — the trick: this check only depends on the serial ring buffer, which is practically
  always free during a telnet-triggered dump (nothing else is writing serially at the same
  time), so it reports "enough room" almost immediately there and effectively doesn't throttle;
  for a serially triggered dump, the exact same precise, load-adaptive throttling as before
  kicks in. `app_wait_ms()` removed again. **Verified:** identical results to the fixed pause
  (serial `dump 800` complete and clean; telnet `dump 500` complete, `dump 800` cleanly cut off
  at the 3072-byte buffer limit, `netinfo` complete) — now without artificial delay on
  small/telnet dumps.
- **Real telnet backpressure in `F_Telnet_MSG()` — fixed 2026-08-31.** The fix above only works
  around the buffer limit, it doesn't cover it: output beyond `TCPIP_TELNET_SKT_TX_BUFF_SIZE`
  (3072 bytes) remained truncated. First attempt — a bounded busy-wait on
  `NET_PRES_SocketWriteIsReady()`, with an added `NET_PRES_SocketFlush()` too — achieved
  **nothing** (`dump 800` still only ~3080 of ~4011 bytes, but taking 6.6s instead of nearly
  instant). Cause: in this bare-metal single-superloop setup, `SYS_CMD_Tasks()` — which calls
  the command handler and through it `F_Telnet_MSG()` — runs in `SYS_Tasks()`
  (`config/default/tasks.c`) **before** `TCPIP_STACK_Task()`/`NET_PRES_Tasks()`. Nothing drains
  the telnet send buffer as long as those two haven't run — unlike UART, where a hardware
  interrupt keeps running independently of the main loop. User question: "would it be possible
  to call `SYS_Tasks()` while waiting?" — answer: not the whole function (would recurse into
  `SYS_CMD_Tasks()` itself — the currently active stack frame with its own static parser state
  — and into `APP_Tasks()`), but exactly the two relevant calls are never reentrant from within
  `F_Telnet_MSG()` (this function is only ever reached via the `SYS_CMD_API` `.msg`/`.print`
  callbacks, so only from `SYS_CMD_Tasks()`, a sibling of `TCPIP_STACK_Task()` inside
  `SYS_Tasks()`, never nested inside it). New `APP_PumpNetworkStack()` (`app.c`/`app.h`) wraps
  `TCPIP_STACK_Task(sysObj.tcpip)` + `NET_PRES_Tasks(sysObj.netPres)`; `F_Telnet_MSG()`'s
  busy-wait calls it instead of just spinning. **Verified:** `dump 800/2000/4000/8000` all
  complete (4011/9938/19813/39554 bytes) in a constant ~1.8–2.2s instead of cutting off at
  ~3072 bytes; `netinfo` also complete. New hand patch `patches/telnet.patch`, details:
  `docs/mcc-generated-code-patches.md` item 8.
- **Follow-up: `F_Telnet_MSG()` still intermittently corrupted large dumps — fixed
  2026-08-31.** User test with `dump 0x20000000 32000` showed: occasionally (not on every run —
  a timing race, not a fixed size threshold) the end of a line was missing (e.g. only 9 instead
  of 16 ASCII dots) and the **next** line's address was appended directly without `\n\r` —
  total byte count varied on every run (158020/158029/157993/158065/158212). Cross-checked at
  the user's request via a `tshark` capture (`follow,tcp,raw`): the wire shows the same content
  as the Python client, not a client artifact — this particular capture run happened to go
  through cleanly, which fits the race-condition theory (a deterministic bug would occur
  identically every time). Cause: `NET_PRES_SocketWriteIsReady()`'s pre-check doesn't reliably
  predict what a single `NET_PRES_SocketWrite()` call actually accepts — the return value was
  still discarded, the same "fire and forget" bug class as with the serial
  `SERCOM1_USART_Write()`, just intermittent instead of consistent. Fixed by looping on the
  real return value: write the remainder again, calling `APP_PumpNetworkStack()` in between,
  bounded by the existing 500ms timeout. User question whether `CmdDumpMem()`'s own serial
  busy-wait (`SYS_CONSOLE_WriteFreeBufferCountGet(...) < pos`) should now "also work for UART
  and telnet" — answer: it already does, for two different reasons (serial throttling for
  UART, harmless immediate pass-through for telnet, since real telnet correctness now lives
  entirely in `F_Telnet_MSG()`) — the related comment in `app.c` was outdated (falsely claimed
  correctness came from the buffer size `TCPIP_TELNET_SKT_TX_BUFF_SIZE`) and was corrected.
  **Verified:** `dump 0x20000000 32000` run 5× in a row, all 5 runs exactly 158065 bytes, no
  more glued-together lines — previously different on every run.
- **The LAN865x RX path had a genuine race condition — fixed 2026-08-31 (see
  `docs/session-log.md` for the full derivation).** Original finding: `rxPkt->pDSeg->segLen`
  deviated from the total length value declared in the IP header, and did so
  **non-deterministically** — the same periodic message showed `88` and `102` bytes at
  different times (true length: 98). Root cause found using the same methodology as in the
  sister project (`FALLSTRICKE.md`, GMAC RX race, 2026-08-27): `_Lock()`/`_Unlock()` in
  `drv_lan865x_api.c` merely wrap `OSAL_MUTEX_Lock/Unlock`, which on this bare-metal build
  (`osal_impl_basic.h`) **is just a simple flag, it doesn't disable interrupts** — the SPI
  transfer-complete callback `_EventHandlerSPI()` → `TC6_SpiBufferDone()` (runs from a genuine
  hardware interrupt) can therefore fire at any time right in the middle of
  `TC6_Service()`/`process_rx()`'s task-context processing — exactly the same bug class
  (task-local "lock" that doesn't actually block the genuinely concurrent ISR).
  **Fix (documented exception, `DRV_LAN865X_INSTANCES_NUMBER==1` in this project makes a
  single stored interrupt-state variable safe):** `_Lock()`/`_Unlock()` now additionally wrap
  `SYS_INT_Disable()`/`SYS_INT_Restore()` — a genuine critical section, as in the sister
  project.
  **Verified:** after the fix, the same test message showed a constant `102` across multiple
  samples (no more scatter); the full `sniffer_capture_test.py` completeness test showed the
  exact same `frame.len/ip.len/udp.length` combination for **all 3982** iperf UDP frames (zero
  variance) — the non-deterministic component is demonstrably fixed.
  **The residual issue left open back then (fixed, size-dependent offset: ~1512-byte frames 10
  bytes short, ~98-byte frames 4 bytes too many) has since been root-caused and fixed —
  2026-08-31, see `docs/session-log.md` for the full derivation.** Two independent, additive
  effects, not a single chunk-boundary quirk:
  1) `TC6_CB_OnRxEthernetPacket()` consistently reports `len`/`segLen` 4 bytes too large
     (presumably the 4-byte FCS still delivered by the T1S PHY and never stripped, contrary to
     `tcpip_mac.h`'s documented RX contract). At this point `len` and `segLen` still always
     agree — no race, no corruption.
  2) The generic, MCC-generated stack code (`library/tcpip/src/tcpip_manager.c`, line ~2544)
     subtracts `sizeof(TCPIP_MAC_ETHERNET_HEADER)` (14) from that before passing it on to
     registered packet handlers like `pktEth0Handler()`/`MIRROR_Eth0Rx()` — documented, correct
     standard behavior of the framework, not a bug in itself.
  **The actual bug (not MCC-generated, app code):** `port_mirror.c`'s `MIRROR_Eth0Rx()` used
  `rxPkt->pDSeg->segLen` at this point directly as the full copy length starting at
  `pMacLayer` — but there it actually means "payload after the 14-byte MAC header", not "full
  frame length". Every sniffed/mirrored RX frame was thereby copied 14 bytes too short
  (invisible for small frames as long as `MIRROR_SAFE_FRAME_LEN`'s clamp didn't kick in; very
  visible for anything beyond a single TC6 SPI chunk). **Fix (one line):**
  `rxPkt->pDSeg->segLen + sizeof(TCPIP_MAC_ETHERNET_HEADER)` passed as the frame length to
  `mirror_ethpkt_to_eth1()` — deliberately only at the RX site, not in `mirror_eth0_tx_hook()`
  (TX packets never go through the RX-side header subtraction, their `segLen` already means
  "full frame length" there).
  **Did in fact cause functional damage, contrary to what was originally noted here:** corrupt
  sniffer captures (every large frame showed "Previous segment not captured"/"ACKed unseen
  segment" in Wireshark) — normal bridge forwarding (`tcpip_mac_bridge.c`) never goes through
  `MIRROR_Eth0Rx()` and was never affected.
  **Verified** (twice: right after the fix and again after fully removing the diagnostic
  instrumentation + a clean rebuild): `sniffer_capture_test.py` shows `COMPLETE` for UDP/TCP in
  both directions, no more "shorter than IP/UDP header claims" warning; `tshark` confirms
  `frame.len=1514`/`tcp.len=1460` (previously `1504`/`1450`) and zero
  `tcp.analysis.lost_segment` hits.
  The temporary diagnostic instrumentation (`g_tc6DiagEnable` in `tc6.c`/`drv_lan865x_api.c`,
  plus the `MIRRORDIAG` added for it in `port_mirror.c`) has been fully removed again.
- **`suppressTx` ported from the sister project (2026-08-31):** `setenv sniffer 1` +
  `saveenv` previously only set the RAM flag "sniffer ON at boot", without actually muting the
  T1S transmitter — confirmed via `lan_read 0x000308F9` (`T1SPMACTL`), which still showed `0x0`
  right after boot instead of `0x4000` (TXD). Reason: the sister project has its own
  `suppressTx` field in `DRV_LAN865X_Configuration` for this, which MCC doesn't generate here.
  **Fix (documented exception, three hand patches):** `bool suppressTx;` added to
  `drv_lan865x.h` (same position as in the sister project, after `rxCutThrough`); a new
  `case 9` inserted into `drv_lan865x_api.c`'s `_InitUserSettings()` state machine, which
  writes `T1SPMACTL=0x4000` when `drvCfg.suppressTx` is set — **before** the final
  `NETWORK_CONTROL`/TXEN write (which was bumped from `case 9` to `case 10` for this); in
  `initialization.c`, `.suppressTx = false,` added to the default initializer and
  `drvLan865xInitData[0].suppressTx = env_sniffer();` added right next to the existing
  `nodeId`/`nodeCount` assignment. **Verified:** `lan_read 0x000308F9` now shows `0x00004000`
  right after boot, even before any `sniffer` command has run. Board reset back to
  `sniffer OFF` after the test (register-confirmed).

---

## 4. Sister project as reference

`C:\work\t1s_bridge\bridge\t1s_100baset_bridge` — own git repo, own `CLAUDE.md` there. Same
hardware family (SAM E54 + LAN865x via SPI + 100BASE-T PHY via GMAC/RMII), already verified
working, there using a **LAN8740A** on an `LAN8740A PHY Daughter Board (AC320004-3)`. For
uncertainties about bridge configuration, GMAC/PHY init data, or pin mapping: diff the
corresponding generated file (`initialization.c`, `configuration.h`,
`peripheral\port\plib_port.c`, component YAMLs under
`firmware\T1S_100BaseT_Bridge.X\T1S_100BaseT_Bridge_default\components\`) 1:1 against it before
speculating — repeatedly the fastest path to the real root cause.

---

## 5. Recording insights

`C:\work` is a throwaway workspace, auto-memory is tied to the path/repo — durable knowledge
therefore goes here in section 3 (dated, `YYYY-MM-DD — bug/insight → fix`, one to two sentences),
not only into memory. Read the target file first to avoid duplicates. Especially worth
recording: bugs together with the correct fix, and dead ends ("approach A doesn't work because
… → don't try it again"). Other Markdown docs (test logs, deep dives) belong under `docs\`, not
in this file.

This file was translated from German to English on 2026-09-01, at the user's request, so that
other contributors to the project can benefit from it too (see section 0).
