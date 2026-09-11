#!/usr/bin/env python3
"""The write side of fuses.bat: program a SAME54 board's fuses (NVM User Row).

fuses_same54.py drives this module; nothing here talks to the user. What it does, and why -
all of it measured on a SAME54 board on 2026-09-11 (the note above PYOCD_LOG_FILTERS in
flash_same54.py has the full story):

  * pyOCD cannot program the User Row with Microchip's own USER_ROW flash algorithm: that
    algorithm fills each quad word out of order and trusts ADDR to follow the page buffer,
    which left the page's first word erased. This module drives NVMCTRL directly instead, in
    the order of Harmony's NVMCTRL_USER_ROW_RowErase/_PageWrite: clear the page buffer,
    erase the page, then load every 128-bit quad word and write it with its address set
    explicitly. The page is read back before anyone is told it worked.
  * The User Row holds factory calibration next to the settings (DS60001507N 9.4: "must not
    be changed"). A new page is always built from the board's own current page, and only
    fields the ATDF describes are replaced. A backup restores a page verbatim, but only onto
    the chip it came from, identified by its 128-bit serial number.

Register addresses, bit masks, command codes and the page location come from the ATDF. The
serial number location and the quad word size come from the SAM D5x/E5x datasheet
DS60001507N. Writing is refused on any NVMCTRL other than the one this was verified on.
"""

import datetime
import json
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
BACKUP_DIR = REPO_ROOT / "json" / "fuse_backups"
BACKUP_FORMAT = "fuses_same54 user row backup 1"

# The NVMCTRL (ATDF module id, version) the write sequence was verified on: ATSAME54P20A, 2026-09-11.
VERIFIED_NVMCTRL = ("U2409", "1.0.0")
# DS60001507N 9.6 "Serial Number": four 32-bit words, word 0 first; unique only with all 128 bits.
SERIAL_WORDS = (0x008061FC, 0x00806010, 0x00806014, 0x00806018)
# DS60001507N 25.8.2 and the ATDF's own WQW caption: Write Quad Word writes a 128-bit word.
QUAD_WORD = 16
READY_TIMEOUT = 3.0


class NvmError(RuntimeError):
    """NVMCTRL refused a command, did not finish it, or the page did not read back."""


def _int(text):
    return int(text, 0)


def _shift(mask):
    return (mask & -mask).bit_length() - 1


class Nvm:
    """NVMCTRL's registers, masks and command codes, and the User Row page - from the ATDF."""

    def __init__(self, root):
        device = root.find("./devices/device")
        instance_module = None if device is None else device.find("./peripherals/module[@name='NVMCTRL']")
        instance = None if instance_module is None else instance_module.find("./instance/register-group[@name='NVMCTRL']")
        module = root.find("./modules/module[@name='NVMCTRL']")
        group = None if module is None else module.find("./register-group[@name='NVMCTRL']")
        segment = None if device is None else device.find(".//memory-segment[@name='USER_PAGE']")
        if instance is None or group is None or segment is None:
            raise ValueError("the ATDF lacks NVMCTRL or the USER_PAGE memory segment")
        self.module_id = (instance_module.get("id"), instance_module.get("version"))
        base = _int(instance.get("offset"))

        def register(name):
            reg = group.find(f"./register[@name='{name}']")
            if reg is None:
                raise ValueError(f"the ATDF lacks NVMCTRL.{name}")
            return reg

        def mask(reg_name, field_name):
            field = register(reg_name).find(f"./bitfield[@name='{field_name}']")
            if field is None:
                raise ValueError(f"the ATDF lacks NVMCTRL.{reg_name}.{field_name}")
            return _int(field.get("mask"))

        def value(group_name, value_name):
            entry = module.find(f"./value-group[@name='{group_name}']/value[@name='{value_name}']")
            if entry is None:
                raise ValueError(f"the ATDF lacks {group_name}.{value_name}")
            return _int(entry.get("value"))

        self.ctrla, self.ctrlb, self.intflag, self.status, self.addr = (
            base + _int(register(name).get("offset")) for name in ("CTRLA", "CTRLB", "INTFLAG", "STATUS", "ADDR"))
        self.wmode_mask = mask("CTRLA", "WMODE")
        self.wmode_manual = value("NVMCTRL_CTRLA__WMODE", "MAN") << _shift(self.wmode_mask)
        self.cmdex_shift = _shift(mask("CTRLB", "CMDEX"))
        self.key = value("NVMCTRL_CTRLB__CMDEX", "KEY")
        self.erase_page = value("NVMCTRL_CTRLB__CMD", "EP")
        self.write_quad_word = value("NVMCTRL_CTRLB__CMD", "WQW")
        self.page_buffer_clear = value("NVMCTRL_CTRLB__CMD", "PBC")
        self.ready = mask("STATUS", "READY")
        self.errors = 0
        for name in ("ADDRE", "PROGE", "LOCKE", "NVME", "SUSP"):
            self.errors |= mask("INTFLAG", name)
        self.all_flags = 0
        for field in register("INTFLAG").findall("bitfield"):
            self.all_flags |= _int(field.get("mask"))
        self.page_start = _int(segment.get("start"))
        self.page_size = _int(segment.get("size"))

    def verified(self):
        return self.module_id == VERIFIED_NVMCTRL

    def command(self, code):
        return (self.key << self.cmdex_shift) | code


def parse_assignments(layout, texts):
    """FIELD=VALUE strings (--set) -> {field name: raw value}.

    Field names are the ATDF's, case-insensitive. A value is a number or one of the field's
    value names (CYC16384, RESET). Where the ATDF's value names are themselves numbers - the
    bootloader size "8" is the raw value 0xE - only a hex number is accepted."""
    fields = {field.name.upper(): field for reg in layout.registers for field in reg.fields}
    changes = {}
    for text in texts:
        name, sep, raw = text.partition("=")
        name, raw = name.strip(), raw.strip()
        if not sep or not name or not raw:
            raise ValueError(f"{text}: expected FIELD=VALUE, e.g. WDT_PER=CYC16384")
        field = fields.get(name.upper())
        if field is None:
            raise ValueError(f"{name}: no such fuse field. Known: {', '.join(f.name for f in fields.values())}")
        value = _parse_value(field, raw)
        if changes.get(field.name, value) != value:
            raise ValueError(f"{field.name} is set twice, to different values")
        changes[field.name] = value
    return changes


def _parse_value(field, raw):
    for number, (value_name, _) in field.values.items():
        if not value_name.isdigit() and value_name.upper() == raw.upper():
            return number
    if any(value_name.isdigit() for value_name, _ in field.values.values()) and not raw.lower().startswith("0x"):
        examples = ", ".join(f"{caption} = {number:#x}"
                             for number, (_, caption) in sorted(field.values.items(), reverse=True)[:3])
        raise ValueError(f"{field.name}={raw} is ambiguous - give the raw value in hex ({examples}, ...)")
    try:
        value = int(raw, 0)
    except ValueError:
        names = ", ".join(value_name for value_name, _ in field.values.values()) or "none"
        raise ValueError(f"{field.name}={raw}: neither a number nor a value name (value names: {names})") from None
    width = bin(field.mask).count("1")
    if not 0 <= value < (1 << width):
        raise ValueError(f"{field.name}={raw}: does not fit its {width} bit(s)")
    if field.values and value not in field.values:
        allowed = ", ".join(f"{number:#x} {value_name}" for number, (value_name, _) in sorted(field.values.items()))
        raise ValueError(f"{field.name}={raw}: not a defined value. Allowed: {allowed}")
    return value


def changes_from_image(layout, image):
    """Every described field's value from a HEX image's fuse bytes (None where missing)."""
    changes = {}
    for reg in layout.registers:
        chunk = image[reg.offset:reg.offset + reg.size]
        if len(chunk) < reg.size or any(b is None for b in chunk):
            raise ValueError(f"the image carries no complete {reg.name}")
        word = int.from_bytes(bytes(chunk), "little")
        for field in reg.fields:
            changes[field.name] = field.value(word)
    return changes


def compose(layout, page, changes):
    """The board's whole User Row `page` with the described fields in `changes` replaced.

    Every other bit - factory calibration, reserved bits, the free bytes behind the fuses -
    keeps the board's value. That is the whole point: a HEX image has zeros there."""
    new = bytearray(page)
    for reg in layout.registers:
        word = int.from_bytes(new[reg.offset:reg.offset + reg.size], "little")
        for field in reg.fields:
            if field.name in changes:
                word = (word & ~field.mask) | ((changes[field.name] << field.shift) & field.mask)
        new[reg.offset:reg.offset + reg.size] = word.to_bytes(reg.size, "little")
    return bytes(new)


def read_serial(target):
    """The chip's 128-bit serial number as 32 hex digits, word 0 first."""
    return "".join(f"{target.read32(address):08X}" for address in SERIAL_WORDS)


def save_backup(page, device, did, serial, page_start, directory=BACKUP_DIR, now=None):
    now = now or datetime.datetime.now()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{device}_{serial}_{now:%Y%m%d-%H%M%S}.json"
    record = {
        "format": BACKUP_FORMAT,
        "device": device,
        "serial": serial,
        "dsu_did": f"0x{did:08X}",
        "address": f"0x{page_start:08X}",
        "saved": now.isoformat(timespec="seconds"),
        "page": bytes(page).hex(),
    }
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def load_backup(path, page_size):
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: not readable as a fuse backup ({exc})") from None
    if not isinstance(record, dict) or record.get("format") != BACKUP_FORMAT:
        raise ValueError(f"{path} is not a fuse backup written by fuses.bat")
    try:
        page = bytes.fromhex(record.get("page", ""))
    except ValueError:
        raise ValueError(f"{path}: the page is not valid hex") from None
    if len(page) != page_size:
        raise ValueError(f"{path} holds {len(page)} bytes, a User Row page has {page_size}")
    return record, page


def check_restore_target(record, device, serial):
    """Refuse a backup from another chip - it carries that chip's factory calibration."""
    if record.get("device") != device:
        raise ValueError(f"the backup is from a {record.get('device')}, this board is a {device}")
    if record.get("serial") != serial:
        raise ValueError(f"the backup is from the chip with serial {record.get('serial')}, this board has {serial} - "
                         "restoring it would copy another chip's factory calibration")


def _wait_ready(target, nvm, what):
    deadline = time.monotonic() + READY_TIMEOUT
    while not target.read16(nvm.status) & nvm.ready:
        if time.monotonic() > deadline:
            raise NvmError(f"NVMCTRL not ready ({what})")
        time.sleep(0.005)


def _run(target, nvm, code, what, address=None):
    _wait_ready(target, nvm, "before " + what)
    target.write16(nvm.intflag, nvm.all_flags)
    if address is not None:
        target.write32(nvm.addr, address)
    target.write16(nvm.ctrlb, nvm.command(code))
    _wait_ready(target, nvm, what)
    flags = target.read16(nvm.intflag)
    if flags & nvm.errors:
        raise NvmError(f"{what}: NVMCTRL.INTFLAG = 0x{flags:04X}")


def write_user_page(target, nvm, page):
    """Erase the User Row page, program `page` into it and read it back.

    Needs a halted core and pyOCD's memory cache off. Raises NvmError when NVMCTRL refuses a
    command or the page does not read back as `page`; CTRLA is restored either way."""
    page = bytes(page)
    if len(page) != nvm.page_size:
        raise ValueError(f"a User Row page has {nvm.page_size} bytes, got {len(page)}")
    blank = b"\xff" * QUAD_WORD
    ctrla = target.read16(nvm.ctrla)
    target.write16(nvm.ctrla, (ctrla & ~nvm.wmode_mask) | nvm.wmode_manual)
    try:
        _run(target, nvm, nvm.page_buffer_clear, "page buffer clear")
        _run(target, nvm, nvm.erase_page, "erase page", nvm.page_start)
        if bytes(target.read_memory_block8(nvm.page_start, nvm.page_size)) != b"\xff" * nvm.page_size:
            raise NvmError("the page is not blank after the erase")
        for offset in range(0, nvm.page_size, QUAD_WORD):
            chunk = page[offset:offset + QUAD_WORD]
            if chunk == blank:
                continue                    # the erase already left it like this
            for i in range(0, QUAD_WORD, 4):
                target.write32(nvm.page_start + offset + i, int.from_bytes(chunk[i:i + 4], "little"))
            # ADDR set explicitly - writing the page buffer also moves ADDR, and trusting that
            # is exactly how Microchip's USER_ROW algorithm lost the first word.
            _run(target, nvm, nvm.write_quad_word, f"write quad word at +{offset}", nvm.page_start + offset)
    finally:
        target.write16(nvm.ctrla, ctrla)
    written = bytes(target.read_memory_block8(nvm.page_start, nvm.page_size))
    if written != page:
        wrong = [i for i in range(nvm.page_size) if written[i] != page[i]]
        raise NvmError(f"the page reads back different in {len(wrong)} byte(s), first at +{wrong[0]}")
