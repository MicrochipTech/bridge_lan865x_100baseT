#!/usr/bin/env python3
"""Show and program a SAME54 board's fuses (NVM User Row) by name.

Reads the User Row from the board over the EDBG probe and decodes it with the field
definitions from the device's own ATDF. The ATDF ships inside the Microchip.SAME54_DFP
pack that flash_same54.py already locates or downloads, so no bit position is written
down in this file. Next to the board it shows the fuses in a HEX image - the values of
MCC's #pragma config - by default the same release HEX flash.bat programs.

Called without programming options it only analyses: it reads without halting the board,
and the firmware keeps running. It writes only when told to with --set, --write-from-hex
or --restore, through fuse_programmer.py: the new page is built from the board's own, the
change is shown, a backup goes to json\\fuse_backups\\, and only after a yes is the page
written, read back and - if it matches - the board reset. flash_same54.py never programs
the fuses; the note above its PYOCD_LOG_FILTERS says why.

fuses.bat -help lists every option with examples.
"""

import argparse
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import flash_same54 as fs         # noqa: E402  - pack lookup, bench.json probe, default target
import fuse_programmer as fp      # noqa: E402  - the write side

REPO_ROOT = Path(__file__).parent.parent
DEFAULT_HEX = REPO_ROOT / "release" / "bridge_lan865x_100baseT.hex"

DESCRIPTION = """\
Show and program a SAME54 board's fuses (NVM User Row) by name.

Without programming options this only ANALYSES: it reads the board's fuses without
halting it and compares them with the release HEX. It writes the fuses ONLY when told
to with --set, --write-from-hex or --restore - and even then it first shows the change,
saves a backup and asks."""

EXAMPLES = """\
analysis - reads only, the default:
  fuses.bat                                   board vs. the release HEX
  fuses.bat --hex <file.hex>                  board vs. another image
  fuses.bat --no-hex                          board only
  fuses.bat --no-board                        image only - no probe needed
  fuses.bat -v                                with every field's description

programming - writes the fuse page, only with one of these options:
  fuses.bat --set WDT_PER=CYC16384 --dry-run  show the change, write nothing
  fuses.bat --set WDT_PER=CYC16384            change a field (asks first)
  fuses.bat --set WDT_ENABLE=1 --set WDT_PER=CYC8192
  fuses.bat --write-from-hex                  set every field to the HEX's value
  fuses.bat --restore json\\fuse_backups\\<file>.json

Field names are the ones in the analysis table; values are numbers or the names
shown there (CYC16384, RESET). Every write keeps the factory calibration bits,
saves a backup to json\\fuse_backups\\ first and resets the board only after the
page reads back correctly."""

# Wording for fields whose ATDF entry has no value names. Only the words live here - which
# bits a field occupies always comes from the ATDF. Meanings per the SAME5x datasheet,
# 9.4 "NVM User Row Mapping".
HINTS = {
    "BOD33_DIS": {0: "BOD33 on", 1: "BOD33 off"},
    "RAMECC_ECCDIS": {0: "RAM ECC on", 1: "RAM ECC off"},
    "WDT_ENABLE": {0: "off", 1: "on"},
    "WDT_ALWAYSON": {0: "no", 1: "yes"},
    "WDT_WEN": {0: "off", 1: "on"},
    "NVMCTRL_SEESBLK": {0: "SmartEEPROM off"},
}

# Shorter wording for ATDF captions too long for one table line with -v. Every other field
# keeps its ATDF caption unchanged.
CAPTIONS = {
    "NVMCTRL_SEESBLK": "SmartEEPROM blocks per sector",   # ATDF: Number Of Physical NVM Blocks Composing a SmartEEPROM Sector
}

NAME_WIDTH = 22
CELL_WIDTH = 24         # the widest value is "0xffffffff  all unlocked"


def _int(text):
    return int(text, 0)


def _ones(value):
    return bin(value).count("1")


class Field:
    def __init__(self, name, caption, mask, values):
        self.name = name
        self.caption = caption
        self.mask = mask
        self.values = values            # {raw value: (name, caption)} from the ATDF value-group
        self.shift = (mask & -mask).bit_length() - 1

    def value(self, word):
        return (word & self.mask) >> self.shift


class Register:
    def __init__(self, name, caption, offset, size, fields):
        self.name = name
        self.caption = caption
        self.offset = offset
        self.size = size
        self.fields = fields
        used = 0
        for field in fields:
            used |= field.mask
        self.undescribed = ((1 << (8 * size)) - 1) & ~used


class Layout:
    """What the ATDF says about one device's User Row."""

    def __init__(self, device, base, registers, did_address, did_expected, did_ignore):
        self.device = device
        self.base = base
        self.registers = registers
        self.length = max(r.offset + r.size for r in registers)
        self.did_address = did_address
        self.did_expected = did_expected
        self.did_ignore = did_ignore    # DID bits that differ between batches (revision, die)
        self.root = None                # the parsed ATDF, for fuse_programmer.Nvm


def parse_atdf(xml_bytes):
    root = ET.fromstring(xml_bytes)
    device = root.find("./devices/device")
    if device is None:
        raise ValueError("no <device> in the ATDF")
    name = device.get("name")
    instance = device.find("./peripherals/module[@name='FUSES']/instance/register-group[@name='USER_FUSES']")
    module = root.find("./modules/module[@name='FUSES']")
    group = None if module is None else module.find("./register-group[@name='USER_FUSES']")
    if instance is None or group is None:
        raise ValueError(f"the ATDF of {name} describes no USER_FUSES")

    def value_group(vg_name):
        # A name like WDT_CONFIG__PER exists in the FUSES and in the WDT module; FUSES wins.
        vg = module.find(f"./value-group[@name='{vg_name}']")
        return vg if vg is not None else root.find(f".//value-group[@name='{vg_name}']")

    registers = []
    for reg in group.findall("register"):
        fields = []
        for bf in reg.findall("bitfield"):
            values = {}
            vg = value_group(bf.get("values")) if bf.get("values") else None
            if vg is not None:
                values = {_int(v.get("value")): (v.get("name"), v.get("caption", "")) for v in vg.findall("value")}
            fields.append(Field(bf.get("name"), bf.get("caption", ""), _int(bf.get("mask")), values))
        registers.append(Register(reg.get("name"), reg.get("caption", ""), _int(reg.get("offset")),
                                  _int(reg.get("size", "4")), fields))
    if not registers:
        raise ValueError(f"the ATDF of {name} lists no USER_FUSES registers")

    did_address = did_expected = None
    did_ignore = 0
    signature = device.find("./property-groups/property-group[@name='SIGNATURES']/property[@name='DSU_DID']")
    dsu = device.find("./peripherals/module[@name='DSU']/instance/register-group[@name='DSU']")
    did = root.find("./modules/module[@name='DSU']/register-group[@name='DSU']/register[@name='DID']")
    if signature is not None and dsu is not None and did is not None:
        did_address = _int(dsu.get("offset")) + _int(did.get("offset"))
        did_expected = _int(signature.get("value"))
        for bf in did.findall("bitfield"):
            if bf.get("name") in ("REVISION", "DIE"):
                did_ignore |= _int(bf.get("mask"))
    layout = Layout(name, _int(instance.get("offset")), registers, did_address, did_expected, did_ignore)
    layout.root = root
    return layout


def load_layout(pack, target):
    member = f"atdf/{target.upper()}.atdf"
    with zipfile.ZipFile(pack) as zf:
        try:
            data = zf.read(member)
        except KeyError:
            raise SystemExit(f"ERROR: {pack} contains no {member} - wrong --target or wrong pack?") from None
    return member, parse_atdf(data)


def register_value(reg, data):
    """The register's word from raw User Row bytes, or None where bytes are missing."""
    chunk = data[reg.offset:reg.offset + reg.size]
    if len(chunk) < reg.size or any(b is None for b in chunk):
        return None
    return int.from_bytes(bytes(chunk), "little")


def field_values(layout, data):
    """{field name: (Field, value)} - value is None where the data lacks the bytes."""
    result = {}
    for reg in layout.registers:
        word = register_value(reg, data)
        for field in reg.fields:
            result[field.name] = (field, None if word is None else field.value(word))
    return result


def describe(field, value):
    if value is None:
        return "-"
    text = f"{value:#x}" if _ones(field.mask) > 1 else str(value)
    if field.name == "NVMCTRL_REGION_LOCKS":        # a 0 bit locks its region
        locked = _ones(field.mask) - _ones(value)
        return f"{text}  " + ("all unlocked" if not locked else f"{locked} locked")
    if value in field.values:
        name, caption = field.values[value]
        return f"{text}  {caption if name.isdigit() else name}"
    hint = HINTS.get(field.name, {}).get(value)
    return f"{text}  {hint}" if hint else text


def differences(layout, board, image):
    """Names of the described fields whose values differ. Undescribed bits never count."""
    theirs = field_values(layout, image)
    return [name for name, (_, value) in field_values(layout, board).items()
            if value is not None and theirs[name][1] is not None and value != theirs[name][1]]


def power_on_warnings(layout, data):
    """Settings that change how the board starts, in words."""
    fields = field_values(layout, data)

    def value(name):
        return fields[name][1] if name in fields else None

    warnings = []
    if value("WDT_ENABLE") == 1:
        if value("WDT_ALWAYSON") == 1:
            warnings.append("watchdog ALWAYS-ON from power-on - the firmware must service it, "
                            "or the board resets every period")
        else:
            warnings.append("watchdog enabled from power-on - the firmware must service or disable it")
    bootprot = value("NVMCTRL_BOOTPROT")
    if bootprot is not None and bootprot != 0xF:        # 0xF is "0 kbytes": no protection
        size = fields["NVMCTRL_BOOTPROT"][0].values.get(bootprot, ("", f"{(15 - bootprot) * 8} kbytes"))[1]
        warnings.append(f"bootloader protection: the first {size} of flash are write-protected")
    if value("NVMCTRL_SEESBLK"):
        warnings.append("SmartEEPROM enabled - it takes NVM blocks at the end of flash")
    locks = value("NVMCTRL_REGION_LOCKS")
    if locks is not None:
        width = _ones(fields["NVMCTRL_REGION_LOCKS"][0].mask)
        locked = width - _ones(locks)
        if locked:
            warnings.append(f"{locked} of {width} flash regions locked from power-on")
    return warnings


def did_matches(layout, did):
    if layout.did_expected is None:
        return True
    return ((did ^ layout.did_expected) & ~layout.did_ignore & 0xFFFFFFFF) == 0


def open_session(target, pack, probe, frequency, connect_mode):
    """A pyOCD session with caching off: "attach" only reads, "halt" stops the core."""
    import logging

    # The same two harmless pyOCD messages flash_same54.py filters, see PYOCD_LOG_FILTERS there.
    logging.getLogger("pyocd.coresight.discovery").setLevel(logging.CRITICAL)
    logging.getLogger("pyocd.target.pack.cmsis_pack").setLevel(logging.ERROR)
    from pyocd.core.helpers import ConnectHelper

    options = {"target_override": target, "frequency": frequency, "pack": str(pack),
               "connect_mode": connect_mode, "cache.enable_memory": False, "cache.enable_register": False}
    session = ConnectHelper.session_with_chosen_probe(unique_id=probe, blocking=False, options=options)
    if session is None:
        raise SystemExit(f"ERROR: probe {probe} not found.")
    return session


def read_board(layout, target, pack, probe, frequency):
    """(DSU DID, User Row bytes) over the probe. Attaches without halting and only reads."""
    with open_session(target, pack, probe, frequency, "attach") as session:
        board = session.board.target
        did = board.read32(layout.did_address) if layout.did_address is not None else None
        data = list(board.read_memory_block8(layout.base, layout.length))
    return did, data


def read_image(path, layout):
    """User Row bytes from a HEX image, None where the image has no data."""
    from intelhex import IntelHex

    ih = IntelHex(str(path))
    end = layout.base + layout.length
    data = [None] * layout.length
    for start, stop in ih.segments():
        for address in range(max(start, layout.base), min(stop, end)):
            data[address - layout.base] = ih[address]
    return data


def resolve_probe(explicit):
    probe = explicit or fs.selected_probe()
    if probe:
        return probe
    probes = fs.list_probes()
    if len(probes) != 1:
        problem = "no probe connected" if not probes else "several probes connected - pass --probe with one of the IDs above"
        sys.exit(f"ERROR: {problem}")
    return probes[0].unique_id


def _row(name, cells, mark, caption, compare):
    line = f"  {name:<{NAME_WIDTH}}" + "".join(f" {cell:<{CELL_WIDTH}}" for cell in cells)
    if compare:
        line += f" {mark:<7}"
    print(f"{line} {caption}".rstrip())


def print_table(layout, columns, verbose):
    compare = len(columns) == 2
    _row("Field", [heading for heading, _ in columns], "", "Description" if verbose else "", compare)
    for reg in layout.registers:
        words = [register_value(reg, data) for _, data in columns]
        print()
        _row(reg.name, ["-" if w is None else f"{w:#010x}" for w in words], "",
             reg.caption if verbose else "", compare)
        for field in reg.fields:
            values = [None if w is None else field.value(w) for w in words]
            mark = ""
            if compare and None not in values:
                mark = "same" if values[0] == values[1] else "DIFFERS"
            _row("  " + field.name, [describe(field, v) for v in values], mark,
                 CAPTIONS.get(field.name, field.caption) if verbose else "", compare)
        if reg.undescribed:
            _row("  (other bits)", ["-" if w is None else f"{w & reg.undescribed:#010x}" for w in words],
                 "-" if compare else "", "", compare)


def _shown(path):
    try:
        return Path(path).resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        return Path(path)


def _identify(board, layout, nvm):
    """(DID, serial, whole User Row page) - refuses a chip the ATDF does not describe."""
    did = board.read32(layout.did_address)
    if not did_matches(layout, did):
        sys.exit(f"ERROR: the board's DSU DID is 0x{did:08X}, but {layout.device} has 0x{layout.did_expected:08X} - "
                 "refusing to write with the wrong field layout. Wrong --target?")
    return did, fp.read_serial(board), bytes(board.read_memory_block8(nvm.page_start, nvm.page_size))


def _print_plan(layout, old, new, restoring):
    print()
    print("  Planned change")
    before, after = field_values(layout, list(old)), field_values(layout, list(new))
    for name, (field, value) in before.items():
        if value != after[name][1]:
            print(f"    {name:<{NAME_WIDTH - 2}} {describe(field, value):<{CELL_WIDTH}} -> {describe(field, after[name][1])}")
    for reg in layout.registers:
        old_word, new_word = register_value(reg, list(old)), register_value(reg, list(new))
        if (old_word ^ new_word) & reg.undescribed:
            print(f"    {'(other bits)':<{NAME_WIDTH - 2}} {old_word & reg.undescribed:#010x}{'':<{CELL_WIDTH - 10}} "
                  f"-> {new_word & reg.undescribed:#010x}  in {reg.name}")
    tail = sum(1 for i in range(layout.length, len(old)) if old[i] != new[i])
    if tail:
        print(f"    {tail} byte(s) behind the fuses (0x{layout.base + layout.length:08X} and up) change as well")
    print()
    if restoring:
        print("[fuses] The whole page comes back from the backup of this very chip.")
    else:
        print("[fuses] Everything else stays as it is, including the factory calibration bits.")
    had = set(power_on_warnings(layout, list(old)))
    for warning in power_on_warnings(layout, list(new)):
        if warning not in had:
            print(f"[warn ] after the write: {warning}")


def _confirmed(yes, device, serial):
    if yes:
        return True
    if not sys.stdin.isatty():
        sys.exit("ERROR: no console to ask on - add --yes to write without asking.")
    print()
    try:
        answer = input(f"Erase and reprogram the fuse page of this {device} (serial {serial})? Type yes to write: ")
    except (EOFError, KeyboardInterrupt):
        # isatty() is not proof of a console: under "cmd /c" with no stdin it still says
        # True, and input() then hits end of file. No answer is not a yes.
        print()
        return False
    return answer.strip().lower() == "yes"


def _write_failed(reason, backup):
    print(f"ERROR: the fuse write failed: {reason}")
    print("       The board was NOT reset and still runs with the fuses it loaded at its last reset.")
    print("       Do not power-cycle or reset it before restoring the saved page:")
    print(f"       fuses.bat --restore {backup}")
    sys.exit(2)


def program(args, pack, layout):
    """--set, --write-from-hex and --restore. Returns the exit code."""
    try:
        nvm = fp.Nvm(layout.root)
    except ValueError as exc:
        sys.exit(f"ERROR: cannot program: {exc}")
    if not nvm.verified():
        sys.exit(f"ERROR: this device's NVMCTRL ({' '.join(nvm.module_id)}) is not the one the write sequence was "
                 f"verified on ({' '.join(fp.VERIFIED_NVMCTRL)}) - refusing to write. Showing the fuses still works.")
    if nvm.page_start != layout.base or layout.did_address is None:
        sys.exit("ERROR: the ATDF gives no device signature or disagrees on the User Row address - refusing to write.")

    record = changes = None
    try:
        if args.set:
            changes = fp.parse_assignments(layout, args.set)
        elif args.write_from_hex:
            path = Path(args.hex) if args.hex else DEFAULT_HEX
            if not path.is_file():
                sys.exit(f"ERROR: {path} does not exist")
            changes = fp.changes_from_image(layout, read_image(path, layout))
            print(f"[fuses] Image  : {_shown(path)}")
        else:
            record, backup_page = fp.load_backup(args.restore, nvm.page_size)
            print(f"[fuses] Backup : {args.restore} (saved {record.get('saved')})")
    except ValueError as exc:
        sys.exit(f"ERROR: {exc}")

    probe = resolve_probe(args.probe)
    with open_session(args.target, pack, probe, args.frequency, "attach") as session:
        did, serial, page = _identify(session.board.target, layout, nvm)
    print(f"[fuses] Board  : {layout.device} (DSU DID 0x{did:08X}), serial {serial}, probe {probe}")

    if record is not None:
        try:
            fp.check_restore_target(record, layout.device, serial)
        except ValueError as exc:
            sys.exit(f"ERROR: {exc}")
        new_page = backup_page
    else:
        new_page = fp.compose(layout, page, changes)

    if new_page == page:
        print("[ok   ] The board already has exactly these fuses - nothing to write.")
        return 0
    _print_plan(layout, page, new_page, record is not None)
    if args.dry_run:
        print("[ok   ] Dry run - the board was not touched.")
        return 0
    if not _confirmed(args.yes, layout.device, serial):
        print("Not confirmed - the board was not touched.")
        return 1

    backup = _shown(fp.save_backup(page, layout.device, did, serial, nvm.page_start))
    print(f"[fuses] Backup : {backup}")
    with open_session(args.target, pack, probe, args.frequency, "halt") as session:
        board = session.board.target
        board.halt()
        if _identify(board, layout, nvm) != (did, serial, page):
            sys.exit("ERROR: the board is not the one just read, or its fuses changed meanwhile - nothing written.")
        try:
            fp.write_user_page(board, nvm, new_page)
        except Exception as exc:        # NvmError, or the SWD link lost mid-write
            _write_failed(exc, backup)
        print(f"[ok   ] Fuse page written, {nvm.page_size} bytes read back identical.")
        if args.no_reset:
            print("[note ] Not reset - the board resumes, and the new fuses take effect at its next reset.")
            return 0
        board.reset()
        after = bytes(board.read_memory_block8(nvm.page_start, nvm.page_size))
    if after != new_page:
        _write_failed("the page reads different after the reset", backup)
    print("[ok   ] Board reset - the new fuses are active.")
    return 0


def main():
    parser = argparse.ArgumentParser(prog="fuses.bat", description=DESCRIPTION, epilog=EXAMPLES,
                                     formatter_class=argparse.RawDescriptionHelpFormatter, add_help=False)
    parser.add_argument("-h", "-help", "--help", action="help", help="Show this help and exit")
    parser.add_argument("--hex", help=f"HEX image to compare with (default: {DEFAULT_HEX.relative_to(REPO_ROOT)})")
    parser.add_argument("--no-hex", action="store_true", help="Show the board only")
    parser.add_argument("--no-board", action="store_true", help="Show the image only - no probe needed")
    parser.add_argument("--from-bin", metavar="FILE",
                        help="Decode a saved raw User Row dump instead of reading the board "
                             "(e.g. from flash_same54.py --read 0x00804000 512 --out FILE)")
    parser.add_argument("--target", default=fs.DEFAULT_TARGET, help=f"pyOCD target name (default: {fs.DEFAULT_TARGET})")
    parser.add_argument("--pack", help="Explicit SAME54_DFP .pack file or unpacked pack directory. Auto-detected if omitted.")
    parser.add_argument("--probe", "-u", help="Unique ID (serial) of the probe. Default: the one recorded in bench.json.")
    parser.add_argument("--frequency", "-f", type=int, default=2_000_000, help="SWD clock in Hz (default: 2000000)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Add the ATDF's description of every field")
    writes = parser.add_argument_group("programming - the only options that write the board's fuses")
    writes.add_argument("--set", action="append", metavar="FIELD=VALUE",
                        help="Change one fuse field, e.g. WDT_PER=CYC16384 or WDT_ENABLE=1. Repeatable.")
    writes.add_argument("--write-from-hex", action="store_true",
                        help="Set every fuse field to its value in the HEX image (--hex, default the release HEX)")
    writes.add_argument("--restore", metavar="BACKUP", help="Write a backup fuses.bat saved back - onto the same chip only")
    writes.add_argument("--dry-run", action="store_true", help="Show what would change, write nothing")
    writes.add_argument("--yes", action="store_true", help="Write without asking")
    writes.add_argument("--no-reset", action="store_true", help="Do not reset after writing; the fuses take effect at the next reset")
    args = parser.parse_args()

    modes = sum(1 for chosen in (args.set, args.write_from_hex, args.restore) if chosen)
    if modes > 1:
        parser.error("--set, --write-from-hex and --restore exclude each other")
    if modes and (args.no_board or args.from_bin):
        parser.error("programming needs the board - drop --no-board and --from-bin")
    if args.write_from_hex and args.no_hex:
        parser.error("--write-from-hex needs the HEX image - drop --no-hex")
    if not modes and (args.dry_run or args.yes or args.no_reset):
        parser.error("--dry-run, --yes and --no-reset only go with --set, --write-from-hex or --restore")
    if args.no_board and args.no_hex and not args.from_bin:
        parser.error("--no-board together with --no-hex leaves nothing to show")

    pack = fs.resolve_pack(args.pack)
    if pack is None:
        sys.exit("ERROR: no Microchip.SAME54_DFP pack - the field layout comes from its ATDF. Pass --pack.")
    member, layout = load_layout(pack, args.target)
    print(f"[fuses] Layout : {member} from {pack}")
    print(f"[fuses] Fuses  : NVM User Row, {layout.length} bytes at 0x{layout.base:08X}")
    if modes:
        return program(args, pack, layout)

    columns = []
    if args.from_bin:
        raw = Path(args.from_bin).read_bytes()[:layout.length]
        if len(raw) < layout.length:
            sys.exit(f"ERROR: {args.from_bin} is shorter than {layout.length} bytes")
        print(f"[fuses] Dump   : {args.from_bin}")
        columns.append(("Dump", list(raw)))
    elif not args.no_board:
        probe = resolve_probe(args.probe)
        did, data = read_board(layout, args.target, pack, probe, args.frequency)
        if did is not None and not did_matches(layout, did):
            sys.exit(f"ERROR: the board's DSU DID is 0x{did:08X}, but {layout.device} has 0x{layout.did_expected:08X} - "
                     "refusing to decode with the wrong field layout. Wrong --target?")
        did_text = "" if did is None else f" (DSU DID 0x{did:08X})"
        print(f"[fuses] Board  : {layout.device}{did_text}, probe {probe}, read-only")
        columns.append(("Board", data))

    if not args.no_hex:
        path = Path(args.hex) if args.hex else DEFAULT_HEX
        shown = path if args.hex else path.relative_to(REPO_ROOT)
        if not path.is_file():
            if args.hex:
                sys.exit(f"ERROR: {path} does not exist")
            print(f"[note ] {shown} not found - nothing to compare with")
        elif path.suffix.lower() != ".hex":
            sys.exit("ERROR: --hex takes a .hex image")
        else:
            image = read_image(path, layout)
            if all(b is None for b in image):
                print(f"[note ] {shown} carries no fuse data - nothing to compare with")
            else:
                print(f"[fuses] Image  : {shown}")
                columns.append(("Image", image))
    if not columns:
        sys.exit("ERROR: nothing to show")

    print()
    print_table(layout, columns, args.verbose)
    print()
    if any(reg.undescribed for reg in layout.registers):
        print("  (other bits): not described in the ATDF - reserved or factory calibration (datasheet 9.4).")
        print("                Never compared, and never to be changed by hand.")
        print()
    if len(columns) == 2:
        count = len(field_values(layout, columns[0][1]))
        diff = differences(layout, columns[0][1], columns[1][1])
        if not diff:
            print(f"[ok   ] {columns[0][0]} and image agree on all {count} fuse fields.")
        else:
            print(f"[warn ] {len(diff)} of {count} fuse fields differ: {', '.join(diff)}.")
            print("        flash.bat will not change them - it never programs fuses. fuses.bat --write-from-hex does.")
    for heading, data in columns:
        for warning in power_on_warnings(layout, data):
            print(f"[warn ] {heading.lower()}: {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
