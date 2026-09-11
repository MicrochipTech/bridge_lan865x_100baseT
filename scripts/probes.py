#!/usr/bin/env python3
"""List the debug probes connected to this PC - and, on request, the chip behind each.

Shows every CMSIS-DAP probe pyOCD sees over USB with its serial, its COM port (the EDBG
console of the same board), which one json\\bench.json selects (the probe flash.bat and
fuses.bat use when no --probe is given), and which board it is wired to according to
json\\boards\\<board>.json. That much needs USB only: no SWD connection is made, the boards
are not touched.

--chip additionally attaches to each probe's target without halting it and reads the DSU
device ID - named from the ATDFs in the Microchip.SAME54_DFP pack - and, for a device from
that pack, the 128-bit serial number fuses.bat binds its backups to. The firmware keeps
running.

The board files also hold each board's private key. Only board_id, ip and probe_serial are
read from them, and nothing else from them is ever printed.
"""

import argparse
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import bridge_core                # noqa: E402  - COM port per probe serial, one implementation
import flash_same54 as fs         # noqa: E402  - bench.json, pack lookup, default target
import fuse_programmer as fp      # noqa: E402  - chip serial number location
import fuses_same54 as fuses      # noqa: E402  - ATDF signatures, read-only pyOCD session

REPO_ROOT = Path(__file__).parent.parent
BOARDS_DIR = REPO_ROOT / "json" / "boards"

DESCRIPTION = """\
List the debug probes connected to this PC.

Shows each probe's serial and COM port, which one json\\bench.json selects, and which
board it is wired to (json\\boards\\). Needs USB only - the boards are not touched. With
--chip it also reads the device and chip serial behind every probe, without halting it."""

EXAMPLES = """\
examples:
  probes.bat                 the list
  probes.bat --chip          plus device and chip serial behind every probe

Use a listed serial with flash.bat --probe <serial> or fuses.bat --probe <serial>;
install.bat --select records the one both use by default."""


def board_assignments(directory=BOARDS_DIR):
    """({probe serial: board id}, number of board files that record no probe serial).

    Unreadable files are skipped - a broken board file must not break a probe list."""
    boards, without_probe = {}, 0
    for path in sorted(Path(directory).glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        if record.get("probe_serial"):
            boards[record["probe_serial"]] = str(record.get("board_id") or record.get("ip") or path.stem)
        else:
            without_probe += 1
    return boards, without_probe


def rows(probes, selected, boards, ports):
    """(number, probe serial, COM port, probe, bench.json, board) per probe, sorted by serial."""
    ordered = sorted(probes, key=lambda probe: probe.unique_id)
    return [(str(number), probe.unique_id, ports.get(probe.unique_id, "-"), probe.description,
             "selected" if probe.unique_id == selected else "-", boards.get(probe.unique_id, "-"))
            for number, probe in enumerate(ordered, 1)]


def notes(connected, selected, boards, without_probe=0):
    """What the list alone does not make obvious."""
    result = []
    if selected and selected not in connected:
        result.append(f"bench.json selects {selected}, which is not connected - pass --probe to flash.bat "
                      "and fuses.bat, or record another probe with install.bat --select")
    if not selected and len(connected) > 1:
        result.append("bench.json selects no probe - with several connected, flash.bat and fuses.bat need --probe")
    for serial, board in sorted(boards.items(), key=lambda item: item[1]):
        if serial not in connected:
            result.append(f"not connected: {board} (probe {serial})")
    if without_probe:
        result.append(f"{without_probe} board file(s) in json\\boards record no probe serial, "
                      "so their boards cannot be shown next to a probe")
    return result


def device_layouts(pack):
    """The fuses_same54 Layout of every ATDF in the pack that carries a device signature."""
    layouts = []
    with zipfile.ZipFile(pack) as zf:
        for member in sorted(zf.namelist()):
            if member.startswith("atdf/") and member.endswith(".atdf"):
                try:
                    layout = fuses.parse_atdf(zf.read(member))
                except ValueError:
                    continue
                if layout.did_expected is not None:
                    layouts.append(layout)
    return layouts


def identify(did, layouts):
    """The layout whose signature matches the DID - revision and die ignored - or None."""
    return next((layout for layout in layouts if fuses.did_matches(layout, did)), None)


def chip_line(probe_serial, target, pack, frequency, layouts):
    """One line about the chip behind a probe. Attaches without halting and only reads."""
    try:
        with fuses.open_session(target, pack, probe_serial, frequency, "attach") as session:
            board = session.board.target
            did = board.read32(layouts[0].did_address)
            layout = identify(did, layouts)
            # The serial number location is the SAM D5x/E5x one - only read it for a device
            # from this pack, never guess it for an unknown chip.
            serial = fp.read_serial(board) if layout is not None else None
    except (Exception, SystemExit) as exc:
        message = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return f"chip: no answer - {message[:90]}"
    if layout is None:
        return f"chip: not a device from {Path(pack).name} (DSU DID 0x{did:08X})"
    return f"chip: {layout.device} (DSU DID 0x{did:08X}), serial {serial}"


def print_list(table, extra_lines):
    headings = ("#", "Probe serial", "COM", "Probe", "bench.json", "Board (json\\boards)")
    widths = [max(len(heading), *(len(row[i]) for row in table)) for i, heading in enumerate(headings)]

    def line(cells):
        return ("  " + "  ".join(cell.ljust(width) for cell, width in zip(cells, widths))).rstrip()

    print(line(headings))
    for row in table:
        print(line(row))
        if extra_lines.get(row[1]):
            print(" " * (4 + widths[0]) + extra_lines[row[1]])


def main():
    parser = argparse.ArgumentParser(prog="probes.bat", description=DESCRIPTION, epilog=EXAMPLES,
                                     formatter_class=argparse.RawDescriptionHelpFormatter, add_help=False)
    parser.add_argument("-h", "-help", "--help", action="help", help="Show this help and exit")
    parser.add_argument("--chip", action="store_true",
                        help="Also attach to every probe's target, without halting it, and show device and chip serial")
    parser.add_argument("--target", default=fs.DEFAULT_TARGET,
                        help=f"pyOCD target name used for --chip (default: {fs.DEFAULT_TARGET})")
    parser.add_argument("--pack", help="Explicit SAME54_DFP .pack file for --chip. Auto-detected if omitted.")
    parser.add_argument("--frequency", "-f", type=int, default=2_000_000, help="SWD clock in Hz for --chip (default: 2000000)")
    args = parser.parse_args()

    from pyocd.core.helpers import ConnectHelper

    probes = ConnectHelper.get_all_connected_probes(blocking=False)
    selected = fs.selected_probe()
    boards, without_probe = board_assignments()
    connected = {probe.unique_id for probe in probes}

    if not probes:
        print("[probes] No debug probe connected.")
    else:
        print(f"[probes] {len(probes)} probe(s) connected")
        extra = {}
        if args.chip:
            pack = fs.resolve_pack(args.pack)
            layouts = device_layouts(pack) if pack is not None else []
            if not layouts:
                print("[note ] --chip needs the Microchip.SAME54_DFP pack - none found, showing the probes only")
            else:
                print(f"[probes] Reading the chip behind each probe without halting it, devices from {Path(pack).name}")
                extra = {serial: chip_line(serial, args.target, pack, args.frequency, layouts) for serial in sorted(connected)}
        print()
        print_list(rows(probes, selected, boards, bridge_core.com_ports_by_probe_serial()), extra)
        print()
    for note in notes(connected, selected, boards, without_probe):
        print(f"[note ] {note}")
    return 0 if probes else 1


if __name__ == "__main__":
    sys.exit(main())
