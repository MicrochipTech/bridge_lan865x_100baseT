# Session Log — tcpip_iperf_lan865x Bridge Bring-Up

Chronological record of actions taken and outcomes reached while bringing up the
10BASE-T1S ↔ 100BASE-T bridge on this app. Kept in English regardless of the console
language used during the session (see `CLAUDE.md`, section 0). Append after each
completed step — do not wait until the end of the session.

---

## 2026-08-30

### Screenshot capture of MCC TCP/IP Configurator state

- Located the day's screenshots in
  `C:\Users\M91221\OneDrive - Microchip Technology Inc\Pictures\Screenshots`.
- Copied them into `docs\images\` with descriptive names reflecting their content
  (MCC TCP/IP Configurator screens):
  - `tcpip-configurator-overview.png` — Overview page, all TCP/IP layers, LAN8742A
    selected in the Data Link layer.
  - `tcpip-configurator-datalink-lan8742a.png` — Data Link layer, GMAC + LAN865x-0
    wired to the LAN8742A PHY (later replaced by a rescroll of the same view — see
    below).
  - `tcpip-configurator-config-summary-interfaces.png` — Config Summary → Interface
    tab: Interface 0 = LAN865x, Interface 1 = GMAC.
  - `tcpip-configurator-datalink-lan8740.png` — Data Link layer, GMAC + LAN865x-0
    wired to the LAN8740 PHY (an earlier/alternate PHY selection).
- A later screenshot of the same Data Link layer view (PHY component list scrolled
  down, `DP83640` highlighted) was copied in as
  `tcpip-configurator-datalink-phy-list-scrolled.png`, then — on request — the
  original `tcpip-configurator-datalink-lan8742a.png` was deleted and this file was
  renamed to take its place, so `tcpip-configurator-datalink-lan8742a.png` now shows
  the scrolled PHY list state.

### GMAC boot failure — first diagnosis pass

- User switched the PHY selection in MCC to the physically correct part, rebuilt,
  and reflashed, but the boot-time error persisted.
- Confirmed via the serial CLI (`cli.bat --port COM8 --read 3 "reset"`, per the
  recipe in `CLAUDE.md` section 2) that the failure is unchanged:
  ```
  TCP/IP Stack: Initialization Started
  TCP/IP Stack: GMAC MAC initialization failed
  TCP/IP Stack: Initialization failed 9 - Aborting!
  ```
- This matches the failure already recorded in `CLAUDE.md` section 3 (entry dated
  2026-08-29/30) — pin assignment and clock/GMAC/MIIM macros had already been
  diff-checked against the working sister project
  (`t1s_100baset_bridge`) and found identical, yet the failure remained.

### Clock configuration comparison (peripheral clocking hypothesis)

- User hypothesized the GMAC peripheral clocking might still be wrong and asked for
  a fresh comparison against the sister project.
- Diffed `firmware\src\config\default\peripheral\clock\plib_clock.c` between this
  project and the sister project: **byte-identical except for a MISRA 2012 → MISRA
  2023 deviation-comment update** (this project was regenerated with a newer MCC
  MISRA ruleset). No functional difference — `MCLK_AHBMASK` (which gates the GMAC
  AHB bus clock) is `0xffffffU` in both, i.e. all AHB clocks including GMAC's are
  enabled; the `GCLK_PCHCTRL[7..9]` channels configured are for SERCOM peripherals
  (SPI/USART), not GMAC — GMAC on SAME54 does not need a dedicated `GCLK` peripheral
  channel, only the AHB bus clock.
- Conclusion: the top-level clock initialization is **not** the source of the
  divergence between the working sister project and this project's GMAC init
  failure — this reconfirms (rather than newly discovers) what section 3 of
  `CLAUDE.md` already noted.
- Also diffed `driver\gmac\src\dynamic\drv_gmac_lib_same5x.c` (this project) against
  `drv_gmac_lib_samE5x.c` (sister project, older Harmony `net` package version):
  large textual rewrite (static-function renames, added inline pointer-cast
  helpers, MISRA-driven casts) consistent with the already-documented package
  version gap (`net v3.14.5` vs. sister `v3.11.1`). The `GMAC_NCFGR` register setup
  (speed/duplex/MDC clock divider bits) itself is unchanged in content, only
  reformatted — so no smoking gun found here yet either.
- **Open follow-up:** the `drv_miim.c` rewrite between package versions (noted in
  `CLAUDE.md` section 3, >1800 diff lines) is still the next candidate to check for
  actual behavioral changes, not just MISRA/style churn.

### Runtime memory-dump CLI command ported from the sister project

- The sister project (`t1s_100baset_bridge/firmware/src/app.c`) has a `dump
  <addr_hex> <count>` CLI command that hex/ASCII-dumps arbitrary RAM, useful for
  reading live driver structs (e.g. GMAC descriptor arrays, MAC config) instead of
  reasoning from source alone.
- Ported the relevant parts into this project's `firmware\src\app.c` (a user file,
  not MCC-generated — allowed to edit per `CLAUDE.md` section 1's exception):
  - `DumpMem(addr, count)` — 16-bytes-per-line hex+ASCII dump; blocks on
    `SYS_CONSOLE_WriteFreeBufferCountGet()` before each `SYS_CONSOLE_PRINT()` line
    to avoid silently dropping output when the SERCOM TX ring buffer fills faster
    than 115200 baud can drain it (same rationale as the sister project's version).
  - `cmd_mem_dump()` — parses `dump <addr_hex> <count>` and calls `DumpMem()`.
  - Registered as a new `"Test"` command group via `SYS_CMD_ADDGRP()` in a local
    `Command_Init()`, called from `APP_Initialize()`.
- Only the memory-dump command was ported (not the sister project's `ipdump`,
  `stats`, `meminfo`, `logclear`, `logstat`, `uptime`, `history` commands), since
  the immediate goal is runtime inspection of GMAC/MAC state, not a full port of
  the sister project's diagnostic command set.
- **Not yet built/flashed** — per working agreement, the user builds in MPLAB X
  themselves; this session does not proactively invoke `build.bat`. Once built and
  flashed, `cli.bat --port COM8 --read N "dump 0x<addr> <count>"` can be used to
  inspect e.g. `gmac_dcpt_array`, `s_gmac_queue`, or `pMACDrv->sGmacData` at
  runtime.
- Added two further commands to the same `"Test"` group, complementing `dump`:
  `peek <addr_hex> [size=1|2|4]` (read a single value, default 4 bytes) and
  `poke <addr_hex> <value_hex> [size=1|2|4]` (write a single value, default 4
  bytes) — for quick single-register/variable inspection and for forcing state
  (e.g. a register bit) while debugging from the CLI, without needing a full
  `dump` block for a one-off check.

### Documentation process set up

- Added section 0 to `CLAUDE.md`: all Markdown files under `docs\` must be written
  in English (the console/chat language is independent and stays whatever the user
  is using); all C and Python code must be entirely in English (identifiers,
  comments, log/console output, docstrings, error messages).
- Established this file (`docs\session-log.md`) as the running record of actions
  and outcomes for this bring-up effort, and `docs\bridge-configuration-manual.md`
  (see below) as a separate, reader-facing how-to document — kept apart so the
  chronological log and the standalone manual don't get tangled together.

### Heap-size comparison — second, more promising divergence found

- User pushed back on the clocking hypothesis being fully closed; re-verified by
  diffing `plib_clock.c` again and confirming (as above) it is byte-identical
  (bar the MISRA comment) and that `PORT_Initialize()` → `CLOCK_Initialize()`
  ordering in `initialization.c` matches the sister project exactly (line
  621/623 here vs. 747/749 there) — clocking stays ruled out.
- Compared heap sizing instead, since `DRV_GMAC_Initialize()` allocates RX/TX
  descriptors and buffers via `macControl->memH` (the TCP/IP internal heap) in
  `F_DRV_GMAC_RxCreate`/`F_DRV_GMAC_TxCreate` (`drv_gmac.c` lines 513-521) — a
  starved heap fails exactly at this point and produces the observed message.
  Found a real, substantial divergence:
  - **TCP/IP stack heap** (`TCPIP_STACK_DRAM_SIZE`, `configuration.h`): **39250
    bytes** here vs. **131072 bytes** in the sister project — despite identical
    consumers (8 RX / 8 TX GMAC descriptors, 1536-byte RX buffers, 2×
    LAN865x RX descriptors, 10 TCP + 10 UDP sockets — all confirmed equal by
    diff).
  - **Linker (libc) heap** (`nbproject\configurations.xml`, `heap-size`
    property): **44960 bytes** here vs. **163840 bytes** in the sister
    project.
  - The two are linked: `tcpipHeapConfig.heapType = TCPIP_STACK_HEAP_TYPE_INTERNAL`
    with `malloc_fnc = malloc` (`initialization.c`/`configuration.h`) means the
    entire TCP/IP heap is obtained via a single `malloc(TCPIP_STACK_DRAM_SIZE)`
    call out of the linker-reserved libc heap. Here that leaves only ~5.7 KB of
    libc heap free for everything else after that one allocation, vs. ~32 KB
    of headroom in the sister project.
  - Both values here are smaller than the sister project's by almost exactly
    the same ratio (~3.6×), which reads as an across-the-board undersized
    heap configuration rather than a deliberate choice — plausibly left over
    from before the GMAC interface was added and never revisited.
- A background research agent (`Explore`, prompted to compare `drv_miim.c`,
  `drv_ethphy.c`, and the LAN8742A vs. LAN8740 PHY objects line-by-line for
  genuine behavioral differences, not MISRA-style noise) reported back in
  parallel: **no behavioral differences found** in any of those areas — only
  renames/casts, and one timing-order shift in `F_DRV_ETHPHY_SetupPhaseReset`
  (reset-timeout deadline computed one MDIO sub-phase earlier) too small
  (~tens of µs against a 500 ms budget) to plausibly be the actual cause. This
  reinforces the heap-size divergence as the leading candidate.
- **Fix applied so far:** user increased the linker heap-size in MCC (System →
  Project Configuration → XC32 Global Options → Linker → General → Heap Size)
  from 44960 to **163840** bytes, matching the sister project — see
  `docs\images\mcc-linker-heap-size-before-44960.png` and
  `docs\images\mcc-linker-heap-size-after-163840.png`.
- **Still open:** `TCPIP_STACK_DRAM_SIZE` itself (the TCP/IP Stack component's
  own "Basic Configuration" heap-size property in MCC, separate from the
  linker heap-size just changed) has **not** yet been raised from 39250 to
  match the sister project's 131072 — needed next, then Generate Code, then a
  build/flash/reset cycle to check whether the GMAC init failure clears.
- **MCC field located and set:** the TCP/IP heap size lives under component
  `TCPIP CORE` → category **"Heap Configuration"** → field **"TCP/IP Stack
  Dynamic RAM Size"** (model id `TCPIP_STACK_DRAM_SIZE`, under `TCP/IP STACK` →
  `BASIC CONFIGURATION` → `TCPIP CORE` in the Project Graph). It was previously
  unset in this project (no `User` override in `tcpipStack.yml`, running on the
  auto-computed default that produced `39250`). Noted along the way: the
  sister project's model has this field set to `65536`, yet its generated
  `configuration.h` shows `131072` — exactly double — suggesting the
  generator template doubles the entered value (possibly a safety margin);
  worth confirming after this project's own Generate Code run rather than
  assuming. User has now set it to **`65535`** here (see
  `docs\images\mcc-tcpip-core-dynamic-ram-size-65535.png`) — matching the
  sister project's `65536` (off by one, likely the field's max/rounding).
  Still needs **Generate Code** to actually take effect in `configuration.h`.

### RESOLVED — root cause confirmed: undersized heap, not clocking/pins/PHY

- After Generate Code, build, and flash with both heap sizes raised (linker
  `heap-size` 44960→163840, MCC `TCPIP_STACK_DRAM_SIZE` 39250→65535), the user
  confirmed: **both network interfaces (LAN865x 10BASE-T1S and GMAC
  100BASE-T) now come up successfully on boot** — the
  `TCP/IP Stack: GMAC MAC initialization failed` / `Initialization failed 9`
  failure is gone.
- Further confirmed with a live test: **`ping 192.168.0.202` (another T1S node
  on the bus) succeeded**, i.e. the 10BASE-T1S side is not just "initialized"
  but actually passing traffic.
- This confirms the diagnosis chain from this session: the clock
  configuration, GMAC/MDIO pin assignment, PHY address, and `drv_miim.c`/
  `drv_ethphy.c` MDIO logic were all correctly ruled out one by one — the
  actual cause was the TCP/IP stack's internal heap (and the libc heap backing
  it via a single `malloc(TCPIP_STACK_DRAM_SIZE)` call) being sized far
  smaller than the sister project's, so `DRV_GMAC_Initialize()`'s descriptor/
  buffer allocation (`F_DRV_GMAC_RxCreate`/`TxCreate`) was starved of memory.
- **Still to verify:** 100BASE-T side traffic (a ping across the GMAC
  interface, not just the T1S side), and bridging behavior between the two
  interfaces end-to-end.

### `netinfo` CLI command — link status confirms the fix on both interfaces

- Ran `netinfo` over the serial CLI (`cli.bat --port COM8 --read 3 "netinfo"`)
  to check both interfaces' state directly:
  ```
  Interface eth0/LAN865X0(0): 192.168.0.11/24, GW 192.168.0.1, MAC 00:04:25:1c:a0:02, Link UP, Status: Ready
  Interface eth1/GMAC(1):     0.0.0.0/24,      GW 0.0.0.0,       MAC 00:04:25:1c:a0:02, Link UP, Status: Ready
  ```
- **Both interfaces report `Link is UP` / `Status: Ready`**, including the
  GMAC/100BASE-T side — direct confirmation that the original bug (the PHY
  link never coming up) is genuinely fixed by the heap-size correction, not
  just that stack initialization no longer aborts.
- Two things flagged for follow-up, not yet resolved:
  - `eth1` (GMAC) has `IPv4 Address: 0.0.0.0` and `Gateway: 0.0.0.0` — no
    address assigned yet, so no traffic can pass over it even though the link
    is physically up. Unclear whether this interface is meant to get a
    DHCP/static address or intentionally run address-less as a pure bridge
    port — needs a decision before further testing on that side.
  - Both interfaces report the **same MAC address**
    (`00:04:25:1c:a0:02`). Expected once the MAC bridge is enabled, but per
    the "Bridge aktivieren" note above, `TCPIP_NETWORK_MACBRIDGE_ADD_IDXn` is
    **not yet** enabled on either `NETCONFIG-0`/`NETCONFIG-1` in this
    project — so a shared MAC at this stage is unexpected; the sister
    project's Config Summary shows distinct `Internal Mac: NO` (interface 0)
    vs. `Internal Mac: YES` (interface 1), suggesting these should currently
    differ. Worth checking the MAC address source configuration before
    enabling bridging.

### `eth1` IP config root cause found: typos in MCC `User` overrides

- Traced why `eth1` (GMAC) showed `0.0.0.0`: `configuration.h` has
  `TCPIP_NETWORK_DEFAULT_IP_ADDRESS_IDX1 = "192.168..12"` (missing an octet —
  double dot) and `TCPIP_NETWORK_DEFAULT_GATEWAY_IDX1 = "192.168.0..1"` (extra
  dot) — both malformed strings that fail to parse, hence the stack falls
  back to `0.0.0.0`. Confirmed in the MCC model
  (`firmware\tcpip_iperf_lan865x.X\tcpip_iperf_lan865x_default\components\tcpipNetConfig_1.yml`):
  both fields carry a `type: User` override with the typo'd value, sitting
  next to the (correct, unused) `type: Dynamic` auto-default
  (`192.168.100.11` / `192.168.100.1` respectively).
- Also confirmed `TCPIP_NETWORK_DEFAULT_MAC_ADDR_IDX1` has **no** `User`
  override in the model — it's just the `Dynamic` auto-default
  (`00:04:25:1C:A0:02`), which happens to be identical to `eth0`'s MAC. Since
  MAC bridging is not yet enabled on either `NETCONFIG` instance (see section
  3 of `CLAUDE.md`), two interfaces sharing one MAC at this stage is likely
  unintended, not a bridging side-effect.
- **Fix path (MCC, not hand-editing `configuration.h` — see rule 1):** select
  the `NETCONFIG` component's **Instance 1** (the one wired to
  `DRV_GMAC_Object` in the Data Link Layer graph) → **Basic Configuration** →
  correct the **"IP Address"** and **"Gateway"** fields (typo fixes), and set
  an explicit, distinct **"MAC Address"** for this instance → **Generate
  Code**.
- **Open decision handed to the user, not yet made:** should `eth1` end up on
  the same subnet as `eth0` (`192.168.0.0/24` — sensible groundwork for the
  planned bridge) or keep the original separate-subnet default
  (`192.168.100.0/24`)? Determines what the corrected "IP Address"/"Gateway"
  values should actually be, beyond just fixing the typos.
- **Fix applied and confirmed via `netinfo`:** user corrected the MCC fields
  (put both interfaces on `192.168.0.0/24`, gave `eth1` its own MAC). Current
  state:
  ```
  eth0/LAN865X0(0): 192.168.0.11, mask 255.255.255.0, gw 192.168.0.1,   MAC 00:04:25:1c:a0:02, Link UP, Ready
  eth1/GMAC(1):     192.168.0.12, mask 255.255.255.0, gw 0.0.0.0,       MAC 00:04:25:1c:a0:03, Link UP, Ready
  ```
  Both interfaces now have distinct, valid IPs and distinct MACs.
  `eth1`'s gateway is still `0.0.0.0` — not corrected, likely harmless for
  same-subnet LAN traffic but worth revisiting if `eth1` ever needs to route
  off-subnet.

### End-to-end ping test plan across the physical test setup

- Test topology as described by the user:
  - **Bridge under test** — serial console on **COM8**. `eth0` (LAN865x/T1S) =
    `192.168.0.11`, `eth1` (GMAC/100BASE-T) = `192.168.0.12`.
  - **A second, independent 10BASE-T1S node** on the same T1S bus as the
    bridge's `eth0` — serial console on **COM23**, IP `192.168.0.202`.
  - **PC** — its Ethernet adapter (labelled "Ethernet 8" in Windows) on
    `192.168.0.100`, connected to the bridge's `eth1` (100BASE-T) side.
  - All three IPs share the `192.168.0.0/24` subnet.
- Planned/observed ping matrix:

  | From → To | Target IP | Result |
  |---|---|---|
  | PC → Bridge `eth0` | 192.168.0.11 | planned |
  | PC → Bridge `eth1` | 192.168.0.12 | planned |
  | PC → other T1S node | 192.168.0.202 | planned |
  | Bridge → other T1S node (`ping 192.168.0.202`, default route via `eth0`) | 192.168.0.202 | **confirmed working** |
  | Bridge → PC (`ping 192.168.0.100 i eth1`, explicit interface) | 192.168.0.100 | planned |
  | Other T1S node → PC | 192.168.0.100 | planned |

- **Gap identified when asked to review the plan for completeness:** the
  mirror direction **other T1S node → Bridge** (pinging the bridge's `eth0`
  and/or `eth1` IP from the COM23 node) was missing from the plan — not
  guaranteed to behave identically to "Bridge → node" just because the
  reverse direction works.
- Also flagged as a useful (optional) diagnostic, not yet run: **other T1S
  node → Bridge `eth1` (192.168.0.12)**. Since the MAC bridge is not yet
  enabled on either `NETCONFIG` instance (see section 3 of `CLAUDE.md`), this
  is expected to **fail** — there is no reason for the stack to forward
  traffic between the two independent network interfaces yet. A failure here
  is a healthy negative-control result, not a bug; a pass would be
  unexpected and worth investigating. The real "does the bridge actually
  bridge" milestone is PC ↔ other-node pinging *each other* directly without
  addressing either of the bridge's own IPs — not yet possible until MAC
  bridging is enabled, tracked as a separate future step.

### Full ping matrix executed — results

- **PC network setup caveat discovered first:** this session runs on the same
  PC as the "Ethernet 8" adapter (192.168.0.100, wired directly to the
  bridge's `eth1`) — but the PC also has a **Wi-Fi adapter on
  192.168.0.78**, i.e. two interfaces sharing the `192.168.0.0/24` subnet.
  Windows silently routed the first round of test pings over Wi-Fi instead
  of the intended Ethernet link (confirmed via `Get-NetIPAddress`), which
  would have produced misleading results (e.g. a "successful" ping possibly
  hitting an unrelated Wi-Fi host rather than the bridge). Fixed by forcing
  the source address explicitly (`ping -S 192.168.0.100 <target>`) for every
  PC-side test below. User independently confirmed the same dual-IP setup.
- **cli.py quoting gotcha hit while running these tests:** invoking
  `cli.bat` through `cmd /c "...\"ping 1.2.3.4\"..."` from Git Bash silently
  split the quoted `"ping 192.168.0.202"` argument into two separate
  single-word commands (each producing `unknown command`) — the nested
  quote levels (bash → `cmd /c` → `cli.bat`'s `%*`) don't survive intact for
  multi-word arguments, even though the exact same pattern works fine for
  single-word commands (`reset`, `netinfo`, already used earlier this
  session without issue). **Fix: call `scripts\cli.py` directly with the
  sister project's venv Python, bypassing `cli.bat`/`cmd /c` entirely** —
  removes one quoting layer and preserves multi-word quoted arguments
  correctly:
  ```
  PYTHONIOENCODING=utf-8 "C:\work\t1s_bridge\bridge\t1s_100baset_bridge\.venv\Scripts\python.exe" "scripts\cli.py" --port COM8 --read 8 "ping 192.168.0.202 i eth0"
  ```
  Prefer this form over the `cli.bat`/`cmd /c` wrapper for any future
  multi-word CLI command from this environment.
- **Results:**

  | From → To | Source IP | Target | Result |
  |---|---|---|---|
  | PC → Bridge `eth0` | 192.168.0.100 (forced with `-S`, see caveat above) | 192.168.0.11 | ❌ timeout (100% loss) |
  | PC → Bridge `eth1` | 192.168.0.100 (forced with `-S`) | 192.168.0.12 | ✅ 4/4 replies |
  | PC → other T1S node | 192.168.0.100 (forced with `-S`) | 192.168.0.202 | ❌ destination host unreachable |
  | Bridge → other T1S node, **no interface specified** | ambiguous/undetermined (this is exactly the ambiguity described below) | 192.168.0.202 | ❌ 0/4 replies |
  | Bridge → other T1S node, **`i eth0`** | 192.168.0.11 | 192.168.0.202 | ✅ 4/4 replies, ~1 ms |
  | Bridge → PC, **`i eth1`** | 192.168.0.12 | 192.168.0.100 | ✅ 4/4 replies, ~1 ms |
  | Other T1S node → Bridge `eth0` | 192.168.0.202 | 192.168.0.11 | ✅ 4/4 replies, 1-2 ms |
  | Other T1S node → PC | 192.168.0.202 | 192.168.0.100 | ❌ 0/4 replies |
  | Other T1S node → Bridge `eth1` (diagnostic) | 192.168.0.202 | 192.168.0.12 | ❌ 0/4 replies (expected negative control) |

- **Interpretation:** every ping between two *directly, physically connected*
  peers succeeds (PC↔`eth1`, `eth0`↔other node). Every ping that would
  require traffic to cross from one bridge interface to the other, or reach
  a device only reachable through the bridge without addressing the bridge
  itself, fails — consistent with the MAC bridge still being disabled.
  Nothing here contradicts the plan; this is exactly the expected
  intermediate state before bridging is turned on.
### MAC bridge found to be already enabled — outdated `CLAUDE.md` note

- When asked "where in MCC" to enable MAC bridging (the planned next step),
  checked the current model/generated state first instead of assuming it was
  still off (per the "not yet activated" note in `CLAUDE.md` section 3, dated
  2026-08-30 but apparently stale): found `TCPIP_NETWORK_MACBRIDGE_ADD_IDX0`
  and `_IDX1` already set to `true` (`User` override) in
  `tcpipNetConfig_0.yml`/`tcpipNetConfig_1.yml`, **and already generated**
  into `configuration.h` (`#define TCPIP_STACK_USE_MAC_BRIDGE` + full bridge
  config: 2 ports, 17 FDB entries, 8-packet pool) and `initialization.c`
  (`tcpipMacbridgeTable[2] = {{0},{1}}`, `tcpipBridgeInitData`, and a
  `{TCPIP_MODULE_MAC_BRIDGE, &tcpipBridgeInitData}` entry in
  `TCPIP_STACK_MODULE_CONFIG_TBL`).
- MCC location for this field (for reference): component **`NETCONFIG`** →
  **Instance 0** / **Instance 1** (the Data Link Layer graph blocks used
  earlier to fix `eth1`'s IP) → checkbox **"Add to MAC Bridge"**.
- This means the MAC bridge was **already structurally active** during the
  full ping-matrix test round above — yet none of the cross-interface pings
  (PC↔`eth0`, PC↔node, node↔PC) worked. Either the flashed firmware at test
  time predated this config being generated, or bridging is enabled in
  config but not actually forwarding for some other reason — undetermined,
  needs the retest below to distinguish.
- User is confident the firmware already tested does stem from this
  bridge-enabled configuration, but as a precaution is redoing **Generate
  Code → Build All → flash** in MPLAB X before the next round of testing.
  **Still to do once that lands:** re-run the three still-failing rows from
  the ping matrix (PC↔`eth0`, PC↔node, node↔PC) — if the MAC bridge is
  genuinely working, these should now succeed without needing to address the
  bridge's own IPs.

### Retest after Generate Code → Build All → flash — bridge still not forwarding

- User redid Generate Code, Build All, and flash in MPLAB X (precaution, was
  already confident the previously-tested firmware matched this config), then
  asked to re-run the full ping matrix. `netinfo` on COM8 first confirmed the
  interface state is unchanged (`eth0` 192.168.0.11, `eth1` 192.168.0.12, both
  Link UP). Full matrix re-run with the same commands as the previous round
  (direct `scripts\cli.py` invocations, `--port COM8`/`COM23`, PowerShell
  `ping -S 192.168.0.100 ...` for the PC side):

  | From → To | Before this reflash | After this reflash |
  |---|---|---|
  | PC → Bridge `eth0` (192.168.0.11) | ❌ timeout | ❌ timeout (unchanged) |
  | PC → Bridge `eth1` (192.168.0.12) | ✅ 4/4 | ✅ 3/3 (unchanged) |
  | PC → other T1S node (192.168.0.202) | ❌ unreachable | ❌ unreachable (unchanged) |
  | Bridge → node, no interface | ❌ 0/4 | ✅ **4/4 — changed** |
  | Bridge → node, `i eth0` | ✅ 4/4 | ✅ 4/4 (unchanged) |
  | Bridge → PC, `i eth1` | ✅ 4/4 | ✅ 4/4 (unchanged) |
  | Node → Bridge `eth0` | ✅ 4/4 | ✅ 4/4 (unchanged) |
  | Node → PC | ❌ 0/4 | ❌ 0/4 (unchanged) |
  | Node → Bridge `eth1` (diagnostic) | ❌ 0/4 | ❌ 0/4 (unchanged) |

- **Only one row changed:** an unqualified `ping <ip>` issued from the
  bridge's own console is no longer ambiguous (works now without `i eth0`).
  This affects only the bridge acting as its own IP host on `eth0`'s route,
  not actual frame forwarding between the two physical segments.
- **The three rows that actually prove bridging (PC↔`eth0`, PC↔node,
  node↔PC) are all still failing, unchanged by the reflash.** Conclusion:
  **the MAC bridge module is configured and generated
  (`TCPIP_STACK_USE_MAC_BRIDGE`, `tcpipMacbridgeTable`,
  `TCPIP_MODULE_MAC_BRIDGE` module entry — all confirmed present in the
  freshly generated `configuration.h`/`initialization.c`), but is not
  actually forwarding frames between `eth0` and `eth1` at runtime.** This is
  now the open problem, distinct from (and following on from) the two
  already-resolved issues this session (heap size, `eth1` IP typos).
  Not yet investigated: whether the bridge module is being started/attached
  to both ports correctly at runtime (vs. just present in the static config
  tables), whether `TCPIP_MAC_BRIDGE_MAX_PORTS_NO`/port-index wiring in
  `tcpipMacbridgeTable[2] = {{0},{1}}` actually matches the two active
  `NETCONFIG` instances, and whether there's a required additional runtime
  call (e.g. explicit bridge-start) beyond static init that's missing.

### Bridge diagnostics via the built-in `bridge` CLI command — root cause found

- The firmware already has a `bridge` console command (guarded by
  `TCPIP_STACK_MAC_BRIDGE_COMMANDS`, already `true`) with subcommands
  `status`, `stats [clr]`, `fdb show`/`reset`/`add`/`delete`, and (if
  `TCPIP_MAC_BRIDGE_EVENT_NOTIFY` is on) `register` — implemented in
  `tcpip_commands.c` around `F_Command_Bridge` /
  `F_Command_BridgeShowStats` / `F_Command_BridgeShowFDB`. `bridge fdb show`
  needs no extra config; `bridge stats` needs "Enable Statistics"
  (`TCPIP_MAC_BRIDGE_STATISTICS`) on to return real counters.
- User enabled both **"Enable Statistics"** and **"Enable Event Notify"** in
  the MAC Bridge's Advanced Settings in MCC (screenshot:
  `docs\images\tcpip-configurator-netconfig-advanced-settings.png`),
  regenerated, rebuilt, and reflashed.
- Confirmed this is the **only** activation mechanism for MAC bridging in
  MCC — there is no separate standalone "MAC Bridge" component in either
  project's model; bridging is derived purely from the two per-`NETCONFIG`
  "Add to MAC Bridge" checkboxes (`TCPIP_NETWORK_MACBRIDGE_ADD_IDX0`/`_IDX1`,
  both already `true` here), and MCC auto-generates the whole bridge module
  (including this Advanced Settings panel, nested under one `NETCONFIG`
  instance for lack of its own component) once ≥2 interfaces have it
  checked.
- **Diagnostic sequence run over COM8/COM23**
  (`scripts\cli.py --port COM8/COM23 --read N "bridge ..."`, same direct-
  Python invocation pattern as the ping tests):
  1. `bridge status` → `status: 2` (`SYS_STATUS_READY`) — module is up.
  2. `bridge fdb show` → 17-entry table, only 3 populated: two `static,
     host` entries for the bridge's own two MACs (`...a0:02` on port 0's
     side, `...a0:03` on port 1's side), and **one dynamic entry learned on
     port 1** (`c0:47:0e:ad:47:2a`, almost certainly the PC's NIC).
     **Nothing learned on port 0** — no entry at all for the other T1S
     node's MAC (`00:04:25:8e:8c:a1`), despite node↔bridge pings having
     succeeded repeatedly over `eth0`.
  3. `bridge stats` → confirms it: **`port 0 stats: pkts received: 0`**
     (T1S/`eth0`), vs. **`port 1 stats: pkts received: 95`**
     (100BASE-T/`eth1`). Port 0 does show `fwd mcast: 91` (i.e. the bridge
     *is* successfully flooding broadcast/multicast frames received on
     port 1 *out* through port 0) — so egress on port 0 works — but nothing
     inbound on port 0 ever reaches the bridge's RX/learn pipeline.
  4. To rule out the counts just being stale/pre-bridge-enable: generated
     fresh T1S-side traffic (`ping 192.168.0.100` from the other node over
     COM23 — still failed, 0/4, as expected) and re-ran `bridge stats`.
     `port 0 pkts received` stayed at **exactly 0**; only port 1's counters
     grew (95→119), from unrelated ambient broadcast traffic on the
     100BASE-T segment, not from the node's ping attempt. This rules out a
     timing/staleness explanation — port 0 genuinely never feeds anything
     into the bridge.
- **Root cause identified by diffing against the sister project:**
  `DRV_LAN865X_PROMISCUOUS_IDX0` = **`false`** in this project's
  `configuration.h`, vs. **`true`** in the sister project's (confirmed via
  its `drvExtMacLan865x_0.yml`, `User` override). With promiscuous mode off,
  the LAN865x driver only hands frames addressed to its own MAC (or
  broadcast) up to the stack — anything else is discarded at the driver
  level before the bridge's port-0 glue ever sees it. A bridge port
  fundamentally needs to see *all* traffic on its segment (to learn source
  MACs and make forwarding decisions for frames not addressed to itself),
  so a non-promiscuous bridge port is a textbook explanation for exactly
  this symptom (port 0 blind to inbound traffic, port 1 fine).
- **Fix location in MCC (not yet applied):** component **`LAN865x`** →
  **Instance 0** (the MAC-layer block in the Data Link Layer graph) →
  checkbox **"Promiscuous Mode"** (model id
  `DRV_LAN865X_PROMISCUOUS_IDX0`) — currently absent as a `User` override in
  this project's `drvExtMacLan865x_0.yml` (running on the component's
  default, `false`). Enable it, Generate Code, Build All, flash, then
  re-run `bridge fdb show`/`bridge stats` (expect a port-0 entry for the
  other node's MAC, non-zero port-0 `pkts received`) and the still-failing
  ping-matrix rows (PC↔`eth0`, PC↔node, node↔PC).

### Promiscuous-mode fix applied — partial success, new asymmetric bug found

- User enabled `DRV_LAN865X_PROMISCUOUS_IDX0` in MCC (LAN865x → Instance 0 →
  "Promiscuous Mode"), regenerated, rebuilt, reflashed. `netinfo` confirmed
  the board came back up unchanged (`eth0` 192.168.0.11, `eth1` 192.168.0.12,
  both Link UP).
- **`bridge stats` baseline right after reflash:** `port 0 pkts received:
  82` (was stuck at 0 before the fix) — confirms the promiscuous-mode fix
  worked, port 0 now genuinely receives traffic into the bridge.
- **`bridge fdb show` baseline:** now shows entries learned **on port 0**
  for the first time — including `00:04:25:8e:8c:a1`, which is exactly the
  other T1S node's MAC (matches its `netinfo` output over COM23 earlier).
  **Also found a second, unexpected dynamic entry learned on port 0:
  `00:04:25:ca:ce:d9`** — not a device the user has mentioned; flagged for
  the user to identify (a third node on the T1S bus? Stray/duplicate
  address?), not yet explained.
- **Re-ran the real bridging test** (PC↔`eth0` and PC↔node via PowerShell
  `ping -S 192.168.0.100 ...`, node↔PC via `scripts\cli.py --port COM23
  "ping 192.168.0.100"`): **all three still fail** — PC→`eth0` and PC→node
  now report `Request timed out` (previously `Destination host
  unreachable` — a minor behavior change, still a failure); node→PC still
  `0/4 replies`.
- **`bridge stats`/`bridge fdb show` re-checked after these ping attempts —
  revealed a precise, asymmetric root cause:**
  ```
  Δ port 0: pkts received +189, dest notme-ucast +5, fwd ucast +0 (stayed at 0 the whole time)
  Δ port 1: fwd ucast +5, fwd direct +5
  Δ FDB entry for the PC's MAC (learned on port 1): fwdPackets +5
  ```
  Interpretation: 5 unicast frames arrived on port 0 addressed to the PC's
  MAC and were **successfully forwarded port 0 → port 1** (the "5" lines up
  with the node's `ping 192.168.0.100` attempt reaching the PC). But **port
  0's `fwd ucast` counter never moved off 0 across the entire test run** —
  the bridge has forwarded broadcast/multicast in both directions
  (`fwd mcast` grows on both ports), and unicast port 0→port 1, but **has
  never once forwarded a unicast frame port 1 → port 0**, despite the
  node's MAC being correctly present in the FDB with a port-0 mapping. This
  exactly explains both remaining failures: the node's outbound requests
  reach the PC (port 0→1 works), but the PC's replies never make it back
  (port 1→0 unicast forwarding is dead) — and symmetrically, anything the
  PC initiates toward the node needs the same broken port 1→0 unicast path.
- **Conclusion:** promiscuous mode was real and necessary but not
  sufficient — there is now a distinct, more narrowly-scoped bug: **unicast
  forwarding *into* port 0 (LAN865x/T1S) never happens**, while unicast
  *out of* port 0 (learned from port 0, delivered to port 1) works, and
  broadcast/multicast works in both directions. Not yet investigated: what
  differs about the bridge's TX-to-port-0 path for forwarded frames vs. the
  LAN865x driver's own locally-originated transmissions (which do work fine
  — e.g. the earlier successful `ping ... i eth0` issued directly from the
  bridge's own console) — this is the next thing to dig into, likely in
  `drv_lan865x_api.c`'s TX entry point or how the bridge glue calls it for
  a forwarded (vs. locally-originated) packet.

### GMAC needs its own "promiscuous equivalent" too - `TCPIP_MAC_RX_FILTER_TYPE_ALL_ACCEPT`

- User asked whether GMAC also needs a promiscuous-mode-like setting (by
  analogy with the LAN865x fix above). Compared `TCPIP_GMAC_RX_FILTERS` in
  `configuration.h` between the two projects:
  - This project (before this fix): `BCAST_ACCEPT | MCAST_ACCEPT |
    UCAST_ACCEPT | CRC_ERROR_REJECT` only.
  - Sister project: additionally `MCAST_HASH_ACCEPT | UCAST_HASH_ACCEPT |
    CRC_ERROR_ACCEPT | MAXFRAME_ACCEPT | ALL_ACCEPT | FRAMEERROR_ACCEPT`.
  - `TCPIP_MAC_RX_FILTER_TYPE_ALL_ACCEPT` is GMAC's equivalent of the
    LAN865x promiscuous flag - plain `UCAST_ACCEPT` only accepts unicast
    frames addressed to GMAC's own MAC; without `ALL_ACCEPT`, any unicast
    frame addressed to someone else (e.g. the PC's MAC, needing forwarding
    to port 0) gets dropped by the GMAC RX filter before the bridge glue
    ever sees it. This lines up exactly with the observed
    `port 1: dest notme-ucast: 0` throughout every prior test - GMAC was
    silently discarding exactly the frames the bridge would need to relay
    toward the T1S side.
  - MCC field: component `GMAC` (under `TCP/IP STACK` -> `DATA LINK
    LAYER`) -> checkbox `TCPIP_GMAC_ETH_FILTER_ALL_ACCEPT`.
- User enabled this checkbox in MCC and regenerated - confirmed by re-reading
  `configuration.h`, which now includes
  `TCPIP_MAC_RX_FILTER_TYPE_ALL_ACCEPT` in `TCPIP_GMAC_RX_FILTERS` (file
  changed mid-investigation, timestamp 2026-08-30 19:52:01). Not yet
  built/flashed at the time this was found - user is doing that now, will
  report back before the next round of `bridge stats`/`bridge fdb show`/
  ping-matrix retesting.

### RESOLVED - full end-to-end bridging confirmed working

- User rebuilt and reflashed with the `TCPIP_GMAC_ETH_FILTER_ALL_ACCEPT` fix
  applied. `netinfo` confirmed the board came back up unchanged (`eth0`
  192.168.0.11, `eth1` 192.168.0.12, both Link UP).
- **The two tests that matter - PC and the other T1S node pinging each
  other directly, addressing neither of the bridge's own IPs - both
  succeeded:**
  ```
  PC (192.168.0.100) -> other T1S node (192.168.0.202): 4/4 replies, 1-3 ms
  Other T1S node (192.168.0.202) -> PC (192.168.0.100):  4/4 replies, 1-2 ms
  ```
  This is the actual proof of a working transparent Layer-2 bridge - traffic
  crosses from the 10BASE-T1S segment to the 100BASE-T segment and back
  without either endpoint needing to know the bridge exists.
- `bridge stats` confirms the fix symmetrically: **`port 0 fwd ucast: 9` and
  `port 1 fwd ucast: 9`** - both directions now forward unicast traffic
  (port 0's `fwd ucast` had been stuck at exactly 0 through every prior test
  in this session; now moving in lockstep with port 1).
- Minor anomaly noted, not blocking: a PC ping to the bridge's `eth0` IP
  (192.168.0.11) got a reply reporting source IP `192.168.0.12` (`eth1`'s
  IP) instead of `.11`. Likely a source-IP-selection quirk once both
  interfaces are bridged into one L2 segment; not yet investigated, doesn't
  affect the actual node<->PC bridging that was the goal.
- **Summary of the full root-cause chain this session, for anyone reading
  this log later:** the original boot failure (`GMAC MAC initialization
  failed`) was caused by an undersized TCP/IP heap and libc heap (fixed by
  raising `TCPIP_STACK_DRAM_SIZE` and the linker `heap-size` to match the
  sister project). Once both interfaces came up, `eth1` had no usable IP due
  to typo'd `User` overrides in the MCC model (fixed). The MAC bridge was
  then found to already be enabled in the model/generated code but not
  actually forwarding traffic, traced via the built-in `bridge`
  status/stats/fdb CLI commands to two separate missing promiscuous-mode
  equivalents - `DRV_LAN865X_PROMISCUOUS_IDX0` for the LAN865x/T1S port and
  `TCPIP_GMAC_ETH_FILTER_ALL_ACCEPT` for the GMAC/100BASE-T port - both now
  enabled and confirmed working end-to-end.
- **Open items for follow-up, not blocking the core bridge function:**
  - The unidentified third MAC learned on port 0 (`00:04:25:ca:ce:d9`,
    noted earlier) - still unexplained.
  - The `.11`/`.12` reply source-IP anomaly noted above.
  - `eth1`'s gateway still `0.0.0.0` (noted earlier as likely harmless).
  - `docs\bridge-configuration-manual.md` is still just a skeleton - now
    that a full working configuration has been reached, this is a good
    point to start filling in its sections from what was learned this
    session.

- **User's conclusion from this test round, confirmed correct:** both network
  interfaces (10BASE-T1S `eth0` and 100BASE-T `eth1`) are individually fully
  functional — link up, correct IP, and every directly-connected peer
  reachable — but the **bridge function itself is not yet present/active**.
  This cleanly separates two previously entangled problems this session: the
  original GMAC/PHY bring-up bug (heap size, resolved) is a *different*
  problem from bridging not being enabled yet (a *planned*, not-yet-done
  step, not a bug) — the ping matrix is the evidence that draws that line.
- **New finding, not previously anticipated:** now that `eth0` and `eth1`
  share the same subnet (`192.168.0.0/24`), an **unqualified `ping <ip>`
  issued on the bridge's own console became ambiguous** — `ping
  192.168.0.202` (no `i eth0`) failed outright (0/4), while the identical
  target with `i eth0` explicitly appended succeeded (4/4). The stack no
  longer reliably infers the right outgoing interface once both sides of the
  bridge are on one subnet. This is likely *expected/necessary* behavior
  once the MAC bridge is actually enabled (a true L2 bridge presents both
  ports as one interface, so the ambiguity resolves itself) — but it's worth
  keeping in mind while testing pre-bridge-activation: **always pass an
  explicit `i eth0`/`i eth1` when pinging from the bridge's own console**
  until bridging is turned on.
- **Next step:** enable the MAC bridge (`TCPIP_NETWORK_MACBRIDGE_ADD_IDXn` on
  both `NETCONFIG-0`/`NETCONFIG-1`, per section 3 of `CLAUDE.md`), regenerate,
  rebuild/flash, then re-run the currently-failing rows (PC↔`eth0`,
  PC↔node, node↔PC) — those should start working once the bridge actually
  forwards between its two ports, which is the real end-to-end proof of a
  working bridge.
- **Exact commands used for this test round** (PowerShell tool unless noted;
  working directory for the Bash commands was
  `apps\tcpip_iperf_lan865x\`), in the order they were run:

  ```powershell
  # PC-side, round 1 (before the dual-IP/Wi-Fi issue was caught - unreliable, see above)
  ping -n 4 192.168.0.11
  ping -n 4 192.168.0.12
  ping -n 4 192.168.0.202

  # confirm the PC's interfaces/IPs
  Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like "192.168.0.*" -or $_.InterfaceAlias -like "*Ethernet*" } | Select-Object InterfaceAlias, IPAddress, PrefixLength

  # PC-side, round 2 (source address forced - the results actually reported above)
  ping -n 3 -S 192.168.0.100 192.168.0.11
  ping -n 3 -S 192.168.0.100 192.168.0.12
  ping -n 3 -S 192.168.0.100 192.168.0.202
  ```

  ```bash
  # Bridge-side, FAILED attempts (cli.bat / cmd /c quoting gotcha, see above) -
  # kept here as a record of what NOT to do, not as a working recipe:
  cd /c/work/t1s_bridge/bridge/harmony/net_10base_t1s/apps/tcpip_iperf_lan865x && \
    PYTHONIOENCODING=utf-8 MSYS_NO_PATHCONV=1 cmd /c "C:\work\t1s_bridge\bridge\harmony\net_10base_t1s\apps\tcpip_iperf_lan865x\cli.bat --port COM8 --read 4 \"ping 192.168.0.202\"" < /dev/null
  cd /c/work/t1s_bridge/bridge/harmony/net_10base_t1s/apps/tcpip_iperf_lan865x && \
    PYTHONIOENCODING=utf-8 MSYS_NO_PATHCONV=1 cmd /c "C:\work\t1s_bridge\bridge\harmony\net_10base_t1s\apps\tcpip_iperf_lan865x\cli.bat --port COM8 --read 4 \"ping 192.168.0.100 i eth1\"" < /dev/null

  # Bridge-side (COM8), working form - direct python.exe invocation:
  PYTHONIOENCODING=utf-8 "/c/work/t1s_bridge/bridge/t1s_100baset_bridge/.venv/Scripts/python.exe" "scripts/cli.py" --port COM8 --read 4 "ping 192.168.0.202" < /dev/null
  PYTHONIOENCODING=utf-8 "/c/work/t1s_bridge/bridge/t1s_100baset_bridge/.venv/Scripts/python.exe" "scripts/cli.py" --port COM8 --read 8 "ping 192.168.0.202" < /dev/null
  PYTHONIOENCODING=utf-8 "/c/work/t1s_bridge/bridge/t1s_100baset_bridge/.venv/Scripts/python.exe" "scripts/cli.py" --port COM8 --read 8 "ping 192.168.0.202 i eth0" < /dev/null
  PYTHONIOENCODING=utf-8 "/c/work/t1s_bridge/bridge/t1s_100baset_bridge/.venv/Scripts/python.exe" "scripts/cli.py" --port COM8 --read 8 "ping 192.168.0.100 i eth1" < /dev/null

  # Other T1S node-side (COM23):
  PYTHONIOENCODING=utf-8 "/c/work/t1s_bridge/bridge/t1s_100baset_bridge/.venv/Scripts/python.exe" "scripts/cli.py" --port COM23 --read 6 "netinfo" < /dev/null
  PYTHONIOENCODING=utf-8 "/c/work/t1s_bridge/bridge/t1s_100baset_bridge/.venv/Scripts/python.exe" "scripts/cli.py" --port COM23 --read 8 "ping 192.168.0.100" < /dev/null   # 1st try, ran out before final line
  PYTHONIOENCODING=utf-8 "/c/work/t1s_bridge/bridge/t1s_100baset_bridge/.venv/Scripts/python.exe" "scripts/cli.py" --port COM23 --read 8 "ping 192.168.0.11" < /dev/null
  PYTHONIOENCODING=utf-8 "/c/work/t1s_bridge/bridge/t1s_100baset_bridge/.venv/Scripts/python.exe" "scripts/cli.py" --port COM23 --read 8 "ping 192.168.0.100" < /dev/null   # 2nd try, still ran out
  PYTHONIOENCODING=utf-8 "/c/work/t1s_bridge/bridge/t1s_100baset_bridge/.venv/Scripts/python.exe" "scripts/cli.py" --port COM23 --read 8 "ping 192.168.0.12" < /dev/null
  PYTHONIOENCODING=utf-8 "/c/work/t1s_bridge/bridge/t1s_100baset_bridge/.venv/Scripts/python.exe" "scripts/cli.py" --port COM23 --read 12 "ping 192.168.0.100" < /dev/null  # 3rd try, --read 12 finally caught the "done" line
  ```

  Also confirmed earlier in this session (same direct-`cli.py` form, `--read 3`)
  for `reset` and `netinfo` on COM8 — those single-word commands worked fine
  even through the `cli.bat`/`cmd /c` wrapper; the quoting gotcha above only
  bit once a command had an embedded space.
- **Confirmed-working reference binary identified:** the user confirmed
  `C:\work\t1s_bridge\bridge\t1s_100baset_bridge\release\T1S_100BaseT_Bridge.hex`
  (built 2026-08-29, 565258 bytes) is a working build of the sister project —
  its 100BASE-T PHY comes up cleanly on that hardware. Note it predates the
  `peek`/`poke` CLI commands added to this project below and only has `dump`.
  Useful as a known-good fallback/comparison image if needed (e.g. to confirm
  the sister board's PHY still links with a binary independent of any source
  changes made since).

### Porting readiness check: what else the sister project has enabled in MCC

- Before porting the sister project's extra application features (packet
  mirroring, a test server, a "no-IP" test mode, persistent env config,
  LAN865x diagnostics - all plain user source files, not MCC components),
  compared the two projects' MCC component trees to find prerequisite gaps.
  Found three MCC components present in the sister project but missing here:
  **Telnet Server** (`TCPIP_STACK_USE_TELNET_SERVER`, needed for the
  sister's `TCPIP_TELNET_AuthenticationRegister()` call), **Net Presentation
  Layer** (`net_Pres`, the crypto/TLS component Telnet depends on), and
  **Emulated EEPROM** (needed by the sister's `env.c` persistent-config
  module). Confirmed via each project's `TCP_forwardSlash_IP STACK`
  component-folder listing and `configuration.h`'s `TCPIP_STACK_USE_*`
  defines. The custom source files themselves (`env.c/h`,
  `lan865x_diag.c/h`, `noip_test.c/h`, `port_mirror.c/h`, `testserver.c/h`)
  sit directly under the sister's `firmware\src\` (same level as `app.c`),
  not under `config\default\`, so they need no MCC component of their own -
  just the three prerequisites above, and a plain file copy afterward.
- Discussed with the user how to port these without losing the ability to
  keep configuring the project with MCC afterward: every prerequisite
  component must be added via the MCC GUI + Generate Code (never by copying
  the sister project's already-generated files into `config\default\`,
  which would leave MCC unaware of them and at risk of being clobbered or
  left inconsistent on a future regenerate); the plain user files are safe
  to copy directly into `firmware\src\` since MCC never touches that
  location. Recommended an incremental approach: add one MCC component at a
  time, Generate Code + build + verify after each, before moving to the
  next - isolates whether any build failure comes from the newly-added MCC
  component itself or from code ported afterward.
- User enabled **Emulated EEPROM** and **Telnet Server** in MCC and
  generated - confirmed via `configuration.h` picking up `EMULATED_EEPROM0`
  and (further down, not yet re-read in full) the Telnet defines.

### Telnet authentication callback ported into `app.c`

- User asked for a Telnet authentication callback in `app.c`, matching the
  sister project's approach (a password check). Found it in the sister's
  `app.c`: `TelnetAuthenticationHandler(user, password, pInfo, hParam)` -  a
  hardcoded check (`user == "admin"`, `password == "password"`) via
  `strcmp()`, printing "Telnet Access Authenticated/Declined" and returning
  `true`/`false` accordingly; registered once in `APP_Initialize()` via
  `TCPIP_TELNET_AuthenticationRegister(TelnetAuthenticationHandler,
  &TelnetHandlerParam)`. Confirmed the `TCPIP_TELNET_AUTH_HANDLER` signature
  in this project's (newer) `telnet.h` is unchanged from what the sister
  project's package version expects - safe to port as-is, no API drift.
- Ported into this project's `app.c` (a user file, not MCC-generated):
  added `#include <string.h>` (for `strcmp`) and
  `#include "config/default/library/tcpip/telnet.h"`, the
  `TelnetAuthenticationHandler()` function and `TelnetHandlerParam`
  variable (placed in the Application Callback Functions section, next to
  `TCPIP_STACK_InitCallback`), and the registration call at the top of
  `APP_Initialize()`.

### Recurring MCC generator bug: missing `#include <stdarg.h>` - second occurrence, in `telnet.c`

- Immediately after enabling Telnet and regenerating, the build failed:
  ```
  ../src/config/default/library/tcpip/src/telnet.c: In function 'F_Telnet_PRINT':
  telnet.c:427: error: implicit declaration of function 'va_start' [-Werror=implicit-function-declaration]
  telnet.c:429: error: implicit declaration of function 'va_end' [-Werror=implicit-function-declaration]
  ```
  Same bug class already documented in `CLAUDE.md` section 3 for
  `drv_lan865x_api.c`'s `PrintRateLimited()` - MCC's code generator omits
  `#include <stdarg.h>` in generated files that use `va_start`/`va_end`,
  with no MCC GUI field to fix it (it's a generator bug, not a
  configuration option). This is the **second** distinct generated file hit
  by the same bug in this project.
- Fixed by adding `#include <stdarg.h>` directly to
  `firmware\src\config\default\library\tcpip\src\telnet.c` (a deliberate,
  documented exception to the "never hand-edit generated code" rule, since
  there is no MCC-side fix available). `CLAUDE.md` section 3's existing
  entry was broadened to cover both occurrences and to flag this as a
  recurring pattern to keep checking for, rather than filed as a new,
  separate entry.
- **Reminder for next time this file (or any other file using
  `va_start`/`va_end`) gets regenerated:** check whether the include
  survived the regenerate, same as the existing `drv_lan865x_api.c`
  reminder.

### DEFERRED: Telnet login still shows "Access Denied" despite the handler registering OK

- User tried logging in via Tera Term (`admin`/`password`) after the Telnet
  callback was ported - got "Access Denied" every time.
- First diagnostic round: added a temporary debug print inside
  `TelnetAuthenticationHandler()` (`Telnet auth attempt: user="..."
  password="..."`) to see the raw received credentials. User reported
  **nothing at all appeared on COM8** during a login attempt - meaning the
  handler itself was never being invoked (in `telnet.c`'s
  `F_Telnet_LogonCheck()`, `authRes` only gets set by calling
  `telnetAuthHandler` if it is non-`NULL`; if it's `NULL`, `authRes` stays
  at its initial `false` with zero console output - exactly matching the
  symptom).
- Traced the registration/reset ordering in the generated code to check
  whether this was plausible: `initialization.c`'s `SYS_Initialize()` calls
  `TCPIP_STACK_Init()` (line 811) before `APP_Initialize()` (line 818);
  `TCPIP_STACK_RUN_TIME_INIT` is `false` in this project, so all TCP/IP
  module inits (including Telnet's, which resets
  `telnetAuthHandler = NULL` - `telnet.c` line ~317) run synchronously
  inside `TCPIP_STACK_Init()`, i.e. before `APP_Initialize()` registers our
  handler - the ordering looked correct on paper, matching the sister
  project's identical pattern.
- Added a second temporary diagnostic: print the return value of
  `TCPIP_TELNET_AuthenticationRegister()` at boot (`Telnet auth handler
  registration: OK/FAILED`) - fires at startup, checkable with a plain
  `reset` over COM8, no need to coordinate timing with a live Tera Term
  attempt. **Result: registration succeeds ("OK").** So the handler *is*
  registered, yet still never gets invoked (or gets invoked but something
  else about the flow still ends in a denial) when an actual Telnet client
  connects - narrowed down but not yet resolved.
- Attempted to self-diagnose further by scripting a simultaneous COM8
  listener + Telnet client (`scratchpad/telnet_diag.py`, pyserial + raw
  socket) to capture both sides of a login attempt in one shot - blocked by
  `COM8` already being held open (`PermissionError: Access denied` from
  `pyserial`), most likely MPLAB X's own terminal/console view holding the
  port. Not yet retried with that closed.
- **Deferred at the user's request** to move on to other work. State to
  pick back up from: registration confirmed working, actual connection
  still denied by something not yet identified; the two temporary debug
  `SYS_CONSOLE_PRINT()` calls are still in `app.c` (harmless, left in place
  on purpose so the next session doesn't have to re-add them) and should be
  removed once Telnet login is confirmed working.

## 2026-08-31

### Five sister-project modules ported; board stopped booting entirely

- Ported `env.c/h`, `lan865x_diag.c/h`, `port_mirror.c/h`, `noip_test.c/h`,
  `testserver.c/h` from `t1s_100baset_bridge` into `firmware\src\` (plain
  user files, no MCC component needed - see the readiness check above), and
  wired their `_Initialize()`/`_Tasks()` calls into `app.c`. Fixed a batch of
  package-version API drift along the way (newer `net`/`tcpip` package here
  vs. the sister's older one): `TCPIP_MAC_PACKET` typedef instead of
  `struct _tag_TCPIP_MAC_PACKET`; `TCPIP_Helper_ProtSglList*` instead of
  `TCPIP_Helper_ProtectedSingleList*`; `TCPIP_STACK_NetDnsPrimarySet` instead
  of `..._NetAddressDnsPrimarySet`; `TCPIP_STACK_HEAP_TYPE_INTERNAL` instead
  of `..._INTERNAL_HEAP`. Two sister-project driver hand-patches
  (`DRV_LAN865X_SetPlcaNodeId()`, `DRV_LAN865X_SendRawEthFrame()`) don't
  exist in this project's driver at all - the PLCA-node-ID gap was documented
  as out of scope, the raw-frame send was reimplemented in `noip_test.c`
  using `TCPIP_PKT_PacketAlloc()` + `DRV_LAN865X_PacketTx()` directly.
  `nbproject\configurations.xml` and (since a GUI build wasn't run)
  `nbproject\Makefile-default.mk` were hand-patched to register the 5 new
  `.c` files as project sources - the itemPath entries are picked up by MCC
  normally; the Makefile entries need re-adding only if MPLAB X's own
  "Clean and Build" ever regenerates that file from scratch without them.
- `BUILD SUCCESSFUL`, but the board then produced **zero console output** on
  any reset - a regression from the previously-working bridge milestone.

### Boot-hang bisection - two false leads before the real causes

- Confirmed via `git worktree add ../net_10base_t1s_lastgood e569c7e` (a
  clean checkout of the last known-good commit, built/flashed
  independently) that the hang was software-caused, not a hardware fault:
  the worktree build booted cleanly on the same physical board.
- Established a reliable diagnostic recipe - far more trustworthy than
  watching the serial console, which can look silent for reasons unrelated
  to the firmware (see the tooling pitfall below):
  ```
  pyocd commander -t atsame54p20a -u <probe-id> -M pre-reset --elf <production.elf> -c "reg" -c "exit"
  xc32-addr2line.exe -e <production.elf> -f -C <pc-hex> <lr-hex>
  ```
  `-M pre-reset` resets and halts immediately; comparing PC across repeated
  invocations (identical address every time = genuinely stuck; changing
  address = running normally) distinguishes a real hang from a false alarm.
- **First false lead:** reverting `app.c` alone (keeping the 5 modules
  linked but uncalled) restored clean boot, so suspicion fell on `app.c`'s
  two large static buffers (`pkt_log`, `frame_data_pool`, ~27KB total)
  possibly landing in `.data` instead of `.bss` because of their explicit
  `= {0}` initializers - a real XC32/MPLAB pitfall in general, but *not*
  what was happening here (confirmed via the linker map: both were already
  in `.bss`). Dropping the initializers anyway (harmless, and correct
  practice regardless) did **not** fix the hang.
- **Second false lead, ruled out empirically:** total linked code size.
  Bisected by re-adding pieces of the port to a reverted `app.c` one at a
  time, flashing and pyOCD-checking each combination:
  - `LAN865X_DIAG_Initialize()` alone → boots fine.
  - `MIRROR_Initialize()` alone (no `LAN865X_DIAG`) → hangs, identically.
  - A synthetic ~1.7KB dead-weight const array + trivial functions (no
    `port_mirror.c` involved at all, and *bigger* than `port_mirror.o`'s
    actual ~1.65KB footprint) → boots fine.
  This proved it was not "total flash image size crossed some threshold" -
  something specific to `port_mirror.c`'s content was responsible.
- Narrowed further by replicating `port_mirror.c`'s `mirror_pool_init()`
  logic inline in `app.c` (no `port_mirror.c` linked at all): 8x
  `TCPIP_PKT_PacketAlloc(sizeof(TCPIP_MAC_PACKET), 1518,
  TCPIP_MAC_PKT_FLAG_STATIC)` + `TCPIP_Helper_ProtSglList*` calls →
  **hangs**. The list-only calls alone (no `TCPIP_PKT_PacketAlloc()`) →
  boots fine. This isolated the trigger to calling `TCPIP_PKT_PacketAlloc()`
  from `app.c`, but - misleadingly, as it turned out - every one of these
  bisection builds still showed the CPU stuck inside unmodified,
  MCC-generated `FDPLL0_Initialize()` (`plib_clock.c:71`,
  `SYS_Initialize()` → `CLOCK_Initialize()`), a pure hardware
  `OSCCTRL_DPLLSYNCBUSY` register poll with no logical connection to
  anything in `app.c` - the actual reason different `app.c` content flipped
  the outcome stayed unexplained by pure source-code reasoning at this
  point (see "Root cause 1" below for why).

### Tooling pitfall found and fixed along the way: `cli.py` false "hang" reports

- Twice, `cli.py --read N "reset"` looked like a hang (`timeout 15 ...
  --read 20 "reset"` exiting with code 124) and was briefly misreported as
  "board hangs" - actually caused by wrapping the call in a **shorter**
  outer `timeout` than the script's own `--read` window, which kills it
  deterministically regardless of the board's state. Root-caused, fixed,
  and documented: `apps\tcpip_iperf_lan865x\CLAUDE.md` section 2 (the
  `cli.py`/pyOCD-recipe rule) and `~\.claude\knowledge\windows-shell-fallstricke.md`
  (general "outer timeout vs. a script's own wait window" lesson).

### Root cause 1 (hardware/silicon): confirmed Microchip Silicon Errata DS80000748K, item 2.13.2

- Consulted the official **SAM D5x/E5x Family Silicon Errata and Data Sheet
  Clarification** (`DS80000748K`), section 2.13.2 "FDPLL Ratio in
  DPLLnRATIO": *"When changing the FDPLL ratio in DPLLnRATIO register
  on-the-fly, STATUS.DPLLnLDRTO will not be set when the ratio update will
  be completed."* Affects **both** Rev A and Rev D silicon (this board is
  covered either way).
- Confirmed by direct register inspection after a boot that had been
  patched to no longer wait forever (see fix below): `OSCCTRL_DPLLRATIO`
  (`0x40001034`) correctly reads back `0x77` (119, the value written) and
  `OSCCTRL_DPLLSTATUS` (`0x40001040`) correctly reads `0x3`
  (`LOCK_Msk | CLKRDY_Msk`, i.e. genuinely locked) - **but**
  `OSCCTRL_DPLLSYNCBUSY` (`0x4000103C`) still reads bit2
  (`DPLLRATIO_Msk`) set, indefinitely, long after the DPLL is provably
  locked and running correctly. The status/sync feedback for this specific
  register is simply not trustworthy on this silicon, exactly as the
  errata describes - the DPLL itself works fine.
- **Fix applied** (hand-patch to MCC-generated `plib_clock.c`, documented
  exception per `CLAUDE.md` section 3 - no MCC GUI field exists for this):
  added a bounded iteration count (`CLOCK_DPLL0_SYNC_TIMEOUT`, initially
  tried at `100000` - found to take 15-20+ seconds wall-clock because each
  poll of a cross-clock-domain synchronized status register is expensive,
  not because of loop-body cost; reduced to `2000`, confirmed sufficient)
  to all three `FDPLL0_Initialize()` wait loops (`DPLLSYNCBUSY.DPLLRATIO`,
  `DPLLSYNCBUSY.ENABLE`, `DPLLSTATUS.LOCK|CLKRDY`), breaking out instead of
  spinning forever. Re-apply this function after any regenerate that
  touches `plib_clock.c`.
- **Caution for future debugging in this file:** a naive `-M attach` +
  `halt` pyOCD snapshot showed the CPU sitting in C-runtime startup
  (`__dinit_clear`/`__pic32c_data_initialization`) on **both** a hung build
  and the known-good baseline alike - a sampling artifact of that specific
  attach mode, not a real program-counter location. Register-level ground
  truth (reading `OSCCTRL_DPLLSTATUS`/`DPLLRATIO`/`DPLLSYNCBUSY` directly,
  or the `-M pre-reset` + repeated-PC-comparison recipe above) is what
  actually resolved this - don't trust a single `-M attach` snapshot's PC
  in isolation.

### Root cause 2 (application bug): `MIRROR_Initialize()` touched the TCP/IP heap before it was ready

- With root cause 1 fixed, the board's clock came up correctly but the
  serial console still produced **no output and no response to any
  command**. A `-M pre-reset` register dump this time showed a genuine,
  reproducible hang (identical PC across repeated halts) inside
  **`HardFault_Handler`** (`exceptions.c:80`), not the clock code.
- Read the fault status registers directly:
  `CFSR` (`0xE000ED28`) = `0x00008200` → `BFSR.PRECISERR` +
  `BFSR.BFARVALID` set (a precise bus fault, faulting address valid);
  `BFAR` (`0xE000ED38`) = `0x0100a8c4` - **not a valid address** in this
  device's flash (`0x00000000`-`0x000FFFFF`) or RAM (`0x20000000`+) map at
  all, i.e. a genuine wild-pointer dereference. Decoded the
  hardware-auto-stacked exception frame from the stack pointer at fault
  time to recover the actual faulting `PC`/`LR` (offsets `+0x18`/`+0x1C`
  from the frame base: `r0,r1,r2,r3,r12,LR,PC,xPSR`), then resolved them:
  ```
  TCPIP_HEAP_MallocInline   library/tcpip/src/tcpip_heap_alloc.h:197
  mirror_pool_init          port_mirror.c:173  (a TCPIP_PKT_PacketAlloc() call)
  ```
- Root cause: `MIRROR_Initialize()` (specifically its `mirror_pool_init()`,
  which pre-allocates 8 static `TCPIP_MAC_PACKET`s via
  `TCPIP_PKT_PacketAlloc()`) was called directly from `APP_Initialize()`.
  `APP_Initialize()` runs *synchronously*, still inside `SYS_Initialize()`
  (`initialization.c:862`, right after `TCPIP_STACK_Init()` at line 855) -
  but `TCPIP_STACK_Init()` only *starts* the TCP/IP stack's own
  asynchronous initialization (visible later as "TCP/IP Stack:
  Initialization Started/Ended" printed from a callback that fires over
  several subsequent main-loop iterations); the heap is not necessarily
  set up yet at the point `APP_Initialize()` runs. The sister project's own
  comment in `port_mirror.c` ("called from `MIRROR_Initialize()`, well
  after `TCPIP_STACK_Init()` has set the heap up") turned out to be an
  assumption that does not hold for this project's exact init ordering.
  The other three ported modules' `_Initialize()` functions only call
  `SYS_CMD_ADDGRP()` (CLI command registration, no heap access) - which is
  exactly why only `MIRROR_Initialize()` ever crashed.
- **Fix applied** (in `app.c`, a genuine user file - no MCC exception
  needed): moved the `MIRROR_Initialize()` call out of `APP_Initialize()`
  and into the existing `APP_STATE_SERVICE_TASKS` state in `APP_Tasks()`,
  right alongside the packet-handler registration and `env_apply()` call -
  the same place this app.c already deferred every other "the stack must
  be up" operation to, via the pre-existing `APP_STATE_INIT` →
  `APP_STATE_WAIT` (5-tick delay) → `APP_STATE_SERVICE_TASKS` sequence.

### RESOLVED - full boot confirmed working end-to-end with all 5 ported modules

- Rebuilt, flashed, and tested over the serial CLI
  (`cli.py --port COM8 --read 10 "reset"`): clean boot text
  (`TCP/IP Stack: Initialization Started/Ended - success`, LAN865x reset
  complete, PLCA node ID applied), followed by `help` (all 7 command groups
  present: `span`, `iperf`, `tcpip`, `testserver`, `noip`, `lan`, `Test`,
  `env`), `mirror` (prints its debug counters, no crash), `stats` (shows
  real eth0/eth1 TX/RX traffic - the bridge is actively forwarding), and
  `showenv` (persisted config intact: IPs, MACs, PLCA node/count).
- Both root causes were independent and additive: the DPLL errata hang
  masked the heap-timing bug entirely (execution never got far enough to
  reach `mirror_pool_init()` before this session's fix), which is why the
  earlier `app.c`-content bisection looked erratic/size-dependent rather
  than pointing cleanly at one function - different builds hit the
  timing-marginal DPLL sync-status quirk differently before either bug
  could even be observed directly.
- **Not yet done:** confirming MCC can still cleanly regenerate this
  project (`Generate Code` in MPLAB X) with all three hand patches
  (`plib_clock.c`, `drv_lan865x_api.c`, `initialization.c`) surviving or
  being straightforward to re-apply - needs the MPLAB X GUI, left for the
  user.

### Full per-module CLI verification matrix - all green

- `lanhelp`/`lan_read 0x0004CA02`/`testmode`/`plca_node`/`plca_stat`/`sqi`
  (all no-arg/status forms): all responded correctly - `lan_read` on the
  PLCA_CTRL1 register read back `0x805` (NODE_CNT=8<<8 | NODE_ID=5,
  matching `plca_node`'s own report), `plca_stat` showed real bus activity
  (12 transmit opportunities, 15 BEACONs, link in range), `testmode`
  confirmed test mode 0 (normal operation).
- `noip_stat` (TX=0/RX=0) → `noip_send 3` (3 frames sent, seq 1-3) →
  `noip_stat` again (TX=3, confirmed).
- `testserver` (idle) → `testserver start` (listening on port 5566) →
  `testserver stop` (stopped, rx=0 tx=0 bytes - no client connected during
  this quick check, but the start/stop lifecycle itself is clean).
- `mirror 1`/`mirror 0`: turns on/off cleanly, `rx_hook` debug counter
  increments while on (6 frames seen). `sniffer 1`/`sniffer 0`: turns the
  LAN8651 transmitter off/back on via a verified register RMW
  (`LAN865X RMW OK ... [VERIFY] PASS`, addr `0x000308F9` bit 0x4000, the
  T1SPMACTL.TXD bit), `tx_submitted=6`/`ack_ok=6`/`max_len_ok=88` proved
  real frames were mirrored and actually transmitted successfully -
  finished with sniffer back OFF (T1S TX re-enabled), no leftover state.
- **Persistence across a real reboot, not just RAM:** `setenv mirror 1` →
  `saveenv` → `reset` (full board reboot) → `showenv` correctly reported
  `mirror ON at boot` - EEPROM persistence survives an actual power-on/
  reset cycle, not just the current session. Restored to `mirror 0` +
  `saveenv` afterward (the bench default), then `readenv` (reload from
  EEPROM without a reboot) confirmed the same state - both persistence
  paths work. **`resetenv` deliberately not exercised** - it would reset
  IPs/MACs/PLCA node config to compiled defaults and persist that, which
  would disrupt the physical multi-node test setup currently in use; left
  for a moment when that disruption is acceptable.
- **Bridge regression via `bridge status`/`bridge stats`/`bridge fdb
  show`** (stronger evidence than a one-off ping: shows continuous live
  traffic, not a single round-trip): status `2` (running), stats clean
  (`failPktAlloc/failDcptAlloc/failLocks/fdbFull` all `0`,
  `pktPoolEmpty`/`dcptPoolEmpty` both `0`), both ports actively forwarding
  (port 0: 114 received/103 fwd mcast; port 1: 107 received/114 fwd
  mcast). FDB showed 17 learned entries including real external MAC
  addresses on both ports with `fwdPackets` in the thousands (1566, 1518)
  - the bridge has been forwarding real traffic continuously and
  correctly throughout this session's testing, not just passing an
  isolated check.

### Committed and pushed (commit `578b86f`)

- The 5-module port plus both boot-hang fixes (DPLL errata timeout,
  `MIRROR_Initialize()` deferral) were committed and pushed to
  `origin/master` (`e569c7e..578b86f`) at the user's request. Full CLI
  verification matrix and bridge regression (see the entries above) were
  green beforehand.

### Impact analysis: what an MCC "Generate Code" run would do to the three hand patches

- User asked, before actually running Generate Code in MPLAB X, what the
  concrete consequences would be. Answered and recorded here so this
  doesn't have to be re-derived next time Generate Code is considered.
- MCC's Generate Code only rewrites files under `config\default\**`
  (driven by the `.mc4`/component model); it never touches
  `firmware\src\*.c/h` directly under `firmware\src\` (i.e. `app.c/h` and
  all 5 newly-ported modules are completely safe), and it does not touch
  `nbproject\configurations.xml` (an MPLAB X IDE project-file concern, not
  an MCC one) - the 5 ported files stay registered as project sources
  either way. That leaves exactly the three documented-exception hand
  patches (`CLAUDE.md` section 3) at risk, each with a different severity:
  1. **`peripheral\clock\plib_clock.c` - most severe.** `FDPLL0_Initialize()`
     would regenerate back to its original unbounded
     `while(...DPLLSYNCBUSY...)` polling loops, with the
     `CLOCK_DPLL0_SYNC_TIMEOUT`-bounded retry removed. Since this loop
     genuinely never exits on this silicon (Errata DS80000748K 2.13.2 -
     see the root-cause entry above), **the board would fail to boot at
     all again**, identically to the hang this session spent most of its
     time diagnosing. This is the one that absolutely must be re-applied
     before any post-regenerate build is even worth flashing.
  2. **`driver\lan865x\src\dynamic\drv_lan865x_api.c` - functional
     regression, not a boot blocker.** The `mirror_eth0_tx_hook()` call
     inserted into `DRV_LAN865X_PacketTx()` would be dropped. `mirror`/
     `sniffer` would keep working for the RX direction (T1S bus -> eth1,
     wired via `MIRROR_Eth0Rx()` called directly from `app.c`'s packet
     handler, unaffected) but would silently stop mirroring the bridge's
     own TX traffic (its own ARP/ping replies) - no crash, no error
     message, just quieter Wireshark captures than expected.
  3. **`config\default\initialization.c` - cosmetic/persistence
     regression, not a boot blocker.** The `#include "env.h"`, the
     `ENV_Init()`/`env_mac_str()`/`drvLan865xInitData[0].nodeId/nodeCount`
     block, the `s_macAddrStr0`/`s_macAddrStr1` buffers, and dropping
     `const` from `drvLan865xInitData[]` would all be reverted.
     Consequence: persisted MAC addresses (`setenv mac0/mac1` +
     `saveenv`) would no longer be applied at boot - `TCPIP_HOSTS_
     CONFIGURATION[]` falls back to the compile-time
     `TCPIP_NETWORK_DEFAULT_MAC_ADDR_IDX0/1` macros - and the PLCA node
     ID/count would only be corrected later, at `env_apply()` time in
     `APP_STATE_SERVICE_TASKS`, instead of being pre-seeded into
     `drvLan865xInitData[]` before the driver's own init reads it -
     meaning a brief window right after boot where the node is live on
     the T1S bus under the wrong PLCA identity, same as before this
     session's `initialization.c` patch existed.
- **Net takeaway:** a Generate Code run is not harmless but also not
  catastrophic - re-applying patch 1 is mandatory just to get a bootable
  image again; patches 2 and 3 are optional quality-of-life restorations
  that can be re-applied at leisure afterward. Recommended order after any
  future Generate Code run: re-apply `plib_clock.c` first, rebuild and
  flash to confirm the board boots at all, *then* re-apply `drv_lan865x_
  api.c` and `initialization.c`, rebuild/flash/retest again.
- **Not yet actually exercised** - this is a predictive analysis from
  reading the three patches' content, not a verified-by-running-Generate-
  Code result. Doing that run (MPLAB X GUI, left for the user) and then
  confirming/correcting this analysis against what MCC actually did is
  still an open item.

### Sniffer/mirror completeness validation ported and run from the sister project

- User pointed out the sister project (`t1s_100baset_bridge`) had done an
  extensive, multi-document investigation into the `sniffer`/mirror path
  (`docs/SNIFFER_1_HYPOTHESEN.md` through `SNIFFER_4_ERGEBNISSE.md`,
  `SNIFFER_CAPTURE_VALIDATION.md`): a real bug (large mirrored frames
  >1514 bytes wedging the PC's USB-Ethernet adapter/Npcap capture),
  root-caused to something outside the bridge (PC/adapter/driver side,
  not reproducible as a firmware bug), mitigated in `port_mirror.c` by
  truncating every mirrored frame to `MIRROR_SAFE_FRAME_LEN` (1514) -
  **already ported verbatim into this project** when `port_mirror.c` was
  copied over. Asked whether to re-validate on this project's own bench.
- Chose the thorough option: ported `scripts/iperf_matrix_test.py` and
  `scripts/sniffer_capture_test.py` from the sister project, adapting
  `DEVICES`/`BRIDGE_ETH0_IP`/`BRIDGE_ETH1_IP`/`PC_IP` to this project's
  actual addresses (Bridge eth0 `192.168.0.11`/eth1 `192.168.0.12`,
  confirmed live via `showenv`; PC `192.168.0.100` on "Ethernet 8",
  confirmed via `Get-NetIPAddress`; `tshark -D` confirmed capture
  interface index `2` is still "Ethernet 8" on this machine).
- **Pre-flight check found a real, unrelated problem before running
  anything:** `FollowerA` (COM10, one of the two shared T1S nodes on this
  bench used by both projects) reported the exact same eth0 MAC
  (`00:04:25:CA:CE:D9`) as this project's own Bridge. `env.c` derives the
  eth0 MAC from the SAME54's factory-programmed 128-bit device serial
  number (`env.c` line ~60-63) - collisions between two different chips
  are practically impossible, so this had to be a stale, manually-set
  `setenv mac0` value on `FollowerA` left over from earlier, unrelated
  work (not something this session's changes could have caused, and nothing
  wrong with the derivation scheme itself). Fixed directly on `FollowerA`
  (COM10, with the user's explicit go-ahead): `setenv mac0
  00:04:25:CA:CE:DB` + `saveenv` + `reset`, verified via `showenv`
  afterward - clean, no collision anymore.
- **Ran `sniffer_capture_test.py --udp-rate 10 --duration 5`**
  (`FollowerA` COM10/`192.168.0.201` <-> `FollowerB` COM23/`192.168.0.202`,
  Bridge COM8 as a passive `sniffer` tap, captures written to the
  scratchpad, log appended to `docs/sniffer_capture_results.log`):
  - UDP FollowerA->FollowerB: 3982 datagrams captured (3972 data + 10
    end-of-stream markers) vs. 3981 expected (source-reported sent count)
    - **COMPLETE**, every sequence id present, 0% loss per the
    destination's own report (9.34 Mbit/s).
  - TCP FollowerA->FollowerB: 3,595,990 bytes / 2485 segments captured vs.
    ~3,616,875 expected - **COMPLETE** (99.4%, within tolerance).
  - UDP FollowerB->FollowerA and TCP FollowerB->FollowerA: same results,
    mirrored (**COMPLETE** both ways).
  - Bridge's own mirror counters after the run: `rx_hook=18062
    passed_filter=18002 tx_submitted=18002 ack_ok=18002 ack_fail=0
    truncated=0 max_len_submitted=1504 max_len_ok=1504` - the 1514-byte
    safety cap never triggered at this frame size, and every mirrored
    frame the driver handed off was hardware-confirmed transmitted.
- **A genuine anomaly found and NOT glossed over:** the script's own
  frame-length sanity check (captured `frame.len` vs. what the packet's
  own IP/UDP headers claim) flagged **all 3982 UDP datagrams in both
  directions** as shorter than their headers claim - by exactly 10 bytes,
  every single time (spot-checked directly:
  `tshark -r ... -T fields -e frame.len -e ip.len -e udp.length` on
  several frames all showed `frame.len=1502, ip.len=1498,
  udp.length=1478` - `ip.len` and `udp.length` agree with each other
  perfectly [`20 + 1478 = 1498`], but the actually-captured frame is only
  `14 + 1488 = 1502` bytes, 10 bytes short of the `14 + ip.len = 1512`
  the header implies). This did **not** show up in the sister project's
  own validated runs (`SNIFFER_CAPTURE_VALIDATION.md` explicitly states
  "None of the runs below triggered this"). Why it didn't corrupt the
  completeness result: iperf's 4-byte UDP sequence id sits at the very
  *start* of the payload, so a consistent 10-byte shortfall at the *end*
  of every frame (landing inside the repetitive `0x55` filler payload
  used by this synthetic test traffic) never touches the bytes the
  completeness check actually reads.
- **Not yet root-caused, but a strong, specific lead found** (user
  recognized this as reminiscent of something in the sister project and
  asked to check - `docs/FALLSTRICKE.md` 2026-08-27, ~line 761 onward):
  the sister project root-caused a related bug where the LAN865x RX path
  **violates the `tcpip_mac.h` contract** - the driver does not strip the
  14-byte Ethernet header + 4-byte FCS from a received frame's
  `pktLen`/`segLen` before handing it to the stack, as the contract
  requires, making eth0 RX length **18 bytes larger** than the real
  frame. That caused two bugs there: `tcpip_mac_bridge.c` wrongly
  rejecting standard-size frames as over-MTU (`failMtu`), and copying 18
  bytes too many into adjacent heap memory when forwarding
  (`_MAC_Bridge_PacketCopy()`, `pktLen` used as the copy length from a
  pointer that already skips the header). Both fixed there with
  hand-patches to `tcpip_mac_bridge.c` (subtracting 18 from `pktLen` for
  `inPort==0`/eth0) - **out of scope for this project's driver contract**,
  a different, newer package version.
  **The direction here is the opposite** (10 bytes *short*, not 18 bytes
  *over*) but the mechanism this points to is the same suspect: this
  project's `mirror_ethpkt_to_eth1()` copies exactly `flen` bytes from
  `pMacLayer`, where `flen` is `MIRROR_Eth0Rx()`'s `rxPkt->pDSeg->segLen`
  verbatim - if *this* (newer) package version's LAN865x driver
  under-reports `segLen` by ~10 bytes for frames in this size class
  (rather than over-reporting by 18, as the sister's older package
  version did), the mirror copy would be truncated by exactly that much,
  fully explaining every observation above: `truncated` stayed `0`
  (`segLen` reads *under* the 1514 cap, not over it), the sequence id
  (early in the payload) survived intact, and every frame in this size
  class was affected identically (a systematic accounting offset, not
  random loss).
  **Confirmed directly, independently of the iperf/mirror test above**:
  used this project's own `ipdump 1` (logs `rxPkt->pDSeg->segLen` for
  every eth0 RX frame via `pktEth0Handler()`, no mirror/sniffer involved
  at all) and inspected ordinary background traffic already present on
  the shared T1S bus (PLCA-related multicast frames from the other nodes,
  `dst=01:00:5e:00:00:01`). Every one of them: reported `len=88`
  (`segLen`), but the frame's own IP header (`45 00 00 54 ...` at offset
  +14) declares total IP length `0x0054` = 84 bytes, i.e. a true frame
  length of `14 + 84 = 98` bytes - **exactly 10 bytes more than the
  `segLen` this driver reports**, matching the UDP mirror-capture finding
  precisely, on completely unrelated traffic. **This is no longer a
  hypothesis: `rxPkt->pDSeg->segLen` on this project's eth0/LAN865x RX
  path is confirmed systematically 10 bytes short of the true frame
  length** (at least for frames in this size range) - very likely the
  same class of `tcpip_mac.h` header/FCS-contract violation the sister
  project found in its own (older) package version, just with a
  different, not-yet-explained fixed offset in this (newer) one. Where
  exactly the missing 10 bytes come from (which part of header/FCS
  accounting) is still open - the next concrete step if this is picked
  back up, rather than starting from scratch.
- **Net assessment:** the mirror path's core guarantee - the mitigation
  for the original large-frame PC-adapter-hang bug, and basic delivery
  completeness (no missing sequence ids, correct TCP byte counts) - is
  confirmed working on this project's own hardware, matching the sister
  project's validated behavior. The 10-byte-per-frame anomaly is real,
  reproducible, and currently unexplained - flagged here as an open item
  for a focused follow-up (in the spirit of the sister project's own
  SNIFFER_1-4 investigation) rather than either dismissed or allowed to
  block reporting the otherwise-clean result.
- Both ported scripts committed to this project's own `scripts/` folder
  (`iperf_matrix_test.py`, `sniffer_capture_test.py`) for reuse in future
  sessions; not yet committed to git (pending user confirmation, this
  entry documents the run before that decision).

### RX `segLen` root-cause attempt: escalated from "fixed offset" to "non-deterministic"

- User asked to fix the confirmed `segLen`-short-by-10 finding. Static
  comparison against the sister project's own working Bridge found
  `tc6.c` (the OPEN Alliance TC6 SPI chunk protocol library) **byte-for-
  byte identical** between the two projects (only a trivial, unrelated
  init-style diff), and `drv_lan865x_api.c`'s `TC6_CB_OnRxEthernetSlice`/
  `TC6_CB_OnRxEthernetPacket` **functionally identical** (only renamed
  helper functions from the package-version API drift already documented
  elsewhere in this log). `DRV_LAN865X_CHUNK_SIZE_IDX0` and RTS/timestamp
  config also identical (`64`, no RX timestamping configured on either
  project). Sender-side bug ruled out: `FollowerA`/`FollowerB` (COM10/
  COM23) are the exact same physical boards already validated bug-free by
  the sister project's own `sniffer_capture_test.py` runs.
- With code and config identical on the suspect path, added temporary,
  runtime-switchable diagnostic instrumentation (hand-patches, documented
  exception, **to be removed once this is resolved**):
  - `tc6.c`: a non-static `uint32_t g_tc6DiagEnable` flag (toggleable live
    via `poke 0x2000b4e4 1/0`, address from the `.map` file after
    building) gates a `SYS_CONSOLE_PRINT` in `process_rx()` printing every
    RX chunk's `buf_len/sv/sbo/ev/ebo/mfd/twoFrames/offsetRx`.
  - `drv_lan865x_api.c`: `TC6_CB_OnRxEthernetPacket()` gated on the same
    flag, prints the final `len`/`segLen` plus the first 48 bytes of the
    received frame's own content, so the chunk trace can be directly
    correlated against the frame's own IP header.
- **Captured the exact same periodic background message type (from
  `FollowerB`/COM23, `8e:8c:a1`) at two different points in time and got
  two different, contradictory results:**
  - Earlier (`ipdump 1`, no chunk-level detail): `segLen=88` for a frame
    whose own IP header declares `ip.len=84` (true frame `14+84=98`) -
    **10 bytes short**.
  - Just now (`g_tc6DiagEnable`, full chunk trace + payload dump): the
    *same kind of message* (same sender, same IP total length `0x0054`
    = 84, only the IP ID/checksum differ - a different instance of an
    identical periodic transmission) produced `segLen=102` - **4 bytes
    over**, not short. Chunk trace: chunk 1 `sv=1 ebo=64` (full 64-byte
    chunk), chunk 2 `sv=0 ev=1 ebo=38` -> `tc6.c` correctly sums
    `64 + 38 = 102`. The arithmetic itself is exactly right given its
    inputs; if `102` is wrong, the LAN865x hardware itself reported an
    incorrect `EBO` (End Byte Offset) footer value for that chunk, not a
    software miscalculation.
  - **This rules out a fixed, code-explainable offset.** The same message
    type shows different, opposite-direction errors at different times -
    consistent with a timing-/race-dependent hardware or SPI-protocol
    issue, not a deterministic accounting bug reachable by reading source
    code. This is the same general *category* of problem as the sister
    project's own, only partially-resolved GMAC RX race condition
    (`FALLSTRICKE.md`: "the race condition got smaller with each step
    ... but not completely eliminated - there is apparently at least one
    more, unfound unguarded access point") - not the same bug (different
    peripheral, LAN865x/SPI here vs. GMAC there), but the same shape: an
    intermittent, hardware-adjacent RX length inconsistency that resists
    static code analysis and needs either statistical/timing-focused
    runtime investigation or hardware-level tracing (e.g. a logic
    analyzer on the SPI bus) to pin down conclusively.
- **Diagnostic flag left in place but disabled** (`poke 0x2000b4e4 0`,
  confirmed) so it does not flood the console during normal use; the
  instrumentation itself (`g_tc6DiagEnable` in `tc6.c`, the gated block in
  `drv_lan865x_api.c`) is still in the tree, ready to re-arm for a future
  continuation of this investigation, and should be stripped out entirely
  once this is either root-caused or the investigation is abandoned.
- **Status: escalated, not fixed, and not safely patchable from what is
  known so far.** Given the effort already invested and the apparent
  depth (matching the sister project's own multi-day, only-partially-
  successful hunt for a structurally similar RX race), continuing
  requires either (a) many more correlated captures to find a pattern in
  *when* the error occurs (load level? specific chunk boundary? timing
  relative to other bus activity?), or (b) hardware-level tracing beyond
  what this session's tooling can do. Reported to the user as-is rather
  than attempting a guessed patch to genuinely non-deterministic,
  hardware-adjacent behavior.

### Real fix applied, following the sister project's own successful methodology

- Asked the user how the sister project handled its own, structurally
  similar non-deterministic RX issue (the GMAC RX race in
  `FALLSTRICKE.md`). Their approach: (1) root-cause via **live memory
  inspection during a reproduced failure**, not just counters; (2) found
  the actual "lock" (`_DRV_GMAC_RxLock`/`Unlock`) was a **no-op** on this
  RTOS-less build, so an interrupt could freely preempt a task-context
  critical section; (3) real fix: make the lock a genuine
  `SYS_INT_Disable()`/`SYS_INT_Restore()` critical section; (4) verify
  with an escalating stress test, and (5) **honestly report the result
  even if it's "improved, not eliminated."** Applied the same approach
  here.
- **Found the equivalent mechanism in the LAN865x/TC6 path:**
  `_EventHandlerSPI()` (`drv_lan865x_api.c`) - the SPI transfer-complete
  callback, invoked from a genuine hardware interrupt (SPI/DMA completion)
  - calls `TC6_SpiBufferDone()` (`tc6.c`) with **no locking at all**.
  Meanwhile `_Lock()`/`_Unlock()` (the only guard around task-context
  `TC6_Service()`, which drives `process_rx()`/
  `TC6_CB_OnRxEthernetSlice()` - the exact functions that accumulate
  `g->offsetRx`/`macPkt->pDSeg->segLen`) turned out to wrap
  `OSAL_MUTEX_Lock()`/`Unlock()`, whose bare-metal ("basic" OSAL)
  implementation (`osal_impl_basic.h`) is a **plain flag check-and-clear**
  - it does not touch `SYS_INT_Disable`/NVIC/PRIMASK at all. The SPI
  completion interrupt can therefore freely preempt task-context RX chunk
  processing at any point - the exact same *shape* of bug as the sister
  project's GMAC race (a task-level "lock" that provides zero protection
  against the actual racing ISR), just in a different peripheral/driver.
- **Fix applied** (hand-patch to MCC-generated `drv_lan865x_api.c`,
  documented exception per `CLAUDE.md` section 3 - `DRV_LAN865X_INSTANCES_
  NUMBER == 1` in this project, confirmed via `configuration.h`, so a
  single saved-interrupt-state variable is safe): `_Lock()`/`_Unlock()`
  now also call `SYS_INT_Disable()`/`SYS_INT_Restore()` around the
  existing mutex, turning the guard into a genuine interrupt-safe critical
  section - the same mechanism as the sister project's fix, applied to
  the equivalent point in this project's driver.
- **Verified with the same runtime chunk-trace instrumentation, before
  and after:**
  - Before the fix: the *same* periodic background message (identical
    sender, identical declared IP length) showed **88** and **102** bytes
    of `segLen` at different points in time for a true-per-header length
    of 98 - non-deterministic, opposite-direction errors.
  - After the fix: the same message type showed **102** consistently
    across every sample taken (multiple separate captures, no more
    variance).
  - Re-ran the full `sniffer_capture_test.py` UDP/TCP completeness
    validation (both directions): still **COMPLETE** as before, and this
    time checked the underlying raw numbers directly -
    `frame.len=1502 ip.len=1498 udp.length=1478` for **all 3982** captured
    iperf UDP datagrams, both directions, with **zero variance** (`tshark`
    field extraction + `sort | uniq -c` showed exactly one distinct
    combination across all 3982 rows). Before the fix this appeared
    consistent too on a small sample, but the background-traffic evidence
    above proves it wasn't reliably so - the same size class could plausibly
    have shown the same kind of instability under different timing.
- **What this fix demonstrably achieved: eliminated the non-deterministic/
  racy component.** What it did **not** explain: a separate, now-provably
  *consistent* ~10-byte-short discrepancy specific to ~1512-byte frames
  (iperf UDP), and a consistent ~4-byte-over discrepancy for the ~98-byte
  background message type - two different fixed offsets for two different
  frame sizes, suggesting a genuine, deterministic, size-dependent
  chunk-boundary accounting detail in `tc6.c` (unrelated to the interrupt
  race just fixed) that has not been root-caused. An attempt to correlate
  a specific, deliberately-sized (`noip_send 1 0 1512`) test frame against
  the live chunk trace did not succeed - the frame confirmed sent by the
  source (`FollowerA`, `seq=2`) never appeared in the bridge's RX
  diagnostic window at all, a separate, likely PLCA-bus-related packet-loss
  question outside this investigation's scope.
- **Decision: stop here.** The interrupt-safety fix is a genuine,
  well-evidenced correctness improvement (real bug, real mechanism, real
  before/after verification) worth keeping regardless of the remaining
  open question. The residual deterministic per-size offset is a smaller,
  separate matter with no demonstrated functional impact (delivery
  completeness has been proven intact throughout this entire
  investigation - sequence ids, TCP byte counts) and diminishing returns
  from further digging in this session. Diagnostic instrumentation
  (`g_tc6DiagEnable` in `tc6.c`, the gated block in `drv_lan865x_api.c`)
  remains in place but disabled - should be stripped out before any
  release build, or re-armed if this is picked back up.

### Confirmed gap: `sniffer=1` persisted in env does not suppress T1S TX at boot

- User asked whether the firmware starts up fully in sniffer mode with
  the transmitter off when `sniffer` is persisted on. Verified directly:
  `setenv sniffer 1` + `saveenv` + `reset`, then - **before** issuing any
  `sniffer` command - `showenv` correctly reports `sniffer ON at boot
  (now: ON)`, but `lan_read 0x000308F9` (T1SPMACTL) reads back
  `0x00000000` - bit `0x4000` (TXD) is **not** set, i.e. the T1S
  transmitter is still actively enabled despite the firmware believing
  (and reporting) sniffer mode is on.
- This matches a gap already flagged in the `initialization.c` hand-patch
  comment during the original port: the sister project suppresses TX from
  the very first driver-init step via a `drvCfg.suppressTx` field that
  does not exist in this project's `DRV_LAN865X_Configuration` struct (a
  hand-patch to the struct's own type there, not just its values - out of
  scope for this port). `MIRROR_Initialize()` here only sets the RAM flag
  `s_sniffer_on` and deliberately does not call `SNIFFER_Set()` (ported
  comment assumes the driver-level suppression already happened, which is
  true on the sister project but not here).
- **Practical consequence:** a board configured with `setenv sniffer 1` +
  `saveenv` is NOT a passive/invisible tap immediately after boot - it
  keeps transmitting on the T1S bus (ARP/whatever else runs at startup)
  until `sniffer 1` is issued live over the CLI, at which point the real
  register write happens. For a true "silent from power-on" sniffer node,
  this would need either porting the `suppressTx` struct extension, or
  moving a live `SNIFFER_Set(true)` call earlier in the boot sequence
  (before `TCPIP_STACK_Init()`/driver init, likely not straightforward
  given `LAN865X_DIAG_Rmw()` needs `SYS_STATUS_READY`).
- Reverted the board to the clean default afterward: `setenv sniffer 0` +
  `saveenv` + live `sniffer 0` (both the persisted and the live RAM/
  hardware state now consistently OFF, register-verified).
- **Fixed** (user asked to implement it the same way as the sister
  project). Ported the `suppressTx` mechanism verbatim:
  - `drv_lan865x.h`: added `bool suppressTx;` to `DRV_LAN865X_Configuration`
    (same position as the sister project, right after `rxCutThrough`).
  - `drv_lan865x_api.c`'s `_InitUserSettings()` init state machine: inserted
    a new `case 9` that writes `T1SPMACTL` (`0x000308F9`) = `0x00004000`
    (TXD) when `drvCfg.suppressTx` is true, *before* the final "Enable
    Data Traffic" `NETWORK_CONTROL`/TXEN write (renumbered from `case 9`
    to `case 10`) - T1SPMACTL is untouched by every earlier step, reset
    default `0x0000`, so a plain write is safe.
  - `initialization.c`: added `.suppressTx = false,` to
    `drvLan865xInitData[]`'s default initializer, and
    `drvLan865xInitData[0].suppressTx = env_sniffer();` right alongside
    the existing `nodeId`/`nodeCount` env overrides (same hand-patch
    block, before `TCPIP_STACK_Init()`). Updated the block's comment
    (previously documented this as an intentional gap - now removed,
    replaced with the sister project's own rationale plus a pointer to
    the confirmed-bug entry above for context).
  - All three are hand-patches to MCC-generated files - documented
    exception, `CLAUDE.md` section 3 already updated.
- **Verified with the exact same test as the confirmed-bug entry above:**
  `setenv sniffer 1` + `saveenv` + `reset`, then - before any `sniffer`
  command - `lan_read 0x000308F9` now reads back `0x00004000` (TXD set)
  immediately after boot. Reverted to the clean default afterward
  (`setenv sniffer 0` + `saveenv` + live `sniffer 0`), register-confirmed
  back to `0x0`, `showenv` back to `sniffer OFF at boot (now: OFF)`.
  Also confirmed a normal boot with `sniffer` persisted OFF still works
  exactly as before (no regression) - `showenv`/`stats` both clean.

### Automated hand-patch re-apply tool (`patches/apply_patches.py`)

- Followed up on `docs/mcc-generated-code-patches.md` with automation: user asked
  whether a Python script could guarantee the hand-patches survive a `Generate Code`
  run, agreed on a git-patch-based design (fails loudly on mismatch instead of
  guessing), then asked for everything under one `patches/` subfolder.
- Built `patches/*.patch` (one unified diff per MCC-generated file, generated via
  `git diff <clean-baseline-commit> HEAD -- <file>` against the last commit where
  each file was still pristine MCC output - not hand-typed) plus
  `patches/apply_patches.py` (tries a clean `git apply`; if that fails, checks
  whether it's already applied via a reverse-apply check; reports `FAILED` only if
  neither works) and `patches/README.md`. The two recurring `#include <stdarg.h>`
  regressions are handled separately as a plain idempotent text check-and-insert,
  since no clean git baseline exists for either occurrence (already baked into the
  oldest available commit for both files). The temporary `tc6.c`/`drv_lan865x_api.c`
  diagnostic instrumentation is deliberately excluded - meant to be deleted, not
  preserved.
- **Verified end-to-end**, not just unit-tested: reverted all 4 git-diff-covered
  files plus both `stdarg.h` occurrences to their exact pre-patch (pristine MCC)
  content using `git show <baseline-commit>:<path>`, confirmed `git status` showed
  the expected 5 files modified, ran `apply_patches.py` for real, then diffed the
  result against the real committed `HEAD` state: **3 of 4 files came back byte-
  identical, the fourth (`drv_lan865x_api.c`) differed only by the intentionally-
  excluded `TEMP DIAG` block** - exactly the expected outcome. Restored the real
  files afterward (`git checkout HEAD --`), confirmed `apply_patches.py --check`
  reports all six items `OK` again and `git status` is clean.

### Ported `bridge_gui.py` (Bridge Status & Configuration GUI) from the sister project

- User connected the sister project's `bridge_gui.py` to this project's board over
  COM8 (screenshot) - it worked (the underlying `showenv` data it read was valid)
  but showed a "no model for TIBR v5" warning plus an Error dialog, because
  `env_model.json` only knew the sister's own `EBRG v5` id. Asked to port the GUI
  over, implicitly including fixing that recognition gap.
- Confirmed by reading `env.c` that this project's env record uses `ENV_MAGIC
  0x54494252` ('TIBR'), `ENV_VERSION 5`, `ENV_VARIANT "tcpip_iperf_lan865x_bridge"`,
  `sizeof(env_t) == 72` - and that every field's printed shape in `cmd_showenv()`
  (ip/mask/gw/dns, mac, plca id/count, mirror/sniffer at-boot lines) is
  field-for-field identical to `EBRG v5`'s regex patterns in the sister's
  `env_model.json`, confirming this was a pure JSON data addition, not a Python
  code change.
- Created `json/` (repo-root-relative to `scripts/`, exactly where
  `bridge_gui.py`'s `CONFIG_FILE`/`MODEL_FILE`/`ENV_MODEL_FILE` constants already
  expect it via `Path(__file__).parent.parent / "json"`, no path-logic changes
  needed): `lan8651_model.json` copied verbatim (chip register map, not
  project-specific), `env_model.json` cloned from the sister's with a new
  `"TIBR v5"` entry (`known_other_ids` also documents `EBRG`/`EPTP` so an
  unrelated variant's record is never misinterpreted), `bridge_config.json`
  written fresh with this project's real defaults (`192.168.0.11`/`.12`, PLCA id
  5/count 8, COM8).
- Copied `scripts/bridge_gui.py` and `scripts/dep_check.py`, adapted:
  `RELEASE_HEX` now points at this project's actual build output
  (`firmware/tcpip_iperf_lan865x.X/dist/default/production/tcpip_iperf_lan865x.X.production.hex`)
  since (unlike the sister's `build.bat`) this project's `build.bat` does not copy
  to a separate `release/` folder; `DEFAULT_CONFIG`'s fallback schema updated to
  match the current `ip0/mask0/.../mirror` key layout (was a stale pre-mask/gw/dns
  schema in the source, harmless in practice since `bridge_config.json` always
  exists once created, but corrected while porting); `dep_check.py`'s
  `INSTALL_SCRIPT` repointed at the sister project's own `batch\setup_venv.bat`,
  consistent with this project's documented "no own `.venv`, reuse the sister's"
  convention (CLAUDE.md section 2) - the missing-dependency dialog should never
  actually fire here since `sv_ttk` is already installed in that shared venv.
- Created `run_gui.bat` at the app root, mirroring `cli.bat`/`flash.bat`'s
  established pattern (hardcoded shared-venv `python.exe` path, falls back to bare
  `python`).
- **Verified against the real board, not just statically:** `python -m py_compile`
  on both ported `.py` files, `json.load()` on all three new JSON files, then a
  standalone regex check reusing `bridge_gui.py`'s own identity+field pattern
  matching logic against a live `showenv` capture
  (`cli.bat --read 4 "showenv"` over COM8) - the identity line resolved to model
  key `"TIBR v5"` and all 14 fields (`ip0/mask0/gw0/dns0/ip1/mask1/gw1/dns1/mac0/
  mac1/plca_id/plca_cnt/mirror/sniffer`) matched with correct extracted values
  (`192.168.0.11`, PLCA id 5 count 8, MAC `00:04:25:CA:CE:D9`/`...DA`, mirror/
  sniffer both OFF) - confirming the "no model for" warning is resolved before
  ever opening the actual window. Then launched `run_gui.bat` for real (background
  process, no immediate exception/traceback) against COM8 for the user to visually
  confirm the Bridge Parameters tab and exercise it interactively.

### Ported `gui_term.py` (three-pane serial terminal) from the sister project

- Follow-up to the `bridge_gui.py` port: "und jetzt das term" - `gui_term.py` is a
  separate standalone tool (three serial consoles in one window, one click
  connects all), not the inlined single-port Terminal tab already inside
  `bridge_gui.py`. `dep_check.py`'s own docstring already called it out as "the
  two GUI entry points, bridge_gui.py and gui_term.py".
- No code changes needed at all: `CONFIG` resolves `json/term_ports.json` via the
  same `parent-of-scripts/json/...` pattern as `bridge_gui.py`'s constants, which
  already lines up correctly in this project's layout, and the tool has no other
  project-specific strings (checked - only generic "T1S Bridge Terminals" window
  title). Copied `scripts/gui_term.py` verbatim.
- `json/term_ports.json` (per-machine COM-port-to-name assignment, gitignored in
  the sister project - never means anything on another machine) copied as-is
  since this is genuinely the *same bench*: slot 1 `bridge`/COM8 (this board),
  slot 2 `A`/COM10, slot 3 `B`/COM23 - the same three boards already named
  FollowerA/FollowerB in `iperf_matrix_test.py`/`sniffer_capture_test.py`.
- Created `run_term.bat` (mirrors the sister's own, shared-venv `pythonw.exe`
  pattern like `run_gui.bat`) and `apps/tcpip_iperf_lan865x/.gitignore`
  (`json/bench.json`, `json/term_ports.json` - per-machine state, same two
  entries as the sister project's `.gitignore`).
- **Verified**: `python -m py_compile` clean; `gui_term.py --selftest` (built-in,
  no window) ran **14/14 checks passed**, correctly read back the copied
  `term_ports.json` (`1=bridge(COM8), 2=A(COM10), 3=B(COM23)`, `font_size=10`);
  then launched the real window for real (background process, no immediate
  exception) for the user to click "Connect All" and confirm all three panes.

### First real MCC regenerate against `patches/apply_patches.py` - and a bug it exposed

- User ran MCC's plain "Generate" in MPLAB X first: touched only `definitions.h`
  (two `#include` lines reordered, cosmetic) plus the `*.yml` manifests
  (timestamps/hashes only) - confirmed via `git diff` and, tellingly, via mtimes:
  `plib_clock.c`/`drv_lan865x_api.c`/`drv_lan865x.h`/`initialization.c` kept their
  **old** mtime from the last hand-patch session, proving MCC's incremental
  generator skips a file entirely when nothing in its component model changed.
  Not yet the real test - `apply_patches.py --check` correctly reported
  "All patches present." because nothing had actually been touched.
- User then used the toolbar dropdown next to "Generate" -> **"Force Update on
  All"**, which bypasses the incremental skip and rewrites every generated file
  from its template regardless of model state, then accepted MCC's own
  diff/merge prompt for each changed file (took the regenerated version
  everywhere, no manual merging) - this is the real test `apply_patches.py` was
  built for.
- **Result:** all four hand-patched files really did get overwritten (`git
  status` showed them modified, mtimes updated to the Force-Update run).
  `apply_patches.py --check` correctly flagged all 6 items as missing
  (`WOULD APPLY`) - but its own final summary line still printed **"All patches
  present."**, a real bug: the summary only ever counted `FAILED` rows, never
  `WOULD APPLY`/`APPLIED`, so it could report "all good" even when every single
  patch was reported missing right above it. **Fixed** (`patches/apply_patches.py`):
  the summary now tracks `pending`/`applied` counts too and prints an accurate
  final line for each case (`"N patch(es) missing - re-run without --check to
  apply."`, exit code 1, when something is pending even though nothing failed).
- Ran `apply_patches.py` for real: **all 6 items applied cleanly** (`[+] APPLIED`
  for the 4 git-diff patches, `[+] APPLIED` for both `stdarg.h` inserts).
  `--check` afterward: clean `[ok] OK` across the board, now correctly printing
  "All patches present." Diffed the four core files against the last committed
  (hand-patched) `HEAD` state: **byte-identical** for `drv_lan865x.h`,
  `initialization.c`, `plib_clock.c`, `telnet.c`; `drv_lan865x_api.c` differed by
  only the deliberately-excluded TEMP DIAG block (12 lines, correctly NOT
  restored) plus one harmless blank line MCC's own template now inserts near the
  top of the file. `tc6.c` lost its TEMP DIAG instrumentation too, also correctly
  left alone (documented as excluded, not a patch-tool target).
- **Conclusion:** the patch-reapply tool now has a genuine, not just synthetic,
  end-to-end pass against a real "Force Update on All" - the scenario it exists
  for.

### Root-caused and fixed the residual sniffer/mirror RX length offset

- User captured a real sniffer-mode session in Wireshark (bridge in sniffer mode,
  iperf TCP between Follower A and B, capture on the PC's mirrored eth1):
  practically every large TCP segment showed "Previous segment not captured"
  immediately followed by "ACKed unseen segment" - a real, reproducible protocol-
  analysis-breaking symptom, not a false alarm. Traced to the "small, deterministic,
  unexplained residual RX length offset" left open in the interrupt-race fix
  earlier this session (`8972180`) - `tcp.seq` advanced by 1460 per segment (the
  sender's real payload) while the captured `tcp.len` was only 1450: exactly 10
  bytes missing from every large mirrored frame, not a Wireshark artifact.
- Re-added the TEMP DIAG instrumentation (`g_tc6DiagEnable`, wiped by the MCC
  Force-Update-on-All regenerate a few steps earlier - restored verbatim from
  commit `8972180`'s diff via `git show`) to `tc6.c`/`drv_lan865x_api.c`, then
  built+flashed+tested this investigation directly (user: "bau du, und flash und
  teste es genau so wie ich es gemacht habe").
  - First attempt: tracing every chunk during a real full-rate iperf run flooded
    the 115200-baud console faster than it could drain, corrupting the trace into
    garbled interleaved fragments. Fixed by gating the chunk-level trace to only
    the frame-ending chunk (`ev || twoFrames`, where `ebo`/`sbo` are actually
    computed) instead of every chunk - cut the volume ~24x for large frames.
  - Still flooded at full iperf rate; switched to a controlled reproduction
    instead: single large UDP datagrams (`iperf -u -b 20000 -l 1472`, ~0.6 s
    apart) via a small ad hoc Python harness driving `iperf_matrix_test.py`'s
    `DeviceServerCapture`/`_send_cmd` helpers directly, giving the UART time to
    fully drain between frames.
- **Found, with a clean trace**: `TC6_CB_OnRxEthernetPacket()` (`drv_lan865x_api.c`)
  consistently reports `len`/`segLen` = real frame length (header + payload)
  **+ 4 bytes** for every frame size tested (64/102/110/1518 for real 60/98/106/
  1514-byte frames) - almost certainly the trailing 4-byte Ethernet FCS that the
  T1S PHY still delivers over SPI and that the driver does not strip, contrary to
  `tcpip_mac.h`'s documented RX contract ("the MAC driver subtracts the FCS...
  before handing over the packet to the stack"). At this level `len` and `segLen`
  always agreed with each other (no corruption, no race) - ruling out `tc6.c`'s
  chunk-boundary math (`sv`/`sbo`/`ev`/`ebo`) as the cause of the visible symptom.
- Added a second, targeted diagnostic in `port_mirror.c` (`MIRRORDIAG`, gated the
  same way) and confirmed with the SAME frames that `rxPkt->pDSeg->segLen` had
  already dropped to exactly 14 less by the time `MIRROR_Eth0Rx()` reads it
  (1518 at the driver -> 1504 at the mirror hook). Found why in the MCC-generated
  `library/tcpip/src/tcpip_manager.c` (lines ~2544-2551): the generic stack RX
  path unconditionally subtracts `sizeof(TCPIP_MAC_ETHERNET_HEADER)` (14) from
  `segLen` before dispatching to registered packet handlers like
  `pktEth0Handler()`/`MIRROR_Eth0Rx()` - documented, correct, standard framework
  behavior (`tcpip_mac.h`: "segLen is updated by each stack layer in turn"), not
  itself a bug.
- **Root cause**: `port_mirror.c`'s `MIRROR_Eth0Rx()` read `rxPkt->pDSeg->segLen`
  at that point and used it directly as "how many bytes to copy starting at
  `pMacLayer`" - but by then it means "payload after the 14-byte MAC header", not
  "full frame length". Every RX-mirrored frame was therefore copied exactly 14
  bytes short. Invisible on small single-chunk frames whenever `MIRROR_SAFE_FRAME_LEN`'s
  later clamp (1514) never engaged, but very visible on anything needing more than
  one TC6 SPI chunk, where the true content ran past what got copied.
- **Fix** (`port_mirror.c`, `MIRROR_Eth0Rx()`, one line): pass
  `rxPkt->pDSeg->segLen + sizeof(TCPIP_MAC_ETHERNET_HEADER)` as the frame length to
  `mirror_ethpkt_to_eth1()` instead of `segLen` alone. Deliberately scoped to the
  RX-mirror call site only - checked `mirror_eth0_tx_hook()` (the TX-mirror call
  site) separately: TX packets are constructed by the stack itself and never go
  through the RX-side header-stripping code path, so their `segLen` already means
  "full frame including header" per `tcpip_mac.h`'s own TX contract; applying the
  same `+14` there would have double-counted and broken a path that was already
  correct.
- **Verified end-to-end, twice** (once right after the fix, once again after
  removing all TEMP DIAG instrumentation and a full clean rebuild+reflash): full
  `sniffer_capture_test.py` run - both UDP directions now `COMPLETE` with **no**
  "shorter than IP/UDP header claims" warning (previously present on every run),
  both TCP directions `COMPLETE`; `dbg: max_len_submitted` rose from `1504` to the
  correct `1514`; a direct `tshark` check of the TCP capture confirmed
  `frame.len=1514`/`tcp.len=1460` (previously `1504`/`1450`) and **zero**
  `tcp.analysis.lost_segment` flags across the whole capture (previously on nearly
  every large segment).
- Removed all TEMP DIAG instrumentation (`g_tc6DiagEnable` and every gated print in
  `tc6.c`, `drv_lan865x_api.c`, `port_mirror.c`) now that the root cause is fixed,
  per the plan documented in `patches/README.md`/`docs/mcc-generated-code-patches.md`.
  Confirmed via `grep -rn "TEMP DIAG\|g_tc6DiagEnable\|MIRRORDIAG\|TC6DIAG"
  firmware/src/` - no remnants. Final clean build+flash+full-test pass confirmed
  the fix holds with the diagnostics gone.
- This **supersedes** the earlier "no known functional impact" assessment in
  `CLAUDE.md` for the residual offset - it did have real impact (corrupted
  sniffer captures), just not on the already-separately-verified bridge-forwarding
  data path (the normal, non-mirror forwarding never went through
  `MIRROR_Eth0Rx()`, so it was never affected by this bug).

### Fixed Telnet login ("Access denied" in TeraTerm) - same root cause class as MIRROR_Initialize()

- User reported TeraTerm always showed "Access denied" logging into the Telnet
  console, asked to test it with a Python tool instead. Wrote a raw-socket test
  script (no `telnetlib` - removed in this machine's Python 3.14) plus a parallel
  `tshark` capture on `tcp port 23` to see the exact bytes on the wire, and a
  `cli.py --listen` capture of the debug console in parallel. Reproduced
  immediately: `admin`/`password` (the hardcoded credentials in
  `TelnetAuthenticationHandler()`, `app.c`) got `Access denied` every time, and -
  the key clue - the handler's own diagnostic print ("Telnet auth attempt:
  user=...") **never appeared** on the debug console, meaning the handler was
  never actually being called despite `APP_Initialize()` reporting successful
  registration ("Telnet auth handler registration: OK") at boot.
- **Root cause, found by reading `telnet.c` (MCC-generated)**: identical bug class
  to the `MIRROR_Initialize()` heap-timing bug fixed earlier this session.
  `TCPIP_TELNET_AuthenticationRegister(TelnetAuthenticationHandler, ...)` was
  called from `APP_Initialize()`, which runs synchronously inside `SYS_Initialize()`
  - before `TCPIP_STACK_Init()`'s asynchronous module initialization has actually
  reached the Telnet module. `TCPIP_TELNET_Initialize()` (`telnet.c` line 317)
  unconditionally sets its module-static `telnetAuthHandler = NULL;` as part of
  that later init - silently wiping out the registration that had appeared to
  succeed moments earlier. By the time a real connection came in,
  `telnetAuthHandler == NULL`, so `telnet.c`'s login-processing code
  (`M_TELNET_USE_AUTHENTICATION_CALLBACK != 0` branch, line ~717-733) never
  called our handler and `authRes` stayed `false` - "Access denied", with no
  trace of our handler ever running.
- **Fix** (`app.c`, no MCC-generated file touched): moved
  `TCPIP_TELNET_AuthenticationRegister()` out of `APP_Initialize()` and into the
  existing `APP_STATE_SERVICE_TASKS` phase, right after `MIRROR_Initialize()` -
  the same place already proven to be "stack is definitely up" for that earlier
  bug. Also removed the now-redundant "Temporary diagnostic: Telnet auth attempt:
  user=... password=..." print from `TelnetAuthenticationHandler()` (logged the
  plaintext password to the console on every attempt - fine for debugging a
  broken handler, not something to leave in once it works); kept the
  Authenticated/Declined outcome prints, which reveal nothing sensitive.
- **Verified end-to-end, twice** (once right after the fix, once again on a full
  clean rebuild+reflash after the debug-print cleanup): the same raw-socket
  Python test now gets `Logged in successfully` plus the full `help` command
  listing instead of `Access denied`, confirmed both in the script's own output
  and in a fresh `tshark` capture of the exchange on the wire.

### Fixed Telnet commands never being recognized ("Please type in a command" on every line)

- Follow-up to the Telnet login fix: user reported that after logging in,
  every typed command (even `help`) got "*** Command Processor: Please type
  in a command***" instead of running. Reproduced with a raw-socket Python
  test sending a whole line at once (`b"help\r\n"`) - that worked fine, so
  the bug needed a live capture to pin down. Captured the real TeraTerm
  session on `tcp port 23` from the moment of connection: TeraTerm sends
  each keystroke as its own TCP segment, and sends Enter as **`0d 00`
  (CR NUL)**, not CR LF - confirmed byte-for-byte in the capture (`tcp.payload
  == 0d00` on every Enter press, both for the username/password lines, which
  worked, and for the `help` line, which didn't).
- **Root cause**: `sys_command.c`'s (MCC-generated) character-input state
  machine (`RunCmdTask()`) handles `'\r'`/`'\n'` as end-of-line, but has no
  case for a bare `'\0'` - RFC 854 allows CR to be followed by LF *or* NUL,
  and TeraTerm uses the NUL form. The trailing NUL byte fell through to the
  generic "valid char; insert and echo it back" branch and got silently
  prepended to the *next* command's `cmdBuff`. Every later `ParseCmdBuffer()`
  call then did `strncpy()`/tokenized a C string whose first byte was `'\0'`
  - looks like an empty string to every string function even though real
  text follows it - so `argc` was always 0 and every command after the very
  first one in a session failed, while the very first one (typed into a
  still-clean buffer) worked. Reproduced synthetically once the real cause
  was known: two consecutive char-by-char `"help\r\x00"` sequences over a raw
  socket - the first succeeded, the second and every one after failed,
  matching the live TeraTerm symptom exactly.
- **Fix** (documented hand-patch exception, `sys_command.c` - see
  `docs/mcc-generated-code-patches.md`): added an explicit `else if (newCh ==
  '\0')` branch right after the `\r`/`\n` case that simply discards the byte
  instead of falling through to the character-insert branch. Regenerated
  `patches/sys_command.patch` from this change (`patches/apply_patches.py`
  now covers five hand-patched files, up from four).
- **Verified**: the same two-consecutive-`"help"` synthetic reproduction now
  shows all three attempts succeeding (previously only the first); confirmed
  live in TeraTerm by the user as well.

### Fixed all custom commands replying to the wrong console over Telnet

- User reported that after the Telnet login fix, typing commands worked (echoed
  correctly, parsed correctly) but their actual OUTPUT never appeared in
  TeraTerm - it went to the serial console instead. Confirmed directly:
  `showenv` over a raw Telnet socket echoed the command and produced nothing
  but an empty prompt.
- **Root cause**: every one of this project's own command modules (`env.c`,
  `app.c`, `port_mirror.c`, `lan865x_diag.c`, `noip_test.c`, `testserver.c`)
  used `SYS_CONSOLE_PRINT()` for a command's reply. That macro always targets
  `SYS_CONSOLE_DEFAULT_INSTANCE` - the fixed serial console - regardless of
  which device actually issued the command. Every `SYS_CMD_FNC` handler is
  passed a `pCmdIO` (`SYS_CMD_DEVICE_NODE*`) specifically so it can reply to
  the right one via `pCmdIO->pCmdApi->print/msg`, but none of the ported code
  used it - 231 `SYS_CONSOLE_PRINT` call sites across the six files, all
  silently hardcoded to serial.
- **Fix**: added `firmware/src/cmd_print.h` (`CMD_PRINT(pCmdIO, ...)` /
  `CMD_MSG(pCmdIO, str)`, thin wrappers around `pCmdIO->pCmdApi->print/msg`)
  and went through all six files converting every command-reply print site,
  file by file, building after each to catch mistakes early:
  - `env.c` (5 commands: showenv/setenv/saveenv/readenv/resetenv) and its
    `pr_addr()` helper (threaded `pCmdIO` through).
  - `app.c` (11 commands: test_help/cmd_stats/show_timestamp/cmd_uptime/
    cmd_logclear/cmd_logstat/my_dump/cmd_mem_dump/cmd_mem_peek/cmd_mem_poke/
    cmd_meminfo). `DumpMem()` itself was left untouched (also used by the
    deferred packet-log drain in `APP_Tasks()`, which has no command context
    and legitimately stays on serial, throttled against
    `SYS_CONSOLE_WriteFreeBufferCountGet()` - no telnet equivalent exists for
    that check) - added a parallel `CmdDumpMem(pCmdIO, ...)` for the actual
    `dump` command instead of giving the shared one a signature change.
  - `port_mirror.c` (cmd_mirror/cmd_sniffer/cmd_bigframe, plus
    `mirror_print_dbg_counters()` threaded with `pCmdIO`).
  - `noip_test.c` (cmd_noip_send/cmd_noip_stat; `NOIP_PrintRxLine()` left as
    serial-only background RX logging).
  - `testserver.c` (cmd_testserver; threaded `pCmdIO` through
    `testserver_start()`/`testserver_stop()`, its only two callers, both
    inside `cmd_testserver` - `TESTSERVER_Tasks()`'s own connect/disconnect
    logging stays serial-only, genuinely async with no command context).
  - `lan865x_diag.c` (the largest: 8 commands, 96 print sites). Its register
    operations are asynchronous by design (queued over SPI, completed later
    from `LAN865X_DIAG_Tasks()`, well after the command handler that started
    them has already returned) - `CMD_PRINT(pCmdIO, ...)` is not usable
    there directly. Added a second helper to `cmd_print.h`,
    `CMD_PRINT_OR_CONSOLE(pCmdIO, ...)` (prints to `pCmdIO` if non-NULL, else
    falls back to `SYS_CONSOLE_PRINT`), plus one module-static
    `s_diag_pCmdIO` in `lan865x_diag.c` remembering who started the currently
    pending operation - safe because the module already only ever allows one
    operation in flight at a time (`LAN865X_DIAG_Busy()`). Each command sets
    `s_diag_pCmdIO = pCmdIO` before triggering its operation;
    `LAN865X_DIAG_Tasks()`'s completion prints (read/write/rmw OK/failed/
    timeout, verify pass/fail, testmode decode, the whole chained
    `plca_stat` sequence, `sqi`'s report) use `CMD_PRINT_OR_CONSOLE()` with
    it. Two things deliberately left on `s_diag_pCmdIO = NULL` (serial-only):
    the `testmode` auto-revert timer (fires up to 600s later - whichever
    session armed it may be long gone) explicitly clears it first, and
    `LAN865X_DIAG_ApplyPlca()` was left as plain `SYS_CONSOLE_PRINT`
    throughout (it is called both from `cmd_plca_node`, a command, and from
    `env.c`'s boot-time `env_apply()`, which has no `pCmdIO` to offer and
    lives in a different translation unit - no reliable way to tell the two
    callers apart on the far side of one shared function without a larger,
    not-done-here change to thread `pCmdIO` through `env_apply()` itself).
- **Verified end-to-end** over a real Telnet session (raw socket, admin/
  password login): `showenv`, `stats`, `meminfo`, `mirror`, and `lanhelp` all
  now print their real output over Telnet instead of an empty prompt: and,
  the harder case, the two genuinely asynchronous diagnostics also came back
  correctly - `lan_read 0x000308F9` showed the real
  "LAN865X Read OK: Addr=... Value=..." completion line, and `plca_stat`
  showed its full chained RMW-then-multi-step-read report, both arriving on
  the Telnet socket that asked for them.
- Hit one unrelated tooling snag while editing `lan865x_diag.c`: a Python
  script used to do a large scoped find-and-replace (`open(path,
  encoding='utf-8')` in text mode, no `newline=''` on the read side) silently
  normalized the whole file from CRLF to LF on write. Caught it from git's
  own "LF will be replaced by CRLF" warning before committing; fixed by
  re-reading the file in binary mode and converting back to CRLF explicitly.
  Worth remembering for next time: when editing a CRLF file with Python,
  either edit as bytes throughout, or open text-mode with `newline=''` on
  *both* the read and the write to preserve whatever line endings were
  already there.

### Fixed "dump" command output truncating/corrupting on larger sizes - a regression from the console-routing fix

- User reported the "dump" command's output looked truncated with larger byte
  counts (their own example: 500 bytes), both over Telnet and, once checked
  directly, over the serial console too. Asked to find a good middle-ground
  Telnet TX buffer size by testing empirically with `dump` (size fully
  user-controlled) and `netinfo`.
- Buffer-size sweep (`TCPIP_TELNET_SKT_TX_BUFF_SIZE`, MCC field, was `0` =
  framework default) against `dump 200/500/800` and `netinfo` over a real
  Telnet session: 0 truncated even 200 bytes; 2048 covered 200 but not
  500/800; 3072 covered 200 and the user's own 500-byte case completely, not
  800; 4096 covered all three but left only ~720 bytes as the largest free
  TCP/IP heap block after a connect/dump/disconnect cycle (measured with a
  consistent methodology: same test script, `meminfo` right after) - too
  tight given this project's own history of heap-exhaustion bugs. **Settled
  on 3072** as the reasonable middle ground (dump up to ~500 bytes and every
  normal command fits; heap headroom stays meaningfully better than at 4096).
  Separately noted, independent of buffer size: heap free after one Telnet
  connect/dump/disconnect cycle dropped from ~17 KB (fresh boot) to ~3.8 KB
  and stayed fragmented (largest block only ~1.6 KB even though ~3.8 KB was
  nominally free) - flagged as a follow-up, not investigated further here.
- **While reproducing this, found the real bug was a self-inflicted
  regression, not (only) a buffer-size question.** A raw serial-console dump
  of 500 bytes (`cli.bat --read 3 "dump 0x20000000 500"`) showed the SAME
  problem, but worse than truncation: the output turned into **garbled,
  interleaved bytes** partway through (`"...2002020020020200202..."`), not a
  clean cutoff. Traced it to the earlier Telnet console-routing fix
  (`CMD_PRINT`, same session, `9b8aa63`): splitting `DumpMem()` into a
  `pCmdIO`-aware `CmdDumpMem()` for the actual "dump" command dropped its
  flow control entirely - the original `DumpMem()`'s
  `SYS_CONSOLE_WriteFreeBufferCountGet()` busy-wait (needed because
  `SERCOM1_USART_Write()`, `plib_sercom1_usart.c`, silently drops whatever
  does not fit in the 1024-byte serial TX ring buffer once it is full,
  returning fewer bytes than requested with nobody checking) never made it
  into the new function. `CmdDumpMem()` printed lines back-to-back with zero
  pacing, at CPU speed, far outrunning both the serial UART (115200 baud)
  and, over Telnet, `F_Telnet_MSG()` (same fire-and-forget pattern -
  `NET_PRES_SocketWrite()`'s return value is discarded too, see
  `telnet.c`).
- **Fix**: `SYS_CONSOLE_WriteFreeBufferCountGet()` is serial-specific and the
  generic `SYS_CMD_API` (`pCmdIO`) that also has to serve a Telnet
  connection has no equivalent "how much room is left" query for either
  transport - so instead of measuring free space, added a fixed per-line
  pacing delay (`app_wait_ms()`, same shape as `noip_test.c`'s
  `noip_wait_ms()`) after each `CMD_PRINT()` call in `CmdDumpMem()`. Started
  at 3 ms - still corrupted, just later (line ~23 instead of ~13) - so it
  was a margin problem, not a structural one; **10 ms** eliminated the
  corruption entirely, both over serial and Telnet.
- **Verified**: serial `dump 0x20000000 800` now ends cleanly and completely
  at `20000310: ... (800 bytes exactly)`, no corruption; `dump 500` ditto.
  Over Telnet with the 10 ms fix in place, `dump 500` (fits the 3072-byte
  buffer) arrives complete, and `dump 800` (does not fit) now truncates
  **cleanly** at the buffer boundary instead of garbling - confirming the
  corruption was specifically the missing pacing, and the remaining
  buffer-size-driven truncation for very large requests is the separate,
  already-understood, lower-severity limitation from the buffer-size sweep
  above.

### Replaced the fixed 10ms dump pacing with the original precise busy-wait

- User pushed back on the fixed-delay fix above: the sister project's
  original `SYS_CONSOLE_WriteFreeBufferCountGet()` busy-wait was already
  proven to work - why not use it instead of a blind per-line sleep that
  penalizes every dump, including ones far too small to ever need it?
- Realized `CmdDumpMem()` doesn't need to detect which transport `pCmdIO`
  resolves to at all: the busy-wait only ever blocks on the **serial**
  console's own ring buffer, which is idle whenever nothing else is
  currently printing to it. For a Telnet-issued dump that check reports
  "plenty of room" almost immediately in practice (nothing else is usually
  writing to the serial port at the same time), so it adds no real delay
  there; for a serial-issued dump it throttles exactly as precisely and
  load-adaptively as it always did. Reverted `CmdDumpMem()` to reuse
  `DumpMem()`'s exact original busy-wait (`SYS_CONSOLE_WriteFreeBufferCountGet(...)
  < pos`) unconditionally, removed the fixed-delay helper (`app_wait_ms()`)
  entirely - no longer needed anywhere.
- **Verified**: serial `dump 0x20000000 800` still ends cleanly and
  completely at `20000310: ...`, no corruption; the same Telnet sweep
  (200/500/800 bytes + `netinfo`) still shows the identical, correct results
  as with the fixed-delay version - `dump 500` complete, `dump 800` cleanly
  truncated at the 3072-byte Telnet buffer boundary, `netinfo` complete -
  now without any artificial per-line delay on small/Telnet dumps.

### Real Telnet-side backpressure in `F_Telnet_MSG()`

- User asked whether Telnet genuinely has no way to check free TX buffer
  space (the excuse behind the previous fix, which only worked by borrowing
  the serial console's idle busy-wait). Checked: `NET_PRES_SocketWriteIsReady()`
  does exist and is already used elsewhere in `telnet.c` (login/banner code)
  - `F_Telnet_MSG()` (the command-output path) just never called it.
- First attempt: bounded busy-wait on `NET_PRES_SocketWriteIsReady()`
  (500ms), later also adding `NET_PRES_SocketFlush()` per the login code's
  pattern. Built, flashed, tested with `dump 800/2000/4000` over Telnet -
  **did not work**: 3075-3093 of ~4011 bytes needed, 6.6s instead of
  near-instant, `dump 4000` returned 0 bytes.
- Root-caused: in this bare-metal, single-superloop build, `SYS_CMD_Tasks()`
  - which runs the command handler that calls `F_Telnet_MSG()` - executes
  *before* `TCPIP_STACK_Task()`/`NET_PRES_Tasks()` in `SYS_Tasks()`
  (`config/default/tasks.c`). Nothing drains a Telnet socket's TX buffer
  until those two run, so a bare busy-wait (with or without a manual flush)
  just burns its timeout waiting on a drain that can never happen from
  inside it. This is the opposite of the UART case: SERCOM TX is drained by
  a hardware interrupt that keeps firing regardless of what the main loop is
  doing, which is exactly why the borrowed-serial-buffer trick worked and a
  plain socket busy-wait can't.
- User asked directly: would it help to call `SYS_Tasks()` itself during the
  wait? Answer: not the *whole* thing - that would recurse into
  `SYS_CMD_Tasks()` (the very frame already on the stack, with its own
  static parser state) and into `APP_Tasks()`, risking interleaved app-level
  state machine execution. But the two specific sub-calls that actually
  matter, `TCPIP_STACK_Task()` and `NET_PRES_Tasks()`, are never reachable
  from `F_Telnet_MSG()`'s own call chain (confirmed: that function is only
  ever reached via the `SYS_CMD_API` `.msg`/`.print` callback, i.e. only
  from `SYS_CMD_Tasks()`, a sibling of `TCPIP_STACK_Task()` in `SYS_Tasks()`,
  never nested inside it) - so calling just those two, out of turn, is safe.
- Added `APP_PumpNetworkStack()` (`app.c`/`app.h`) wrapping exactly those two
  calls (needs `sysObj`, hence a new `#include "definitions.h"` in `app.c`
  - the only file in this app that now includes it). `F_Telnet_MSG()` calls
  it from inside its busy-wait loop instead of just spinning.
- **Verified** (rebuilt, reflashed, retested with a longer-timeout test
  script since large dumps now legitimately take a couple of seconds):
  `dump 800` -> 4011 bytes in 1.81s, complete; `dump 2000` -> 9938 bytes in
  1.81s, complete; `dump 4000` -> 19813 bytes in 1.81s, complete; `dump 8000`
  -> 39554 bytes in 2.21s, complete; `netinfo` -> complete, 1.81s. All
  previously truncated at ~3072 bytes (the `TCPIP_TELNET_SKT_TX_BUFF_SIZE`
  limit); none truncate now, regardless of size, and latency stays flat
  (~1.8-2.2s) rather than growing with output size.
- New hand-patch, see `docs/mcc-generated-code-patches.md` item 8 and
  `patches/telnet.patch`.

### Follow-up: `F_Telnet_MSG()` still corrupted large dumps intermittently

- User asked for a `dump 0x20000000 32000` test. It "came in bursts" (expected)
  but the output "wasn't clean throughout - sometimes one line runs straight
  into the next with no line break". Reproduced: byte-level diff of the raw
  socket capture showed a line's ASCII tail cut short (e.g. 9 of 16 `.`
  characters) with the *next* line's `20000940:` address glued directly onto
  it - no `\n\r` between them at all, and total bytes received differed on
  every run (158020/158029/157993/158065/158212 across 5 runs).
- User also asked to cross-check with a `tshark` capture rather than trust the
  Python client alone. Captured `tcp port 23` during a run, extracted the
  server->client bytes via `tshark -r <pcap> -q -z "follow,tcp,raw,0"` (lines
  starting with a tab = server->client) and re-ran the same corruption check
  against the wire-level bytes: same banner/prompt content as the Python
  capture (no client-side reinterpretation happening), and that particular
  capture run happened to complete clean - consistent with a *timing race*,
  not a fixed size limit or a client-side artifact (a deterministic bug would
  reproduce identically every run; a race would not, and did not).
- Root cause: `F_Telnet_MSG()`'s pre-check, `NET_PRES_SocketWriteIsReady(tSkt,
  len, 0U) < len`, does not reliably predict what a single
  `NET_PRES_SocketWrite()` call actually accepts. The call was still made
  once with its return value discarded - same "fire and forget" bug this
  whole investigation started from on the serial side
  (`SERCOM1_USART_Write()`), just intermittent here instead of consistent.
- Fixed by looping on the real return value: write, and if it accepted fewer
  bytes than requested, pump the stack (`APP_PumpNetworkStack()`) and retry
  the remainder, bounded by the same 500ms per-message deadline as before.
- User also asked directly whether `CmdDumpMem()`'s own serial-buffer busy-wait
  (`SYS_CONSOLE_WriteFreeBufferCountGet(...) < pos`, `app.c`) should now be
  made to "work with both UART and Telnet" given the new fix. Answer: it
  already does, for two different reasons - it is the real throttle for a
  serial-issued dump, and a harmless near-instant no-op for a Telnet-issued
  one (Telnet's actual correctness now lives entirely in `F_Telnet_MSG()`).
  Updated that function's comment, which had gone stale - it previously
  described Telnet correctness as coming from sizing
  `TCPIP_TELNET_SKT_TX_BUFF_SIZE` to cover the reply, which stopped being true
  the moment `F_Telnet_MSG()` grew its own retry loop.
- **Verified:** `dump 0x20000000 32000` re-run 5x back-to-back, all 5 runs
  158065 bytes, zero glued/malformed lines - deterministic where it was
  variable before. `docs/mcc-generated-code-patches.md` item 8 and
  `patches/telnet.patch` updated to match the final code.

### New parallel GUI: `bridge_gui_telnet.py` connects over Telnet instead of UART

- User asked for a second GUI, parallel to `bridge_gui.py`, that talks to the
  board over the now-working Telnet server (TCP/23) instead of the EDBG COM
  port - same tabs/features, only the connection layer swapped, with
  IP/user/password fields (stored in a config file of its own) replacing the
  COM port picker. Explicit constraint: don't touch `bridge_gui.py` or any of
  its files.
- Implementation: copied `scripts/bridge_gui.py` to `scripts/bridge_gui_telnet.py`
  and replaced only the connection-specific parts - the serial `Link` class
  became `TelnetLink` (same `open()/write()/close()` interface, same
  `(port, "data"/"lost", payload)` queue protocol the rest of the GUI already
  consumes generically via `self.port_link`), the COM-port picker in the top
  bar became IP/User/Password entries, `get_available_com_ports()` and the
  `winreg` fallback were dropped (no longer reachable), and the class was
  renamed `BridgeGUITelnet`. The Flash/Erase/Select-Hex quick commands (SWD via
  pyOCD, independent of the CLI/terminal link either way) were left untouched.
  New dedicated config file `json/bridge_gui_telnet_config.json` (ip/telnet_user/
  telnet_password + the same bridge/values session-state shape as
  `bridge_config.json`, but never shared with it), defaults `192.168.0.12` /
  `admin` / `password` as given. New launcher `run_gui_telnet.bat`, parallel to
  `run_gui.bat`.
- `TelnetLink._login()` drives the Login:/Password: prompt from
  `library/tcpip/src/telnet.c` (`TELNET_START_MSG`/`TELNET_ASK_PASSWORD_MSG`/
  `TELNET_FAIL_LOGON_MSG`/`TELNET_LOGON_OK`): a line there ends on the first CR
  or LF in the buffer, so sending a whole `"user\r\n"`/`"pass\r\n"` at once is
  enough, no character-by-character sending needed for the login step itself.
- **Verified against the real board (192.168.0.12, admin/password):** a raw
  `TelnetLink.open()` + `write(b"stats\r")` smoke test (no GUI, `scripts/`
  directory) logged in, captured the welcome banner as the first queued
  "data" chunk, and returned the `stats` command's full echoed output
  correctly. A second run with a wrong password raised `PermissionError:
  Access denied - check user/password` as expected, confirming the failure
  path. `python -m py_compile scripts/bridge_gui_telnet.py` also passes, and
  a diff against `bridge_gui.py` shows changes confined to exactly the
  connection-layer edits described above - `bridge_gui.py`,
  `bridge_config.json`, `gui_term.py` and `run_gui.bat` are untouched
  (confirmed via `git status`).

---

### MCC Generate Code round-trip test (Telnet TX buffer) + apply_patches.py ordering bug

- Deliberate test to demonstrate the difference between a hand-edit to
  generated code (silently reverted) and an MCC-model-driven value (survives
  Generate Code): raised the Telnet Server component's "Default Socket TX
  Buffer Size" field from `3072` to `3200` in the MCC GUI, then ran Generate
  Code with Force to Update.
- **Confirmed:** `configuration.h`'s `TCPIP_TELNET_SKT_TX_BUFF_SIZE` came out
  as `3200`, proving the value now comes from the model, not a hand-edit that
  would have reverted to `0`.
- **Side effect, expected but worth recording precisely:** the same Generate
  Code run wiped all 6 documented hand-patches (`docs/mcc-generated-code-patches.md`)
  - confirmed via `patches/apply_patches.py --check` (all 6 `.patch` files plus
    both `stdarg.h` fixes reported missing). Notably this included the
    boot-critical DPLL sync-timeout fix (`plib_clock.c`, item 1) - the board
    would not have booted if flashed at that point.
- Ran `patches/apply_patches.py` (no `--check`) to reapply: 5 of 6 `.patch`
  files plus both `stdarg.h` fixes applied cleanly; `telnet.patch` reported
  `FAILED` ("neither applies cleanly nor is already applied").
- **Root-caused the `telnet.patch` failure:** not a real MCC content change -
  `main()` in `apply_patches.py` applied the 6 `.patch` files *before* the two
  `stdarg.h` fixes. `telnet.patch`'s first hunk sits immediately next to
  `telnet.c`'s `#include <stdarg.h>` line (inserts `sys_time.h` right after
  it), so when the recurring MCC `stdarg.h` generator bug (item 6) struck
  again on this same regenerate, the hunk's context no longer matched at the
  point the patch loop ran - even though the patch content itself was still
  correct. Manually re-applied `telnet.patch`'s three hunks by hand (verified
  identical to the patch file's content), then fixed the actual bug: reordered
  `apply_patches.py`'s `main()` to run the `stdarg.h` fixes first, before the
  `.patch` file loop. Re-verified with `--check`: all 8 entries `OK`.
- Net result after the full round-trip: only two real deltas remain vs. the
  prior commit - `TCPIP_TELNET_SKT_TX_BUFF_SIZE` `3072` → `3200`, and the
  `apply_patches.py` ordering fix. All 6 hand-patched files matched their
  prior committed content exactly once re-applied (confirmed via `git status`
  showing no diff on any of them).

---

### Ported setup.bat/install.bat from the sister project (own .venv)

- User asked for the sister project's `setup.bat`/`install.bat` machine-setup
  mechanism, closing the fragility gap noted in `CLAUDE.md` section 2 (this
  project's scripts pointed at the sister project's `.venv` directly).
- Scoping questions resolved before porting: own independent `.venv` (not
  just a fallback repair path), and mirror all of the sister's setup steps
  1:1 rather than a reduced subset.
- Investigated actual file dependencies before copying anything: this
  project's `flash_same54.py` already exposes the same `BENCH_PATH`/
  `find_pack_dir`/`load_bench`/`save_bench` symbols the sister's
  `install_prereqs.py` imports, and needs the same three packages
  (`pyserial`, `pyocd`, `sv-ttk`) per an import scan of `scripts/*.py` -
  `setup_venv.bat`, `install_prereqs.py`, `setup_debug.py`,
  `requirements.txt` copy over verbatim, no project-specific references in
  any of them.
- Found one step with no purpose here: the sister's `setup_compiler.py`/
  `setup_compiler.config` only feeds `build_summary.py`'s post-build
  `xc32-nm` step, which this project doesn't have - dropped from `setup.bat`
  (now 4 steps instead of 5) rather than ported as dead configuration.
- `genmk.bat` (headless `nbproject\Makefile-*.mk` generation) - ported and
  tested live against this project, since the project-independent MCC/Harmony
  knowledge base had a *negative* finding from 2026-08-07 (headless
  `prjMakefilesGenerator` never worked, only "open once in the IDE" did).
  Backed up the existing IDE-generated `Makefile-*.mk` first. First run:
  `rc=0`, no error, but wrong result - the ported version-selection used a
  hardcoded MPLAB X version list (`v6.25 v6.20 ...`) that doesn't include
  `v6.35`, which is what's actually installed here alongside `v6.25` and what
  the IDE itself uses. It silently picked `v6.25` instead:
  `Makefile-local-default.mk` came out pointing at a different DFP pack path
  and Java version than the real IDE build - wrong in exactly the way the
  script's own header comment warns about for `xc32-bin2hex`, one level up.
  Restored the backup, fixed `genmk.bat` to use the same dynamic
  newest-first directory scan `build.bat` already uses for `make.exe`
  (`dir /b /ad /o-n ...\MPLABX\v*`) instead of the hardcoded list, re-ran:
  picked `v6.35` correctly, `Makefile-local-default.mk` came out
  byte-identical to the backed-up IDE original, `Makefile-default.mk`
  differed only in per-file flag-hash suffixes (same harmless noise pattern
  as the MCC Generate Code diffs earlier this session) - no path/compiler
  difference left. Verified via diff against the backup, not via an actual
  build (per the "user builds in MPLAB X" rule).
- Documented the genmk.bat finding in both this project's `CLAUDE.md` and
  the central, project-independent MCC-Harmony knowledge file (a hardcoded
  MPLAB X version list anywhere is a latent bug the moment a newer version
  is installed alongside an older one) - it's a generic lesson, not specific
  to this project.
- Repointed `flash.bat`, `cli.bat`, `run_gui.bat`, `run_gui_telnet.bat`,
  `run_term.bat`, `patches/apply_patches.bat`, and `scripts/dep_check.py`'s
  fallback-repair path from the sister project's hardcoded `.venv` path to
  this project's own (`%~dp0.venv` / `Path(__file__)`-relative). Added
  `.venv/` to this project's `.gitignore` (matches the sister project's
  convention; `json/bench.json`/`json/term_ports.json` were already
  ignored).
- Deliberately did not run `setup.bat` itself (creates a real `.venv`,
  installs packages from the network, and asks for probe selection against
  real hardware) - left for the user to run.

---

<!-- Append new dated entries above this line as work continues. -->
