#!/usr/bin/env python3
"""
Offline test for the write side of fuses.bat (scripts\\fuse_programmer.py).
No board, no probe, no pack, no network - stdlib only.

NVMCTRL is simulated with the behaviour the datasheet gives (DS60001507N 25.8) and the
real board showed on 2026-09-11: writing the page buffer moves ADDR along with it, Erase
Page blanks the whole 512-byte page, Write Quad Word programs the 16 bytes at ADDR (bits
only go from 1 to 0), and a command that fails sets PROGE. Because ADDR follows the page
buffer here exactly as on the chip, a write routine that trusted it - the mistake in
Microchip's USER_ROW flash algorithm - would fail these tests.

The field layout is the trimmed ATDF from test_fuses_same54.py plus the NVMCTRL elements
fuse_programmer.py reads, copied from the ATSAME54P20A ATDF (Microchip.SAME54_DFP 3.11.261).

    python scripts\\test_fuse_programmer.py

Exits non-zero on the first failing expectation, so it works in a build script.
"""

import datetime
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import fuse_programmer as fp        # noqa: E402
import fuses_same54 as fuses        # noqa: E402
import test_fuses_same54 as base    # noqa: E402  - the trimmed ATDF, BOARD and IMAGE

USER_PAGE = b"""      <address-spaces>
        <address-space name="base" id="base" start="0x00000000" size="0x100000000">
          <memory-segment name="USER_PAGE" start="0x00804000" size="0x200" type="user_page" pagesize="512" rw="RW"/>
        </address-space>
      </address-spaces>
"""

NVMCTRL_INSTANCE = b"""        <module name="NVMCTRL" id="U2409" version="1.0.0">
          <instance name="NVMCTRL">
            <register-group name="NVMCTRL" name-in-module="NVMCTRL" address-space="base" offset="0x41004000"/>
          </instance>
        </module>
"""

NVMCTRL_MODULE = b"""    <module name="NVMCTRL" id="U2409" version="1.0.0" caption="Non-Volatile Memory Controller">
      <register-group name="NVMCTRL" caption="Non-Volatile Memory Controller">
        <register name="CTRLA" offset="0x0" rw="RW" size="2" initval="0x0004" caption="Control A">
          <bitfield name="AUTOWS" caption="Auto Wait State Enable" mask="0x4"/>
          <bitfield name="SUSPEN" caption="Suspend Enable" mask="0x8"/>
          <bitfield name="WMODE" caption="Write Mode" mask="0x30" values="NVMCTRL_CTRLA__WMODE"/>
          <bitfield name="PRM" caption="Power Reduction Mode during Sleep" mask="0xC0" values="NVMCTRL_CTRLA__PRM"/>
          <bitfield name="RWS" caption="NVM Read Wait States" mask="0xF00"/>
        </register>
        <register name="CTRLB" offset="0x4" rw="W" size="2" initval="0x0000" caption="Control B">
          <bitfield name="CMD" caption="Command" mask="0x7F" values="NVMCTRL_CTRLB__CMD"/>
          <bitfield name="CMDEX" caption="Command Execution" mask="0xFF00" values="NVMCTRL_CTRLB__CMDEX"/>
        </register>
        <register name="INTFLAG" offset="0x10" rw="RW" size="2" atomic-op="clear:INTFLAG" initval="0x0000" caption="Interrupt Flag Status and Clear">
          <bitfield name="DONE" caption="Command Done" mask="0x1"/>
          <bitfield name="ADDRE" caption="Address Error" mask="0x2"/>
          <bitfield name="PROGE" caption="Programming Error" mask="0x4"/>
          <bitfield name="LOCKE" caption="Lock Error" mask="0x8"/>
          <bitfield name="ECCSE" caption="ECC Single Error" mask="0x10"/>
          <bitfield name="ECCDE" caption="ECC Dual Error" mask="0x20"/>
          <bitfield name="NVME" caption="NVM Error" mask="0x40"/>
          <bitfield name="SUSP" caption="Suspended Write Or Erase Operation" mask="0x80"/>
          <bitfield name="SEESFULL" caption="Active SEES Full" mask="0x100"/>
          <bitfield name="SEESOVF" caption="Active SEES Overflow" mask="0x200"/>
          <bitfield name="SEEWRC" caption="SEE Write Completed" mask="0x400"/>
        </register>
        <register name="STATUS" offset="0x12" rw="R" size="2" initval="0x0000" caption="Status">
          <bitfield name="READY" caption="Ready to accept a command" mask="0x1"/>
          <bitfield name="PRM" caption="Power Reduction Mode" mask="0x2"/>
          <bitfield name="LOAD" caption="NVM Page Buffer Active Loading" mask="0x4"/>
        </register>
        <register name="ADDR" offset="0x14" rw="RW" size="4" initval="0x00000000" caption="Address">
          <bitfield name="ADDR" caption="NVM Address" mask="0xFFFFFF"/>
        </register>
      </register-group>
      <value-group name="NVMCTRL_CTRLA__WMODE">
        <value name="MAN" caption="Manual Write" value="0"/>
        <value name="ADW" caption="Automatic Double Word Write" value="1"/>
        <value name="AQW" caption="Automatic Quad Word" value="2"/>
        <value name="AP" caption="Automatic Page Write" value="3"/>
      </value-group>
      <value-group name="NVMCTRL_CTRLB__CMD">
        <value name="EP" caption="Erase Page - Only supported in the USER and AUX pages." value="0x0"/>
        <value name="EB" caption="Erase Block - Erases the block addressed by the ADDR register, not supported in the user page" value="0x1"/>
        <value name="WP" caption="Write Page - Writes the contents of the page buffer to the page addressed by the ADDR register, not supported in the user page" value="0x3"/>
        <value name="WQW" caption="Write Quad Word - Writes a 128-bit word at the location addressed by the ADDR register." value="0x4"/>
        <value name="PBC" caption="Page Buffer Clear - Clears the page buffer." value="0x15"/>
      </value-group>
      <value-group name="NVMCTRL_CTRLB__CMDEX">
        <value name="KEY" caption="Execution Key" value="0xA5"/>
      </value-group>
    </module>
"""


def _with_nvmctrl(atdf):
    anchors = [(b"      <peripherals>\n", USER_PAGE + b"      <peripherals>\n"),
               (b"      </peripherals>\n", NVMCTRL_INSTANCE + b"      </peripherals>\n"),
               (b"  </modules>\n", NVMCTRL_MODULE + b"  </modules>\n")]
    for anchor, replacement in anchors:
        assert atdf.count(anchor) == 1, anchor
        atdf = atdf.replace(anchor, replacement)
    return atdf


ATDF = _with_nvmctrl(base.ATDF)
LAYOUT = fuses.parse_atdf(ATDF)
NVM = fp.Nvm(LAYOUT.root)

# A realistic whole page: the board's 12 fuse bytes, then free User Row bytes in use.
PAGE = bytes(base.BOARD) + bytes(range(20, 20 + 20)) + b"\xff" * (512 - 12 - 20)


class FakeBoard:
    """A SAME54 as far as fuse_programmer touches it."""

    DID = 0x61840300
    SERIAL = (0x11111111, 0x22222222, 0x33333333, 0x44444444)

    def __init__(self, page):
        self.page = bytearray(page)
        self.buffer = {}
        self.ctrla = 0x0504             # what the running firmware leaves in CTRLA
        self.intflag = 0
        self.addr = 0
        self.log = []                   # (command, page offset of ADDR)
        self.fail_commands = False      # every command sets PROGE
        self.erase_leaves = None        # a byte value the erase leaves instead of 0xFF
        self.lose_quad_word_at = None   # a WQW offset that silently programs nothing

    def _in_page(self, address):
        return NVM.page_start <= address < NVM.page_start + NVM.page_size

    def read16(self, address):
        if address == NVM.status:
            return NVM.ready
        if address == NVM.intflag:
            return self.intflag
        if address == NVM.ctrla:
            return self.ctrla
        raise AssertionError(f"unexpected read16 at 0x{address:08X}")

    def write16(self, address, value):
        if address == NVM.ctrla:
            self.ctrla = value
        elif address == NVM.intflag:
            self.intflag &= ~value
        elif address == NVM.ctrlb:
            self._command(value)
        else:
            raise AssertionError(f"unexpected write16 at 0x{address:08X}")

    def read32(self, address):
        if address == LAYOUT.did_address:
            return self.DID
        if address in fp.SERIAL_WORDS:
            return self.SERIAL[fp.SERIAL_WORDS.index(address)]
        raise AssertionError(f"unexpected read32 at 0x{address:08X}")

    def write32(self, address, value):
        if address == NVM.addr:
            self.addr = value
        elif self._in_page(address):
            for i, byte in enumerate(value.to_bytes(4, "little")):
                self.buffer[address + i] = byte
            self.addr = address         # DS60001507N 25.8.8: page buffer writes move ADDR
        else:
            raise AssertionError(f"unexpected write32 at 0x{address:08X}")

    def read_memory_block8(self, address, length):
        assert address == NVM.page_start and length == NVM.page_size
        return list(self.page)

    def _command(self, value):
        key, code = value >> 8, value & 0x7F
        offset = self.addr - NVM.page_start
        if key != NVM.key or self.fail_commands:
            self.intflag |= 0x4         # PROGE
            return
        if code == NVM.page_buffer_clear:
            self.buffer.clear()
            self.log.append(("PBC", None))
        elif code == NVM.erase_page:
            self.page[:] = bytes([0xFF if self.erase_leaves is None else self.erase_leaves]) * NVM.page_size
            self.log.append(("EP", offset))
        elif code == NVM.write_quad_word:
            self.log.append(("WQW", offset))
            if offset != self.lose_quad_word_at:
                for i in range(fp.QUAD_WORD):
                    self.page[offset + i] &= self.buffer.pop(self.addr + i, 0xFF)
        else:
            raise AssertionError(f"unexpected NVMCTRL command 0x{code:02X}")
        self.intflag |= 0x1             # DONE


def check(condition, message):
    if not condition:
        print(f"FAIL: {message}")
        sys.exit(1)


def expect_error(error, call, fragment):
    try:
        call()
    except error as exc:
        check(fragment in str(exc), f"the error '{exc}' does not mention '{fragment}'")
        return
    check(False, f"no {error.__name__}, expected one mentioning '{fragment}'")


def test_register_map_comes_from_the_atdf():
    check((NVM.ctrla, NVM.ctrlb, NVM.intflag, NVM.status, NVM.addr) ==
          (0x41004000, 0x41004004, 0x41004010, 0x41004012, 0x41004014), "NVMCTRL register addresses")
    check((NVM.command(NVM.erase_page), NVM.command(NVM.write_quad_word), NVM.command(NVM.page_buffer_clear)) ==
          (0xA500, 0xA504, 0xA515), "command words with the key")
    check(NVM.errors == 0xCE and NVM.all_flags == 0x7FF, f"error mask 0x{NVM.errors:x}, all flags 0x{NVM.all_flags:x}")
    check((NVM.page_start, NVM.page_size) == (0x00804000, 512), "User Row page")
    check(NVM.wmode_mask == 0x30 and NVM.wmode_manual == 0, "manual write mode")
    check(NVM.verified(), "the verified NVMCTRL is recognised")


def test_another_nvmctrl_is_not_verified():
    other = fp.Nvm(fuses.parse_atdf(ATDF.replace(b'name="NVMCTRL" id="U2409"', b'name="NVMCTRL" id="U9999"')).root)
    check(not other.verified(), "an NVMCTRL the sequence was not verified on must not count as verified")


def test_assignments():
    check(fp.parse_assignments(LAYOUT, ["wdt_per=cyc16384"]) == {"WDT_PER": 0xB}, "value name, any case")
    check(fp.parse_assignments(LAYOUT, ["WDT_ENABLE=1", "BOD33_ACTION=RESET"]) == {"WDT_ENABLE": 1, "BOD33_ACTION": 1},
          "number and value name")
    check(fp.parse_assignments(LAYOUT, ["NVMCTRL_BOOTPROT=0xE"]) == {"NVMCTRL_BOOTPROT": 0xE}, "bootloader size in hex")
    expect_error(ValueError, lambda: fp.parse_assignments(LAYOUT, ["NVMCTRL_BOOTPROT=8"]), "ambiguous")
    expect_error(ValueError, lambda: fp.parse_assignments(LAYOUT, ["WDT_PER=0xC"]), "not a defined value")
    expect_error(ValueError, lambda: fp.parse_assignments(LAYOUT, ["WDT_ENABLE=2"]), "does not fit")
    expect_error(ValueError, lambda: fp.parse_assignments(LAYOUT, ["BOD12_CALIB=0"]), "no such fuse field")
    expect_error(ValueError, lambda: fp.parse_assignments(LAYOUT, ["WDT_PER"]), "expected FIELD=VALUE")
    expect_error(ValueError, lambda: fp.parse_assignments(LAYOUT, ["WDT_PER=0xA", "WDT_PER=0xB"]), "set twice")


def test_compose_never_touches_undescribed_bits():
    from_image = fp.compose(LAYOUT, PAGE, fp.changes_from_image(LAYOUT, base.IMAGE))
    check(from_image == PAGE, "the project's HEX fuses on this board must change nothing - its zeros are placeholders")
    changed = fp.compose(LAYOUT, PAGE, {"WDT_PER": 0xB})
    check([i for i in range(512) if changed[i] != PAGE[i]] == [6], "WDT_PER=0xB changes byte 6 only")
    check(changed[6] == 0xAC, f"byte 6 is 0x{changed[6]:02X}")
    everything = fp.compose(LAYOUT, PAGE, {f.name: 0 for reg in LAYOUT.registers for f in reg.fields})
    for reg in LAYOUT.registers:
        old = int.from_bytes(PAGE[reg.offset:reg.offset + reg.size], "little")
        new = int.from_bytes(everything[reg.offset:reg.offset + reg.size], "little")
        check((old & reg.undescribed) == (new & reg.undescribed), f"{reg.name}: undescribed bits must survive")
    check(everything[LAYOUT.length:] == PAGE[LAYOUT.length:], "the free bytes behind the fuses must survive")
    expect_error(ValueError, lambda: fp.changes_from_image(LAYOUT, [None] * 12), "no complete USER_WORD_0")


def test_write_programs_the_page_with_explicit_addresses():
    board = FakeBoard(PAGE)
    new = fp.compose(LAYOUT, PAGE, {"WDT_PER": 0xB})
    fp.write_user_page(board, NVM, new)
    check(bytes(board.page) == new, "the simulated page holds the new page")
    check(board.ctrla == 0x0504, f"CTRLA restored, is 0x{board.ctrla:04X}")
    check(board.log[:2] == [("PBC", None), ("EP", 0)], f"starts with page buffer clear and erase: {board.log[:2]}")
    offsets = [offset for command, offset in board.log if command == "WQW"]
    expected = [o for o in range(0, 512, 16) if new[o:o + 16] != b"\xff" * 16]
    check(offsets == expected, f"one WQW per non-blank quad word, each at its own address: {offsets}")


def test_a_failing_command_raises_and_restores_ctrla():
    board = FakeBoard(PAGE)
    board.fail_commands = True
    expect_error(fp.NvmError, lambda: fp.write_user_page(board, NVM, PAGE), "INTFLAG = 0x0004")
    check(board.ctrla == 0x0504, "CTRLA restored after a failed command")


def test_an_erase_that_leaves_data_stops_before_writing():
    board = FakeBoard(PAGE)
    board.erase_leaves = 0x00
    expect_error(fp.NvmError, lambda: fp.write_user_page(board, NVM, PAGE), "not blank")
    check(not [c for c, _ in board.log if c == "WQW"], "nothing written into a page that is not blank")


def test_a_lost_quad_word_fails_the_read_back():
    board = FakeBoard(PAGE)
    board.lose_quad_word_at = 0
    expect_error(fp.NvmError, lambda: fp.write_user_page(board, NVM, PAGE), "first at +0")


def test_serial_number():
    check(fp.read_serial(FakeBoard(PAGE)) == "11111111222222223333333344444444", "128-bit serial, word 0 first")


def test_backups():
    serial = fp.read_serial(FakeBoard(PAGE))
    with tempfile.TemporaryDirectory() as tmp:
        when = datetime.datetime(2026, 9, 11, 15, 30, 0)
        path = fp.save_backup(PAGE, "ATSAME54P20A", FakeBoard.DID, serial, NVM.page_start, directory=tmp, now=when)
        check(path.name == f"ATSAME54P20A_{serial}_20260911-153000.json", path.name)
        record, page = fp.load_backup(path, NVM.page_size)
        check(page == PAGE, "the backup restores the page byte for byte")
        fp.check_restore_target(record, "ATSAME54P20A", serial)
        expect_error(ValueError, lambda: fp.check_restore_target(record, "ATSAME54P20A", "0" * 32),
                     "another chip's factory calibration")
        expect_error(ValueError, lambda: fp.check_restore_target(record, "ATSAME54N20A", serial), "this board is a")

        short = Path(tmp) / "short.json"
        short.write_text(json.dumps(dict(record, page=PAGE[:100].hex())), encoding="utf-8")
        expect_error(ValueError, lambda: fp.load_backup(short, NVM.page_size), "holds 100 bytes")
        foreign = Path(tmp) / "foreign.json"
        foreign.write_text(json.dumps({"page": PAGE.hex()}), encoding="utf-8")
        expect_error(ValueError, lambda: fp.load_backup(foreign, NVM.page_size), "not a fuse backup")


def main():
    tests = [test_register_map_comes_from_the_atdf, test_another_nvmctrl_is_not_verified, test_assignments,
             test_compose_never_touches_undescribed_bits, test_write_programs_the_page_with_explicit_addresses,
             test_a_failing_command_raises_and_restores_ctrla, test_an_erase_that_leaves_data_stops_before_writing,
             test_a_lost_quad_word_fails_the_read_back, test_serial_number, test_backups]
    for test in tests:
        test()
        print(f"ok   {test.__name__}")
    print("All fuse programming tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
