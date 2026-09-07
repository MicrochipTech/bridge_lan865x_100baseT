# Network Bootload — Practical Recipe for an AI Agent

How to flash this project's firmware onto a board **over the network**, using its
own dual-bank OTA update mechanism, instead of a debug probe. This is the
preferred way to flash any board that is currently reachable on the network — see
§4 for why, and [`pyocd-agent-debugging-guide.md`](pyocd-agent-debugging-guide.md)
for the SWD/pyOCD fallback path.

Background and full command reference already exist and are not repeated here:
[`dual-bank-bootloader-concept.md`](dual-bank-bootloader-concept.md) (why/how),
[`dual-bank-bootloader-plan.md`](dual-bank-bootloader-plan.md) (implementation),
[`cli-reference.md`](cli-reference.md#bootload--firmware-update-into-the-inactive-flash-bank)
(the `bootload` console commands themselves). This document is only the
operational recipe: what to actually run, and what goes wrong in practice.

---

## 1. Prerequisites

- The board is reachable: `ping <ip>` succeeds **and** its mTLS Telnet console is
  answering (see §3 if it isn't — do not assume a network problem before checking
  this).
- The image to send is already built: `release/bridge_lan865x_100baseT.hex` is
  what `build.bat` refreshes on every successful build and is the one already
  tracked in git — flash that path unless deliberately testing an unreleased build
  from `firmware/tcpip_iperf_lan865x.X/dist/default/production/...production.hex`.
- This project's own `.venv` has `scripts/bootload.py`'s dependencies.

## 2. The command

```bash
.venv/Scripts/python.exe scripts/bootload.py --ip <ip> --hex release/bridge_lan865x_100baseT.hex
```

A healthy run goes through seven phases and needs no other input — `bootload.py`
sends the environment-preserving commit and the post-boot `confirm` itself:

```
phase 1: prepare      image size/crc32, current bank/user-page fuse check
phase 2: arm           BL: READY port=5567 ...
phase 3: transfer      progress bar, ends with a throughput line
phase 4: verify        BL: OK written=... crc=...
phase 5: commit        BL: COMMIT env-copy+bankswap in 500ms, the board will reset
phase 6: reboot        board is back after Ns
phase 7: check         BL: RUNNING bank=... crc=... match=1 ; BL: CONFIRMED
```

`match=1` and `BL: CONFIRMED` are the actual proof the update worked — the tool's
own exit code and the absence of a traceback are necessary but not sufficient
(see §3 for what a *broken* run looks like instead).

The board keeps its IP and every other persisted setting across the update — the
commit step copies the emulated-EEPROM environment into the new bank before
swapping specifically so this holds. No separate re-provisioning step is needed
just because the firmware changed.

## 3. When `console.open()` fails instead of a clean transfer

Two distinct failure signatures were both seen live on this bench flashing
followers on 2026-09-07, and neither is a `bootload.py` bug:

```
ssl.SSLEOFError: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol
```
or
```
TimeoutError: timed out          # raw socket.create_connection(), not even TLS yet
```

Both happen in phase 1, before any image bytes are sent. First, confirm the board
is actually alive: `ping <ip>`. If ping succeeds but the console still won't open:

- **This project's Telnet only allows one connection at a time**
  (`TCPIP_TELNET_MAX_CONNECTIONS=1`). If anything else currently holds that slot
  (a GUI session, another script, a half-abandoned previous attempt), a new
  connection is refused or times out. Close whatever else is connected and retry
  — often enough on its own.
- If retrying doesn't help and the symptom is a **raw TCP timeout with no
  SYN-ACK at all** (not just a TLS-layer failure), this matches the TCP-stack
  DoS symptom already documented for this project (`session-log.md`,
  2026-09-06/07): the board's TCP stack has stopped answering entirely while
  ICMP still works. `bootload.py` cannot fix this — it needs the network stack
  to be reachable in the first place. The pragmatic recovery, confirmed to work
  live on both follower boards this session:
  ```bash
  .venv/Scripts/python.exe -m pyocd reset -t atsame54p20a -f 2000000 --pack <pack> -u <probe-serial>
  ```
  A **plain reset only** — not a flash — just to clear the stuck TCP state, then
  retry the `bootload.py` command normally. This is a recovery step, not a
  deviation from "flash over the network": the actual firmware update still goes
  through `bootload.py` once the board can be reached again.
- This recurred on two different boards in a row after ordinary repeated
  connection attempts over a long testing session (no deliberate flood) — worth
  flagging to the user if it happens rather than treating each occurrence as a
  one-off.

## 4. Why prefer this over `flash.bat`/pyOCD-SWD

- Exercises the project's own real update mechanism end-to-end (dual-bank swap,
  probation/rollback, environment preservation) instead of bypassing it.
- Needs no physical SWD/EDBG probe access to the specific board — only network
  reachability. Useful when boards are only reachable remotely, or when several
  need the same image and swapping probes/cables between them is the alternative.
- User preference on this project, confirmed 2026-09-07: default to `bootload`
  for any board that is currently network-reachable; reach for `flash.bat`/pyOCD
  only when it genuinely is not (unprovisioned, bricked, or the network stack
  itself is the thing under test).

## 5. Flashing several boards

There is no bulk/multi-board mode in `bootload.py` itself — call it once per
board IP, sequentially. Each call is independent (its own Telnet session, its own
commit/reboot/confirm cycle), so one board's failure (§3) does not require
restarting the others.
