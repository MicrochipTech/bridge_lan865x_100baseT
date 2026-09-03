#!/usr/bin/env python3
"""
iperf_matrix_test.py - iperf UDP/TCP test matrix across the T1S bridge setup.

Ported from a related in-house project (2026-08-28), adapted for this
project's own bench addresses.

Tests every directed pair among the four nodes:
    PC, Bridge, Follower A, Follower B
for two things:
    - "UDP Max": an ascending search over candidate bitrates, stopping at
      the first one with too much loss; reports the highest rate that
      still came through cleanly.
    - "TCP": a single throughput measurement (TCP self-tunes its own
      rate, no search needed).

Bridge and Follower A/B are controlled over their EDBG virtual COM port
using the same "iperf"/"iperfk" CLI commands documented in this project's
CLAUDE.md. The PC uses the real iperf2 binary (jperf-2.0.2/bin/iperf.exe).

Trust rule (important, carried over verbatim from that project's own notes,
2026-08-27): the embedded iperf CLIENT's own self-reported UDP loss is
meaningless - it never receives real feedback from the far end, so it
always prints ~0% loss regardless of what actually arrived. Only a
receiver (the destination, running as iperf SERVER) that actually counts
incoming sequence numbers/bytes gives a trustworthy number. This script
therefore always reads the result from the DESTINATION side of each test:
    - destination is a device (Bridge/Follower)  -> its own "iperf -s"
      interval/session report, captured live over its COM port while
      the test runs.
    - destination is the PC                      -> the real iperf.exe
      server's own report.

Usage:
    python iperf_matrix_test.py
    python iperf_matrix_test.py --log my_run.log --udp-duration 3 --tcp-duration 5
    python iperf_matrix_test.py --pairs "PC->Bridge,FollowerA->FollowerB"

Output:
    - Progress is printed to the screen as each test runs.
    - Full details (every rate step tried, raw captured device output,
      parsed result) are appended to the log file (default:
      docs/iperf_matrix_results.log) as they complete, so a run that's
      interrupted partway still leaves a usable log.

Windows timer resolution (carried over verbatim from the same notes,
2026-08-28): the real iperf.exe's UDP client paces packets via a sleep loop,
and Windows' default ~15.6 ms timer resolution makes it fall behind and
catch up in bursts of several packets within under a millisecond - the
long-run rate matches the target but the momentary overload causes real
loss that has nothing to do with the bridge/T1S link. This script therefore
requests 1 ms system timer resolution via winmm.timeBeginPeriod(1) for its
whole run.
"""
import argparse
import ctypes
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import serial  # pyserial

# --- Node configuration -----------------------------------------------
# COM ports / IPs as verified live on this bench, 2026-09-03 (see
# CLAUDE.md for the hardware setup). Override on the command line if your
# bench differs.
#
# All three boards now run the same bridge firmware and therefore have TWO
# interfaces each - this used to be true of the Bridge alone, and the
# per-destination interface handling below is no longer a special case for it.
# What differs is which interface actually carries traffic:
#
#   "ip"        the address other nodes aim at
#   "eth0"      T1S side, "eth1" 100BASE-TX side - the board's own addresses
#   "toward_pc" the interface facing the PC's 100BASE-TX segment; for a
#               follower that is still eth0, because it reaches the PC
#               through the T1S segment and the bridge, not on its own eth1

DEVICES = {
    "Bridge": {
        "port": "COM8", "ip": "192.168.0.12",
        "eth0": "192.168.0.11", "eth1": "192.168.0.12",
        "toward_pc": "eth1",   # wired straight to the PC
    },
    "FollowerA": {
        "port": "COM10", "ip": "192.168.0.21",
        "eth0": "192.168.0.21", "eth1": "192.168.0.22",
        "toward_pc": "eth0",   # eth1 has a PHY but no cable
    },
    "FollowerB": {
        "port": "COM23", "ip": "192.168.0.31",
        "eth0": "192.168.0.31", "eth1": None,
        "toward_pc": "eth0",   # no 100BASE-TX PHY fitted at all
    },
}
PC_IP = "192.168.0.100"
ALL_NODES = ["PC", "Bridge", "FollowerA", "FollowerB"]

PAIRS = [
    ("PC", "Bridge"), ("PC", "FollowerA"), ("PC", "FollowerB"),
    ("Bridge", "PC"), ("Bridge", "FollowerA"), ("Bridge", "FollowerB"),
    ("FollowerA", "PC"), ("FollowerA", "Bridge"), ("FollowerA", "FollowerB"),
    ("FollowerB", "PC"), ("FollowerB", "Bridge"), ("FollowerB", "FollowerA"),
]

UDP_RATE_STEPS_MBIT = [1, 2, 5, 8, 10, 20, 50, 80]
UDP_LOSS_THRESHOLD_PCT = 2.0
IPERF_PORT = 5001

DEFAULT_IPERF_EXE = r"C:\work\t1s_bridge\jperf-2.0.2\bin\iperf.exe"

# --- Logging -------------------------------------------------------------

_log_file = None


def log_open(path):
    global _log_file
    _log_file = open(path, "a", encoding="utf-8")
    _log_file.write(f"\n\n===== run started {datetime.now().isoformat()} =====\n")
    _log_file.flush()


def log(msg=""):
    print(msg)
    if _log_file:
        _log_file.write(msg + "\n")
        _log_file.flush()


# --- Embedded device control (serial CLI) --------------------------------

def _drain(ser, seconds):
    out = bytearray()
    floor = time.time() + seconds
    deadline = floor
    while time.time() < deadline:
        n = ser.in_waiting
        if n:
            out += ser.read(n)
            deadline = max(floor, time.time() + 0.3)
        else:
            time.sleep(0.02)
    return out.decode("utf-8", errors="replace")


def _send_cmd(ser, cmd):
    ser.write((cmd + "\r\n").encode("ascii"))
    ser.flush()


class DeviceServerCapture(threading.Thread):
    """Starts an iperf server on a device and keeps the connection open
    for `duration` seconds to capture everything it prints (its own
    interval/session reports), then stops the session. Runs in a thread
    so it can overlap with the client side running on another node."""

    def __init__(self, port, udp, duration):
        super().__init__()
        self.port = port
        self.udp = udp
        self.duration = duration
        self.output = ""
        self.error = None

    def run(self):
        try:
            with serial.Serial(self.port, 115200, timeout=0.1) as ser:
                ser.reset_input_buffer()
                ser.write(b"\r\n")
                time.sleep(0.2)
                ser.reset_input_buffer()
                # defensive: a previous test's session (e.g. one that hung
                # waiting on an ARP reply that never came) can otherwise
                # still be sitting on the single session slot, and this
                # server would never even start ("All instances busy.
                # Retry later!") with no other symptom.
                _send_cmd(ser, "iperfk -i 0")
                time.sleep(0.6)
                ser.read(ser.in_waiting)
                cmd = "iperf -s" + (" -u" if self.udp else "")
                _send_cmd(ser, cmd)
                # small settle so the server is bound before the client fires
                time.sleep(0.3)
                self.output = _drain(ser, self.duration)
                # stop the session and grab any trailing output (final
                # session-report line often prints right as/after "Tx done")
                _send_cmd(ser, "iperfk -i 0")
                self.output += _drain(ser, 1.0)
        except Exception as exc:  # noqa: BLE001 - report, don't crash the run
            self.error = str(exc)


def device_run_client(port, dest_ip, udp, rate_bps, duration, extra="", bind_ip=None):
    """Fire the client command on a device. Its own printed summary is
    NOT trusted for UDP loss (see module docstring) but is still logged
    for reference, and this blocks long enough for the destination's
    server capture to see the whole exchange.

    bind_ip pins the outgoing interface via "iperfi" first - only the
    Bridge has more than one interface, so this only matters for it
    (see bind_addr_for): without it "iperf -c" silently
    uses "the default interface", which is wrong whenever the target
    sits on the Bridge's other side and the session just hangs forever
    waiting on an ARP that can never resolve.
    """
    try:
        with serial.Serial(port, 115200, timeout=0.1) as ser:
            ser.reset_input_buffer()
            ser.write(b"\r\n")
            time.sleep(0.2)
            ser.reset_input_buffer()
            # defensive: clear any session left hanging by a previous test
            # (e.g. one that never got an ARP reply) before starting a new
            # one, otherwise "iperf -c" fails outright with "All instances
            # busy. Retry later!"
            _send_cmd(ser, "iperfk -i 0")
            time.sleep(0.6)
            ser.read(ser.in_waiting)
            if bind_ip:
                _send_cmd(ser, f"iperfi -a {bind_ip} -i 0")
                time.sleep(0.3)
                ser.read(ser.in_waiting)
            cmd = f"iperf -c {dest_ip}"
            if udp:
                cmd += f" -u -b {rate_bps}"
            cmd += f" -t {duration}"
            if extra:
                cmd += " " + extra
            _send_cmd(ser, cmd)
            return _drain(ser, duration + 2)
    except Exception as exc:  # noqa: BLE001
        return f"<client error: {exc}>"


def device_get_ip(name):
    port = DEVICES[name]["port"]
    with serial.Serial(port, 115200, timeout=0.1) as ser:
        ser.reset_input_buffer()
        ser.write(b"\r\n")
        time.sleep(0.2)
        ser.reset_input_buffer()
        _send_cmd(ser, "showenv")
        out = _drain(ser, 1.5)
    ips = re.findall(r"\bip\s+(\d+\.\d+\.\d+\.\d+)", out)
    if not ips:
        raise RuntimeError(f"{name}: no IP found in showenv output:\n{out}")
    # Bridge prints eth0 then eth1 - the configured DEVICES[name]["ip"]
    # tells us which one we actually mean (eth1/100BASE-TX for the Bridge).
    want = DEVICES[name]["ip"]
    return want if want in ips else ips[0]


# --- PC control (real iperf2) ---------------------------------------------

class PCServerCapture(threading.Thread):
    def __init__(self, iperf_exe, udp, duration):
        super().__init__()
        self.iperf_exe = iperf_exe
        self.udp = udp
        self.duration = duration
        self.output = ""

    def run(self):
        cmd = [self.iperf_exe, "-s", "-B", PC_IP, "-p", str(IPERF_PORT), "-i", "1"]
        if self.udp:
            cmd.append("-u")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True)
        time.sleep(self.duration + 1.5)
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        self.output = out or ""


def pc_run_client(iperf_exe, dest_ip, udp, rate_bps, duration):
    # -B pins the source address: this PC has more than one NIC in the
    # 192.168.0.0/24 range - without it Windows routing can silently pick
    # the wrong interface and the packets never reach the bridge/T1S
    # segment at all.
    cmd = [iperf_exe, "-c", dest_ip, "-B", PC_IP, "-p", str(IPERF_PORT), "-t", str(duration)]
    if udp:
        cmd += ["-u", "-b", str(rate_bps)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=duration + 5)
        return res.stdout + res.stderr
    except subprocess.TimeoutExpired as exc:
        return f"<client timeout: {exc}>"


def pc_kill_stray_iperf():
    subprocess.run(["taskkill", "/F", "/IM", "iperf.exe"],
                    capture_output=True, text=True)


# --- Result parsing --------------------------------------------------------

def parse_device_report(text):
    """Parse the LAST '[0.0- X.X sec] drop/total (loss%) Kbps' line from an
    embedded iperf server's captured output (both UDP and TCP servers
    print this - for TCP, drop/loss are always 0 since it's not tracked
    the same way, the Kbps figure is what matters there)."""
    matches = re.findall(
        r"\[\s*0\.0-\s*([\d.]+)\s*sec\]\s*(\d+)/\s*(\d+)\s*\(\s*(\d+)%\)\s*(\d+)\s*Kbps",
        text)
    if not matches:
        return None
    _, dropped, total, loss_pct, kbps = matches[-1]
    return {
        "mbit_s": float(kbps) / 1000.0,
        "loss_pct": float(loss_pct),
        "dropped": int(dropped),
        "total": int(total),
    }


def parse_pc_report(text, udp):
    """Parse the final summary line from a real iperf2 server's output."""
    if udp:
        m = re.findall(
            r"([\d.]+)\s*Mbits/sec\s*[\d.]+\s*ms\s*(\d+)/\s*(\d+)\s*\((\d+(?:\.\d+)?)%\)",
            text)
        if not m:
            return None
        mbit, dropped, total, loss_pct = m[-1]
        return {"mbit_s": float(mbit), "loss_pct": float(loss_pct),
                "dropped": int(dropped), "total": int(total)}
    else:
        m = re.findall(r"([\d.]+)\s*Mbits/sec", text)
        if not m:
            return None
        return {"mbit_s": float(m[-1]), "loss_pct": 0.0, "dropped": 0, "total": 0}


# --- Test orchestration -----------------------------------------------------

def get_ip(name, cache):
    if name == "PC":
        return PC_IP
    if name not in cache:
        cache[name] = device_get_ip(name)
    return cache[name]


def dest_addr_for(src, dst, cache):
    """Which of dst's addresses src should aim at.

    Boards reach each other across the T1S segment, so a board addresses
    another board's eth0 - aiming at a two-homed board's eth1 would ask the
    traffic to take a detour it may not have. Only the PC comes in from the
    100BASE-TX side, and it uses the canonical address.
    """
    if dst == "PC":
        return PC_IP
    if src == "PC":
        return get_ip(dst, cache)
    return DEVICES[dst]["eth0"]


def bind_addr_for(src, dst):
    """The source address to pin on src's iperf client, or None for the PC.

    Every board here has two interfaces, so "the default interface" is only
    the right one by accident - it has to be named per destination.
    """
    if src == "PC":
        return None
    dev = DEVICES[src]
    return dev[dev["toward_pc"]] if dst == "PC" else dev["eth0"]


def run_one(src, dst, udp, rate_bps, duration, iperf_exe, ip_cache):
    """Run a single client/server exchange src -> dst, return the parsed
    result from the DESTINATION's own report plus the raw text for the log."""
    dst_ip = dest_addr_for(src, dst, ip_cache)

    # The server's capture window must outlast the client by a comfortable
    # margin on both ends: the 0.8s head start below before the client
    # fires, plus the trailing summary line the device/iperf server only
    # prints a moment after the last data arrives. Too short and the final
    # "[0.0- X sec] ..." report line gets cut off -> parse_*_report()
    # silently returns None.
    server_capture_s = duration + 3.0

    if dst == "PC":
        pc_kill_stray_iperf()
        server = PCServerCapture(iperf_exe, udp, server_capture_s)
    else:
        server = DeviceServerCapture(DEVICES[dst]["port"], udp, server_capture_s)
    server.start()
    time.sleep(0.8)  # let the server bind before the client fires

    if src == "PC":
        client_out = pc_run_client(iperf_exe, dst_ip, udp, rate_bps, duration)
    else:
        bind_ip = bind_addr_for(src, dst)
        client_out = device_run_client(DEVICES[src]["port"], dst_ip, udp,
                                        rate_bps, duration, bind_ip=bind_ip)

    server.join(timeout=duration + 5)
    dst_out = server.output
    if getattr(server, "error", None):
        dst_out += f"\n<server thread error: {server.error}>"

    # Give the destination a moment to fully release the just-finished
    # iperf session before the next test tries to start a new one -
    # back-to-back "iperf -s" calls right after "iperfk" occasionally hit
    # the device mid-teardown and the new session silently produces no
    # report at all.
    time.sleep(1.5)

    if dst == "PC":
        result = parse_pc_report(dst_out, udp)
    else:
        result = parse_device_report(dst_out)

    return result, client_out, dst_out


def test_tcp(src, dst, duration, iperf_exe, ip_cache):
    log(f"  TCP {src} -> {dst} ({duration}s) ...")
    result, client_out, dst_out = run_one(src, dst, False, None, duration,
                                           iperf_exe, ip_cache)
    if not result:
        log("    client output:\n" + "\n".join(
            "      " + l for l in client_out.splitlines() if l.strip()))
    log(f"    destination report:\n" + "\n".join(
        "      " + l for l in dst_out.splitlines() if l.strip()))
    if result:
        log(f"    => {result['mbit_s']:.2f} Mbit/s")
    else:
        log("    => NO RESULT (parse failed or nothing received)")
    return result


def test_udp_max(src, dst, duration, iperf_exe, ip_cache):
    log(f"  UDP max {src} -> {dst} (steps: {UDP_RATE_STEPS_MBIT} Mbit/s, "
        f"{duration}s each) ...")
    best = None
    for rate_mbit in UDP_RATE_STEPS_MBIT:
        rate_bps = rate_mbit * 1_000_000
        result, client_out, dst_out = run_one(src, dst, True, rate_bps, duration,
                                               iperf_exe, ip_cache)
        if result is None:
            log(f"    {rate_mbit:>4} Mbit/s -> NO RESULT, stopping search")
            log("    client output:\n" + "\n".join(
                "      " + l for l in client_out.splitlines() if l.strip()))
            log("    destination output:\n" + "\n".join(
                "      " + l for l in dst_out.splitlines() if l.strip()))
            break
        log(f"    {rate_mbit:>4} Mbit/s -> {result['mbit_s']:.2f} Mbit/s, "
            f"{result['loss_pct']:.1f}% loss "
            f"({result['dropped']}/{result['total']})")
        if result["loss_pct"] <= UDP_LOSS_THRESHOLD_PCT:
            best = (rate_mbit, result)
        else:
            log(f"    -> loss above {UDP_LOSS_THRESHOLD_PCT}% threshold, "
                f"stopping search")
            break
    if best:
        rate_mbit, result = best
        log(f"    => MAX clean rate: requested {rate_mbit} Mbit/s, "
            f"measured {result['mbit_s']:.2f} Mbit/s, {result['loss_pct']:.1f}% loss")
    else:
        log("    => no rate came through cleanly (even the lowest step failed)")
    return best[1] if best else None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default="docs/iperf_matrix_results.log")
    ap.add_argument("--iperf-exe", default=DEFAULT_IPERF_EXE)
    ap.add_argument("--udp-duration", type=float, default=3.0,
                     help="seconds per UDP rate step")
    ap.add_argument("--tcp-duration", type=float, default=5.0,
                     help="seconds per TCP test")
    ap.add_argument("--pairs", default=None,
                     help="comma-separated subset, e.g. 'PC->Bridge,FollowerA->FollowerB' "
                          "(default: all 12 directions)")
    args = ap.parse_args()

    if not Path(args.iperf_exe).exists():
        log(f"ERROR: iperf.exe not found at {args.iperf_exe} (--iperf-exe to override)")
        return 1

    pairs = PAIRS
    if args.pairs:
        pairs = []
        for chunk in args.pairs.split(","):
            a, b = chunk.split("->")
            pairs.append((a.strip(), b.strip()))

    log_open(args.log)
    log(f"iperf matrix test - {datetime.now().isoformat()}")
    log(f"pairs: {pairs}")
    log(f"UDP rate steps: {UDP_RATE_STEPS_MBIT} Mbit/s, "
        f"loss threshold {UDP_LOSS_THRESHOLD_PCT}%, "
        f"{args.udp_duration}s/step, TCP {args.tcp_duration}s")

    ip_cache = {}
    results = {}

    pc_kill_stray_iperf()

    winmm = ctypes.WinDLL("winmm")
    winmm.timeBeginPeriod(1)
    try:
        for src, dst in pairs:
            log(f"\n### {src} -> {dst} ###")
            udp_result = test_udp_max(src, dst, args.udp_duration, args.iperf_exe, ip_cache)
            tcp_result = test_tcp(src, dst, args.tcp_duration, args.iperf_exe, ip_cache)
            results[(src, dst)] = {"udp": udp_result, "tcp": tcp_result}
    finally:
        winmm.timeEndPeriod(1)

    log("\n\n===== SUMMARY =====")
    for (src, dst), r in results.items():
        udp = r["udp"]
        tcp = r["tcp"]
        udp_s = f"{udp['mbit_s']:.2f} Mbit/s, {udp['loss_pct']:.1f}% loss" if udp else "FAIL"
        tcp_s = f"{tcp['mbit_s']:.2f} Mbit/s" if tcp else "FAIL"
        log(f"{src:>10} -> {dst:<10}  UDP max: {udp_s:<28}  TCP: {tcp_s}")

    log(f"\nFull log written to {args.log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
