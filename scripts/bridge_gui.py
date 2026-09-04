#!/usr/bin/env python3
"""
Bridge Status & Configuration GUI

Operates the T1S/100BASE-TX bridge over the EDBG COM port (115200 8N1):
bridge parameters, LAN8651 registers, IEEE test modes, and a terminal.

Standalone apart from two pip packages: sv-ttk (required, for the theme) and pyserial
(optional, without COM port access the tool still runs) - dep_check.py
checks both at startup and offers to run setup_venv.bat if needed.
Also needs bridge_config.json (lives in the json folder in the repo root, two levels above
this script). No cli.py, no test_lan8651.py --
those open the COM port themselves and collide with this GUI's connection,
because the port is exclusive under Windows. All commands go through the
one open link.
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, simpledialog, filedialog
import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import re
from pathlib import Path
from typing import Dict, Optional, List
import queue
import time
import winreg

try:
    import serial
except ImportError:
    serial = None


def _console_python() -> str:
    """Never use sys.executable blindly for the flash_same54.py subprocess call: if
    this GUI is running under pythonw.exe (no second console window), flash_same54.py
    builds [sys.executable, "-m", "pyocd", ...] from it - i.e. "pythonw.exe -m pyocd
    erase --chip ...". That GRANDCHILD process loses its stdout somewhere in the
    chain under pythonw: the flash/erase command still echoes, then nothing more -
    not even "Chip erase complete", even though the erase itself completed fine on
    the real board (measured on the sibling project this was ported from: under
    console python.exe it streams every sector line in under 10s, under pythonw.exe
    it goes silent right after the command line). python.exe sits next to
    pythonw.exe in a standard install; falls back to sys.executable if that is not
    there."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.is_file():
            return str(candidate)
    return sys.executable


PYOCD_PYTHON = _console_python()

# Configuration file path - bridge_config.json lives in json/ (repo root), not
# next to this script.
CONFIG_FILE = Path(__file__).parent.parent / "json" / "bridge_config.json"

# Flash/Erase over the EDBG probe (SWD), independent of the open serial link - see
# flash_current_hex()/erase_chip(). RELEASE_HEX points at the tracked release\ HEX
# build.bat refreshes after every successful build (CLAUDE.md section 2) - like the
# related project's GUI, so a fresh clone can flash without building first. Not the
# dist\ build output directly: that one only exists after a local build, and picking
# it as the default would silently flash a stale/never-built path on a fresh clone.
# FLASH_SAME54_SCRIPT already knows how to find the SAME54_DFP pack and pick a
# probe, this GUI only adds the picker for "which probe, if more than one is
# connected" and the confirmation dialogs.
RELEASE_HEX = Path(__file__).parent.parent / "release" / "bridge_lan865x_100baseT.hex"
FLASH_SAME54_SCRIPT = Path(__file__).parent / "flash_same54.py"
# Typed into the erase confirmation dialog, not just clicked - a chip erase is not
# reversible (wipes firmware AND the emulated EEPROM, both live in the same flash).
ERASE_CONFIRM_WORD = "ERASE"

# The register model: addresses, mnemonics, bitfields, provenance. Kept separate from the
# configuration because it is a different kind of thing -- a reference derived from the
# datasheet that someone debugging an error relies on. The GUI reads it
# and NEVER writes it; values and session state belong in bridge_config.json.
# If something is wrong, fix this file, not this source code -- then run
# "python scripts\check_register_model.py". lan8651_model.json lives in json\
# (repo root), not next to this script.
MODEL_FILE = Path(__file__).parent.parent / "json" / "lan8651_model.json"

# The environment model: which fields the EEPROM record has, how they are read from showenv
# and with which CLI command they are written -- per identifier and version.
# Firmware variants share the EEPROM offset but not the layout; that's why
# the identifier is read from the device and checked against this model instead of guessing it.
# env_model.json also lives in json\ (repo root).
ENV_MODEL_FILE = Path(__file__).parent.parent / "json" / "env_model.json"

# Default configuration
# Defaults for when bridge_config.json is missing. The register tab then does NOT
# come along -- it comes from the datasheet (LAN8650-1-Data-Sheet-60001734.pdf,
# chapter 11, 182 registers) and lives exclusively in bridge_config.json.
# This used to hold hand-added addresses; four of them didn't exist
# at all and two had the wrong name, see CLAUDE.md section 6.
DEFAULT_CONFIG = {
    "comport": "COM8",
    "baudrate": 115200,
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

# Terminal-spezifische Konstanten (aus gui_term.py)
KEYSYM_BYTES = {
    "Return": b"\r",
    "KP_Enter": b"\r",
    "BackSpace": b"\x08",
    "Delete": b"\x1b[3~",
    "Left": b"\x1b[D",
    "Right": b"\x1b[C",
    "Up": b"\x1b[A",
    "Down": b"\x1b[B",
    "Home": b"\x1b[H",
    "End": b"\x1b[F",
    "Escape": b"\x1b",
    "Tab": b"\t",
}

IGNORED_KEYSYMS = {
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
    "Caps_Lock", "Num_Lock", "Scroll_Lock", "Win_L", "Win_R", "App", "Insert",
    "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12",
}

CONTROL_BIT = 0x0004
POLL_MS = 30
BLINK_MS = 500
MAX_VIEW_LINES = 1000
BAUD = 115200


class Screen:
    """Byte-to-text converter (aus gui_term.py)"""

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


class Link:
    """Serial connection mit Reader-Thread (aus gui_term.py)"""

    def __init__(self, port, q, baud=BAUD):
        self.port = port
        self.baud = baud
        self.q = q
        self.ser = None
        self.stop = threading.Event()
        self.thread = None

    def open(self):
        if serial is None:
            raise ImportError("pyserial not installed")
        self.ser = serial.Serial(self.port, self.baud, timeout=0.05)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        while not self.stop.is_set():
            try:
                n = self.ser.in_waiting
                data = self.ser.read(n if n else 1)
            except Exception as error:
                if not self.stop.is_set():
                    self.q.put((self.port, "lost", str(error)))
                return
            if data:
                self.q.put((self.port, "data", data))

    def write(self, data):
        if self.ser is None:
            raise OSError("not connected")
        self.ser.write(data)

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=0.5)
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None


def get_available_com_ports() -> List[str]:
    """
    Detect available COM ports on Windows.

    First tries pyserial, falls back to Windows Registry.
    """
    # Try pyserial first (most reliable)
    try:
        from serial.tools import list_ports
        ports = [port.device for port in list_ports.comports()]
        return sorted(ports) if ports else []
    except ImportError:
        pass

    # Fallback: Windows Registry
    try:
        com_ports = []
        reg_path = r"HARDWARE\DEVICEMAP\SERIALCOMM"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path) as key:
            for i in range(winreg.QueryInfoKey(key)[1]):
                name, value, _ = winreg.EnumValue(key, i)
                if value.startswith("COM"):
                    com_ports.append(value)
        return sorted(com_ports) if com_ports else []
    except Exception:
        pass

    # Last resort: check common ports
    common_ports = ["COM1", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9"]
    available = []
    for port in common_ports:
        try:
            import serial
            s = serial.Serial(port, timeout=0)
            s.close()
            available.append(port)
        except Exception:
            pass
    return available if available else common_ports


class ResponseParser:
    """Evaluates the device's response lines.

    This used to run a subprocess on cli.py. That didn't work: cli.py opens
    the COM port itself (cli.py:47), and it is exclusive under Windows -- as long as
    the GUI is connected, the subprocess gets "access denied". All
    commands therefore go through the already-open link
    (BridgeGUI.send_command_via_link); only the parsing remains here.
    """

    def parse_register_read(self, output: str) -> Optional[str]:
        """Extract the value from 'LAN865X Read OK: Addr=... Value=...'."""
        match = re.search(r'Value=0x([0-9A-Fa-f]+)', output)
        if match:
            return "0x" + match.group(1)
        return None


# sv-ttk semantic colors, chosen for legibility on its dark background
# (#1c1c1c); the light variant reuses the colors this file used before the
# sv-ttk theme existed, already tuned for a light background. See
# BridgeGUI._restore_semantic_colors for why these are needed at all.
RED_DARK = "#ff6b6b"
GREEN_DARK = "#4ac94a"
MUTED_DARK = "#9a9a9a"
RED_LIGHT = "#b00000"
GREEN_LIGHT = "#009900"
MUTED_LIGHT = "#555555"


class BridgeGUI:
    def __init__(self, root: tk.Tk, dark: bool = True):
        # Before self.setup_ui() below (which builds the top bar's ~8
        # buttons): a style-level ttk.Style().configure() applies to every
        # button built afterwards, so setting it this early avoids a
        # resize/flash and survives sv-ttk's own idle-task restyle later -
        # verified, see docs/FALLSTRICKE.md (2026-08-26).
        self._dark = dark
        self._tighten_button_style()
        self.root = root
        self.root.title("Bridge Status & Configuration")
        # 1000 px was enough until the bulk buttons moved into the top bar; that row
        # needs 1162 px (measured) and pack() does not wrap - it silently cuts off
        # whatever is furthest right. The position is set too: on the 1280 px screen
        # here Windows placed the wider window at x=78, which pushed "Open from JSON"
        # off the edge - the buttons were there, just not reachable.
        self.root.geometry(self._fitting_geometry(1220, 700))
        self.root.minsize(1180, 500)
        # Maximized from the first frame, not a later resize - the fixed geometry above
        # still matters as what "restore down" returns to and as the size on a platform
        # where 'zoomed' were ever unavailable. Windows-only tool (see winreg import), so
        # no cross-platform fallback for the state name.
        self.root.state("zoomed")

        self.config = self.load_config()
        self.model = self.load_model()
        self.env_model = self.load_env_model()
        # What the device reported about its environment. Stays None as long as nothing
        # has been read -- the GUI then does not claim to know what is in the EEPROM.
        self.env_identity: Optional[dict] = None
        self.cli = ResponseParser()
        self.result_queue = queue.Queue()
        self.connected = False
        self.port_link: Optional[Link] = None  # Global connection for CLI + terminal

        # The path last picked via "Select Hex..." -- "Flash" uses this instead of
        # always RELEASE_HEX (release\bridge_lan865x_100baseT.hex) once one has been
        # chosen. Session state only (like bridge_config.json's "values"), not
        # persisted.
        self._selected_hex_path: Path = RELEASE_HEX

        # Command responses run through their OWN queue. Otherwise
        # terminal_process_queue() (main thread, every 30 ms) and the worker thread
        # compete for the same chunks, and the response arrives torn apart -> empty fields.
        self.cmd_response_q = queue.Queue()
        self.cmd_pending = threading.Event()
        self.cmd_lock = threading.Lock()

        # Scrollable canvases (register tab per MMS group, bridge parameter tab), for the
        # ONE global mouse wheel handler in _register_wheel_canvas/_on_global_wheel.
        self._wheel_canvases: list = []
        self.root.bind_all("<MouseWheel>", self._on_global_wheel)
        self._bind_page_scroll_keys()

        # Validate saved COM port is still available
        available = get_available_com_ports()
        saved_port = self.config.get("comport", "COM8")
        if available and saved_port not in available:
            self.config["comport"] = available[0]
            self.save_config()

        self.setup_ui()

        # Auto-refresh COM ports on startup
        if available:
            self.set_status(f"Ready ({len(available)} port(s) available)")
        else:
            self.set_error_status("No COM ports detected")

        self.update_connection_indicator()

        # Start blink timer for terminal cursor
        self.root.after(BLINK_MS, self._blink_loop)

        self.process_queue()

        # Forces sv-ttk's own idle-task restyle to run NOW, before trying to
        # fix anything it just broke - it reapplies its palette via an idle
        # task the first time the event loop turns, which steamrolls every
        # ttk.Label's construction-time foreground= (the ones just built
        # above carry real information: red errata/warning text, green
        # decoded bitfield values). Verified by testing both orders, not
        # assumed - patching before that idle task has fired gets silently
        # reverted by it right afterwards. See docs/FALLSTRICKE.md (2026-08-26).
        self.root.update_idletasks()
        self._restore_semantic_colors()
        self.update_connection_indicator()  # re-assert red/green now that it will stick
        # AFTER the window has its final size (root.state("zoomed") above),
        # not before: the DWM attribute alone was set correctly (return code
        # 0 = S_OK) but the title bar visibly stayed light without a stable
        # window to repaint yet.
        self._apply_dark_titlebar(self._dark)

    def _apply_dark_titlebar(self, dark: bool) -> None:
        """Color the native Windows title bar to match - sv-ttk (and ttk in
        general) only reaches ttk/tk widgets, the title bar is the OS's own
        window chrome and has no tkinter API at all. Windows 10 (2004+) and
        Windows 11 expose it through the DWM, called here directly via ctypes.

        root.winfo_id() is the embedded CHILD window Tk hands out, not the
        real top-level HWND the title bar belongs to - GetParent() walks up
        to the one DWM actually needs; skipping that step is why naive
        versions of this recipe silently do nothing. Attribute 20 is
        DWMWA_USE_IMMERSIVE_DARK_MODE on Windows 11 and Windows 10 20H1+; 19
        was the same attribute's number on the two Windows 10 builds just
        before that, tried as a fallback.

        Setting the attribute is not enough by itself - confirmed
        first-hand: DwmSetWindowAttribute returned 0 (S_OK) yet the title
        bar stayed light. DWM only repaints the non-client area (the title
        bar) on its own schedule; SetWindowPos with SWP_FRAMECHANGED forces
        that repaint now instead of waiting for one to happen on its own
        (a resize, a focus change, ...).
        """
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            value = ctypes.c_int(1 if dark else 0)
            for attribute in (20, 19):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
            SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER, SWP_FRAMECHANGED = 0x2, 0x1, 0x4, 0x20
            ctypes.windll.user32.SetWindowPos(
                hwnd, None, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
        except OSError:
            pass  # Windows build without this DWM attribute - title bar stays light

    @staticmethod
    def _tighten_button_style() -> None:
        """sv-ttk's own TButton padding ({8 2 8 3}, see theme/dark.tcl) plus
        its SunValleyBodyFont (~14px, noticeably bigger than the default ttk
        theme's font) make each button meaningfully wider than this top bar
        was ever sized for - measured: the ~8-button top bar grows from
        ~1250px to ~1580px required width. That is enough to push the
        connection indicator and status label (packed on the right) off the
        edge of a window that isn't extra wide. Tightened back down; still
        legible, no longer silently clips the status area."""
        style = ttk.Style()
        style.configure("TButton", padding=(4, 2), font=("Segoe UI", 9))

    def _restore_semantic_colors(self) -> None:
        """Reconstruct the warning/success colors sv-ttk's idle-task recolor
        erases from each label's TEXT content instead (the only thing still
        available once that happens - color info, not just style, is gone).
        Fragile in the sense that it silently stops matching if a label's
        wording changes elsewhere in this file - the trade-off for not
        threading an explicit "this label is a warning" flag through every
        call site that builds one."""
        red = RED_DARK if self._dark else RED_LIGHT
        green = GREEN_DARK if self._dark else GREEN_LIGHT
        muted = MUTED_DARK if self._dark else MUTED_LIGHT

        env_widget = getattr(self, "_env_identity_widget", None)
        if env_widget is not None:
            env_widget.configure(foreground=muted)

        def walk(widget):
            labels = [c for c in widget.winfo_children() if isinstance(c, ttk.Label)]
            for i, label in enumerate(labels):
                text = str(label.cget("text")).strip()
                if text.startswith("⚠") or text.startswith("->"):
                    # "⚠ <errata items>" (register row) and the errata summary/
                    # implication lines in the bitfield section - all warning-red.
                    label.configure(foreground=red)
                elif text in ("AFTER RESET", "NEXT BOOT"):
                    # The "applies" hint next to mac0/mac1/mirror in Bridge Parameters.
                    label.configure(foreground=red)
                elif text.startswith("\U0001f4cb"):
                    # The bitfield section's register-description line.
                    label.configure(foreground=muted)
                elif text.startswith("["):
                    # "[bits] meaning" - the label right after it in the same row is
                    # the decoded VALUE (a StringVar, usually empty here, so it can't
                    # be matched by its own text) - identified structurally instead.
                    label.configure(foreground=muted)
                    if i + 1 < len(labels):
                        labels[i + 1].configure(foreground=green)
                elif text.startswith("Model:") or text == "no register model loaded":
                    label.configure(foreground=muted)
            for child in widget.winfo_children():
                walk(child)

        walk(self.root)

    def _register_wheel_canvas(self, canvas: tk.Canvas) -> None:
        """Register a scrollable canvas for the global mouse wheel handler."""
        self._wheel_canvases.append(canvas)

    def _bind_page_scroll_keys(self) -> None:
        """Page Up/Down scrolls the visible tab -- independent of the mouse wheel.

        Pure keyboard events are completely independent of the question "which event does
        the input device even deliver" (see _on_global_wheel), and therefore
        also work where MouseWheel never arrives -- e.g. on a
        Windows precision touchpad. Deliberately only Prior/Next (page up/down), not
        arrow keys or Home/End: those would interfere in a focused entry field or
        a combobox (moving the cursor, opening a dropdown) and would scroll the view
        along unasked. Prior/Next are unassigned there by default.

        Which canvas is currently "visible" is tracked via <<NotebookTabChanged>> on
        both notebooks (main tabs, register subtabs); the terminal's
        own Prior/Next binding on terminal_text still takes precedence there, because a
        binding on the widget instance is evaluated before "all" (bind_all).
        """
        self._active_scroll_canvas: Optional[tk.Canvas] = None

        def _scroll_active(direction):
            if self._active_scroll_canvas is not None:
                self._active_scroll_canvas.yview_scroll(direction, "pages")

        self.root.bind_all("<Prior>", lambda e: _scroll_active(-1))
        self.root.bind_all("<Next>", lambda e: _scroll_active(1))

    def _on_main_tab_changed(self, event=None):
        """Main tab changed: set the canvas scrollable there for page up/down.

        For "LAN8651 Registers" the currently selected MMS subtab applies, not this tab
        itself -- reg_notebook.select() is already valid for that, because the subtab
        notebook gets built along with the main tab.
        """
        current = self.notebook.tab(self.notebook.select(), "text")
        if current == "Bridge Parameters":
            self._active_scroll_canvas = getattr(self, "_bridge_scroll_canvas", None)
        elif current == "LAN8651 Registers":
            reg_nb = getattr(self, "_reg_notebook", None)
            self._active_scroll_canvas = (
                self._reg_tab_canvases.get(reg_nb.select()) if reg_nb else None)
        elif current == "Test Modes":
            self._active_scroll_canvas = getattr(self, "_testmodes_scroll_canvas", None)
        else:
            # Terminal/Help do not scroll via a registered canvas.
            self._active_scroll_canvas = None

    def _on_reg_tab_changed(self, event=None):
        """MMS subtab changed: adopt its canvas for page up/down.

        The register subtab notebook internally selects its first tab while building
        and in doing so fires its OWN <<NotebookTabChanged>> -- regardless of whether
        "LAN8651 Registers" is even the visible main tab. Without this check,
        this internal event overwrites the correct canvas of the bridge parameter
        tab as soon as the user's register subtabs are run through internally even
        ONCE at program startup (which Tk itself does while building).
        """
        if self.notebook.tab(self.notebook.select(), "text") != "LAN8651 Registers":
            return
        self._active_scroll_canvas = self._reg_tab_canvases.get(self._reg_notebook.select())

    def _on_global_wheel(self, event):
        """ONE handler for the whole window instead of Enter/Leave per canvas.

        Stays correct for a real mouse: ONE handler is attached globally (`bind_all`) and
        fires on every MouseWheel event, regardless of which widget technically
        received it -- it decides for itself what gets scrolled, via the actual
        pointer position (`event.x_root/y_root`) and `winfo_containing`.

        On THIS machine that alone isn't enough: an isolated test (only one canvas,
        nothing else) showed hundreds of Enter/Motion events but NOT a single
        MouseWheel, even though real scrolling happened -- the device is a Windows precision
        touchpad, whose two-finger gesture doesn't trigger WM_MOUSEWHEEL at all
        for Tk windows. Not a binding problem, the event simply never arrives. Hence
        additionally the keyboard bindings below (_bind_page_scroll_keys) as a path that does
        not depend on this event's delivery.
        """
        try:
            target = self.root.winfo_containing(event.x_root, event.y_root)
        except KeyError:
            # winfo_containing can throw this when the pointer is currently over a
            # widget of a foreign toplevel/process.
            return
        while target is not None:
            if target in self._wheel_canvases:
                target.yview_scroll(int(-event.delta / 120), "units")
                return
            target = target.master

    def _fitting_geometry(self, width: int, height: int) -> str:
        """Geometry string for a window of this size, clamped onto the visible screen.

        Shrinks the request if the screen is smaller and places the window so the right
        edge stays on screen - a window that is merely wide is a nuisance, one that hangs
        over the edge hides controls without any hint that they exist.
        """
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = min(width, screen_w - 20)
        height = min(height, screen_h - 80)
        x = max(0, (screen_w - width) // 2)
        y = max(0, min(40, (screen_h - height) // 2))
        return f"{width}x{height}+{x}+{y}"

    def load_model(self) -> dict:
        """Load the register model. If it is missing, this is STATED, not hidden.

        An empty register tab would be the worst answer: the GUI would look functional
        and simply have no registers. Whoever is debugging here should know that the
        reference is missing.
        """
        try:
            with open(MODEL_FILE, "r", encoding="utf-8") as f:
                model = json.load(f)
        except FileNotFoundError:
            messagebox.showerror(
                "Register model missing",
                f"{MODEL_FILE.name} was not found.\n\n"
                "The register tab stays empty. The file belongs next to bridge_gui.py and "
                "describes the LAN8651 register set: addresses, bit fields, provenance.")
            return {}
        except ValueError as exc:
            messagebox.showerror(
                "Register model unreadable",
                f"{MODEL_FILE.name} is not valid JSON:\n\n{exc}\n\n"
                "The register tab stays empty. Check it with: python scripts\\check_register_model.py")
            return {}
        return model

    def load_env_model(self) -> dict:
        """Load the environment model. If it is missing, the parameter tab stays empty and says so."""
        try:
            with open(ENV_MODEL_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            messagebox.showerror(
                "Environment model missing",
                f"{ENV_MODEL_FILE.name} was not found.\n\n"
                "The parameter tab stays empty. The file describes the EEPROM record "
                "per environment id and version.")
            return {}
        except ValueError as exc:
            messagebox.showerror(
                "Environment model unreadable",
                f"{ENV_MODEL_FILE.name} is not valid JSON:\n\n{exc}\n\n"
                "Check it with: python scripts\\check_env_model.py")
            return {}

    def env_entry_for(self, identity: Optional[dict]) -> dict:
        """Model entry for ONE identity -- without querying the GUI's state.

        The worker needs this: it has just pulled the identity from the same showenv output,
        but self.env_identity is only set in the main thread. If it fell back to
        env_entry(), it would interpret the values against the OLD state -- and the GUI would show
        filled fields under a line saying they aren't interpreted.
        """
        envs = self.env_model.get("environments", {})
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

    def env_entry(self) -> dict:
        """The model entry the GUI is currently working against.

        As long as the device has not reported anything, this is the only or first entry --
        the fields have to be built before anyone connects. After reading the
        identity, the matching entry is taken; if there isn't one, the result is empty
        and the GUI shows the values as not interpretable instead of making them up.
        """
        envs = self.env_model.get("environments", {})
        if not envs:
            return {}
        if self.env_identity:
            return self.env_entry_for(self.env_identity)
        return next(iter(envs.values()))

    def env_identity_label_color(self, ok: bool) -> None:
        """Gray as long as everything matches - red as soon as it doesn't."""
        widget = getattr(self, "_env_identity_widget", None)
        if widget is not None:
            widget.configure(foreground="#555" if ok else "#b00")

    def env_identity_line(self) -> str:
        """The line above the parameter tab: what is in the EEPROM and whether we can interpret it."""
        if not self.env_model:
            return "no environment model loaded"
        if not self.env_identity:
            envs = ", ".join(self.env_model.get("environments", {}))
            return f"Environment: not read yet - the model knows {envs}. 'Read Environment' asks the device."
        ident = self.env_identity
        ee = f"{ident.get('eeprom_id')} v{ident.get('eeprom_version')}"
        fw = f"{ident.get('firmware_id')} v{ident.get('firmware_version')}"
        crc = ident.get("eeprom_crc", "?")
        entry = self.env_entry()
        if entry:
            if ident.get("eeprom_id") == ident.get("firmware_id") and crc == "ok":
                note = "model fits"
            elif crc == "ok":
                # Not an error: the firmware still reads this legacy identity and has accepted the
                # record -- otherwise there would be no model entry here. On the
                # next saveenv it writes it back with the new identity.
                note = (f"legacy id, accepted by the firmware - the next "
                        f"'{entry.get('commands', {}).get('persist', 'saveenv')}' "
                        f"rewrites it as {ident.get('firmware_id')}")
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
                    f"{ident.get('firmware_variant', '')} - {note}")
        return (f"WARNING: the EEPROM reports {ee}, which this tool has no model for. "
                f"The values below are NOT interpreted. Firmware {fw} "
                f"{ident.get('firmware_variant', '')}")

    def model_source_line(self) -> str:
        """One-liner about provenance that the GUI displays -- so it never claims
        more than it can back up."""
        if not self.model:
            return "no register model loaded"
        ds = self.model.get("sources", {}).get("datasheet", {})
        er = self.model.get("sources", {}).get("errata", {})
        ver = self.model.get("verification", {})
        n_reg = sum(len(g.get("registers", {})) for g in self.model.get("groups", {}).values())
        n_ver = ver.get("registers_verified", 0)
        line = (f"Model: {ds.get('doc', '?')} ({ds.get('date', '?')}), Chapter "
                f"{ds.get('chapter', '?')} - {n_reg} registers, {n_ver} checked against "
                f"the document")
        if er:
            line += f" | Errata {er.get('doc')}"
        return line

    def load_config(self) -> dict:
        """Load configuration from JSON or create default.

        encoding="utf-8" is not optional here. save_config() writes UTF-8; without an
        explicit encoding this read takes the Windows default (cp1252), so the three
        bytes of "'" come back as three separate characters and the next save writes
        THOSE as UTF-8. The damage therefore compounds with every round trip -
        "Manufacturer's" turned into "Manufacturerâ€™s" and then into
        "ManufacturerÃ¢â‚¬â„¢s" - and nothing ever reports an error, because every
        intermediate file is valid JSON.
        """
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, 'r', encoding="utf-8") as f:
                return json.load(f)
        return DEFAULT_CONFIG.copy()

    def save_config(self):
        """Write configuration -- serialize first, then replace.

        open(..., 'w') empties the file when opened; if json.dump then fails,
        the register tab is gone. So convert to bytes first (an error then
        is raised before anything is touched), write to a neighboring file,
        and only at the end swap it in via os.replace.
        """
        data = json.dumps(self.config, indent=2, ensure_ascii=False).encode("utf-8")
        tmp = CONFIG_FILE.with_suffix(".json.tmp")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, CONFIG_FILE)

    def setup_ui(self):
        """Build the main UI"""
        # Top frame: COM port selection
        top_frame = ttk.Frame(self.root)
        top_frame.pack(fill=tk.X, padx=5, pady=5)

        ttk.Label(top_frame, text="COM Port:").pack(side=tk.LEFT)
        self.comport_var = tk.StringVar(value=self.config.get("comport", "COM8"))

        # Get available COM ports
        available_ports = get_available_com_ports()
        self.comport_combo = ttk.Combobox(
            top_frame,
            textvariable=self.comport_var,
            values=available_ports,
            width=10,
            state="readonly"
        )
        self.comport_combo.pack(side=tk.LEFT, padx=5)

        # Refresh COM ports button
        ttk.Button(top_frame, text="🔄 Refresh Ports", command=self.refresh_com_ports).pack(side=tk.LEFT, padx=2)

        ttk.Button(top_frame, text="Update COM Port", command=self.update_comport).pack(side=tk.LEFT, padx=2)

        # One toggle button instead of separate Connect/Disconnect buttons: the
        # connection state (the "Online"/"Offline" indicator further right, see
        # connection_frame below) sits at the far right of a toolbar that easily runs
        # wider than the window, so on a narrow window it's scrolled out of view and
        # there's no way to tell connected from not. A plain tk.Button (not ttk) so
        # its background color can be set directly regardless of the sv-ttk theme.
        self.connect_toggle_btn = tk.Button(top_frame, text="🔴 Connect", bg="#c0392b", fg="white",
                                             activebackground="#c0392b", activeforeground="white",
                                             command=self.toggle_connection)
        self.connect_toggle_btn.pack(side=tk.LEFT, padx=2)

        # Register bulk actions. They live up here rather than at the bottom of the
        # register tab because they are what one reaches for while watching the
        # registers, and the tab scrolls - a button below the scroll area is off screen
        # exactly when it is wanted.
        ttk.Separator(top_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(top_frame, text="🔄 Bulk Register Read All", command=self.bulk_read_registers).pack(side=tk.LEFT, padx=2)
        ttk.Button(top_frame, text="💾 Bulk Register Write All", command=self.bulk_write_registers).pack(side=tk.LEFT, padx=2)
        ttk.Button(top_frame, text="Save to JSON", command=self.save_registers_json).pack(side=tk.LEFT, padx=2)
        ttk.Button(top_frame, text="Open from JSON", command=self.load_registers_json).pack(side=tk.LEFT, padx=2)

        # Connection indicator
        self.connection_frame = ttk.Frame(top_frame)
        self.connection_frame.pack(side=tk.RIGHT, padx=10)

        self.connection_indicator = tk.Canvas(self.connection_frame, width=15, height=15, bg="white", highlightthickness=1)
        self.connection_indicator.pack(side=tk.LEFT)

        self.connection_label = ttk.Label(self.connection_frame, text="Offline", foreground="red")
        self.connection_label.pack(side=tk.LEFT, padx=5)

        # Status label
        self.status_label = ttk.Label(top_frame, text="Ready", foreground="blue")
        self.status_label.pack(side=tk.RIGHT, padx=5)

        # Notebook (tabs)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        # Binding BEFORE the add() calls: <<NotebookTabChanged>> is not fired
        # synchronously by Tk, but only processed on the next event loop pass (i.e.
        # at the earliest on the first mainloop()/update()). By that time
        # the tab construction has long finished, so the target attributes exist -- the handler
        # is thus the single source of truth, no race with an explicit
        # assignment at the end, which this deferred event would otherwise silently
        # overwrite.
        self.notebook.bind("<<NotebookTabChanged>>", self._on_main_tab_changed)

        self.create_bridge_tab()
        self.create_registers_tab()
        self.create_testmodes_tab()
        self.create_terminal_tab()
        self.create_about_tab()

        # Immediate starting value in case no <<NotebookTabChanged>>
        # is ever delivered for some reason -- "Bridge Parameters" is the initially visible tab.
        self._active_scroll_canvas = self._bridge_scroll_canvas

    # Columns per quick-command group. Fixed rather than computed from the pane width:
    # the pane is resizable (it's one side of a PanedWindow), so a width-driven column
    # count would reflow every drag - a stable grid is easier to scan as more commands
    # are added.
    QUICK_COMMAND_COLUMNS = 3

    def _build_quick_command_groups(self, parent, groups: List[tuple]) -> None:
        """Lay out Quick Commands as one grid of buttons per group.

        `groups` is [(title, [(label, command), ...]), ...]. Every button in every
        group shares one width, computed from the longest label across ALL groups -
        so the panel reads as one aligned grid instead of each LabelFrame picking its
        own width, and a longer label added later (more quick commands are coming)
        widens the whole panel instead of getting clipped. Replaces the old fixed
        width=15 (sized for "Write Environment", wasteful for "Save to JSON") and the old
        single-column pack() layout, which is what ate the vertical space this was
        meant to free up.
        """
        button_width = max(len(label) for _, buttons in groups for label, _ in buttons) + 2
        for title, buttons in groups:
            grp = ttk.LabelFrame(parent, text=title, padding=5)
            grp.pack(fill=tk.X, padx=5, pady=5)
            for col in range(self.QUICK_COMMAND_COLUMNS):
                grp.columnconfigure(col, weight=1)
            for i, (label, command) in enumerate(buttons):
                row, col = divmod(i, self.QUICK_COMMAND_COLUMNS)
                ttk.Button(grp, text=label, command=command, width=button_width).grid(
                    row=row, column=col, sticky="ew", padx=2, pady=2)

    def create_bridge_tab(self):
        """Create Bridge Parameters tab"""
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Bridge Parameters")

        # Fields dictionary
        self.bridge_fields: Dict[str, tk.StringVar] = {}

        # Environment identity right at the top, BEFORE the paned area - that fills
        # the rest with expand=True, a label packed later would land below it. It
        # decides whether the values below mean what the label says.
        self.env_identity_var = tk.StringVar(value=self.env_identity_line())
        self._env_identity_widget = ttk.Label(frame, textvariable=self.env_identity_var,
                                              foreground="#555", wraplength=1150,
                                              justify=tk.LEFT)
        self._env_identity_widget.pack(anchor="w", padx=8, pady=(4, 2))

        # Main paned window: parameters on left, commands/output on right
        paned = ttk.PanedWindow(frame, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # LEFT SIDE: Bridge Parameters
        left_frame = ttk.LabelFrame(paned, text="Configuration Parameters", padding=5)
        paned.add(left_frame, weight=1)

        # Scrollable frame for parameters
        canvas = tk.Canvas(left_frame)
        scrollbar = ttk.Scrollbar(left_frame, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        # The fields come from env_model.json, not from a list in the source code: which
        # ones exist depends on the device's identity and version, and that is exactly the point.
        saved = self.config.get("bridge", {})
        # The normal case: the command that makes the value permanent. Fields that behave
        # exactly like this need no hint - only the outliers get one.
        default_applies = self.env_entry().get("commands", {}).get("persist", "saveenv")
        for key, fld in self.env_entry().get("fields", {}).items():
            self.bridge_fields[key] = tk.StringVar(value=str(saved.get(key, "")))
            row = ttk.Frame(scrollable_frame)
            row.pack(fill=tk.X, padx=5, pady=(4, 0))

            # No read/write per field: the environment is read as a whole ("Read
            # Environment", a showenv) and written as a whole ("Write Environment", a saveenv).
            # Writing a single field in isolation makes no sense here, unlike the
            # register table next door - there every register is its own
            # access, here it is one shared record.
            ttk.Label(row, text=fld.get("label", key) + ":", width=22).pack(side=tk.LEFT)
            ttk.Entry(row, textvariable=self.bridge_fields[key], width=30).pack(side=tk.LEFT, padx=5)

            # When the value takes effect -- but ONLY if it deviates from the normal case. For eleven
            # of the thirteen fields that is saveenv, and a line "mask0 -> saveenv" under
            # "mask eth0:" would just repeat the label. What remains are the two
            # cases that are genuinely surprising: the MAC only takes effect after a reset, the
            # mirror only on the next boot. Those are shown in the line, and in red.
            applies = fld.get("applies", "")
            if applies and applies != default_applies:
                ttk.Label(row, text=f"  {applies}", font=("Courier", 8),
                          foreground="#b00").pack(side=tk.LEFT)

        # Mouse wheel: just register this canvas in the central list. The actual
        # delivery is handled by ONE global handler (see _register_wheel_canvas) - see
        # there for why neither Enter/Leave on the canvas nor a direct binding on the
        # child widgets was reliable.
        self._register_wheel_canvas(canvas)
        self._bridge_scroll_canvas = canvas  # for the page up/down fallback

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # RIGHT SIDE: Quick Commands
        right_frame = ttk.LabelFrame(paned, text="Quick Commands", padding=5)
        paned.add(right_frame, weight=1)

        # Data, not one pack() call per button: a group is just its title and its
        # (label, command) pairs, so a new quick command later is one appended tuple,
        # not a copy-pasted Button line. "Write Environment" (not "Write All"): it does
        # not just set the fields (setenv only touches the RAM copy), saveenv also puts
        # them in the EEPROM - the name says something durable landed in the device.
        quick_command_groups = [
            ("Environment", [
                ("Read Environment", self.read_all_bridge),
                ("Write Environment", self.write_environment),
                ("Save to JSON", self.save_bridge_json),
                ("Open from JSON", self.load_bridge_json),
            ]),
            ("Device", [
                ("Mirror: Enable", lambda: self.run_async_cmd("mirror 1")),
                ("Mirror: Disable", lambda: self.run_async_cmd("mirror 0")),
                ("Sniffer: Enable", lambda: self.run_async_cmd("sniffer 1")),
                ("Sniffer: Disable", lambda: self.run_async_cmd("sniffer 0")),
                ("Read Stats", lambda: self.run_async_cmd("stats")),
                ("Memory Info", lambda: self.run_async_cmd("meminfo")),
                ("Build Timestamp", lambda: self.run_async_cmd("timestamp")),
                ("Reset Device", self.reset_device),
                ("Flash", self.flash_current_hex),
                ("Select Hex...", self.flash_select_hex),
                ("Erase chip...", self.erase_chip),
            ]),
        ]
        self._build_quick_command_groups(right_frame, quick_command_groups)

        # Output frame
        output_frame = ttk.LabelFrame(right_frame, text="Command Output", padding=5)
        output_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Output controls
        output_ctrl_frame = ttk.Frame(output_frame)
        output_ctrl_frame.pack(fill=tk.X, pady=2)
        ttk.Button(output_ctrl_frame, text="Clear", command=self.clear_bridge_output, width=10).pack(side=tk.LEFT)

        self.bridge_output = tk.Text(output_frame, height=20, width=40, state=tk.DISABLED, wrap=tk.WORD)
        self.bridge_output.pack(fill=tk.BOTH, expand=True)

        scrollbar_out = ttk.Scrollbar(self.bridge_output)
        scrollbar_out.pack(side=tk.RIGHT, fill=tk.Y)
        self.bridge_output.config(yscrollcommand=scrollbar_out.set)

    def create_registers_tab(self):
        """Create LAN8651 Registers tab with categories"""
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="LAN8651 Registers")

        self.register_fields: Dict[str, tk.StringVar] = {}
        self.register_categories: Dict[str, List[str]] = {}
        # Keep the register map (name/description/bitfields per address) in memory.
        # Without it, save_registers_json only wrote back {address: value} and
        # destroyed the map generated from the datasheet on the first save.
        self.register_meta: Dict[str, dict] = {}

        # Provenance line: which document, which revision, how much of it was checked.
        # Deliberately shown at the top, not in a help text -- whoever reads registers should
        # see what they are currently relying on.
        ttk.Label(frame, text=self.model_source_line(), foreground="#555").pack(
            anchor="w", padx=8, pady=(4, 0))

        # Create sub-notebook for register categories
        reg_notebook = ttk.Notebook(frame)
        reg_notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self._reg_notebook = reg_notebook            # for the page up/down fallback
        self._reg_tab_canvases: Dict[str, tk.Canvas] = {}  # tab name (widget path) -> canvas
        reg_notebook.bind("<<NotebookTabChanged>>", self._on_reg_tab_changed)

        # The map comes from the model, the last-read values come from the config.
        registers = {name: g.get("registers", {})
                     for name, g in self.model.get("groups", {}).items()}
        saved_values = self.config.get("values", {})

        # Create a tab for each category
        for category, regs in registers.items():
            self.register_categories[category] = list(regs.keys())
            category_frame = ttk.Frame(reg_notebook)
            reg_notebook.add(category_frame, text=category)

            # Scrollable area. The bindings MUST capture this iteration's
            # canvas (default argument): a `lambda e: canvas...`
            # accesses the loop variable, and after the last
            # iteration that points to the same, last-created canvas for ALL tabs --
            # then only the last tab gets a valid scrollregion and all
            # the others can't be scrolled past the visible height.
            canvas = tk.Canvas(category_frame, highlightthickness=0)
            scrollbar = ttk.Scrollbar(category_frame, orient="vertical", command=canvas.yview)
            scrollable_frame = ttk.Frame(canvas)
            inner_id = canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
            canvas.configure(yscrollcommand=scrollbar.set)

            def _on_content(event, cv=canvas):
                cv.configure(scrollregion=cv.bbox("all"))

            def _on_canvas(event, cv=canvas, wid=inner_id):
                # Stretch the content to the full canvas width, otherwise everything sticks to the left
                cv.itemconfigure(wid, width=event.width)
                cv.configure(scrollregion=cv.bbox("all"))

            scrollable_frame.bind("<Configure>", _on_content)
            canvas.bind("<Configure>", _on_canvas)
            # Mouse wheel: just register this canvas in the central list, see
            # _register_wheel_canvas for the actual (global) handler.
            self._register_wheel_canvas(canvas)
            self._reg_tab_canvases[str(category_frame)] = canvas

            # Create fields for each register in this category
            for addr, info in regs.items():
                self.register_fields[addr] = tk.StringVar(value=saved_values.get(addr, ""))

                # Model format: mnemonic + name, bits as objects with name/description.
                reg_name = info.get("mnemonic", "")
                reg_desc = info.get("name", "")
                bits = info.get("bits", {})
                bitfields = {spec: f"{f.get('name', '')} - {f.get('description', '')}".strip(" -")
                             for spec, f in bits.items()}
                errata = info.get("errata", [])

                self.register_meta[addr] = {
                    "category": category,
                    "name": reg_name,
                    "description": reg_desc,
                    "bitfields": bitfields,
                    "errata": errata,
                }

                # Main row (address, name, value, buttons)
                row = ttk.Frame(scrollable_frame)
                row.pack(fill=tk.X, padx=5, pady=1)

                ttk.Label(row, text=f"{addr}", width=14, font=("Courier", 9)).pack(side=tk.LEFT)
                name_text = f"{reg_name}" if reg_name else reg_desc[:20]
                ttk.Label(row, text=name_text, width=30).pack(side=tk.LEFT)

                # Registers with an errata entry are flagged. Without the flag, a
                # register whose value, per the errata, doesn't mean what it says would look
                # exactly like any other -- that is the misleading situation this addresses.
                if errata:
                    items = ", ".join(e.get("item", "?") for e in errata)
                    ttk.Label(row, text=f"⚠ {items}", foreground="#b00",
                              font=("Courier", 8)).pack(side=tk.LEFT, padx=(0, 4))

                value_var = self.register_fields[addr]
                value_entry = ttk.Entry(row, textvariable=value_var, width=14, font=("Courier", 9))
                value_entry.pack(side=tk.LEFT, padx=5)

                ttk.Button(row, text="Read", width=6, command=lambda a=addr: self.read_register(a)).pack(side=tk.LEFT, padx=2)
                ttk.Button(row, text="Write", width=6, command=lambda a=addr: self.write_register(a)).pack(side=tk.LEFT, padx=2)

                # Bitfields row (if available)
                if bitfields:
                    # Separator row
                    sep_row = ttk.Frame(scrollable_frame)
                    sep_row.pack(fill=tk.X, padx=20, pady=0)

                    # Description + Bitfields
                    desc_text = ttk.Label(sep_row, text=f"📋 {reg_desc}", font=("Courier", 8), foreground="#666")
                    desc_text.pack(anchor=tk.W)

                    for e in errata:
                        ttk.Label(sep_row,
                                  text=f"   ⚠ {e.get('doc', '')} {e.get('item', '')}: "
                                       f"{e.get('summary', '')}",
                                  font=("Courier", 8), foreground="#b00",
                                  wraplength=900, justify=tk.LEFT).pack(anchor=tk.W)
                        if e.get("implication"):
                            ttk.Label(sep_row, text=f"      -> {e['implication']}",
                                      font=("Courier", 8), foreground="#b00",
                                      wraplength=900, justify=tk.LEFT).pack(anchor=tk.W)

                    # Bitfield definitions. The read-out value sits at the end of the SAME
                    # line instead of in a combined line below: that saves one line per register
                    # and avoids jumping back and forth between field name and value.
                    # Two labels side by side, because a ttk.Label can only have one color.
                    field_vars: Dict[str, tk.StringVar] = {}
                    for bits, meaning in bitfields.items():
                        bf_row = ttk.Frame(sep_row)
                        bf_row.pack(anchor=tk.W, fill=tk.X)
                        ttk.Label(bf_row, text=f"   [{bits}] {meaning}",
                                  font=("Courier", 8), foreground="#444").pack(side=tk.LEFT)
                        fv = tk.StringVar()
                        field_vars[bits] = fv
                        ttk.Label(bf_row, textvariable=fv, font=("Courier", 8),
                                  foreground="#009900").pack(side=tk.LEFT)

                    # One callback per register updates all its fields. The
                    # default arguments are mandatory: without them, all callbacks would point
                    # after the loop to the last-created variables.
                    def make_update_decoded(val_var=value_var, fvars=field_vars):
                        def update_decoded(*args):
                            hex_val = val_var.get()
                            for spec, var in fvars.items():
                                var.set(self.decode_one_bitfield(hex_val, spec))
                        return update_decoded

                    callback = make_update_decoded()
                    value_var.trace_add("write", callback)
                    callback()   # show saved values right away while building

            canvas.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")

        # The bulk buttons that used to sit here are in the top bar now (create_widgets),
        # where they stay visible no matter which tab is open or how far it is scrolled.

    # (mode, title, description) - description is the "explained in detail" text shown
    # in that mode's own group. Kept here as data, not spread across widget calls, so a
    # fifth mode is one more tuple. Longer background (setup notes, safety) stays in
    # IEEE 802.3-2022 section 147.5.2; this is the summary worth having next to the button.
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

    def create_testmodes_tab(self):
        """Create Test Modes tab: one group per mode, each with its own start button and
        auto-revert field - an empty field means that mode runs until something else
        changes it, not just until the next read.
        """
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Test Modes")

        # Scrollable like the other content-heavy tabs (Bridge Parameters, Registers) -
        # five groups with a description don't fit on every screen. See
        # _register_wheel_canvas for the global mouse wheel handler and
        # _bind_page_scroll_keys for the page up/down fallback (touchpad).
        canvas = tk.Canvas(frame)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        self._register_wheel_canvas(canvas)
        self._testmodes_scroll_canvas = canvas

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Two columns instead of one long chain of five groups - Mode 0 spans the full
        # width, Mode 1-4 in a 2x2 grid below it. Uses the width a maximized
        # window actually has instead of leaving it unused, and thereby needs only
        # three rows instead of five: fits on any reasonably sized screen without
        # scrolling. All children of scrollable_frame must therefore consistently use grid() instead of
        # pack() - Tk does not allow mixing both in the same container.
        scrollable_frame.columnconfigure(0, weight=1, uniform="testmode_col")
        scrollable_frame.columnconfigure(1, weight=1, uniform="testmode_col")

        ttk.Label(
            scrollable_frame,
            text="⚠️  Test modes disconnect the T1S link - the bridge is unreachable while one is active.\n"
                 "Register: T1STSTCTL (0x000308FB), bits 15:13. IEEE 802.3-2022 section 147.5.2 transmitter test modes.",
            justify=tk.LEFT, foreground="red"
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=8, pady=8)

        # Mode 0: always reachable first, regardless of which test mode is currently
        # running - no auto-revert field, "normal operation" has no duration.
        normal_frame = ttk.LabelFrame(scrollable_frame, text="Mode 0 - Normal Operation", padding=10)
        normal_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=5)
        ttk.Label(normal_frame, text="Ends any active test mode and restores normal T1S operation.",
                  justify=tk.LEFT).pack(anchor=tk.W)
        row0 = ttk.Frame(normal_frame)
        row0.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(row0, text="Return to Normal Mode",
                   command=lambda: self.apply_testmode(0), width=22).pack(side=tk.LEFT, padx=2)
        ttk.Button(row0, text="Read Current Mode",
                   command=self.read_testmode, width=22).pack(side=tk.LEFT, padx=2)

        # A separate StringVar per test mode for the auto-revert field, so apply_testmode
        # knows which field belongs to which "Start" button.
        self.testmode_timeout_vars: Dict[int, tk.StringVar] = {}
        for i, (mode, title, description) in enumerate(self.TEST_MODES):
            grp_row, grp_col = divmod(i, 2)
            grp = ttk.LabelFrame(scrollable_frame, text=f"Mode {mode} - {title}", padding=10)
            grp.grid(row=2 + grp_row, column=grp_col, sticky="nsew", padx=8, pady=5)

            # wraplength keeps the description readable in the now half-as-wide column,
            # instead of stretching the column to the length of the longest line.
            ttk.Label(grp, text=description, justify=tk.LEFT, wraplength=600).pack(anchor=tk.W)

            ctrl = ttk.Frame(grp)
            ctrl.pack(fill=tk.X, pady=(8, 0))
            ttk.Label(ctrl, text="Auto-revert (sec, empty = runs until changed):").pack(side=tk.LEFT, padx=(0, 5))
            timeout_var = tk.StringVar(value="")
            self.testmode_timeout_vars[mode] = timeout_var
            ttk.Entry(ctrl, textvariable=timeout_var, width=8).pack(side=tk.LEFT, padx=5)
            ttk.Button(ctrl, text=f"Start Test Mode {mode}",
                       command=lambda m=mode: self.apply_testmode(m), width=20).pack(side=tk.LEFT, padx=10)

    def create_terminal_tab(self):
        """Create Serial Terminal tab with gui_term.py logic"""
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Terminal")

        self.terminal_q = queue.Queue()
        self.terminal_link: Optional[Link] = None
        self.terminal_screen = Screen()
        self._terminal_seen_total = 0
        self._terminal_widget_lines = 0
        self._terminal_cursor_on = True

        # Clear button frame
        ctrl_frame = ttk.Frame(frame)
        ctrl_frame.pack(side=tk.TOP, fill=tk.X, padx=4, pady=4)

        ttk.Button(ctrl_frame, text="Clear all", width=14, command=self.terminal_clear_all).pack(side=tk.LEFT, padx=2)

        # Terminal display
        self.terminal_text = scrolledtext.ScrolledText(
            frame,
            wrap="char",
            width=100,
            height=30,
            font=("Consolas", 10),
            background="#101010",
            foreground="#d8d8d8",
            insertwidth=0
        )
        self.terminal_text.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.terminal_text.tag_config("cursor", background="#d8d8d8", foreground="#101010")

        self.terminal_text.bind("<Key>", self.terminal_on_key)
        self.terminal_text.bind("<Control-c>", self.terminal_on_ctrl_c)
        self.terminal_text.bind("<Control-v>", self.terminal_on_paste)
        self.terminal_text.bind("<<Paste>>", self.terminal_on_paste)
        self.terminal_text.bind("<Button-3>", self.terminal_on_paste)
        self.terminal_text.bind("<Button-1>", lambda _e: self.terminal_text.focus_set())
        self.terminal_text.bind("<Prior>", lambda e: (self.terminal_text.yview_scroll(-1, "pages"), "break"))
        self.terminal_text.bind("<Next>", lambda e: (self.terminal_text.yview_scroll(1, "pages"), "break"))

        # Set focus to terminal
        self.terminal_text.focus_set()


    def terminal_disconnect(self):
        """Disconnect from terminal"""
        if self.terminal_link:
            self.terminal_link.close()
            self.terminal_link = None
            self.terminal_note("getrennt")

    def terminal_clear_all(self):
        """Clear terminal display"""
        self.terminal_screen = Screen()
        self._terminal_seen_total = 0
        self._terminal_widget_lines = 0
        self.terminal_text.delete(1.0, tk.END)

    def terminal_on_key(self, event):
        """Handle key input"""
        if event.keysym in ("Prior", "Next"):
            return  # Handled by bind
        if event.keysym in IGNORED_KEYSYMS:
            return "break"

        data = KEYSYM_BYTES.get(event.keysym)
        if data is None:
            if event.char:
                data = event.char.encode("latin-1", "ignore")
            elif (event.state & CONTROL_BIT) and len(event.keysym) == 1 and event.keysym.isalpha():
                data = bytes([ord(event.keysym.lower()) & 0x1F])

        if data:
            if self.port_link:
                try:
                    self.port_link.write(data)
                except OSError as e:
                    self.terminal_note(f"not connected: {e}")
            else:
                print("DEBUG: No port_link")
                self.terminal_note("not connected")

        return "break"

    def terminal_on_ctrl_c(self, _event=None):
        """Ctrl+C: copy or send interrupt"""
        try:
            selected = self.terminal_text.get("sel.first", "sel.last")
            if selected:
                self.terminal_text.clipboard_clear()
                self.terminal_text.clipboard_append(selected)
                return "break"
        except tk.TclError:
            pass

        if self.port_link:
            try:
                self.port_link.write(b"\x03")
            except OSError:
                self.terminal_note("not connected")
        return "break"

    def terminal_on_paste(self, _event=None):
        """Paste from clipboard"""
        if not self.port_link:
            self.terminal_note("not connected")
            return "break"
        try:
            text = self.root.clipboard_get()
            lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            if lines and lines[-1] == "":
                lines.pop()
            if lines:
                self._terminal_paste_next(lines)
        except Exception:
            pass
        return "break"

    def _terminal_paste_next(self, lines):
        """Paste lines one by one with delay"""
        if not lines or not self.port_link:
            return
        try:
            self.port_link.write(lines[0].encode("latin-1", "ignore") + b"\r\n")
        except OSError:
            return
        if len(lines) > 1:
            self.root.after(120, self._terminal_paste_next, lines[1:])

    def terminal_note(self, line):
        """Add note to terminal"""
        data = f"\r\n[{line}]\r\n".encode("latin-1", "replace")
        self.terminal_screen.feed(data)
        self._terminal_render()

    def terminal_feed(self, data):
        """Feed bytes to terminal"""
        if not data:
            return
        self.terminal_screen.feed(data)
        self._terminal_render()

    def _terminal_render(self):
        """Render screen to text widget"""
        added = self.terminal_screen.total - self._terminal_seen_total
        new_lines = []
        if added > 0:
            new_lines = (self.terminal_screen.lines[-added:]
                         if added <= len(self.terminal_screen.lines)
                         else list(self.terminal_screen.lines))

        first = self._terminal_widget_lines + 1
        at_bottom = self.terminal_text.yview()[1] > 0.999

        # Delete old current line and insert new content
        self.terminal_text.delete(f"{first}.0", f"{first}.end")
        self.terminal_text.insert(f"{first}.0",
                                 "".join(line + "\n" for line in new_lines)
                                 + self.terminal_screen.cur + " ")
        self._terminal_widget_lines += len(new_lines)
        self._terminal_seen_total = self.terminal_screen.total

        if self._terminal_widget_lines > MAX_VIEW_LINES:
            drop = self._terminal_widget_lines - MAX_VIEW_LINES
            self.terminal_text.delete("1.0", f"{drop + 1}.0")
            self._terminal_widget_lines -= drop

        self._terminal_place_cursor()
        if at_bottom:
            self.terminal_text.see(tk.END)

    def _terminal_place_cursor(self):
        """Place/render cursor"""
        self.terminal_text.tag_remove("cursor", "1.0", tk.END)
        if not self._terminal_cursor_on:
            return
        row = self._terminal_widget_lines + 1
        col = self.terminal_screen.col
        self.terminal_text.tag_add("cursor", f"{row}.{col}", f"{row}.{col + 1}")

    def terminal_blink(self):
        """Blink cursor"""
        self._terminal_cursor_on = not self._terminal_cursor_on
        self._terminal_place_cursor()

    def terminal_process_queue(self):
        """Process messages from terminal queue"""
        try:
            while True:
                port, kind, payload = self.terminal_q.get_nowait()
                if kind == "data":
                    self.terminal_feed(payload)
                elif kind == "lost":
                    self.terminal_note(f"Connection lost: {payload}")
                    self.port_link = None
                    self.disconnect_device()
        except queue.Empty:
            pass

    def create_about_tab(self):
        """Create About/Help tab"""
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Help")

        text = tk.Text(frame, wrap=tk.WORD, padx=10, pady=10)
        text.pack(fill=tk.BOTH, expand=True)

        help_text = """Bridge Status & Configuration GUI
Version 1.0

This GUI provides an interface to the T1S/100BASE-TX Bridge firmware.

TABS:
- Bridge Parameters: Read/write bridge configuration (IP, MAC, PLCA)
- LAN8651 Registers: Direct register access with individual read/write
- Test Modes: Apply IEEE test modes and run diagnostic scripts
- Help: This page

FEATURES:
- Individual Read/Write: Each parameter/register has its own Read/Write button
- Bulk Operations: Read/Write all bridge parameters or registers at once
- JSON Persistence: Save configuration to file and reload it
- Threading: Long-running operations don't freeze the GUI
- Status Updates: Real-time feedback on each operation

WORKFLOW:
1. Select the correct COM port (e.g., COM8)
2. Use Read Environment to fetch current state from bridge
3. Edit values as needed
4. Use Write All to apply changes to bridge
5. Use Save to JSON to persist configuration

REGISTER ACCESS:
- Addresses use MMS encoding (upper 16 bits = bank, lower 16 bits = offset)
- Examples: 0x000308FB = Test Mode, 0x0004CA02 = PLCA_CTRL1
- Always read back after write to verify

TEST MODES:
- Modes 1-4 disconnect the link during the test
- Use testmode command for automatic readback and verification
- See docs/LAN8651_TEST_MODES.md for measurement setup

CLI COMMAND:
python cli.py --port COM8 --read 1 "<command>"

Example commands:
  stats              - Show traffic statistics
  lan_read 0x0004CA02      - Read PLCA_CTRL1
  lan_write 0x0004CA02 0x80    - Write PLCA_CTRL1
  testmode 1         - Apply test mode 1
  mirror 1           - Enable port mirror (this bridge's own T1S traffic only)
  sniffer 1          - Enable sniffer (ALL T1S traffic, incl. other nodes)
"""

        text.insert(1.0, help_text)
        text.config(state=tk.DISABLED)

    def refresh_com_ports(self):
        """Refresh the list of available COM ports"""
        available_ports = get_available_com_ports()

        if not available_ports:
            messagebox.showwarning("No COM Ports", "No COM ports detected. Check device connection.")
            self.comport_combo['values'] = []
            self.set_error_status("No COM ports available")
            return

        self.comport_combo['values'] = available_ports

        # If current selection is not in list, pick first available
        if self.comport_var.get() not in available_ports:
            self.comport_var.set(available_ports[0])

        port_list = ", ".join(available_ports)
        self.set_status(f"Found {len(available_ports)} port(s): {port_list}")

    def update_comport(self):
        """Update COM port in config"""
        port = self.comport_var.get()
        if not port:
            messagebox.showwarning("Warning", "Please select a COM port")
            return

        self.config["comport"] = port
        self.save_config()
        self.set_status(f"COM port set to {port}")

    def set_status(self, message: str, duration: int = 3000):
        """Update status label with message"""
        self.status_label.config(text=message, foreground="green")
        if duration > 0:
            self.root.after(duration, lambda: self.status_label.config(text="Ready", foreground="blue"))

    def set_error_status(self, message: str):
        """Update status label with error"""
        self.status_label.config(text=message, foreground="red")

    def clear_bridge_output(self):
        """Clear the bridge command output text widget"""
        self.bridge_output.config(state=tk.NORMAL)
        self.bridge_output.delete(1.0, tk.END)
        self.bridge_output.config(state=tk.DISABLED)

    def update_connection_indicator(self):
        """Update the connection indicator circle/label, and the Connect/Disconnect toggle button."""
        if self.connected:
            self.connection_indicator.delete("all")
            self.connection_indicator.create_oval(2, 2, 13, 13, fill="green", outline="darkgreen")
            self.connection_label.config(text="Online", foreground="green")
            self.connect_toggle_btn.config(text="🟢 Disconnect", bg="#27ae60",
                                            activebackground="#27ae60")
        else:
            self.connection_indicator.delete("all")
            self.connection_indicator.create_oval(2, 2, 13, 13, fill="red", outline="darkred")
            self.connection_label.config(text="Offline", foreground="red")
            self.connect_toggle_btn.config(text="🔴 Connect", bg="#c0392b",
                                            activebackground="#c0392b")

    def toggle_connection(self):
        """The single Connect/Disconnect button: act on whichever is currently true."""
        if self.port_link:
            self.disconnect_device()
        else:
            self.connect_device()

    def connect_device(self):
        """Open COM port (shared by CLI + Terminal)"""
        port = self.comport_var.get()
        if not port:
            messagebox.showwarning("Warning", "Please select a COM port first")
            return

        if self.port_link:
            messagebox.showinfo("Info", "Already connected")
            return

        def worker():
            link = Link(port, self.result_queue)
            try:
                link.open()
                self.result_queue.put(("port_opened", link))
            except Exception as e:
                self.result_queue.put(("port_failed", str(e)))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        self.set_status("Connecting...")

    def disconnect_device(self):
        """Close COM port (for CLI + Terminal)"""
        if self.port_link:
            self.port_link.close()
            self.port_link = None
            # Terminal note
            if hasattr(self, 'terminal_note'):
                self.terminal_note("getrennt")

        self.connected = False
        self.update_connection_indicator()
        self.set_status("Disconnected")

    def run_async_cmd(self, command: str, timeout_ms: int = 1500):
        """Send a command over the open link, response goes into Command Output."""
        if not self.port_link:
            self.set_error_status("Not connected")
            messagebox.showwarning("Not connected",
                                   "Press Connect first, then send the command.")
            return

        def worker():
            output = self.send_command_via_link(command, timeout_ms=timeout_ms)
            text = self.clean_response(command, output)
            self.result_queue.put(("cmd_result", bool(text), text or "no response"))

        threading.Thread(target=worker, daemon=True).start()
        self.set_status(f"Running: {command}")

    def reset_device(self):
        """Reset the MCU via the Harmony command processor's built-in 'reset' command.

        Confirmed first, unlike the other Device buttons (mirror/stats/meminfo/timestamp):
        those just read or flip a runtime flag, this one reboots the board and drops
        whatever else was in progress on the link.
        """
        if not messagebox.askyesno("Confirm", "Reset the device now? The board will reboot."):
            return
        self.run_async_cmd("reset")

    def flash_current_hex(self):
        """Flash self._selected_hex_path via pyOCD - onto a probe YOU pick. Defaults to
        RELEASE_HEX (release\\bridge_lan865x_100baseT.hex) until "Select Hex..." picks a
        different file; that choice then sticks for every later "Flash" click, this
        session, until "Select Hex..." is used again.

        Goes through flash_same54.py directly, not through install.bat's saved probe
        selection: with more than one EDBG probe on the desk, silently flashing
        whichever one bench.json happens to name is exactly the mistake this dialog
        exists to prevent. Asks pyOCD which probes are actually connected right now.

        Independent of the open serial link: pyOCD talks to the EDBG probe's SWD
        interface, not the COM port, so nothing needs to disconnect first.
        """
        if not self._selected_hex_path.is_file():
            messagebox.showerror("Not found", f"{self._selected_hex_path} is missing.")
            return
        self._open_probe_picker("flash", hex_path=self._selected_hex_path)

    def flash_select_hex(self):
        """Flash a hex file YOU pick, not necessarily RELEASE_HEX -- e.g. a fresh local
        build (dist/default/production/...hex), one from another branch, or one
        someone else sent you. Starts browsing wherever the current selection sits
        (release\\ by default). The
        choice also becomes the new default for "Flash", so picking once covers
        every later flash until this is used again."""
        initial_dir = (self._selected_hex_path.parent
                        if self._selected_hex_path.parent.is_dir() else Path(__file__).parent)
        chosen = filedialog.askopenfilename(
            parent=self.root, title="Select hex file to flash",
            initialdir=str(initial_dir), filetypes=[("Hex files", "*.hex"), ("All files", "*.*")])
        if not chosen:
            return
        self._selected_hex_path = Path(chosen)
        self._open_probe_picker("flash", hex_path=self._selected_hex_path)

    def erase_chip(self):
        """Chip-erase a probe YOU pick - firmware AND the emulated EEPROM.

        A plain flash only programs the regions the hex file covers and leaves the
        emulated EEPROM (PLCA id/count, IP, MAC settings) untouched, by design. This
        is the way to reach a true blank state for one probe, picked here rather than
        assumed from bench.json - same reasoning as flash_current_hex() above.
        """
        self._open_probe_picker("erase")

    def _open_probe_picker(self, mode: str, hex_path: Optional[Path] = None) -> None:
        if not FLASH_SAME54_SCRIPT.is_file():
            messagebox.showerror("Not found", f"{FLASH_SAME54_SCRIPT} is missing.")
            return
        probes = self._list_probes()
        if not probes:
            messagebox.showerror(
                "pyOCD",
                "No connected probes found (check the USB connection, or pip install pyocd).")
            return
        self._show_probe_picker(probes, mode, hex_path)

    def _list_probes(self) -> List[tuple]:
        """Probes per pyOCD, via 'flash_same54.py --list' rather than importing pyocd
        directly here - pyocd stays a dependency of the flash tool, not this GUI (see
        the module docstring: "Standalone apart from pyserial"). Output is
        one line per probe: '<unique_id>  <vendor> <product>' (flash_same54.py's own
        list_probes())."""
        try:
            proc = subprocess.run(
                [PYOCD_PYTHON, str(FLASH_SAME54_SCRIPT), "--list"],
                capture_output=True, text=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as exc:
            messagebox.showerror("pyOCD", f"Could not list probes: {exc}")
            return []
        port_by_serial = self._com_ports_by_probe_serial()
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

    def _com_ports_by_probe_serial(self) -> Dict[str, str]:
        """Map an EDBG probe's serial (pyOCD's unique_id) to its own COM port, so the
        probe picker can show which port the CLI/terminal would use for that same
        board - not just an opaque serial number.

        An EDBG probe exposes its debug (CMSIS-DAP) and virtual-COM (CDC) function as
        separate USB interfaces of the SAME composite device, sharing one USB serial
        descriptor - verified on this bench (2026-08-29): pyserial's serial_number and
        pyOCD's unique_id came back byte-identical for all three connected probes.
        """
        if serial is None:
            return {}
        from serial.tools import list_ports
        return {p.serial_number: p.device for p in list_ports.comports() if p.serial_number}

    def _show_probe_picker(self, probes: List[tuple], mode: str,
                            hex_path: Optional[Path] = None) -> None:
        """Modal dialog: pick ONE of the probes found, then go straight on - a second,
        generic confirmation dialog afterward would just repeat what already stands
        here in red. Erase still gets the typed-word prompt on top of this, the same
        as ERASE_CONFIRM_WORD elsewhere - a click in a list is not a substitute for it.
        """
        if mode == "erase":
            title = "Select probe to erase"
            heading = "Chip-erase (firmware AND emulated EEPROM) via pyOCD on:"
            warning = ("This erases EVERYTHING on the selected board: firmware and the "
                       "emulated EEPROM (PLCA id/count, IP, MAC settings). "
                       "It will need reflashing afterward.")
            action_label = "Erase..."
        else:
            title = "Select probe to flash"
            heading = f"Flash {hex_path.name} onto:"
            warning = "This erases and reprograms the selected board, then resets it."
            action_label = "Flash"

        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(dialog, text=heading, padding=(10, 10, 10, 4)).pack(anchor=tk.W)

        listbox = tk.Listbox(dialog, width=70, height=min(8, len(probes)))
        for unique_id, desc in probes:
            listbox.insert(tk.END, f"{unique_id}   {desc}")
        listbox.selection_set(0)
        listbox.pack(padx=10, pady=(0, 5), fill=tk.BOTH, expand=True)

        ttk.Label(dialog, text=warning, foreground="#b00", wraplength=460,
                  justify=tk.LEFT).pack(anchor=tk.W, padx=10)

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=10)

        def do_action(event=None):
            sel = listbox.curselection()
            if not sel:
                return
            unique_id, desc = probes[sel[0]]
            dialog.destroy()
            if mode == "erase":
                self._erase_probe(unique_id, desc)
            else:
                self._flash_probe(unique_id, desc, hex_path)

        ttk.Button(btn_frame, text=action_label, command=do_action).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=dialog.destroy).pack(side=tk.LEFT, padx=5)
        dialog.bind("<Return>", do_action)
        dialog.bind("<Escape>", lambda e: dialog.destroy())
        listbox.focus_set()

    def _flash_probe(self, unique_id: str, description: str, hex_path: Path) -> None:
        """Start the actual flash, for ONE explicitly chosen probe and hex file."""
        self.set_status(f"Flashing probe {unique_id} ...")
        self._run_pyocd_op("Flash", [str(hex_path), "--probe", unique_id], description)

    def _erase_probe(self, unique_id: str, description: str) -> None:
        """Chip-erase ONE explicitly chosen probe - gated on the typed confirmation
        word, not just the click in the probe list (see _show_probe_picker())."""
        typed = simpledialog.askstring(
            "Confirm erase",
            f"This PERMANENTLY erases probe {unique_id} ({description}):\n"
            "firmware AND the emulated EEPROM.\n\n"
            f"Type {ERASE_CONFIRM_WORD} to proceed:",
            parent=self.root)
        if typed != ERASE_CONFIRM_WORD:
            self.set_status("Erase cancelled", duration=2000)
            return
        self.set_status(f"Erasing probe {unique_id} ...")
        self._run_pyocd_op("Erase", ["--erase", "--probe", unique_id], description, timeout=120)

    def _run_pyocd_op(self, label: str, extra_args: List[str], description: str = "",
                       timeout: int = 180) -> None:
        """Run flash_same54.py in the background and stream its output into the
        Command Output box line by line, instead of showing it all at once at the
        end.

        Line by line rather than subprocess.run(capture_output=True): that collects
        everything until the process exits, and the box would show NOTHING for the
        ~20-30s an erase/program/reset takes - unsettling right at the moment someone
        is most likely to wonder whether the click did anything at all.
        PYTHONUNBUFFERED affects every Python interpreter in the chain
        (flash_same54.py -> "python -m pyocd"), because neither of them sets its own
        env= - without it, Python buffers its own stdout in blocks as soon as the
        target is not a real console (here: the pipe). 'label' shows up in the status
        line ("Flash OK"/"Erase failed") and at the top of the log.
        """
        def worker():
            timestamp = time.strftime("%H:%M:%S")
            suffix = f"  ({description})" if description else ""
            self.result_queue.put(("op_line",
                                   f"[{timestamp}] $ flash_same54.py {' '.join(extra_args)}{suffix}"))
            env = dict(os.environ, PYTHONUNBUFFERED="1")
            success = False
            proc = None
            try:
                proc = subprocess.Popen(
                    [PYOCD_PYTHON, str(FLASH_SAME54_SCRIPT)] + extra_args,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, env=env,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                for line in proc.stdout:
                    self.result_queue.put(("op_line", line.rstrip("\n")))
                proc.wait(timeout=timeout)
                success = proc.returncode == 0
            except subprocess.TimeoutExpired:
                if proc is not None:
                    proc.kill()
                self.result_queue.put(("op_line", f"flash_same54.py did not finish within {timeout} s - killed."))
            except OSError as exc:
                self.result_queue.put(("op_line", f"flash_same54.py failed to start: {exc}"))
            self.result_queue.put(("op_done", success, label))

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def clean_response(command: str, output: str) -> str:
        """Remove the command echo and prompt characters from the response."""
        lines = []
        for raw in output.replace("\r", "\n").split("\n"):
            line = raw.strip().lstrip(">").strip()
            if not line or line == command.strip():
                continue
            lines.append(line)
        return "\n".join(lines)

    def _blink_loop(self):
        """Blink cursor in terminal"""
        if hasattr(self, 'terminal_blink'):
            self.terminal_blink()
        self.root.after(BLINK_MS, self._blink_loop)

    def process_queue(self):
        """Process results from background threads"""
        try:
            while True:
                result = self.result_queue.get_nowait()

                # Serial data goes to BOTH consumers: to the terminal for
                # display, and - only while a command is running - to the worker.
                # Two queues, one copy each, so there's no race for the chunks.
                if len(result) >= 3 and result[1] == "data":
                    self.terminal_q.put(result)
                    if self.cmd_pending.is_set():
                        self.cmd_response_q.put(result)
                    continue

                if result[0] == "port_opened":
                    _, link = result
                    self.port_link = link
                    self.connected = True
                    self.update_connection_indicator()
                    self.set_status(f"Connected to {link.port}", duration=2000)
                    if hasattr(self, 'terminal_note'):
                        self.terminal_note(f"connected: {link.port}")
                    if hasattr(self, 'bridge_output'):
                        self.bridge_output.config(state=tk.NORMAL)
                        timestamp = time.strftime("%H:%M:%S")
                        self.bridge_output.insert(tk.END, f"[{timestamp}] ✓ Connected to {link.port}\n\n")
                        self.bridge_output.config(state=tk.DISABLED)

                elif result[0] == "port_failed":
                    _, error = result
                    self.connected = False
                    self.update_connection_indicator()
                    self.set_error_status(f"Connection failed: {error}")
                    messagebox.showerror("Connection Error", error)

                elif result[0] == "connect_result":
                    _, success, message = result
                    if success:
                        self.set_status("Command OK", duration=2000)
                    else:
                        self.set_error_status(f"Error: {message}")

                elif result[0] == "cmd_result":
                    _, success, output = result
                    if success:
                        self.set_status("Command OK", duration=2000)
                        # Show output in bridge_output text widget
                        if hasattr(self, 'bridge_output'):
                            self.bridge_output.config(state=tk.NORMAL)
                            timestamp = time.strftime("%H:%M:%S")
                            self.bridge_output.insert(tk.END, f"[{timestamp}] {output}\n\n")
                            self.bridge_output.see(tk.END)
                            self.bridge_output.config(state=tk.DISABLED)
                    else:
                        self.set_error_status(f"Error: {output}")
                        messagebox.showerror("Error", f"Command failed:\n{output}")

                elif result[0] == "op_line":
                    # One line right away, not collected until the end - that is the
                    # whole point of streaming it (see _run_pyocd_op()).
                    if hasattr(self, 'bridge_output'):
                        self.bridge_output.config(state=tk.NORMAL)
                        self.bridge_output.insert(tk.END, result[1] + "\n")
                        self.bridge_output.see(tk.END)
                        self.bridge_output.config(state=tk.DISABLED)

                elif result[0] == "op_done":
                    _, success, label = result
                    if success:
                        self.set_status(f"{label} OK", duration=3000)
                    else:
                        self.set_error_status(f"{label} failed - see Command Output")
                        messagebox.showerror(
                            f"{label} failed",
                            "flash_same54.py reported an error - see Command Output for details.")

                elif result[0] == "register_read":
                    _, addr, success, value = result
                    if success and value:
                        self.register_fields[addr].set(value)
                        self.set_status(f"Read {addr}: {value}", duration=2000)
                    else:
                        self.set_error_status(f"Failed to read {addr}")

                elif result[0] == "bulk_progress":
                    _, done, total, failed = result
                    self.set_status(f"Reading registers {done}/{total}"
                                    + (f"  ({failed} without a response)" if failed else ""))

                elif result[0] == "bridge_read":
                    _, key, success, value = result
                    if success:
                        self.bridge_fields[key].set(value)
                        self.set_status(f"Read {key}: {value}", duration=2000)
                    else:
                        self.set_error_status(f"Failed to read {key}")

                elif result[0] == "env_identity":
                    # Identity of the EEPROM record. If the device reports something for which
                    # there is no model, the line states this explicitly -- and in red, because
                    # then the values below are not interpreted.
                    self.env_identity = result[1]
                    if self.env_identity is None:
                        # It was asked for, but no identity came back. That is something
                        # different from "not asked yet" and must be shown as such,
                        # otherwise an old firmware looks like an unread one.
                        self.env_identity_var.set(
                            "Environment: the device reported no id - this firmware "
                            "predates the identity line in showenv. The values below are read "
                            "with the model, without proof that it fits.")
                        self.env_identity_label_color(False)
                    else:
                        self.env_identity_var.set(self.env_identity_line())
                        # Red means "the values below can't be trusted", not "the
                        # identity is new". A legacy identity that the firmware accepts
                        # is just information -- coloring it red would make people used
                        # to a red line, and the real warning would get lost in it.
                        usable = bool(self.env_entry()) and \
                            self.env_identity.get("eeprom_crc", "").lower() == "ok"
                        self.env_identity_label_color(usable)

        except queue.Empty:
            pass

        # Process terminal queue
        if hasattr(self, 'terminal_process_queue'):
            self.terminal_process_queue()

        self.root.after(POLL_MS, self.process_queue)

    # Register read/write via open Link (not cli.py)
    def decode_one_bitfield(self, value_hex: str, bits_range: str) -> str:
        """The value of ONE bitfield, as an appendix for its own line.

        Empty string if nothing was read or the value is unreadable - then
        the line shows only the description, and nobody mistakes a 0 for a measurement.
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

    def send_command_via_link(self, cmd: str, timeout_ms: int = 700) -> str:
        """Send a command over the open link and wait for the response.

        Runs in the worker thread. The response arrives via cmd_response_q, which is
        only filled while cmd_pending is set -- so the terminal
        consumer in the main thread doesn't read away the same chunks.
        """
        if not self.port_link:
            return "ERROR: Not connected"

        # Only one command at a time, otherwise two responses would get mixed up.
        with self.cmd_lock:
            # Discard old leftovers, otherwise the previous command's response ends up here.
            while True:
                try:
                    self.cmd_response_q.get_nowait()
                except queue.Empty:
                    break

            self.cmd_pending.set()
            try:
                self.port_link.write(cmd.encode() + b"\r")

                start = time.time()
                chunks = []
                # Set the moment the response looks done (own echo + a trailing "> " on
                # its own line) but not yet trusted - see below for why it needs a
                # settle window instead of returning immediately.
                prompt_seen_at = None
                PROMPT_SETTLE_S = 0.15

                while time.time() - start < timeout_ms / 1000.0:
                    try:
                        port, kind, payload = self.cmd_response_q.get(timeout=0.01)
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
                    # Done as soon as the marker is present AND the line has been
                    # completed -- an "OK:" without a line ending is only the beginning.
                    # Always takes priority over the prompt-based check below, and is
                    # why 'lan_read'/'lan_write' resolve almost instantly despite
                    # needing the settle window (see below): their "OK:"/"ERROR" line
                    # lands a few ms after the prompt, well inside PROMPT_SETTLE_S, so
                    # this fires first on the very next chunk.
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
                    # legitimately arrives in two TCP segments with the eth0/eth1/mac/
                    # plca/mirror/sniffer lines up to a full second behind the identity
                    # line - the idle cutoff fired in that gap and truncated the
                    # response to the identity line alone, which then made every field
                    # in bridge_gui's env parser come back unmatched (found stays 0) -
                    # the "Command failed" dialog on Read Environment, confirmed
                    # 2026-09-04. Serial does not show the gap, which is why the same
                    # read worked over the COM port.
                    #
                    # 'cmd in text' guards against a DIFFERENT race this introduced: a
                    # rapid back-to-back loop (bulk_read_registers, one lan_read per
                    # register) can still have the PREVIOUS command's own trailing ">"
                    # in flight when this command's cmd_pending goes up - that stray
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
                    # near-100% of the time in bulk_read_registers - far worse than the
                    # original bug, and confirmed on both this GUI and the Telnet one
                    # (not Telnet-specific: the race is in the shared cmd_response_q/
                    # cmd_pending routing, not the link). The settle window lets the
                    # marker check above win the race on the next chunk when there is
                    # one; only a command like 'showenv', whose trailing prompt really
                    # is the last thing sent, ever actually waits out PROMPT_SETTLE_S.
                    last_line = text.replace("\r", "").rstrip("\n").rsplit("\n", 1)[-1].strip()
                    if last_line == ">" and cmd in text:
                        if prompt_seen_at is None:
                            prompt_seen_at = time.time()
                    else:
                        prompt_seen_at = None

                return "".join(chunks)
            finally:
                self.cmd_pending.clear()

    # Bridge parameter methods
    def read_all_bridge(self):
        """Fetch all bridge parameters with a single showenv."""
        if not self.port_link:
            self.set_error_status("Not connected")
            messagebox.showwarning("Not connected", "Press Connect first.")
            return

        self.set_status("Reading bridge parameters...")

        def worker():
            output = self.send_command_via_link("showenv", timeout_ms=1500)
            # Identity first, and the values are interpreted using EXACTLY this entry.
            # Otherwise the GUI would show filled fields under a line saying the
            # environment is unknown -- both from the same response, and contradictory.
            ident = self.parse_env_identity(output)
            entry = self.env_entry_for(ident)
            self.result_queue.put(("env_identity", ident))
            found = 0
            for key in self.bridge_fields:
                value = self.parse_showenv(output, key, entry)
                if value is not None:
                    found += 1
                self.result_queue.put(("bridge_read", key, value is not None, value or ""))
            self.result_queue.put(("cmd_result", found > 0,
                                   self.clean_response("showenv", output)
                                   or "showenv: no response"))

        threading.Thread(target=worker, daemon=True).start()

    def write_environment(self):
        """Write the whole environment: every filled field, then into the EEPROM.

        Two safeguards, both justified:

        The identity is checked beforehand. If the device reports an environment for which
        there is no model here, the setenv keys would be guesswork - so nothing gets
        written. If the identity hasn't been read yet at all, this function fetches it
        itself instead of assuming it will fit.

        And 'saveenv' is the default, not a follow-up question: whoever presses "Write Environment"
        wants something that survives the reset. Whoever only wants to change the RAM copy
        uses the Write button on the individual field.
        """
        if not self.port_link:
            self.set_error_status("Not connected")
            messagebox.showwarning("Not connected", "Press Connect first.")
            return

        if not self.env_identity:
            out = self.send_command_via_link("showenv", timeout_ms=1500)
            self.env_identity = self.parse_env_identity(out)
            self.env_identity_var.set(self.env_identity_line())

        env = self.env_entry()
        if not env:
            ident = self.env_identity or {}
            messagebox.showerror(
                "Unknown environment",
                f"The device reports environment id {ident.get('eeprom_id', '?')} "
                f"v{ident.get('eeprom_version', '?')}, which has no model in "
                f"{ENV_MODEL_FILE.name}.\n\n"
                "Nothing was written: which fields this environment has, and what they are "
                "called, would be guesswork.")
            return

        cmds = []
        for key, var in self.bridge_fields.items():
            value = var.get().strip()
            if not value:
                continue
            cmd = self.bridge_write_command(key, value)
            if cmd:
                cmds.append(cmd)

        if not cmds:
            messagebox.showinfo("Info", "No writable parameter has a value.")
            return

        persist_cmd = env.get("commands", {}).get("persist", "saveenv")
        if not messagebox.askyesno(
                "Write environment?",
                f"Send {len(cmds)} values to the device and store them in the EEPROM "
                f"with '{persist_cmd}'?\n\n" + "\n".join(cmds) + f"\n{persist_cmd}"):
            return

        def worker():
            log = []
            for cmd in cmds:
                out = self.send_command_via_link(cmd, timeout_ms=1500)
                log.append(f"> {cmd}\n{self.clean_response(cmd, out)}")
            out = self.send_command_via_link(persist_cmd, timeout_ms=3000)
            log.append(f"> {persist_cmd}\n{self.clean_response(persist_cmd, out)}")
            # Afterwards, read back what is actually stored - a write confirmation is
            # no proof that the device actually accepted the value.
            check = self.send_command_via_link("showenv", timeout_ms=1500)
            self.result_queue.put(("env_identity", self.parse_env_identity(check)))
            for key in self.bridge_fields:
                value = self.parse_showenv(check, key)
                self.result_queue.put(("bridge_read", key, value is not None, value or ""))
            log.append(f"> showenv (verify)\n{self.clean_response('showenv', check)}")
            self.result_queue.put(("cmd_result", True, "\n".join(log)))

        threading.Thread(target=worker, daemon=True).start()
        self.set_status("Writing environment...")

    def bridge_write_command(self, key: str, value: str) -> Optional[str]:
        """Field name -> CLI command, both from the model.

        Neither the key nor the command form are in the source code anymore: 'commands.
        write_field' and 'cli_key' come from env_model.json, so a different firmware
        variant only needs a different model file and no patch here.
        """
        env = self.env_entry()
        fld = env.get("fields", {}).get(key)
        if not fld:
            return None
        template = env.get("commands", {}).get("write_field", "setenv {cli_key} {value}")
        return template.format(cli_key=fld["cli_key"], value=value)

    def parse_showenv(self, output: str, key: str, entry: Optional[dict] = None) -> Optional[str]:
        """Pull one value from the showenv output -- using the pattern from the model.

        'entry' allows passing in the model entry instead of using the GUI's
        current one: the worker has already resolved the identity from the same output.

        'reads_as' maps the device's display onto the value setenv expects
        (mirror reports ON/OFF, but 1/0 is written). Without that, the GUI would show a word
        that can't be written back.
        """
        env = self.env_entry() if entry is None else entry
        fld = env.get("fields", {}).get(key)
        if not fld or not fld.get("pattern"):
            return None
        m = re.search(fld["pattern"], output)
        if not m:
            return None
        raw = m.group(1)
        return fld.get("reads_as", {}).get(raw, raw)

    def parse_env_identity(self, output: str) -> Optional[dict]:
        """Die Kennungszeile von showenv auswerten (Muster: env_model.json 'identity')."""
        ident = self.env_model.get("identity", {})
        if not ident.get("pattern"):
            return None
        m = re.search(ident["pattern"], output)
        if not m:
            return None
        return dict(zip(ident.get("groups", []), m.groups()))

    def save_bridge_json(self):
        """Save bridge parameters to JSON"""
        self.config["bridge"] = {}
        for key, var in self.bridge_fields.items():
            value = var.get()
            try:
                # Try to convert to number if possible
                if '.' in value:
                    self.config["bridge"][key] = float(value)
                else:
                    self.config["bridge"][key] = int(value) if value.isdigit() else value
            except ValueError:
                self.config["bridge"][key] = value

        self.save_config()
        self.set_status("Bridge config saved to JSON")

    def load_bridge_json(self):
        """Load bridge parameters from JSON"""
        if not CONFIG_FILE.exists():
            messagebox.showwarning("Warning", "Config file not found")
            return

        cfg = json.load(open(CONFIG_FILE, encoding="utf-8"))
        bridge_cfg = cfg.get("bridge", {})

        for key, value in bridge_cfg.items():
            if key in self.bridge_fields:
                self.bridge_fields[key].set(str(value))

        self.set_status("Bridge config loaded from JSON")

    # Register methods
    def read_register(self, addr: str):
        """Read a single register via open Link"""
        def worker():
            if not self.port_link:
                self.result_queue.put(("register_read", addr, False, "Not connected"))
                return

            output = self.send_command_via_link(f"lan_read {addr}")
            value = self.cli.parse_register_read(output)
            if not value:
                value = output.strip()

            success = "OK:" in output
            self.result_queue.put(("register_read", addr, success, value))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

    def write_register(self, addr: str):
        """Write a single register via open Link"""
        value = self.register_fields.get(addr, tk.StringVar()).get()

        if not value:
            messagebox.showwarning("Warning", f"No value for {addr}")
            return

        if not value.startswith("0x"):
            value = "0x" + value

        def worker():
            if not self.port_link:
                self.result_queue.put(("register_read", addr, False, "Not connected"))
                return

            # Write
            output = self.send_command_via_link(f"lan_write {addr} {value}")
            time.sleep(0.2)

            # Read back to verify
            output_rb = self.send_command_via_link(f"lan_read {addr}")
            value_readback = self.cli.parse_register_read(output_rb)
            success_rb = "OK:" in output_rb

            self.result_queue.put(("register_read", addr, success_rb, value_readback or output_rb.strip()))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        self.set_status(f"Writing {addr} = {value}...")

    def bulk_read_registers(self):
        """Read all registers of the current tab."""
        addrs = list(self.register_fields.keys())
        self.set_status(f"Reading {len(addrs)} registers...")

        def worker():
            if not self.port_link:
                self.set_error_status("Not connected")
                return

            failed = []
            for n, addr in enumerate(addrs, 1):
                # Up to 2 retries: back-to-back 'lan_read' can occasionally outrun the
                # LAN865x SPI transaction ("ERROR: Previous LAN operation still in
                # progress" - a real, transient busy state, not a bug), or lose a rare
                # race against send_command_via_link()'s prompt-settle window. Neither
                # reproduces on a fresh attempt a few ms later, so retrying is cheap and
                # far better than either slowing every read down or reporting a false
                # "no response" - confirmed 2026-09-04 (repeated bulk reads against the
                # same board show a different 0-4 addresses failing each time, not a
                # consistent set, which is what a real per-register issue would look
                # like instead).
                value = ""
                for attempt in range(3):
                    output = self.send_command_via_link(f"lan_read {addr}")
                    value = self.cli.parse_register_read(output)
                    if not value:
                        m = re.search(r'Value=(0x[0-9A-Fa-f]+)', output)
                        value = m.group(1) if m else ""
                    if value:
                        break

                if value:
                    self.result_queue.put(("register_read", addr, True, value))
                else:
                    failed.append(addr)
                    self.result_queue.put(("register_read", addr, False, ""))

                if n % 10 == 0 or n == len(addrs):
                    self.result_queue.put(("bulk_progress", n, len(addrs), len(failed)))

            if failed:
                self.result_queue.put(("cmd_result", True,
                                       f"Bulk read: {len(addrs)-len(failed)}/{len(addrs)} ok, "
                                       f"no response from: {', '.join(failed[:12])}"
                                       + (" ..." if len(failed) > 12 else "")))

        threading.Thread(target=worker, daemon=True).start()

    def bulk_write_registers(self):
        """Write all registers via open Link"""
        if not messagebox.askyesno("Confirm", "Write all registers to device?"):
            return

        self.set_status("Writing all registers...")

        def worker():
            if not self.port_link:
                self.set_error_status("Not connected")
                return

            for addr, var in self.register_fields.items():
                value = var.get()
                if not value:
                    continue

                if not value.startswith("0x"):
                    value = "0x" + value

                self.send_command_via_link(f"lan_write {addr} {value}")
                time.sleep(0.15)

            # Bulk read to verify
            self.result_queue.put(("cmd_result", True, "All registers written. Reading back..."))
            time.sleep(0.2)

            for addr in self.register_fields.keys():
                output = self.send_command_via_link(f"lan_read {addr}")
                value = self.cli.parse_register_read(output)
                success = "OK:" in output
                if value:
                    self.result_queue.put(("register_read", addr, success, value))

                time.sleep(0.15)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

    def save_registers_json(self):
        """Save only the VALUES that were read. The GUI can no longer touch the model.

        This function used to write the whole register map back and in doing so
        damaged it twice: once because it rebuilt it from the widgets (anything without a
        widget fell out), once via the encoding. Since the map lives in lan8651_model.json
        and only {address: value} ends up here, this class of bug is structurally gone --
        a function that doesn't write a file can't corrupt it.
        """
        if not self.register_fields:
            messagebox.showwarning("Warning", "No register model loaded - nothing saved.")
            return

        values = {addr: var.get() for addr, var in self.register_fields.items() if var.get()}
        self.config["values"] = values
        self.config.pop("registers", None)  # leftover from when the map used to live here
        self.save_config()

        self.set_status(f"{len(values)} register values saved "
                        f"(model untouched)", duration=3000)

    def load_registers_json(self):
        """Load saved register values back into the fields."""
        if not CONFIG_FILE.exists():
            messagebox.showwarning("Warning", "Config file not found")
            return

        cfg = json.load(open(CONFIG_FILE, encoding="utf-8"))
        n = 0
        for addr, val in (cfg.get("values") or {}).items():
            if addr in self.register_fields and val:
                self.register_fields[addr].set(str(val))
                n += 1

        # Older bridge_config.json files still carried the values in the "registers" tree.
        for key, value in (cfg.get("registers") or {}).items():
            if isinstance(value, dict):
                for addr, entry in value.items():
                    if addr not in self.register_fields:
                        continue
                    val = entry.get("value", "") if isinstance(entry, dict) else str(entry)
                    if val:
                        self.register_fields[addr].set(val)
                        n += 1
            elif key in self.register_fields:
                self.register_fields[key].set(str(value))
                n += 1

        self.set_status(f"{n} register values loaded from JSON", duration=3000)

    # Test mode methods
    def read_testmode(self):
        """Read current test mode"""
        self.run_async_cmd("lan_read 0x000308FB")

    def apply_testmode(self, mode: int):
        """Start the given test mode (0 = back to normal).

        Modes 1-4 each have their own auto-revert field on the Test Modes tab; mode 0 has
        none, since "return to normal" has no duration to set. An empty field means the
        mode runs until something else changes it, matching what the firmware's own
        `testmode <mode>` (no timeout argument) does.
        """
        timeout_var = getattr(self, "testmode_timeout_vars", {}).get(mode)
        timeout = timeout_var.get().strip() if timeout_var else ""

        cmd = f"testmode {mode}"
        if timeout:
            cmd += f" {timeout}"

        self.run_async_cmd(cmd)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--light", action="store_true", help="use the light variant instead of dark")
    args = ap.parse_args()

    import dep_check
    if not dep_check.ensure_dependencies(
            hard=[("sv_ttk", "sv-ttk")], optional=[("serial", "pyserial")]):
        sys.exit(0)
    import sv_ttk

    root = tk.Tk()
    sv_ttk.set_theme("light" if args.light else "dark")
    gui = BridgeGUI(root, dark=not args.light)
    root.mainloop()


if __name__ == "__main__":
    main()
