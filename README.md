# tcpip_iperf_lan865x — T1S ↔ 100BASE-T Bridge

A **10BASE-T1S ↔ 100BASE-T Layer-2 bridge** firmware for the ATSAME54P20A. It
bridges a 10BASE-T1S segment onto ordinary Fast Ethernet, so any device on
the T1S side becomes reachable — and reachable *from* — a normal IP network,
exactly as if it were plugged into a regular Ethernet switch. On-board
diagnostics cover the bridge itself (packet mirroring, register access, PLCA
control, a raw-Ethernet loopback test, a TCP echo test server, a built-in
`iperf` throughput tester) plus **persistent network/PLCA configuration** on
an Emulated EEPROM (§6.2), all reachable over **two independent consoles at
once** — the EDBG serial port and a Telnet server on the 100BASE-T side (§10).

Sister firmware to `t1s_100baset_bridge`: same ATSAME54P20A, same LAN865x
(`eth0`, T1S) driven the same way, same core module set (`env.c`,
`lan865x_diag.c`, `noip_test.c`, `port_mirror.c`) — but not identical. This
project's `eth1` (100BASE-T) is driven by an external **LAN8742A** PHY over
the SAM E54's internal GMAC instead of the sister's LAN8740A, it adds a
**Telnet console** and a **TCP echo test server** (`testserver`) neither of
which the sister has, and its hand-patches to MCC-generated code are tracked
by a small automated tool (`patches\apply_patches.py`, §5.5) rather than
reapplied by hand.

---

## Contents

- [1. What this firmware is for](#1-what-this-firmware-is-for)
- [2. Features](#2-features)
- [3. Hardware setup](#3-hardware-setup)
- [4. Firmware architecture](#4-firmware-architecture)
- [5. Building it yourself](#5-building-it-yourself)
- [6. Changing IP and PLCA configuration](#6-changing-ip-and-plca-configuration)
- [7. Port mirror and sniffer: capturing the T1S bus in Wireshark](#7-port-mirror-and-sniffer-capturing-the-t1s-bus-in-wireshark)
- [8. Throughput testing](#8-throughput-testing)
- [9. Transmitter test modes and PLCA diagnostics](#9-transmitter-test-modes-and-plca-diagnostics)
- [10. Telnet console (TCP/23)](#10-telnet-console-tcp23)

---

## 1. What this firmware is for

The board sits between two worlds:

```
   PC / lab network / internet     Bridge (this firmware)            T1S bus
   100BASE-T (RJ45)          ATSAME54P20A + LAN865x + LAN8742A   10BASE-T1S (2-wire)
   ┌──────────────┐  100M    ┌───────────────────────────┐  T1S   ┌──────────────┐
   │  Wireshark   │◄────────►│ eth1 (GMAC)   eth0 (LAN865x)│◄──────►│  any T1S     │
   │  ping/telnet │.12/.11   │   └── MAC bridge (L2) ──┘   │ PLCA   │  node(s)     │
   └──────────────┘          └───────────────────────────┘ node 5  └──────────────┘
```

It does two jobs:

**a) Transparent L2 bridge.** The two interfaces — `eth0` (the T1S MAC-PHY)
and `eth1` (100BASE-T) — are joined by the Harmony **MAC bridge**, so
traffic from the 100BASE-T side (ARP, ICMP/ping, ordinary IP traffic) flows
through to any node on the T1S segment and back, with MAC learning (FDB).
From a PC on the 100BASE-T side you can simply `ping <t1s-node-ip>` and reach
it *through* the bridge as if it were on the local Ethernet. The bridge does
**not** forward manually in application code — the Harmony MAC bridge
handles all L2 forwarding in both directions.

**b) T1S bus analyzer / SPAN port.** The firmware can mirror T1S traffic onto
`eth1` so you can capture the two-wire bus in **Wireshark** on the PC —
including replies from the T1S side *and* the bridge's own requests (`mirror`
command), or every frame on the bus regardless of source (`sniffer`
command). It also has raw frame dump/logging (`ipdump`, `logstat`), a
raw-Ethernet loopback test (`noip_send`), LAN865x register peek/poke
(`lan_read`/`lan_write`), PLCA node-ID control and bus-health counters
(`plca_stat`, `sqi`), and per-interface counters (`stats`).

---

## 2. Features

### Bridging (core function)

- Transparent 10BASE-T1S ↔ 100BASE-T Layer-2 bridge on the ATSAME54P20A:
  `eth0` (LAN8651 T1S MAC-PHY, SPI) and `eth1` (GMAC + LAN8742A, RMII) joined
  into one L2 segment.
- Hardware/stack-level forwarding via the Harmony MAC bridge, with MAC
  learning — no manual forwarding in application code.
- Bidirectional, protocol-agnostic: ARP, ICMP, arbitrary IP traffic pass both
  ways; a T1S node is reachable from the LAN exactly as if plugged into an
  Ethernet switch.

### Diagnostics and bus analysis

- **Port mirror / SPAN** (`mirror [0|1]`, `sniffer [0|1]`, `span` command
  group): copies T1S traffic to `eth1` for Wireshark — see [§7](#7-port-mirror-and-sniffer-capturing-the-t1s-bus-in-wireshark)
  for the full mechanism and the difference between the two commands.
- **Raw-frame test** (`noip_send <n> [gap_ms]`, `noip_stat`, `noip` group):
  deterministic EtherType `0x88B5` frames that bypass the TCP/IP stack — a
  reproducible source for oscilloscope captures, independent of any IP
  configuration.
- **Oversized-frame test** (`bigframe <total_len>`, `span` group): sends one
  raw, oversized frame straight out `eth1` — a targeted tool for exercising
  the mirror path's frame-length handling.
- **Packet logging** (`ipdump [0..3]`, `logstat`, `logclear`, `Test` group):
  deferred ring-buffer RX dump per interface.
- **Counters and memory** (`stats`, `meminfo`, `dump`/`peek`/`poke <addr>`,
  `uptime`, `Test` group): per-interface TX/RX counters, C-runtime **and**
  TCP/IP heap figures, raw memory access, and time since boot/last reset.
- **Build identification** (`timestamp`): the only way to tell from the
  outside which image is running.
- **On-device command discovery** (`help`, `lanhelp`): the firmware lists its
  own commands per group.

### LAN865x registers, IEEE test modes and PLCA (`lan865x_diag.c`, `lan` group)

- Generic register peek/poke (`lan_read`, `lan_write`) across all MMS banks.
- Read-modify-write with masked verify (`lan_rmw <addr> <mask> <val>`), for
  registers where several control bits share one word.
- IEEE 802.3-2022 §147.5.2 transmitter test modes (`testmode [0..4]
  [seconds]`) — see [§9](#9-transmitter-test-modes-and-plca-diagnostics).
- PLCA node-ID control (`plca_node [id]`, 0 = coordinator) and two bus-health
  diagnostics beyond plain register access: `plca_stat` (link/node/timeout+
  beacon counters and events below IP-frame level) and `sqi` (continuous
  Signal Quality Indicator, per node or all, with an optional periodic
  report) — see [§9](#9-transmitter-test-modes-and-plca-diagnostics).

### Persistent configuration (`env` group, Emulated EEPROM)

- Versioned, CRC-protected record in the last 16 KB of flash: per-interface
  IP/mask/gateway/DNS, both MACs, PLCA node id/count, and the mirror/sniffer
  boot state.
- CLI-editable, no rebuild (`showenv`, `setenv`, `saveenv`, `readenv`,
  `resetenv`) — IP and PLCA apply live, MAC at next reset.
- Loaded before the stack: `ENV_Init()` runs ahead of `TCPIP_STACK_Init()`,
  so a persisted MAC is in effect when interfaces bind.
- Per-board unique MAC derived from the SAME54's serial number (`eth1` = eth0
  with the low byte incremented), seeded on first boot from the compiled
  `configuration.h` defaults — one firmware image, distinct boards.

### Throughput testing

- Built-in iperf2-compatible tester (`iperf`, `iperfk`, `iperfi`, `iperfs`,
  `iperf` group): TCP/UDP, server or client — measures end-to-end throughput
  across the bridge path.
- **TCP echo test server** (`testserver [start [port]|stop]`, `testserver`
  group) — a small polled state machine on port `5566` by default, echoing
  received bytes back in fixed chunks, for bandwidth-ramp testing distinct
  from `iperf`. See [§8](#8-throughput-testing).

### Telnet console (unique to this project — see §10)

- A second, independent CLI console over TCP/23, alongside the EDBG serial
  port — same command set, same groups, its own login.
- A parallel GUI tool (`run_gui_telnet.bat` / `scripts\bridge_gui_telnet.py`)
  connects over Telnet instead of the COM port.

### Host-side tooling and build system

- `build.bat` / `flash.bat` / `setup.bat`: MPLAB-X-Makefile build wrapper
  plus a pyOCD flasher with probe auto-detect — see [§5](#5-building-it-yourself).
- `cli.py` / `cli.bat` — send CLI commands and collect answers over the EDBG
  COM port (115200 8N1).
- `run_gui.bat` / `run_gui_telnet.bat` — status/configuration GUIs, one per
  console transport.
- `run_term.bat` — three serial consoles (this bridge plus two T1S follower
  boards on this bench) in one window.
- `scripts\iperf_matrix_test.py` — an `iperf` throughput matrix across PC,
  bridge and two T1S follower nodes (UDP max-rate search plus TCP, both
  directions per pair); always reads UDP loss from the *receiving* side, not
  the embedded client's own report.
- `scripts\sniffer_capture_test.py` — validates the `sniffer` mirror path:
  drives real UDP/TCP traffic between two T1S follower nodes while `tshark`
  captures on the PC, and checks the capture for completeness/truncation.
- `patches\apply_patches.py` — reapplies this project's hand-patches to
  MCC-generated code after a `Generate Code` run; see [§5.5](#55-after-editing-in-mcc-reapplying-hand-patches).

---

## 3. Hardware setup

Built on a Microchip SAM E54 host with the T1S side on the **MIKROE-5543
Two-Wire ETH Click (LAN8651)** — same board population as the sister
project's `eth0`. `eth1` is driven by the SAM E54's internal **GMAC** over
RMII; the MCC PHY driver component selected for it is **LAN8742A**, but the
physical daughter-card actually fitted on this bench is the same
**AC320004-3** board the sister project uses (LAN8740A) — confirmed by
flashing the sister's own firmware onto this board's `eth1` and seeing it
come up immediately, no hardware change. See
[`docs/how-to-bridge.md`](docs/how-to-bridge.md) for the MCC-level GMAC/PHY
configuration steps actually performed, and
[`docs/bridge-configuration-manual.md`](docs/bridge-configuration-manual.md)
(a work-in-progress skeleton) for where a full hardware write-up is intended
to land.

![The assembled bridge board: the SAM E54 Curiosity Ultra host (red board) with the green Two-Wire ETH Click (10BASE-T1S MAC-PHY, plugged into the "X32" header, top left) for eth0, and the LAN8740A PHY Daughter Board (AC320004-3, bottom left, with the RJ45 jack) for eth1 — a separate plug-in module, not part of the Curiosity board itself.](docs/images/eval-board-sam-e54-curiosity-t1s-click.jpg)

### Bridge board: bill of materials

| Part | What it is | Microchip order number |
|---|---|---|
| **MCU host** (Cortex-M4F, runs this firmware) | SAM E54 Curiosity (Ultra) board | **DM320210** |
| **100BASE-T PHY** for `eth1` (plugs into the board's PHY daughter-card header) | LAN8740A PHY Daughter Board | **AC320004-3** |
| **10BASE-T1S MAC-PHY** for `eth0` (SPI ↔ two-wire bus) | MikroElektronika Two-Wire ETH Click (LAN8651) | **MIKROE-5543** |

> **Other PHY/switch daughter boards fit the same connector but need a
> different MCC-selected driver, not just a config edit** — the
> `AC320004-x` series covers several interchangeable-connector options:
>
> | Order number | Chip | Type | Notable features |
> |---|---|---|---|
> | **AC320004-3** | LAN8740A | single-port 10/100 PHY | HP Auto-MDIX, flexPWR low-power modes — the one fitted here |
> | AC320004-4 | LAN9303 | 3-port managed switch (2 external ports + 1 RMII/MII host port) | integrated switch fabric, VLAN/QoS |
> | AC320004-5 | KSZ8041 | single-port 10/100 PHY | RMII or MII selectable |
> | AC320004-6 | KSZ8061 | single-port 10/100 PHY | small QFN package, low power — Microchip's more commonly bundled option for this header, easy to grab by mistake |
> | AC320004-7 | KSZ8863 | 3-port managed switch (2 integrated PHYs + 1 RMII/MII host port) | VLAN support, integrated switch fabric |
>
> Swapping to any of these requires selecting the matching Harmony PHY
> driver component in MCC and regenerating — see the `DRV_ETHPHY_LAN8740`/
> `drv_extphy_lan8740.c` note in the sister project for what that entails.

### How `eth0` (LAN865x) is wired

| Signal | SAM E54 pin | Notes |
|---|---|---|
| Chip select (CS) | **PC15** | `DRV_LAN865X_SPI_CS_IDX0` |
| Interrupt (INT) | **PC14** | `DRV_LAN865X_INTERRUPT_PIN_IDX0` |

### Network and addressing (default)

| Interface | Role | IP | Mask | PLCA |
|---|---|---|---|---|
| `eth0` | T1S (LAN865x) | **192.168.0.11** | /24 | node id **5**, node count **8** |
| `eth1` | 100BASE-T (GMAC+LAN8742A) | **192.168.0.12** | /24 | — |
| T1S node (example) | *any device* | e.g. `192.168.0.54` | /24 | follower |

Both bridge interfaces share one `192.168.0.0/24` subnet — the MAC bridge
makes that a single L2 segment. Put the PC's RJ45 adapter on the same
subnet, on an address other than `.11`/`.12`/whatever the T1S node(s) use.

> **Known quirk, not silently corrected:** `configuration.h`'s
> `TCPIP_NETWORK_DEFAULT_GATEWAY_IDX1` macro is literally `"192.168.0..1"`
> (a double dot) in the generated source. It is a pre-existing typo in this
> MCC field, not a copy error in this README — worth fixing via MCC if it
> ever matters, since a hand-edit to that generated file would not survive
> the next `Generate Code`.

> **PLCA coordinator.** If the T1S side is meant to run with this board as
> coordinator, set the node id to **0** (`plca_node 0` at runtime, or
> `DRV_LAN865X_PLCA_NODE_ID_IDX0` in `configuration.h` for a persistent
> change) — see [§6](#6-changing-ip-and-plca-configuration).

### Host PC: giving the `eth1` adapter a static address

Both bridge interfaces are compiled as `TCPIP_NETWORK_CONFIG_IP_STATIC`, so
they hold `.11`/`.12` for as long as the board runs. The PC's adapter needs a
manually assigned address in `192.168.0.0/24` that is not already taken
(e.g. `192.168.0.220`).

```bat
rem Windows, in an administrator console:
netsh interface ip set address name="Ethernet 8" static 192.168.0.220 255.255.255.0
```

```sh
# Linux
sudo ip addr add 192.168.0.220/24 dev eth0     # adapter name from `ip link`
```

Verify with two pings, each with its own diagnostic meaning:

```sh
ping 192.168.0.12     # eth1 (GMAC/LAN8742A) answers -> cable and 100BASE-T link are up
ping 192.168.0.11     # eth0 (LAN865x) answers -> the bridge really forwards to the T1S side
```

### Console and cabling

1. **Debugger + console:** one USB cable from the PC to the SAM E54 board's
   embedded-debugger USB port — both the programmer and the virtual COM port
   for the serial CLI (**115200 8N1**).
2. **100BASE-T:** the RJ45 on the LAN8742A side ↔ the PC's Ethernet adapter.
3. **T1S:** the two-wire bus from the LAN865x Click to whatever node(s) sit
   on the T1S segment.
4. **Telnet (optional, no extra cable):** once `eth1` is up, the same CLI is
   also reachable over `telnet 192.168.0.12` — see [§10](#10-telnet-console-tcp23).

---

## 4. Firmware architecture

Built on **MPLAB Harmony 3** for the ATSAME54P20A. Single-threaded
cooperative superloop (`SYS_Tasks()`); no RTOS, no threads, no locks.

```
                       ┌──────────────────────────────────────────────┐
   serial CLI ───────► │ SYS_CMD console groups: Test / env / span /   │
   (EDBG COM)          │ noip / lan / iperf / testserver               │
   Telnet CLI  ───────►│   (app.c / env.c / lan865x_diag / port_mirror │
   (TCP/23)            │    / noip_test / testserver)                 │
                       ├──────────────────────────────────────────────┤
   T1S bus  ◄──────────┤ eth0: DRV_LAN865X ┐                          │
                       │                   ├─ TCPIP MAC bridge (L2) ─┐ │
   100BASE-T ◄─────────┤ eth1: GMAC+LAN8742A┘  + Harmony TCP/IP stack │ │
                       ├──────────────────────────────────────────────┤
                       │ Emulated EEPROM (last 16 KB flash) — env.c   │
                       └──────────────────────────────────────────────┘
```

### The bridge data path

- `TCPIP_STACK_USE_MAC_BRIDGE` is enabled with both interfaces added
  (`TCPIP_NETWORK_MACBRIDGE_ADD_IDXn`, an MCC per-interface checkbox — see
  `docs/how-to-bridge.md` §4.5). **The MAC bridge does all L2 forwarding**
  between T1S and 100BASE-T — there is no manual forwarding code in the
  application.

### Application modules

- **`app.c`** — the application state machine (`INIT → WAIT →
  SERVICE_TASKS → IDLE`), the `Test` command group, and the Telnet
  authentication handler (§10). `APP_STATE_SERVICE_TASKS` is where
  everything that needs a running TCP/IP stack (mirror init, Telnet auth
  registration, `env_apply()`) is deliberately deferred to — calling any of
  it from `APP_Initialize()` runs into heap/init-timing bugs that were
  root-caused and fixed during this project's bring-up (see
  `docs/session-log.md`).
- **`env.c`** — persistent config on the Emulated EEPROM, the `env` command
  group. See [§6](#6-changing-ip-and-plca-configuration).
- **`lan865x_diag.c`** — LAN865x register access, IEEE test modes, PLCA
  control and bus-health diagnostics, the `lan` command group. See
  [§9](#9-transmitter-test-modes-and-plca-diagnostics).
- **`port_mirror.c`** — the `eth0` → `eth1` SPAN/sniffer mechanism, the
  `span` command group. See [§7](#7-port-mirror-and-sniffer-capturing-the-t1s-bus-in-wireshark).
- **`noip_test.c`** — raw EtherType-0x88B5 loopback test, the `noip`
  command group.
- **`testserver.c`** — a small polled TCP echo server (no signal handlers,
  by design) for bandwidth-ramp testing, the `testserver` command group.
  See [§8](#8-throughput-testing).
- **`cmd_print.h`** — `CMD_PRINT`/`CMD_MSG`/`CMD_PRINT_OR_CONSOLE` wrappers
  around a command's own `pCmdIO`, used by every module above so a
  command's reply goes back over whichever console (serial or Telnet) asked
  for it, not always to the serial port.

### CLI commands

Six command groups; type the command name directly (no group prefix
needed). Full descriptions are each command's own `help`/`<group>help`
output on the device — this is the reference.

**`Test` group** (`app.c`):

| Command | Description |
|---|---|
| `help` | show Test group commands |
| `timestamp` | show build timestamp |
| `uptime` | time since boot/last reset (d hh:mm:ss) |
| `ipdump [0..3]` | dump RX frames (0=off, 1=eth0, 2=eth1, 3=both) |
| `stats` | TX/RX counters for eth0 and eth1 |
| `meminfo` | free memory: C-runtime heap and TCP/IP heap |
| `dump <addr_hex> <count>` | memory dump (hex) |
| `peek <addr_hex> [size=1\|2\|4]` | read a single value |
| `poke <addr_hex> <value_hex> [size=1\|2\|4]` | write a single value |
| `logclear` / `logstat` | clear / show deferred packet-log statistics |

**`env` group** (`env.c`) — persistent config on the Emulated EEPROM:

| Command | Description |
|---|---|
| `showenv` | show the current network config (RAM shadow) |
| `setenv <key> <val>` | edit the RAM shadow — keys: `ip0/mask0/gw0/dns0`, `ip1/…`, `mac0`/`mac1`, `plca_id`/`plca_cnt`, `mirror`, `sniffer` |
| `saveenv` | persist to EEPROM and apply it live |
| `readenv` | reload from EEPROM and apply (discard unsaved edits) |
| `resetenv` | restore compiled defaults, persist and apply |

**`lan` group** (`lan865x_diag.c`) — registers, test modes, PLCA:

| Command | Description |
|---|---|
| `lanhelp` | list these commands |
| `lan_read <addr_hex>` / `lan_write <addr_hex> <value_hex>` | LAN865X register access |
| `lan_rmw <addr> <mask> <value>` | read-modify-write + verify |
| `testmode [0..4] [seconds]` | IEEE transmitter test mode, no arg = show current |
| `plca_node [id]` | get/set PLCA node ID (0 = coordinator), no arg = show current |
| `plca_stat` | PLCA bus health below IP-frame level (link/nodes/timeout+beacon counts/events) |
| `sqi [node\|all\|off]`, `sqi report <sec>\|off` | continuous Signal Quality Indicator, no arg = show current report |

**`noip` group** (`noip_test.c`) — raw EtherType-0x88B5 loopback:

| Command | Description |
|---|---|
| `noip_send <n> [gap_ms]` | send `n` raw frames bypassing the TCP/IP stack |
| `noip_stat` | TX/RX counters, independent of any protocol state |

**`span` group** (`port_mirror.c`) — the eth0 → eth1 mirror:

| Command | Description |
|---|---|
| `mirror [0\|1]` | mirror eth0(T1S) RX **and** the bridge's own TX to eth1 for Wireshark |
| `sniffer [0\|1]` | mirror **all** eth0(T1S) RX to eth1, not just this bridge's own traffic |
| `bigframe <total_len>` | send one raw, oversized frame straight out eth1 |

**`iperf` group** — Harmony's built-in throughput tester:

| Command | Description |
|---|---|
| `iperf [options]` | start a throughput test session (server or client) |
| `iperfk` | stop the running session |
| `iperfi <address>` | bind the test to a specific local interface |
| `iperfs <tx\|rx> <bytes>` | set the TX/RX buffer size |

**`testserver` group** (`testserver.c`) — TCP echo server:

| Command | Description |
|---|---|
| `testserver [start [port]\|stop]` | start/stop a TCP echo server (default port 5566) for bandwidth-ramp testing |

Harmony stack commands (`netinfo`, `bridge`, `ping`, `setip`, `setgw`, etc.)
are also available.

---

## 5. Building it yourself

Plain **MPLAB X** project (no CMake/Ninja) with a thin shell wrapper around
MPLAB X's own build, plus a **pyOCD**-based flash tool (no MDB/MPLAB X
needed just to program the board).

### 5.1 Tool prerequisites (per machine)

| Requirement | Notes |
|---|---|
| **MPLAB X IDE** | must be **installed** (its `make` and `prjMakefilesGenerator` are used) and supplies the SAME54_DFP device pack — never has to stay open |
| **MPLAB XC32** | see `setup_compiler.config` in the sister project's convention; this project resolves the compiler the same way `build.bat` finds `make.exe` — no separate compiler-selection step here (see §5.2) |
| **Python 3.9+** | `pyserial`/`pyocd`/`sv-ttk`, installed by `setup.bat` into this project's own `.venv` |
| **Terminal** | the board's EDBG virtual COM port, 115200 8N1, **or** `telnet 192.168.0.12` once the board is up (§10) |

### 5.2 One-time setup after cloning

```bat
setup.bat
```

Runs four independent steps (a failure in one is reported but does not
abort the rest): a Python virtual environment at `.venv\` with
`pyserial`/`pyocd`/`sv-ttk` installed into it (`batch\setup_venv.bat` —
never the machine's global Python), pyOCD + probe/pack check
(`install.bat --install`), the SAME54_DFP VS Code debug fix
(`scripts\setup_debug.py`), and the project Makefiles (`batch\genmk.bat`).
There is no compiler-selection step here, unlike the sister project's
`setup.bat` — its purpose there is feeding a `build_summary.py` post-build
step this project doesn't have.

**No IDE session is strictly needed for the Makefiles.** A fresh checkout
has no `nbproject\Makefile-*.mk` fragments (gitignored — they hold absolute
paths of the machine that generated them); `batch\genmk.bat` generates them
from the tracked `nbproject\configurations.xml` via MPLAB X's own
`prjMakefilesGenerator.bat`. `build.bat` calls it automatically if the
fragments are missing.

### 5.3 Build and flash

```bat
build.bat            :: incremental build (build.bat rebuild = clean, build.bat clean)
flash.bat             :: program the board via pyOCD and release it from reset
flash.bat --list      :: list connected probes
```

Like the sister project, `build.bat` copies the resulting HEX into a
tracked **`release\bridge_lan865x_100baseT.hex`** after every successful
build, so a fresh clone can flash without building first — `flash.bat`
defaults to exactly that file. Only `build.bat` refreshes `release\`; a
build done from inside the MPLAB X IDE leaves it stale. To flash a fresh
local build instead, pass the `dist\` path explicitly:

```bat
flash.bat firmware\tcpip_iperf_lan865x.X\dist\default\production\tcpip_iperf_lan865x.X.production.hex
```

### 5.4 Everyday CLI / GUI access

```bat
cli.bat "stats" "netinfo"                    :: ad-hoc commands over the EDBG COM port
cli.bat --port COM8 --read 3 "reset"
run_gui.bat                                   :: status/config GUI over the COM port
run_gui_telnet.bat                            :: same GUI, over Telnet (TCP/23) instead
run_term.bat                                   :: this board + two T1S follower boards, one window
```

![Bridge Status & Configuration GUI (`run_gui.bat`/`run_gui_telnet.bat`): the Bridge Parameters tab, showing per-interface IP/mask/gateway/DNS/MAC, PLCA node id/count, and the Quick Commands panel (Environment read/write, Mirror/Sniffer toggles, stats, flashing).](docs/images/gui-bridge-parameters-tab-overview.png)

![`run_term.bat`: three serial consoles in one window — this bridge (COM8) plus two T1S follower boards (COM10 = node A/201, COM23 = node B/202) — here running `plca_node`/`plca_stat` on each to compare configured vs. observed PLCA node count.](docs/images/gui-telnet-three-terminals-plca-status.png)

### 5.5 After editing in MCC: reapplying hand-patches

A handful of files under `firmware\src\config\default\` carry small,
documented hand-patches (silicon errata workaround, a driver race fix,
Telnet backpressure, and others — full list and rationale in
[`docs/mcc-generated-code-patches.md`](docs/mcc-generated-code-patches.md)).
Every `Generate Code` run in MCC silently reverts them. After any MCC
change:

```bat
python patches\apply_patches.py --check    :: dry run - report only
python patches\apply_patches.py            :: apply whatever is missing
```

See [`patches/README.md`](patches/README.md) for how it works and how to
regenerate a patch file if a hand-patch itself changes.

---

## 6. Changing IP and PLCA configuration

The IP addresses (`eth0` = 192.168.0.11, `eth1` = 192.168.0.12) and the PLCA
parameters can be changed two ways:

- **Persistent, no rebuild — the `env` command group.** Backed by the
  Emulated EEPROM; survives reset/power-cycle. Recommended.
- **Persistent, requires rebuild — edit `configuration.h`.** Only matters
  for the *compiled-in* defaults that `env` seeds a blank/freshly flashed
  EEPROM from (`resetenv` also restores these).

### 6.1 Persistent: edit the build config and rebuild

All defaults live in **`firmware/src/config/default/configuration.h`** (an
MCC-generated file — a plain text edit + rebuild is fully supported; only a
*re-run of MCC code generation* would overwrite it, in which case make the
change in the MCC project instead).

| Setting | Macro | Default |
|---|---|---|
| eth0 (T1S) IP | `TCPIP_NETWORK_DEFAULT_IP_ADDRESS_IDX0` | `"192.168.0.11"` |
| eth0 gateway | `TCPIP_NETWORK_DEFAULT_GATEWAY_IDX0` | `"192.168.0.1"` |
| eth0 MAC | `TCPIP_NETWORK_DEFAULT_MAC_ADDR_IDX0` | `"00:04:25:1C:A0:02"` (fallback only — `env` derives the real per-board MAC from the SAME54 serial number) |
| eth1 (100BASE-T) IP | `TCPIP_NETWORK_DEFAULT_IP_ADDRESS_IDX1` | `"192.168.0.12"` |
| eth1 gateway | `TCPIP_NETWORK_DEFAULT_GATEWAY_IDX1` | `"192.168.0..1"` (sic — see the quirk note in [§3](#3-hardware-setup)) |
| eth1 MAC | `TCPIP_NETWORK_DEFAULT_MAC_ADDR_IDX1` | `"00:04:25:1C:A0:03"` (fallback only, see above) |
| PLCA node id | `DRV_LAN865X_PLCA_NODE_ID_IDX0` | `5` |
| PLCA node count | `DRV_LAN865X_PLCA_NODE_COUNT_IDX0` | `8` |

**These macros are only the fallback defaults — a stored `env` record
wins.** `env_load_defaults()` (`env.c`) reads exactly these same macros to
seed a blank/corrupt EEPROM; once the stack is up, `env_apply()` (`app.c` →
`env.c`) pushes the *persisted* configuration into the running stack. Run
**`showenv`** to see what is actually in effect — a flash/reflash does not
erase the Emulated EEPROM (it lives outside the hex image), so an edited
macro can appear to have no effect until you `resetenv` or `setenv` +
`saveenv` the value directly.

### 6.2 Persistent via the `env` CLI group (recommended)

```text
showenv                       # see current config
setenv dns0 1.1.1.1            # edit the RAM shadow
saveenv                        # persist to EEPROM and apply live
resetenv                       # back to compiled defaults, persisted and applied
```

A **MAC** change follows the same `setenv mac0 XX:XX:XX:XX:XX:XX` +
`saveenv` pattern, but — unlike IP/PLCA — only takes effect after the *next*
reset, since the TCP/IP stack reads the MAC once, at `TCPIP_STACK_Init()`.

**PLCA node id/count** persist *and* take effect without a reset, but two
different commands do the two halves: `setenv plca_id <id>` only updates the
RAM shadow (no register write, no effect yet); `saveenv` is what actually
writes the PLCA register on the LAN865x. If the LAN register state machine
is busy at that moment, the apply is skipped and must be retried with
another `saveenv` once idle — verify with a register read
(`lan_read 0x0004CA02`, `PLCA_CTRL1`: `NODE_CNT` in bits 15:8, `NODE_ID` in
bits 7:0), not with the bare `plca_node` query, which reports the driver's
intent rather than the PHY's actual state.

### 6.3 Volatile runtime via Harmony stack commands / `plca_node`

```text
netinfo                                   # show both interfaces, IPs, MACs, status
setip  eth0 192.168.0.19 255.255.255.0    # set eth0 IPv4 address + mask
setgw  eth0 192.168.0.1                    # set eth0 gateway
plca_node 0                                # set PLCA node id (writes PLCA_CTRL1)
plca_node                                  # (no arg) read back the current node id
```

> ⚠️ **These specific commands are volatile.** Anything set with `setip`/
> `setgw`/bare `plca_node <id>` is lost on the next reset or power-cycle —
> the board boots back to whatever `env` has persisted (§6.2). Use these
> only to try a value before persisting it with `setenv`/`saveenv`.

---

## 7. Port mirror and sniffer: capturing the T1S bus in Wireshark

The `span` command group turns the bridge into a SPAN/monitor port: it
copies T1S (`eth0`) traffic onto `eth1` so a PC running Wireshark on its
Fast-Ethernet adapter can see the two-wire bus.

### Why a mirror is needed

Two things are otherwise invisible to a PC capture on `eth1`:

1. **The endpoint's replies** arrive on `eth0` and — because they are
   addressed to the bridge itself — are delivered *locally* by the MAC
   bridge, never forwarded onto `eth1`.
2. **The firmware's own requests** (a `ping`, ARP, ...) are sent *out* of
   `eth0` by the bridge. A node never receives its own transmissions, so no
   RX packet handler ever sees them either.

### `mirror` vs. `sniffer`

Both clone frames to `eth1`, but with a different filter:

| Command | RX filter | TX side |
|---|---|---|
| `mirror [0\|1]` | only frames addressed **to this bridge** (dst MAC == eth0 MAC) — the endpoint's replies | this bridge's own outgoing frames (src MAC == eth0 MAC), so a firmware-originated `ping` shows both request and reply |
| `sniffer [0\|1]` | **every** frame eth0 receives, including traffic between two other T1S nodes that never involves this bridge at all | same as `mirror` — traffic between two other nodes never touches this bridge's own TX path to begin with |

`mirror`'s narrower RX filter exists specifically to keep a bridge-focused
capture duplicate-free: a PC→endpoint frame the bridge merely *forwards*
already reaches the PC natively via the normal bridge path, so mirroring it
again would just be noise. `sniffer` skips that filter when you want to see
the whole bus, not just this bridge's own conversations. Both can be on at
once; `sniffer` is simply the broader of the two.

### Using it

1. On the PC, start **Wireshark** on the Fast-Ethernet adapter connected to
   `eth1`.
2. On the board CLI (serial or Telnet): `mirror 1` or `sniffer 1`.
3. Watch T1S traffic appear in Wireshark.

```text
mirror 1                # eth0(T1S) -> eth1 mirror: ON (RX to this bridge + this bridge's own TX)
ping 192.168.0.54        # now visible on eth1: request + reply
mirror 0                 # turn it off when done
```

### Limitations

- Exact L2 frames (header + payload) are cloned verbatim, single-segment
  only (a hypothetical multi-segment frame would be truncated).
- Mirroring adds one cloned `eth1` transmit per matching frame — leave it
  **off** for normal bridging to avoid the extra load.
- Both are runtime toggles that default to **off** on every boot (unless
  persisted via `env`, §6.2).

### One fragile coupling: the TX-mirror hand-patch

The TX-mirror half depends on a hand-patch inside MCC-generated code:
`DRV_LAN865X_PacketTx()` in `drv_lan865x_api.c` calls
`mirror_eth0_tx_hook()`, a symbol re-run of `Generate Code` silently removes
(see item 5 in
[`docs/mcc-generated-code-patches.md`](docs/mcc-generated-code-patches.md)).
The symptom is subtle — the capture still shows frames *from* the bus, just
none of the bridge's own, which looks like a half-working mirror rather than
a missing patch. `patches\apply_patches.py --check` catches this after any
MCC change (§5.5); `scripts\sniffer_capture_test.py` exercises the RX/mirror
path against real traffic between two T1S nodes.

---

## 8. Throughput testing

### `iperf` (built-in, protocol-compatible)

`iperf`/`iperfk`/`iperfi`/`iperfs` are the Harmony TCP/IP stack's own
built-in throughput tester, protocol-compatible with the classic
`iperf`/`iperf2` tool — measures end-to-end throughput across the bridge
path: PC → `eth1` → the MAC bridge → `eth0` → the T1S endpoint, and back.

| Option | Meaning | Default |
|---|---|---|
| `-s` | run as **server** | — |
| `-c <ip>` | run as **client**, connect to `<ip>` | — |
| `-u` | UDP instead of TCP | TCP |
| `-b <rate>` | target bandwidth for a UDP client (bps; `K`/`M` suffix) | 10 Mbps |
| `-t <secs>` | test duration | 10 s |
| `-p <port>` | server/target port | 5001 |

```text
iperf -s                                  # on the board, as a server
iperf -c 192.168.0.220 -u -b 50M -t 20    # from the board, as a client
iperfk                                     # stop a running session
```

Testing from the **PC side** works the same way with roles reversed, e.g.
`iperf -s` on the board, then `iperf2 -c 192.168.0.12` from the PC.

### `scripts\iperf_matrix_test.py` (host-side automation)

Runs an `iperf` matrix across the PC, this bridge, and two T1S follower
nodes on the bench — UDP max-rate search plus a TCP measurement, both
directions per pair — driving the device nodes over their CLI the same way
`cli.py` does. Always reads UDP loss from the *receiving* side, never the
embedded client's own self-reported figure. Logs incrementally to
`docs\iperf_matrix_results.log` so an interrupted run still leaves usable
data.

```bat
python scripts\iperf_matrix_test.py
python scripts\iperf_matrix_test.py --udp-duration 10 --pairs "A->B,B->A"
```

### `testserver` — TCP echo test server

A separate, simpler throughput tool: `testserver start [port]` opens a plain
TCP echo server (default port `5566`) as a small polled state machine
(`IDLE → LISTEN → ECHO`, no signal handlers, driven from the same superloop
as everything else), echoing received bytes back in fixed 512-byte chunks
and tracking RX/TX byte counters. Useful for a bandwidth-ramp test from an
external tool that just wants an echo endpoint, distinct from `iperf`'s own
protocol.

```text
testserver start          # listen on the default port (5566)
testserver start 6000     # listen on a specific port
testserver stop
```

---

## 9. Transmitter test modes and PLCA diagnostics

The LAN8651 implements the transmitter test modes of **IEEE 802.3-2022
§147.5.2** in hardware — a defined, continuous pattern with no user traffic,
which is what level, jitter, droop and spectrum measurements need. Selecting
one is a plain register write, no firmware change required.

```text
testmode              # show the current mode, decoded
testmode 1            # enter test mode 1, verified by readback
testmode 1 30         # ... and revert to normal operation after 30 s
testmode 0            # back to normal operation
```

| Mode | Purpose | Instrument |
|---|---|---|
| 0 | normal operation | — |
| 1 | output voltage, timing jitter | oscilloscope |
| 2 | output droop | oscilloscope |
| 3 | PSD mask / transmitter distortion | spectrum analyser |
| 4 | transmitter high impedance | measure the bus *without* this transmitter |

The command writes `T1STSTCTL` and reads it back automatically, reporting
`[VERIFY] PASS`/`FAIL` — the readback is the actual evidence that the
register kept the value, not just that the write transaction completed.
Modes 1–4 **take the T1S link down** by design; the CLI itself is unaffected
(it runs over EDBG/Telnet, not T1S). Use the optional timeout when in doubt
— a forgotten test mode later presents as a link that will not come up.

**Beyond the transmitter test modes**, `plca_stat` and `sqi` give ongoing
bus-health visibility without touching the transmitter at all:
`plca_stat` reports link/node/timeout+beacon counters and events below
IP-frame level; `sqi [node|all|off]` runs a continuous Signal Quality
Indicator, optionally with a periodic report (`sqi report <sec>`).

---

## 10. Telnet console (TCP/23)

Unlike the sister project, this firmware exposes its full CLI over a second,
independent transport: **Telnet on TCP/23**, alongside the EDBG serial port.
Same command groups, same commands — the only difference is which console a
reply goes back over, handled uniformly by `cmd_print.h`'s
`CMD_PRINT`/`CMD_MSG` wrappers throughout every command module.

```text
telnet 192.168.0.12
Login: admin
Password: password
```

Credentials are currently hardcoded in `TelnetAuthenticationHandler()`
(`app.c`) — `admin`/`password` — not an `env`-configurable field. Up to
`TCPIP_TELNET_MAX_CONNECTIONS` (2) concurrent sessions are supported.

`run_gui_telnet.bat` / `scripts\bridge_gui_telnet.py` is a Telnet-based
counterpart to `run_gui.bat`/`bridge_gui.py` — same status/configuration
GUI, connecting to the board's IP instead of a COM port. Its own config file
(`json\bridge_gui_telnet_config.json`) defaults to `192.168.0.12` /
`admin` / `password`.

### Behind the scenes: why this needed real work

Getting a second console transport onto an MCC-generated stack surfaced
several non-obvious bugs, all root-caused and fixed during this project's
bring-up (full narrative in `docs/session-log.md`, hand-patch details in
[`docs/mcc-generated-code-patches.md`](docs/mcc-generated-code-patches.md)):

- **Auth registered too early** — `TCPIP_TELNET_AuthenticationRegister()`
  called from `APP_Initialize()` was silently overwritten by the stack's own
  later module init; fixed by deferring it to `APP_STATE_SERVICE_TASKS`.
- **Every command after the first failed** — TeraTerm terminates a line with
  CR+NUL, not CR+LF; the generated character-input state machine had no case
  for a bare NUL, which corrupted the *next* command's buffer. Hand-patched.
- **Command replies went to the wrong console** — all six command modules
  used `SYS_CONSOLE_PRINT()` (always serial) instead of the `pCmdIO` each
  handler receives; fixed via `cmd_print.h`.
- **Large output (`dump`, `netinfo`) truncated or corrupted over Telnet** —
  the Telnet TX path had no real backpressure; fixed with a bounded
  retry-on-short-write loop in a hand-patch to `telnet.c`, verified
  deterministic up to 32000-byte dumps.

None of this is Telnet-server-specific caution dressing — it's the reason
the Telnet console is safe to rely on today rather than a demo that happens
to work for `help`.

---

*See [`docs/how-to-bridge.md`](docs/how-to-bridge.md) for the step-by-step
MCC configuration this bridge was built with, and
[`docs/session-log.md`](docs/session-log.md) for the full chronological
bring-up history behind every fix referenced above.*
