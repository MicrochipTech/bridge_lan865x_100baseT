r"""
build_summary.py — Post-build memory and interrupt summary for the
                   T1S<->100BASE-TX bridge firmware.

Ported from a related in-house project (2026-09-01), unchanged apart from
the output image filename prefix.

Reads:
  * memoryfile.xml  — total flash / RAM from the linker (MPLAB X writes this
                       next to the .hex/.elf; it's what drives the IDE's own
                       memory-usage gauge)
  * *.map           — linker map (heap size, stack size, section details)
  * default.elf     — via xc32-nm (active interrupt handlers)

Prints a concise human-readable summary to stdout.

Usage (called by build.bat after a successful build):
    python build_summary.py <DIST_DIR> <ELF_PATH> <XC32_BIN_DIR>
"""

import datetime
import glob
import io
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zlib

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _kib(n: int) -> str:
    return f"{n / 1024:.1f} KiB"


def _pct(used: int, total: int) -> str:
    return f"{100 * used / total:.1f}%" if total else "?%"


def _bar(used: int, total: int, width: int = 30) -> str:
    filled = round(width * used / total) if total else 0
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _find_one(base_dir: str, filename_glob: str) -> str | None:
    """Search base_dir and its parent tree (MPLAB X splits dist/ and build/)
    for a file matching filename_glob. Returns the first match, or None."""
    candidates = glob.glob(os.path.join(base_dir, "**", filename_glob), recursive=True)
    if not candidates:
        parent = os.path.dirname(base_dir.rstrip("\\/"))
        # Also check the sibling build/ tree (MPLAB X: dist/ has the image,
        # build/ has the intermediate objects + map + memoryfile.xml).
        sibling = os.path.join(parent, "..", "build")
        candidates = glob.glob(os.path.join(sibling, "**", filename_glob), recursive=True)
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# 1. Flash / RAM from memoryfile.xml
# ---------------------------------------------------------------------------

def read_memory_xml(build_dir: str) -> dict:
    path = _find_one(build_dir, "memoryfile.xml")
    result = {}
    if not path or not os.path.isfile(path):
        return result
    tree = ET.parse(path)
    for mem in tree.getroot().iter("memory"):
        name  = mem.get("name", "").strip()
        used  = int(mem.findtext("used",   "0"))
        total = int(mem.findtext("length", "0"))
        free  = int(mem.findtext("free",   "0"))
        result[name] = {"used": used, "total": total, "free": free}
    return result


# ---------------------------------------------------------------------------
# 2. Heap / Stack from the linker .map file
# ---------------------------------------------------------------------------

def read_map_file(build_dir: str) -> dict:
    path = _find_one(build_dir, "*.map")
    result = {"heap": None, "stack": None, "bss_sections": []}
    if not path or not os.path.isfile(path):
        return result

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    # _min_heap_size / _min_stack_size are linker script assigns:
    #   0x0000afa0     _min_heap_size = 0xafa0
    for m in re.finditer(r'_min_heap_size\s*=\s*(0x[0-9a-fA-F]+)', content):
        result["heap"] = int(m.group(1), 16)
    for m in re.finditer(r'_min_stack_size\s*=\s*(0x[0-9a-fA-F]+)', content):
        result["stack"] = int(m.group(1), 16)

    # Text / data / bss totals from the section summary block at the top:
    # .text  0x00000000   0x20431  ...
    # .bss   0x20000180   0x3de0   ...
    for m in re.finditer(r'^(\.text|\.data|\.bss)\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)',
                         content, re.MULTILINE):
        result["bss_sections"].append({
            "name": m.group(1),
            "addr": int(m.group(2), 16),
            "size": int(m.group(3), 16),
        })

    return result


# ---------------------------------------------------------------------------
# 3. Active interrupts from xc32-nm
# ---------------------------------------------------------------------------

# Well-known placeholder / non-interrupt symbols to exclude
_EXCLUDE_RE = re.compile(
    r'^(Dummy_Handler|_EventHandlerSPI|CommandPingHandler|'
    r'DRV_SPI_TransferEventHandlerSet|pktEth0Handler|pktEth1Handler|'
    r'TCPIP_ARP_Handler|TCPIP_STACK_PacketHandler|'
    r'TCPIP_TCP_SignalHandler|TCPIP_UDP_SignalHandler|'
    r'TCPIPStack|F_TCPIP|F_DNS|F_Iperf|TCPIP_IPV4)',
    re.IGNORECASE,
)

# ARM/SAME54 core (non-peripheral) interrupt handler names
_CORE_INTERRUPTS = {
    "Reset_Handler", "NMI_Handler", "NonMaskableInt_Handler",
    "HardFault_Handler", "MemoryManagement_Handler",
    "BusFault_Handler", "UsageFault_Handler",
    "SVCall_Handler", "SVC_Handler",
    "DebugMonitor_Handler", "PendSV_Handler", "SysTick_Handler",
}

# Suffix patterns that identify hardware interrupt handlers
_IRQ_NAME_RE = re.compile(
    r'(?:_?InterruptHandler|_Handler)$'
)


def read_active_interrupts(elf_path: str, xc32_bin: str) -> tuple[list, list]:
    """
    Returns (core_irqs, peripheral_irqs) — lists of handler name strings.
    Only non-weak (T/t) and known active weak (W but not Dummy_Handler address)
    symbols are included.
    """
    nm_exe = os.path.join(xc32_bin, "xc32-nm.exe") if xc32_bin else ""
    if not nm_exe or not os.path.isfile(nm_exe):
        return [], []

    try:
        out = subprocess.check_output(
            [nm_exe, "--defined-only", elf_path],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        out = e.output or ""

    # Address of Dummy_Handler — weak handlers redirected there are "not implemented"
    dummy_addr = None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "Dummy_Handler":
            dummy_addr = parts[0].lstrip("0") or "0"
            break

    core_irqs = []
    peripheral_irqs = []

    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        addr_str, sym_type, name = parts[0], parts[1], parts[2]

        # Must look like a hardware/core handler
        if not _IRQ_NAME_RE.search(name):
            continue

        # Only process globally-visible symbols (T/W); skip file-local (t/w)
        if sym_type not in ("T", "W"):
            continue

        # Skip non-handler utility functions caught by the suffix pattern
        if _EXCLUDE_RE.match(name):
            continue

        # Weak symbol pointing to Dummy_Handler → not implemented
        if sym_type.upper() == "W":
            canon_addr = addr_str.lstrip("0") or "0"
            if dummy_addr and canon_addr == dummy_addr:
                continue  # Default weak alias, not active

        # Strip handler suffix to get the peripheral name
        irq_name = re.sub(r'(_?InterruptHandler|_Handler)$', '', name)

        if name in _CORE_INTERRUPTS:
            core_irqs.append(irq_name)
        else:
            peripheral_irqs.append(irq_name)

    return sorted(set(core_irqs)), sorted(set(peripheral_irqs))


# ---------------------------------------------------------------------------
# 4. Build timestamp from ELF binary
# ---------------------------------------------------------------------------

def read_build_timestamp(elf_path: str) -> tuple[str, str]:
    """
    Scans the ELF binary for the embedded build-time string written by:
        SYS_CONSOLE_PRINT("Build Timestamp: " __DATE__ " " __TIME__ "\\n\\r");
    Returns (human_str, tag_str) e.g. ("Apr  8 2026 14:44:18", "20260408_144418")
    or ("", "") if not found.
    """
    try:
        with open(elf_path, "rb") as f:
            data = f.read()
    except OSError:
        return "", ""

    m = re.search(
        rb'Build Timestamp: ([A-Za-z]{3} [ \d]\d \d{4} \d{2}:\d{2}:\d{2})',
        data,
    )
    if not m:
        return "", ""

    ts_str = m.group(1).decode("ascii", errors="replace")
    try:
        ts_norm = re.sub(r"\s+", " ", ts_str.strip())
        dt = datetime.datetime.strptime(ts_norm, "%b %d %Y %H:%M:%S")
        ts_tag = dt.strftime("%Y%m%d_%H%M%S")
    except ValueError:
        ts_tag = ts_str.replace(" ", "_").replace(":", "")

    return ts_str, ts_tag


# ---------------------------------------------------------------------------
# 5. Format / print summary
# ---------------------------------------------------------------------------

BOOTLOAD_MAX_IMAGE = 0x7C000   # one flash bank minus the emulated-EEPROM window


def read_bootload_image(hex_path: str) -> dict:
    """Size and CRC32 of the flat image the network bootloader would send.

    Not the same number as the linker's "program memory used": that counts only
    the bytes in sections, this is the whole span from 0 to the highest code
    address, gaps filled with 0xFF, which is what actually gets programmed page
    by page - and what scripts/bootload.py announces in 'bootload arm'. Records
    outside the image window (the NVM user page at 0x00804000) are excluded, as
    both update paths leave those alone.

    The ceiling is real, not advisory: with dual-bank updates the firmware must
    fit in one bank below the environment hand-over region, so an image at or
    above BOOTLOAD_MAX_IMAGE can no longer be updated over the network.
    """
    if not os.path.isfile(hex_path):
        return {}
    chunks, base, top = {}, 0, 0
    with open(hex_path) as fh:
        for raw in fh:
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
                if addr + count <= BOOTLOAD_MAX_IMAGE:
                    chunks[addr] = payload
                    top = max(top, addr + count)
            elif rtype == 0x01:
                break
    if top == 0:
        return {}
    data = bytearray(b"\xFF" * top)
    for addr, payload in chunks.items():
        data[addr:addr + len(payload)] = payload
    return {"size": top, "crc": zlib.crc32(bytes(data)) & 0xFFFFFFFF,
            "limit": BOOTLOAD_MAX_IMAGE}


def print_summary(mem: dict, map_info: dict, core_irqs: list, periph_irqs: list,
                  build_ts: str = "", image: dict | None = None) -> str:
    """Prints the build summary to stdout and returns it as a string."""
    buf = io.StringIO()

    def out(s: str = "") -> None:
        print(s)
        buf.write(s + "\n")

    SEP = "=" * 62
    out()
    out(SEP)
    out("  BUILD SUMMARY")
    out(SEP)

    # --- Build timestamp ---
    if build_ts:
        out()
        out(f"  Build      : {build_ts}")

    # --- Flash ---
    if "program" in mem:
        m = mem["program"]
        used, total, free = m["used"], m["total"], m["free"]
        pct = _pct(used, total)
        out()
        out(f"  Flash (program memory)")
        out(f"    Used   : {used:>8,} bytes  ({_kib(used):>10})  {pct}")
        out(f"    Free   : {free:>8,} bytes  ({_kib(free):>10})")
        out(f"    Total  : {total:>8,} bytes  ({_kib(total):>10})")
        out(f"    {_bar(used, total)}")

    # --- Network-update image ---
    if image:
        size, limit = image["size"], image["limit"]
        out()
        out(f"  Bootload image (what 'bootload arm' would announce)")
        out(f"    Size   : {size:>8,} bytes  ({_kib(size):>10})  {_pct(size, limit)} of one bank")
        out(f"    CRC32  : 0x{image['crc']:08X}")
        if size >= limit:
            out(f"    *** TOO BIG for a dual-bank update: the limit is {limit:,} bytes")
            out(f"        (one 512 KiB bank minus the 16 KiB environment region)")
        out(f"    {_bar(size, limit)}")

    # --- RAM ---
    # memoryfile.xml's "data" region is .data+.bss only (statically-allocated
    # globals) - the same number the IDE's own memory gauge shows. It does
    # NOT include the heap/stack reservation carved out of the same RAM by
    # the linker script, so on its own it understates how much RAM is
    # actually committed. Shown here for reference, then combined with the
    # heap reservation below for the realistic total.
    heap = map_info.get("heap")
    stack = map_info.get("stack")
    if "data" in mem:
        m = mem["data"]
        used, total, free = m["used"], m["total"], m["free"]
        pct = _pct(used, total)
        out()
        out(f"  RAM (data memory: .data + .bss only, excludes heap/stack)")
        out(f"    Used   : {used:>8,} bytes  ({_kib(used):>10})  {pct}")
        out(f"    Free   : {free:>8,} bytes  ({_kib(free):>10})")
        out(f"    Total  : {total:>8,} bytes  ({_kib(total):>10})")
        out(f"    {_bar(used, total)}")

    # --- Heap / Stack ---
    out()
    out("  Linker-Reserved Regions")
    if heap is not None:
        out(f"    Heap   : {heap:>8,} bytes  ({_kib(heap):>10})  (_min_heap_size)")
    else:
        out("    Heap   :       -- not found in map --")

    if stack is not None:
        out(f"    Stack  : {stack:>8,} bytes  ({_kib(stack):>10})  (_min_stack_size)")
    else:
        out("    Stack  :       -- not found in map --")

    # --- RAM including heap/stack reservation ---
    if "data" in mem and (heap is not None or stack is not None):
        m = mem["data"]
        total = m["total"]
        committed = m["used"] + (heap or 0) + (stack or 0)
        free = total - committed
        pct = _pct(committed, total)
        reserved_bits = []
        if heap is not None:
            reserved_bits.append("heap")
        if stack is not None:
            reserved_bits.append("stack")
        out()
        out(f"  RAM committed (.data + .bss + {' + '.join(reserved_bits)})")
        out(f"    Used   : {committed:>8,} bytes  ({_kib(committed):>10})  {pct}")
        out(f"    Free   : {free:>8,} bytes  ({_kib(free):>10})")
        out(f"    Total  : {total:>8,} bytes  ({_kib(total):>10})")
        out(f"    {_bar(committed, total)}")

    # --- Interrupts ---
    out()
    out("  Interrupt Handlers")
    core_str = ", ".join(core_irqs) if core_irqs else "none"
    out(f"    Core IRQs        ({len(core_irqs):2d}): {core_str}")

    out(f"    Peripheral IRQs  ({len(periph_irqs):2d}):")
    if periph_irqs:
        for name in periph_irqs:
            out(f"      - {name}")
    else:
        out("      none")

    out()
    out(SEP)
    out()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 6. Image output  (image/<project>_<timestamp>.hex + build_summary_<ts>.txt)
# ---------------------------------------------------------------------------

def write_image(elf_path: str, summary_text: str, ts_tag: str) -> None:
    """
    Creates <out_dir>/image/ and writes:
      - bridge_lan865x_100baseT_<ts_tag>.hex  (copy of the production .hex)
      - build_summary_<ts_tag>.txt            (summary text)
    """
    out_dir   = os.path.dirname(os.path.abspath(elf_path))
    image_dir = os.path.join(out_dir, "image")
    os.makedirs(image_dir, exist_ok=True)

    tag         = ts_tag if ts_tag else "unknown"
    hex_dst     = os.path.join(image_dir, f"bridge_lan865x_100baseT_{tag}.hex")
    summary_dst = os.path.join(image_dir, f"build_summary_{tag}.txt")

    hex_src = os.path.splitext(elf_path)[0] + ".hex"
    if os.path.isfile(hex_src):
        shutil.copy2(hex_src, hex_dst)
        print(f"  Image HEX  : {hex_dst}")
    else:
        print(f"  WARNING: HEX not found at {hex_src}, skipping copy.")

    with open(summary_dst, "w", encoding="utf-8") as f:
        f.write(summary_text)
    print(f"  Summary    : {summary_dst}")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) != 4:
        print("Usage: python build_summary.py <DIST_DIR> <ELF_PATH> <XC32_BIN_DIR>")
        sys.exit(1)

    build_dir = sys.argv[1]
    elf_path  = sys.argv[2]
    xc32_bin  = sys.argv[3]

    mem                    = read_memory_xml(build_dir)
    map_info               = read_map_file(build_dir)
    core_irqs, periph_irqs = read_active_interrupts(elf_path, xc32_bin)
    build_ts, ts_tag       = read_build_timestamp(elf_path)

    image = read_bootload_image(os.path.splitext(elf_path)[0] + ".hex")
    summary_text = print_summary(mem, map_info, core_irqs, periph_irqs, build_ts, image)
    write_image(elf_path, summary_text, ts_tag)


if __name__ == "__main__":
    main()
