#!/usr/bin/env python3
r"""
faultlog_analyze.py - Explain a "faultlog"/boot-time crash report (crashlog.c)

Reads the report text from the clipboard (or --file), parses the register
and fault-status values crashlog.c prints, decodes every CFSR/HFSR bit it
sets in plain language, resolves PC and LR against the project's own ELF
with xc32-addr2line, and prints the actual source line each one points at.

Usage:
    python scripts\faultlog_analyze.py                  # reads the clipboard
    python scripts\faultlog_analyze.py --file report.txt
    python scripts\faultlog_analyze.py --elf path\to\other.elf

The ELF must be the exact build that produced the report (same rule the
report's own "Resolve PC with: xc32-addr2line ..." line already states) -
defaults to this project's current build output; pass --elf to point at an
archived one instead.
"""
import argparse
import json
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ELF = os.path.join(
    REPO_ROOT, "firmware", "tcpip_iperf_lan865x.X", "dist", "default",
    "production", "tcpip_iperf_lan865x.X.production.elf",
)
COMPILER_CONFIG = os.path.join(REPO_ROOT, "setup_compiler.config")


# --- CFSR/HFSR bit tables ---------------------------------------------------
# (bit, name, explanation). Grouped by sub-register exactly as CFSR packs
# them: MMFSR in bits 0-7, BFSR in bits 8-15, UFSR in bits 16-31.

MMFSR_BITS = [
    (0, "IACCVIOL", "Instruction fetch from a region the MPU marks execute-never or otherwise inaccessible."),
    (1, "DACCVIOL", "Data read/write to a region the MPU denies access to."),
    (3, "MUNSTKERR", "MemManage fault while unstacking registers on exception return."),
    (4, "MSTKERR", "MemManage fault while stacking registers on exception entry (e.g. a corrupted/overflowed stack pointer)."),
    (5, "MLSPERR", "MemManage fault during lazy FPU state preservation."),
]
MMFSR_VALID_BIT = 7  # MMARVALID

BFSR_BITS = [
    (0, "IBUSERR", "Bus fault on an instruction fetch."),
    (1, "PRECISERR", "Precise data bus error - the faulting instruction is exactly identified (BFAR below is meaningful if valid)."),
    (2, "IMPRECISERR", "Imprecise data bus error - a write was queued and failed asynchronously; the faulting address could not be captured, treat PC/LR here with caution."),
    (3, "UNSTKERR", "Bus fault while unstacking registers on exception return."),
    (4, "STKERR", "Bus fault while stacking registers on exception entry (e.g. a corrupted/overflowed stack pointer)."),
    (5, "LSPERR", "Bus fault during lazy FPU state preservation."),
]
BFSR_VALID_BIT = 7  # BFARVALID (bit 15 of CFSR, bit 7 of BFSR)

UFSR_BITS = [
    (0, "UNDEFINSTR", "The CPU tried to execute an instruction it does not recognize - a genuinely undefined opcode, a deliberate test (crashtest's \"udf\"), or execution that wandered into data/garbage."),
    (1, "INVSTATE", "Invalid execution state - most often a branch/call to an address with the Thumb bit (bit 0) clear, which Cortex-M cannot execute (there is no ARM mode)."),
    (2, "INVPC", "Invalid PC load, or an EXC_RETURN integrity check failed on exception return - often a corrupted stack or a stale/reused exception frame."),
    (3, "NOCP", "Attempted to use a coprocessor (commonly the FPU) that is not present or not enabled."),
    (8, "UNALIGNED", "An unaligned memory access was trapped (only possible if CCR.UNALIGN_TRP is set)."),
    (9, "DIVBYZERO", "An integer division by zero was trapped (only possible if CCR.DIV_0_TRP is set)."),
]

HFSR_BITS = [
    (1, "VECTTBL", "A bus fault occurred while reading the vector table itself for this exception - suggests a corrupted or misconfigured VTOR/vector table."),
    (30, "FORCED", "This HardFault was escalated from a MemManage/Bus/UsageFault that could not be taken directly (its own handler is disabled, or a fault of equal/higher priority was already active)."),
    (31, "DEBUGEVT", "A debug event occurred while the debug monitor/halting debug was disabled."),
]

FAULT_EXCEPTION_NUMBERS = {"HardFault": 3, "MemManageFault": 4, "BusFault": 5, "UsageFault": 6}


def decode_bits(value, table):
    return [(bit, name, expl) for bit, name, expl in table if value & (1 << bit)]


# --- Clipboard / input -------------------------------------------------------

def read_clipboard():
    try:
        import tkinter
    except ImportError:
        sys.exit("Cannot read the clipboard: tkinter is not available in this Python. "
                 "Use --file instead.")
    root = tkinter.Tk()
    root.withdraw()
    try:
        return root.clipboard_get()
    except tkinter.TclError:
        sys.exit("Clipboard is empty or does not contain text.")
    finally:
        root.destroy()


# --- Report parsing -----------------------------------------------------------

def find_hex(name, text):
    m = re.search(rf"\b{name}=0x([0-9A-Fa-f]+)", text)
    return int(m.group(1), 16) if m else None


def parse_report(text):
    fault = re.search(r"===\s*Last\s+(\w+)\s*===", text)
    fields = {
        "fault_name": fault.group(1) if fault else None,
        "pc": find_hex("PC", text),
        "lr": find_hex("LR", text),
        "psr": find_hex("PSR", text),
        "r0": find_hex("R0", text),
        "r1": find_hex("R1", text),
        "r2": find_hex("R2", text),
        "r3": find_hex("R3", text),
        "r12": find_hex("R12", text),
        "cfsr": find_hex("CFSR", text),
        "hfsr": find_hex("HFSR", text),
        "bfar": find_hex("BFAR", text),
        "mmfar": find_hex("MMFAR", text),
    }
    if fields["pc"] is None or fields["cfsr"] is None:
        sys.exit("Could not find PC=... and CFSR=... in the given text - "
                 "is this really a faultlog report? (paste the whole block, "
                 "from '=== Last ...' down to the '======' line)")
    return fields


# --- addr2line ----------------------------------------------------------------

def find_addr2line():
    candidates = []
    if os.path.isfile(COMPILER_CONFIG):
        try:
            with open(COMPILER_CONFIG, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            bin_dir = cfg.get("bin_dir")
            if bin_dir:
                candidates.append(os.path.join(bin_dir, "xc32-addr2line.exe"))
        except (json.JSONDecodeError, OSError):
            pass
    candidates.append(r"C:\Program Files\Microchip\xc32\v5.10\bin\xc32-addr2line.exe")
    candidates.append("xc32-addr2line")  # rely on PATH as a last resort
    for c in candidates:
        if c == "xc32-addr2line" or os.path.isfile(c):
            return c
    return None


def resolve(addr2line, elf, addr):
    """Returns (function, file, line) or (None, None, None) if unresolved."""
    if addr2line is None or addr is None:
        return None, None, None
    try:
        out = subprocess.run(
            [addr2line, "-f", "-C", "-e", elf, f"0x{addr:08X}"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"  (addr2line failed to run: {e})")
        return None, None, None
    lines = out.stdout.strip().splitlines()
    if len(lines) < 2:
        return None, None, None
    func = lines[0].strip()
    loc = lines[1].strip()
    if ":" not in loc or loc.startswith("??"):
        return func, None, None
    file_part, _, line_part = loc.rpartition(":")
    try:
        line_no = int(line_part)
    except ValueError:
        return func, None, None
    return func, file_part, line_no


def print_source_context(file_path, line_no, context=3):
    if not file_path or not os.path.isfile(file_path):
        print(f"    (source file not found on this machine: {file_path})")
        return
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    start = max(1, line_no - context)
    end = min(len(lines), line_no + context)
    for n in range(start, end + 1):
        marker = "->" if n == line_no else "  "
        print(f"    {marker} {n:5d} | {lines[n - 1].rstrip()}")


# --- Report -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", help="read the report from this file instead of the clipboard")
    ap.add_argument("--elf", default=DEFAULT_ELF, help="ELF that produced the report (default: this project's current build)")
    ap.add_argument("--context", type=int, default=3, help="source lines of context around PC/LR (default 3)")
    args = ap.parse_args()

    text = open(args.file, "r", encoding="utf-8", errors="replace").read() if args.file else read_clipboard()
    r = parse_report(text)

    print("=" * 78)
    print(f"Fault type : {r['fault_name'] or '(unknown - not found in text)'}")
    exc_num = FAULT_EXCEPTION_NUMBERS.get(r["fault_name"])
    if exc_num is not None:
        print(f"             (ARMv7-M exception number {exc_num})")
    print(f"PC         : 0x{r['pc']:08X}" if r["pc"] is not None else "PC         : (not found)")
    print(f"LR         : 0x{r['lr']:08X}" if r["lr"] is not None else "LR         : (not found)")
    if r["psr"] is not None:
        print(f"PSR        : 0x{r['psr']:08X}  (stacked - reflects the INTERRUPTED context, not the fault itself)")
    print()

    if not os.path.isfile(args.elf):
        print(f"ELF not found: {args.elf}")
        print("Pass --elf <path> to point at the exact build that produced this report.")
        addr2line = None
    else:
        addr2line = find_addr2line()
        if addr2line is None:
            print("xc32-addr2line not found (checked setup_compiler.config and the default XC32 install path).")

    if addr2line and os.path.isfile(args.elf):
        for label, addr in (("PC (fault site)", r["pc"]), ("LR (return address / caller)", r["lr"])):
            if addr is None:
                continue
            func, file_path, line_no = resolve(addr2line, args.elf, addr)
            print(f"{label}: 0x{addr:08X}")
            if func:
                print(f"  in {func}()")
            if file_path and line_no:
                print(f"  {file_path}:{line_no}")
                print_source_context(file_path, line_no, args.context)
            else:
                print("  (could not resolve to a source line - stale ELF, or address is not code)")
            print()

    print("-" * 78)
    print("Registers at fault time:")
    for name in ("r0", "r1", "r2", "r3", "r12"):
        v = r[name]
        if v is not None:
            print(f"  {name.upper():<4}= 0x{v:08X}  ({v})")
    print()

    print("-" * 78)
    cfsr = r["cfsr"] or 0
    hfsr = r["hfsr"] or 0
    print(f"CFSR = 0x{cfsr:08X}")
    mmfsr = cfsr & 0xFF
    bfsr = (cfsr >> 8) & 0xFF
    ufsr = (cfsr >> 16) & 0xFFFF
    groups = [
        ("MemManage sub-status (CFSR bits 0-7)", mmfsr, MMFSR_BITS, MMFSR_VALID_BIT, "mmfar", "MMFAR"),
        ("BusFault sub-status (CFSR bits 8-15)", bfsr, BFSR_BITS, BFSR_VALID_BIT, "bfar", "BFAR"),
        ("UsageFault sub-status (CFSR bits 16-31)", ufsr, UFSR_BITS, None, None, None),
    ]
    any_decoded = False
    for title, sub, table, valid_bit, addr_key, addr_label in groups:
        hits = decode_bits(sub, table)
        if not hits:
            continue
        any_decoded = True
        print(f"\n  {title}:")
        for bit, name, expl in hits:
            print(f"    [{name}] {expl}")
        if valid_bit is not None and (sub & (1 << valid_bit)):
            addr = r.get(addr_key)
            if addr is not None:
                print(f"    -> {addr_label} is valid: 0x{addr:08X} (the actual faulting data address)")
    if not any_decoded:
        print("  No CFSR sub-status bits set (fault status was not captured, or this is not a Mem/Bus/UsageFault).")

    print(f"\nHFSR = 0x{hfsr:08X}")
    for bit, name, expl in decode_bits(hfsr, HFSR_BITS):
        print(f"  [{name}] {expl}")

    print()
    print("=" * 78)


if __name__ == "__main__":
    main()
