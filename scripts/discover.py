#!/usr/bin/env python3
"""
Find this project's boards on the local network segment.

Standalone and GUI-free, like pki.py/bootload.py: a plain TCP+TLS subnet
scan on port 23, not mDNS/DNS-SD. Chosen over mDNS deliberately - a mDNS
responder would need a new Harmony component in every board's firmware
(flash is already at 66% of budget, see docs/tls-poc-report.md §3.1) and a
reflash of the whole fleet, for a bench of a handful of boards on one flat
/24 where a plain scan takes a couple of seconds. Not implemented: crossing
subnet boundaries, which mDNS would need a reflector for anyway.

A board is identified by completing our mutual-TLS handshake (our CA, our
shared client identity) against port 23 - a bare "something answered on
port 23" is not enough, since that proves nothing about *which* server is
listening. Does not log in (no username/password sent): the handshake alone
is enough to fingerprint the board and confirm it trusts our CA.

    python scripts/discover.py --base-ip 192.168.0.12
        # scans 192.168.0.1-254, port 23
"""

import argparse
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

TELNET_PORT = 23
TCP_TIMEOUT = 0.3
# A lone handshake against a real board takes ~1.15-1.2s (measured live,
# docs/tls-poc-report.md sec.10 - RSA-2048 on the board's Cortex-M4F is the
# cost, even with the SP/ASM speed-up). Under MAX_WORKERS concurrent
# handshakes that regularly pushed past a 2.0s timeout (confirmed live: 2 of
# 3 real boards on the bench timed out at 2.0s, every run, only the one
# that happened to be scheduled first came back) - GIL/thread-scheduling
# contention across dozens of threads adds real latency on top of the
# board's own cost. 6s absorbs that with margin; MAX_WORKERS trimmed too so
# there is less contention to absorb in the first place.
TLS_TIMEOUT = 6.0
MAX_WORKERS = 16
# Paces how fast new probes are submitted - see scan_subnet()'s docstring for
# why: each probe against a non-existent host is itself an ARP broadcast,
# and 254 of them fired at once floods the T1S segment this board bridges
# onto. 30ms caps that around ~33/s; the whole scan still finishes in well
# under 10s on a quiet /24.
SUBMIT_INTERVAL = 0.03

TLS_CA_CERT = Path(__file__).parent.parent / "certs" / "ca" / "ca_cert.pem"
TLS_CLIENT_CERT = Path(__file__).parent.parent / "certs" / "client" / "client_cert.pem"
TLS_CLIENT_KEY = Path(__file__).parent.parent / "certs" / "client" / "client_key.pem"


def _subnet_from_base_ip(base_ip: str):
    """192.168.0.12 -> ['192.168.0.1', ..., '192.168.0.254'] (skips .0/.255).
    Only ever a /24 - good enough for one flat bench network, not a general
    CIDR scanner."""
    parts = base_ip.strip().split(".")
    if len(parts) != 4:
        raise ValueError("not an IPv4 address: %r" % base_ip)
    prefix = ".".join(parts[:3])
    return ["%s.%d" % (prefix, i) for i in range(1, 255)]


def _probe(ip: str):
    """Returns a dict on a confirmed board, None otherwise. Never raises -
    every failure mode (closed port, TCP timeout, TLS/cert rejection) just
    means "not one of ours" here."""
    try:
        raw = socket.create_connection((ip, TELNET_PORT), timeout=TCP_TIMEOUT)
    except OSError:
        return None

    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cafile=str(TLS_CA_CERT))
        ctx.load_cert_chain(certfile=str(TLS_CLIENT_CERT), keyfile=str(TLS_CLIENT_KEY))
        ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        raw.settimeout(TLS_TIMEOUT)
        ts = ctx.wrap_socket(raw, server_hostname=ip)
    except Exception:
        raw.close()
        return None

    try:
        peer_der = ts.getpeercert(binary_form=True)
    finally:
        ts.close()

    if not peer_der:
        return None

    import hashlib
    fp = hashlib.sha256(peer_der).hexdigest()
    return {"ip": ip, "fingerprint_sha256": fp}


def scan_subnet(base_ip: str, max_workers: int = MAX_WORKERS, progress=None):
    """Scan the /24 containing base_ip, return a list of {ip, fingerprint_sha256}
    dicts for every host that completed our mutual-TLS handshake, sorted by IP.
    progress(done, total), if given, is called after each address finishes -
    use it to drive a progress bar; scanning 254 addresses typically takes
    several seconds on a quiet LAN.

    Submits candidates paced by SUBMIT_INTERVAL rather than all 254 at once:
    this board bridges the scanned Ethernet segment onto 10BASE-T1S
    (tcpip_mac_bridge.c) - confirmed live (2026-09-05) that an unpaced burst
    of connection attempts across a whole /24 (each one's ARP resolution for
    a non-existent host is itself a broadcast) gets forwarded onto the much
    slower T1S segment and overflows the LAN865x's receive FIFO
    (`LAN865x_0 Status0.Receive Buffer Overflow Error`, drv_lan865x_api.c) -
    repeated scans this way eventually wedged all three bench boards'
    Telnet/TCP stack solid (ping still answered, port 23 did not; recovered
    only with a SWD reset). Pacing the submissions keeps the ARP broadcast
    rate well under what the T1S side needs to be able to drain."""
    candidates = _subnet_from_base_ip(base_ip)
    found = []
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for ip in candidates:
            futures[pool.submit(_probe, ip)] = ip
            time.sleep(SUBMIT_INTERVAL)
        for future in as_completed(futures):
            done += 1
            if progress:
                progress(done, len(candidates))
            result = future.result()
            if result:
                found.append(result)
    found.sort(key=lambda r: tuple(int(x) for x in r["ip"].split(".")))
    return found


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-ip", required=True, help="any IP in the /24 to scan, e.g. 192.168.0.12")
    args = ap.parse_args()

    def _progress(done, total):
        print("\rscanning... %d/%d" % (done, total), end="", flush=True)

    results = scan_subnet(args.base_ip, progress=_progress)
    print()
    if not results:
        print("no boards found")
    for r in results:
        print("%-15s  %s" % (r["ip"], r["fingerprint_sha256"]))
