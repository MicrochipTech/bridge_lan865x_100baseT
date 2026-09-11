#!/usr/bin/env python3
"""
Offline test for the fuse decoding behind fuses.bat (scripts\\fuses_same54.py).
No board, no probe, no pack, no network - stdlib only.

fuses_same54.py takes every bit position from the device's ATDF. This test feeds it a
trimmed copy of the ATSAME54P20A ATDF from Microchip.SAME54_DFP 3.11.261 - the elements
the tool reads, with the USER_FUSES block and three of its value groups copied verbatim -
and two real byte sets:

  * BOARD - the User Row read from a SAME54 board on 2026-09-11.
  * IMAGE - what MCC's #pragma config for this project puts into the release HEX.

Both hold the same settings but differ in the bits the ATDF does not describe: the board
keeps factory values there, the HEX has zeros. The tool must call that "same" - reporting
it as a difference would invite someone to "fix" the factory calibration.

    python scripts\\test_fuses_same54.py

Exits non-zero on the first failing expectation, so it works in a build script.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import fuses_same54 as fuses  # noqa: E402

ATDF = b"""<?xml version="1.0" encoding="UTF-8"?>
<avr-tools-device-file>
  <devices>
    <device name="ATSAME54P20A" architecture="CORTEX-M4" family="SAME">
      <peripherals>
        <module name="DSU" id="U2410" version="1.0.0">
          <instance name="DSU">
            <register-group name="DSU" name-in-module="DSU" address-space="base" offset="0x41002000"/>
          </instance>
        </module>
        <module name="FUSES" id="U2409" version="1.0.0">
          <instance name="FUSES">
            <register-group name="USER_FUSES" name-in-module="USER_FUSES" address-space="fuses" offset="0x00804000"/>
          </instance>
        </module>
      </peripherals>
      <property-groups>
        <property-group name="SIGNATURES">
          <property name="DSU_DID" value="0x61840300"/>
        </property-group>
      </property-groups>
    </device>
  </devices>
  <modules>
    <module name="DSU" id="U2410" version="1.0.0" caption="Device Service Unit">
      <register-group name="DSU" caption="Device Service Unit">
        <register name="DID" offset="0x18" rw="R" size="4" initval="0x61840200" caption="Device Identification">
          <bitfield name="DEVSEL" caption="Device Select" mask="0xFF"/>
          <bitfield name="REVISION" caption="Revision Number" mask="0xF00"/>
          <bitfield name="DIE" caption="Die Number" mask="0xF000"/>
          <bitfield name="SERIES" caption="Series" mask="0x3F0000" values="DSU_DID__SERIES"/>
          <bitfield name="FAMILY" caption="Family" mask="0xF800000" values="DSU_DID__FAMILY"/>
          <bitfield name="PROCESSOR" caption="Processor" mask="0xF0000000" values="DSU_DID__PROCESSOR"/>
        </register>
      </register-group>
    </module>
    <module name="FUSES" id="U2409" version="1.0.0" caption="Non-Volatile Fuses">
      <register-group name="USER_FUSES">
        <register name="USER_WORD_0" offset="0x0" size="4" rw="RW" caption="USER Page Word 0">
          <bitfield name="BOD33_DIS" caption="BOD33 Disable" mask="0x1"/>
          <bitfield name="BOD33USERLEVEL" caption="BOD33 User Level" mask="0x1FE"/>
          <bitfield name="BOD33_ACTION" caption="BOD33 Action" mask="0x600" values="SUPC_BOD33__ACTION"/>
          <bitfield name="BOD33_HYST" caption="BOD33 Hysteresis" mask="0x7800"/>
          <bitfield name="NVMCTRL_BOOTPROT" caption="Bootloader Size" mask="0x3C000000" values="NVMCTRL_STATUS__BOOTPROT"/>
        </register>
        <register name="USER_WORD_1" offset="0x4" size="4" rw="RW" caption="USER Page Word 1">
          <bitfield name="NVMCTRL_SEESBLK" caption="Number Of Physical NVM Blocks Composing a SmartEEPROM Sector" mask="0xF"/>
          <bitfield name="NVMCTRL_SEEPSZ" caption="Size Of SmartEEPROM Page" mask="0x70"/>
          <bitfield name="RAMECC_ECCDIS" caption="RAM ECC Disable fuse" mask="0x80"/>
          <bitfield name="WDT_ENABLE" caption="WDT Enable" mask="0x10000"/>
          <bitfield name="WDT_ALWAYSON" caption="WDT Always On" mask="0x20000"/>
          <bitfield name="WDT_PER" caption="WDT Period" mask="0x3C0000" values="WDT_CONFIG__PER"/>
          <bitfield name="WDT_WINDOW" caption="WDT Window" mask="0x3C00000" values="WDT_CONFIG__WINDOW"/>
          <bitfield name="WDT_EWOFFSET" caption="WDT Early Warning Offset" mask="0x3C000000" values="WDT_EWCTRL__EWOFFSET"/>
          <bitfield name="WDT_WEN" caption="WDT Window Mode Enable" mask="0x40000000"/>
        </register>
        <register name="USER_WORD_2" offset="0x8" size="4" rw="RW" caption="USER Page Word 2">
          <bitfield name="NVMCTRL_REGION_LOCKS" caption="NVM Region Locks" mask="0xFFFFFFFF"/>
        </register>
      </register-group>
      <value-group name="SUPC_BOD33__ACTION">
        <value name="NONE" caption="No action" value="0x0"/>
        <value name="RESET" caption="The BOD33 generates a reset" value="0x1"/>
        <value name="INT" caption="The BOD33 generates an interrupt" value="0x2"/>
        <value name="BKUP" caption="The BOD33 puts the device in backup sleep mode" value="0x3"/>
      </value-group>
      <value-group name="NVMCTRL_STATUS__BOOTPROT">
        <value name="0" caption="0 kbytes" value="0xF"/>
        <value name="8" caption="8 kbytes" value="0xE"/>
        <value name="16" caption="16 kbytes" value="0xD"/>
        <value name="24" caption="24 kbytes" value="0xC"/>
        <value name="32" caption="32 kbytes" value="0xB"/>
        <value name="40" caption="40 kbytes" value="0xA"/>
        <value name="48" caption="48 kbytes" value="0x9"/>
        <value name="56" caption="56 kbytes" value="0x8"/>
        <value name="64" caption="64 kbytes" value="0x7"/>
        <value name="72" caption="72 kbytes" value="0x6"/>
        <value name="80" caption="80 kbytes" value="0x5"/>
        <value name="88" caption="88 kbytes" value="0x4"/>
        <value name="96" caption="96 kbytes" value="0x3"/>
        <value name="104" caption="104 kbytes" value="0x2"/>
        <value name="112" caption="112 kbytes" value="0x1"/>
        <value name="120" caption="120 kbytes" value="0x0"/>
      </value-group>
      <value-group name="WDT_CONFIG__PER">
        <value name="CYC8" caption="8 clock cycles" value="0x0"/>
        <value name="CYC16" caption="16 clock cycles" value="0x1"/>
        <value name="CYC32" caption="32 clock cycles" value="0x2"/>
        <value name="CYC64" caption="64 clock cycles" value="0x3"/>
        <value name="CYC128" caption="128 clock cycles" value="0x4"/>
        <value name="CYC256" caption="256 clock cycles" value="0x5"/>
        <value name="CYC512" caption="512 clock cycles" value="0x6"/>
        <value name="CYC1024" caption="1024 clock cycles" value="0x7"/>
        <value name="CYC2048" caption="2048 clock cycles" value="0x8"/>
        <value name="CYC4096" caption="4096 clock cycles" value="0x9"/>
        <value name="CYC8192" caption="8192 clock cycles" value="0xA"/>
        <value name="CYC16384" caption="16384 clock cycles" value="0xB"/>
      </value-group>
    </module>
  </modules>
</avr-tools-device-file>
"""

BOARD = list(bytes.fromhex("3992ffff 80ffa8aa ffffffff".replace(" ", "")))
IMAGE = list(bytes.fromhex("3912003c 8000a82a ffffffff".replace(" ", "")))

LAYOUT = fuses.parse_atdf(ATDF)


def check(condition, message):
    if not condition:
        print(f"FAIL: {message}")
        sys.exit(1)


def test_layout_comes_from_the_atdf():
    check(LAYOUT.device == "ATSAME54P20A", f"device {LAYOUT.device}")
    check(LAYOUT.base == 0x00804000 and LAYOUT.length == 12, f"base 0x{LAYOUT.base:x}, length {LAYOUT.length}")
    check([r.name for r in LAYOUT.registers] == ["USER_WORD_0", "USER_WORD_1", "USER_WORD_2"], "register names")
    check([r.undescribed for r in LAYOUT.registers] == [0xC3FF8000, 0x8000FF00, 0],
          f"undescribed bits {[hex(r.undescribed) for r in LAYOUT.registers]}")
    check(LAYOUT.did_address == 0x41002018, f"DID address 0x{LAYOUT.did_address:x}")
    check(LAYOUT.did_expected == 0x61840300 and LAYOUT.did_ignore == 0xFF00, "DID signature and ignored bits")


def test_board_decodes_to_the_project_settings():
    values = fuses.field_values(LAYOUT, BOARD)
    # initialization.c's #pragma config, field by field
    expected = {"BOD33_DIS": 1, "BOD33USERLEVEL": 0x1C, "BOD33_ACTION": 1, "BOD33_HYST": 2,
                "NVMCTRL_BOOTPROT": 0xF, "NVMCTRL_SEESBLK": 0, "NVMCTRL_SEEPSZ": 0, "RAMECC_ECCDIS": 1,
                "WDT_ENABLE": 0, "WDT_ALWAYSON": 0, "WDT_PER": 0xA, "WDT_WINDOW": 0xA, "WDT_EWOFFSET": 0xA,
                "WDT_WEN": 0, "NVMCTRL_REGION_LOCKS": 0xFFFFFFFF}
    got = {name: value for name, (_, value) in values.items()}
    check(got == expected, f"board decodes to {got}")
    words = {name: fuses.describe(field, value) for name, (field, value) in values.items()}
    check(words["NVMCTRL_BOOTPROT"] == "0xf  0 kbytes", words["NVMCTRL_BOOTPROT"])
    check(words["WDT_PER"] == "0xa  CYC8192", words["WDT_PER"])
    check(words["BOD33_ACTION"] == "0x1  RESET", words["BOD33_ACTION"])
    check(words["BOD33_DIS"] == "1  BOD33 off", words["BOD33_DIS"])
    check(words["NVMCTRL_REGION_LOCKS"] == "0xffffffff  all unlocked", words["NVMCTRL_REGION_LOCKS"])
    check(fuses.power_on_warnings(LAYOUT, BOARD) == [], "no warnings for the project's settings")


def test_undescribed_bits_are_not_a_difference():
    check(BOARD != IMAGE, "fixture: the raw bytes must differ")
    check(fuses.differences(LAYOUT, BOARD, IMAGE) == [], f"{fuses.differences(LAYOUT, BOARD, IMAGE)}")


def test_a_real_difference_is_reported():
    image = list(IMAGE)
    image[6] = 0xAC                     # WDT_PER 0xA -> 0xB, the harmless change tested on the board
    check(fuses.differences(LAYOUT, BOARD, image) == ["WDT_PER"], f"{fuses.differences(LAYOUT, BOARD, image)}")
    field = fuses.field_values(LAYOUT, image)["WDT_PER"][0]
    check(fuses.describe(field, 0xB) == "0xb  CYC16384", fuses.describe(field, 0xB))


def test_power_on_warnings():
    data = list(BOARD)
    data[6] |= 0x03                     # WDT_ENABLE and WDT_ALWAYSON, bits 16 and 17 of word 1
    data[3] = (data[3] & ~0x3C) | (0xE << 2)    # NVMCTRL_BOOTPROT 0xE = 8 kbytes, bits 26..29 of word 0
    data[8] &= 0xFE                     # lock region 0
    warnings = fuses.power_on_warnings(LAYOUT, data)
    check(any("ALWAYS-ON" in w for w in warnings), f"{warnings}")
    check(any("first 8 kbytes" in w for w in warnings), f"{warnings}")
    check(any("1 of 32 flash regions" in w for w in warnings), f"{warnings}")


def test_missing_image_bytes_are_not_a_difference():
    image = list(IMAGE)
    image[4:8] = [None] * 4
    check(fuses.differences(LAYOUT, BOARD, image) == [], "a partial image must not produce differences")
    field, value = fuses.field_values(LAYOUT, image)["WDT_PER"]
    check(value is None and fuses.describe(field, value) == "-", "missing bytes show as '-'")


def test_device_check():
    check(fuses.did_matches(LAYOUT, 0x61840400), "another revision of the same chip must match")
    check(fuses.did_matches(LAYOUT, 0x61841300), "another die of the same chip must match")
    check(not fuses.did_matches(LAYOUT, 0x61840301), "a different DEVSEL must not match")


def test_read_image_from_hex():
    try:
        from intelhex import IntelHex
    except ImportError:
        print("  skipped: intelhex not installed (it comes with pyOCD)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        with_fuses = IntelHex()
        with_fuses.frombytes(b"\x00\x01\x02\x03", offset=0)
        with_fuses.frombytes(bytes(IMAGE), offset=0x00804000)
        path = Path(tmp) / "with_fuses.hex"
        with_fuses.write_hex_file(str(path))
        check(fuses.read_image(path, LAYOUT) == IMAGE, "fuse bytes read back from a HEX")

        without = IntelHex()
        without.frombytes(b"\x00\x01\x02\x03", offset=0)
        path = Path(tmp) / "without_fuses.hex"
        without.write_hex_file(str(path))
        check(all(b is None for b in fuses.read_image(path, LAYOUT)), "an image without fuses reads as all None")


def test_verbose_table_fits_120_columns():
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        fuses.print_table(LAYOUT, [("Board", BOARD), ("Image", IMAGE)], verbose=True)
    longest = max(buffer.getvalue().splitlines(), key=len)
    check(len(longest) < 120, f"{len(longest)} characters wrap in a 120-column console: {longest.strip()}")


def main():
    tests = [test_layout_comes_from_the_atdf, test_board_decodes_to_the_project_settings,
             test_undescribed_bits_are_not_a_difference, test_a_real_difference_is_reported,
             test_power_on_warnings, test_missing_image_bytes_are_not_a_difference, test_device_check,
             test_read_image_from_hex, test_verbose_table_fits_120_columns]
    for test in tests:
        test()
        print(f"ok   {test.__name__}")
    print("All fuse decoding tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
