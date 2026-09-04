#!/usr/bin/env python3
"""
Dual-bank firmware update over the network - the protocol side, without any GUI.

Sends a built .hex to a running board: control ('bootload arm', status, verify)
goes over the normal Telnet console on TCP/23, the image itself over the
firmware's own binary port (default 5567). See firmware/src/bootload.c for the
receiving end and docs/dual-bank-bootloader-plan.md for why it is split that way.

Deliberately standalone and importable: bridge_gui_telnet.py's Bootload button
is meant to be a thin wrapper around run_update() with a progress callback, so
the whole transfer can be brought up and debugged from a command line first,
with no Tk in the picture.

    python scripts/bootload.py --ip 192.168.0.12                 # send release\\...hex
    python scripts/bootload.py --ip 192.168.0.12 --hex some.hex
    python scripts/bootload.py --ip 192.168.0.12 --info          # just ask the board
    python scripts/bootload.py --ip 192.168.0.12 --selftest      # erase/write/read-back test

By default the update is activated: once the image is verified in the inactive
bank, 'bootload commit' copies the environment across, swaps the banks and
resets; this tool then waits for the board to come back, proves that what is now
running is what it sent, and confirms it - which is what keeps it. Without that
confirmation the board rolls itself back to the previous image, so an update that
comes up unreachable repairs itself. Pass --no-commit to stop after the verify,
--no-confirm to watch the rollback happen.

Standard library only (socket, zlib) - no new dependency for the GUI to inherit.
An Intel HEX parser is 30 lines and is right here rather than pulling in
intelhex, which is currently only present in .venv as a pyOCD dependency and is
not in scripts/requirements.txt.
"""

import argparse
import socket
import struct
import sys
import time
import zlib
from pathlib import Path

TELNET_PORT = 23
DATA_PORT = 5567
BL_MAGIC = 0x424C4452                      # 'BLDR', must match BL_MAGIC in bootload.c
BL_MAX_IMAGE = 0x7C000                     # one bank minus the emulated-EEPROM window
USER_PAGE_ADDR = 0x00804000                # fuses - never sent, only inspected

RELEASE_HEX = Path(__file__).parent.parent / "release" / "bridge_lan865x_100baseT.hex"

LOGIN_TIMEOUT = 10.0
REPLY_TIMEOUT = 5.0
REBOOT_TIMEOUT = 60.0      # bank swap + reset + stack/PHY coming back up


class BootloadError(Exception):
    """Anything that stops the update. The board is always left running its
    current firmware - there is no half-updated state this can leave behind."""


# ---------------------------------------------------------------------------
# Intel HEX
# ---------------------------------------------------------------------------

class Image:
    def __init__(self, data, crc, aux):
        self.data = data          # flat bytes, offset 0 == flash address 0
        self.size = len(data)
        self.crc = crc            # zlib.crc32, the same number the firmware computes
        self.aux = aux            # {address: bytes} for records outside the image window


def load_image(hex_path):
    """Flat binary image from address 0 to the highest code byte, gaps filled with
    0xFF (the erased-flash value, so a gap costs nothing to program).

    Records outside 0..BL_MAX_IMAGE are NOT part of the image and are returned
    separately: a release HEX for this project contains 12 bytes at 0x00804000,
    the NVM user page (BOD33, BOOTPROT, SmartEEPROM, WDT, region locks). That
    page lives in the auxiliary space, outside both flash banks, and needs
    different NVM commands. Nobody programs it: pyOCD skips that record when
    flashing this HEX over SWD as well ("no memory region defined for address
    0x00804000"), so dropping it here does not make the network path differ from
    the SWD path. What the device's fuses actually say is checked separately -
    see check_fuses()."""
    chunks = {}
    aux = {}
    base = 0
    top = 0
    for raw in Path(hex_path).read_text().splitlines():
        line = raw.strip()
        if not line.startswith(":"):
            continue
        rec = bytes.fromhex(line[1:])
        count, offset, rtype = rec[0], (rec[1] << 8) | rec[2], rec[3]
        payload = rec[4:4 + count]
        if rtype == 0x04:
            base = ((payload[0] << 8) | payload[1]) << 16
        elif rtype == 0x02:
            base = ((payload[0] << 8) | payload[1]) << 4
        elif rtype == 0x00:
            addr = base + offset
            if addr + count <= BL_MAX_IMAGE:
                chunks[addr] = payload
                top = max(top, addr + count)
            else:
                aux[addr] = payload
        elif rtype == 0x01:
            break

    if top == 0:
        raise BootloadError("%s contains no code below 0x%X" % (hex_path, BL_MAX_IMAGE))

    data = bytearray(b"\xFF" * top)
    for addr, payload in chunks.items():
        data[addr:addr + len(payload)] = payload
    return Image(bytes(data), zlib.crc32(bytes(data)) & 0xFFFFFFFF, aux)


# ---------------------------------------------------------------------------
# Telnet console
# ---------------------------------------------------------------------------

class Console:
    """Minimal synchronous Telnet client for the firmware's command console.

    Simpler than bridge_gui_telnet.py's TelnetLink (no reader thread, no
    terminal mirroring) and it can afford to be: every command sent from here
    answers with exactly one line that starts with a known marker, so there is
    no need for that file's prompt-settle heuristics. Login flow and prompt
    strings are the same, from firmware/src/config/default/library/tcpip/src/telnet.c.
    """

    def __init__(self, ip, user, password, port=TELNET_PORT):
        self.ip = ip
        self.user = user
        self.password = password
        self.port = port
        self.sock = None
        self.buf = b""

    def open(self, attempts=3):
        """Log in, retrying a connection the board drops straight away.

        The firmware allows two Telnet sessions (TCPIP_TELNET_MAX_CONNECTIONS)
        and reaps a closed one from its own task, which runs every 100 ms. A
        client that closes a session and immediately opens a new one - the GUI
        handing its console over to an update, for instance - can therefore land
        in the gap where the old session still counts, and the board accepts the
        TCP connection and closes it again without a prompt. Waiting a second
        and asking again is the whole fix."""
        for attempt in range(attempts):
            try:
                self._open_once()
                return
            except BootloadError:
                self.close()
                if attempt == attempts - 1:
                    raise
                time.sleep(1.0)

    def _open_once(self):
        self.buf = b""
        self.sock = socket.create_connection((self.ip, self.port), timeout=LOGIN_TIMEOUT)
        self.sock.settimeout(0.2)
        self._until([b"Login:"], LOGIN_TIMEOUT)
        self.sock.sendall(self.user.encode("latin-1") + b"\r\n")
        self._until([b"Password:"], LOGIN_TIMEOUT)
        self.sock.sendall(self.password.encode("latin-1") + b"\r\n")
        got = self._until([b"Logged in successfully", b"Access denied"], LOGIN_TIMEOUT)
        if b"Access denied" in got:
            raise BootloadError("Telnet login refused - check user/password")

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _until(self, markers, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(m in self.buf for m in markers):
                out, self.buf = self.buf, b""
                return out
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise BootloadError("Telnet connection closed by the board")
            self.buf += chunk
        raise BootloadError("no reply to a console command within %.0f s "
                            "(waited for %s)" % (timeout, b"/".join(markers).decode()))

    def command(self, cmd, markers=(b"BL: ",), timeout=REPLY_TIMEOUT):
        """Send one command, return the first line containing one of the markers."""
        self.buf = b""
        self.sock.sendall(cmd.encode("latin-1") + b"\r")
        text = self._until(list(markers), timeout).decode("latin-1", "ignore")
        for line in text.replace("\r", "\n").split("\n"):
            if any(m.decode() in line for m in markers):
                return line.strip()
        return text.strip()


def _peek(console, addr):
    reply = console.command("peek 0x%08X 4" % addr, markers=(b"0x%08X:" % addr,))
    return int(reply.split(":")[1].strip(), 16)


def check_fuses(console, image, log):
    """Check the DEVICE's fuses against what the bootloader assumes, and report -
    but do not act on - the HEX's own user-page record.

    Why it is not a comparison: measured on a board 2026-09-04, the HEX's user
    page (0x3C001239) and the device's (0xFFFF9239) differ in bits 25:15, the
    BOD12 factory calibration that Table 9-2 marks "do not change". The linker
    emits zeros there because it cannot know the per-die values. pyOCD refuses
    that record too ("no memory region defined for address 0x00804000"), so the
    SWD path does not program the user page either - the two update paths agree,
    and a mismatch here is normal, not a warning sign.

    What IS worth checking are the two fields that decide whether this update
    mechanism is allowed to work at all:

      BOOTPROT (word0 bits 29:26) - a protected boot section would make writes
                                    into the low addresses fail (STATUS.PROGE)
      SBLK     (word1 bits 3:0)   - SmartEEPROM reserves space at the end of
                                    both banks and makes BKSWRST reallocate it
    """
    for addr in image.aux:
        if not (USER_PAGE_ADDR <= addr < USER_PAGE_ADDR + 0x200):
            raise BootloadError("HEX contains data at 0x%08X, outside both the image "
                                "and the user page - refusing" % addr)

    word0 = _peek(console, USER_PAGE_ADDR)
    word1 = _peek(console, USER_PAGE_ADDR + 4)
    bootprot = (word0 >> 26) & 0xF
    sblk = word1 & 0xF
    log("user page : 0x%08X 0x%08X -> BOOTPROT=0x%X, SBLK=%d" % (word0, word1, bootprot, sblk))
    if bootprot != 0xF:
        raise BootloadError("BOOTPROT=0x%X: the first %d KiB of the main address space are "
                            "write protected. Clear BOOTPROT over SWD before using this path."
                            % (bootprot, (15 - bootprot) * 8))
    if sblk != 0:
        raise BootloadError("SBLK=%d: SmartEEPROM is enabled, which reserves space at the end "
                            "of both banks and changes what BKSWRST does. Not supported by "
                            "this updater." % sblk)


# ---------------------------------------------------------------------------
# The update itself
# ---------------------------------------------------------------------------

def _noop(*_args, **_kwargs):
    pass


def wait_for_reboot(ip, user, password, timeout=REBOOT_TIMEOUT, log=print):
    """Poll the Telnet console until the board is back after the bank swap.

    Reaching it under the SAME address is already the headline result: it means
    the environment hand-over copy worked and the new firmware did not fall back
    to the compiled-in default IP."""
    deadline = time.time() + timeout
    time.sleep(2.0)                     # the reset itself
    last = None
    while time.time() < deadline:
        try:
            console = Console(ip, user, password)
            console.open()
            log("board is back after %.0f s" % (timeout - (deadline - time.time())))
            return console
        except Exception as exc:        # refused, timed out, half-open stack
            last = exc
            time.sleep(1.0)
    raise BootloadError("board did not come back within %.0f s (last error: %s)"
                        % (timeout, last))


def run_update(ip, hex_path=RELEASE_HEX, user="admin", password="password",
               data_port=DATA_PORT, progress=_noop, log=print, cancel=lambda: False,
               fuse_check=True, commit=True, confirm=True):
    """The full update: prepare, arm, transfer, verify, commit, reboot, check.

    With commit=False it stops after the verify - the image sits in the inactive
    bank and the board keeps running what it was running, which is the safe way
    to exercise the transfer path on its own.

    progress(phase_no, phase_name, done, total) is called as the transfer runs;
    'done'/'total' are bytes during the transfer and 0/0 otherwise. cancel() is
    polled between socket writes - returning True aborts cleanly ('bootload
    abort' on the console, nothing left half-written that anything boots from).

    Returns a dict with size, crc and the board's closing line. Raises
    BootloadError on anything else."""
    image = load_image(hex_path)
    progress(1, "prepare", 0, 0)
    log("image  : %s" % hex_path)
    log("size   : %d bytes (%.1f KiB, %.0f%% of one bank), crc32=0x%08X"
        % (image.size, image.size / 1024.0, 100.0 * image.size / BL_MAX_IMAGE, image.crc))
    if image.size > BL_MAX_IMAGE:
        raise BootloadError("image is %d bytes, the per-bank limit is %d"
                            % (image.size, BL_MAX_IMAGE))

    console = Console(ip, user, password)
    console.open()
    try:
        log(console.command("bootload"))
        if fuse_check:
            check_fuses(console, image, log)

        progress(2, "arm", 0, 0)
        reply = console.command("bootload arm %d %08X" % (image.size, image.crc))
        log(reply)
        if "READY" not in reply:
            raise BootloadError("board did not arm: %s" % reply)

        progress(3, "transfer", 0, image.size)
        header = struct.pack("<III", BL_MAGIC, image.size, image.crc)
        header += struct.pack("<I", zlib.crc32(header) & 0xFFFFFFFF)

        data_sock = socket.create_connection((ip, data_port), timeout=10.0)
        try:
            data_sock.sendall(header)
            sent = 0
            step = 4096
            t0 = time.time()
            while sent < image.size:
                if cancel():
                    raise BootloadError("cancelled")
                chunk = image.data[sent:sent + step]
                data_sock.sendall(chunk)
                sent += len(chunk)
                progress(3, "transfer", sent, image.size)
            elapsed = max(time.time() - t0, 1e-6)
            log("transferred %d bytes in %.1f s (%.0f kB/s)"
                % (sent, elapsed, sent / 1024.0 / elapsed))

            progress(4, "verify", 0, 0)
            data_sock.settimeout(30.0)          # the board CRCs the whole image first
            closing = b""
            while b"\n" not in closing:
                part = data_sock.recv(256)
                if not part:
                    break
                closing += part
        finally:
            data_sock.close()

        closing = closing.decode("latin-1", "ignore").strip()
        log(closing or "(board closed the data connection without a reply)")
        if not closing.startswith("BL: OK"):
            raise BootloadError("verify failed: %s" % (closing or "no reply"))

        log(console.command("bootload"))
        if not commit:
            return {"size": image.size, "crc": image.crc, "reply": closing,
                    "committed": False}

        # Phase 5: activate. The board answers first and swaps half a second
        # later, so this reply still gets out; then the connection dies with the
        # reset, which is expected and not an error.
        progress(5, "commit", 0, 0)
        reply = console.command("bootload commit")
        log(reply)
        if "COMMIT" not in reply:
            raise BootloadError("commit refused: %s" % reply)
        console.close()

        progress(6, "reboot", 0, 0)
        console = wait_for_reboot(ip, user, password, log=log)

        # Phase 7: prove that what is now RUNNING is what we sent, and that the
        # environment came across.
        progress(7, "check", 0, 0)
        running = console.command("bootload verify %d %08X" % (image.size, image.crc))
        log(running)
        log(console.command("bootload info", markers=(b"running bank",), timeout=5.0))
        log(console.command("showenv", markers=(b"eth1  ip",), timeout=5.0))
        if "match=1" not in running:
            raise BootloadError("the running image is NOT the one that was sent: %s" % running)

        # The board put this boot on probation before it swapped: if nobody who
        # can actually reach it over the network says so within the probation
        # window, it swaps back to the previous image on its own. Having just
        # logged in and verified what is running IS that confirmation.
        if confirm:
            log(console.command("bootload confirm"))
        else:
            log("NOT confirming (--no-confirm): the board rolls back by itself "
                "when the probation window expires")
        return {"size": image.size, "crc": image.crc, "reply": closing,
                "committed": True, "running": running}
    except BaseException:
        try:
            console.command("bootload abort", markers=(b"BL: ",), timeout=2.0)
        except Exception:
            pass
        raise
    finally:
        console.close()


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _bar(done, total, width=40):
    filled = int(width * done / total) if total else 0
    return "[%s%s] %3.0f%%" % ("#" * filled, "-" * (width - filled),
                               100.0 * done / total if total else 0.0)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ip", required=True, help="board IP address")
    ap.add_argument("--hex", default=str(RELEASE_HEX), help="HEX file to send")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="password")
    ap.add_argument("--port", type=int, default=DATA_PORT, help="image data port")
    ap.add_argument("--info", action="store_true", help="print 'bootload info' and exit")
    ap.add_argument("--selftest", action="store_true",
                    help="run 'bootload selftest' (erase+write+read-back in the inactive bank) and exit")
    ap.add_argument("--no-fuse-check", action="store_true",
                    help="skip the BOOTPROT/SmartEEPROM fuse check")
    ap.add_argument("--no-commit", action="store_true",
                    help="transfer and verify only - do not swap banks, do not reset")
    ap.add_argument("--no-confirm", action="store_true",
                    help="skip the probation confirmation, so the board rolls the update "
                         "back on its own - for testing that path")
    args = ap.parse_args(argv)

    try:
        if args.info or args.selftest:
            console = Console(args.ip, args.user, args.password)
            console.open()
            try:
                cmd = "bootload selftest" if args.selftest else "bootload info"
                # Both answer with several lines; selftest also blocks the board for
                # up to ~250 ms while the block erase runs (tFEB, see the plan).
                print(console.command(cmd, markers=(b"BL: ",), timeout=15.0))
            finally:
                console.close()
            return 0

        state = {"phase": 0}

        def on_progress(no, name, done, total):
            if no == 3 and total:
                sys.stdout.write("\r  %-9s %s %d/%d" % (name, _bar(done, total), done, total))
                sys.stdout.flush()
                if done >= total:
                    sys.stdout.write("\n")
            elif no != state["phase"]:
                print("phase %d: %s" % (no, name))
            state["phase"] = no

        result = run_update(args.ip, args.hex, args.user, args.password,
                            data_port=args.port, progress=on_progress,
                            fuse_check=not args.no_fuse_check,
                            commit=not args.no_commit,
                            confirm=not args.no_confirm)
        if result["committed"]:
            print("\nOK - the board is running the new image "
                  "(%d bytes, crc=0x%08X) and kept its environment."
                  % (result["size"], result["crc"]))
        else:
            print("\nOK - %d bytes verified in the inactive bank (crc=0x%08X)."
                  % (result["size"], result["crc"]))
            print("Not activated (--no-commit): the board keeps running its current image.")
        return 0
    except BootloadError as exc:
        print("\nFAILED: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
