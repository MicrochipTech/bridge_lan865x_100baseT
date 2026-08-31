#!/usr/bin/env python3
"""
Bridge Status & Configuration GUI

Bedient die T1S/100BASE-T-Bridge über den EDBG-COM-Port (115200 8N1):
Bridge-Parameter, LAN8651-Register, IEEE-Testmodi und ein Terminal.

Standalone bis auf zwei pip-Pakete: sv-ttk (Pflicht, fürs Theme) und pyserial
(optional, ohne COM-Port-Zugriff läuft das Tool trotzdem) - dep_check.py
prüft beides beim Start und bietet bei Bedarf setup_venv.bat an.
Dazu bridge_config.json (liegt im json-Ordner im Repo-Root, zwei Ebenen über
diesem Skript). Kein cli.py, kein test_lan8651.py --
die öffnen den COM-Port selbst und kollidieren mit der Verbindung dieser GUI,
weil der Port unter Windows exklusiv ist. Alle Kommandos laufen über den
einen offenen Link.
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
# sister project's GUI, so a fresh clone can flash without building first. Not the
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

# Das Registermodell: Adressen, Mnemonics, Bitfelder, Herkunft. Getrennt von der
# Konfiguration, weil es etwas anderes ist -- eine aus dem Datenblatt abgeleitete
# Referenz, auf die sich jemand verlaesst, der einen Fehler sucht. Die GUI liest es
# und schreibt es NIE; Werte und Sitzungszustand gehoeren in bridge_config.json.
# Stimmt etwas nicht, wird diese Datei korrigiert, nicht dieser Quelltext -- danach
# "python scripts\check_register_model.py". lan8651_model.json liegt in json\
# (Repo-Root), nicht neben diesem Skript.
MODEL_FILE = Path(__file__).parent.parent / "json" / "lan8651_model.json"

# Das Environment-Modell: welche Felder der EEPROM-Datensatz hat, wie sie aus showenv
# gelesen und mit welchem CLI-Kommando sie geschrieben werden -- je Kennung und Version.
# Firmware-Varianten teilen sich den EEPROM-Offset, aber nicht das Layout; deshalb wird
# die Kennung vom Geraet gelesen und gegen dieses Modell gehalten, statt sie zu raten.
# env_model.json liegt ebenfalls in json\ (Repo-Root).
ENV_MODEL_FILE = Path(__file__).parent.parent / "json" / "env_model.json"

# Default configuration
# Vorgaben, falls bridge_config.json fehlt. Die Registerkarte kommt dann NICHT
# mit -- sie stammt aus dem Datenblatt (LAN8650-1-Data-Sheet-60001734.pdf,
# Kapitel 11, 182 Register) und steht ausschliesslich in bridge_config.json.
# Frueher standen hier von Hand ergaenzte Adressen; vier davon existierten gar
# nicht und zwei trugen den falschen Namen, siehe CLAUDE.md Abschnitt 6.
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
    """Wertet die Antwortzeilen des Geräts aus.

    Hier lief früher ein Unterprozess auf cli.py. Das ging nicht: cli.py öffnet
    den COM-Port selbst (cli.py:47), und der ist unter Windows exklusiv -- solange
    die GUI verbunden ist, bekommt der Unterprozess "Zugriff verweigert". Alle
    Kommandos laufen deshalb über den bereits offenen Link
    (BridgeGUI.send_command_via_link); übrig bleibt das Parsen.
    """

    def parse_register_read(self, output: str) -> Optional[str]:
        """Wert aus 'LAN865X Read OK: Addr=... Value=...' ziehen."""
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
        # Was das Geraet ueber sein Environment gemeldet hat. Bleibt None, solange nichts
        # gelesen wurde -- die GUI behauptet dann nicht, sie wuesste, was im EEPROM steht.
        self.env_identity: Optional[dict] = None
        self.cli = ResponseParser()
        self.result_queue = queue.Queue()
        self.connected = False
        self.port_link: Optional[Link] = None  # Globale Verbindung für CLI + Terminal

        # The path last picked via "Select Hex..." -- "Flash" uses this instead of
        # always RELEASE_HEX (release\bridge_lan865x_100baseT.hex) once one has been
        # chosen. Session state only (like bridge_config.json's "values"), not
        # persisted.
        self._selected_hex_path: Path = RELEASE_HEX

        # Kommando-Antworten laufen über eine EIGENE Queue. Sonst konkurrieren
        # terminal_process_queue() (Main-Thread, alle 30 ms) und der Worker-Thread
        # um dieselben Chunks, und die Antwort kommt zerrissen an -> leere Felder.
        self.cmd_response_q = queue.Queue()
        self.cmd_pending = threading.Event()
        self.cmd_lock = threading.Lock()

        # Scrollbare Canvases (Register-Tab je MMS-Gruppe, Bridge-Parameter-Tab), fuer den
        # EINEN globalen Mausrad-Handler in _register_wheel_canvas/_on_global_wheel.
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
        """Ein scrollbares Canvas fuer den globalen Mausrad-Handler eintragen."""
        self._wheel_canvases.append(canvas)

    def _bind_page_scroll_keys(self) -> None:
        """Bild-hoch/-runter scrollt den sichtbaren Tab -- unabhaengig vom Mausrad.

        Reine Tastatur-Ereignisse sind von der Frage "welches Ereignis liefert das
        Eingabegeraet ueberhaupt" ganz unabhaengig (siehe _on_global_wheel) und
        funktionieren deshalb auch dort, wo MouseWheel nie ankommt -- etwa bei einem
        Windows-Precision-Touchpad. Bewusst nur Prior/Next (Bild hoch/runter), nicht
        Pfeiltasten oder Pos1/Ende: die wuerden in einem fokussierten Entry-Feld oder
        einer Combobox mitlaufen (Cursor bewegen, Dropdown oeffnen) und die Ansicht dabei
        ungefragt mitscrollen. Prior/Next sind dort standardmaessig unbelegt.

        Welches Canvas gerade "sichtbar" ist, wird ueber <<NotebookTabChanged>> auf
        beiden Notebooks (Haupt-Tabs, Register-Untertabs) nachgefuehrt; die Terminal-
        eigene Prior/Next-Bindung auf terminal_text hat dort weiterhin Vorrang, weil eine
        Bindung auf der Widget-Instanz vor "all" (bind_all) ausgewertet wird.
        """
        self._active_scroll_canvas: Optional[tk.Canvas] = None

        def _scroll_active(direction):
            if self._active_scroll_canvas is not None:
                self._active_scroll_canvas.yview_scroll(direction, "pages")

        self.root.bind_all("<Prior>", lambda e: _scroll_active(-1))
        self.root.bind_all("<Next>", lambda e: _scroll_active(1))

    def _on_main_tab_changed(self, event=None):
        """Haupt-Tab gewechselt: das dort scrollbare Canvas fuer Bild-hoch/-runter setzen.

        Bei "LAN8651 Registers" gilt der aktuell gewaehlte MMS-Untertab, nicht dieser Tab
        selbst -- dafuer ist reg_notebook.select() bereits gueltig, weil das Untertab-
        Notebook beim Anlegen des Haupt-Tabs mit aufgebaut wird.
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
            # Terminal/Help scrollen nicht ueber ein registriertes Canvas.
            self._active_scroll_canvas = None

    def _on_reg_tab_changed(self, event=None):
        """MMS-Untertab gewechselt: dessen Canvas fuer Bild-hoch/-runter uebernehmen.

        Das Register-Untertab-Notebook waehlt beim Aufbau intern seinen ersten Tab aus
        und feuert dabei sein EIGENES <<NotebookTabChanged>> -- unabhaengig davon, ob
        "LAN8651 Registers" ueberhaupt der sichtbare Haupttab ist. Ohne diese Pruefung
        ueberschreibt dieses interne Ereignis das richtige Canvas des Bridge-Parameter-
        Tabs, sobald der Nutzer die Register-Untertabs auch nur EINMAL beim Programmstart
        intern durchlaeuft (was Tk beim Aufbau selbst tut).
        """
        if self.notebook.tab(self.notebook.select(), "text") != "LAN8651 Registers":
            return
        self._active_scroll_canvas = self._reg_tab_canvases.get(self._reg_notebook.select())

    def _on_global_wheel(self, event):
        """EIN Handler fuers ganze Fenster statt Enter/Leave je Canvas.

        Bleibt fuer eine echte Maus stehen: EIN Handler haengt global (`bind_all`) und
        feuert bei jedem MouseWheel-Ereignis, egal welches Widget es technisch zugestellt
        bekam -- er entscheidet selbst, was gescrollt wird, ueber die tatsaechliche
        Zeigerposition (`event.x_root/y_root`) und `winfo_containing`.

        Auf DIESEM Rechner reicht das allein nicht: ein isolierter Test (nur ein Canvas,
        sonst nichts) zeigte hunderte Enter/Motion-Ereignisse, aber KEIN einziges
        MouseWheel, obwohl real gescrollt wurde -- das Geraet ist ein Windows-Precision-
        Touchpad, dessen Zwei-Finger-Geste bei Tk-Fenstern gar kein WM_MOUSEWHEEL
        ausloest. Kein Bindungsproblem, das Ereignis kommt nie an. Deshalb zusaetzlich
        die Tastatur-Bindungen unten (_bind_page_scroll_keys) als Weg, der nicht von der
        Zustellung dieses Ereignisses abhaengt.
        """
        try:
            target = self.root.winfo_containing(event.x_root, event.y_root)
        except KeyError:
            # winfo_containing kann das werfen, wenn der Zeiger gerade ueber einem
            # Widget eines fremden Toplevels/Prozesses steht.
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
        """Das Registermodell laden. Fehlt es, wird das GESAGT, nicht verschwiegen.

        Ein leerer Registertab waere die schlechteste Antwort: die GUI saehe funktionsfaehig
        aus und haette nur keine Register. Wer hier debuggt, soll wissen, dass ihm die
        Referenz fehlt.
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
        """Das Environment-Modell laden. Fehlt es, bleibt der Parametertab leer und sagt es."""
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
        """Modelleintrag zu EINER Kennung -- ohne den Zustand der GUI zu befragen.

        Der Worker braucht das: er hat die Kennung gerade erst aus derselben showenv-Ausgabe
        gezogen, self.env_identity wird aber erst im Main-Thread gesetzt. Griffe er auf
        env_entry() zurueck, deutete er die Werte nach dem ALTEN Stand -- und die GUI zeigte
        gefuellte Felder unter einer Zeile, die sagt, sie seien nicht gedeutet.
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
        """Der Modelleintrag, nach dem die GUI gerade arbeitet.

        Solange das Geraet nichts gemeldet hat, ist das der einzige bzw. erste Eintrag --
        die Felder muessen ja aufgebaut werden, bevor jemand verbindet. Nach dem Lesen der
        Kennung wird der passende Eintrag genommen; gibt es keinen, ist das Ergebnis leer
        und die GUI zeigt die Werte als nicht deutbar an, statt sie zu erfinden.
        """
        envs = self.env_model.get("environments", {})
        if not envs:
            return {}
        if self.env_identity:
            return self.env_entry_for(self.env_identity)
        return next(iter(envs.values()))

    def env_identity_label_color(self, ok: bool) -> None:
        """Grau, solange alles zusammenpasst - rot, sobald es das nicht tut."""
        widget = getattr(self, "_env_identity_widget", None)
        if widget is not None:
            widget.configure(foreground="#555" if ok else "#b00")

    def env_identity_line(self) -> str:
        """Die Zeile ueber dem Parametertab: was im EEPROM steht und ob wir es deuten koennen."""
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
                # Kein Fehler: die Firmware liest diese Alt-Kennung noch und hat den
                # Datensatz angenommen -- sonst gaebe es hier keinen Modelleintrag. Beim
                # naechsten saveenv schreibt sie ihn mit der neuen Kennung zurueck.
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
        """Einzeiler zur Herkunft, den die GUI anzeigt -- damit sie nicht mehr behauptet,
        als sie belegen kann."""
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
        """Konfiguration schreiben -- erst serialisieren, dann ersetzen.

        open(..., 'w') leert die Datei beim Öffnen; scheitert json.dump danach,
        ist die Registerkarte weg. Deshalb zuerst in Bytes wandeln (ein Fehler
        fliegt dann, bevor irgendetwas angefasst wird), in eine Nachbardatei
        schreiben und erst zum Schluss über os.replace austauschen.
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

        # Connect/Disconnect buttons
        ttk.Button(top_frame, text="🟢 Connect", command=self.connect_device).pack(side=tk.LEFT, padx=2)
        ttk.Button(top_frame, text="🔴 Disconnect", command=self.disconnect_device).pack(side=tk.LEFT, padx=2)

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
        # Binding VOR den add()-Aufrufen: <<NotebookTabChanged>> wird von Tk nicht
        # synchron ausgeloest, sondern erst beim naechsten Event-Loop-Durchlauf (also
        # fruehestens beim ersten mainloop()/update()) verarbeitet. Zu dem Zeitpunkt ist
        # der Tab-Aufbau laengst fertig, die Zielattribute existieren also -- der Handler
        # ist damit die einzige Quelle der Wahrheit, kein Wettlauf mit einer expliziten
        # Zuweisung am Ende, die dieses nachgeholte Ereignis sonst stillschweigend
        # ueberschreibt.
        self.notebook.bind("<<NotebookTabChanged>>", self._on_main_tab_changed)

        self.create_bridge_tab()
        self.create_registers_tab()
        self.create_testmodes_tab()
        self.create_terminal_tab()
        self.create_about_tab()

        # Sofortiger Startwert, falls aus irgendeinem Grund kein <<NotebookTabChanged>>
        # nachgeholt wird -- "Bridge Parameters" ist der initial sichtbare Tab.
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

        # Kennung des Environments ganz oben, VOR dem Paned-Bereich - der fuellt mit
        # expand=True den Rest, ein spaeter gepacktes Label landete darunter. Sie
        # entscheidet, ob die Werte unten bedeuten, was die Beschriftung sagt.
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

        # Die Felder kommen aus env_model.json, nicht aus einer Liste im Quelltext: welche
        # es gibt, haengt an Kennung und Version des Geraets, und genau das ist der Punkt.
        saved = self.config.get("bridge", {})
        # Der Normalfall: das Kommando, das den Wert dauerhaft macht. Felder, die genau so
        # wirken, brauchen keinen Hinweis - nur die Ausreisser bekommen einen.
        default_applies = self.env_entry().get("commands", {}).get("persist", "saveenv")
        for key, fld in self.env_entry().get("fields", {}).items():
            self.bridge_fields[key] = tk.StringVar(value=str(saved.get(key, "")))
            row = ttk.Frame(scrollable_frame)
            row.pack(fill=tk.X, padx=5, pady=(4, 0))

            # Kein Read/Write je Feld: das Environment wird als Ganzes gelesen ("Read
            # Environment", ein showenv) und als Ganzes geschrieben ("Write Environment", ein saveenv).
            # Ein einzelnes Feld isoliert zu schreiben ergibt hier keinen Sinn, den die
            # Register-Tabelle nebenan hat - dort ist jedes Register ein eigenständiger
            # Zugriff, hier ist es ein gemeinsamer Datensatz.
            ttk.Label(row, text=fld.get("label", key) + ":", width=22).pack(side=tk.LEFT)
            ttk.Entry(row, textvariable=self.bridge_fields[key], width=30).pack(side=tk.LEFT, padx=5)

            # Wann der Wert wirkt -- aber NUR, wenn es vom Normalfall abweicht. Fuer elf
            # der dreizehn Felder ist das saveenv, und eine Zeile "mask0 -> saveenv" unter
            # "mask eth0:" wiederholt bloss die Beschriftung. Uebrig bleiben die zwei
            # Faelle, die wirklich ueberraschen: die MAC wirkt erst nach einem Reset, der
            # Mirror erst beim naechsten Boot. Die stehen dafuer in der Zeile und in Rot.
            applies = fld.get("applies", "")
            if applies and applies != default_applies:
                ttk.Label(row, text=f"  {applies}", font=("Courier", 8),
                          foreground="#b00").pack(side=tk.LEFT)

        # Mausrad: dieses Canvas nur in die zentrale Liste eintragen. Die eigentliche
        # Zustellung erledigt EIN globaler Handler (siehe _register_wheel_canvas) - siehe
        # dort, warum weder Enter/Leave am Canvas noch eine direkte Bindung auf die
        # Kind-Widgets zuverlaessig war.
        self._register_wheel_canvas(canvas)
        self._bridge_scroll_canvas = canvas  # fuer den Bild-hoch/-runter-Fallback

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
        # Registerkarte (Name/Beschreibung/Bitfelder je Adresse) im Speicher halten.
        # Ohne die schrieb save_registers_json nur {Adresse: Wert} zurück und hat
        # die aus dem Datenblatt erzeugte Karte beim ersten Speichern vernichtet.
        self.register_meta: Dict[str, dict] = {}

        # Herkunftszeile: welches Dokument, welche Revision, wie viel davon abgeglichen.
        # Steht bewusst oben und nicht in einem Hilfetext -- wer Register liest, soll
        # sehen, worauf er sich gerade verlaesst.
        ttk.Label(frame, text=self.model_source_line(), foreground="#555").pack(
            anchor="w", padx=8, pady=(4, 0))

        # Create sub-notebook for register categories
        reg_notebook = ttk.Notebook(frame)
        reg_notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self._reg_notebook = reg_notebook            # fuer den Bild-hoch/-runter-Fallback
        self._reg_tab_canvases: Dict[str, tk.Canvas] = {}  # Tab-Name (Widgetpfad) -> Canvas
        reg_notebook.bind("<<NotebookTabChanged>>", self._on_reg_tab_changed)

        # Die Karte kommt aus dem Modell, die zuletzt gelesenen Werte aus der Config.
        registers = {name: g.get("registers", {})
                     for name, g in self.model.get("groups", {}).items()}
        saved_values = self.config.get("values", {})

        # Create a tab for each category
        for category, regs in registers.items():
            self.register_categories[category] = list(regs.keys())
            category_frame = ttk.Frame(reg_notebook)
            reg_notebook.add(category_frame, text=category)

            # Scrollbarer Bereich. Die Bindungen MÜSSEN das Canvas dieses
            # Durchlaufs festhalten (Default-Argument): ein `lambda e: canvas...`
            # greift auf die Schleifenvariable zu, und die zeigt nach dem letzten
            # Durchlauf für ALLE Tabs auf dasselbe, zuletzt erzeugte Canvas --
            # dann bekommt nur der letzte Tab eine gültige scrollregion und alle
            # anderen lassen sich nicht über die sichtbare Höhe hinaus scrollen.
            canvas = tk.Canvas(category_frame, highlightthickness=0)
            scrollbar = ttk.Scrollbar(category_frame, orient="vertical", command=canvas.yview)
            scrollable_frame = ttk.Frame(canvas)
            inner_id = canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
            canvas.configure(yscrollcommand=scrollbar.set)

            def _on_content(event, cv=canvas):
                cv.configure(scrollregion=cv.bbox("all"))

            def _on_canvas(event, cv=canvas, wid=inner_id):
                # Inhalt auf die volle Canvasbreite ziehen, sonst klebt alles links
                cv.itemconfigure(wid, width=event.width)
                cv.configure(scrollregion=cv.bbox("all"))

            scrollable_frame.bind("<Configure>", _on_content)
            canvas.bind("<Configure>", _on_canvas)
            # Mausrad: dieses Canvas nur in die zentrale Liste eintragen, siehe
            # _register_wheel_canvas fuer den eigentlichen (globalen) Handler.
            self._register_wheel_canvas(canvas)
            self._reg_tab_canvases[str(category_frame)] = canvas

            # Create fields for each register in this category
            for addr, info in regs.items():
                self.register_fields[addr] = tk.StringVar(value=saved_values.get(addr, ""))

                # Modellformat: mnemonic + name, bits als Objekte mit name/description.
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

                # Register mit Errata-Eintrag werden markiert. Ohne die Marke sieht ein
                # Register, dessen Wert laut Errata nicht bedeutet, was dransteht, genauso
                # aus wie jedes andere -- das ist die Irrefuehrung, um die es hier geht.
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

                    # Bitfield definitions. Der ausgelesene Wert steht am Ende DERSELBEN
                    # Zeile statt in einer Sammelzeile darunter: das spart je Register eine
                    # Zeile und erspart das Zurueckspringen zwischen Feldname und Wert.
                    # Zwei Labels nebeneinander, weil ein ttk.Label nur eine Farbe kann.
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

                    # Ein Callback je Register aktualisiert alle seine Felder. Die
                    # Default-Argumente sind Pflicht: ohne sie zeigen alle Callbacks nach
                    # der Schleife auf die zuletzt erzeugten Variablen.
                    def make_update_decoded(val_var=value_var, fvars=field_vars):
                        def update_decoded(*args):
                            hex_val = val_var.get()
                            for spec, var in fvars.items():
                                var.set(self.decode_one_bitfield(hex_val, spec))
                        return update_decoded

                    callback = make_update_decoded()
                    value_var.trace_add("write", callback)
                    callback()   # gespeicherte Werte gleich beim Aufbau zeigen

            canvas.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")

        # The bulk buttons that used to sit here are in the top bar now (create_widgets),
        # where they stay visible no matter which tab is open or how far it is scrolled.

    # (mode, title, description) - description is the "ausführlich erklärt" text shown
    # in that mode's own group. Kept here as data, not spread across widget calls, so a
    # fifth mode is one more tuple. Longer background (setup notes, safety) stays in
    # docs/LAN8651_TEST_MODES.md; this is the summary worth having next to the button.
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

        # Scrollable wie die anderen inhaltsschweren Tabs (Bridge Parameters, Registers) -
        # fuenf Gruppen mit Beschreibung passen nicht auf jeden Bildschirm. Siehe
        # _register_wheel_canvas fuer den globalen Mausrad-Handler und
        # _bind_page_scroll_keys fuer den Bild-hoch/-runter-Fallback (Touchpad).
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

        # Zwei Spalten statt einer langen Kette von fuenf Gruppen - Mode 0 ueber die volle
        # Breite, Mode 1-4 im 2x2-Raster darunter. Nutzt die Breite, die ein maximiertes
        # Fenster tatsaechlich hat, statt sie ungenutzt zu lassen, und braucht dadurch nur
        # drei Zeilen statt fuenf: passt auf jeden vernuenftig grossen Bildschirm, ohne zu
        # scrollen. Alle Kinder von scrollable_frame muessen deshalb konsequent grid() statt
        # pack() verwenden - Tk erlaubt beides nicht gemischt im selben Container.
        scrollable_frame.columnconfigure(0, weight=1, uniform="testmode_col")
        scrollable_frame.columnconfigure(1, weight=1, uniform="testmode_col")

        ttk.Label(
            scrollable_frame,
            text="⚠️  Test modes disconnect the T1S link - the bridge is unreachable while one is active.\n"
                 "Register: T1STSTCTL (0x000308FB), bits 15:13. Background and measurement setup: docs/LAN8651_TEST_MODES.md",
            justify=tk.LEFT, foreground="red"
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=8, pady=8)

        # Mode 0: immer zuerst erreichbar, unabhaengig davon, welcher Testmodus gerade
        # laeuft - kein Auto-Revert-Feld, "Normalbetrieb" hat keine Dauer.
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

        # Je Testmodus ein eigenes StringVar fuers Auto-Revert-Feld, damit apply_testmode
        # weiss, welches Feld zu welchem "Start"-Knopf gehoert.
        self.testmode_timeout_vars: Dict[int, tk.StringVar] = {}
        for i, (mode, title, description) in enumerate(self.TEST_MODES):
            grp_row, grp_col = divmod(i, 2)
            grp = ttk.LabelFrame(scrollable_frame, text=f"Mode {mode} - {title}", padding=10)
            grp.grid(row=2 + grp_row, column=grp_col, sticky="nsew", padx=8, pady=5)

            # wraplength haelt die Beschreibung in der jetzt halb so breiten Spalte lesbar,
            # statt die Spalte auf die Laenge der laengsten Zeile aufzuziehen.
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

This GUI provides an interface to the T1S/100BASE-T Bridge firmware.

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
        """Update the connection indicator circle and label"""
        if self.connected:
            self.connection_indicator.delete("all")
            self.connection_indicator.create_oval(2, 2, 13, 13, fill="green", outline="darkgreen")
            self.connection_label.config(text="Online", foreground="green")
        else:
            self.connection_indicator.delete("all")
            self.connection_indicator.create_oval(2, 2, 13, 13, fill="red", outline="darkred")
            self.connection_label.config(text="Offline", foreground="red")

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
        """Kommando über den offenen Link absetzen, Antwort ins Command Output."""
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
        (release\\ by default, same as the sister project's "Select Hex..."). The
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
        the module docstring: "Standalone. Gebraucht werden nur pyserial"). Output is
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
        """Echo des Kommandos und Prompt-Zeichen aus der Antwort entfernen."""
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

                # Serielle Daten gehen an BEIDE Konsumenten: ans Terminal zur
                # Anzeige und - nur solange ein Kommando läuft - an den Worker.
                # Zwei Queues, je eine Kopie, deshalb kein Wettlauf um die Chunks.
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
                    # Kennung des EEPROM-Datensatzes. Meldet das Geraet etwas, wofuer es
                    # kein Modell gibt, sagt die Zeile das ausdruecklich -- und rot, denn
                    # dann sind die Werte darunter nicht gedeutet.
                    self.env_identity = result[1]
                    if self.env_identity is None:
                        # Gefragt wurde, nur kam keine Kennung zurueck. Das ist etwas
                        # anderes als "noch nicht gefragt" und muss auch so dastehen,
                        # sonst haelt man eine alte Firmware fuer eine ungelesene.
                        self.env_identity_var.set(
                            "Environment: the device reported no id - this firmware "
                            "predates the identity line in showenv. The values below are read "
                            "with the model, without proof that it fits.")
                        self.env_identity_label_color(False)
                    else:
                        self.env_identity_var.set(self.env_identity_line())
                        # Rot heisst "den Werten unten ist nicht zu trauen", nicht "die
                        # Kennung ist neu". Eine Alt-Kennung, die die Firmware annimmt,
                        # ist eine Information -- faerbte man sie rot, gewoehnte man sich
                        # an eine rote Zeile, und die echte Warnung ginge darin unter.
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
        """Den Wert EINES Bitfelds als Anhaengsel fuer dessen eigene Zeile.

        Leerer String, wenn nichts gelesen wurde oder der Wert unlesbar ist - dann steht
        in der Zeile nur die Beschreibung, und niemand haelt eine 0 fuer eine Messung.
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
        """Kommando über den offenen Link schicken und auf die Antwort warten.

        Läuft im Worker-Thread. Die Antwort kommt über cmd_response_q, die nur
        gefüllt wird, solange cmd_pending gesetzt ist -- damit liest der Terminal-
        Konsument im Main-Thread nicht dieselben Chunks weg.
        """
        if not self.port_link:
            return "ERROR: Not connected"

        # Nur ein Kommando gleichzeitig, sonst mischen sich zwei Antworten.
        with self.cmd_lock:
            # Altbestand verwerfen, sonst landet die Antwort des Vorgängers hier.
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
                idle = 0

                while time.time() - start < timeout_ms / 1000.0:
                    try:
                        port, kind, payload = self.cmd_response_q.get(timeout=0.01)
                    except queue.Empty:
                        # Erst abbrechen, wenn schon etwas da war -- sonst wartet
                        # der erste Durchlauf gar nicht auf das Gerät.
                        idle += 1
                        if chunks and idle > 4:
                            break
                        continue

                    if kind != "data":
                        continue
                    chunks.append(payload.decode("latin-1", "ignore"))
                    idle = 0
                    text = "".join(chunks)
                    # Fertig, sobald der Marker da ist UND die Zeile abgeschlossen
                    # wurde -- ein "OK:" ohne Zeilenende ist erst der Anfang.
                    for marker in ("OK:", "ERROR"):
                        pos = text.find(marker)
                        if pos >= 0 and "\n" in text[pos:]:
                            return text

                return "".join(chunks)
            finally:
                self.cmd_pending.clear()

    # Bridge parameter methods
    def read_all_bridge(self):
        """Alle Bridge-Parameter mit einem showenv holen."""
        if not self.port_link:
            self.set_error_status("Not connected")
            messagebox.showwarning("Not connected", "Press Connect first.")
            return

        self.set_status("Reading bridge parameters...")

        def worker():
            output = self.send_command_via_link("showenv", timeout_ms=1500)
            # Erst die Kennung, und die Werte werden mit GENAU diesem Eintrag gedeutet.
            # Sonst zeigt die GUI gefuellte Felder unter einer Zeile, die sagt, das
            # Environment sei unbekannt -- beides aus derselben Antwort, und widerspruechlich.
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
        """Das ganze Environment schreiben: jedes gefuellte Feld, dann ins EEPROM.

        Zwei Sicherungen, die beide begruendet sind:

        Die Kennung wird vorher geprueft. Meldet das Geraet ein Environment, fuer das es
        hier kein Modell gibt, waeren die setenv-Schluessel geraten - dann wird nicht
        geschrieben. Wurde die Kennung noch gar nicht gelesen, holt diese Funktion sie
        selbst nach, statt anzunehmen, es werde schon passen.

        Und 'saveenv' ist die Vorgabe, nicht die Rueckfrage: wer "Write Environment"
        drueckt, will etwas, das den Reset uebersteht. Wer nur die RAM-Kopie aendern will,
        nimmt den Write-Knopf am einzelnen Feld.
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
            # Danach zurueckhalen, was wirklich drinsteht - eine Schreibbestaetigung ist
            # kein Beleg dafuer, dass das Geraet den Wert auch angenommen hat.
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
        """Feldname -> CLI-Kommando, beides aus dem Modell.

        Weder der Schluessel noch die Kommandoform stehen noch im Quelltext: 'commands.
        write_field' und 'cli_key' kommen aus env_model.json, damit eine andere Firmware-
        Variante nur eine andere Modelldatei braucht und keinen Patch hier.
        """
        env = self.env_entry()
        fld = env.get("fields", {}).get(key)
        if not fld:
            return None
        template = env.get("commands", {}).get("write_field", "setenv {cli_key} {value}")
        return template.format(cli_key=fld["cli_key"], value=value)

    def parse_showenv(self, output: str, key: str, entry: Optional[dict] = None) -> Optional[str]:
        """Einen Wert aus der showenv-Ausgabe ziehen -- mit dem Muster aus dem Modell.

        'entry' erlaubt es, den Modelleintrag mitzugeben, statt den aktuellen der GUI zu
        nehmen: der Worker hat die Kennung aus derselben Ausgabe schon aufgeloest.

        'reads_as' bildet die Anzeige des Geraets auf den Wert ab, den setenv erwartet
        (mirror meldet ON/OFF, geschrieben wird 1/0). Ohne das steht in der GUI ein Wort,
        das man nicht zurueckschreiben kann.
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
        """Alle Register des aktuellen Tabs lesen."""
        addrs = list(self.register_fields.keys())
        self.set_status(f"Reading {len(addrs)} registers...")

        def worker():
            if not self.port_link:
                self.set_error_status("Not connected")
                return

            failed = []
            for n, addr in enumerate(addrs, 1):
                output = self.send_command_via_link(f"lan_read {addr}")
                value = self.cli.parse_register_read(output)
                if not value:
                    m = re.search(r'Value=(0x[0-9A-Fa-f]+)', output)
                    value = m.group(1) if m else ""

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
        """Nur die gelesenen WERTE speichern. Das Modell kann die GUI nicht mehr anfassen.

        Frueher schrieb diese Funktion die ganze Registerkarte zurueck und hat sie dabei
        zweimal beschaedigt: einmal, weil sie sie aus den Widgets neu aufbaute (alles ohne
        Widget fiel raus), einmal ueber die Kodierung. Seit die Karte in lan8651_model.json
        liegt und hier nur {Adresse: Wert} landet, ist diese Fehlerklasse konstruktiv weg --
        eine Funktion, die eine Datei nicht schreibt, kann sie nicht kaputtmachen.
        """
        if not self.register_fields:
            messagebox.showwarning("Warning", "No register model loaded - nothing saved.")
            return

        values = {addr: var.get() for addr, var in self.register_fields.items() if var.get()}
        self.config["values"] = values
        self.config.pop("registers", None)  # Altlast aus der Zeit, als die Karte hier stand
        self.save_config()

        self.set_status(f"{len(values)} register values saved "
                        f"(model untouched)", duration=3000)

    def load_registers_json(self):
        """Gespeicherte Registerwerte zurueck in die Felder holen."""
        if not CONFIG_FILE.exists():
            messagebox.showwarning("Warning", "Config file not found")
            return

        cfg = json.load(open(CONFIG_FILE, encoding="utf-8"))
        n = 0
        for addr, val in (cfg.get("values") or {}).items():
            if addr in self.register_fields and val:
                self.register_fields[addr].set(str(val))
                n += 1

        # Aeltere bridge_config.json trugen die Werte noch im "registers"-Baum.
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
