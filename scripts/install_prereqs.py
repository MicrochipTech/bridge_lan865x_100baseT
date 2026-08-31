#!/usr/bin/env python3
"""Installer/preflight check for everything flash_same54.py needs.

Automatically installs whatever can be safely automated (pyocd via pip).
Also checks USB probe visibility and the local Microchip.SAME54_DFP pack,
printing clear, concrete next steps for anything missing -- a pack download
is deliberately NOT attempted automatically, since there is no confirmed
official direct-download URL for it (the pack is normally provided locally
via MPLAB X/MCC).

Usage:
    python install_prereqs.py            # check, then pick the probe
    python install_prereqs.py --install  # same, plus install missing pyocd via pip
    python install_prereqs.py --select   # only pick the probe, skip the checks

Every run ends with the probe question - Enter keeps the current choice. The answer is
stored in json/bench.json (see flash_same54.py's BENCH_PATH), keyed on the probe's
serial number, and read back by flash.bat, so a plain "flash.bat" always programs the
same board even when several are plugged in.
"""

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PYOCD_PIN = "0.43.0"  # verified on real hardware; 0.44.1 causes SWD connection issues

sys.path.insert(0, str(Path(__file__).parent))
# Same pack search, same bench.json handling as flash_same54.py - one implementation each.
from flash_same54 import BENCH_PATH, find_pack_dir, load_bench, save_bench


def check_python():
    ok = sys.version_info >= (3, 9)
    version = ".".join(str(p) for p in sys.version_info[:3])
    print(f"{'OK' if ok else 'MISSING'}: Python {version} ({'>= 3.9' if ok else 'Python 3.9+ required, see https://www.python.org/'})")
    return ok


def check_pyocd(install):
    try:
        import pyocd
        version = pyocd.__version__
        print(f"OK: pyocd {version} already installed")
        if version != PYOCD_PIN:
            print(
                f"     Note: verified with pyocd {PYOCD_PIN}. If you hit SWD problems right at\n"
                f"     connect (FAULT ACK immediately, not just on AP#2-4), consider switching back:\n"
                f"     pip install \"pyocd=={PYOCD_PIN}\""
            )
        return True
    except ImportError:
        pass

    if not install:
        print(f"MISSING: pyocd is not installed. Install it with: pip install \"pyocd=={PYOCD_PIN}\"")
        print("         Or run this tool again with --install.")
        return False

    print(f"Installing pyocd=={PYOCD_PIN} ...")
    rc = subprocess.call([sys.executable, "-m", "pip", "install", f"pyocd=={PYOCD_PIN}"])
    if rc != 0:
        print("ERROR: pip install pyocd failed (see output above)")
        return False
    print("OK: pyocd installed")
    return True


def get_probes():
    """All connected CMSIS-DAP probes, or None if pyocd is not installed."""
    try:
        from pyocd.core.helpers import ConnectHelper
    except ImportError:
        return None
    return ConnectHelper.get_all_connected_probes(blocking=False)


def check_probes(probes):
    if probes is None:
        print("SKIPPED: pyocd not installed, cannot detect probes")
        return False

    if not probes:
        print(
            "WARNING: no EDBG/nEDBG probe detected.\n"
            "         Connect the board via USB to the debug/programming port."
        )
        return False

    chosen = load_bench().get("selected")
    print(f"OK: {len(probes)} probe(s) detected:")
    for p in probes:
        mark = " <- flash.bat uses this one" if p.unique_id == chosen else ""
        print(f"     {p.unique_id}  {p.vendor_name} {p.product_name}{mark}")
    if chosen is None:
        print("     None recorded yet - run \"install.bat --select\" to pick one for flash.bat.")
    elif all(p.unique_id != chosen for p in probes):
        print(f"     NOTE: bench.json names {chosen}, which is not connected.")
        print("           Reconnect that board or run \"install.bat --select\" to pick another.")
    return True


def _ask(prompt, count, default):
    """One prompt for a 1..count choice. Returns the index, or None if it cannot be answered.

    Enter takes `default`; None as default means there is no safe assumption, so empty
    input is rejected and a non-interactive run gives up instead of guessing.
    """
    for _ in range(3):
        try:
            raw = input(prompt).strip()
        except EOFError:
            # Not being able to ask is not an error as long as a default exists - that
            # keeps setup.bat and scripted runs working. Without one it has to stay open.
            return default
        if raw == "":
            if default is not None:
                return default
            print(f"       Please enter a number between 1 and {count}.")
            continue
        if raw.isdigit() and 1 <= int(raw) <= count:
            return int(raw)
        print(f"       Please enter a number between 1 and {count}.")
    return default


def select_probe(probes):
    """Ask which probe flash.bat should program and record it in bench.json.

    Asked on every install.bat run, deliberately: on a bench with several boards the
    question "which one am I about to overwrite?" is the whole point, and a choice that
    is only made once silently ages. Enter keeps the current one, so re-running is cheap.
    """
    if not probes:
        print("SKIPPED: no probe connected, nothing to choose.")
        return False

    bench = load_bench()
    current = bench.get("selected")
    connected = [p.unique_id for p in probes]

    if current in connected:
        default = connected.index(current) + 1
    elif len(probes) == 1:
        default = 1
    else:
        default = None
    if current and current not in connected:
        print(f"NOTE: bench.json names {current}, which is not connected - please choose again.")

    print("Which probe should flash.bat program?")
    for i, p in enumerate(probes, 1):
        mark = "  <- current" if p.unique_id == current else ""
        print(f"  [{i}] {p.unique_id}  {p.vendor_name} {p.product_name}{mark}")

    hint = f"1-{len(probes)}" if default is None else f"1-{len(probes)}, Enter = {default}"
    index = _ask(f"Selection [{hint}]: ", len(probes), default)
    if index is None:
        print(
            "SKIPPED: no answer and no safe default (several probes, none recorded yet).\n"
            "         Run \"install.bat --select\" in a console, or write the serial into\n"
            f"         {BENCH_PATH.name} under \"selected\" yourself."
        )
        return False

    picked = probes[index - 1]
    if picked.unique_id == current:
        print(f"OK: unchanged - flash.bat keeps programming {current}.")
        return True
    entry = bench.setdefault("probes", {}).setdefault(picked.unique_id, {})
    entry.update({
        "vendor": picked.vendor_name,
        "product": picked.product_name,
        "seen": datetime.now().isoformat(timespec="seconds"),
    })
    entry.setdefault("label", picked.product_name)
    bench["version"] = bench.get("version", 1)
    bench["note"] = (
        "Keyed on the probe SERIAL. flash.bat reads \"selected\" and programs that board; "
        "run \"install.bat --select\" to change it. Machine-specific - do not expect it to "
        "mean anything on another bench."
    )
    bench["selected"] = picked.unique_id
    save_bench(bench)
    print(f"OK: {BENCH_PATH.name} written - flash.bat now programs {picked.unique_id}.")
    return True


def check_pack():
    pack_dir = find_pack_dir()
    if pack_dir is None:
        print(
            "MISSING: no Microchip.SAME54_DFP pack found locally.\n"
            "         This tool does NOT download it automatically (no confirmed official\n"
            "         direct-download URL). Two options:\n"
            "         1) Install MPLAB X (or just open it once) -- it downloads the pack\n"
            "            automatically into the local cache (~/.mchp_packs/Microchip/SAME54_DFP/...).\n"
            "         2) Download it manually from https://packs.download.microchip.com/ and\n"
            "            pass it explicitly with --pack <path> when flashing."
        )
        return False
    print(f"OK: Microchip.SAME54_DFP pack found: {pack_dir}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Check/install prerequisites for flash_same54.py")
    parser.add_argument("--install", action="store_true", help="Automatically install missing pyocd via pip")
    parser.add_argument("--select", action="store_true",
                        help="Only choose which connected probe flash.bat should program, then exit")
    args = parser.parse_args()

    if args.select:
        probes = get_probes()
        if probes is None:
            print("pyocd is not installed - run \"install.bat --install\" first.")
            sys.exit(1)
        sys.exit(0 if select_probe(probes) else 1)

    print("== Prerequisites for flash_same54.py ==\n")

    results = [check_python(), check_pyocd(args.install)]
    print()
    probes = get_probes()
    results.append(check_probes(probes))
    print()
    results.append(check_pack())
    print()

    # Always ask, with or without --install: which board flash.bat will overwrite is
    # the one thing on a multi-board bench that must not be assumed silently.
    if probes:
        select_probe(probes)
        print()

    if all(results):
        print("Everything is ready -- flash_same54.py can be used.")
        sys.exit(0)

    print("Something is still missing -- see the messages above.")
    sys.exit(1)


if __name__ == "__main__":
    main()
