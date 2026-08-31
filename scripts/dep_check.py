#!/usr/bin/env python3
"""Shared dependency check for the two GUI entry points, bridge_gui.py and
gui_term.py.

On a fresh checkout without `pip install -r scripts/requirements.txt`, sv-ttk (a hard
dependency of both tools - they always run themed, there is no plain-ttk
fallback anymore) would otherwise fail with an unconditional import at module
load - and run_term.bat launches via pythonw with no exit-code check, so that
import crash would produce no window and no error at all (see
docs/FALLSTRICKE.md, dated 2026-08-26, from when this first bit the separate
"_modern" builds that have since been merged into these two files). This
module runs BEFORE any of that: it checks with importlib.util.find_spec
(never a real import, so it cannot itself crash), and if something required
is missing, shows a small Tk dialog offering to run setup_venv.bat right
there, streaming its output.

A successful install always ends in "please restart" rather than trying to
pick the new package up in the running process: a failed `import serial` at
module scope elsewhere in the same file has already been evaluated and
cached as failed by the time this runs, so continuing in-process would leave
that stale even though the package is now on disk.

    ensure_dependencies(hard=[("sv_ttk", "sv-ttk")], optional=[("serial", "pyserial")])

Returns True if the caller should proceed, False if it should exit - the
caller still does its own sys.exit() so it can unwind cleanly in its own
context (before or after building its root window).
"""

import importlib.util
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import scrolledtext, ttk
from pathlib import Path

# This project has its own .venv (see CLAUDE.md section 2 / setup.bat) -
# sv-ttk/pyserial/pyocd normally end up there via setup.bat, so this dialog
# should rarely fire. Points at this project's own setup_venv.bat as the
# fallback repair path if it ever does.
INSTALL_SCRIPT = Path(__file__).resolve().parent.parent / "batch" / "setup_venv.bat"


def _missing(deps):
    return [(mod, pip_name) for mod, pip_name in deps if importlib.util.find_spec(mod) is None]


def ensure_dependencies(hard=(), optional=()):
    missing_hard = _missing(hard)
    missing_optional = _missing(optional)
    if not missing_hard and not missing_optional:
        return True

    root = tk.Tk()
    root.withdraw()
    result = {"proceed": False}
    _show_dialog(root, missing_hard, missing_optional, result)
    root.destroy()
    return result["proceed"]


def _show_dialog(root, missing_hard, missing_optional, result):
    all_missing = missing_hard + missing_optional
    names = ", ".join(pip_name for _, pip_name in all_missing)

    dlg = tk.Toplevel(root)
    dlg.title("Missing Dependencies")
    dlg.resizable(False, False)
    dlg.grab_set()

    intro = (
        "This tool cannot run without the following package(s):\n\n  " + names
        if missing_hard else
        "This tool needs the following package(s) for full functionality:\n\n  " + names
    )
    msg = ttk.Label(dlg, text=intro + "\n\nInstall now by running setup_venv.bat?",
                     justify="left", padding=12)
    msg.pack(fill="x")

    output = scrolledtext.ScrolledText(dlg, width=70, height=14, state="disabled")

    btns = ttk.Frame(dlg, padding=8)
    btns.pack(fill="x")

    def close(proceed):
        result["proceed"] = proceed
        dlg.destroy()

    def install():
        install_btn.configure(state="disabled")
        if continue_btn is not None:
            continue_btn.configure(state="disabled")
        exit_btn.configure(state="disabled")
        output.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        _run_install(dlg, output, on_done)

    def on_done(success):
        exit_btn.configure(state="normal")
        if success:
            msg.configure(text="Installed successfully. Please restart the tool.")
        else:
            msg.configure(text="Installation failed - see output above.")
            install_btn.configure(state="normal")
            if continue_btn is not None:
                continue_btn.configure(state="normal")

    install_btn = ttk.Button(btns, text="Install now", command=install)
    install_btn.pack(side="left")

    continue_btn = None
    if not missing_hard:
        continue_btn = ttk.Button(btns, text="Continue anyway", command=lambda: close(True))
        continue_btn.pack(side="left", padx=8)

    exit_btn = ttk.Button(btns, text="Exit", command=lambda: close(False))
    exit_btn.pack(side="right")

    dlg.protocol("WM_DELETE_WINDOW", lambda: close(False))
    dlg.update_idletasks()
    dlg.wait_window()


def _run_install(dlg, output_widget, on_done):
    q = queue.Queue()

    def worker():
        try:
            proc = subprocess.Popen(
                [str(INSTALL_SCRIPT)], cwd=str(INSTALL_SCRIPT.parent),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, shell=True)
            for line in proc.stdout:
                q.put(("line", line))
            proc.wait()
            q.put(("done", proc.returncode == 0))
        except OSError as error:
            q.put(("line", f"ERROR: {error}\n"))
            q.put(("done", False))

    threading.Thread(target=worker, daemon=True).start()

    def poll():
        try:
            while True:
                kind, payload = q.get_nowait()
                if kind == "line":
                    output_widget.configure(state="normal")
                    output_widget.insert("end", payload)
                    output_widget.see("end")
                    output_widget.configure(state="disabled")
                else:
                    on_done(payload)
                    return
        except queue.Empty:
            pass
        dlg.after(100, poll)

    dlg.after(100, poll)
