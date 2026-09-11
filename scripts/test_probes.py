#!/usr/bin/env python3
"""
Offline test for probes.bat (scripts\\probes.py). No probe, no USB, no pack - stdlib only.

What is tested is everything probes.py decides by itself: which probe is marked as the
bench.json selection, which COM port and which board each probe gets - the board from
json\\boards\\, with broken, unrelated and probe-less files in the folder as on a real bench
(pki.py writes an empty probe_serial unless one is given) - the notes under the list, and
which device a DSU DID belongs to: revision and die ignored, anything else refused. The
device signature comes from the trimmed ATDF in test_fuses_same54.py.

    python scripts\\test_probes.py

Exits non-zero on the first failing expectation, so it works in a build script.
"""

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import fuses_same54 as fuses      # noqa: E402
import probes                     # noqa: E402
import test_fuses_same54 as base  # noqa: E402  - the trimmed ATSAME54P20A ATDF

BRIDGE, FOLLOWER_A, FOLLOWER_B = "ATML3264031800001049", "ATML3264031800001103", "ATML3264031800001290"


class FakeProbe:
    def __init__(self, unique_id, description="Atmel Corp. EDBG CMSIS-DAP"):
        self.unique_id = unique_id
        self.description = description


def check(condition, message):
    if not condition:
        print(f"FAIL: {message}")
        sys.exit(1)


def boards_folder(tmp):
    folder = Path(tmp)
    files = {
        "bridge-192-168-0-12.json": {"board_id": "bridge-192-168-0-12", "ip": "192.168.0.12",
                                     "probe_serial": BRIDGE, "key_pem": "SECRET"},
        "followerA-192-168-0-21.json": {"board_id": "followerA-192-168-0-21", "ip": "192.168.0.21",
                                        "probe_serial": FOLLOWER_A, "key_pem": "SECRET"},
        "followerB-192-168-0-31.json": {"board_id": "followerB-192-168-0-31", "ip": "192.168.0.31",
                                        "probe_serial": "", "key_pem": "SECRET"},
        "spare.json": {"board_id": "spare"},
    }
    for name, record in files.items():
        (folder / name).write_text(json.dumps(record), encoding="utf-8")
    (folder / "broken.json").write_text("{ not json", encoding="utf-8")
    return folder


def test_board_assignments():
    with tempfile.TemporaryDirectory() as tmp:
        boards, without_probe = probes.board_assignments(boards_folder(tmp))
        check(probes.board_assignments(Path(tmp) / "gone") == ({}, 0), "a missing folder means no assignments")
    check(boards == {BRIDGE: "bridge-192-168-0-12", FOLLOWER_A: "followerA-192-168-0-21"}, f"{boards}")
    check(without_probe == 2, f"an empty and a missing probe_serial both count as no probe, got {without_probe}")
    check(all("SECRET" not in board for board in boards.values()), "nothing but board_id may reach the list")


def test_rows_mark_the_selection_the_port_and_the_board():
    boards = {BRIDGE: "bridge-192-168-0-12", FOLLOWER_A: "followerA-192-168-0-21"}
    ports = {BRIDGE: "COM12", FOLLOWER_B: "COM7"}
    table = probes.rows([FakeProbe(FOLLOWER_B), FakeProbe(BRIDGE), FakeProbe(FOLLOWER_A)], BRIDGE, boards, ports)
    check([row[1] for row in table] == [BRIDGE, FOLLOWER_A, FOLLOWER_B], "sorted by probe serial")
    check([row[0] for row in table] == ["1", "2", "3"], "numbered")
    check([row[2] for row in table] == ["COM12", "-", "COM7"], "COM port by probe serial")
    check([row[4] for row in table] == ["selected", "-", "-"], "only the bench.json probe is marked")
    check([row[5] for row in table] == ["bridge-192-168-0-12", "followerA-192-168-0-21", "-"], "board by probe serial")


def test_notes():
    boards = {BRIDGE: "bridge-192-168-0-12", FOLLOWER_B: "followerB-192-168-0-31"}
    check(probes.notes({BRIDGE, FOLLOWER_B}, BRIDGE, boards) == [], "nothing to say when all is in order")
    missing = probes.notes({FOLLOWER_A}, BRIDGE, boards)
    check(any(f"bench.json selects {BRIDGE}, which is not connected" in n for n in missing), f"{missing}")
    check(f"not connected: bridge-192-168-0-12 (probe {BRIDGE})" in missing, f"{missing}")
    check(f"not connected: followerB-192-168-0-31 (probe {FOLLOWER_B})" in missing, f"{missing}")
    unselected = probes.notes({BRIDGE, FOLLOWER_A}, None, {})
    check(unselected == ["bench.json selects no probe - with several connected, flash.bat and fuses.bat need --probe"],
          f"{unselected}")
    check(probes.notes({BRIDGE}, None, {}) == [], "one probe and no selection needs no note")
    without = probes.notes({BRIDGE}, BRIDGE, {}, without_probe=3)
    check(len(without) == 1 and without[0].startswith("3 board file(s) in json"), f"{without}")


def test_identify_the_chip():
    layouts = [fuses.parse_atdf(base.ATDF)]
    check(probes.identify(0x61840300, layouts).device == "ATSAME54P20A", "the exact signature")
    check(probes.identify(0x61841400, layouts).device == "ATSAME54P20A", "another revision and die of the same chip")
    check(probes.identify(0x61840301, layouts) is None, "another device of the family is not this ATDF")
    check(probes.identify(0x10040000, layouts) is None, "a foreign chip is not named")


def test_list_output():
    table = probes.rows([FakeProbe(BRIDGE), FakeProbe(FOLLOWER_A)], BRIDGE, {BRIDGE: "bridge-192-168-0-12"},
                        {BRIDGE: "COM12", FOLLOWER_A: "COM13"})
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        probes.print_list(table, {BRIDGE: "chip: ATSAME54P20A (DSU DID 0x61840300), serial 69CACED9533346484E202020FF11061D"})
    lines = buffer.getvalue().splitlines()
    check(lines[0].split()[:3] == ["#", "Probe", "serial"], f"heading: {lines[0]}")
    check(all(text in lines[1] for text in (BRIDGE, "COM12", "selected", "bridge-192-168-0-12")), f"row: {lines[1]}")
    check(lines[2].strip().startswith("chip: ATSAME54P20A"), f"chip line under its probe: {lines[2]}")
    check(FOLLOWER_A in lines[3] and len(lines) == 4, f"no chip line for a probe without one: {lines}")
    check(max(len(line) for line in lines) < 120, "fits a 120-column console")


def main():
    tests = [test_board_assignments, test_rows_mark_the_selection_the_port_and_the_board, test_notes,
             test_identify_the_chip, test_list_output]
    for test in tests:
        test()
        print(f"ok   {test.__name__}")
    print("All probe list tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
