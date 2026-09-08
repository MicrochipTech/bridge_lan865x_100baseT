#!/usr/bin/env python3
"""
Find this project's boards on the local network segment.

Standalone and GUI-free, like pki.py/bootload.py. One way to do it -
discover_boards(), what both the CLI and the GUI's single "Discover Boards"
button run - in two steps:

  1. a small plaintext UDP broadcast query; the boards answer for
     themselves with MAC+IP (broadcast_discover(), firmware Discovery_Tasks()
     in firmware/src/app.c). Well under a second, and it touches no address
     where no board lives.
  2. one mutual-TLS handshake against each IP that actually answered, to
     add the CA-verified fingerprint that step 1 has no way to prove.

So the identifying information still comes from TLS, but the *addresses* it
is asked of come from the boards themselves rather than from sweeping a
subnet. A board is identified by completing our mutual-TLS handshake (our
CA, our shared client identity) against port 23 - a bare "something
answered on port 23" is not enough, since that proves nothing about *which*
server is listening. Does not log in (no username/password sent): the
handshake alone fingerprints the board and confirms it trusts our CA.

The full /24 sweep (scan_subnet()) is still here but is an escape hatch,
`--tls-scan` only, with no button in the GUI: it exists for a board whose
discovery responder is down or whose firmware predates it. It is not free -
every probe against an address where nothing lives is itself an ARP
broadcast, this board bridges the scanned segment onto 10BASE-T1S, and an
unpaced sweep has wedged the whole bench before (see its docstring). `ping`
sweeps are not used anywhere here: ping is answered by the TCP/IP stack
even when the application side is wedged, so it proves less than step 1.

Not mDNS/DNS-SD - that was tried and dropped, see the Broadcast discovery
block below.

    python scripts/discover.py --base-ip 192.168.0.12
        # broadcast, then mTLS per answer: IP + MAC + fingerprint
    python scripts/discover.py --base-ip 192.168.0.12 --tls-scan
        # escape hatch: sweeps 192.168.0.1-254 on port 23 instead
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


DISCOVERY_QUERY_RESEND_INTERVAL = 0.25


def broadcast_discover(base_ip: str, timeout: float = 1.0, port: int = DISCOVERY_UDP_PORT):
    """Broadcast a discovery query on base_ip's /24 and collect replies for
    `timeout` seconds. Returns a list of {"ip", "mac"} dicts, sorted by IP -
    same shape of use as scan_subnet() but no fingerprint (no TLS happens
    here at all) and typically returns well under a second instead of
    several. ip comes from the reply's own source address (always correct);
    the MAC in the payload identifies which of the board's two interfaces
    (eth0/T1S or eth1/100BASE-TX - this board bridges both) actually
    answered.

    Resends the query every DISCOVERY_QUERY_RESEND_INTERVAL instead of
    sending it just once: confirmed live (2026-09-06) that a single query
    already misses a board that answers reliably to a TLS probe seconds
    later - plain UDP, no retransmission of its own, so one lost query or
    reply is enough. A few resends inside the same listen window cost
    nothing extra in wall-clock time and dedupe for free (`found` is keyed
    by ip)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind((_local_ip_for(base_ip), 0))
    sock.settimeout(0.2)
    found = {}
    try:
        deadline = time.time() + timeout
        next_send = 0.0
        while time.time() < deadline:
            if time.time() >= next_send:
                sock.sendto(DISCOVERY_QUERY_MAGIC, (_directed_broadcast(base_ip), port))
                next_send = time.time() + DISCOVERY_QUERY_RESEND_INTERVAL
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
# 0.3s was too tight for a real host under the full sweep's own congestion -
# confirmed live (2026-09-06): a board that connects in 0.01-0.02s in
# isolation timed out its TCP connect at 0.313s under a full /24 sweep.
# 0.6s gives real hosts margin without meaningfully slowing down the sweep
# (dead addresses still dominate the pacing, see SUBMIT_INTERVAL).
TCP_TIMEOUT = 0.6
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
# Retry budget for a host that answered the TCP connect but didn't finish
# the TLS handshake within TLS_TIMEOUT during the paced sweep - confirmed
# live (2026-09-06) that a board reached through the Bridge's T1S forwarding
# (e.g. a Follower) intermittently loses that race under a full /24 sweep's
# ARP-broadcast load, even though an isolated probe against it alone
# completes in ~1.2s. Set comfortably above the firmware's own
# ENC_CONNECT_TIMEOUT_MS=15s handshake-wedge budget
# (net_pres_enc_glue.c) so a legitimately slow handshake gets to finish.
RETRY_TLS_TIMEOUT = 16.0

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


def _probe(ip: str, timeout: float = TLS_TIMEOUT):
    """Returns (result, tcp_ok). result is a dict on a confirmed board, None
    otherwise. tcp_ok tells "no such host" (TCP connect itself failed) apart
    from "host answered but the TLS handshake didn't finish in time" - only
    the latter is worth scan_subnet() retrying with a longer budget. Never
    raises - every failure mode (closed port, TCP timeout, TLS/cert
    rejection) just means "not one of ours" here."""
    try:
        raw = socket.create_connection((ip, TELNET_PORT), timeout=TCP_TIMEOUT)
    except OSError:
        return None, False

    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cafile=str(TLS_CA_CERT))
        ctx.load_cert_chain(certfile=str(TLS_CLIENT_CERT), keyfile=str(TLS_CLIENT_KEY))
        ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        raw.settimeout(timeout)
        ts = ctx.wrap_socket(raw, server_hostname=ip)
    except Exception:
        raw.close()
        return None, True

    try:
        peer_der = ts.getpeercert(binary_form=True)
    finally:
        ts.close()

    if not peer_der:
        return None, True

    import hashlib
    fp = hashlib.sha256(peer_der).hexdigest()
    return {"ip": ip, "fingerprint_sha256": fp}, True


def operator_identity_ready() -> bool:
    """Whether this checkout has the operator side of the mutual-TLS
    handshake at all. A fresh clone does NOT: certs/client/ is generated
    locally and deliberately not in the repo (docs/pki-clean-start.md), so
    every handshake would fail for a reason that has nothing to do with the
    boards. Checked so that case can be reported as itself instead of as 254
    (or three) unexplained failures."""
    return (TLS_CA_CERT.is_file() and TLS_CLIENT_CERT.is_file()
            and TLS_CLIENT_KEY.is_file())


def discover_boards(base_ip: str, timeout: float = 1.0, progress=None):
    """The normal way to find boards: broadcast first, then talk TLS only to
    what actually answered. Returns a list of {"ip", "mac",
    "fingerprint_sha256"} dicts sorted by IP, where fingerprint_sha256 is
    None for a board that answered the broadcast but did not complete our
    mutual-TLS handshake (no identity from our CA yet, Telnet busy, or its
    TLS side is wedged - all worth seeing in the list rather than hiding).
    progress(done, total), if given, is called after each board's handshake.

    Note what this deliberately does NOT do: touch a single address that no
    board answered from. That is the whole difference to scan_subnet() below
    - no /24 sweep, so no ARP broadcasts bridged onto the T1S segment, and
    no ping sweep either (ping is answered by the TCP/IP stack even when the
    application side is wedged, so it proves less than the broadcast does).

    The handshakes run serially and with the generous RETRY_TLS_TIMEOUT
    rather than concurrently: a lone handshake against a real board costs
    ~1.2s (RSA-2048 on its Cortex-M4F), and running several at once is
    exactly what made real boards time out during the old full sweep. For
    the handful of boards a bench has, serial is both fast enough and the
    variant that never loses one."""
    boards = broadcast_discover(base_ip, timeout=timeout)
    have_identity = operator_identity_ready()
    results = []
    for done, b in enumerate(boards, 1):
        probe = None
        if have_identity:   # otherwise every handshake fails for our reason, not theirs
            probe, _tcp_ok = _probe(b["ip"], timeout=RETRY_TLS_TIMEOUT)
        results.append({"ip": b["ip"], "mac": b["mac"],
                        "fingerprint_sha256": probe["fingerprint_sha256"] if probe else None})
        if progress:
            progress(done, len(boards))
    return results


def scan_subnet(base_ip: str, max_workers: int = MAX_WORKERS, progress=None):
    """Scan the /24 containing base_ip, return a list of {ip, fingerprint_sha256}
    dicts for every host that completed our mutual-TLS handshake, sorted by IP.
    progress(done, total), if given, is called after each address finishes -
    use it to drive a progress bar; scanning 254 addresses typically takes
    several seconds on a quiet LAN.

    First TLS-probes, individually and with the generous RETRY_TLS_TIMEOUT,
    whatever broadcast_discover() reports (near-instant, see that function) -
    this is what actually finds a board reached through the Bridge's T1S
    forwarding reliably (confirmed live, 2026-09-06: such a board's own
    handshake completes in ~1.2s in isolation, but was lost intermittently by
    the full-sweep path below under that sweep's own broadcast load). Then
    still runs the full paced /24 sweep for whatever broadcast didn't report
    (e.g. its responder is down but Telnet/TLS still works, or firmware
    predates it) - this is why both stay: the broadcast prefilter is a
    reliability/speed improvement, not a trust dependency, since the TLS
    handshake alone still identifies every board either path finds.

    The full sweep submits candidates paced by SUBMIT_INTERVAL rather than
    all at once: this board bridges the scanned Ethernet segment onto
    10BASE-T1S (tcpip_mac_bridge.c) - confirmed live (2026-09-05) that an
    unpaced burst of connection attempts across a whole /24 (each one's ARP
    resolution for a non-existent host is itself a broadcast) gets forwarded
    onto the much slower T1S segment and overflows the LAN865x's receive
    FIFO (`LAN865x_0 Status0.Receive Buffer Overflow Error`,
    drv_lan865x_api.c) - repeated scans this way eventually wedged all three
    bench boards' Telnet/TCP stack solid (ping still answered, port 23 did
    not; recovered only with a SWD reset). Pacing the submissions keeps the
    ARP broadcast rate well under what the T1S side needs to be able to
    drain. Any host that answered the TCP connect but didn't finish its TLS
    handshake within TLS_TIMEOUT is retried once, serially, with
    RETRY_TLS_TIMEOUT - the retry only touches hosts already confirmed live,
    so it can't itself trigger the ARP-broadcast load this sweep paces
    against."""
    found = []
    found_ips = set()
    try:
        live_ips = [r["ip"] for r in broadcast_discover(base_ip)]
    except OSError:
        live_ips = []
    for ip in live_ips:
        result, _tcp_ok = _probe(ip, timeout=RETRY_TLS_TIMEOUT)
        if result:
            found.append(result)
            found_ips.add(ip)

    candidates = [ip for ip in _subnet_from_base_ip(base_ip) if ip not in found_ips]
    retry_ips = []
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
            result, tcp_ok = future.result()
            if result:
                found.append(result)
            elif tcp_ok:
                retry_ips.append(futures[future])

    for ip in retry_ips:
        result, _tcp_ok = _probe(ip, timeout=RETRY_TLS_TIMEOUT)
        if result:
            found.append(result)

    found.sort(key=lambda r: tuple(int(x) for x in r["ip"].split(".")))
    return found


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-ip", required=True, help="any IP in the /24 to look on, e.g. 192.168.0.12")
    # The full sweep is opt-in, never the default - see the module docstring
    # for why (ARP-broadcast load bridged onto the T1S segment). --broadcast
    # is kept as an accepted no-op so older notes/scripts still run.
    ap.add_argument("--tls-scan", action="store_true",
                    help="opt-in: full /24 TCP+mutual-TLS sweep on port 23 instead of the "
                         "broadcast - slower and noisy, but yields a CA-verified fingerprint "
                         "per board (see scan_subnet())")
    ap.add_argument("--broadcast", action="store_true",
                    help=argparse.SUPPRESS)   # deprecated: broadcast is the default now
    args = ap.parse_args()

    if args.tls_scan:
        def _progress(done, total):
            print("\rscanning... %d/%d" % (done, total), end="", flush=True)

        results = scan_subnet(args.base_ip, progress=_progress)
        print()
        if not results:
            print("no boards found")
        for r in results:
            print("%-15s  %s" % (r["ip"], r["fingerprint_sha256"]))
    else:
        results = discover_boards(args.base_ip)
        if not results:
            print("no boards answered the broadcast")
        for r in results:
            print("%-15s  %-17s  %s" % (r["ip"], r["mac"],
                                        r["fingerprint_sha256"] or "(no mTLS handshake)"))
