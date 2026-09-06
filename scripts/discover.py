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

# *****************************************************************************
# Broadcast discovery (firmware/src/app.c, Discovery_Tasks()) - a tiny custom
# plaintext UDP protocol, not mDNS. mDNS-SD was tried and dropped (2026-09-06,
# see configuration.h's HAND-PATCH note on TCPIP_STACK_USE_ZEROCONF_MDNS_SD):
# Microchip's own query parser asserted on essentially every incoming query
# and never actually answered one. This is near-instant (one broadcast round
# trip, well under a second) versus scan_subnet()'s several-second TLS sweep
# below, and deliberately carries no trust of its own - a board answers with
# just its MAC+IP, nothing encrypted or authenticated. That is the whole
# point: it has to work before the querier and the board share any TLS
# identity. Everything after discovery (reading status, provisioning a new
# certificate) goes over the existing mutual-TLS Telnet console exactly as
# before - see cert_provision.py.
DISCOVERY_UDP_PORT = 30303
DISCOVERY_QUERY_MAGIC = b"BRDQ"
DISCOVERY_REPLY_MAGIC = b"BRDR"
DISCOVERY_REPLY_LEN = 15   # magic(4) + version(1) + mac(6) + ipv4(4)


def _local_ip_for(target_ip: str) -> str:
    """Which local NIC's IP the OS routing table would use to reach target_ip -
    the standard connect()-a-UDP-socket-and-read-it-back trick (no packet is
    actually sent; UDP connect() just does the route lookup). Needed because
    a directed broadcast sent without binding the sending socket to the
    matching NIC silently goes out the WRONG interface on a multi-homed
    Windows PC and nothing is ever seen back - confirmed live (2026-09-06),
    same lesson as the mDNS-SD post-mortem's multicast tests."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_ip, 1))
        return s.getsockname()[0]
    finally:
        s.close()


def _directed_broadcast(base_ip: str) -> str:
    """192.168.0.12 -> '192.168.0.255' - same /24-only assumption as
    _subnet_from_base_ip below. A directed broadcast (not 255.255.255.255)
    so this reaches the right subnet even from a multi-homed Windows PC,
    same lesson as the mDNS multicast tests during mDNS-SD's post-mortem:
    binding/targeting the wrong interface silently sees nothing."""
    parts = base_ip.strip().split(".")
    if len(parts) != 4:
        raise ValueError("not an IPv4 address: %r" % base_ip)
    return ".".join(parts[:3]) + ".255"


def broadcast_discover(base_ip: str, timeout: float = 1.0, port: int = DISCOVERY_UDP_PORT):
    """Broadcast one discovery query on base_ip's /24 and collect replies for
    `timeout` seconds. Returns a list of {"ip", "mac"} dicts, sorted by IP -
    same shape of use as scan_subnet() but no fingerprint (no TLS happens
    here at all) and typically returns well under a second instead of
    several. ip comes from the reply's own source address (always correct);
    the MAC in the payload identifies which of the board's two interfaces
    (eth0/T1S or eth1/100BASE-TX - this board bridges both) actually
    answered."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind((_local_ip_for(base_ip), 0))
    sock.settimeout(0.2)
    found = {}
    try:
        sock.sendto(DISCOVERY_QUERY_MAGIC, (_directed_broadcast(base_ip), port))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(64)
            except socket.timeout:
                continue
            if len(data) < DISCOVERY_REPLY_LEN or data[:4] != DISCOVERY_REPLY_MAGIC:
                continue   # not our protocol - some other broadcast chatter on this port
            mac = ":".join("%02X" % b for b in data[5:11])
            found[addr[0]] = {"ip": addr[0], "mac": mac}
    finally:
        sock.close()
    return sorted(found.values(), key=lambda r: tuple(int(x) for x in r["ip"].split(".")))


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
    ap.add_argument("--broadcast", action="store_true",
                    help="use the fast UDP broadcast protocol instead of the TLS subnet scan "
                         "(no fingerprint - just IP+MAC, see broadcast_discover())")
    args = ap.parse_args()

    if args.broadcast:
        results = broadcast_discover(args.base_ip)
        if not results:
            print("no boards found")
        for r in results:
            print("%-15s  %s" % (r["ip"], r["mac"]))
    else:
        def _progress(done, total):
            print("\rscanning... %d/%d" % (done, total), end="", flush=True)

        results = scan_subnet(args.base_ip, progress=_progress)
        print()
        if not results:
            print("no boards found")
        for r in results:
            print("%-15s  %s" % (r["ip"], r["fingerprint_sha256"]))
