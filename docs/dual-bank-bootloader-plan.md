# Dual-Bank TCP Bootloader — Implementation Plan

**Status: plan, nothing implemented yet.** Companion to
[`dual-bank-bootloader-concept.md`](dual-bank-bootloader-concept.md), which
stays the *why*. This document is the *how*: every address, register, file and
function that has to change, in the order they should be built, with the
datasheet reference that settled each of the concept document's open items.

Everything below was checked against `SAME54_Datasheet.pdf` (DS60001507K), the
MCC-generated PLIB already in this project, and the tracked
`release\bridge_lan865x_100baseT.hex` — not from memory. Where something is
still an assumption that only hardware can confirm, it says so and has a test in
[§10](#10-test-plan).

The end state, in one sentence: a **Bootload** button in the Telnet GUI opens a
window with a progress bar, and when the bar is full the board runs the new
firmware with its environment intact.

---

## 1. What the datasheet settled

The concept document listed six open items. All are answered.

| Open item | Answer | Source |
|---|---|---|
| Bank split | 1 MiB array = **BANKA 512 KiB + BANKB 512 KiB**. Both are mapped in the NVM main address space at the same time: the active bank at `0x00000000`, the other one directly above it at `0x00080000`. | §25.6.3, §9.2 |
| Write-target addressing | **There is no bank-select register.** `NVMCTRL.ADDR` (offset `0x14`) is a plain 24-bit *byte* address in the main address space, and page-buffer writes are ordinary 32-bit stores to the target address, which load `ADDR` automatically. "Program the inactive bank" therefore just means "write to `0x00080000 + offset`", whatever `STATUS.AFIRST` says. `AFIRST` is read only to *report* which physical bank that is. | §25.6.6, §25.8.8 |
| Erase granularity | Erase = **block = 16 pages = 8192 B**; write = **page = 512 B** (or quad-word). Both already wrapped by the generated PLIB. | Table 25-2 |
| Post-swap confirmation | Not solved by hardware — see [§8](#8-optional-probation-and-automatic-rollback-wp6), a persistent-RAM probation flag. | |
| `BOOTPROT` | User page word0 = `0x3C001239` → bits 29:26 = `0xF` → **(15−15)·8 KB = no protected region**. Nothing to disable, nothing in the way. | Table 9-2, decoded from the release HEX |
| SmartEEPROM | User page word1 = `0x2AA80080` → `SBLK` = 0 → **disabled**. Nothing reserved by hardware at the end of either bank, and `BKSWRST`'s SmartEEPROM reallocation step is a no-op. | Table 9-2, §25.6.7 |

Two more facts that were not on the list but matter:

- **Region locks.** User page word2 = `0xFFFFFFFF` → all 32 regions unlocked. The
  bootloader still reads `NVMCTRL_RegionLockStatusGet()` and issues `UR` for the
  target region, in case a future build ever locks one (§25.6.5).
- **Both banks share one page buffer** (§25.6.6.2). Consequence for the design: a
  half-filled page buffer must never survive a return from `BOOTLOAD_Tasks()`,
  or a `saveenv` running in the same main loop would corrupt it. The state
  machine below always fills and commits a whole page inside one call.

### Timing budget

| Operation | Typ. | Max. | Source |
|---|---|---|---|
| Page write (512 B), `tFPW` | 1.5 ms | 3 ms | Table 54-40 |
| Block erase (8 KiB), `tFEB` | 50 ms | 200 ms | Table 54-40 |

For today's image (§2): 25 blocks × 50 ms = **1.3 s** of erase, 400 pages ×
1.5 ms = **0.6 s** of programming — about **2 s typical, 6 s worst case** of NVM
time for a full update, all of it overlapped with the network transfer and none
of it stalling the CPU (RWW, §25.6.6.3). The environment copy in §4 adds ~0.15 s
typical, and that part *does* stall.

---

## 2. Flash map

Current image, measured from `release\bridge_lan865x_100baseT.hex`:

- highest code address `0x00031DF4` → **flat image 204,276 B = 400 pages = 25 blocks**
- plus a 12-byte record at `0x00804000` — the **NVM user page** (fuses). Not part
  of any bank; see [§7.4](#74-the-user-page-record-in-the-hex).

| Region | Address | Size | Purpose |
|---|---|---|---|
| Active bank, code | `0x00000000`–`0x0007BFFF` | 496 KiB | running firmware, linked at 0 as today |
| Active bank, top | `0x0007C000`–`0x0007FFFF` | 16 KiB | **new:** environment hand-over region (§4) |
| Inactive bank, code | `0x00080000`–`0x000FBFFF` | 496 KiB | **update target** |
| Inactive bank, top | `0x000FC000`–`0x000FFFFF` | 16 KiB | live emulated EEPROM (`env`) — never written by the bootloader |

Both halves swap roles on every `BKSWRST`; the addresses above stay correct as
written, because they are relative to whichever bank is active.

Image budget: 204,276 B of 507,904 B = **40 %** of one bank. Comfortable — but no
longer the 19.8 % the concept document quoted, because the usable region halves.
That is what the linker change in [§5](#5-linker-and-build-changes-wp3) makes
explicit and enforced.

---

## 3. Transport: a dedicated binary TCP port

The concept document deferred this. Deciding it now, because everything else
depends on it.

| | ASCII lines over the Telnet console | Dedicated binary TCP port |
|---|---|---|
| Payload overhead | ×2 (hex) or ×1.33 (base64) plus line framing | none |
| Console line length | `SYS_CMD` takes one command line at a time, each through the parser and echo path | not involved |
| Progress / abort | shares the one console stream with the reply traffic | independent socket; the console stays free for status |
| Precedent in this repo | — | [`testserver.c`](../firmware/src/testserver.c): a proven non-blocking TCP server state machine |
| Stray connection | — | prevented by the magic header plus the `arm` handshake |

**Decision: control over the existing Telnet console, image over its own TCP port
(default 5567).** `testserver.c` on 5566 is the template — including its two
hard-won details: call `TCPIP_TCP_Disconnect()` on a server socket to return it to
listening, and never assume `TCPIP_TCP_ArrayGet/Put` moved everything you asked
for.

Flow control is "don't drain when busy": while NVMCTRL is programming, the state
machine does not read from the socket, the TCP window closes, and the PC side
blocks. No protocol-level ACKs needed.

---

## 4. The environment problem — the one critical decision

**Not in the concept document, and it decides whether a remote update keeps the
board reachable.**

`EEPROM_EMULATOR_EEPROM_START_ADDRESS` is `0xFC000`, 16 KiB = 2 blocks
(`firmware/src/config/default/library/emulated_eeprom/emulated_eeprom_local.h:66`).
MCC derives that value as *flash size − EEPROM size* (`1048576 − 16384 = 1032192`,
visible as a `Dynamic` symbol in
`tcpip_iperf_lan865x_default/components/lib_emulated_eeprom.yml`) — "the end of
the 1 MiB", which with one bank in use was simply the end of flash.

With bank swapping in play, that address is in the **upper half of the main
address space, i.e. physically in whichever bank is *not* running.**

### Why "both banks share the last page" cannot work

The main address space does not contain a region that survives a swap. `BKSWRST`
exchanges which physical bank is mapped at `0x00000000` and which at
`0x00080000`; every address in the 1 MiB window therefore points at the *other*
physical flash after the swap. `0xFC000` before the swap and `0xFC000` after the
swap are two different physical regions. There is no shared last page to be had.

The only genuinely bank-independent non-volatile stores on this part are:

- the **auxiliary space**, i.e. the NVM user page at `0x00804000` — 512 B, of
  which "the remaining 480 Bytes can be used for storing custom parameters"
  (§9.4). Outside both banks, untouched by chip erase and by `BKSWRST`. The
  72-byte `env` record would fit easily. Against it: erase granularity is the
  whole page (`EP`), writes are quad-word only, there is no wear-levelling, and
  the first 32 bytes are the fuses — a botched write there costs BOD/BOOTPROT/WDT
  settings, which is a far worse failure than a lost IP address.
- **SmartEEPROM**, which `BKSWRST` explicitly reallocates into the opposite bank
  (§25.6.7 step 2) — the feature the silicon designers intended for exactly this.
  Against it: it needs the `SBLK`/`PSZ` fuses set in the user page plus a reset to
  take effect, and `env.c`'s storage backend rewritten. A much larger change than
  the update mechanism itself.

Both stay on the table for later; neither is the right first step.

### Is the MCC emulated-EEPROM library willing to live somewhere else?

**Yes — it is fully relocatable, and this was checked, not assumed.** The
constant is used exactly once in the whole library:

```c
/* emulated_eeprom.c:712 */
eeprom_instance.main_array = (EEPROM_PAGE*)EEPROM_EMULATOR_EEPROM_START_ADDRESS;
```

Everything after that is relative — `main_array[physical_page]`, row and page
indices, `spare_row` — and the erase/write calls are plain
`NVMCTRL_BlockErase((uint32_t)flashAddr)` / `NVMCTRL_PageBufferCommit(...)` on that
pointer. There is no flash-size arithmetic, no "top of flash" special case, and
no absolute address anywhere else in the module. The only requirements on a new
base are 8 KiB block alignment and 32 free pages (= 2 blocks); `0x7C000` satisfies
both.

The cost of moving it is therefore not in the library, it is in MCC bookkeeping:
the symbol is `Dynamic` (computed), so a changed value must either be pinned as a
`User` value in the component model or maintained as an entry in `patches\` —
otherwise the next `Generate Code` silently puts it back to `0xFC000`. See
[`mcc-generated-code-patches.md`](mcc-generated-code-patches.md).

### The three workable options

| | **A — leave it at `0xFC000`** (chosen) | **B — move it to `0x7C000`** | **C — user page / SmartEEPROM** |
|---|---|---|---|
| Where the live record sits | top of the **inactive** bank | top of the **running** bank | outside both banks |
| Hand-over at commit | copy 16 KiB `0xFC000` → `0x7C000` | copy 16 KiB `0x7C000` → `0xFC000` | none needed |
| Copy writes into | the **running** bank → **CPU stalls** ~0.15 s typ, ~0.5 s max | the inactive bank → RWW, no stall | — |
| MCC-generated files touched | **none** | `emulated_eeprom_local.h` (patch or `User` symbol) | user page format / `env.c` backend |
| Migration on existing boards | **none** — the stored environment stays valid | env lost on the first SWD flash unless a legacy import is written into `env.c` | full rewrite of the storage layer |
| Risk to the live record during a transfer | it is inside the target bank, protected by the image size limit (`≤ 0x7C000`, also enforced by the linker) | not in the target bank at all | none |

**Decision, 2026-09-04: A.** It is the option that changes nothing outside the
new module: no MCC patch to maintain, no environment migration, and the image size
limit that protects the live record is the same constant the linker already
enforces. Its one drawback is a bounded, documented CPU stall (§25.6.6.1:
"Reading in a bank stalls the bus when it is being programmed or erased") that
happens immediately before a deliberate reset.

**B stays fully specified as the fallback, and switching is cheap** — one constant plus a patch entry,
because of the single use site above. Test T5 measures the stall on hardware and is the
only thing that would reopen this decision. Doing this the other way round (B first) would mean a patch to maintain
and a lost environment on every existing board, to buy a property that A may well
not need.

### Why the copy is correct, traced over two generations

Under A, starting from a fresh board (`AFIRST=1`, BANKA runs):

1. Firmware runs from BANKA at `0x0`; `env` lives at `0xFC000` = **BANKB top**.
2. Update writes the image into `0x80000…` = BANKB, never above `0xFBFFF`.
3. Commit copies `0xFC000` → `0x7C000` (BANKA top), then `BKSWRST`.
4. BANKB is now at `0x0` and runs the new firmware. It reads `env` at `0xFC000`,
   which is now **BANKA top** — the copy. Environment intact.
5. Next update, from BANKB: target is BANKA at `0x80000…`, live `env` at `0xFC000`
   = BANKA top again, copy goes to `0x7C000` = BANKB top, swap. Same picture with
   the physical banks exchanged.

Without step 3 the new firmware finds a blank region, the emulated EEPROM library
formats it, `env.c` seeds the compiled defaults — and the board comes back at the
default IP, i.e. the network update disconnects itself. That is the failure this
whole section exists to prevent.

Implementation notes for the copy: call `EMU_EEPROM_PageBufferCommit()` first so
nothing is pending, copy page by page through a RAM buffer (source and
destination are both flash), check `NVMCTRL_ErrorGet()` after every step, and — if
the copy fails — **do not swap**. An update that comes back without a network
configuration is worse than no update.

---

## 5. Linker and build changes (WP3)

`ROM_LENGTH=0xfc000` today
(`firmware/tcpip_iperf_lan865x.X/nbproject/configurations.xml:1136`, and the same
property in the FreeRTOS project). That lets the linker place code anywhere up to
`0xFC000` — including `0x7C000`, the hand-over region, and including addresses
beyond one bank, where a dual-bank image is impossible by construction.

**Change to `ROM_LENGTH=0x7c000`** in both projects. Not cosmetic: it turns "the
image fits in one bank *and* leaves the hand-over region alone" into a link-time
error instead of a field failure. Current usage 204 KiB of 496 KiB, so no
practical cost.

`build.bat` needs no change, but it is the natural place to check that the
produced HEX's top address is `< 0x7C000` and to print image size and CRC32 next
to the existing `Released:` line — the same numbers the GUI will send.

---

## 6. Firmware: `bootload.c` / `bootload.h` (WP1, WP2)

A new self-contained module in `firmware/src/`, shaped like `testserver.c` and
`port_mirror.c`: polled state machine, own CLI group, no ISRs, no RTOS
dependency. Driven from `APP_STATE_IDLE` in `app.c`, right after
`TESTSERVER_Tasks()`:

```c
/* Firmware self-update into the inactive flash bank - see bootload.c */
BOOTLOAD_Tasks();
```

and registered from `APP_STATE_SERVICE_TASKS` next to the other `*_Initialize()`
calls.

### 6.1 Public API

```c
void BOOTLOAD_Initialize(void);   /* register the 'bootload' CLI group */
void BOOTLOAD_Tasks(void);        /* drive the state machine, every main-loop pass */
bool BOOTLOAD_IsActive(void);     /* true from 'arm' until done/abort, for anything
                                     that may want to hold off its own flash writes */
```

### 6.2 CLI commands (group `bootload`)

| Command | Effect | Reply (one machine-readable line) |
|---|---|---|
| `bootload` | status | `BL: state=IDLE bank=A rx=0 size=0 err=0` |
| `bootload arm <bytes> <crc32>` | validate size and alignment, reset counters, open TCP 5567, go to `WAIT_CONN` | `BL: READY port=5567 max=507904` or `BL: ERR <reason>` |
| `bootload abort` | close the socket, back to `IDLE` (nothing in the target bank is trusted) | `BL: ABORTED` |
| `bootload commit` | only from `VERIFIED`: flush and copy the environment, then `BKSWRST` | `BL: COMMIT`, then the connection dies |
| `bootload verify <bytes> <crc32>` | CRC32 the **running** image at `0x0` over `<bytes>` and compare — the post-reboot proof | `BL: RUNNING crc=0x… match=1` |
| `bootload info` | active bank, target window, limits, last error | multi-line |

Replies use `CMD_PRINT()` ([`cmd_print.h`](../firmware/src/cmd_print.h)), so they
go back to whoever asked — serial or Telnet.

### 6.3 States

```
IDLE ─arm─> WAIT_CONN ─client─> RECV ─all bytes─> VERIFY ─ok─> VERIFIED ─commit─> (BKSWRST)
  ^             │                 │                 │ fail             │
  └──abort/err──┴─────────────────┴─────────────────┴──────────────────┘
```

### 6.4 The receive path, concretely

```c
#define BL_TARGET_BASE   0x80000u                    /* inactive bank, always */
#define BL_MAX_IMAGE     0x7C000u                    /* = ROM_LENGTH; env region excluded */
#define BL_PAGE          NVMCTRL_FLASH_PAGESIZE      /* 512  */
#define BL_BLOCK         NVMCTRL_FLASH_BLOCKSIZE     /* 8192 */

static uint32_t s_page[BL_PAGE / 4];   /* one page, word-aligned */
static uint32_t s_fill;                /* bytes in s_page       */
static uint32_t s_written;             /* bytes committed to flash */
```

Per `BOOTLOAD_Tasks()` call, while `!NVMCTRL_IsBusy()` and a byte budget (8 KiB,
as in `testserver.c`) is left:

1. `TCPIP_TCP_ArrayGet()` into `s_page`, up to the page boundary.
2. When `s_fill == BL_PAGE`:
   - if `(s_written % BL_BLOCK) == 0` → `NVMCTRL_BlockErase(BL_TARGET_BASE + s_written)`,
     then return and come back when `!NVMCTRL_IsBusy()`. Erase-on-demand: only the
     blocks the image actually needs are erased, and the erase overlaps the
     transfer.
   - `NVMCTRL_PageWrite(s_page, BL_TARGET_BASE + s_written)`; `s_written += BL_PAGE`;
     `s_fill = 0`.
3. After every NVM operation, `NVMCTRL_ErrorGet()`: any of `PROGE`/`LOCKE`/`ADDRE`/
   `NVME` aborts the transfer with an error code and closes the socket.
4. The final partial page is padded with `0xFF` and written the same way.

The PLIB calls are fire-and-poll (`plib_nvmctrl.c` issues the command and returns;
`NVMCTRL_IsBusy()` reads `STATUS.READY`), so nothing here blocks the main loop —
the TCP/IP stack, the bridge forwarding path and the console keep running
throughout. That is the whole point of RWW.

### 6.5 Data-socket framing

A 16-byte header, then raw image bytes:

```
offset 0   u32  magic = 0x424C4452   ('BLDR')
offset 4   u32  size  = image bytes, must equal the 'arm' size
offset 8   u32  crc32 = image CRC32, must equal the 'arm' crc
offset 12  u32  hcrc  = CRC32 over the first 12 bytes
```

Any mismatch against what `arm` announced closes the connection without touching
flash — that is what keeps a stray connection or a second GUI from writing
anything. At the end the firmware sends one ASCII line back on the same socket and
closes: `BL: OK written=204276 crc=0x…` or `BL: ERR <reason>`.

### 6.6 Verify

CRC32, IEEE 802.3 / zlib-compatible (reflected, init `0xFFFFFFFF`, final XOR) so
the PC side can use `zlib.crc32` unchanged. Table-driven (256 entries, 1 KiB of
flash), chunked at 4 KiB per `BOOTLOAD_Tasks()` call, so a pass over 200 KiB
(~15 ms of CPU in total) never shows up as a main-loop stall.

**Invalidate the cache first.** `startup_xc32.c:85` enables the CMCC, so a
read-back can otherwise be served from a stale line. Before the verify pass call
`CMCC_InvalidateAll()`, and additionally set `NVMCTRL_CTRLA.CACHEDIS0/1` for the
duration of the pass, restoring `CTRLA` afterwards.

### 6.7 Commit

```c
/* state must be VERIFIED */
(void)EMU_EEPROM_PageBufferCommit();                  /* nothing pending in the env buffer */
bl_copy_region(0x0007C000u, 0x000FC000u, 0x4000u);    /* 2 block erases + 32 page writes - STALLS */
NVMCTRL_BankSwap();                                   /* BKSWRST: atomic, flips AFIRST, resets */
/* not reached */
```

(Under option B of §4 the two addresses swap places and the copy no longer
stalls; nothing else in the module changes.)

---

## 7. PC side

### 7.1 `scripts/bootload.py` — the protocol without the GUI (WP4)

Build the transport as a standalone module *first*, with an `--ip/--hex` command
line, so the whole firmware side can be brought up and debugged on the bench
before any Tk code exists. The GUI then imports it and only supplies a progress
callback.

```python
def flash_over_tcp(ip, hex_path, telnet=(user, password),
                   port=5567, progress=lambda phase, done, total: None,
                   cancel=lambda: False) -> Result
```

| # | Phase | Action | Bar |
|---|---|---|---|
| 1 | Prepare | parse HEX → flat image + CRC32; size and limit checks; read `timestamp` and `bootload info` from the board | 0–5 % |
| 2 | Arm | `bootload arm <size> <crc>` over Telnet, expect `BL: READY` | 5 % |
| 3 | Transfer | connect TCP 5567, send header + image, progress by bytes written to the socket | 5–85 % |
| 4 | Verify | read the firmware's closing line from the data socket | 85–90 % |
| 5 | Commit | `bootload commit`, expect `BL: COMMIT`, then the link drops | 90–95 % |
| 6 | Reboot | close the Telnet link, poll for the login prompt for up to 30 s, reconnect | 95–99 % |
| 7 | Post-check | `bootload verify <size> <crc>` → `match=1`, plus `timestamp` to show the new build | 100 % |

The bar in phase 3 counts bytes accepted by the socket, which runs a few KiB
ahead of what is actually in flash (TCP buffering). Honest enough for a bar;
phase 4's reply is the authoritative "it really is written".

Cancel is offered up to and including phase 4 (`bootload abort`); from phase 5 the
button is disabled — a `BKSWRST` in flight is not cancellable, and there is
nothing to undo anyway, since the previous bank is untouched.

### 7.2 Intel HEX → binary

About 25 lines: type-04/02 extended addresses, type-00 data, gaps filled with
`0xFF`, flat image from `0x0` to the highest code address, padded to a page
boundary. `intelhex` happens to be in `.venv` (a pyOCD dependency) but is *not* in
`scripts/requirements.txt` — an inline parser keeps the GUI's "standalone apart
from sv-ttk" promise intact. Adding `intelhex` to `requirements.txt` is the
alternative; pick one and say which in the file header.

### 7.3 GUI integration (WP5) — `scripts/bridge_gui_telnet.py`

- **Button** `⬆ Bootload…` in the *Device* group of `quick_command_groups`
  (`create_bridge_tab()`), next to `Flash` / `Select Hex...`. It uses the same
  `self._selected_hex_path` as the SWD buttons, so "Select Hex..." picks the image
  for both paths.
- **Dialog**: a modal `tk.Toplevel` with a phase label ("3/7 Transferring
  132 KiB / 200 KiB"), a determinate `ttk.Progressbar`, a small scrolling log box
  (the same lines that go to Command Output), `Cancel` (disabled from phase 5)
  and, at the end, `Close`.
- **Threading**: the established pattern — a worker thread calls
  `bootload.flash_over_tcp()` and posts `("bl_phase", n, text)`,
  `("bl_progress", done, total)`, `("bl_log", line)` and `("bl_done", ok, msg)`
  into `self.result_queue`; `process_queue()` grows four `elif` branches that
  update the dialog. No Tk call ever happens off the main thread.
- **Link ownership**: the worker must not touch `self.port_link` for phases 5–7.
  It sends `bootload commit` through the normal `send_command_via_link()`, then
  posts `("bl_reconnect",)`; the main thread runs `disconnect_device()`, and the
  worker opens a fresh `TelnetLink` and hands it over as `("port_opened", link)` —
  the same message `connect_device()` already produces, so the indicator, the
  terminal note and the Command Output line behave exactly as after a manual
  reconnect.
- **Guard rails** before phase 1: refuse if not connected; refuse if the HEX's top
  address is ≥ `0x7C000`; `askyesno` naming the file, its size, its CRC and the
  board's IP — this reboots a remote device.
- **Config**: two new keys in `json/bridge_gui_telnet_config.json` —
  `"bootload_port": 5567` and `"bootload_reboot_timeout_s": 30`.

`bridge_gui.py` (the serial GUI) stays out of scope: the update needs an IP route
to the board, which that tool does not necessarily have. Because the logic lives
in `scripts/bootload.py`, adding it later is a button and a dialog, nothing more.

### 7.4 The user-page record in the HEX

The release HEX contains 12 bytes at `0x00804000` — the NVM user page (BOD33,
BOOTPROT, SmartEEPROM, WDT, region locks). The bootloader path **cannot and must
not** program it: it is in the auxiliary space, outside both banks, and it needs
`EP`/`WQW`, not `WP`.

`bootload.py` therefore drops every record outside `0x00000000…BL_MAX_IMAGE` and —
because silently dropping fuse changes is exactly how a board ends up subtly
different from the HEX that was supposedly flashed onto it — compares those 12
bytes against the running device over the existing CLI (`peek 0x00804000 4`,
`peek 0x00804004 4`, `peek 0x00804008 4`) and refuses with a clear message if they
differ: *"this HEX changes the NVM user page; that can only be done over SWD
(Flash button)"*.

---

## 8. Optional: probation and automatic rollback (WP6)

Covers the concept document's remaining open item — an image that passes CRC but
does not come up usefully.

- Before `BKSWRST`, set a flag (magic, state, counter, CRC) in the Backup RAM at
  `0x47000000` - not cleared by the C runtime, retained across a warm reset, gone
  after a power cycle, which is the semantics wanted here: whoever pulls the power
  has intervened, and nothing should roll back behind their back.
- **Not** as a second `__attribute__((persistent))` variable, unlike
  `crashlog.c`'s record. That one is written and read by the same firmware, so
  wherever the linker puts it, both ends agree. This flag is written by the OLD
  image and read by the NEW one - two builds, two potentially different `.pbss`
  layouts - so its address has to be pinned by construction: a fixed absolute
  slot at the top of the region, `0x47001FE0`, far from what `.pbss` allocates
  from the bottom. Verified on hardware 2026-09-04, with no code at all:

  ```
  poke 0x47001FE0 0xC0FFEE01 ; reset
  --- after the reset (uptime 19 s) ---
  0x47001FE0: 0xC0FFEE01
  ```

  The region is 8 KB, not the 512 Bytes a hasty read of Table 9-1 suggests (that
  value belongs to the NVM User Row one row below): `crashlog.c` occupies 520 of
  those 8192 bytes, reads and writes up to `0x47001FFC` work and do not alias, and
  the linker script's `BKUPRAM_LENGTH 0x2000` agrees. No linker change needed.
- On boot, if the flag is set, `BOOTLOAD_Initialize()` starts a 180 s timer and
  prints a warning on the console.
- `bootload confirm` (sent by the GUI in phase 7) clears the flag.
- If the timer expires unconfirmed: clear the flag, print, `BKSWRST` back.

Honest limits: it does not help if the new image dies before reaching
`APP_Tasks()` (only SWD does), and the flag is lost on a power cycle, so a board
power-cycled during probation simply keeps the new image. Cheap insurance against
the realistic failure — "boots, but the network never comes up" — not against a
dead image. Build it after the happy path works end to end.

---

## 9. Work packages

**Landed 2026-09-04: WP0-WP6, the whole plan.** The mechanism works end to end on hardware, from
the command line (`scripts/bootload.py --ip <board>`) and from the Telnet GUI's
**Bootload (network)** button: send a build, verify it in the inactive bank, copy
the environment across, swap banks, wait for the reboot, prove that what is now
running is what was sent. Tests T1-T9 pass except the ones that need a second T1S
node; see [bench results](#10a-bench-results-2026-09-04). An update that comes up
unreachable now undoes itself (WP6).

| WP | Content | Files | Rough size |
|---|---|---|---|
| WP0 ✅ | Bench proof of the addressing assumption: write a pattern to `0x80000`, read it back, check the app keeps running (test T2) — a throwaway `bootload selftest` subcommand | `bootload.c` | ½ day |
| WP1 ✅ | Module + CLI + TCP receive + erase-on-demand + page writes + CRC verify. **No commit yet** — the swap stays out until this is boringly reliable | `bootload.c/.h`, `app.c` | 1–2 days |
| WP2 ✅ | Environment copy + `commit` + `BKSWRST` + `bootload verify` | `bootload.c` | ½–1 day |
| WP3 ✅ | `ROM_LENGTH=0x7c000` in the `.X` project, size/CRC line in `build.bat` | `nbproject/configurations.xml` ×2, `build.bat` | 1 h |
| WP4 ✅ | `scripts/bootload.py`: HEX→bin, protocol, phases, standalone CLI | new file | 1 day |
| WP5 ✅ | GUI button + progress dialog + queue branches + config keys | `bridge_gui_telnet.py`, `json/bridge_gui_telnet_config.json` | 1 day |
| WP6 ✅ | Probation / automatic rollback | `bootload.c`, `app.c` | ½ day |
| WP7 | Docs: `cli-reference.md` (new group, and the "what survives a reset" table), `README.md`, `docs/index.md`, and mark the concept document as superseded | docs | 2 h |

Order matters: **WP3 before WP2.** The environment copy writes to `0x7C000`, and
until the linker is capped, that address may hold running code.

---

## 10. Test plan

Each test states what it proves on real hardware, before the next one is worth
running.

| # | Test | Proves |
|---|---|---|
| T1 | `bootload info` on a fresh board: reports `bank=A`, target window `0x80000…0xFBFFF`, limits | the `STATUS.AFIRST` read and the map |
| T2 | Erase one block at `0x80000`, write a known page, `dump 0x80000 64` | the "inactive bank is always at `0x80000`" assumption — **the single most important assumption in this design** |
| T3 | Keep an `iperf` or `testserver` run going during T2 | RWW: programming one bank does not disturb forwarding out of the other |
| T4 | Full transfer of the current image, `bootload verify` against the CRC, **without** commit; then `bootload abort` and a normal reboot | the whole receive path, and that an abandoned update leaves the device exactly as it was |
| T5 | Time the environment copy (`0xFC000 → 0x7C000`) and confirm the console still responds afterwards | the stall is bounded and survivable — **the only test that would reopen the option-A decision in §4** |
| T6 | Full update with an identical image; after the reboot check `bootload verify match=1`, `showenv` unchanged, `uptime` reset | end to end, and above all that **the environment survived** |
| T7 | Full update to a genuinely different build (changed `timestamp`) | the new code actually runs |
| T8 | Pull the cable mid-transfer, then reconnect and repeat | a dropped connection leaves the running firmware untouched, and a retry works |
| T9 | Two updates in a row (A→B→A) | both swap directions, and the environment survives generation 2 — the case §4's trace predicts but only hardware confirms |

Recovery path throughout: `flash.bat` or the GUI's Flash button over SWD. Keep a
probe on the bench for T5–T9.

---

## 10a. Bench results (2026-09-04)

Board on EDBG probe `...1049`, `192.168.0.12`, image 210,140 bytes
(`crc32=0x58559D74`), firmware built the same day. Everything below is captured
output, not a summary of expectations.

**T1 - map and bank identification.** `bootload info` reports `bank A
(STATUS.AFIRST=1)`, target window `0x00080000 .. 0x000FBFFF`, `RUNLOCK=0xFFFFFFFF`
(all regions unlocked, as the user page predicted).

**T2 - the addressing assumption. PASS.** This is the one everything rested on:

```
> bootload selftest
bootload selftest: bank A is running; erasing+writing 0x00080000
  erase 37747 us, write 1478 us, readback [0]=0xB0070000 [127]=0xB007007F
BL: selftest PASS (0 mismatching words)
```

The inactive bank really is reachable at `0x00080000`, erase and page write work
there, and the read-back is exact. The measured times sit right on the data
sheet's typical figures (tFEB 50 ms typ - the part is faster; tFPW 1.5 ms typ).

**T3 - read-while-write under load. PASS.** A 4 KiB ping-pong TCP echo load
(`testserver`, port 5566) running from the PC while a full image was programmed
into the other bank - 26 block erases plus 411 page writes:

```
  t+ 0s      52 kB/s          (partial bucket)
  t+ 1s      80 kB/s
  t+ 2s      80 kB/s
  t+ 3s      80 kB/s  <== flash erase/program
  t+ 4s      80 kB/s  <== flash erase/program
  t+ 5s      92 kB/s  <== flash erase/program
  t+ 6s      80 kB/s
  t+ 7s      80 kB/s
echo errors: none
```

No dip, no timeout, no dropped connection. Because the load is a serialized
round trip rather than a bandwidth stream, a main-loop stall would have shown up
directly as a lower rate - it did not.

**T4 - full transfer and verify. PASS.** 210,140 bytes in 1.1 s (184 kB/s over
the 100BASE-TX side), `BL: OK written=210140 crc=0x58559D74` - the CRC the
firmware computes over what actually landed in flash equals `zlib.crc32()` of the
file on the PC. `uptime` afterwards shows no reset; `showenv`, `faultlog` and
`meminfo` are unchanged.

`bootload verify 210140 58559D74` against the **running** image returns
`match=1` - the post-reboot check WP2 will need already works.

**T8 - dropped connection. PASS.** A TCP reset (`SO_LINGER 0`) after ~37 KiB:

```
BL: state=IDLE bank=A rx=37360 written=36864 size=210140 err=6 (connection)
```

Clean failure, socket closed, back to IDLE, running firmware untouched. The
retry immediately afterwards completed normally.

**Correction this produced: the user-page comparison was the wrong check.**
The device's user page reads `0xFFFF9239 / 0xAAA8FF80`, the HEX's record
`0x3C001239 / 0x2AA80080`. They differ in the BOD12 factory calibration bits
(25:15), which Table 9-2 marks "do not change" and which the linker fills with
zeros because it cannot know the per-die values. pyOCD refuses that record when
flashing over SWD as well (`no memory region defined for address 0x00804000`),
so both update paths leave the user page alone and a mismatch there is normal.
`scripts/bootload.py` now checks what actually matters instead - `BOOTPROT` must
be `0xF` and `SBLK` must be 0 - and reports the raw words:

```
user page : 0xFFFF9239 0xAAA8FF80 -> BOOTPROT=0xF, SBLK=0
```

**T5 - the cost of the environment copy. 78 ms.** The copy is the one part that
writes into the bank the CPU executes from, so it stalls the bus:

```
bootload: environment copied 0x000FC000 -> 0x0007C000 in 78375 us; swapping banks in 300ms
```

Two block erases, 32 page writes and a 16 KiB compare. Bounded, an order of
magnitude below the half-second the decision in section 4 was willing to accept,
and it happens immediately before a deliberate reset. Option A stands.

**T6/T7/T9 - commit, swap, environment, both directions. PASS.** Five updates
were run over the network in a row, including one that installed a genuinely
different build (T7) and one that installed the fix described below. Every one:

```
phase 5: commit
BL: COMMIT env-copy+bankswap in 500ms, the board will reset
phase 6: reboot
board is back after 13 s
phase 7: check
BL: RUNNING bank=B size=211036 crc=0xC859A179 match=1
```

The board comes back on the same IP after ~13 s, `bootload verify` against the
**running** image matches, and the active bank alternates A -> B -> A -> B as
expected (T9).

**The environment test that actually proves something.** Reconnecting on the same
IP is weak evidence: this board's stored addresses happen to equal the compiled
defaults, so a board that lost its environment entirely would come back looking
identical. And from the second commit onwards source and destination hold the
same bytes, so the copy's own read-back verify cannot fail either. The test that
discriminates is to change the environment first:

```
setenv dns0 192.168.0.99 ; saveenv          # persisted, confirmed with readenv
python scripts/bootload.py --ip 192.168.0.12
... after the swap:
  eth0  ip 192.168.0.11  mask 255.255.255.0  gw 192.168.0.1  dns 192.168.0.99
```

A value that cannot come from the compiled defaults survived the bank swap. That
is the hand-over working.

### Two findings this shook out

**1. The new command group silently cost another one.** `MAX_CMD_GROUP` in the
MCC-generated `sys_command.h` is 8, and this project now wants nine groups.
`SYS_CMD_ADDGRP()` returns false and nothing acts on it, so adding `bootload`
made the `span` group (`mirror`, `sniffer`) vanish from a running board -
`*** Command Processor: unknown command. ***` - with one line in the boot log as
the only trace: `MIRROR: SYS_CMD_ADDGRP failed`. `MIRROR_Initialize()` registers
last (it is deferred to `APP_STATE_SERVICE_TASKS`), so it was the one that lost.
Fixed by raising the limit to 12, kept as `patches/sys_command_h.patch`, item 15
in [`mcc-generated-code-patches.md`](mcc-generated-code-patches.md). The fix was
then rolled out to the board over the network, which is as good a test of the
updater as any.

**2. SYS_TIME under-reports across a same-bank flash operation, badly.** The
first measurement of the environment copy said 6.4 ms - a tenth of the truth.
`SYS_TIME`'s hardware counter here is a 16-bit TC0 wrapping every 1 ms
(`plib_tc0.c`: COUNT16, DIV1, CC0 = 59999) whose overflow interrupt extends the
count in software. While the bus is stalled that interrupt cannot be serviced,
and every wrap after the first is lost. `bootload.c` therefore measures with the
Cortex-M4 cycle counter (`DWT->CYCCNT`), the same source `cpuload.c` uses, which
keeps counting through a stall - that is where the 78 ms above comes from.
Anything else in this firmware that times an operation across a same-bank erase
or page write with `SYS_TIME` is measuring too little, by roughly the stall.

**The GUI path (WP5), driven end to end without a human clicking.** A script
instantiated the real `BridgeGUITelnet`, connected it, auto-answered the
confirmation and pressed **Bootload (network)**, then read what the dialog itself
displayed:

```
   1.5s  bar=  0%  1/7  preparing ...
   2.3s  bar=  5%  2/7  arming the board ...
   2.5s  bar= 40%  3/7  transferring  88 KiB / 206 KiB
   3.4s  bar= 80%  3/7  transferring  192 KiB / 206 KiB
   3.6s  bar= 85%  4/7  verifying in flash ...
   4.2s  bar= 92%  6/7  waiting for the reboot ...
  17.2s  bar= 97%  7/7  checking what is running ...
  17.4s  bar=100%  done - the board is running the new firmware
```

**One thing that only showed up through the GUI:** the first attempt failed with
`Telnet connection closed by the board`. The GUI hands its own console over for
the duration of an update - the board reboots halfway through, so holding the old
socket makes no sense - and opening the updater's console in the same breath
lands in the gap where the firmware still counts the session just closed
(`TCPIP_TELNET_MAX_CONNECTIONS` is 2, reaped by a task that runs every 100 ms).
The board then accepts the TCP connection and closes it again without a prompt.
`Console.open()` now retries three times a second apart, the GUI worker waits a
second before starting, and the reconnect afterwards retries four times.

**WP6 - probation, all three paths on hardware.**

*Rollback (no confirmation).* `bootload.py --no-confirm`, then watching the board
from the PC once every 10 s:

```
     0s  BL: state=IDLE bank=A ... probation=ARMED 162s left
   ...
   162s  BL: state=IDLE bank=A ... probation=ARMED 1s left
   172s  BL: state=IDLE bank=B ... probation=off
```

and from its own console:

```
bootload: this image is ON PROBATION after a bank swap (#2).
bootload: 'bootload confirm' within 180s keeps it - otherwise the board swaps back to the previous image.
bootload: not confirmed within 180s - swapping back to the previous image now
bootload: the previous update was ROLLED BACK - it was not confirmed within 180s, so this (older) image was restored. Swap #2.
```

*Confirmation.* A normal update run: `BL: CONFIRMED image kept, rollback
cancelled`, then `probation=off` and the same bank still active 200 s later.

*Intervention.* Probation armed, then a plain `reset` before the window expires:
`discarding a stale probation note (reset cause was not NVM)`, `probation=off`,
bank unchanged - the board keeps what was booted rather than rolling back behind
the operator's back. The boot snapshot in `bootload info` showed why:
`rcause=0x40` (system reset request), not `0x08` (NVM).

**The defect this shook out: a missing memory barrier.** The first probation
runs came up with `probation=off` and the note reading all zeros, while `RCAUSE`
correctly said NVM. The note is written a few hundred milliseconds before
`BKSWRST`, and stores to RAM are posted - `volatile` orders them against each
other but does not wait for them to land. `NVIC_SystemReset()` carries a `__DSB()`
for exactly this reason; `NVMCTRL_BankSwap()` is a bare register write and does
not, so the reset can arrive while the note is still in the write buffer.
`bl_prob_write()` now ends with `__DSB()`. In fairness: the barrier alone did not
make the next run pass either, and the two runs between it and the working build
were never explained - what is certain is that every path has been exercised
repeatedly since, with the countdown, the rollback, the confirmation and the
stale-note case all reproducing.

**Where the note lives, and why not `.pbss`.** Fixed absolute address
`0x47001FE0`, the top of the 8 KB Backup RAM. Backup RAM survives a warm reset -
and, measured here, a `BKSWRST` in particular (`poke 0x47001FC0`, update+commit,
`peek` → value intact). It is deliberately not a `__attribute__((persistent))`
variable like `crashlog.c`'s record: that one is written and read by the same
firmware, so wherever the linker puts it both ends agree, while this note crosses
from one build to another.

**T3b - traffic THROUGH the bridge while it programs itself. PASS.** The half of
T3 that needed a second node: PC (100BASE-TX) -> bridge 192.168.0.12 -> T1S ->
node 192.168.0.21 running `testserver`, echoed back the same way, so every byte
crosses the bridge twice. The PC's ARP table holding the node's own MAC confirms
this really is the L2 forwarding path and not some detour. A full image was
transferred into the bridge's inactive bank in the middle of it:

```
  t+ 4s    52.0 kB/s
  t+ 5s    52.0 kB/s
  t+ 6s    56.0 kB/s  <== flash erase/program
  t+ 7s    56.0 kB/s  <== flash erase/program
  t+ 8s    52.0 kB/s  <== flash erase/program
  t+ 9s    52.0 kB/s
  t+10s    56.0 kB/s
echo errors: none
```

No dip - the differences are one 4 KiB chunk of bucket quantisation. Since the
load is a serialized round trip (~13 per second), a main-loop stall of tens of
milliseconds would have been visible immediately.

**And the cost of the swap itself: 1.9 s.** Programming disturbs nothing, but the
reset obviously interrupts forwarding. Measured by connecting to the node through
the bridge every 100 ms during a full update with commit:

```
   3.3s  forwarding gone
   5.2s  forwarding back
```

Worth knowing because it is far shorter than the ~13 s the updater waits for the
Telnet console: the MAC bridge is forwarding again long before the console is
usable. A single ping per second through the same update lost exactly one packet.

**The real-deployment shape: updating a node BEHIND the bridge.** The last test
updated the T1S node 192.168.0.21 over the network - every byte of it forwarded
by the bridge, which is how this would be used in the field. Three things about
that board made it a better test than any run on the bridge itself:

- it started from **bank B**, so the swap went B -> A, the mirror image of every
  previous run;
- its environment differs from the compiled defaults in every field that matters
  (eth0 192.168.0.21 / eth1 .22 instead of .11/.12, PLCA id 1, its own MACs), so
  a lost environment would have been unmissable - it would have come back as
  192.168.0.12 and collided with the bridge. After the swap: `eth0 ip
  192.168.0.21 ... plca id 1 ... mac 00:04:25:8E:8C:A1`, unchanged;
- it was back in **9 s**, and `bootload verify` against the running image
  matched.

**Still not run:** nothing from this plan. What remains open is deliberate: an
image that faults before `BOOTLOAD_Initialize()` still needs SWD (see WP6's
limits), and the environment hand-over has only been exercised with the boards in
this lab setup.

---

## 11. Open risks

- **The `0x80000` assumption (T2).** The datasheet says both banks are mapped and
  that the active one is at the base address, but never spells out the inactive
  one's address for this part in a single sentence. Everything else follows from
  it, so it gets its own test before any other code is written.
- **The environment copy stalls the CPU** (§4, option A). Documented behaviour,
  bounded, immediately before a reset — but it is the step to watch, and the
  reason option B stays specified.
- **A broken-but-CRC-valid image** still needs SWD unless WP6 is built.
- **`.pbss` layout across versions.** `crashlog.c`'s persistent record survives the
  `BKSWRST` reset, but the new build may lay `.pbss` out differently; its magic
  check should be verified to reject a stale record rather than print garbage. The
  same applies to WP6's own flag.
- **Image growth.** At 40 % of a bank there is room, but the ceiling is now hard —
  hence the build-time check in WP3.
