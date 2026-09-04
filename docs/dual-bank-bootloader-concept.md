# Dual-Bank TCP Bootloader — Concept

**Status: built and working (2026-09-04).** This document remains the *why* -
the discussion draft that decided the approach. What was actually implemented,
every address and register it rests on, and the hardware measurements are in
[`dual-bank-bootloader-plan.md`](dual-bank-bootloader-plan.md); the commands are
in [`cli-reference.md`](cli-reference.md#bootload--firmware-update-into-the-inactive-flash-bank).

Two things below were settled differently than sketched here: the image goes
over its own binary TCP port rather than as ASCII lines through the Telnet
console, and the update has to hand the emulated EEPROM over to the other bank
before swapping - without that step a board comes back at its compiled-in
default IP, i.e. disconnects itself. See sections 3 and 4 of the plan.

## Motivation

Flash usage on the current build is far from the limit: `19.8 %` of the
`0xfc000`-byte region the linker is configured for (`204203` bytes used,
from `tcpip_iperf_lan865x.X.production.map`), out of a `1 MiB` part
(`ATSAME54P20A`). There is comfortably enough headroom to hold two full
copies of the firmware, which opens the door to a remote update path: push a
new image to the board over the network (the bridge already has a Telnet
console) instead of requiring a debug probe and `flash.bat`.

## Target hardware: NVMCTRL has a built-in dual-bank swap

The ATSAME54P20A does not need a software A/B scheme built from scratch —
the NVM controller already implements one, called *Safe Flash Update Using
Dual Banks* (datasheet
[§25.6.7](https://onlinedocs.microchip.com/oxy/GUID-F5813793-E016-46F5-A9E2-718D8BCED496-en-US-15/GUID-558A81D3-2F03-432E-AAFB-8C9BF7A48A96.html),
memory organization in
[§25.6.2](https://onlinedocs.microchip.com/oxy/GUID-F5813793-E016-46F5-A9E2-718D8BCED496-en-US-15/GUID-5A9A6A5B-B77D-496F-8F1C-2866787380AC.html)):

- The 1 MiB flash array is split into two fixed physical banks, `BANKA` and
  `BANKB` (expected 512 KiB each for this part — the exact split from the
  Physical Memory Map / `PARAM.NVMP` is an open item, see below).
- A dedicated fuse (`STATUS.AFIRST`) records which bank is currently mapped
  to the main address space's base address, `0x00000000`. This mapping is
  self-contained in the NVM controller — **the running firmware is always
  linked and executed at address `0x0`, regardless of which physical bank it
  ends up in.** One linker script, one build, one `.hex` — no per-bank link
  address, no VTOR remap in software.
- **Read-While-Write (RWW):** programming or erasing one bank does not stall
  reads (i.e. code fetch) from the other bank. The application can keep
  running normally out of its own bank while it writes the new image into
  the other one — no flash-write routine needs to be relocated to RAM for
  this reason (RAM relocation would only matter if a bank tried to erase or
  write a page it is currently executing from, which does not happen here).
- Once the new image is fully written, a single command — `BKSWRST` — flips
  the `AFIRST` fuse and immediately resets the device. After reset, the
  bank that was just written is the one mapped at `0x0` and boots normally.
  This command is atomic; the old bank's contents are left untouched, so a
  second `BKSWRST` is always available as a manual rollback.

This replaces the RAM-based copy-in-place step from the earlier version of
this idea (write the new image into the *unused half* of flash, then run a
routine from RAM that copies it into the *actual* boot location, then
reset). With native dual-bank swap, the bank the image is written to
becomes the boot location directly — there is no copy pass, and the
correctness-critical moment shrinks from "a page-by-page software copy loop
that must not be interrupted" down to one atomic hardware operation.

## Proposed update flow

**Telnet GUI side** (`bridge_gui.py` or a dedicated tool):
1. Load the current build's `.hex` file, convert it to a flat binary image
   using a Python `hex` file library (Intel HEX → binary).
2. Open the existing Telnet console session, send the binary image
   line-by-line, ASCII-encoded, to the target.

**Target side:**
1. Receive and decode each line as it arrives.
2. Write the decoded bytes into the currently **inactive** bank via
   NVMCTRL, batching into full 512 B pages as they fill (see open items
   below on erase/write granularity). The application keeps running
   normally out of the active bank throughout — this is the RWW property
   above, not a stop-the-world operation.
3. Once the transfer is complete, verify the written image (CRC or
   similar) before trusting it. If verification fails, discard and report —
   the active bank and the currently running firmware are never touched, so
   a failed transfer leaves the device exactly as it was.
4. If verification passes, issue `BKSWRST`. The device resets and boots the
   new image directly.

## Open items (not yet resolved — flagged, not blocking further discussion)

- **Bank split.** Confirm `BANKA`/`BANKB` size for this exact part number
  (expected 512 KiB/512 KiB of the 1 MiB array) via the Physical Memory Map
  section of the datasheet or by reading `PARAM.NVMP` at runtime, rather
  than assuming.
- **Write-target addressing.** The exact register sequence for targeting
  "the inactive bank" while programming — how the `ADDR` field relates to
  `STATUS.AFIRST` — needs a closer, register-level read of the datasheet
  before writing any code. Not yet pinned down.
- **Erase granularity.** Erase is block-level per bank (`Table 25-2`); write
  is page (512 B) or quad-word. The receiving side needs to erase the
  target blocks ahead of (or interleaved with) the incoming line-by-line
  transfer, and buffer partial pages correctly — a naive "write whatever
  arrived" loop will not line up with page boundaries.
- **Post-swap confirmation / rollback.** A new image that passes the CRC
  check but is otherwise broken (bad init code, wrong config) will still
  boot successfully into a working `BKSWRST` and then get stuck — a second,
  manual `BKSWRST` is always available as an escape hatch, but an automatic
  "did the new image actually come up correctly" self-check (e.g. must
  reach a known-good state, such as the Telnet console listening, within
  N seconds, else auto-revert) is not designed yet.
- **`BOOTPROT`.** Currently unset (`None`, 0-byte protected region) on this
  project — dual-bank swap does not require a protected boot section, but
  this should be reconfirmed once the feature is actually exercised.
- **SmartEEPROM.** Unused today. If it is ever enabled, it consumes space
  from the end of each bank equally (`SEESTAT.SBLK`) — no action needed
  unless that changes.

## Known critical points, deliberately deferred

Line-by-line ASCII transfer over Telnet as the transport, and what happens
if the connection drops mid-transfer, were both raised as open questions in
discussion and intentionally set aside for now — the point of this document
is to capture the dual-bank mechanism and flow, not to resolve every
robustness question before deciding whether to pursue this at all.
