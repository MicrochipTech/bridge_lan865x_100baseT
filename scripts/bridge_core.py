#!/usr/bin/env python3
"""
Shared core of the Telnet bridge tools - everything that is NOT a user interface.

Extracted from bridge_gui_telnet.py when the web front end (bridge_web_telnet.py)
was added, for one reason: the connection layer and the command protocol must not
exist twice. The timing rules in CommandChannel.send() in particular were measured
against the real board over several sessions (see the comments there); a second,
independently drifting copy in a second front end would be the single most reliable
way to lose them.

What lives here:
  * TelnetLink     - the TLS/Telnet connection with its reader thread
  * CommandChannel - one open link plus the arbitration that keeps the terminal
                     consumer and the command consumer from eating each other's bytes
  * Screen         - byte stream to text, for front ends without a real terminal
                     emulator (Tk). The web front end does not need it: xterm.js
                     does that job properly and completely.
  * the model helpers (env_model.json / lan8651_model.json), all pure functions

What deliberately does NOT live here: anything that opens a window, asks a
question or shows a message. A missing model file is reported by RAISING here;
how that is presented - messagebox or a red line in a browser - is the front
end's business.

Standard library only, so the command line tools (bootload.py, cli.py) can use it
without pulling in a GUI toolkit or a web framework.
"""

import json
import os
import queue
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths. All of them are relative to the repo root, i.e. one level above this
# script's directory - same as in bridge_gui_telnet.py, from where they came.
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parent.parent

# TLS bring-up (branch t1s-t1s-bridge-lan8670): the firmware's Telnet port now
# requires a TLS handshake with mutual certificate auth (net_pres_enc_glue.c) -
# this is this project's own CA/client identity (certs/ca/ + certs/default/,
# repo root), generated with openssl, NOT wolfSSL's public test certs. See
# pki.py's module docstring for why the CA lives in its own directory
# (certs/ca/, separate from any leaf identity) and the session log for why a
# project CA exists at all: wolfSSL's canned test PKI is both expired and
# internally mismatched (its "client" cert doesn't even chain to its own "CA"
# cert).
TLS_CA_CERT = _ROOT / "certs" / "ca" / "ca_cert.pem"
TLS_CLIENT_CERT = _ROOT / "certs" / "client" / "client_cert.pem"
TLS_CLIENT_KEY = _ROOT / "certs" / "client" / "client_key.pem"

# The register model: addresses, mnemonics, bit fields, origin. Kept separate from
# the configuration because it's a different kind of thing -- a reference derived
# from the datasheet that someone tracking down a bug relies on. The front ends only
# read it, NEVER write it; values and session state belong in the config file.
# If something is wrong here, fix that file, not the source code -- afterwards run
# "python scripts\check_register_model.py".
MODEL_FILE = _ROOT / "json" / "lan8651_model.json"

# The environment model: which fields the EEPROM record has, how they are read from
# showenv and which CLI command writes them -- per identity and version.
# Firmware variants share the EEPROM offset but not the layout; that's why the
# identity is read from the device and matched against this model instead of guessed.
ENV_MODEL_FILE = _ROOT / "json" / "env_model.json"

# Connection fields and session state. Shared by BOTH front ends on purpose: the
# IP and credentials you last connected with should be there no matter which of
# the two you start. Deliberately separate from bridge_config.json (bridge_gui.py's
# own file), which continues to belong exclusively to the serial GUI.
CONFIG_FILE = _ROOT / "json" / "bridge_gui_telnet_config.json"

# The tracked release HEX build.bat refreshes after every successful build - the
# default for "Flash" and for the network update, so a fresh clone can flash
# without building first. Deliberately not the dist\ build output: that only
# exists after a local build, and picking it as the default would silently flash
# a stale or never-built path on a fresh clone.
RELEASE_HEX = _ROOT / "release" / "bridge_lan865x_100baseT.hex"
FLASH_SAME54_SCRIPT = _ROOT / "scripts" / "flash_same54.py"

# Flash/RAM totals for the "Memory Overview" command: build.bat's own
# scripts/build_summary.py writes this after every local build (xc32-bin2hex's
# --memorysummary output). Only reflects the LAST LOCAL BUILD, not necessarily
# what is flashed on the connected board right now - the overview says so
# explicitly rather than implying it read the device's own flash usage (there is
# no CLI command for that; meminfo only covers RAM).
MEMORYFILE_XML = (_ROOT / "firmware" / "tcpip_iperf_lan865x.X"
                  / "dist" / "default" / "production" / "memoryfile.xml")

# Typed into the erase confirmation prompt, not just clicked - a chip erase is not
# reversible (wipes firmware AND the emulated EEPROM, both live in the same flash).
ERASE_CONFIRM_WORD = "ERASE"

# ---------------------------------------------------------------------------
# Connection constants
# ---------------------------------------------------------------------------

MAX_VIEW_LINES = 1000
TELNET_PORT = 23
TELNET_CONNECT_TIMEOUT = 5.0   # TCP connect timeout, seconds
TELNET_LOGIN_TIMEOUT = 5.0     # waiting for Login:/Password:/Logged in.../Access denied, seconds

# Fallback values if bridge_gui_telnet_config.json is missing. The register tab then
# does NOT come along -- it's derived from the datasheet (LAN8650-1-Data-Sheet-60001734.pdf,
# chapter 11, 182 registers) and lives exclusively in that configuration file.
# ip/telnet_user/telnet_password are the defaults for this board (192.168.0.12,
# admin/password) - changeable to a different board at any time by editing the fields
# and connecting; the front end remembers whatever was last used to connect.
DEFAULT_CONFIG = {
    "ip": "192.168.0.12",
    "telnet_user": "admin",
    "telnet_password": "password",
    "bridge": {
        "ip0": "192.168.0.11",
        "mask0": "255.255.255.0",
        "gw0": "192.168.0.1",
        "dns0": "192.168.0.1",
        "ip1": "192.168.0.12",
        "mask1": "255.255.255.0",
        "gw1": "192.168.0.1",
        "dns1": "192.168.0.1",
        "mac0": "00:04:25:00:00:00",
        "mac1": "00:04:25:00:00:01",
        "plca_id": 5,
        "plca_cnt": 8,
        "mirror": 0,
    },
    "values": {},
}


class Screen:
    """Byte-to-text converter (aus gui_term.py).

    Only front ends WITHOUT a real terminal emulator need this: Tk has no such
    widget, so the escape sequences have to be resolved into plain text here.
    bridge_web_telnet.py does not use it - xterm.js does that job in the browser,
    including everything this class silently drops.
    """

    def __init__(self, max_lines=MAX_VIEW_LINES):
        self.max_lines = max_lines
        self.lines = []
        self.total = 0
        self.cur = ""
        self.col = 0
        self._esc = None

    def feed(self, data):
        """Feed raw bytes and convert to screen state"""
        for b in data:
            if self._esc is not None:
                self._esc.append(b)
                if len(self._esc) == 1:
                    if b not in (0x5B, 0x4F):
                        self._esc = None
                    continue
                if 0x40 <= b <= 0x7E:
                    self._sequence(bytes(self._esc))
                    self._esc = None
                continue
            if b == 0x1B:
                self._esc = bytearray()
            elif b == 0x0D:
                self.col = 0
            elif b == 0x0A:
                self._newline()
            elif b == 0x08:
                self.col = max(0, self.col - 1)
            elif b == 0x09:
                self._put(" " * (8 - (self.col % 8)))
            elif 0x20 <= b <= 0xFF and b != 0x7F:
                self._put(chr(b))

    def _sequence(self, seq):
        """Handle escape sequences"""
        final = seq[-1:]
        body = seq[1:-1]
        if final == b"K":
            if body in (b"", b"0"):
                self.cur = self.cur[:self.col]
            elif body == b"1":
                self.cur = " " * self.col + self.cur[self.col:]
            elif body == b"2":
                self.cur = ""
                self.col = 0

    def _put(self, s):
        if self.col > len(self.cur):
            self.cur += " " * (self.col - len(self.cur))
        self.cur = self.cur[:self.col] + s + self.cur[self.col + len(s):]
        self.col += len(s)

    def _newline(self):
        self.lines.append(self.cur)
        self.total += 1
        self.cur = ""
        self.col = 0
        if len(self.lines) > self.max_lines:
            del self.lines[:len(self.lines) - self.max_lines]

    def text(self):
        return "".join(line + "\n" for line in self.lines) + self.cur


def _wrap_telnet_tls(sock, host):
    """Upgrade a freshly connected plain socket to TLS with mutual-cert auth,
    matching the firmware's net_pres_enc_glue.c (branch t1s-t1s-bridge-lan8670).
    Raises (ssl.SSLError, FileNotFoundError, ...) on failure - the caller closes
    the socket and propagates.

    check_hostname is off on purpose: the server cert's CN ("bridge-server",
    certs/default/server_cert.pem) is a fixed name picked at cert-generation
    time, not the board's IP - there is no DNS/mDNS name here to match against.
    The server's identity is still verified (CERT_REQUIRED, against our CA),
    just not by hostname.
    """
    # Checked explicitly, and this is the whole reason bootload.py shares this
    # function instead of keeping its own copy: OpenSSL's own failure for a
    # missing cert file is a FileNotFoundError with NO filename on it, so the
    # caller has nothing to report but "[Errno 2] No such file or directory".
    for path in (TLS_CA_CERT, TLS_CLIENT_CERT, TLS_CLIENT_KEY):
        if not path.is_file():
            raise FileNotFoundError(
                "missing TLS file: %s - the CA and the operator client identity are "
                "generated per checkout, not shipped (docs/pki-clean-start.md). "
                "Create them on the Certificates tab (\"CA Status\", then \"Create "
                "Operator Client Identity\") or with: python scripts\\pki.py" % path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile=str(TLS_CA_CERT))
    ctx.load_cert_chain(certfile=str(TLS_CLIENT_CERT), keyfile=str(TLS_CLIENT_KEY))
    # Confirmed live against the real board: without this, OpenSSL 3.x aborts
    # the handshake with "UNSAFE_LEGACY_RENEGOTIATION_DISABLED" - the firmware's
    # wolfSSL build doesn't send the (RFC 5746) renegotiation_info extension,
    # which OpenSSL 3.x's default policy otherwise insists on. Not a client bug
    # to route around blindly - just this embedded TLS stack not implementing an
    # extension a modern desktop TLS library assumes.
    ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
    # Whatever timeout the caller put on the socket is left alone. TelnetLink
    # below already creates it with TELNET_CONNECT_TIMEOUT, so setting it again
    # here was a no-op for the only caller this function had - but bootload.py
    # now shares it, and its two call sites open with 10 s deliberately (the
    # console login, and the image data port). Re-imposing 5 s here would have
    # silently shortened both.
    return ctx.wrap_socket(sock, server_hostname=host)


def missing_tls_files() -> List[Path]:
    """Which of the three files the TLS handshake needs are not there.

    The CA is checked out or created once; the operator client identity
    (certs/client/) is generated per checkout and NOT shipped, so on a fresh
    clone it is the one that is missing - and every tool that opens a console
    then fails inside OpenSSL, which does not put the filename in the error.
    """
    return [p for p in (TLS_CA_CERT, TLS_CLIENT_CERT, TLS_CLIENT_KEY) if not p.is_file()]


def tls_identity_problem() -> Optional[str]:
    """A ready-to-show sentence if the client identity is incomplete, else None.

    Worth checking BEFORE opening a console rather than letting the handshake
    fail: bootload.Console retries three times over ~12 seconds first, and what
    comes out the other end is a bare "[Errno 2] No such file or directory" with
    no path in it - OpenSSL does not set the filename on that exception. Naming
    the file and the button that creates it turns a dead end into one click.
    """
    missing = missing_tls_files()
    if not missing:
        return None
    names = ", ".join(str(p) for p in missing)
    if TLS_CA_CERT in missing:
        return (f"No project CA yet - missing: {names}. Create it on the "
                f"Certificates tab with \"CA Status\", which also issues the "
                f"operator client identity.")
    return (f"No operator client identity yet - missing: {names}. This is "
            f"generated per checkout and not shipped in the repo. Create it on "
            f"the Certificates tab with \"Create Operator Client Identity\"; "
            f"the boards trust the CA, not this certificate, so nothing has to "
            f"be reprovisioned afterwards.")


class TelnetLink:
    """Telnet connection with a reader thread - same interface as the serial
    Link from bridge_gui.py (open()/write()/close(), bytes delivered over the same
    queue as (self.port, "data"/"lost", payload)), so the front ends stay
    unchanged. Runs the login (Login:/Password: prompt) synchronously in open(),
    before the reader thread starts - after that it's all normal command/terminal
    traffic, exactly like the serial original.

    Prompt strings and flow come from firmware/src/config/default/library/tcpip/
    src/telnet.c (TELNET_START_MSG, TELNET_ASK_PASSWORD_MSG, TELNET_FAIL_LOGON_MSG,
    TELNET_LOGON_OK): there a line ends on the first CR OR LF in the buffer, a
    complete "user\\r\\n"/"pass\\r\\n" sent at once is enough, no need to send
    character by character (unlike the later command prompt, the SYS_CMD editor -
    but that too handles a whole "<cmd>\\r" in one go without issue, see
    CommandChannel.send()).
    """

    def __init__(self, host, q, telnet_port=TELNET_PORT, username="", password=""):
        self.host = host
        self.port = host  # Display name for status lines ("Connected to {link.port}")
        self.telnet_port = telnet_port
        self.username = username
        self.password = password
        self.q = q
        self.sock = None
        self.stop = threading.Event()
        self.thread = None

    def open(self):
        sock = socket.create_connection((self.host, self.telnet_port),
                                        timeout=TELNET_CONNECT_TIMEOUT)
        try:
            sock = _wrap_telnet_tls(sock, self.host)
        except Exception:
            sock.close()
            raise
        sock.settimeout(0.2)
        self.sock = sock
        try:
            leftover = self._login()
        except Exception:
            sock.close()
            self.sock = None
            raise
        self.sock.settimeout(0.05)
        if leftover:
            self.q.put((self.port, "data", leftover))
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _recv_until(self, markers, overall_timeout=TELNET_LOGIN_TIMEOUT):
        """Collect bytes until one of the markers (bytes) shows up in the buffer - or
        raise TimeoutError if the device doesn't respond within overall_timeout seconds."""
        buf = b""
        deadline = time.time() + overall_timeout
        while time.time() < deadline:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("connection closed during login")
            buf += chunk
            if any(m in buf for m in markers):
                return buf
        raise TimeoutError("no response from %s (expected %s)" %
                           (self.host, b"/".join(markers).decode("latin-1")))

    def _login(self):
        """Login:/Password: dialog. Returns whatever already arrived after 'Logged in
        successfully' (e.g. the start of the welcome message) - that ends up as a
        perfectly normal first "data" message in the queue, instead of being
        discarded."""
        self._recv_until([b"Login:"])
        self.sock.sendall(self.username.encode("latin-1", "ignore") + b"\r\n")
        self._recv_until([b"Password:"])
        self.sock.sendall(self.password.encode("latin-1", "ignore") + b"\r\n")
        buf = self._recv_until([b"Logged in successfully", b"Access denied"])
        if b"Access denied" in buf:
            raise PermissionError("Access denied - check user/password")
        marker = b"Logged in successfully"
        return buf[buf.find(marker) + len(marker):]

    def _read(self):
        while not self.stop.is_set():
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError as error:
                if not self.stop.is_set():
                    self.q.put((self.port, "lost", str(error)))
                return
            if not data:
                if not self.stop.is_set():
                    self.q.put((self.port, "lost", "connection closed by device"))
                return
            self.q.put((self.port, "data", data))

    def write(self, data):
        if self.sock is None:
            raise OSError("not connected")
        self.sock.sendall(data)

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=0.5)
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None


class CommandChannel:
    """The one open link plus the arbitration around it.

    Two consumers want the device's bytes: the terminal, which shows everything,
    and whoever is currently running a command and waiting for its response. They
    must not read the same chunks away from each other, which is what `pending`
    and `response_q` are for: the front end's queue pump hands every "data" tuple
    to the terminal AND - only while a command is in flight - to dispatch(), which
    copies it into response_q for send().

    Front ends own an instance of this instead of the four separate primitives
    they used to keep; `link` is None whenever nothing is connected.
    """

    def __init__(self):
        self.link: Optional[TelnetLink] = None
        self.response_q = queue.Queue()
        self.pending = threading.Event()
        self.lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self.link is not None

    def dispatch(self, result) -> None:
        """Hand one ("port", "data", payload) tuple to the waiting command, if any.

        Called from the front end's queue pump for every data tuple, AFTER it has
        given the terminal its own copy. Does nothing while no command is running -
        that is the point: chunks that arrive between commands belong to the
        terminal alone and must not pile up in response_q, where the NEXT command
        would find them and mistake them for its own response.
        """
        if self.pending.is_set():
            self.response_q.put(result)

    def send(self, cmd: str, timeout_ms: int = 700) -> str:
        """Send a command over the open link and wait for the response.

        Runs on a worker thread. The response arrives via response_q, which is
        only filled while `pending` is set -- so the terminal consumer on the main
        thread doesn't read away the same chunks.
        """
        if not self.link:
            return "ERROR: Not connected"

        # Only one command at a time, otherwise two responses get mixed together.
        with self.lock:
            # Discard any leftovers, otherwise the previous command's response lands here.
            while True:
                try:
                    self.response_q.get_nowait()
                except queue.Empty:
                    break

            self.pending.set()
            try:
                self.link.write(cmd.encode() + b"\r")

                start = time.time()
                chunks = []
                # Set the moment the response looks done (own echo + a trailing "> " on
                # its own line) but not yet trusted - see below for why it needs a
                # settle window instead of returning immediately.
                prompt_seen_at = None
                PROMPT_SETTLE_S = 0.15

                while time.time() - start < timeout_ms / 1000.0:
                    try:
                        port, kind, payload = self.response_q.get(timeout=0.01)
                    except queue.Empty:
                        # Nothing new since the trailing prompt appeared - if that's
                        # been true for the whole settle window, it really is done.
                        if prompt_seen_at is not None and time.time() - prompt_seen_at >= PROMPT_SETTLE_S:
                            return "".join(chunks)
                        continue

                    if kind != "data":
                        continue
                    chunks.append(payload.decode("latin-1", "ignore"))
                    text = "".join(chunks)
                    # Done once the marker is there AND the line is complete -- an
                    # "OK:" without a line ending is only the beginning. Always takes
                    # priority over the prompt-based check below, and is why
                    # 'lan_read'/'lan_write' resolve almost instantly despite needing
                    # the settle window (see below): their "OK:"/"ERROR" line lands
                    # a few ms after the prompt, well inside PROMPT_SETTLE_S, so this
                    # fires first on the very next chunk.
                    for marker in ("OK:", "ERROR"):
                        pos = text.find(marker)
                        if pos >= 0 and "\n" in text[pos:]:
                            return text
                    # The CLI re-printing its "> " prompt on its own line is the
                    # definitive end-of-response marker for a command whose own output
                    # never contains "OK:"/"ERROR" (namely 'showenv') - independent of
                    # what the command actually prints, unlike the marker check above.
                    # This replaces an earlier idle-based cutoff (return once ~40ms
                    # passed with no new chunk) that was wrong over Telnet: 'showenv'
                    # legitimately arrives in two TCP segments, with the eth0/eth1/mac/
                    # plca/mirror/sniffer lines up to a full second behind the identity
                    # line - the idle cutoff fired in that gap and truncated the
                    # response to the identity line alone, which then made every field
                    # in the env parser come back unmatched (found stays 0) - the
                    # "Command failed" dialog on Read Environment against
                    # 192.168.0.21/.32, confirmed 2026-09-04.
                    #
                    # 'cmd in text' guards against a DIFFERENT race this introduced: a
                    # rapid back-to-back loop (bulk register reads, one lan_read per
                    # register) can still have the PREVIOUS command's own trailing ">"
                    # in flight when this command's pending flag goes up - that stray
                    # byte alone would satisfy "last line is '>'" before this command's
                    # own echo has even arrived. A genuine completion always contains
                    # this command's own echo.
                    #
                    # And why this can't just return immediately, unlike the fixes
                    # above: measured directly against the board (2026-09-04), this
                    # firmware's console prints the fresh "> " prompt right after
                    # echoing the command line - BEFORE dispatching to the command
                    # handler - so for 'lan_read'/'lan_write' the prompt reliably
                    # arrives a few ms BEFORE the real "LAN865X ... OK: ... Value=..."
                    # line, not after. Trusting it immediately (as an earlier version of
                    # this fix did) returned the bare echo+prompt with no value at all,
                    # near-100% of the time in bulk register reads - far worse than the
                    # original bug. The settle window lets the marker check above win
                    # the race on the next chunk when there is one; only a command like
                    # 'showenv', whose trailing prompt really is the last thing sent,
                    # ever actually waits out PROMPT_SETTLE_S.
                    last_line = text.replace("\r", "").rstrip("\n").rsplit("\n", 1)[-1].strip()
                    if last_line == ">" and cmd in text:
                        if prompt_seen_at is None:
                            prompt_seen_at = time.time()
                    else:
                        prompt_seen_at = None

                return "".join(chunks)
            finally:
                self.pending.clear()


class ResponseParser:
    """Evaluates the device's response lines.

    Like the serial original (bridge_gui.py): no separate subprocess per
    command, all commands run over the single already-open link
    (CommandChannel.send); parsing is all that's left to do.
    """

    def parse_register_read(self, output: str) -> Optional[str]:
        """Extract the value from 'LAN865X Read OK: Addr=... Value=...'."""
        match = re.search(r'Value=0x([0-9A-Fa-f]+)', output)
        if match:
            return "0x" + match.group(1)
        return None


def clean_response(command: str, output: str) -> str:
    """Strip the command echo and prompt characters from the response."""
    lines = []
    for raw in output.replace("\r", "\n").split("\n"):
        line = raw.strip().lstrip(">").strip()
        if not line or line == command.strip():
            continue
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Model and config files. load_json_model RAISES - deliberately: what a missing
# model file should look like (a modal dialog, a red line on the page) is the
# front end's decision, and the two front ends answer it differently.
# ---------------------------------------------------------------------------

def load_json_model(path: Path) -> dict:
    """Read one of the two model files. Propagates FileNotFoundError and ValueError."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_config() -> dict:
    """Load the shared connection/session config, or the defaults if it's missing.

    encoding="utf-8" is not optional here. save_config() writes UTF-8; without an
    explicit encoding this read takes the Windows default (cp1252), so the three
    bytes of a typographic apostrophe come back as three separate characters and
    the next save writes THOSE as UTF-8. The damage therefore compounds with every
    round trip - "Manufacturer's" turning into ever longer garbage - and nothing
    ever reports an error, because every intermediate file is valid JSON.
    """
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return DEFAULT_CONFIG.copy()


def save_config(config: dict) -> None:
    """Write the configuration -- serialize first, then replace.

    open(..., 'w') empties the file the moment it's opened; if json.dump then
    fails, the register tab is gone. So convert to bytes first (an error is then
    raised before anything gets touched), write to a neighboring file, and only
    swap it in via os.replace at the very end.
    """
    data = json.dumps(config, indent=2, ensure_ascii=False).encode("utf-8")
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, CONFIG_FILE)


def env_entry_for(env_model: dict, identity: Optional[dict]) -> dict:
    """Model entry for ONE identity -- without consulting any front end state.

    A worker thread needs this: it has just pulled the identity out of that same
    showenv output, but the front end's own `identity` is only updated on the UI
    thread. Interpreting the values against the OLD state would show filled-in
    fields under a line saying they aren't interpreted.
    """
    envs = env_model.get("environments", {})
    if not envs:
        return {}
    if not identity:
        return next(iter(envs.values()))
    # Match on the FIRMWARE's own identity, not the stored EEPROM record's: showenv
    # always prints the value lines from its own current struct (the loaded record if
    # valid, the compiled defaults otherwise) - never raw bytes in a foreign layout.
    # firmware_id/version therefore always describes what layout those lines are in,
    # while eeprom_id/version/crc is purely "what was found at boot" diagnostic info.
    # Keying on eeprom_id instead would refuse to interpret a perfectly valid set of
    # values whenever the stored record is blank/corrupt (e.g. a freshly erased chip
    # after a full MPLAB X program) - exactly the case "Write Environment" exists to fix.
    for found_id, found_ver in (
            (identity.get("firmware_id"), str(identity.get("firmware_version"))),
            (identity.get("eeprom_id"), str(identity.get("eeprom_version")))):
        for env in envs.values():
            if str(env.get("version")) != found_ver:
                continue
            if found_id == env.get("id") or found_id in env.get("accepts_ids", []):
                return env
    return {}


def env_entry(env_model: dict, identity: Optional[dict]) -> dict:
    """The model entry a front end is currently working against.

    As long as the device hasn't reported anything, this is the only or first
    entry -- the fields have to be built before anyone connects, after all. Once
    the identity has been read, the matching entry is used; if there isn't one,
    the result is empty and the front end shows the values as not interpretable,
    instead of making them up.
    """
    envs = env_model.get("environments", {})
    if not envs:
        return {}
    if identity:
        return env_entry_for(env_model, identity)
    return next(iter(envs.values()))


def env_identity_line(env_model: dict, identity: Optional[dict]) -> str:
    """The line above the parameter fields: what's in the EEPROM and whether we
    can interpret it. env_identity_ok() answers the same question as a bool, for
    whatever the front end colors."""
    if not env_model:
        return "no environment model loaded"
    if not identity:
        envs = ", ".join(env_model.get("environments", {}))
        return f"Environment: not read yet - the model knows {envs}. 'Read Environment' asks the device."
    ee = f"{identity.get('eeprom_id')} v{identity.get('eeprom_version')}"
    fw = f"{identity.get('firmware_id')} v{identity.get('firmware_version')}"
    crc = identity.get("eeprom_crc", "?")
    entry = env_entry(env_model, identity)
    if entry:
        if identity.get("eeprom_id") == identity.get("firmware_id") and crc == "ok":
            note = "model fits"
        elif crc == "ok":
            # Not an error: the firmware still reads this legacy identity and has
            # accepted the record -- otherwise there would be no model entry here.
            # On the next saveenv it writes it back with the new identity.
            note = (f"legacy id, accepted by the firmware - the next "
                    f"'{entry.get('commands', {}).get('persist', 'saveenv')}' "
                    f"rewrites it as {identity.get('firmware_id')}")
        else:
            # No record the firmware trusts was found at boot (blank/erased EEPROM -
            # e.g. right after a full chip program - or a foreign/corrupt record), so
            # it fell back to its compiled defaults. Those defaults ARE in this
            # firmware's own current layout, so the values below are still real and
            # safe to read/write - only 'Write Environment' + saveenv is missing to
            # make them survive a reset.
            note = ("no valid record found at boot - showing the firmware's compiled "
                    "defaults. 'Write Environment' persists them.")
        return (f"Environment: EEPROM {ee} (crc {crc}) | Firmware {fw} "
                f"{identity.get('firmware_variant', '')} - {note}")
    return (f"WARNING: the EEPROM reports {ee}, which this tool has no model for. "
            f"The values below are NOT interpreted. Firmware {fw} "
            f"{identity.get('firmware_variant', '')}")


def env_identity_ok(env_model: dict, identity: Optional[dict]) -> bool:
    """Whether the identity line above describes a situation the tool understands.
    Gray when true, red when not."""
    if not env_model or not identity:
        return True
    return bool(env_entry(env_model, identity))


def model_source_line(model: dict) -> str:
    """A one-liner about the provenance, shown by the front end -- so it doesn't
    claim more than it can back up."""
    if not model:
        return "no register model loaded"
    ds = model.get("sources", {}).get("datasheet", {})
    er = model.get("sources", {}).get("errata", {})
    ver = model.get("verification", {})
    n_reg = sum(len(g.get("registers", {})) for g in model.get("groups", {}).values())
    n_ver = ver.get("registers_verified", 0)
    line = (f"Model: {ds.get('doc', '?')} ({ds.get('date', '?')}), Chapter "
            f"{ds.get('chapter', '?')} - {n_reg} registers, {n_ver} checked against "
            f"the document")
    if er:
        line += f" | Errata {er.get('doc')}"
    return line


def bridge_write_command(entry: dict, key: str, value: str) -> Optional[str]:
    """Field name -> CLI command, both from the model.

    Neither the key nor the command form live in the source code anymore:
    'commands.write_field' and 'cli_key' come from env_model.json, so a different
    firmware variant only needs a different model file and no patch here.
    """
    fld = entry.get("fields", {}).get(key)
    if not fld:
        return None
    template = entry.get("commands", {}).get("write_field", "setenv {cli_key} {value}")
    return template.format(cli_key=fld["cli_key"], value=value)


def parse_showenv(entry: dict, output: str, key: str) -> Optional[str]:
    """Extract one value from the showenv output -- using the pattern from the model.

    'reads_as' maps the device's display value to the value setenv expects
    (mirror reports ON/OFF, what gets written is 1/0). Without it, a front end
    would show a word that can't be written back.
    """
    fld = entry.get("fields", {}).get(key)
    if not fld or not fld.get("pattern"):
        return None
    m = re.search(fld["pattern"], output)
    if not m:
        return None
    raw = m.group(1)
    return fld.get("reads_as", {}).get(raw, raw)


def parse_env_identity(env_model: dict, output: str) -> Optional[dict]:
    """Die Kennungszeile von showenv auswerten (Muster: env_model.json 'identity')."""
    ident = env_model.get("identity", {})
    if not ident.get("pattern"):
        return None
    m = re.search(ident["pattern"], output)
    if not m:
        return None
    return dict(zip(ident.get("groups", []), m.groups()))


# ---------------------------------------------------------------------------
# Registers
# ---------------------------------------------------------------------------

def register_view(model: dict) -> Tuple[Dict[str, List[str]], Dict[str, dict]]:
    """Flatten the register model into what a front end needs to draw it.

    Returns (categories, meta): categories maps an MMS group name to its list of
    addresses in model order, meta maps an address to
    {category, name, description, bitfields, errata}. Both front ends build their
    register table from exactly this - the model file stays the single description
    of the chip, and neither front end reimplements reading it.
    """
    categories: Dict[str, List[str]] = {}
    meta: Dict[str, dict] = {}
    for category, group in model.get("groups", {}).items():
        regs = group.get("registers", {})
        categories[category] = list(regs.keys())
        for addr, info in regs.items():
            bits = info.get("bits", {})
            meta[addr] = {
                "category": category,
                "name": info.get("mnemonic", ""),
                "description": info.get("name", ""),
                "bitfields": {spec: f"{f.get('name', '')} - {f.get('description', '')}".strip(" -")
                              for spec, f in bits.items()},
                "errata": info.get("errata", []),
            }
    return categories, meta


def decode_one_bitfield(value_hex: str, bits_range: str) -> str:
    """The value of ONE bit field, as a suffix for its own line.

    Empty string if nothing was read or the value is unreadable - then the line
    shows only the description, and no one mistakes a 0 for a measurement.
    """
    if not value_hex or not value_hex.strip():
        return ""
    try:
        value = int(value_hex, 16)
    except (ValueError, TypeError):
        return ""
    try:
        if ":" in bits_range:
            high, low = map(int, bits_range.split(":"))
            width = high - low + 1
            field_value = (value >> low) & ((1 << width) - 1)
        else:
            width = 1
            field_value = (value >> int(bits_range)) & 1
    except ValueError:
        return ""
    if width == 1:
        return f"  = {field_value}"
    return f"  = {field_value} (0x{field_value:X})"


def normalize_register_value(value: str) -> str:
    """What lan_write wants: a 0x-prefixed hex literal. Typing 'A1' should work."""
    value = value.strip()
    if value and not value.lower().startswith("0x"):
        value = "0x" + value
    return value


def read_register_value(channel: "CommandChannel", addr: str,
                        attempts: int = 3) -> Optional[str]:
    """One register, with the retries a back-to-back read loop needs.

    Up to `attempts` tries: back-to-back 'lan_read' can occasionally outrun the
    LAN865x SPI transaction ("ERROR: Previous LAN operation still in progress" - a
    real, transient busy state, not a bug), or lose a rare race against
    CommandChannel.send()'s prompt-settle window. Neither reproduces on a fresh
    attempt a few ms later, so retrying is cheap and far better than either slowing
    every read down or reporting a false "no response" - confirmed 2026-09-04
    (repeated bulk reads against the same board show a different 0-4 addresses
    failing each time, not a consistent set, which is what a real per-register
    issue would look like instead).

    Returns the value as '0x...' or None if every attempt came back without one.
    """
    parser = ResponseParser()
    for _ in range(attempts):
        output = channel.send(f"lan_read {addr}")
        value = parser.parse_register_read(output)
        if not value:
            m = re.search(r'Value=(0x[0-9A-Fa-f]+)', output)
            value = m.group(1) if m else ""
        if value:
            return value
    return None


# ---------------------------------------------------------------------------
# Test modes
#
# (mode, title, description) - description is the "explained in detail" text shown
# in that mode's own group. Kept as data, not spread across widget calls, so a
# fifth mode is one more tuple and both front ends get it. Longer background
# (setup notes, safety) stays in docs/LAN8651_TEST_MODES.md; this is the summary
# worth having next to the button.
# ---------------------------------------------------------------------------

TESTMODE_REGISTER = "0x000308FB"      # T1STSTCTL, bits 15:13

TEST_MODES = [
    (1, "Output Voltage & Timing Jitter",
     "Drives the bus with the IEEE 802.3 §147.5.2 test pattern for amplitude and edge timing.\n"
     "Measures: differential output amplitude (peak-to-peak), timing jitter of the edges, rise/fall time.\n"
     "Instrument: oscilloscope, differential probe at the MDI, terminated bus."),
    (2, "Output Droop",
     "Drives the bus with a sustained-symbol pattern to expose AC-coupling droop.\n"
     "Measures: amplitude sag from the start to the end of the sustained interval, as % of the initial value.\n"
     "Instrument: oscilloscope, differential probe, averaging on."),
    (3, "PSD Mask (Spectral Emissions)",
     "Drives the bus with a pattern whose spectral content is compared against the IEEE PSD mask.\n"
     "Measures: power spectral density vs. the standard's mask, especially where the trace comes closest to it.\n"
     "Instrument: spectrum analyzer - needs a balun/transformer fixture, the bus is differential 100 Ω, "
     "the analyzer input is single-ended 50 Ω."),
    (4, "Transmitter High Impedance",
     "Puts the transmitter into a high-impedance state instead of driving the bus.\n"
     "Measures: the rest of the segment without this node's contribution, or this node's own off-state impedance.\n"
     "Instrument: oscilloscope, TDR, or ohmmeter - this node stays physically attached but electrically silent."),
]


def testmode_command(mode: int, timeout: str = "") -> str:
    """'testmode <mode>' plus the optional auto-revert seconds. An empty timeout
    means the mode runs until something else changes it, matching what the
    firmware's own `testmode <mode>` (no argument) does."""
    timeout = (timeout or "").strip()
    return f"testmode {mode} {timeout}".strip()


# ---------------------------------------------------------------------------
# Memory overview
# ---------------------------------------------------------------------------

def read_local_flash_ram_summary() -> Optional[str]:
    """Flash/RAM totals from the last local build (MEMORYFILE_XML), formatted as
    two lines - or None if this machine never built locally (a fresh clone that
    only ever flashed release\\...hex has no dist\\ output)."""
    try:
        xml_text = MEMORYFILE_XML.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None

    def block(name):
        m = re.search(
            r'<memory name="%s">.*?<length>(\d+)</length>\s*'
            r'<used>(\d+)</used>\s*<free>(\d+)</free>' % name,
            xml_text, re.DOTALL)
        return tuple(int(g) for g in m.groups()) if m else None

    program = block("program")
    data = block("data")
    if not program or not data:
        return None
    length, used, free = program
    pct = 100.0 * used / length if length else 0.0
    lines = ["Flash (last local build, not necessarily what's on the board now):",
             "  used %d / %d bytes (%.1f%%), %d free" % (used, length, pct, free)]
    length, used, free = data
    pct = 100.0 * used / length if length else 0.0
    lines.append("RAM, static .data+.bss (same build):")
    lines.append("  used %d / %d bytes (%.1f%%), %d free" % (used, length, pct, free))
    return "\n".join(lines)


def format_memory_overview(cleaned_meminfo: str) -> str:
    """The live device heap (from a cleaned 'meminfo' response) plus, if this
    machine has a local build, that build's Flash/RAM figures - the same two
    halves as a manual 'meminfo' plus reading build.bat's own summary, combined
    into one panel instead of two separate lookups."""
    lines = ["=== Live heap (device, right now) ==="]
    m = re.search(r"C-runtime heap:\s*total=(\d+)\s+largest free block=(\d+)", cleaned_meminfo)
    if m:
        total, largest = int(m.group(1)), int(m.group(2))
        lines.append("C-runtime heap (wolfSSL/malloc): %d bytes total, "
                     "largest free block %d bytes" % (total, largest))
        lines.append("  (nano-malloc - no exact free total, only the "
                     "largest single block it could hand out right now)")
    m = re.search(r"TCP/IP heap:\s*size=(\d+)\s+free=(\d+)\s+maxblock=(\d+)\s+highwater=(\d+)",
                  cleaned_meminfo)
    if m:
        size, free, maxblock, highwater = (int(g) for g in m.groups())
        lines.append("TCP/IP heap: %d bytes total, %d free, largest block %d, "
                     "high-water mark %d" % (size, free, maxblock, highwater))
    if len(lines) == 1:
        lines.append(cleaned_meminfo or "(no response)")

    local = read_local_flash_ram_summary()
    lines.append("")
    if local:
        lines.append(local)
    else:
        lines.append("(no local build found under dist\\ - Flash/RAM figures need "
                     "a local build.bat run; meminfo above still reflects the board "
                     "as it is right now)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Flash / erase over the EDBG probe (pyOCD, SWD) - independent of the Telnet link
#
# These run on the machine the front end's PROCESS runs on, not the machine the
# browser runs on. That distinction does not exist for the Tk GUI and is the one
# real asymmetry in the web front end: the probe has to be plugged into the
# server.
# ---------------------------------------------------------------------------

def console_python() -> str:
    """Never use sys.executable blindly for the flash_same54.py subprocess call: if
    the front end is running under pythonw.exe (no console window), flash_same54.py
    builds [sys.executable, "-m", "pyocd", ...] from it - i.e. "pythonw.exe -m pyocd
    erase --chip ...". That GRANDCHILD process loses its stdout somewhere in the
    chain under pythonw: the flash/erase command still echoes, then nothing more -
    not even "Chip erase complete", even though the erase itself completed fine on
    the real board. python.exe sits next to pythonw.exe in a standard install; falls
    back to sys.executable if that is not there."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.is_file():
            return str(candidate)
    return sys.executable


PYOCD_PYTHON = console_python()

# Windows-only flag; the front ends are Windows tools, but keeping the lookup
# soft means importing this module on another platform does not fail outright.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def com_ports_by_probe_serial() -> Dict[str, str]:
    """Map an EDBG probe's serial (pyOCD's unique_id) to its own COM port, so a
    probe picker can show which COM port belongs to the same physical board - not
    just an opaque serial number. Informational only: both front ends talk to the
    board over Telnet, not that COM port.

    An EDBG probe exposes its debug (CMSIS-DAP) and virtual-COM (CDC) function as
    separate USB interfaces of the SAME composite device, sharing one USB serial
    descriptor - verified on this bench (2026-08-29): pyserial's serial_number and
    pyOCD's unique_id came back byte-identical for all three connected probes.

    Empty dict if pyserial is not installed - it is an optional dependency, and
    only this label depends on it.
    """
    try:
        from serial.tools import list_ports
    except ImportError:
        return {}
    return {p.serial_number: p.device for p in list_ports.comports() if p.serial_number}


def list_probes() -> List[Tuple[str, str]]:
    """Connected probes per pyOCD, as (unique_id, description).

    Goes through 'flash_same54.py --list' rather than importing pyocd here: pyocd
    stays a dependency of the flash tool, not of the front ends. Raises OSError or
    subprocess.SubprocessError if the call itself fails; an empty list means pyOCD
    ran and found nothing.
    """
    proc = subprocess.run(
        [PYOCD_PYTHON, str(FLASH_SAME54_SCRIPT), "--list"],
        capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW)
    port_by_serial = com_ports_by_probe_serial()
    probes = []
    for line in proc.stdout.splitlines():
        m = re.match(r"^(\S+)\s{2,}(.+)$", line.strip())
        if m:
            unique_id, desc = m.group(1), m.group(2)
            port = port_by_serial.get(unique_id)
            if port:
                desc = f"{desc}  ({port})"
            probes.append((unique_id, desc))
    return probes


def stream_pyocd_op(extra_args: List[str], on_line: Callable[[str], None],
                    timeout: int = 180) -> bool:
    """Run flash_same54.py and hand its output to on_line as it arrives.

    Line by line rather than capture_output=True: that collects everything until
    the process exits, and the front end would show NOTHING for the ~20-30 s an
    erase/program/reset takes - unsettling right at the moment someone is most
    likely to wonder whether the click did anything at all. PYTHONUNBUFFERED
    affects every Python interpreter in the chain (flash_same54.py -> "python -m
    pyocd"), because neither of them sets its own env= - without it, Python buffers
    its own stdout in blocks as soon as the target is not a real console (here: the
    pipe).

    Returns True if flash_same54.py exited 0. Blocking - call it on a worker thread.
    """
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = None
    try:
        proc = subprocess.Popen(
            [PYOCD_PYTHON, str(FLASH_SAME54_SCRIPT)] + extra_args,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env, creationflags=_NO_WINDOW)
        for line in proc.stdout:
            on_line(line.rstrip("\n"))
        proc.wait(timeout=timeout)
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        if proc is not None:
            proc.kill()
        on_line(f"flash_same54.py did not finish within {timeout} s - killed.")
    except OSError as exc:
        on_line(f"flash_same54.py failed to start: {exc}")
    return False


# ---------------------------------------------------------------------------
# Firmware update over the network ("Bootload")
#
# The whole protocol lives in scripts/bootload.py; this is only the mapping a
# front end needs to turn its phase callbacks into a progress bar.
# ---------------------------------------------------------------------------

# Phase -> percent range on the bar. The transfer (phase 3) is the only one with
# byte-level progress and gets most of the bar; the others are single steps, and
# their ranges exist so the bar keeps moving through the commit and the reboot
# instead of sitting at "almost done" for 15 seconds.
BOOTLOAD_PHASE_PCT = {1: (0, 5), 2: (5, 6), 3: (6, 85), 4: (85, 90),
                      5: (90, 92), 6: (92, 97), 7: (97, 100)}
BOOTLOAD_PHASE_TEXT = {1: "preparing", 2: "arming the board", 3: "transferring",
                       4: "verifying in flash", 5: "committing (bank swap)",
                       6: "waiting for the reboot", 7: "checking what is running"}
BOOTLOAD_UNCANCELLABLE_FROM = 5   # once the swap is in flight there is nothing to cancel


def bootload_progress_text(phase: int, name: str, done: int, total: int) -> Tuple[float, str]:
    """(percent, caption) for one bootload progress callback."""
    lo, hi = BOOTLOAD_PHASE_PCT.get(phase, (0, 100))
    if phase == 3 and total:
        return (lo + (hi - lo) * done / total,
                f"{phase}/7  transferring  {done // 1024} KiB / {total // 1024} KiB")
    return lo, f"{phase}/7  {BOOTLOAD_PHASE_TEXT.get(phase, name)} ..."
