#!/usr/bin/env python3
"""
Simulated-board test for bridge_core and both front ends. No hardware needed.

Why this exists: the connection layer and the command protocol used to live inside
bridge_gui_telnet.py and now live in bridge_core, shared with bridge_web_telnet.py.
That move is only safe as long as something keeps checking that both front ends
still read a full environment out of a realistic showenv - and "realistic" here
means the two cases that were actually measured against the board and cost a
session each to diagnose:

  * showenv arrives in TWO TCP segments, with the value lines up to a second
    behind the identity line. An idle-based cutoff fires in that gap and truncates
    the response to the identity line, after which every field comes back
    unmatched.
  * the console prints its fresh "> " prompt right after echoing the command,
    BEFORE running it - so for lan_read the prompt arrives before the value. Trust
    the prompt immediately and the response is an empty echo.

The FakeLink below reproduces both. Run it after touching anything in
bridge_core.CommandChannel, and before believing a change is harmless:

    python scripts\\test_bridge_core_sim.py

Exits non-zero on the first failing expectation, so it works in a build script.
"""

import sys
import threading
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import bridge_core                     # noqa: E402
import bridge_gui_telnet as tkgui      # noqa: E402
import bridge_web_telnet as web        # noqa: E402

# What the firmware's showenv prints, split where the real one splits.
SHOWENV_HEAD = (
    b"showenv\r\n"
    b"env id TIBR version 5 crc ok | firmware id TIBR version 5 bridge\r\n"
)
SHOWENV_TAIL = (
    b"eth0 ip 192.168.0.11 mask 255.255.255.0 gw 192.168.0.1 dns 192.168.0.1\r\n"
    b"eth1 ip 192.168.0.12 mask 255.255.255.0 gw 192.168.0.1 dns 192.168.0.1\r\n"
    b"eth0 mac 00:04:25:00:00:00\r\n"
    b"eth1 mac 00:04:25:00:00:01\r\n"
    b"plca id 5 count 8\r\n"
    b"mirror OFF at boot\r\n"
    b"sniffer OFF at boot\r\n"
    b"> "
)
SEGMENT_GAP_S = 1.0

_failures = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok ' if ok else 'FAIL'}] {label}: {got!r}")
    if not ok:
        _failures.append(f"{label}: got {got!r}, want {want!r}")


class FakeLink:
    """Stands in for bridge_core.TelnetLink - same write()/close(), same queue
    protocol ((port, "data", payload) tuples pushed from a background thread)."""

    def __init__(self, q):
        self.port = "fake-board"
        self.q = q
        self.closed = False

    def write(self, data):
        cmd = data.decode("latin-1").strip()
        threading.Thread(target=self._respond, args=(cmd,), daemon=True).start()

    def _respond(self, cmd):
        if cmd == "showenv":
            self.q.put((self.port, "data", SHOWENV_HEAD))
            time.sleep(SEGMENT_GAP_S)
            self.q.put((self.port, "data", SHOWENV_TAIL))
        elif cmd.startswith("lan_read"):
            # Prompt BEFORE the value, as the real console does it.
            addr = cmd.split()[-1]
            self.q.put((self.port, "data", cmd.encode() + b"\r\n> "))
            time.sleep(0.02)
            self.q.put((self.port, "data",
                        b"LAN865X Read OK: Addr=%s Value=0x0283A1\r\n" % addr.encode()))
        elif cmd == "meminfo":
            self.q.put((self.port, "data", cmd.encode() + b"\r\n"
                        b"C-runtime heap: total=65536 largest free block=41000\r\n"
                        b"TCP/IP heap: size=98304 free=51200 maxblock=40960 highwater=61000\r\n"
                        b"OK: meminfo\r\n"))
        else:
            self.q.put((self.port, "data", cmd.encode() + b"\r\nOK: done\r\n"))

    def close(self):
        self.closed = True


def test_command_channel():
    """The protocol on its own, without either front end."""
    print("bridge_core.CommandChannel")
    import queue
    q = queue.Queue()
    channel = bridge_core.CommandChannel()
    channel.link = FakeLink(q)

    # Stand in for a front end's queue pump.
    stop = threading.Event()

    def pump():
        while not stop.is_set():
            try:
                channel.dispatch(q.get(timeout=0.02))
            except queue.Empty:
                pass

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    try:
        out = channel.send("showenv", timeout_ms=5000)
        check("showenv survives the segment gap", "sniffer OFF at boot" in out, True)

        start = time.time()
        out = channel.send("lan_read 0x0000", timeout_ms=1500)
        took = time.time() - start
        value = bridge_core.ResponseParser().parse_register_read(out)
        check("lan_read gets the value, not the bare prompt", value, "0x0283A1")
        # The settle window is 150 ms; a read that waited out the full timeout would
        # mean the "OK:" marker check never fired.
        check("lan_read returns promptly", took < 1.0, True)
    finally:
        stop.set()
        t.join(timeout=1)


def test_web_front_end():
    print("bridge_web_telnet")
    link = FakeLink(web.session.result_queue)
    web.session.result_queue.put(("port_opened", link))
    web.pump()
    check("connected", web.session.view["connected"], True)

    web.read_environment()
    deadline = time.time() + SEGMENT_GAP_S + 5
    while time.time() < deadline and web.session.view["busy"]:
        web.pump()
        time.sleep(web.POLL_S)
    web.pump()

    check("all 14 fields read", len(web.session.values), 14)
    check("ip0", web.session.values.get("ip0"), "192.168.0.11")
    check("mac1", web.session.values.get("mac1"), "00:04:25:00:00:01")
    check("mirror ON/OFF mapped to 1/0", web.session.values.get("mirror"), "0")
    check("identity", (web.session.env_identity or {}).get("firmware_id"), "TIBR")
    check("identity line says the model fits",
          "model fits" in web.session.view["identity"], True)

    # The terminal must have seen the same bytes the command consumed - two
    # consumers, one copy each.
    term, cursor = web.session.term_since(0)
    check("terminal saw the response too", b"plca id 5 count 8" in term, True)
    check("a second browser tab catches up from zero",
          web.session.term_since(0)[0] == term, True)
    check("nothing new after the cursor", web.session.term_since(cursor)[0], b"")

    web.disconnect()
    check("disconnected", link.closed, True)


def test_tk_front_end():
    print("bridge_gui_telnet")
    root = tk.Tk()
    root.withdraw()
    app = tkgui.BridgeGUITelnet(root)
    try:
        app.port_link = FakeLink(app.result_queue)
        app.connected = True
        check("port_link property reaches the channel", app._channel.connected, True)

        app.read_all_bridge()
        deadline = time.time() + SEGMENT_GAP_S + 5
        while time.time() < deadline:
            root.update()          # process_queue runs on its own after() loop
            time.sleep(0.02)

        values = {k: v.get() for k, v in app.bridge_fields.items()}
        check("all 14 fields read", len([v for v in values.values() if v]), 14)
        check("ip0", values.get("ip0"), "192.168.0.11")
        check("mac1", values.get("mac1"), "00:04:25:00:00:01")
        check("sniffer ON/OFF mapped to 1/0", values.get("sniffer"), "0")
        check("identity", (app.env_identity or {}).get("eeprom_id"), "TIBR")
        check("terminal saw the response too",
              "plca id 5 count 8" in app.terminal_screen.text(), True)
    finally:
        root.destroy()


def test_pure_helpers():
    """The model-driven pieces both front ends draw from - no board involved."""
    print("bridge_core helpers")
    categories, meta = bridge_core.register_view(
        bridge_core.load_json_model(bridge_core.MODEL_FILE))
    check("register groups", len(categories), 6)
    check("registers", sum(len(a) for a in categories.values()), len(meta))
    check("every address has meta", all(a in meta for g in categories.values() for a in g), True)

    check("bitfield, single bit", bridge_core.decode_one_bitfield("0x03", "1"), "  = 1")
    # 0x0283A1 >> 13 = 20, & 0b111 = 4 - the T1STSTCTL test-mode field.
    check("bitfield, range", bridge_core.decode_one_bitfield("0x0283A1", "15:13"), "  = 4 (0x4)")
    # An unread register must decode to nothing at all, so a 0 is never mistaken
    # for a measurement.
    check("bitfield, nothing read", bridge_core.decode_one_bitfield("", "15:13"), "")
    check("bitfield, not hex", bridge_core.decode_one_bitfield("zzz", "0"), "")

    check("0x prefix added", bridge_core.normalize_register_value("A1"), "0xA1")
    check("0x prefix kept", bridge_core.normalize_register_value("0xA1"), "0xA1")

    # The client identity check that turns a 12-second dead end into one click.
    # Whichever state this checkout is in, the two must agree - a "problem" text
    # exactly when a file is missing, and it has to name the file.
    missing = bridge_core.missing_tls_files()
    problem = bridge_core.tls_identity_problem()
    check("a problem is reported exactly when a file is missing",
          problem is not None, bool(missing))
    if missing:
        check("the message names the missing file",
              all(str(p) in problem for p in missing), True)
        check("the message names the button that fixes it",
              "Certificates tab" in problem, True)
        print(f"       (this checkout is missing {len(missing)}: "
              f"{', '.join(p.name for p in missing)})")

    check("testmode, no timeout", bridge_core.testmode_command(2), "testmode 2")
    check("testmode, timeout", bridge_core.testmode_command(2, " 30 "), "testmode 2 30")
    check("testmode 0", bridge_core.testmode_command(0), "testmode 0")
    check("four test modes", len(bridge_core.TEST_MODES), 4)

    pct, caption = bridge_core.bootload_progress_text(3, "transfer", 512 * 1024, 1024 * 1024)
    check("bootload halfway through the transfer", round(pct), 46)
    check("bootload caption", caption, "3/7  transferring  512 KiB / 1024 KiB")
    pct, caption = bridge_core.bootload_progress_text(6, "reboot", 0, 0)
    check("bootload phase 6 keeps moving", pct, 92)

    # The web front end renders a whole register's detail block as one element;
    # the decoded value has to be in it, and the description has to be escaped.
    detail = web.register_detail_html(meta["0x000308FB"], "0x0283A1")
    check("detail block mentions a bit field", "[15:13]" in detail, True)
    check("detail block has no raw angle brackets from the model",
          "<script" not in detail.lower(), True)


def test_web_registers_and_memory():
    """The register bulk path and the memory overview, through the web front end."""
    print("bridge_web_telnet: registers and memory")
    link = FakeLink(web.session.result_queue)
    web.session.result_queue.put(("port_opened", link))
    web.pump()

    # A subset, so this stays a test and not a five-minute soak: the loop is the
    # same one, only shorter.
    full_meta = web.session.reg_meta
    subset = dict(list(full_meta.items())[:12])
    web.session.reg_meta = subset
    for addr in subset:
        web.session.reg_values[addr] = ""
    try:
        web.bulk_read_registers()
        deadline = time.time() + 30
        while time.time() < deadline and web.session.view["busy"]:
            web.pump()
            time.sleep(web.POLL_S)
        web.pump()
    finally:
        web.session.reg_meta = full_meta

    got = [a for a in subset if web.session.reg_values.get(a)]
    check("every register in the subset answered", len(got), len(subset))
    check("value parsed", web.session.reg_values[next(iter(subset))], "0x0283A1")
    check("busy cleared", web.session.view["busy"], False)

    # A single write does a read-back afterwards - a write that reports OK is not
    # proof the register took the value.
    addr = next(iter(subset))
    web.session.reg_values[addr] = "A5"
    web.write_register(addr)
    deadline = time.time() + 10
    while time.time() < deadline and web.session.reg_values[addr] == "A5":
        web.pump()
        time.sleep(web.POLL_S)
    check("write reads back", web.session.reg_values[addr], "0x0283A1")

    web.memory_overview()
    deadline = time.time() + 10
    while time.time() < deadline and "C-runtime heap" not in web.session.view["log"]:
        web.pump()
        time.sleep(web.POLL_S)
    check("memory overview parsed the heap",
          "65536 bytes total" in web.session.view["log"], True)
    check("memory overview parsed the TCP/IP heap",
          "high-water mark 61000" in web.session.view["log"], True)

    web.disconnect()


def test_mqtt_start_failure_is_visible():
    """A broker that refuses to start must SAY so, not sit on "starting...".

    The first version of the web front end caught only BrokerError and never
    reset the status, so a checkout without certs/mqtt/ showed "starting..."
    for good with the Start button still lit - the failure text went to a log
    pane that is not visible from the broker card. Found on 2026-09-08.
    """
    print("bridge_web_telnet: a failed broker start")
    if web.mqtt_broker is None:
        print("  [skip] amqtt not installed")
        return

    service = web.session.mqtt_service
    web.session.mqtt["status"] = "starting..."
    web.session.mqtt["ok"] = True

    # Whatever this checkout's state, drive the exact failure path: start() with
    # a broker identity that is not there raises, and that has to reach the
    # status line through the pump.
    import pki
    if pki.MQTT_BROKER_CERT_PATH.is_file():
        print("  (this checkout HAS a broker identity - testing the pump only)")
        web.post("mqtt_start_failed", "simulated failure")
    else:
        try:
            service.start(bind_ip="127.0.0.1", port=18883, timeout=2.0)
            check("start should have refused without an identity", True, False)
            service.stop()
            return
        except Exception as exc:                           # noqa: BLE001
            check("start refuses without a broker identity",
                  "identity" in str(exc).lower(), True)
            web.post("mqtt_start_failed", str(exc))

    web.pump()
    check("status no longer says starting",
          web.session.mqtt["status"].startswith("start failed"), True)
    check("status is flagged as an error", web.session.mqtt["ok"], False)
    check("the broker is not marked running", web.session.mqtt["running"], False)
    check("the log carries the reason", "ERROR:" in web.session.mqtt["log"], True)


def main():
    test_pure_helpers()
    test_command_channel()
    test_web_front_end()
    test_web_registers_and_memory()
    test_mqtt_start_failure_is_visible()
    test_tk_front_end()
    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print("  -", f)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
