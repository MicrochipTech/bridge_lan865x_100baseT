#!/usr/bin/env python3
"""
sniffer_capture_test.py - validate the bridge's "sniffer" mirror path.

Ported from the sister project (t1s_100baset_bridge/scripts/sniffer_capture_test.py),
adapted for this project's own bench addresses. See that project's
docs/SNIFFER_1_HYPOTHESEN.md through SNIFFER_4_ERGEBNISSE.md for the full
history of the bug this test validates the mitigation for.

Enables `sniffer` on the bridge (mirrors every eth0/T1S frame to eth1, for
the PC to capture - see CLAUDE.md/port_mirror.c), then runs real UDP and
TCP traffic directly between Follower A and Follower B (both directions)
while tshark records everything arriving on the PC's NIC. Afterward it
checks whether the capture is actually complete:

    - UDP: iperf embeds a 32-bit sequence id as the first 4 bytes of each
      datagram's payload. The source's own "Sent N datagrams" count is the
      ground truth for how many frames were actually put on the T1S bus
      (Follower A/B talk directly to each other - the bridge in sniffer
      mode is a passive tap, not a participant). This script extracts the
      sequence ids seen in the capture and reports any gaps against that
      expected range.
    - TCP: compares the destination's own reported received bytes (its
      "iperf -s" session report, the same trust rule as iperf_matrix_test.py)
      against the total TCP payload bytes actually captured for that
      connection.

Also checks for frame-length truncation (MIRROR_SAFE_FRAME_LEN, see
port_mirror.c / the sister project's docs/SNIFFER_4_ERGEBNISSE.md): a
captured frame shorter than what its own IP/UDP header claims would mean
real payload bytes are missing, not just harmless trailing padding.

Requires: pyserial, Wireshark/tshark on PATH-equivalent (uses the default
install location, override with --tshark), and reuses node config/helpers
from iperf_matrix_test.py (must be in the same directory).

Usage:
    python sniffer_capture_test.py
    python sniffer_capture_test.py --duration 5 --udp-rate 8 --log sniffer_run.log

Run with --help for the full list of parameters and what each one does.
"""
import argparse
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from iperf_matrix_test import (
    DEVICES,
    DeviceServerCapture,
    device_run_client,
    parse_device_report,
)

DEFAULT_TSHARK = r"C:\Program Files\Wireshark\tshark.exe"
CAPTURE_IFACE = "2"  # "Ethernet 8" on this PC - verified live 2026-08-31 (tshark -D)
IPERF_PORT = 5001

PAIRS = [("FollowerA", "FollowerB"), ("FollowerB", "FollowerA")]

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


def bridge_sniffer(on, bridge_port="COM8"):
    import serial
    with serial.Serial(bridge_port, 115200, timeout=0.1) as ser:
        ser.reset_input_buffer()
        ser.write(b"\r\n")
        time.sleep(0.2)
        ser.reset_input_buffer()
        ser.write((f"sniffer {1 if on else 0}\r\n").encode())
        ser.flush()
        out = bytearray()
        deadline = time.time() + 1.0
        while time.time() < deadline:
            n = ser.in_waiting
            if n:
                out += ser.read(n)
            time.sleep(0.02)
        return out.decode(errors="replace")


def start_tshark(tshark_exe, cap_file, duration, bpf_filter):
    proc = subprocess.Popen(
        [tshark_exe, "-i", CAPTURE_IFACE, "-f", bpf_filter, "-w", str(cap_file),
         "-a", f"duration:{duration}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Starting back-to-back captures on the same interface isn't instant -
    # firing the client before tshark actually opened the adapter silently
    # loses the whole test. A fixed, empirically-sized delay is simpler and
    # reliable than trying to detect readiness from tshark's own stdout
    # (fully-buffered when piped, so a readline() there can block for the
    # whole capture duration instead of a couple seconds).
    time.sleep(2.5)
    return proc


def tshark_fields(tshark_exe, cap_file, display_filter, fields):
    cmd = [tshark_exe, "-r", str(cap_file), "-Y", display_filter, "-T", "fields"]
    for f in fields:
        cmd += ["-e", f]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    rows = []
    for line in res.stdout.splitlines():
        if line.strip():
            rows.append(line.split("\t"))
    return rows


def analyze_udp(tshark_exe, cap_file, src_ip, dst_ip, sent_count):
    rows = tshark_fields(
        tshark_exe, cap_file,
        f"udp.dstport=={IPERF_PORT} and ip.src=={src_ip} and ip.dst=={dst_ip}",
        ["frame.len", "ip.len", "udp.length", "data.data"])

    seqs = []
    terminators = 0
    truncated = 0
    for row in rows:
        if len(row) < 4 or not row[3]:
            continue
        frame_len, ip_len, udp_len = row[0], row[1], row[2]
        try:
            udp_payload_present = int(frame_len) - 14 - 20  # eth + ip headers
            if udp_payload_present < int(udp_len):
                truncated += 1
        except ValueError:
            pass
        hexdata = row[3]
        if len(hexdata) >= 8:
            # iperf's UDP id field is a SIGNED 32-bit int: real datagrams
            # count up from 0, but the closing datagram(s) send the id with
            # the sign flipped as an end-of-stream marker and may repeat it
            # several times if not acked. Reading it as unsigned turns that
            # into a value near 2**32, and a naive range(min(seqs),
            # max(seqs)) gap check then tries to iterate ~4 billion numbers.
            raw = int(hexdata[:8], 16)
            signed = raw - 0x100000000 if raw >= 0x80000000 else raw
            if signed < 0:
                terminators += 1
            else:
                seqs.append(signed)

    captured = len(seqs) + terminators
    missing = []
    if seqs:
        seq_set = set(seqs)
        lo, hi = min(seqs), max(seqs)
        missing = [s for s in range(lo, hi + 1) if s not in seq_set]

    return {
        "captured": captured,
        "data_captured": len(seqs),
        "terminators_captured": terminators,
        "expected": sent_count,
        "missing_count": len(missing),
        "missing_sample": missing[:10],
        "truncated_with_data_loss": truncated,
    }


def analyze_tcp(tshark_exe, cap_file, src_ip, dst_ip, expected_bytes):
    rows = tshark_fields(
        tshark_exe, cap_file,
        f"tcp.port=={IPERF_PORT} and ip.src=={src_ip} and ip.dst=={dst_ip}",
        ["tcp.len"])
    total = 0
    segments = 0
    for row in rows:
        if row and row[0]:
            total += int(row[0])
            segments += 1
    return {
        "captured_bytes": total,
        "captured_segments": segments,
        "expected_bytes": expected_bytes,
    }


def run_udp_test(src, dst, duration, tshark_exe, cap_dir, rate_bps):
    src_ip = DEVICES[src]["ip"]
    dst_ip = DEVICES[dst]["ip"]
    cap_file = cap_dir / f"udp_{src}_to_{dst}.pcapng"

    server = DeviceServerCapture(DEVICES[dst]["port"], udp=True, duration=duration + 3.0)
    server.start()
    time.sleep(0.5)
    ts = start_tshark(tshark_exe, cap_file, duration + 4,
                       f"udp port {IPERF_PORT} and host {src_ip} and host {dst_ip}")

    client_out = device_run_client(DEVICES[src]["port"], dst_ip, udp=True,
                                    rate_bps=rate_bps, duration=duration)
    server.join(timeout=duration + 6)
    ts.wait(timeout=duration + 8)

    # The embedded client doesn't print "Sent N datagrams" (that's a
    # real-iperf.exe-only message) - it prints the same
    # "[0.0- X sec] dropped/attempted (loss%) Kbps" session report as any
    # other iperf instance. "attempted" here is the client's own outgoing
    # packet-id counter (trustworthy - it's counting its own sends, not
    # anything requiring feedback from the far end), unlike "dropped" which
    # would be meaningless for a pure sender.
    src_result = parse_device_report(client_out)
    sent_count = src_result["total"] if src_result else None
    dst_result = parse_device_report(server.output)

    log(f"  client (source) output:\n" + "\n".join(
        "    " + l for l in client_out.splitlines() if l.strip()))
    log(f"  destination report: " + (
        f"{dst_result['dropped']}/{dst_result['total']} lost, "
        f"{dst_result['mbit_s']:.2f} Mbit/s" if dst_result else "NO RESULT"))
    log(f"  source reports it actually sent: {sent_count}")

    if sent_count is None:
        log("  => cannot verify: source's 'Sent N datagrams' line not found")
        return

    analysis = analyze_udp(tshark_exe, cap_file, src_ip, dst_ip, sent_count)
    log(f"  capture analysis: {analysis['captured']} datagrams captured "
        f"({analysis['data_captured']} data + {analysis['terminators_captured']} "
        f"end-of-stream marker(s)), expected {analysis['expected']} "
        f"(source-reported sent count)")
    if analysis["missing_count"] == 0 and analysis["captured"] >= analysis["expected"]:
        log(f"  => COMPLETE: every sent sequence id is present in the capture")
    else:
        log(f"  => INCOMPLETE: {analysis['missing_count']} sequence id(s) missing "
            f"from the capture, e.g. {analysis['missing_sample']}")
    if analysis["truncated_with_data_loss"]:
        log(f"  => WARNING: {analysis['truncated_with_data_loss']} captured frame(s) "
            f"are shorter than their own IP/UDP header claims - real payload bytes "
            f"missing, not just harmless padding")


def run_tcp_test(src, dst, duration, tshark_exe, cap_dir):
    src_ip = DEVICES[src]["ip"]
    dst_ip = DEVICES[dst]["ip"]
    cap_file = cap_dir / f"tcp_{src}_to_{dst}.pcapng"

    server = DeviceServerCapture(DEVICES[dst]["port"], udp=False, duration=duration + 3.0)
    server.start()
    time.sleep(0.5)
    ts = start_tshark(tshark_exe, cap_file, duration + 4,
                       f"tcp port {IPERF_PORT} and host {src_ip} and host {dst_ip}")

    client_out = device_run_client(DEVICES[src]["port"], dst_ip, udp=False,
                                    rate_bps=None, duration=duration)
    server.join(timeout=duration + 6)
    ts.wait(timeout=duration + 8)

    dst_result = parse_device_report(server.output)
    log(f"  destination report: " + (
        f"{dst_result['mbit_s']:.2f} Mbit/s over {duration}s" if dst_result else "NO RESULT"))

    if dst_result is None:
        log("  => cannot verify: destination report not parsed")
        return

    expected_bytes = int(dst_result["mbit_s"] * 1_000_000 * duration / 8)
    analysis = analyze_tcp(tshark_exe, cap_file, src_ip, dst_ip, expected_bytes)
    log(f"  capture analysis: {analysis['captured_bytes']} bytes / "
        f"{analysis['captured_segments']} segments captured, "
        f"~{expected_bytes} bytes expected (from destination's reported rate)")
    ratio = (analysis["captured_bytes"] / expected_bytes) if expected_bytes else 0
    if ratio >= 0.95:
        log(f"  => COMPLETE (within measurement tolerance, {ratio*100:.1f}% of expected)")
    else:
        log(f"  => INCOMPLETE: only {ratio*100:.1f}% of the expected bytes were captured")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python sniffer_capture_test.py
  python sniffer_capture_test.py --udp-rate 8 --duration 8
  python sniffer_capture_test.py --log my_run.log --cap-dir C:\\captures
""")
    ap.add_argument("--log", default="docs/sniffer_capture_results.log",
                     help="log file to append the full run to (default: "
                          "docs/sniffer_capture_results.log, relative to the current "
                          "directory). Progress is also printed to the screen as it happens.")
    ap.add_argument("--tshark", default=DEFAULT_TSHARK,
                     help=f"path to tshark.exe (default: {DEFAULT_TSHARK})")
    ap.add_argument("--duration", type=float, default=5.0,
                     help="seconds each test (UDP and TCP, per direction) runs for "
                          "(default: 5.0)")
    ap.add_argument("--udp-rate", type=float, default=5.0,
                     help="target UDP bandwidth in Mbit/s that the sending Follower's "
                          "iperf client is asked to generate for the UDP tests "
                          "(default: 5.0). This is a fixed rate, not a search like "
                          "iperf_matrix_test.py's UDP Max - raise it to stress-test "
                          "the sniffer mirror path closer to the T1S ceiling "
                          "(~9.4 Mbit/s on the sister project's bench).")
    ap.add_argument("--cap-dir", default=".",
                     help="directory to write the four .pcapng capture files into "
                          "(default: current directory). Created if it doesn't exist.")
    args = ap.parse_args()

    # --udp-rate takes Mbit/s. Neither link on this bench (10BASE-T1S: ~10,
    # 100BASE-T: 100) can ever need more than 100 - a value above that is
    # almost certainly bps typed by mistake, which multiplies out to a
    # Tbit/s target that overflows the embedded client's 32-bit rate field
    # (wraps to a small or even negative "Target rate").
    if not (0 < args.udp_rate <= 100):
        print(f"ERROR: --udp-rate {args.udp_rate} is out of range - it takes Mbit/s, "
              f"not bps (e.g. --udp-rate 9 for 9 Mbit/s, not --udp-rate 9000000). "
              f"Valid range: 0 < rate <= 100 Mbit/s.")
        return 1

    if not Path(args.tshark).exists():
        print(f"ERROR: tshark not found at {args.tshark} (--tshark to override)")
        return 1

    udp_rate_bps = args.udp_rate * 1_000_000

    cap_dir = Path(args.cap_dir)
    cap_dir.mkdir(parents=True, exist_ok=True)

    log_open(args.log)
    log(f"sniffer capture test - {datetime.now().isoformat()}")
    log(f"duration per test: {args.duration}s, UDP rate: {args.udp_rate} Mbit/s")

    log("\nEnabling sniffer on the bridge...")
    log(bridge_sniffer(True))

    try:
        for src, dst in PAIRS:
            log(f"\n### UDP {src} -> {dst} (while sniffing) ###")
            run_udp_test(src, dst, args.duration, args.tshark, cap_dir, udp_rate_bps)
            time.sleep(1.5)  # let the capture interface settle before the next test

            log(f"\n### TCP {src} -> {dst} (while sniffing) ###")
            run_tcp_test(src, dst, args.duration, args.tshark, cap_dir)
            time.sleep(1.5)
    finally:
        log("\nDisabling sniffer on the bridge...")
        log(bridge_sniffer(False))

    log(f"\nFull log written to {args.log}")
    log(f"Capture files written to {cap_dir.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
