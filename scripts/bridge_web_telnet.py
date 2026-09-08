#!/usr/bin/env python3
"""
Bridge Status & Configuration - web front end

The same tool as bridge_gui_telnet.py, in a browser instead of a Tk window: all
seven tabs - Bridge Parameters, LAN8651 Registers, Test Modes, Terminal,
Certificates, MQTT and Help - plus flash/erase over the EDBG probe and the
firmware update over the network.

The browser shows the UI; this process still does the work. That split is not a
design preference, it is forced - a browser cannot open a raw TCP socket to the
board's Telnet port, let alone one with a client certificate. So "in the browser"
means: this Python process runs a local web server and keeps the one TLS/Telnet
connection, and the page is the display.

Everything below the UI is bridge_core, shared verbatim with the Tk GUI - the
connection, the login, the command protocol with its measured settle window, the
register and environment models, the pyOCD calls and the memory summary. Neither
front end has its own copy of any of it, so a fix to the protocol reaches both.

WHAT THE BROWSER DOES BETTER
    The terminal. xterm.js is a real terminal emulator: bridge_gui_telnet.py has
    to resolve the escape sequences itself (bridge_core.Screen, a partial ANSI
    parser that silently drops what it doesn't know), while here the device's
    bytes go to xterm.js untouched and keystrokes come back the same way -
    arrows, Tab completion and Ctrl-C included.

WHAT IS DIFFERENT FROM THE TK GUI, AND WHY
    * ONE session, many views. There is exactly one TLS/Telnet connection to the
      board, so a second browser tab is a second view of it, not a second link:
      both see the same terminal, the same values, the same log. Anything else
      would need the board to accept more than one Telnet client, which it does
      not.
    * "Local" means the SERVER. Flashing over the EDBG probe, the certificate
      folders, the MQTT broker and the "last local build" figures all happen on
      the machine this process runs on - not the machine the browser runs on.
      Where the Tk GUI opened a file dialog, this offers both a server-side path
      and an upload, because those two are only the same machine by coincidence.
    * No "open folder" button. os.startfile on the server would open a window
      nobody is sitting in front of. The path is shown instead.

Run it - the page is then at http://127.0.0.1:8088 :
    run_web_telnet.bat                  (or: python scripts\\bridge_web_telnet.py)
    python scripts\\bridge_web_telnet.py --port 9000 --host 0.0.0.0

SECURITY - read before using --host. By default this binds to 127.0.0.1, i.e. only
this machine can reach it, and that default is deliberate: whoever can open this
page can log into the board, rewrite its EEPROM, chip-erase it over the probe and
issue certificates in this project's name, using the credentials stored in
json/bridge_gui_telnet_config.json. There is no authentication in front of the
page. Binding it to a reachable interface puts all of that on the network for
anyone who can route to it.

Needs nicegui, cryptography and amqtt (pip install -r scripts/requirements.txt).
"""

import argparse
import html
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from nicegui import app, ui

import bootload
import bridge_core
import discover

# cryptography (Certificates) and amqtt (MQTT) are hard requirements here, exactly
# as in the Tk GUI: both tabs are built unconditionally. Guarded anyway so a
# checkout where setup.bat has not run reaches the dependency check below with a
# readable message instead of dying at import time.
try:
    import cert_provision
    import mqtt_broker
    import pki
except ImportError:
    cert_provision = mqtt_broker = pki = None

# How often the shared queue pump runs. 30 ms, same as the Tk GUI's POLL_MS - the
# terminal has to feel like a terminal, and this is what that costs.
POLL_S = 0.03
# MQTT events come from the broker's own asyncio thread and are unrelated to the
# link results, so they get their own, slower pump - as in the Tk GUI.
MQTT_POLL_S = 0.3

# The terminal backlog kept in this process, in bytes. Only used to catch a
# browser up when it connects or reloads; xterm.js keeps its own scrollback once
# the page is open.
TERM_BACKLOG = 200_000

# Log lengths, in lines.
LOG_LINES = 400

# Where an uploaded HEX lands. Deliberately a temp directory and not the repo:
# an upload is "this file, now", not a new tracked artefact.
UPLOAD_DIR = Path(tempfile.gettempdir()) / "bridge_web_telnet"


class Session:
    """Everything the front end knows, in one object shared by all browser clients.

    Mutated by worker threads only through result_queue, and read by the UI either
    through NiceGUI bindings (which propagate globally and safely) or by per-client
    timers. No worker and no pump ever touches a UI element: the pump runs app-wide
    with no browser client in context, and touching one client's elements from
    there is exactly the bug that would only show up with two tabs open.
    """

    def __init__(self) -> None:
        self.channel = bridge_core.CommandChannel()
        self.result_queue: queue.Queue = queue.Queue()

        self.config = bridge_core.load_config()

        # Models. A missing one is reported where its fields would have been, not
        # in a dialog - an empty tab that looks functional is the worst answer.
        self.model: dict = {}
        self.model_error: Optional[str] = None
        try:
            self.model = bridge_core.load_json_model(bridge_core.MODEL_FILE)
        except (OSError, ValueError) as exc:
            self.model_error = f"{bridge_core.MODEL_FILE.name}: {exc}"
        self.env_model: dict = {}
        self.env_model_error: Optional[str] = None
        try:
            self.env_model = bridge_core.load_json_model(bridge_core.ENV_MODEL_FILE)
        except (OSError, ValueError) as exc:
            self.env_model_error = f"{bridge_core.ENV_MODEL_FILE.name}: {exc}"

        self.env_identity: Optional[dict] = None
        self.reg_categories, self.reg_meta = bridge_core.register_view(self.model)

        # Bound straight into the input elements, so every browser client edits the
        # same values and sees the others' edits.
        self.conn = {
            "ip": str(self.config.get("ip", "")),
            "user": str(self.config.get("telnet_user", "")),
            "password": str(self.config.get("telnet_password", "")),
        }
        saved = self.config.get("bridge", {})
        self.fields = bridge_core.env_entry(self.env_model, None).get("fields", {})
        self.values = {key: str(saved.get(key, "")) for key in self.fields}
        saved_regs = self.config.get("values", {})
        self.reg_values = {addr: str(saved_regs.get(addr, "")) for addr in self.reg_meta}

        # Auto-revert seconds per test mode; empty means "until something changes it".
        # Keyed by the mode as a STRING: NiceGUI's binding normalises property names
        # and asserts they are strings, so an int key fails at bind time with
        # "Property name tuple must contain only strings" - a long way from the
        # dictionary that caused it.
        self.testmode_timeouts = {str(mode): "" for mode, _t, _d in bridge_core.TEST_MODES}

        self.view = {
            "status": "not connected",
            "connected": False,
            "identity": bridge_core.env_identity_line(self.env_model, None),
            "identity_ok": True,
            "log": "",
            "busy": False,
            "progress": "",          # "n/m registers" during a bulk operation
        }

        # The HEX that "Flash" and "Bootload" use. A server-side path, because that
        # is where the probe and the flash tool are; the upload button writes into
        # UPLOAD_DIR and points this at the result.
        self.hex_path = {"path": str(bridge_core.RELEASE_HEX)}

        self.term_buf = bytearray()
        self.term_total = 0

        # Certificates tab
        self.cert = {"log": "", "scanning": False}
        self.cert_known: List[dict] = []
        self.cert_found: List[dict] = []
        self.cert_selected: Optional[str] = None

        # MQTT tab
        self.mqtt_service = mqtt_broker.MqttBrokerService() if mqtt_broker else None
        self.mqtt = {
            "log": "",
            "status": "stopped",
            "running": False,
            "bind": mqtt_broker.DEFAULT_BIND_IP if mqtt_broker else "0.0.0.0",
            "port": mqtt_broker.DEFAULT_PORT if mqtt_broker else 8883,
            "iface": "auto",
            "discovering": False,
            "ok": True,            # False turns the status line red, see mqtt_tab
        }
        self.mqtt_boards: List[dict] = []
        self.mqtt_clients: Dict[str, dict] = {}
        self.mqtt_selected: Optional[str] = None

        # Firmware update over the network
        self.bootload = {"active": False, "pct": 0.0, "caption": "", "log": "",
                         "header": "", "done": False, "cancellable": True}
        self.bootload_cancel = threading.Event()

    # --- model helpers, all of them straight out of bridge_core ---------------

    def entry(self) -> dict:
        return bridge_core.env_entry(self.env_model, self.env_identity)

    def refresh_identity(self) -> None:
        self.view["identity"] = bridge_core.env_identity_line(self.env_model, self.env_identity)
        self.view["identity_ok"] = bridge_core.env_identity_ok(self.env_model, self.env_identity)

    # --- terminal backlog ----------------------------------------------------

    def term_feed(self, data: bytes) -> None:
        self.term_buf += data
        self.term_total += len(data)
        if len(self.term_buf) > TERM_BACKLOG:
            del self.term_buf[:len(self.term_buf) - TERM_BACKLOG]

    def term_since(self, cursor: int):
        """(bytes, new_cursor) for a client that has consumed up to `cursor`.

        A client that has fallen further behind than the backlog gets what is left
        and jumps forward - dropping bytes silently is the right call for a terminal
        view, unlike for a command response.
        """
        base = self.term_total - len(self.term_buf)
        if cursor >= self.term_total:
            return b"", self.term_total
        start = max(0, cursor - base)
        return bytes(self.term_buf[start:]), self.term_total

    # --- logs ----------------------------------------------------------------

    @staticmethod
    def _append(text: str, line: str, stamp: bool = True) -> str:
        prefix = f"[{time.strftime('%H:%M:%S')}] " if stamp else ""
        lines = text.splitlines()
        lines.append(prefix + line)
        return "\n".join(lines[-LOG_LINES:])

    def log(self, text: str) -> None:
        self.view["log"] = self._append(self.view["log"], text)

    def cert_log(self, text: str) -> None:
        self.cert["log"] = self._append(self.cert["log"], text.rstrip("\n"))

    def mqtt_log(self, text: str) -> None:
        self.mqtt["log"] = self._append(self.mqtt["log"], text)

    def bootload_log(self, text: str) -> None:
        self.bootload["log"] = self._append(self.bootload["log"], text.rstrip(), stamp=False)

    def status(self, text: str) -> None:
        self.view["status"] = text


session = Session()


def post(*message) -> None:
    """Everything a worker thread wants the UI to know goes through here."""
    session.result_queue.put(message)


# ---------------------------------------------------------------------------
# The pumps. ONE of each for the whole app (app.timer, not ui.timer): the reader
# thread's queue must be drained exactly once. A per-client ui.timer would mean
# two browser tabs each eating half the bytes.
# ---------------------------------------------------------------------------

def pump() -> None:
    while True:
        try:
            result = session.result_queue.get_nowait()
        except queue.Empty:
            return

        if len(result) >= 3 and result[1] == "data":
            # Both consumers get their own copy, same rule as in the Tk GUI: the
            # terminal always, the running command only while one is running.
            session.term_feed(result[2])
            session.channel.dispatch(result)
            continue

        kind = result[0]
        if kind == "port_opened":
            link = result[1]
            session.channel.link = link
            session.view["connected"] = True
            session.status(f"connected to {link.port}")
            session.log(f"connected to {link.port}")
        elif kind == "port_failed":
            session.channel.link = None
            session.view["connected"] = False
            session.status(f"connection failed: {result[1]}")
            session.log(f"connection failed: {result[1]}")
        elif len(result) >= 3 and result[1] == "lost":
            session.channel.link = None
            session.view["connected"] = False
            session.status(f"connection lost: {result[2]}")
            session.log(f"connection lost: {result[2]}")
        elif kind == "values":
            session.values.update(result[1])
        elif kind == "identity":
            session.env_identity = result[1]
            session.refresh_identity()
        elif kind == "register":
            addr, value = result[1], result[2]
            if value:
                session.reg_values[addr] = value
        elif kind == "progress":
            session.view["progress"] = result[1]
        elif kind == "log":
            session.log(result[1])
        elif kind == "cert_log":
            session.cert_log(result[1])
        elif kind == "cert_found":
            session.cert_found = result[1]
            session.cert["scanning"] = False
        elif kind == "cert_known":
            session.cert_known = result[1]
        elif kind == "mqtt_log":
            session.mqtt_log(result[1])
        elif kind == "mqtt_start_failed":
            # The status line is the only broker feedback visible without
            # scrolling to the log, so the failure has to land there too.
            session.mqtt["status"] = f"start failed - {result[1]}"
            session.mqtt["running"] = False
            session.mqtt["ok"] = False
            session.mqtt_log(f"ERROR: {result[1]}")
        elif kind == "mqtt_boards":
            session.mqtt_boards = result[1]
            session.mqtt["discovering"] = False
        elif kind == "mqtt_board_state":
            row_ip, via, state = result[1], result[2], result[3]
            for row in session.mqtt_boards:
                if row["ip"] == row_ip:
                    row["via"], row["state"] = via, state
        elif kind == "bl_progress":
            _, phase, name, done, total = result
            pct, caption = bridge_core.bootload_progress_text(phase, name, done, total)
            session.bootload["pct"] = pct / 100.0
            session.bootload["caption"] = caption
            if phase >= bridge_core.BOOTLOAD_UNCANCELLABLE_FROM:
                session.bootload["cancellable"] = False
        elif kind == "bl_log":
            session.bootload_log(result[1])
        elif kind == "bl_done":
            ok, message = result[1], result[2]
            session.bootload["pct"] = 1.0 if ok else session.bootload["pct"]
            session.bootload["caption"] = (
                "done - the board is running the new firmware" if ok
                else "failed - the board still runs its previous firmware")
            session.bootload_log(("OK: " if ok else "FAILED: ") + message)
            session.bootload["cancellable"] = False
            session.bootload["done"] = True
            session.status("bootload OK" if ok else f"bootload failed: {message}")
        elif kind == "status":
            session.status(result[1])
        elif kind == "busy":
            session.view["busy"] = result[1]
            if not result[1]:
                session.view["progress"] = ""


def mqtt_pump() -> None:
    """Drains mqtt_service.events - the broker's asyncio thread, unrelated to the
    Telnet link results above."""
    if session.mqtt_service is None:
        return
    while True:
        try:
            kind, payload, ts = session.mqtt_service.events.get_nowait()
        except queue.Empty:
            return
        when = time.strftime("%H:%M:%S", time.localtime(ts))

        if kind == "started":
            session.mqtt["status"] = f"listening on {payload} (TLS/mTLS)"
            session.mqtt["running"] = True
            session.mqtt["ok"] = True
            session.mqtt_log(f"broker started on {payload}")
        elif kind == "stopped":
            session.mqtt["status"] = "stopped"
            session.mqtt["running"] = False
            session.mqtt_clients.clear()
            session.mqtt_log("broker stopped")
        elif kind == "error":
            session.mqtt_log(f"ERROR: {payload}")
        elif kind == "client":
            client_id, remote, cert_cn = payload
            session.mqtt_clients[client_id] = {
                "client_id": client_id, "remote": remote, "cert_cn": cert_cn,
                "last_seen": when, "last_payload": ""}
            session.mqtt_log(f"client connected: {client_id} ({remote}, cert CN={cert_cn})")
        elif kind == "client_gone":
            session.mqtt_clients.pop(payload, None)
            session.mqtt_log(f"client disconnected: {payload}")
        elif kind == "message":
            client_id, topic, data = payload
            try:
                preview = data.decode("utf-8", errors="replace")
            except Exception:                              # noqa: BLE001
                preview = repr(data)
            # A retained/late message from a client this session never saw a
            # CLIENT_CONNECTED for (e.g. the broker restarted) still gets a row.
            row = session.mqtt_clients.setdefault(client_id, {
                "client_id": client_id, "remote": "", "cert_cn": "",
                "last_seen": when, "last_payload": ""})
            row["last_seen"], row["last_payload"] = when, preview
            session.mqtt_log(f"{client_id} -> {topic}: {preview}")


def in_worker(fn, *args, **kwargs) -> None:
    """Run fn on a daemon thread. Every board/probe/PKI call goes through here:
    the event loop must not block, for the same reason the Tk GUI never blocks its
    main thread."""
    threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()


def busy_worker(fn):
    """in_worker plus the busy flag that greys out the action buttons."""
    def wrapped():
        try:
            fn()
        finally:
            post("busy", False)
    session.view["busy"] = True
    in_worker(wrapped)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect() -> None:
    host = session.conn["ip"].strip()
    if not host:
        ui.notify("Enter the device's IP address first", type="warning")
        return
    if session.channel.connected:
        ui.notify("Already connected", type="info")
        return
    if not require_tls(session.log):
        return

    user, password = session.conn["user"], session.conn["password"]
    # Remember what was used to connect - the Tk GUI writes the same three keys to
    # the same file, so whichever front end you start next comes up pointed at the
    # board you last used.
    session.config["ip"] = host
    session.config["telnet_user"] = user
    session.config["telnet_password"] = password
    try:
        bridge_core.save_config(session.config)
    except OSError as exc:
        ui.notify(f"could not save the config: {exc}", type="negative")

    session.status(f"connecting to {host} ...")

    def worker():
        link = bridge_core.TelnetLink(host, session.result_queue,
                                      username=user, password=password)
        try:
            link.open()
            post("port_opened", link)
        except Exception as exc:                           # noqa: BLE001 - shown as text
            post("port_failed", str(exc))

    in_worker(worker)


def disconnect() -> None:
    link = session.channel.link
    if link:
        link.close()
    session.channel.link = None
    session.view["connected"] = False
    session.status("not connected")


def require_link() -> bool:
    if not session.channel.connected:
        ui.notify("Press Connect first", type="warning")
        return False
    return True


def require_tls(log=None) -> bool:
    """Refuse, with an actionable message, if the client identity is incomplete.

    Checked BEFORE anything opens a console. Without it, a fresh checkout with no
    certs/client/ fails deep inside OpenSSL - bootload.Console retries three times
    over ~12 seconds and then reports a bare "[Errno 2] No such file or directory"
    with no path in it, which is a dead end even though the fix is a button on the
    Certificates tab. Hit exactly that way on 2026-09-08, pushing an identity to
    192.168.0.21.
    """
    problem = bridge_core.tls_identity_problem()
    if problem is None:
        return True
    ui.notify(problem, type="negative", multi_line=True, close_button="OK")
    if log is not None:
        log(problem)
    return False


def run_cmd(command: str, timeout_ms: int = 1500) -> None:
    """Send a command over the open link; the response goes into the command log."""
    if not require_link():
        return
    session.status(f"running: {command}")

    def worker():
        output = session.channel.send(command, timeout_ms=timeout_ms)
        text = bridge_core.clean_response(command, output)
        post("log", f"> {command}\n{text or 'no response'}")
        post("status", "command OK" if text else f"{command}: no response")

    in_worker(worker)


async def confirm(title: str, body: str, action: str = "OK",
                  danger: bool = False, code: str = "") -> bool:
    """A yes/no dialog, as close to the Tk messagebox.askyesno it replaces as the
    browser gets. Returns True only if the action button was pressed."""
    with ui.dialog() as dialog, ui.card().classes("w-[40rem]"):
        ui.label(title).classes("text-lg font-medium")
        ui.label(body).classes("text-sm whitespace-pre-wrap")
        if code:
            ui.code(code).classes("w-full max-h-64 overflow-auto")
        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("flat")
            ui.button(action, on_click=lambda: dialog.submit(True)) \
                .props("color=negative" if danger else "")
    return bool(await dialog)


async def ask_text(title: str, label: str, initial: str = "") -> Optional[str]:
    """One-line text prompt, replacing Tk's simpledialog.askstring. None on cancel."""
    with ui.dialog() as dialog, ui.card().classes("w-[32rem]"):
        ui.label(title).classes("text-lg font-medium")
        field = ui.input(label, value=initial).props("outlined dense autofocus").classes("w-full")
        field.on("keydown.enter", lambda: dialog.submit(field.value))
        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=lambda: dialog.submit(None)).props("flat")
            ui.button("OK", on_click=lambda: dialog.submit(field.value))
    return await dialog


# ---------------------------------------------------------------------------
# Bridge parameters
# ---------------------------------------------------------------------------

def read_environment() -> None:
    if not require_link():
        return
    session.status("reading bridge parameters ...")

    def worker():
        # 5000 ms, not the usual ~1000: 'showenv' reaching a Follower crosses the
        # T1S hop through the Bridge's own L2 forwarding, and that path has been
        # measured sending the value lines up to ~1 s behind the identity line.
        out = session.channel.send("showenv", timeout_ms=5000)
        # Identity FIRST, and the values are then read against exactly that entry -
        # otherwise the page shows filled-in fields under a line saying the
        # environment is unknown, both derived from the same response.
        ident = bridge_core.parse_env_identity(session.env_model, out)
        entry = bridge_core.env_entry_for(session.env_model, ident)
        values = {}
        for key in session.fields:
            value = bridge_core.parse_showenv(entry, out, key)
            if value is not None:
                values[key] = value
        post("identity", ident)
        post("values", values)
        post("log", f"> showenv\n{bridge_core.clean_response('showenv', out)}")
        post("status", f"read {len(values)} of {len(session.fields)} parameters"
                       if values else "showenv: nothing could be read")

    busy_worker(worker)


async def write_environment() -> None:
    """Write every filled-in field, then persist - with the identity checked first.

    Both safeguards are the Tk GUI's, for the same reasons: if the device reports an
    environment this tool has no model for, the setenv keys would be guesswork, so
    nothing is written. And persisting is the default, not a follow-up question -
    whoever presses this wants something that survives a reset.
    """
    if not require_link():
        return

    entry = session.entry()
    if not entry:
        ident = session.env_identity or {}
        ui.notify(f"The device reports environment id {ident.get('eeprom_id', '?')} "
                  f"v{ident.get('eeprom_version', '?')}, which has no model in "
                  f"{bridge_core.ENV_MODEL_FILE.name} - nothing was written.",
                  type="negative", multi_line=True, close_button="OK")
        return

    cmds = []
    for key in session.fields:
        value = session.values.get(key, "").strip()
        if not value:
            continue
        cmd = bridge_core.bridge_write_command(entry, key, value)
        if cmd:
            cmds.append(cmd)

    if not cmds:
        ui.notify("No writable parameter has a value.", type="info")
        return

    persist = entry.get("commands", {}).get("persist", "saveenv")
    if not await confirm("Write environment?",
                         f"Send {len(cmds)} values to the device and store them in the "
                         f"EEPROM with '{persist}'?",
                         action="Write", danger=True,
                         code="\n".join(cmds + [persist])):
        return

    session.status("writing environment ...")

    def worker():
        for cmd in cmds:
            out = session.channel.send(cmd, timeout_ms=1500)
            post("log", f"> {cmd}\n{bridge_core.clean_response(cmd, out)}")
        out = session.channel.send(persist, timeout_ms=3000)
        post("log", f"> {persist}\n{bridge_core.clean_response(persist, out)}")
        # Read back afterwards - a write confirmation is not proof that the device
        # accepted the value.
        check = session.channel.send("showenv", timeout_ms=5000)
        ident = bridge_core.parse_env_identity(session.env_model, check)
        back = bridge_core.env_entry_for(session.env_model, ident)
        values = {}
        for key in session.fields:
            value = bridge_core.parse_showenv(back, check, key)
            if value is not None:
                values[key] = value
        post("identity", ident)
        post("values", values)
        post("log", f"> showenv (verify)\n{bridge_core.clean_response('showenv', check)}")
        post("status", "environment written and read back")

    busy_worker(worker)


def save_bridge_json() -> None:
    """Save bridge parameters into the shared config file."""
    bridge = {}
    for key, value in session.values.items():
        try:
            if "." in value:
                bridge[key] = float(value)
            else:
                bridge[key] = int(value) if value.isdigit() else value
        except ValueError:
            bridge[key] = value
    session.config["bridge"] = bridge
    try:
        bridge_core.save_config(session.config)
    except OSError as exc:
        ui.notify(f"could not save: {exc}", type="negative")
        return
    session.status(f"bridge config saved to {bridge_core.CONFIG_FILE.name}")


def load_bridge_json() -> None:
    """Load bridge parameters back out of the shared config file."""
    try:
        cfg = bridge_core.load_config()
    except (OSError, ValueError) as exc:
        ui.notify(f"could not read the config: {exc}", type="negative")
        return
    n = 0
    for key, value in (cfg.get("bridge") or {}).items():
        if key in session.values:
            session.values[key] = str(value)
            n += 1
    session.status(f"{n} bridge values loaded from {bridge_core.CONFIG_FILE.name}")


def memory_overview() -> None:
    """The live device heap (meminfo) plus, if this machine has a local build,
    that build's Flash/RAM figures - the same two halves as a manual 'meminfo'
    plus reading build.bat's own summary, combined into one panel."""
    if not require_link():
        return
    session.status("reading memory overview ...")

    def worker():
        raw = session.channel.send("meminfo", timeout_ms=1500)
        cleaned = bridge_core.clean_response("meminfo", raw)
        post("log", bridge_core.format_memory_overview(cleaned))
        post("status", "memory overview read")

    in_worker(worker)


async def reset_device() -> None:
    """Confirmed first, unlike the other Device buttons (mirror/stats/meminfo/
    timestamp): those just read or flip a runtime flag, this one reboots the board
    and drops whatever else was in progress on the link."""
    if not require_link():
        return
    if await confirm("Reset the device?", "The board will reboot.",
                     action="Reset", danger=True):
        run_cmd("reset")


# ---------------------------------------------------------------------------
# Registers
# ---------------------------------------------------------------------------

def read_register(addr: str) -> None:
    if not require_link():
        return

    def worker():
        value = bridge_core.read_register_value(session.channel, addr)
        post("register", addr, value or "")
        post("status", f"{addr} = {value}" if value else f"{addr}: no response")

    in_worker(worker)


def write_register(addr: str) -> None:
    value = bridge_core.normalize_register_value(session.reg_values.get(addr, ""))
    if not value:
        ui.notify(f"No value for {addr}", type="warning")
        return
    if not require_link():
        return
    session.status(f"writing {addr} = {value} ...")

    def worker():
        session.channel.send(f"lan_write {addr} {value}")
        time.sleep(0.2)
        # Read back to verify - a write that reports OK is not proof the register
        # took the value.
        back = bridge_core.read_register_value(session.channel, addr)
        post("register", addr, back or "")
        post("status", f"{addr} written, reads back {back}" if back
                       else f"{addr} written, no read-back")

    in_worker(worker)


def bulk_read_registers() -> None:
    if not require_link():
        return
    addrs = list(session.reg_meta)
    session.status(f"reading {len(addrs)} registers ...")

    def worker():
        failed = []
        for n, addr in enumerate(addrs, 1):
            # Retries included - see bridge_core.read_register_value for why a
            # back-to-back read loop needs them.
            value = bridge_core.read_register_value(session.channel, addr)
            if value:
                post("register", addr, value)
            else:
                failed.append(addr)
            if n % 10 == 0 or n == len(addrs):
                post("progress", f"{n}/{len(addrs)} registers, {len(failed)} without an answer")
        if failed:
            post("log", f"Bulk read: {len(addrs) - len(failed)}/{len(addrs)} ok, "
                        f"no response from: {', '.join(failed[:12])}"
                        + (" ..." if len(failed) > 12 else ""))
        post("status", f"bulk read done: {len(addrs) - len(failed)}/{len(addrs)}")

    busy_worker(worker)


async def bulk_write_registers() -> None:
    if not require_link():
        return
    pending = {a: bridge_core.normalize_register_value(v)
               for a, v in session.reg_values.items() if v.strip()}
    if not pending:
        ui.notify("No register has a value to write.", type="info")
        return
    if not await confirm("Write all registers?",
                         f"Write {len(pending)} register values to the device, then "
                         f"read them all back?", action="Write all", danger=True):
        return
    session.status("writing all registers ...")

    def worker():
        for addr, value in pending.items():
            session.channel.send(f"lan_write {addr} {value}")
            time.sleep(0.15)
        post("log", f"{len(pending)} registers written. Reading back ...")
        time.sleep(0.2)
        for n, addr in enumerate(pending, 1):
            value = bridge_core.read_register_value(session.channel, addr)
            post("register", addr, value or "")
            if n % 10 == 0 or n == len(pending):
                post("progress", f"{n}/{len(pending)} read back")
            time.sleep(0.15)
        post("status", "bulk write done")

    busy_worker(worker)


def save_registers_json() -> None:
    """Save only the VALUES that were read. The front end never touches the model.

    The Tk GUI's version of this used to write the whole register map back, and in
    doing so damaged it twice - once because it rebuilt it from the widgets, once
    via the encoding. Since the map lives in lan8651_model.json and only
    {address: value} lands here, that whole class of bug is structurally gone.
    """
    if not session.reg_values:
        ui.notify("No register model loaded - nothing saved.", type="warning")
        return
    values = {addr: v for addr, v in session.reg_values.items() if v}
    session.config["values"] = values
    session.config.pop("registers", None)   # legacy: the map used to live here
    try:
        bridge_core.save_config(session.config)
    except OSError as exc:
        ui.notify(f"could not save: {exc}", type="negative")
        return
    session.status(f"{len(values)} register values saved (model untouched)")


def load_registers_json() -> None:
    try:
        cfg = bridge_core.load_config()
    except (OSError, ValueError) as exc:
        ui.notify(f"could not read the config: {exc}", type="negative")
        return
    n = 0
    for addr, val in (cfg.get("values") or {}).items():
        if addr in session.reg_values and val:
            session.reg_values[addr] = str(val)
            n += 1
    # Older config files still carried the values in the "registers" tree.
    for key, value in (cfg.get("registers") or {}).items():
        if isinstance(value, dict):
            for addr, entry in value.items():
                if addr not in session.reg_values:
                    continue
                val = entry.get("value", "") if isinstance(entry, dict) else str(entry)
                if val:
                    session.reg_values[addr] = val
                    n += 1
        elif key in session.reg_values:
            session.reg_values[key] = str(value)
            n += 1
    session.status(f"{n} register values loaded from {bridge_core.CONFIG_FILE.name}")


def register_detail_html(meta: dict, value: str) -> str:
    """Description, errata and decoded bit fields for ONE register, as one element.

    One ui.html rather than one label per bit field: the model has 183 registers
    with several fields each, and a label apiece is a few thousand Vue components
    for a page that then takes seconds to build. The decoded value sits at the end
    of its own field's line, in green, exactly as the Tk GUI shows it.
    """
    parts = []
    if meta["description"]:
        parts.append(f'<div class="opacity-60">{html.escape(meta["description"])}</div>')
    for e in meta["errata"]:
        line = f'{e.get("doc", "")} {e.get("item", "")}: {e.get("summary", "")}'
        parts.append(f'<div class="text-red-400">&#9888; {html.escape(line)}</div>')
        if e.get("implication"):
            parts.append('<div class="text-red-400 pl-6">&rarr; '
                         f'{html.escape(e["implication"])}</div>')
    for bits, meaning in meta["bitfields"].items():
        decoded = bridge_core.decode_one_bitfield(value, bits)
        parts.append(
            f'<div class="opacity-75">[{html.escape(bits)}] {html.escape(meaning)}'
            f'<span class="text-green-400">{html.escape(decoded)}</span></div>')
    if not parts:
        return ""
    return ('<div class="font-mono text-xs leading-tight pl-4 pb-1">'
            + "".join(parts) + "</div>")


# ---------------------------------------------------------------------------
# Flash / erase over the EDBG probe
# ---------------------------------------------------------------------------

async def pick_probe(mode: str) -> None:
    """Pick ONE of the probes pyOCD currently sees, then go straight on - a second,
    generic confirmation afterwards would only repeat the warning that already
    stands in the dialog. Erase additionally needs the typed word."""
    if not bridge_core.FLASH_SAME54_SCRIPT.is_file():
        ui.notify(f"{bridge_core.FLASH_SAME54_SCRIPT} is missing.", type="negative")
        return
    hex_path = Path(session.hex_path["path"])
    if mode == "flash" and not hex_path.is_file():
        ui.notify(f"{hex_path} is missing.", type="negative")
        return
    try:
        probes = bridge_core.list_probes()
    except (OSError, subprocess.SubprocessError) as exc:
        ui.notify(f"Could not list probes: {exc}", type="negative")
        return
    if not probes:
        ui.notify("No connected probes found (check the USB connection, or "
                  "pip install pyocd).", type="negative")
        return

    if mode == "erase":
        heading = "Chip-erase (firmware AND emulated EEPROM) via pyOCD on:"
        warning = ("This erases EVERYTHING on the selected board: firmware and the "
                   "emulated EEPROM (PLCA id/count, IP, MAC settings). It will need "
                   "reflashing afterward.")
        action = "Erase ..."
    else:
        heading = f"Flash {hex_path.name} onto:"
        warning = "This erases and reprograms the selected board, then resets it."
        action = "Flash"

    options = {uid: f"{uid}   {desc}" for uid, desc in probes}
    with ui.dialog() as dialog, ui.card().classes("w-[46rem]"):
        ui.label(heading).classes("text-lg font-medium")
        select = ui.select(options, value=probes[0][0]).props("outlined dense").classes("w-full")
        ui.label(warning).classes("text-sm text-negative")
        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=lambda: dialog.submit(None)).props("flat")
            ui.button(action, on_click=lambda: dialog.submit(select.value)) \
                .props("color=negative")
    unique_id = await dialog
    if not unique_id:
        return
    description = dict(probes)[unique_id]

    if mode == "erase":
        typed = await ask_text(
            "Confirm erase",
            f"This PERMANENTLY erases probe {unique_id} ({description}): firmware AND "
            f"the emulated EEPROM. Type {bridge_core.ERASE_CONFIRM_WORD} to proceed")
        if typed != bridge_core.ERASE_CONFIRM_WORD:
            session.status("erase cancelled")
            return
        run_pyocd("Erase", ["--erase", "--probe", unique_id], description, timeout=120)
    else:
        run_pyocd("Flash", [str(hex_path), "--probe", unique_id], description)


def run_pyocd(label: str, extra_args: List[str], description: str = "",
              timeout: int = 180) -> None:
    """Stream flash_same54.py's output into the command log line by line - see
    bridge_core.stream_pyocd_op for why not all at once at the end."""
    session.status(f"{label.lower()}ing ...")

    def worker():
        suffix = f"  ({description})" if description else ""
        post("log", f"$ flash_same54.py {' '.join(extra_args)}{suffix}")
        ok = bridge_core.stream_pyocd_op(
            extra_args, lambda line: post("log", line), timeout=timeout)
        post("status", f"{label} OK" if ok else f"{label} failed")

    busy_worker(worker)


def on_hex_upload(event) -> None:
    """A HEX picked in the BROWSER. The Tk GUI opens a file dialog on the machine
    it runs on; here that machine is the server, so a browser on another machine
    needs a way to get the file across. Lands in a temp directory, not the repo:
    an upload is "this file, now", not a new tracked artefact."""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = UPLOAD_DIR / Path(event.name).name
    target.write_bytes(event.content.read())
    session.hex_path["path"] = str(target)
    session.status(f"using uploaded {target.name}")
    session.log(f"uploaded {target.name} -> {target}")


# ---------------------------------------------------------------------------
# Firmware update over the network ("Bootload")
# ---------------------------------------------------------------------------

async def bootload_start() -> None:
    """Send the selected HEX to the board over the network and activate it."""
    if not session.channel.connected:
        ui.notify("Connect to the board first - the update needs the Telnet console "
                  "to arm the board and to check the result afterwards.",
                  type="warning", multi_line=True, close_button="OK")
        return
    if session.bootload["active"]:
        ui.notify("An update is already running.", type="info")
        return
    if not require_tls(session.log):
        return

    hex_path = Path(session.hex_path["path"])
    try:
        image = bootload.load_image(hex_path)
    except Exception as exc:                               # noqa: BLE001
        ui.notify(f"Cannot read {hex_path}: {exc}", type="negative", multi_line=True)
        return

    host = session.conn["ip"].strip()
    if not await confirm(
            "Update firmware over the network?",
            f"Send this image to {host} and activate it?\n\n"
            f"File:  {hex_path.name}\n"
            f"Size:  {image.size:,} bytes "
            f"({100.0 * image.size / bootload.BL_MAX_IMAGE:.0f}% of one bank)\n"
            f"CRC32: 0x{image.crc:08X}\n\n"
            "The board writes it into its inactive flash bank while it keeps running, "
            "then swaps banks and reboots. A failed transfer changes nothing.",
            action="Update", danger=True):
        return

    user, password = session.conn["user"], session.conn["password"]
    session.bootload_cancel = threading.Event()
    session.bootload.update({
        "active": True, "pct": 0.0, "caption": "1/7  preparing ...", "log": "",
        "done": False, "cancellable": True,
        "header": f"{hex_path.name}  ->  {host}   "
                  f"{image.size:,} bytes, CRC32 0x{image.crc:08X}"})

    # This front end's own Telnet session steps aside for the duration: the board
    # reboots halfway through, which would leave it holding a dead socket, and
    # bootload.py needs a console of its own to arm and check. The worker
    # reconnects at the end through the same ("port_opened", link) message connect()
    # uses, so the indicator and the log behave exactly as after a manual reconnect.
    disconnect()
    in_worker(bootload_worker, host, user, password, hex_path)


def bootload_worker(host, user, password, hex_path) -> None:
    """Runs the update off the event loop. Everything it wants to show goes through
    result_queue, like every other worker here."""
    ok = False
    # The board allows two Telnet sessions and reaps a closed one from its own
    # 100 ms task, so a console opened in the same breath as the one just dropped
    # can be accepted and closed again without a prompt. bootload.py retries for
    # exactly this reason; giving it a head start costs nothing.
    time.sleep(1.0)
    try:
        result = bootload.run_update(
            host, hex_path, user, password,
            data_port=int(session.config.get("bootload_port", bootload.DATA_PORT)),
            progress=lambda phase, name, done, total: post("bl_progress", phase, name, done, total),
            log=lambda line: post("bl_log", str(line)),
            cancel=session.bootload_cancel.is_set)
        ok = True
        message = (f"{result['size']:,} bytes running from the other bank "
                   f"(crc 0x{result['crc']:08X}), environment kept")
    except Exception as exc:                               # noqa: BLE001
        message = str(exc)
    post("bl_done", ok, message)

    # Reconnect this front end's own console, the same way connect() does - with
    # the same tolerance for the session-reap gap, and a little extra patience
    # because the board may have only just finished rebooting.
    last = ""
    for _attempt in range(4):
        link = bridge_core.TelnetLink(host, session.result_queue,
                                      username=user, password=password)
        try:
            link.open()
            post("port_opened", link)
            return
        except Exception as exc:                           # noqa: BLE001
            last = str(exc)
            time.sleep(1.5)
    post("port_failed", f"reconnect after the update failed: {last}")


def bootload_cancel() -> None:
    session.bootload_cancel.set()
    session.bootload["cancellable"] = False
    session.bootload_log("cancelling ...")


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------

def cert_refresh() -> None:
    boards = pki.list_boards()
    rows = [{"board_id": b["board_id"], "ip": b.get("ip", ""),
             "fingerprint": b["fingerprint_sha256"][:23] + "...",
             "created": b.get("created", "")[:19]} for b in boards]
    session.cert_known = rows
    session.cert_log("%d known board identit%s"
                     % (len(boards), "y" if len(boards) == 1 else "ies"))


def cert_scan() -> None:
    """The one discovery action (discover.discover_boards): UDP broadcast to find
    the boards - they answer for themselves with MAC+IP, near-instant, and nothing
    is touched that isn't a board - then one mutual-TLS handshake against each
    answer for the CA-verified fingerprint the broadcast cannot prove. No /24 sweep
    and no ping sweep: the first floods ARP onto the bridged T1S segment (which has
    wedged the bench before), the second proves less than the broadcast does since
    the TCP/IP stack answers ping even when the application side is wedged.

    A board with no fingerprint answered the broadcast but did not complete the
    handshake - that is shown, not hidden: it is exactly the factory-fresh board
    that has no identity from our CA yet, which is what this tab exists to fix.
    """
    base_ip = session.conn["ip"].strip()
    if not base_ip:
        ui.notify("Enter any IP on the target /24 in the IP field above first.",
                  type="warning")
        return
    session.cert_found = []
    session.cert["scanning"] = True
    prefix = ".".join(base_ip.split(".")[:3])
    if discover.operator_identity_ready():
        session.cert_log(f"Broadcasting on {prefix}.255, then TLS-probing every answer...")
    else:
        # Fresh checkout: no certs/client/ yet, so the fingerprint column would be
        # empty for every board with no way to tell why.
        session.cert_log(f"Broadcasting on {prefix}.255 (no operator client identity yet - "
                         "fingerprints stay empty until \"Create Operator Client Identity\")")

    def worker():
        try:
            # Fingerprint first, IP only as a fallback: the fingerprint is what
            # actually identifies a board, an IP is just where it happened to be
            # when its identity was issued.
            boards = pki.list_boards()
            by_fp = {b["fingerprint_sha256"].replace(":", "").lower(): b["board_id"]
                     for b in boards}
            by_ip = {b.get("ip", ""): b["board_id"] for b in boards if b.get("ip")}
            rows = []
            for r in discover.discover_boards(base_ip):
                fp = r["fingerprint_sha256"]
                if fp:
                    fp_display = ":".join(fp[i:i + 2] for i in range(0, len(fp), 2)).upper()
                    board_id = by_fp.get(fp) or by_ip.get(r["ip"], "(unregistered)")
                else:
                    fp_display = "(no mTLS handshake)"
                    board_id = by_ip.get(r["ip"], "(unregistered)")
                rows.append({"ip": r["ip"], "mac": r["mac"], "board_id": board_id,
                             "fingerprint": fp_display})
            post("cert_found", rows)
        except Exception as exc:                           # noqa: BLE001
            post("cert_log", f"Discovery failed: {exc}")
            post("cert_found", [])

    in_worker(worker)


async def cert_ca_status() -> None:
    try:
        if pki.ca_exists():
            cert, _ = pki.load_ca()
            session.cert_log(f"CA present: {pki.CA_CERT_PATH}")
            session.cert_log(f"  subject: {cert.subject.rfc4514_string()}")
            session.cert_log(f"  fingerprint: {pki.fingerprint(cert)}")
        elif await confirm("No CA yet",
                           f"No project CA exists yet ({pki.CA_CERT_PATH}).\nCreate one now?",
                           action="Create"):
            pki.create_ca()
            pki.issue_client_identity()
            session.cert_log("Created a new CA and the shared client identity.")
    except pki.PkiError as exc:
        ui.notify(f"PKI error: {exc}", type="negative", multi_line=True)


async def cert_issue_client() -> None:
    """The operator identity every host-side tool presents (both front ends,
    bootload.py, discover.py's TLS probe). Generated per checkout, not shipped.
    Replacing it is harmless as long as the CA stays the same: the boards trust the
    CA, not this leaf, so nothing has to be reprovisioned."""
    try:
        if not pki.ca_exists():
            ui.notify("Create the CA first (CA Status).", type="warning")
            return
        if pki.CLIENT_CERT_PATH.is_file():
            if not await confirm(
                    "Replace client identity?",
                    f"An operator client identity already exists ({pki.CLIENT_CERT_PATH}).\n\n"
                    "Replace it? Boards keep working without reprovisioning - they trust "
                    "the CA, not this particular certificate.", action="Replace"):
                return
        cert, _ = pki.issue_client_identity(force=True)
        session.cert_log(f"Operator client identity: {pki.CLIENT_CERT_PATH}")
        session.cert_log(f"  fingerprint: {pki.fingerprint(cert)}")
    except pki.PkiError as exc:
        ui.notify(f"PKI error: {exc}", type="negative", multi_line=True)


async def cert_issue_board() -> None:
    if not pki.ca_exists():
        ui.notify("Create the CA first (Certificate Authority > CA Status).", type="warning")
        return
    suggested = "bridge-" + session.conn["ip"].strip().replace(".", "-")
    board_id = await ask_text("Issue board identity",
                              "Board id (short, filesystem-safe - e.g. a probe serial)",
                              suggested)
    if not board_id:
        return
    ip = await ask_text("Issue board identity", "Board IP (optional)",
                        session.conn["ip"].strip())
    force = False
    if pki.board_info(board_id) is not None:
        force = await confirm(
            "Reissue board identity",
            f"Board '{board_id}' already has an identity.\n\n"
            "Reissue it? The old identity stops working once the new one is pushed "
            "and the board is reset - remember to push it afterwards.",
            action="Reissue", danger=True)
        if not force:
            return
    try:
        info = pki.issue_board_identity(board_id, ip=ip or "", force=force)
        session.cert_log(f"Issued identity for '{board_id}': {info['fingerprint_sha256']}")
        cert_refresh()
    except pki.PkiError as exc:
        ui.notify(f"PKI error: {exc}", type="negative", multi_line=True)


async def cert_delete_selected() -> None:
    """Host-side only: drops this board's json record and key material. The board
    itself keeps whatever it has saved in EEPROM until someone runs 'cert_reset' +
    'reset' on it - said plainly in the dialog, because "deleted here" and "back to
    the default identity out there" are two different things."""
    board_id = session.cert_selected
    if not board_id:
        ui.notify("Select a board in the list first.", type="info")
        return
    if not await confirm(
            "Delete identity?",
            f"Delete the host-side identity for {board_id}?\n\n"
            f"Removes json/boards/{board_id}.json and certs/boards/{board_id}/ (its "
            "private key). This cannot be undone - the certificate would have to be "
            "reissued.\n\n"
            "The board itself keeps the identity saved in its EEPROM until you run "
            "\"Reset Device to Compiled-In Default Identity\" on it.",
            action="Delete", danger=True):
        return
    pki.delete_board(board_id)
    session.cert_log(f"Deleted host-side identity: {board_id}")
    session.cert_selected = None
    cert_refresh()


async def cert_delete_all() -> None:
    """Step 1 of the clean start in docs/pki-clean-start.md."""
    boards = pki.list_boards()
    if not boards:
        ui.notify("No board identities are known.", type="info")
        return
    if not await confirm(
            "Delete ALL identities?",
            f"Delete the host-side identities of all {len(boards)} known "
            f"board{'' if len(boards) == 1 else 's'}?\n\n"
            "Empties json/boards/ and certs/boards/, private keys included. This "
            "cannot be undone.\n\n"
            "The CA, the operator client identity and the MQTT broker identity are "
            "kept - only boards are removed. Each board keeps what is saved in its "
            "own EEPROM until it is reset to the compiled-in default identity.",
            action="Delete all", danger=True):
        return
    gone = pki.delete_all_boards()
    session.cert_log("Deleted %d host-side board identit%s"
                     % (len(gone), "y" if len(gone) == 1 else "ies"))
    session.cert_selected = None
    cert_refresh()


def cert_show_device() -> None:
    if not require_tls(session.cert_log):
        return
    host, user, password = (session.conn["ip"].strip(), session.conn["user"],
                            session.conn["password"])
    session.cert_log(f"Reading {host}'s active identity...")

    def worker():
        try:
            c = bootload.Console(host, user, password)
            c.open()
            try:
                reply = c.command("cert_show", markers=(b"CERT: ",))
            finally:
                c.close()
            post("cert_log", reply)
        except Exception as exc:                           # noqa: BLE001
            post("cert_log", f"ERROR: {exc}")

    in_worker(worker)


async def cert_push_selected() -> None:
    board_id = session.cert_selected
    if not board_id:
        ui.notify("Select a board in the list first.", type="info")
        return
    if not require_tls(session.cert_log):
        return
    host, user, password = (session.conn["ip"].strip(), session.conn["user"],
                            session.conn["password"])
    if not await confirm(
            "Push identity?",
            f"Push board '{board_id}'s server certificate+key to {host} and save it?\n\n"
            "The device keeps using its CURRENT identity for existing connections; a "
            "'reset' (Bridge Parameters > Reset Device) is still needed afterwards to "
            "actually switch to the new one.", action="Push", danger=True):
        return
    session.cert_log(f"Pushing '{board_id}' to {host}...")

    def worker():
        try:
            cert_provision.push_board(host, board_id, user, password,
                                      log=lambda s: post("cert_log", s))
            post("cert_log", "Done - device still needs a reset to use it.")
        except Exception as exc:                           # noqa: BLE001
            post("cert_log", f"ERROR: {exc}")

    in_worker(worker)


async def cert_reset_device() -> None:
    if not require_tls(session.cert_log):
        return
    host, user, password = (session.conn["ip"].strip(), session.conn["user"],
                            session.conn["password"])
    if not await confirm("Reset identity?",
                         f"Revert {host}'s TLS identity to the compiled-in default? "
                         "A device 'reset' is still needed afterwards.",
                         action="Reset", danger=True):
        return
    session.cert_log(f"Resetting {host}'s identity...")

    def worker():
        try:
            c = bootload.Console(host, user, password)
            c.open()
            try:
                reply = c.command("cert_reset", markers=(b"CERT: ",))
            finally:
                c.close()
            post("cert_log", reply)
        except Exception as exc:                           # noqa: BLE001
            post("cert_log", f"ERROR: {exc}")

    in_worker(worker)


# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------

def mqtt_discover() -> None:
    """Same broadcast discovery as the Certificates tab, minus the TLS pass: here
    the boards' fingerprints are irrelevant, only where they are and which
    interface answered (that MAC is what makes "via: auto" work)."""
    base_ip = session.conn["ip"].strip()
    if not base_ip:
        ui.notify("Enter any IP on the target /24 in the IP field above first.",
                  type="warning")
        return
    session.mqtt_boards = []
    session.mqtt["discovering"] = True
    session.mqtt_log("Broadcasting on %s.255..." % ".".join(base_ip.split(".")[:3]))

    def worker():
        try:
            rows = [{"ip": r["ip"], "mac": r["mac"], "via": "", "state": ""}
                    for r in discover.broadcast_discover(base_ip)]
            post("mqtt_boards", rows)
        except Exception as exc:                           # noqa: BLE001
            post("mqtt_log", f"Discovery failed: {exc}")
            post("mqtt_boards", [])

    in_worker(worker)


def mqtt_iface_for(console, mac: str) -> Optional[str]:
    """Which interface name the given MAC belongs to, from 'showenv':

        eth0  mac 00:04:25:CA:CE:D9
        eth1  mac 00:04:25:CA:CE:DA  (applied at boot)

    The MAC comes from the board's own discovery reply, so it is the interface that
    reached this PC - exactly the one 'mqtt_broker -i' has to pin. Returns None if
    the reply cannot be matched, and the caller falls back to the firmware's own
    default rather than guessing."""
    text = console.command_text("showenv", markers=(b"plca",))
    want = mac.strip().upper()
    for line in text.replace("\r", "\n").split("\n"):
        parts = line.split()
        if len(parts) >= 3 and parts[0].startswith("eth") and parts[1] == "mac":
            if parts[2].upper() == want:
                return parts[0]
    return None


async def mqtt_start_client() -> None:
    """Give one board its broker address over its own Telnet console -
    'mqtt_broker <ip> <port> -i <iface>' - and read back what came of it.

    The broker address is this PC as seen FROM that board (discover.local_ip_toward),
    not the Bind IP field: 0.0.0.0 means "listen everywhere" here and is not an
    address a board can connect to, and this bench PC has two addresses in the same
    /24 anyway. An explicit bind address is used as-is, since then that is the only
    one that answers.

    Runtime only, like the console command it wraps: a board reset forgets it again.
    """
    row = next((r for r in session.mqtt_boards if r["ip"] == session.mqtt_selected), None)
    if row is None:
        ui.notify("Discover the boards first, then select one.", type="info")
        return
    if not require_tls(session.mqtt_log):
        return
    try:
        port = int(session.mqtt["port"])
    except (TypeError, ValueError):
        ui.notify("Port must be a number.", type="negative")
        return
    if session.mqtt_service is None or not session.mqtt_service.is_running():
        if not await confirm("Broker not running",
                             "The broker is not started, so the board will retry until "
                             "it is.\n\nSend the command anyway?", action="Send anyway"):
            return

    bind_ip = str(session.mqtt["bind"]).strip()
    broker_ip = bind_ip if bind_ip and bind_ip != "0.0.0.0" else discover.local_ip_toward(row["ip"])
    choice = session.mqtt["iface"]
    ip, mac = row["ip"], row["mac"]
    user, password = session.conn["user"], session.conn["password"]
    session.mqtt_log(f"Pointing {ip} at {broker_ip}:{port} (via {choice})...")

    def worker():
        try:
            c = bootload.Console(ip, user, password)
            c.open()
            try:
                iface = choice
                if choice == "auto":
                    iface = mqtt_iface_for(c, mac) or "eth1"
                reply = c.command(f"mqtt_broker {broker_ip} {port} -i {iface}",
                                  markers=(b"MQTT:",))
                post("mqtt_log", f"{ip}: {reply}")
                # The client connects from its own task; asking immediately would
                # only ever report "connecting".
                time.sleep(2.0)
                state = c.command("mqtt_status", markers=(b"MQTT:",))
            finally:
                c.close()
            post("mqtt_board_state", ip, iface, state)
        except Exception as exc:                           # noqa: BLE001
            post("mqtt_log", f"{ip}: ERROR: {exc}")
            post("mqtt_board_state", ip, "", "failed")

    in_worker(worker)


async def mqtt_create_identity() -> None:
    """The broker's own TLS server identity (certs/mqtt/), signed by the project CA -
    which is why nothing has to change on the boards afterwards: they already trust
    that CA, so they verify this certificate with the firmware they are running."""
    try:
        if not pki.ca_exists():
            ui.notify(f"No project CA at {pki.CA_CERT_PATH} - see the Certificates tab.",
                      type="warning", multi_line=True)
            return
        if pki.MQTT_BROKER_CERT_PATH.is_file():
            if not await confirm(
                    "Replace broker identity?",
                    f"A broker identity already exists ({pki.MQTT_BROKER_CERT_PATH}).\n\n"
                    "Replace it? A running broker keeps the old one until it is "
                    "restarted; boards need no change either way, they verify it "
                    "against the CA.", action="Replace"):
                return
        cert, _ = pki.issue_mqtt_broker_identity(force=True)
        session.mqtt_log(f"Broker identity: {pki.MQTT_BROKER_CERT_PATH}")
        session.mqtt_log(f"  fingerprint: {pki.fingerprint(cert)}")
        session.mqtt_log("Ready - \"Start Broker\" now works.")
    except pki.PkiError as exc:
        ui.notify(f"MQTT: {exc}", type="negative", multi_line=True)


async def mqtt_start_broker() -> None:
    if session.mqtt_service is None:
        return
    if session.mqtt_service.is_running():
        ui.notify("The broker is already running.", type="info")
        return
    try:
        port = int(session.mqtt["port"])
    except (TypeError, ValueError):
        ui.notify("Port must be a number.", type="negative")
        return

    # Checked here rather than left to the BrokerError five seconds later, and
    # mqtt_broker.py's own message is deliberately not passed through: it is
    # GUI-free and names its CLI fix ("run: python scripts/pki.py
    # issue-mqtt-broker"), which is correct there and wrong here - the button
    # that does it is two inches to the right.
    if not pki.CA_CERT_PATH.is_file():
        ui.notify(f"No project CA at {pki.CA_CERT_PATH} - create it on the "
                  f"Certificates tab first.", type="negative", multi_line=True,
                  close_button="OK")
        return
    if not (pki.MQTT_BROKER_CERT_PATH.is_file() and pki.MQTT_BROKER_KEY_PATH.is_file()):
        if not await confirm(
                "The broker has no TLS identity yet",
                f"The broker needs a server certificate of its own "
                f"({pki.MQTT_BROKER_CERT_PATH}), and this checkout has none - it is "
                f"generated per checkout, not shipped.\n\n"
                f"Create it now? It is signed by the project CA, so the boards need "
                f"no change: they verify it against that CA with the firmware they "
                f"are already running.", action="Create it"):
            return
        await mqtt_create_identity()
        if not pki.MQTT_BROKER_CERT_PATH.is_file():
            return

    bind_ip = str(session.mqtt["bind"]).strip() or mqtt_broker.DEFAULT_BIND_IP
    session.mqtt["status"] = "starting..."
    session.mqtt["ok"] = True

    def worker():
        try:
            session.mqtt_service.start(bind_ip=bind_ip, port=port)
        except Exception as exc:                           # noqa: BLE001
            # Catch-all on purpose. Only BrokerError was caught here at first, and
            # anything else - a bind clash, a bad certificate, a plugin import -
            # vanished with the worker thread and left the status line reading
            # "starting..." for good, with the Start button still lit. A stuck
            # spinner is a worse bug than the failure it hides.
            post("mqtt_start_failed", str(exc))

    in_worker(worker)


def mqtt_stop_broker() -> None:
    in_worker(session.mqtt_service.stop)


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def bound_log(source: dict, key: str, height: str = "h-64") -> None:
    """A read-only, monospaced log view bound to a shared string. Bound rather
    than appended to per client, so the pump never touches an element."""
    ui.textarea().bind_value_from(source, key) \
        .props("readonly outlined dense input-class=font-mono input-style=height:100%") \
        .classes(f"w-full {height}")


def mirror_table(table: ui.table, rows_of) -> None:
    """Keep a table in step with shared state from the CLIENT side.

    The pump has no browser client in context and must not touch elements, so
    tables are refreshed by a per-client timer that compares what it has against
    what the session holds. Cheap at 3 Hz for the handful of rows these tables ever
    carry, and it means two open tabs both stay current with no bookkeeping.

    The rows handed to the table are COPIES, and that is the whole point of the
    comparison working at all: the pump mutates its row dicts in place
    (mqtt_clients[id]["last_seen"], mqtt_board_state's row["state"]), so handing
    the table the live dicts would leave both sides of `!=` pointing at the same
    objects - equal forever, and only added/removed rows would ever reach the
    browser. Cell updates within a row would silently never show.
    """
    def sync():
        rows = [dict(r) for r in rows_of()]
        if table.rows != rows:
            table.rows = rows
            table.update()
    ui.timer(0.3, sync)


def connection_bar() -> None:
    with ui.row().classes("w-full items-center gap-3 flex-wrap"):
        ui.label("Bridge (Telnet)").classes("text-lg font-medium")
        ui.input("IP").bind_value(session.conn, "ip").props("dense outlined").classes("w-40")
        ui.input("User").bind_value(session.conn, "user").props("dense outlined").classes("w-32")
        ui.input("Password", password=True).bind_value(session.conn, "password") \
            .props("dense outlined").classes("w-32")
        ui.button("Connect", on_click=connect) \
            .bind_enabled_from(session.view, "connected", lambda c: not c)
        ui.button("Disconnect", on_click=disconnect).props("flat") \
            .bind_enabled_from(session.view, "connected")
        ui.space()
        # The indicator is a bound icon rather than a repainted label: the pump has
        # no browser client in context and can only change state, never elements.
        ui.icon("circle") \
            .bind_name_from(session.view, "connected",
                            lambda c: "check_circle" if c else "radio_button_unchecked") \
            .bind_text_color_from(session.view, "connected",
                                  lambda c: "positive" if c else "grey")
        ui.label().bind_text_from(session.view, "status").classes("text-sm text-grey-7")
        ui.label().bind_text_from(session.view, "progress").classes("text-sm text-primary")


class TerminalView:
    """One xterm.js instance per browser client, fed from the shared backlog.

    The cursor is per client, the byte stream is not: that is what lets a reload or
    a second tab pick up the session instead of starting blank, without a second
    consumer competing for the reader thread's queue.
    """

    def __init__(self) -> None:
        self.cursor = 0
        self.term = ui.xterm(options={
            "fontFamily": "Consolas, 'DejaVu Sans Mono', monospace",
            "fontSize": 13,
            "convertEol": False,
            "cursorBlink": True,
            "scrollback": 5000,
        }, on_data=lambda e: send_terminal(e.data)).classes("w-full h-[34rem]")
        ui.timer(POLL_S, self.drain)
        # xterm.js sizes itself to whatever the container was when it initialised,
        # which on a fresh page load is before the tab panel has its final width.
        # One fit() shortly after connecting fixes the column count.
        ui.timer(0.5, self.term.fit, once=True)

    def drain(self) -> None:
        data, self.cursor = session.term_since(self.cursor)
        if data:
            self.term.write(data)


def send_terminal(data: str) -> None:
    """Keystrokes from xterm.js straight onto the wire.

    latin-1, matching what the reader thread decodes with and what the firmware's
    console speaks - the board is not UTF-8, and encoding a pasted character into
    two bytes here would put two characters into its line editor.
    """
    link = session.channel.link
    if not link:
        return
    try:
        link.write(data.encode("latin-1", "ignore"))
    except OSError as exc:
        session.status(f"write failed: {exc}")


def terminal_tab() -> None:
    with ui.row().classes("w-full items-center gap-2"):
        ui.label("Type straight into the terminal - xterm.js sends every keystroke, "
                 "including arrows, Tab and Ctrl-C, to the board's console.") \
            .classes("text-sm text-grey-7")
        ui.space()
        ui.button("Ctrl-C", on_click=lambda: send_terminal("\x03")).props("flat dense")
    TerminalView()


def quick_commands() -> None:
    """The Quick Commands panel, as data rather than one call per button - a new
    command later is one appended tuple. "Write Environment" (not "Write All"): it
    does not just set the fields (setenv only touches the RAM copy), saveenv also
    puts them in the EEPROM, so the name says something durable landed in the
    device."""
    groups = [
        ("Environment", [
            ("Read Environment", read_environment, True),
            ("Write Environment", write_environment, True),
            ("Save to JSON", save_bridge_json, False),
            ("Open from JSON", load_bridge_json, False),
        ]),
        ("Device", [
            ("Mirror: Enable", lambda: run_cmd("mirror 1"), True),
            ("Mirror: Disable", lambda: run_cmd("mirror 0"), True),
            ("Sniffer: Enable", lambda: run_cmd("sniffer 1"), True),
            ("Sniffer: Disable", lambda: run_cmd("sniffer 0"), True),
            ("Read Stats", lambda: run_cmd("stats"), True),
            ("Memory Info", lambda: run_cmd("meminfo"), True),
            ("Memory Overview", memory_overview, True),
            ("Build Timestamp", lambda: run_cmd("timestamp"), True),
            ("Reset Device", reset_device, True),
        ]),
    ]
    for title, buttons in groups:
        with ui.card().classes("w-full p-3"):
            ui.label(title).classes("text-sm font-medium opacity-70")
            with ui.grid(columns=3).classes("w-full gap-2"):
                for label, handler, needs_link in buttons:
                    btn = ui.button(label, on_click=handler).props("outline no-caps")
                    if needs_link:
                        btn.bind_enabled_from(session.view, "connected")


def flash_panel() -> None:
    """Flash, erase and the network update. All three act on the machine THIS
    PROCESS runs on - the probe is plugged in there, and so is the HEX."""
    with ui.card().classes("w-full p-3"):
        ui.label("Firmware").classes("text-sm font-medium opacity-70")
        ui.label("The HEX path and the EDBG probe are on the machine running this "
                 "server, not the one running the browser. Upload a file if they "
                 "are not the same machine.").classes("text-xs text-grey-7")
        with ui.row().classes("w-full items-center gap-2"):
            ui.input("HEX file (server-side path)").bind_value(session.hex_path, "path") \
                .props("dense outlined").classes("flex-grow")
            ui.button(icon="restore", on_click=lambda: session.hex_path.update(
                path=str(bridge_core.RELEASE_HEX))).props("flat dense") \
                .tooltip(f"back to {bridge_core.RELEASE_HEX.name}")
        ui.upload(on_upload=on_hex_upload, auto_upload=True, max_files=1,
                  label="...or upload a .hex from this browser") \
            .props("accept=.hex flat dense").classes("w-full")
        with ui.grid(columns=3).classes("w-full gap-2"):
            ui.button("Flash (probe)", on_click=lambda: pick_probe("flash")) \
                .props("outline no-caps")
            ui.button("Erase chip...", on_click=lambda: pick_probe("erase")) \
                .props("outline no-caps color=negative")
            ui.button("Bootload (network)", on_click=bootload_start) \
                .props("outline no-caps").bind_enabled_from(session.view, "connected")


def bootload_panel() -> None:
    """Shown in place of a modal dialog: a dialog belongs to one browser client,
    and this update is a property of the session - every open tab should see it."""
    with ui.card().classes("w-full p-3").bind_visibility_from(session.bootload, "active"):
        ui.label("Firmware update over the network").classes("text-sm font-medium")
        ui.label().bind_text_from(session.bootload, "header").classes("text-xs font-mono")
        ui.label().bind_text_from(session.bootload, "caption").classes("text-sm")
        ui.linear_progress(show_value=False).bind_value_from(session.bootload, "pct")
        bound_log(session.bootload, "log", "h-48")
        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=bootload_cancel).props("flat") \
                .bind_enabled_from(session.bootload, "cancellable")
            ui.button("Close", on_click=lambda: session.bootload.update(active=False)) \
                .bind_enabled_from(session.bootload, "done")


def parameters_tab() -> None:
    if session.env_model_error:
        ui.label(f"No environment model loaded - {session.env_model_error}") \
            .classes("text-negative")
        ui.label("The fields below are missing because the model describes them: "
                 "which fields this environment has, what they are called, and which "
                 "command writes them.").classes("text-sm text-grey-7")
        return

    with ui.row().classes("w-full gap-4 items-start no-wrap"):
        # LEFT: the parameters themselves
        with ui.column().classes("gap-1 flex-grow"):
            # The identity line, gray while the model fits and red when it doesn't.
            # Two labels rather than one with a bound color: NiceGUI can bind an
            # element's text, value, visibility and enabled state, but not its CSS
            # classes - and the color is the whole point here.
            for ok, style in ((True, "text-sm text-grey-7"), (False, "text-sm text-negative")):
                ui.label().bind_text_from(session.view, "identity").classes(style) \
                    .bind_visibility_from(session.view, "identity_ok", lambda v, ok=ok: v is ok)

            # No read/write per field: the environment is read as a whole (one
            # showenv) and written as a whole (one saveenv). Writing a single field
            # in isolation makes no sense here, unlike the register table - there
            # each register is its own independent access, here it is one shared
            # record.
            default_applies = session.entry().get("commands", {}).get("persist", "saveenv")
            for key, fld in session.fields.items():
                with ui.row().classes("items-center gap-2 w-full"):
                    ui.label(fld.get("label", key) + ":").classes("w-36 text-right text-sm")
                    field = ui.input().bind_value(session.values, key) \
                        .props("dense outlined").classes("flex-grow max-w-md")
                    if fld.get("description"):
                        field.tooltip(fld["description"])
                    # When the value takes effect -- but ONLY if it deviates from
                    # the normal case. For twelve of the fourteen fields that is
                    # saveenv, and a note repeating it under every one of them would
                    # just repeat the label. What is left are the two that genuinely
                    # surprise: the MAC only after a reset, the mirror only on the
                    # next boot.
                    applies = fld.get("applies")
                    if applies and applies != default_applies:
                        ui.label(applies).classes("text-xs text-negative")

        # RIGHT: quick commands, firmware, and the command output
        with ui.column().classes("gap-2 w-[34rem] shrink-0"):
            quick_commands()
            flash_panel()
            bootload_panel()
            with ui.row().classes("w-full items-center"):
                ui.label("Command output").classes("text-sm font-medium")
                ui.space()
                ui.button("Clear", on_click=lambda: session.view.update(log="")) \
                    .props("flat dense no-caps")
            bound_log(session.view, "log", "h-80")


def registers_tab() -> None:
    if session.model_error:
        ui.label(f"No register model loaded - {session.model_error}").classes("text-negative")
        ui.label("The register table stays empty. The file describes the LAN8651 "
                 "register set: addresses, bit fields, provenance.") \
            .classes("text-sm text-grey-7")
        return

    # Provenance line: which document, which revision, how much of it was verified.
    # Deliberately at the top and not in a help text - whoever reads registers
    # should see what they are relying on.
    ui.label(bridge_core.model_source_line(session.model)).classes("text-sm text-grey-7")

    with ui.row().classes("w-full gap-2 items-center"):
        ui.button("Bulk Read All", on_click=bulk_read_registers, icon="refresh") \
            .props("no-caps").bind_enabled_from(session.view, "connected")
        ui.button("Bulk Write All", on_click=bulk_write_registers, icon="save") \
            .props("no-caps color=negative").bind_enabled_from(session.view, "connected")
        ui.button("Save to JSON", on_click=save_registers_json).props("outline no-caps")
        ui.button("Open from JSON", on_click=load_registers_json).props("outline no-caps")
        ui.space()
        ui.label().bind_text_from(session.view, "progress").classes("text-sm text-primary")

    # Built one group at a time, when that group's tab is first opened.
    #
    # Not premature: the model has 183 registers, each with a two-way bound input
    # and a bound bit-field block, and building all six groups up front put ~8600
    # active bindings on the page. NiceGUI re-evaluates every one of them ten times
    # a second, which cost 20-50 ms per pass - a page that burns a third of a core
    # while nobody is looking at it. One group at a time is 20-40 registers, and
    # the groups nobody opens cost nothing.
    built: set = set()

    with ui.tabs().classes("w-full") as group_tabs:
        tabs = {name: ui.tab(name) for name in session.reg_categories}
    first = next(iter(session.reg_categories), None)
    with ui.tab_panels(group_tabs, value=tabs[first] if first else None).classes("w-full"):
        panels = {name: ui.tab_panel(tabs[name]) for name in session.reg_categories}

    def build(name: str) -> None:
        if name in built or name not in panels:
            return
        built.add(name)
        with panels[name]:
            with ui.column().classes("gap-0 w-full"):
                for addr in session.reg_categories[name]:
                    register_row(addr)

    def on_group(event) -> None:
        # The event value is the tab NAME, which is what ui.tab() was given.
        build(str(event.value))

    group_tabs.on_value_change(on_group)
    if first:
        build(first)


def register_row(addr: str) -> None:
    meta = session.reg_meta[addr]
    with ui.row().classes("items-center gap-2 w-full"):
        ui.label(addr).classes("font-mono text-xs w-28")
        ui.label(meta["name"] or meta["description"][:20]).classes("text-xs w-52 truncate")
        # Registers with an errata entry get marked. Without the mark, a register
        # whose value doesn't mean, per the errata, what it says would look just
        # like any other - that is exactly the misleading impression at stake.
        if meta["errata"]:
            items = ", ".join(e.get("item", "?") for e in meta["errata"])
            ui.label(f"⚠ {items}").classes("text-xs text-negative w-28 truncate")
        else:
            ui.label().classes("w-28")
        ui.input().bind_value(session.reg_values, addr) \
            .props("dense outlined input-class=font-mono").classes("w-36")
        # No bind_enabled_from on these two: that would be another 366 bindings on
        # the busiest tab, for a state the handlers check anyway (require_link
        # notifies rather than failing silently). The bulk buttons above, of which
        # there are two, do carry the binding.
        ui.button("Read", on_click=lambda a=addr: read_register(a)).props("flat dense no-caps")
        ui.button("Write", on_click=lambda a=addr: write_register(a)).props("flat dense no-caps")
    if meta["bitfields"] or meta["errata"]:
        ui.html().bind_content_from(
            session.reg_values, addr,
            lambda v, m=meta: register_detail_html(m, v))


def testmodes_tab() -> None:
    ui.label("Test modes disconnect the T1S link - the bridge is unreachable while "
             "one is active.").classes("text-negative")
    ui.label(f"Register: T1STSTCTL ({bridge_core.TESTMODE_REGISTER}), bits 15:13. "
             "Background and measurement setup: docs/LAN8651_TEST_MODES.md") \
        .classes("text-sm text-grey-7")

    # Mode 0 first and always reachable, regardless of which test mode is running -
    # no auto-revert field, "normal operation" has no duration.
    with ui.card().classes("w-full p-3"):
        ui.label("Mode 0 - Normal Operation").classes("font-medium")
        ui.label("Ends any active test mode and restores normal T1S operation.") \
            .classes("text-sm")
        with ui.row():
            ui.button("Return to Normal Mode",
                      on_click=lambda: run_cmd(bridge_core.testmode_command(0))) \
                .props("no-caps").bind_enabled_from(session.view, "connected")
            ui.button("Read Current Mode",
                      on_click=lambda: run_cmd(f"lan_read {bridge_core.TESTMODE_REGISTER}")) \
                .props("outline no-caps").bind_enabled_from(session.view, "connected")

    with ui.grid(columns=2).classes("w-full gap-3"):
        for mode, title, description in bridge_core.TEST_MODES:
            with ui.card().classes("p-3"):
                ui.label(f"Mode {mode} - {title}").classes("font-medium")
                ui.label(description).classes("text-sm whitespace-pre-wrap")
                with ui.row().classes("items-center gap-2 mt-2"):
                    ui.input("Auto-revert (sec, empty = until changed)") \
                        .bind_value(session.testmode_timeouts, str(mode)) \
                        .props("dense outlined").classes("w-64")
                    ui.button(f"Start Test Mode {mode}",
                              on_click=lambda m=mode: run_cmd(bridge_core.testmode_command(
                                  m, session.testmode_timeouts[str(m)]))) \
                        .props("no-caps color=negative") \
                        .bind_enabled_from(session.view, "connected")

    ui.label("The command output is on the Bridge Parameters tab.") \
        .classes("text-xs text-grey-7")


def certificates_tab() -> None:
    if pki is None:
        ui.label("cryptography is not installed - this tab needs it (pip install -r "
                 "scripts/requirements.txt).").classes("text-negative")
        return

    with ui.row().classes("w-full gap-4 items-start no-wrap"):
        with ui.column().classes("gap-3 flex-grow"):
            # Two different questions, hence two lists: what is actually out there
            # on the wire right now, and what this workstation has issued an
            # identity for. A board can be reachable without an identity yet, or
            # have an identity but be powered off.
            ui.label("Discovered on network").classes("text-sm font-medium")
            ui.button("Discover Boards", on_click=cert_scan, icon="search") \
                .props("no-caps").bind_enabled_from(session.cert, "scanning",
                                                    lambda s: not s)
            found = ui.table(
                columns=[{"name": c, "label": c, "field": c, "align": "left"}
                         for c in ("ip", "mac", "board_id", "fingerprint")],
                rows=[], row_key="ip", selection="single").classes("w-full")
            mirror_table(found, lambda: session.cert_found)

            # on_select, not .on("selection", ...): the table registers its own raw
            # handler at construction to fill `selected`, and only handlers added
            # through on_select are guaranteed to run after it has.
            def on_found(_e):
                if found.selected:
                    # Selecting a discovered board points the connection fields at
                    # it - that is what one wants next in every case.
                    session.conn["ip"] = found.selected[0]["ip"]
                    board_id = found.selected[0]["board_id"]
                    if board_id != "(unregistered)":
                        session.cert_selected = board_id
            found.on_select(on_found)

            ui.label("Known boards (json/boards/*.json)").classes("text-sm font-medium mt-2")
            known = ui.table(
                columns=[{"name": c, "label": c, "field": c, "align": "left"}
                         for c in ("board_id", "ip", "fingerprint", "created")],
                rows=[], row_key="board_id", selection="single").classes("w-full")
            mirror_table(known, lambda: session.cert_known)

            def on_known(_e):
                session.cert_selected = known.selected[0]["board_id"] if known.selected else None
            known.on_select(on_known)

        with ui.column().classes("gap-2 w-[30rem] shrink-0"):
            with ui.card().classes("w-full p-3"):
                ui.label("Certificate Authority").classes("text-sm font-medium opacity-70")
                ui.button("CA Status", on_click=cert_ca_status).props("outline no-caps").classes("w-full")
                # certs/client/ is generated per checkout, not shipped in the repo,
                # so a fresh clone needs a way to make it that isn't "go read
                # pki.py's CLI".
                ui.button("Create Operator Client Identity", on_click=cert_issue_client) \
                    .props("outline no-caps").classes("w-full")
            with ui.card().classes("w-full p-3"):
                ui.label("Board identities").classes("text-sm font-medium opacity-70")
                ui.button("Refresh List", on_click=cert_refresh).props("outline no-caps").classes("w-full")
                ui.button("Issue New Board Identity...", on_click=cert_issue_board) \
                    .props("outline no-caps").classes("w-full")
                ui.button("Delete Selected Identity", on_click=cert_delete_selected) \
                    .props("outline no-caps color=negative").classes("w-full")
                # Step 1 of a clean start (docs/pki-clean-start.md) - a bench being
                # started over has to get back to that state before anything is
                # reissued.
                ui.button("Delete ALL Identities...", on_click=cert_delete_all) \
                    .props("outline no-caps color=negative").classes("w-full")
                # No "open folder": os.startfile would open a window on the server,
                # where nobody is sitting. The path is the useful part anyway.
                ui.label(f"Board key material: {pki.BOARDS_CERT_DIR}") \
                    .classes("text-xs text-grey-7 break-all")
            with ui.card().classes("w-full p-3"):
                ui.label("Selected device (ip/user/password above)") \
                    .classes("text-sm font-medium opacity-70")
                ui.button("Show Device's Active Identity", on_click=cert_show_device) \
                    .props("outline no-caps").classes("w-full")
                ui.button("Push Selected Board Identity to Device", on_click=cert_push_selected) \
                    .props("outline no-caps").classes("w-full")
                ui.button("Reset Device to Compiled-In Default Identity",
                          on_click=cert_reset_device) \
                    .props("outline no-caps color=negative").classes("w-full")
            ui.label("Log").classes("text-sm font-medium")
            bound_log(session.cert, "log", "h-72")


def mqtt_tab() -> None:
    if mqtt_broker is None:
        ui.label("amqtt is not installed - this tab needs it (pip install -r "
                 "scripts/requirements.txt).").classes("text-negative")
        return

    with ui.card().classes("w-full p-3"):
        ui.label("Broker (TLS/mTLS only - no plaintext listener exists)") \
            .classes("text-sm font-medium opacity-70")
        with ui.row().classes("items-center gap-2 flex-wrap"):
            ui.input("Bind IP").bind_value(session.mqtt, "bind") \
                .props("dense outlined").classes("w-40")
            ui.number("Port", format="%d").bind_value(session.mqtt, "port") \
                .props("dense outlined").classes("w-28")
            ui.button("Start Broker", on_click=mqtt_start_broker).props("no-caps") \
                .bind_enabled_from(session.mqtt, "running", lambda r: not r)
            ui.button("Stop Broker", on_click=mqtt_stop_broker).props("outline no-caps") \
                .bind_enabled_from(session.mqtt, "running")
            # certs/mqtt/ is generated per checkout, not shipped, so on a fresh
            # clone the first "Start Broker" fails on a missing identity. Making
            # that a button here rather than only an error message pointing at
            # pki.py's CLI - this tab is where someone hits the problem.
            ui.button("Create Broker Identity", on_click=mqtt_create_identity) \
                .props("outline no-caps")
        # Two labels, blue and red: NiceGUI cannot bind an element's CSS classes,
        # and a failed start has to look different from "listening".
        for ok, style in ((True, "text-sm text-primary"), (False, "text-sm text-negative")):
            ui.label().bind_text_from(session.mqtt, "status").classes(style)                 .bind_visibility_from(session.mqtt, "ok", lambda v, ok=ok: v is ok)

    with ui.card().classes("w-full p-3"):
        # The other half of the job: the boards are MQTT *clients*, and each one
        # only starts publishing once its 'mqtt_broker' command has been given over
        # its own console - a per-board, runtime-only setting. Doing that by hand
        # means one Telnet session per board, so the same discovery panel the
        # Certificates tab has is here too, with "start the client" as its action.
        ui.label("Boards on network - start their MQTT client remotely") \
            .classes("text-sm font-medium opacity-70")
        with ui.row().classes("items-center gap-2"):
            ui.button("Discover Boards", on_click=mqtt_discover, icon="search") \
                .props("no-caps").bind_enabled_from(session.mqtt, "discovering",
                                                    lambda d: not d)
            ui.button("Start MQTT Client on Selected Board", on_click=mqtt_start_client) \
                .props("outline no-caps")
            # "auto" is the useful default and not a guess: the discovery reply
            # carries the MAC of the interface that answered US, and 'showenv' lists
            # both interfaces' MACs - so the interface facing the PC can be
            # identified rather than assumed. The firmware's own default is eth1,
            # which is wrong for any board reaching the PC through the bridge's T1S
            # side (Follower B has no 100BASE-TX PHY at all: RemoteBind fails).
            ui.label("via:").classes("text-sm")
            ui.select(["auto", "eth0", "eth1"]).bind_value(session.mqtt, "iface") \
                .props("dense outlined").classes("w-28")
        boards = ui.table(
            columns=[{"name": c, "label": c, "field": c, "align": "left"}
                     for c in ("ip", "mac", "via", "state")],
            rows=[], row_key="ip", selection="single").classes("w-full")
        mirror_table(boards, lambda: session.mqtt_boards)

        def on_board(_e):
            session.mqtt_selected = boards.selected[0]["ip"] if boards.selected else None
        boards.on_select(on_board)

    with ui.row().classes("w-full gap-4 items-start no-wrap"):
        with ui.column().classes("flex-grow gap-1"):
            ui.label("Connected clients (live)").classes("text-sm font-medium")
            clients = ui.table(
                columns=[{"name": c, "label": c, "field": c, "align": "left"}
                         for c in ("client_id", "remote", "cert_cn", "last_seen",
                                   "last_payload")],
                rows=[], row_key="client_id").classes("w-full")
            mirror_table(clients, lambda: list(session.mqtt_clients.values()))
        with ui.column().classes("w-[34rem] shrink-0 gap-1"):
            ui.label("Live message / event log").classes("text-sm font-medium")
            bound_log(session.mqtt, "log", "h-80")


def help_tab() -> None:
    ui.markdown(f"""
### Bridge Status & Configuration - web front end

The same tool as `run_gui_telnet.bat`, in a browser. This process holds the one
TLS/Telnet connection to the board; the page is the display. A second browser tab
is a second view of the same session, not a second connection.

**Tabs**

* **Bridge Parameters** - the EEPROM record read as a whole (`showenv`) and written
  as a whole (`setenv` per field, then `saveenv`), plus the quick commands, the
  firmware panel and the command output.
* **LAN8651 Registers** - {len(session.reg_meta)} registers in
  {len(session.reg_categories)} MMS groups, from `json/lan8651_model.json`. Read
  and write per register, or in bulk. Bit fields are decoded live from the value in
  the field.
* **Test Modes** - the four IEEE test modes plus a way back to normal operation.
  These disconnect the T1S link; the board is unreachable while one is active.
* **Terminal** - the board's console, through xterm.js. Every keystroke goes
  straight to the device.
* **Certificates** - this project's small PKI: the CA, the operator identity, and
  one identity per board, plus pushing one onto a device.
* **MQTT** - the TLS-only broker the boards publish to, and pointing a board at it.

**Where things happen**

Flashing over the EDBG probe, the certificate folders, the MQTT broker and the
"last local build" figures are all on the machine running this server
(`{Path.cwd()}`), not the machine running the browser. Where the Tk GUI opens a
file dialog, this page offers a server-side path and an upload.

**Configuration**

`{bridge_core.CONFIG_FILE}` - shared with the Tk GUI, so the IP and credentials
carry over between the two.

**Security**

Bound to 127.0.0.1 by default. There is no login in front of this page, and
whoever opens it can reconfigure and erase the board.
""").classes("max-w-4xl")


@ui.page("/")
def index() -> None:
    ui.dark_mode(True)
    with ui.column().classes("w-full max-w-[110rem] mx-auto p-4 gap-2"):
        connection_bar()
        with ui.tabs().classes("w-full") as tabs:
            tab_param = ui.tab("Bridge Parameters", icon="tune")
            tab_reg = ui.tab("LAN8651 Registers", icon="memory")
            tab_test = ui.tab("Test Modes", icon="science")
            tab_term = ui.tab("Terminal", icon="terminal")
            tab_cert = ui.tab("Certificates", icon="verified_user")
            tab_mqtt = ui.tab("MQTT", icon="hub")
            tab_help = ui.tab("Help", icon="help")
        with ui.tab_panels(tabs, value=tab_param).classes("w-full"):
            for tab, builder in ((tab_param, parameters_tab), (tab_reg, registers_tab),
                                 (tab_test, testmodes_tab), (tab_term, terminal_tab),
                                 (tab_cert, certificates_tab), (tab_mqtt, mqtt_tab),
                                 (tab_help, help_tab)):
                with ui.tab_panel(tab):
                    builder()


def port_in_use(host: str, port: int) -> bool:
    """Whether something is already listening there.

    Checked before ui.run, because uvicorn's own failure for this is
    "[Errno 10048] error while attempting to bind on address ... only one usage of
    each socket address ... is normally permitted", printed AFTER a cheerful
    "NiceGUI ready to go on http://..." - which reads as if it had started. The
    usual cause is simply a second instance, and the fix is to close the first one
    or pass --port.

    SO_REUSEADDR is deliberately NOT set: on Windows it would let this bind
    succeed alongside the existing listener and report the port as free.
    """
    import socket as _socket
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1",
                        help="interface to bind to (default 127.0.0.1 - see the "
                             "security note in this file's docstring before changing it)")
    parser.add_argument("--port", type=int, default=8088, help="TCP port (default 8088)")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open a browser window on startup")
    args = parser.parse_args()

    import dep_check
    if not dep_check.ensure_dependencies(
            hard=[("nicegui", "nicegui"), ("cryptography", "cryptography"),
                  ("amqtt", "amqtt")],
            optional=[("serial", "pyserial")]):
        sys.exit(0)

    if args.host not in ("127.0.0.1", "localhost"):
        print(f"WARNING: binding to {args.host} - anyone who can reach this port can "
              f"log into the board, rewrite its EEPROM and erase it over the probe. "
              f"There is no authentication in front of this page.", file=sys.stderr)

    probe_host = "127.0.0.1" if args.host == "localhost" else args.host
    if port_in_use(probe_host, args.port):
        print(f"ERROR: {probe_host}:{args.port} is already in use - most likely this "
              f"tool is already running.\n"
              f"       Close that instance and open http://{probe_host}:{args.port} "
              f"instead, or start this one on another port:\n"
              f"           python scripts\\bridge_web_telnet.py --port {args.port + 1}",
              file=sys.stderr)
        sys.exit(1)

    if pki is not None:
        cert_refresh()
    app.timer(POLL_S, pump)
    app.timer(MQTT_POLL_S, mqtt_pump)
    app.on_shutdown(disconnect)
    ui.run(host=args.host, port=args.port, title="Bridge (Telnet)",
           show=not args.no_browser, reload=False, favicon="\N{ELECTRIC PLUG}")


# NiceGUI re-imports this module as "__mp_main__" in its reload/multiprocessing
# setup, so the usual __main__ guard alone is not enough.
if __name__ in {"__main__", "__mp_main__"}:
    main()
