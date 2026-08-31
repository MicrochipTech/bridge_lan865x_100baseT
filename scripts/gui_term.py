#!/usr/bin/env python3
"""
gui_term.py - three serial consoles in one window, one click connects all.

Ported from the t1s_ptp_bridge sibling project's scripts/gui_term.py, with the
probe-serial-number resolution dropped: that project keys its bench.json on a
debug probe's USB serial and re-resolves the COM port at every run, because its
boards get re-plugged into different USB hubs constantly. This repo assigns
COM ports directly instead - open "Setup > Configure Ports", pick a port from
the live list for each of the three terminal slots, give it a name, Save. The
same dialog's "Display" section sets the font size and text color for all
three panes at once. The assignment lives in term_ports.json (gitignored,
machine-specific, created by that dialog - do not hand-edit it) and takes
effect immediately, no restart.

    python gui_term.py                  three panes, not connected
    python gui_term.py --connect        connect all three on startup
    python gui_term.py --columns        panes side by side instead of stacked
    python gui_term.py --font-size 9    override term_ports.json for this run only
    python gui_term.py --selftest       no window: check the screen model

Nothing connects on its own except with --connect: click "Connect All", or the
button in a single pane's header to connect just that one. Click into a pane to
send keystrokes to it. The splitters between panes can be dragged.

Controls
--------

| Key / mouse              | Effect                                        |
|---------------------------|-----------------------------------------------|
| **Connect All**           | opens or closes all three ports at once       |
| button in a pane's header | connect/disconnect just that board            |
| click into a pane         | keyboard now goes to that board                |
| any key                   | goes out as a byte, the device echoes it       |
| arrow up/down, backspace  | the firmware's line editor (Harmony SYS_CMD)   |
| **Ctrl+C**                 | selection present -> copy, else the abort byte |
| **Ctrl+V**, right-click    | paste, one line at a time with a short pause   |
| Page Up / Page Down       | scrolls the view, is NOT sent to the device    |

No separate status window: whatever a connection has to report is a line in
ITS OWN pane (`[connected: COM8]`), and the live/dead state is the dot in that
pane's header. A message next to three terminals is always the wrong board's
message.

Why the terminal emulation is this small
-----------------------------------------

The firmware emits almost no escape sequences - Harmony's SYS_CMD line editor
redraws with backspace, overwrite and spaces. What needs handling is CR, LF,
BS, TAB and printable bytes; CSI sequences are discarded (`ESC[K` clears to
end of line). Bytes are mapped through latin-1, never UTF-8, so every one of
the 256 byte values is exactly one character and nothing is swallowed or
combined. This is not a VT100 - if firmware with ANSI colors ever shows up,
`pyte` is the right tool, not an extension of this one.

Who owns the byte stream
-------------------------

Tkinter is not thread-safe, and three ports need three continuous readers. So:
one reader thread per connection, dropping raw bytes into ONE queue; the main
thread drains it every 30 ms and draws. Only the main thread ever writes.
Opening a port also runs in a thread - a busy port runs into a timeout, and
the window would freeze in the main thread while that happens.
"""

import argparse
import ctypes
import json
import os
import queue
import sys
import threading
import winreg

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# term_ports.json lives in json/ (repo root), not next to this script - see the
# repo-wide "all JSON files live under json/" convention.
CONFIG = os.path.join(os.path.dirname(HERE), "json", "term_ports.json")

# Fixed order, not derived from the JSON - a slot with no port assigned yet
# still gets a pane, so "Setup > Configure Ports" has somewhere to point.
SLOT_KEYS = ["1", "2", "3"]

BAUD = 115200
POLL_MS = 30
BLINK_MS = 500
COLUMNS = 80
HEIGHT = 14               # lines per pane; three of them stacked
MAX_VIEW_LINES = 3000
PASTE_GAP_MS = 120        # one line per pause, else the firmware's line buffer overflows

BG = "#101010"
FG = "#d8d8d8"
HEAD_BG = "#1c1c1c"
DOT_ON = "#48b048"
DOT_OFF = "#b04848"

# Terminal text font size and color are configurable (Setup > Configure Ports,
# "Display" section) - these are just the fallback for a fresh term_ports.json
# or one written before that section existed. BG and the header colors stay
# fixed; only the requested "color of the terminal font" is a setting.
DEFAULT_FONT_SIZE = 10
DEFAULT_TEXT_COLOR = FG
MIN_FONT_SIZE = 6
MAX_FONT_SIZE = 24

try:
    import tkinter as tk
    from tkinter import ttk, scrolledtext, messagebox
except ImportError:                          # pragma: no cover - Tk missing only in odd builds
    tk = None

# Special keys mapped to the bytes a VT terminal would send.
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

CONTROL_BIT = 0x0004      # Ctrl in event.state, measured on Windows


# --------------------------------------------------------------- port list
def get_available_com_ports():
    """Currently plugged-in COM ports, as (device, probe_serial_or_None) pairs, for
    the config dialog's dropdowns - the serial (an EDBG probe's USB serial descriptor,
    shared between its CDC/COM and CMSIS-DAP functions) lets you tell boards apart
    without unplugging one to see which port disappears.

    Same approach as bridge_gui.py's helper of the same name - duplicated rather than
    imported, so this tool stays launchable on its own (see the module docstring: it
    depends on nothing but pyserial and term_ports.json).
    """
    try:
        from serial.tools import list_ports
        ports = [(p.device, p.serial_number) for p in list_ports.comports()]
        return sorted(ports)
    except ImportError:
        pass

    try:
        com_ports = []
        reg_path = r"HARDWARE\DEVICEMAP\SERIALCOMM"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path) as key:
            for i in range(winreg.QueryInfoKey(key)[1]):
                _name, value, _type = winreg.EnumValue(key, i)
                if value.startswith("COM"):
                    com_ports.append((value, None))
        return sorted(com_ports)
    except OSError:
        return []


# --------------------------------------------------------------- assignment
def load_config(path=CONFIG):
    """(slot_list, font_size, text_color) - everything term_ports.json holds.

    slot_list is [(key, name, port), ...] for the three terminal slots, in
    SLOT_KEYS order. A missing file is not an error: every slot comes back
    unnamed and without a port, and font_size/text_color come back as the
    DEFAULT_* constants - each pane says so, and "Setup > Configure Ports" is
    what creates the file, on the first Save.
    """
    slots = {}
    display = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        slots = cfg.get("slots") or {}
        display = cfg.get("display") or {}
    slot_list = []
    for key in SLOT_KEYS:
        info = slots.get(key) or {}
        slot_list.append((key, info.get("name") or "", info.get("port") or None))
    font_size = display.get("font_size") or DEFAULT_FONT_SIZE
    text_color = display.get("text_color") or DEFAULT_TEXT_COLOR
    return slot_list, font_size, text_color


def save_config(path, entries, font_size, text_color):
    """Write term_ports.json from [(key, name, port), ...] plus the display settings.

    Serialize to a neighbor file and os.replace() over the target, rather than
    truncating it in place - a crash mid-write leaves the old file intact
    instead of an empty one (see docs/FALLSTRICKE.md in the bridge_gui.py project
    for the incident that made this the habit here).
    """
    cfg = {
        "version": 1,
        "note": ("Port assignment and display settings for gui_term.py, set via "
                 "'Setup > Configure Ports' - ports are picked directly from the "
                 "currently available list, not resolved from a probe serial "
                 "number like the sibling project's bench.json. Machine-specific; "
                 "do not expect it to mean anything on another machine, and do "
                 "not hand-edit it, the dialog is the source of truth."),
        "display": {"font_size": font_size, "text_color": text_color},
        "slots": {key: {"name": name, "port": port} for key, name, port in entries},
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


# --------------------------------------------------------------- connection
class Link:
    """One serial connection plus its reader thread.

    The thread owns reading, the main thread owns writing - so nothing needs
    locking. close() sets the stop flag first: a ser.close() in the middle of
    a running read() raises, and without the flag that would be
    indistinguishable from a real disconnect.
    """

    def __init__(self, key, port, q, baud=BAUD):
        self.key = key
        self.port = port
        self.baud = baud
        self.q = q
        self.ser = None
        self.stop = threading.Event()
        self.thread = None

    def open(self):
        import serial                        # pyserial
        self.ser = serial.Serial(self.port, self.baud, timeout=0.05)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        while not self.stop.is_set():
            try:
                n = self.ser.in_waiting
                data = self.ser.read(n if n else 1)
            except (OSError, ValueError) as error:
                if not self.stop.is_set():
                    self.q.put((self.key, "lost", str(error)))
                return
            if data:
                self.q.put((self.key, "data", data))

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
            except OSError:
                pass
        self.ser = None


# --------------------------------------------------------------- screen
class Screen:
    """Bytes in, lines out. Not a VT100 - see the module docstring.

    Lines are NOT wrapped here - the Text widget does that with wrap='char'.
    That keeps the cursor's column a character position in the logical line,
    and wrapping costs no model state.
    """

    def __init__(self, max_lines=MAX_VIEW_LINES):
        self.max_lines = max_lines
        self.lines = []          # completed lines
        self.total = 0           # per completed line, including dropped ones
        self.cur = ""            # the unfinished line
        self.col = 0
        self._esc = None

    def feed(self, data):
        for b in data:
            if self._esc is not None:
                self._esc.append(b)
                if len(self._esc) == 1:
                    if b not in (0x5B, 0x4F):    # not a CSI/SS3 sequence
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
                self._put(chr(b))            # latin-1: one byte, one character
            # BEL, NUL, DEL and the rest of the control bytes are dropped

    def _sequence(self, seq):
        final = seq[-1:]
        body = seq[1:-1]
        if final == b"K":                    # clear to end of line
            if body in (b"", b"0"):
                self.cur = self.cur[:self.col]
            elif body == b"1":
                self.cur = " " * self.col + self.cur[self.col:]
            elif body == b"2":
                self.cur = ""
                self.col = 0
        # everything else is discarded: the firmware does not send it

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


# --------------------------------------------------------------- one pane
class Pane:
    """A header (state dot, title, connect button) and the terminal below it."""

    def __init__(self, parent, key, name, port, app, font, height, text_color):
        self.key = key
        self.name = name
        self.port = port
        self.app = app
        self.screen = Screen()
        self._seen_total = 0
        self._widget_lines = 0
        self._cursor_on = True

        self.frame = tk.Frame(parent, background=HEAD_BG)

        head = tk.Frame(self.frame, background=HEAD_BG)
        head.pack(side="top", fill="x")
        self.dot = tk.Label(head, text="●", foreground=DOT_OFF,
                            background=HEAD_BG, font=("Segoe UI", 9))
        self.dot.pack(side="left", padx=(6, 3))
        self.title_label = tk.Label(head, text=self._title(), foreground=FG,
                                    background=HEAD_BG, font=("Segoe UI", 9),
                                    anchor="w")
        self.title_label.pack(side="left", fill="x", expand=True)
        self.button = ttk.Button(head, text="connect", width=10,
                                 command=lambda: app.toggle_one(self.key))
        self.button.pack(side="right", padx=4, pady=2)

        self.text = scrolledtext.ScrolledText(
            self.frame, wrap="char", width=COLUMNS, height=height,
            font=font, background=BG, foreground=text_color,
            insertwidth=0,               # own block cursor instead of Tk's caret
            state="disabled")
        self.text.pack(side="top", fill="both", expand=True)
        self.text.tag_config("cursor", background=text_color, foreground=BG)

        self.text.bind("<Key>", self._on_key)
        self.text.bind("<Control-c>", self._on_ctrl_c)
        self.text.bind("<Control-v>", self._on_paste)
        self.text.bind("<<Paste>>", self._on_paste)
        self.text.bind("<<Cut>>", lambda _e: "break")
        self.text.bind("<Button-3>", self._on_paste)
        # Do NOT swallow Button-1, or mouse selection stops working.
        self.text.bind("<Button-1>", lambda _e: self.text.focus_set())
        self._render()

    def _title(self):
        return "%s  %s" % (self.port or "(no port)", self.name or "(unnamed)")

    def retitle(self, name, port):
        """Applied by Setup > Configure Ports without restarting the tool."""
        self.name = name
        self.port = port
        self.title_label.config(text=self._title())

    def apply_display(self, font, text_color):
        """Font size / text color from Setup > Configure Ports, live."""
        self.text.config(font=font, foreground=text_color)
        self.text.tag_config("cursor", background=text_color, foreground=BG)

    # -- output -------------------------------------------------------
    def feed(self, data):
        if not data:
            return
        self.screen.feed(data)
        self._render()

    def note(self, line):
        """A message into this pane itself, not a shared status window."""
        self.feed(("\r\n[%s]\r\n" % line).encode("latin-1", "replace"))

    def _render(self):
        added = self.screen.total - self._seen_total
        new_lines = []
        if added > 0:
            new_lines = (self.screen.lines[-added:]
                         if added <= len(self.screen.lines)
                         else list(self.screen.lines))

        # Only ever compute within the unfinished line, never up to "end": a
        # delete(..., "end") swallows the newline of the line before it.
        first = self._widget_lines + 1
        at_bottom = self.text.yview()[1] > 0.999
        self.text.config(state="normal")
        self.text.delete("%d.0" % first, "%d.end" % first)
        self.text.insert("%d.0" % first,
                         "".join(line + "\n" for line in new_lines)
                         + self.screen.cur + " ")
        self._widget_lines += len(new_lines)
        self._seen_total = self.screen.total

        if self._widget_lines > MAX_VIEW_LINES:
            drop = self._widget_lines - MAX_VIEW_LINES
            self.text.delete("1.0", "%d.0" % (drop + 1))
            self._widget_lines -= drop

        self._place_cursor()
        if at_bottom:                # whoever scrolled up wants to stay there
            self.text.see("end")
        self.text.config(state="disabled")

    def _place_cursor(self):
        self.text.tag_remove("cursor", "1.0", "end")
        if not self._cursor_on:
            return
        row = self._widget_lines + 1
        col = self.screen.col
        self.text.tag_add("cursor", "%d.%d" % (row, col), "%d.%d" % (row, col + 1))

    def blink(self):
        self._cursor_on = not self._cursor_on
        self.text.config(state="normal")
        self._place_cursor()
        self.text.config(state="disabled")

    def clear(self):
        self.screen = Screen()
        self._seen_total = 0
        self._widget_lines = 0
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.config(state="disabled")
        self._render()

    def set_state(self, connected):
        self.dot.config(foreground=DOT_ON if connected else DOT_OFF)
        self.button.config(text="disconnect" if connected else "connect")

    # -- input ----------------------------------------------------------
    def _on_key(self, event):
        if event.keysym in ("Prior", "Next"):
            self.text.yview_scroll(-1 if event.keysym == "Prior" else 1, "pages")
            return "break"
        if event.keysym in IGNORED_KEYSYMS:
            return "break"

        data = KEYSYM_BYTES.get(event.keysym)
        if data is None:
            if event.char:
                data = event.char.encode("latin-1", "ignore")
            elif (event.state & CONTROL_BIT) and len(event.keysym) == 1 \
                    and event.keysym.isalpha():
                # Checked via event.char only: AltGr also sets the Control bit
                # on Windows, and AltGr+Q is meant to send '@'.
                data = bytes([ord(event.keysym.lower()) & 0x1F])
        if data:
            self.app.send(self.key, data)
        return "break"

    def _on_ctrl_c(self, _event=None):
        if self.copy_selection():
            return "break"
        self.app.send(self.key, b"\x03")
        return "break"

    def copy_selection(self):
        try:
            selected = self.text.get("sel.first", "sel.last")
        except tk.TclError:
            return False
        if not selected:
            return False
        self.text.clipboard_clear()
        self.text.clipboard_append(selected)
        return True

    def _on_paste(self, _event=None):
        self.app.paste(self.key)
        return "break"


# --------------------------------------------------------------- config dialog
class ConfigDialog(tk.Toplevel):
    """Setup > Configure Ports: one group per terminal slot, each a name and a
    COM port chosen from a live list. Save writes term_ports.json and applies
    to the running window right away - no restart.
    """

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("Configure Ports")
        self.resizable(False, False)
        self.transient(app.root)
        self.grab_set()

        self.name_vars = {}
        self.port_vars = {}
        self.port_combos = {}

        current = {key: (name, port) for key, name, port in app.slot_list}

        for key in SLOT_KEYS:
            name, port = current.get(key, ("", None))
            grp = ttk.LabelFrame(self, text="Terminal %s" % key, padding=10)
            grp.pack(fill="x", padx=10, pady=(10, 0))

            row1 = ttk.Frame(grp)
            row1.pack(fill="x", pady=2)
            ttk.Label(row1, text="Name:", width=10).pack(side="left")
            name_var = tk.StringVar(value=name)
            self.name_vars[key] = name_var
            ttk.Entry(row1, textvariable=name_var, width=30).pack(side="left", padx=5)

            row2 = ttk.Frame(grp)
            row2.pack(fill="x", pady=2)
            ttk.Label(row2, text="COM Port:", width=10).pack(side="left")
            # No textvariable here: the combobox DISPLAYS "COM8   (ATML...)" but the
            # saved/used value must stay the bare port - see _on_port_selected().
            port_var = tk.StringVar(value=port or "")
            self.port_vars[key] = port_var
            combo = ttk.Combobox(row2, width=34, state="readonly")
            combo.pack(side="left", padx=5)
            combo.bind("<<ComboboxSelected>>", lambda e, k=key: self._on_port_selected(k))
            self.port_combos[key] = combo

        # Applies to all three panes at once, not per terminal - one font,
        # one text color for the whole window.
        disp_frame = ttk.LabelFrame(self, text="Display", padding=10)
        disp_frame.pack(fill="x", padx=10, pady=(10, 0))

        row = ttk.Frame(disp_frame)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="Font Size:", width=10).pack(side="left")
        self.font_size_var = tk.IntVar(value=app.font_size)
        ttk.Spinbox(row, from_=MIN_FONT_SIZE, to=MAX_FONT_SIZE,
                   textvariable=self.font_size_var, width=5).pack(side="left", padx=5)

        ttk.Label(row, text="Text Color:").pack(side="left", padx=(20, 5))
        self.text_color_var = tk.StringVar(value=app.text_color)
        self.color_swatch = tk.Label(row, text="      ", relief="sunken",
                                     background=app.text_color)
        self.color_swatch.pack(side="left", padx=(0, 5))
        ttk.Button(row, text="Choose...", command=self._pick_color).pack(side="left")

        btn_row = ttk.Frame(self)
        btn_row.pack(fill="x", padx=10, pady=10)
        ttk.Button(btn_row, text="Refresh Ports", command=self._refresh_ports).pack(side="left")
        ttk.Button(btn_row, text="Cancel", command=self.destroy).pack(side="right", padx=(5, 0))
        ttk.Button(btn_row, text="Save", command=self._save).pack(side="right")

        self._refresh_ports()

    @staticmethod
    def _format_port(port: str, probe_serial) -> str:
        return f"{port}   ({probe_serial})" if probe_serial else port

    def _on_port_selected(self, key):
        """Combobox has no textvariable (see __init__) - translate its displayed
        "COM8   (ATML...)" back to the bare port before it lands in port_vars, the
        thing _save() actually writes to term_ports.json."""
        display = self.port_combos[key].get()
        self.port_vars[key].set(display.split()[0] if display else "")

    def _refresh_ports(self):
        """Live ports (each with its probe's own serial, so a board is identifiable
        without unplugging it to see which port disappears) plus, per slot, whatever
        it already has - so reassigning one slot does not silently blank another
        slot's port just because that device happens to be unplugged right now."""
        available_info = get_available_com_ports()
        serial_by_port = dict(available_info)
        available = [device for device, _serial in available_info]
        for key in SLOT_KEYS:
            saved = self.port_vars[key].get()
            values = list(available)
            if saved and saved not in values:
                values.append(saved)
            combo = self.port_combos[key]
            combo["values"] = [""] + [self._format_port(p, serial_by_port.get(p)) for p in values]
            combo.set(self._format_port(saved, serial_by_port.get(saved)) if saved else "")

    def _pick_color(self):
        from tkinter import colorchooser
        _rgb, hex_color = colorchooser.askcolor(
            color=self.text_color_var.get() or DEFAULT_TEXT_COLOR,
            title="Terminal Text Color", parent=self)
        if hex_color:
            self.text_color_var.set(hex_color)
            self.color_swatch.config(background=hex_color)

    def _save(self):
        entries = []
        used_by = {}
        for key in SLOT_KEYS:
            name = self.name_vars[key].get().strip()
            port = self.port_vars[key].get().strip() or None
            if port and port in used_by:
                messagebox.showerror(
                    "Configure Ports",
                    "Terminal %s and Terminal %s both use %s. Two terminals "
                    "cannot share one serial port." % (used_by[port], key, port))
                return
            if port:
                used_by[port] = key
            entries.append((key, name, port))

        try:
            font_size = int(self.font_size_var.get())
        except (tk.TclError, ValueError):
            font_size = self.app.font_size
        font_size = max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, font_size))
        text_color = self.text_color_var.get().strip() or self.app.text_color

        save_config(self.app.config_path, entries, font_size, text_color)
        self.app.apply_slots(entries)
        self.app.apply_display(font_size, text_color)
        self.destroy()


# --------------------------------------------------------------- the window
class App:
    def __init__(self, root, config_path, slot_list, font_size=DEFAULT_FONT_SIZE,
                 text_color=DEFAULT_TEXT_COLOR, columns=False, height=HEIGHT):
        # Before the bar/buttons below are built: a style-level
        # ttk.Style().configure() applies to every button built afterwards,
        # so setting it this early avoids a resize/flash and survives
        # sv-ttk's own idle-task restyle later - see bridge_gui.py's
        # BridgeGUI._tighten_button_style for the same reasoning, and
        # docs/FALLSTRICKE.md (2026-08-26).
        self._tighten_button_style()
        self.root = root
        self.config_path = config_path
        self.q = queue.Queue()
        self.links = {}
        self.panes = {}
        self.slot_list = slot_list
        self.font_size = font_size
        self.text_color = text_color

        font = ("Consolas", font_size)

        # A native tk.Menu strip ignores its color options entirely on
        # Windows - confirmed on screen: background/foreground set on it and
        # on the "Setup" submenu had no visible effect at all. Not a
        # color-choice bug, a Tk-on-Windows limitation with no config-option
        # workaround; the strip is drawn by the OS's own themed menu bar
        # renderer, which Tk's Menu widget does not control. Built directly
        # as a ttk.Menubutton in its own bar instead, which sv-ttk does
        # style like any other ttk widget - the dropdown list that appears
        # on click is still a native tk.Menu popup (unavoidable, Tk has no
        # ttk-based dropdown menu), so it may still show with native
        # styling; far less noticeable than the always-visible strip.
        menu_bar = ttk.Frame(root)
        menu_bar.pack(side="top", fill="x")
        menu_button = ttk.Menubutton(menu_bar, text="Setup")
        menu_button.pack(side="left", padx=4, pady=2)
        setup_menu = tk.Menu(menu_button, tearoff=0)
        setup_menu.add_command(label="Configure Ports...", command=self.open_config_dialog)
        menu_button["menu"] = setup_menu

        bar = ttk.Frame(root)
        bar.pack(side="top", fill="x")
        self.all_button = ttk.Button(bar, text="Connect All", width=14,
                                     command=self.toggle_all)
        self.all_button.pack(side="left", padx=4, pady=4)
        ttk.Button(bar, text="Clear All", width=12,
                   command=self.clear_all).pack(side="left", padx=(0, 4), pady=4)

        # Stacked, in SLOT_KEYS order.
        self.panes_container = ttk.PanedWindow(root,
                                orient="horizontal" if columns else "vertical")
        self.panes_container.pack(side="top", fill="both", expand=True)
        for key, name, port in slot_list:
            pane = Pane(self.panes_container, key, name, port, self, font, height, text_color)
            self.panes_container.add(pane.frame, weight=1)
            self.panes[key] = pane
            if not port:
                pane.note("not configured - use Setup > Configure Ports")

        self._retitle_window()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(POLL_MS, self._tick)
        self.root.after(BLINK_MS, self._blink)
        first = slot_list[0][0] if slot_list else None
        if first:
            self.panes[first].text.focus_set()

        # Forces sv-ttk's own idle-task restyle to run NOW, before undoing
        # what it just did to the three panes built above - it reapplies its
        # palette via an idle task the first time the event loop turns, and
        # that pass only touches widgets that already existed before then
        # (a dialog opened later, e.g. Setup > Configure Ports, is
        # unaffected and needs no restoration - verified directly).
        self.root.update_idletasks()
        self._restore_pane_colors()
        self._replace_pane_scrollbars()
        self._apply_dark_titlebar(self.root, dark=self._is_dark())

    @staticmethod
    def _is_dark() -> bool:
        return ttk.Style().theme_use() == "sun-valley-dark"

    @staticmethod
    def _tighten_button_style() -> None:
        """sv-ttk's own TButton padding/font make buttons noticeably wider
        than the default ttk theme's - this bar was never at risk of
        overflowing on its own, this is purely for visual consistency with
        bridge_gui.py's tighter buttons."""
        style = ttk.Style()
        style.configure("TButton", padding=(4, 2), font=("Segoe UI", 9))

    def _restore_pane_colors(self) -> None:
        """sv-ttk's idle-task recolor pass overwrites each pane's terminal
        text color - which is the exact setting Setup > Configure Ports'
        "Display" section lets the user pick, so leaving it overwritten
        would silently undo that feature - and each pane's connect-state dot
        (DOT_ON/DOT_OFF), otherwise stuck showing the theme's plain text
        color regardless of whether that board is actually connected."""
        for key, pane in self.panes.items():
            pane.text.configure(foreground=self.text_color)
            pane.dot.configure(foreground=DOT_ON if self.connected(key) else DOT_OFF)

    def _replace_pane_scrollbars(self) -> None:
        """Each pane's scrolledtext.ScrolledText bundles its own plain
        tk.Scrollbar (see cpython's tkinter/scrolledtext.py - it builds one
        in its own __init__ and keeps it as .vbar). On Windows that renders
        as a native scrollbar control and ignores every Tk color option -
        unlike tk.Canvas/tk.Text, sv-ttk cannot recolor it either, confirmed
        by it staying visibly white after everything else went dark.
        Swapped for a ttk.Scrollbar bound to the same Text widget, which
        sv-ttk does reach."""
        for pane in self.panes.values():
            old = pane.text.vbar
            frame = old.master
            old.destroy()
            pane.text.pack_forget()
            new = ttk.Scrollbar(frame, orient="vertical", command=pane.text.yview)
            new.pack(side="right", fill="y")
            pane.text.configure(yscrollcommand=new.set)
            pane.text.pack(side="left", fill="both", expand=True)
            pane.text.vbar = new

    @staticmethod
    def _apply_dark_titlebar(window, dark: bool) -> None:
        """Color a native Windows title bar to match - sv-ttk (and ttk in
        general) only reaches ttk/tk widgets, the title bar is the OS's own
        window chrome and has no tkinter API at all. Windows 10 (2004+) and
        Windows 11 expose it through the DWM, called here directly via
        ctypes. GetParent() walks up from the embedded child window Tk hands
        out to the real top-level HWND the title bar belongs to - skipping
        that step is why naive versions of this recipe silently do nothing.
        Setting the DWM attribute alone measurably succeeds (returns S_OK)
        but does not visibly repaint the title bar on its own; SetWindowPos
        with SWP_FRAMECHANGED forces that repaint now. Takes `window` rather
        than always using self.root because the Setup dialog is its own
        top-level window with its own title bar."""
        try:
            hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
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

    # -- setup dialog -------------------------------------------------
    def open_config_dialog(self):
        """Opens well after startup, past sv-ttk's one-time recolor pass -
        its ttk widgets pick up the theme normally and its raw tk widgets
        (the color swatch) keep whatever color they were given, no
        restoration needed. Only its own native title bar needs the same
        treatment as the main window - it is a separate top-level window
        with a title bar of its own."""
        dlg = ConfigDialog(self)
        dlg.update_idletasks()
        self._apply_dark_titlebar(dlg, dark=self._is_dark())
        return dlg

    def apply_slots(self, entries):
        """Re-point the panes at a fresh (key, name, port) list, live.

        A pane whose port just changed is disconnected first - otherwise it
        would keep silently talking to whatever used to be on that port.
        """
        self.slot_list = entries
        for key, name, port in entries:
            pane = self.panes.get(key)
            if pane is None:
                continue
            if self.connected(key) and self.links[key].port != port:
                self._close_one(key)
                pane.note("reconfigured - was connected, disconnected")
            pane.retitle(name, port)
        self._retitle_window()

    def apply_display(self, font_size, text_color):
        """Font size / text color from Setup > Configure Ports, live."""
        self.font_size = font_size
        self.text_color = text_color
        font = ("Consolas", font_size)
        for pane in self.panes.values():
            pane.apply_display(font, text_color)

    # -- connect --------------------------------------------------------
    def connected(self, key):
        return key in self.links

    def toggle_all(self):
        if self.links:
            for key in list(self.links):
                self._close_one(key)
        else:
            self.connect(list(self.panes))
        self._retitle_window()

    def toggle_one(self, key):
        if self.connected(key):
            self._close_one(key)
            self._retitle_window()
        else:
            self.connect([key])

    def connect(self, keys):
        """Open ports in their own thread - a busy port runs into a timeout,
        and the window would freeze in the main thread while that happens."""
        ports_by_key = {key: port for key, _name, port in self.slot_list}
        todo = []
        for key in keys:
            if self.connected(key):
                continue
            port = ports_by_key.get(key)
            if not port:
                self.panes[key].note("no port assigned")
                continue
            todo.append((key, port))
        if not todo:
            return
        for key, port in todo:
            self.panes[key].note("connecting %s ..." % port)
        threading.Thread(target=self._open_many, args=(todo,),
                         daemon=True).start()

    def _open_many(self, todo):
        for key, port in todo:
            link = Link(key, port, self.q)
            try:
                link.open()
            except OSError as error:
                self.q.put((key, "failed", "%s: %s" % (port, error)))
                continue
            self.q.put((key, "opened", link))

    def _close_one(self, key):
        link = self.links.pop(key, None)
        if link is None:
            return
        link.close()
        self.panes[key].set_state(False)
        self.panes[key].note("disconnected: %s" % link.port)

    # -- bytes ------------------------------------------------------------
    def send(self, key, data):
        link = self.links.get(key)
        if link is None:
            self.panes[key].note("not connected")
            return
        try:
            link.write(data)
        except OSError as error:
            self.panes[key].note("send failed: %s" % error)

    def paste(self, key):
        """One line at a time with a pause - a burst all at once overflows
        the firmware's line buffer."""
        if not self.connected(key):
            self.panes[key].note("not connected")
            return
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        if not lines:
            return
        if len(lines) == 1:
            self.send(key, lines[0].encode("latin-1", "ignore"))
            return
        self._paste_next(key, lines)

    def _paste_next(self, key, lines):
        if not lines or not self.connected(key):
            return
        self.send(key, lines[0].encode("latin-1", "ignore") + b"\r\n")
        if len(lines) > 1:
            self.root.after(PASTE_GAP_MS, self._paste_next, key, lines[1:])

    def clear_all(self):
        for pane in self.panes.values():
            pane.clear()

    # -- loop ---------------------------------------------------------
    def _tick(self):
        for _ in range(400):                 # cap: the window stays responsive
            try:
                key, kind, payload = self.q.get_nowait()
            except queue.Empty:
                break
            pane = self.panes.get(key)
            if pane is None:
                continue
            if kind == "data":
                pane.feed(payload)
            elif kind == "opened":
                self.links[key] = payload
                pane.set_state(True)
                pane.note("connected: %s" % payload.port)
                self.send(key, b"\r\n")      # nudge the prompt, like cli.py does
                self._retitle_window()
            elif kind == "failed":
                pane.note(payload)
            elif kind == "lost":
                link = self.links.pop(key, None)
                if link is not None:
                    link.close()
                pane.set_state(False)
                pane.note("connection lost: %s" % payload)
                self._retitle_window()
        self.root.after(POLL_MS, self._tick)

    def _blink(self):
        for pane in self.panes.values():
            pane.blink()
        self.root.after(BLINK_MS, self._blink)

    def _retitle_window(self):
        n = len(self.links)
        self.all_button.config(text="Disconnect All" if n else "Connect All")
        self.root.title("T1S Bridge Terminals - " + " | ".join(
            "%s %s" % (name or "(unnamed)", port or "?")
            for _key, name, port in self.slot_list))

    def close(self):
        for key in list(self.links):
            self._close_one(key)
        self.root.destroy()


# --------------------------------------------------------------- self-test
def selftest():
    """The screen model and loading the assignment - no window."""
    fails = []

    def check(label, got, want):
        if got != want:
            fails.append("%s: %r != %r" % (label, got, want))

    s = Screen()
    s.feed(b"hello\r\n")
    check("line finished", (s.lines, s.cur, s.col), (["hello"], "", 0))

    s = Screen()
    s.feed(b"abc\x08\x08X")                  # backspace overwrites
    check("backspace", s.cur, "aXc")

    s = Screen()
    s.feed(b"abc\x08 \x08")                  # this is how SYS_CMD erases one character
    check("erase sequence", (s.cur, s.col), ("ab ", 2))

    s = Screen()
    s.feed(b"> stats\rX")                    # CR without LF: column 0
    check("CR", s.cur, "X stats")

    s = Screen()
    s.feed(b"long\x1b[K")
    check("CSI K at end", s.cur, "long")
    s = Screen()
    s.feed(b"longer text\r\x1b[K")
    check("CSI K from column 0", s.cur, "")

    s = Screen()
    s.feed(b"\x1b[A\x1b[Bfoo")               # arrow keys are discarded
    check("CSI discarded", s.cur, "foo")

    s = Screen()
    s.feed(b"a\tb")
    check("tab", s.cur, "a       b")

    s = Screen(max_lines=2)
    s.feed(b"1\n2\n3\n4\n")
    check("scrollback", (s.lines, s.total), (["3", "4"], 4))

    s = Screen()
    s.feed(bytes([0x07, 0x00, 0x7F]) + b"x")
    check("control bytes dropped", s.cur, "x")

    s = Screen()
    s.feed(b"ab\n")
    s.feed(b"cd")
    check("text() across both", s.text(), "ab\ncd")

    try:
        slots, font_size, text_color = load_config(CONFIG)
    except (OSError, ValueError) as error:
        fails.append("term_ports.json: %s" % error)
        slots, font_size, text_color = [], DEFAULT_FONT_SIZE, DEFAULT_TEXT_COLOR
    if slots:
        keys = [s[0] for s in slots]
        if keys != SLOT_KEYS:
            fails.append("slot order %r != %r" % (keys, SLOT_KEYS))
        print("   assignment: " + ", ".join(
            "%s=%s(%s)" % (k, n or "-", p or "-") for k, n, p in slots))
        print("   display: font_size=%s text_color=%s" % (font_size, text_color))

    for line in fails:
        print("FAIL " + line)
    print("gui_term selftest: %d checks, %d failure(s)"
          % (11 + len(slots), len(fails)))
    return 1 if fails else 0


# --------------------------------------------------------------- entry point
def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=CONFIG,
                    help="port assignment file (default: json/term_ports.json in the repo root)")
    ap.add_argument("--connect", action="store_true",
                    help="open all three ports right at startup (default: no)")
    ap.add_argument("--columns", action="store_true",
                    help="panes side by side instead of stacked")
    ap.add_argument("--font-size", type=int, default=None,
                    help="override the font size from term_ports.json for this run only")
    ap.add_argument("--height", type=int, default=HEIGHT,
                    help="lines per pane at startup")
    ap.add_argument("--selftest", action="store_true",
                    help="no window: check the screen model and the assignment")
    ap.add_argument("--light", action="store_true", help="use the light variant instead of dark")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if tk is None:
        print("Tkinter is missing from this Python.")
        return 2

    import dep_check
    if not dep_check.ensure_dependencies(
            hard=[("sv_ttk", "sv-ttk")], optional=[("serial", "pyserial")]):
        return 0
    import sv_ttk

    slot_list, font_size, text_color = load_config(args.config)
    if args.font_size is not None:
        font_size = args.font_size

    root = tk.Tk()
    sv_ttk.set_theme("light" if args.light else "dark")
    app = App(root, args.config, slot_list, font_size=font_size, text_color=text_color,
              columns=args.columns, height=args.height)
    if args.connect:
        app.connect(list(app.panes))
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
