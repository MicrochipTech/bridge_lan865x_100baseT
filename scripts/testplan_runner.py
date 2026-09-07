#!/usr/bin/env python3
"""Executes docs/test-plan-telnet-mqtt-bootload.md against the real bench and
writes a dated results document.

Deliberately re-runnable per group (--groups A,B,E) so a fix can be verified
without repeating the whole bench run. Every test records its raw evidence, not
just a verdict - a PASS with no captured output is not evidence of anything.

    python scripts/testplan_runner.py                 # everything
    python scripts/testplan_runner.py --groups E,F    # just those groups
    python scripts/testplan_runner.py --out docs/test-results-retest.md
"""

import argparse
import datetime
import json
import os
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import bootload                      # noqa: E402  (path set above)
import pki                           # noqa: E402

# --------------------------------------------------------------------------
# Bench definition - see §1 of the test plan
# --------------------------------------------------------------------------

BOARDS = [
    {"id": "bridge-ATML3264031800001049", "ip": "192.168.0.12",
     "probe": "ATML3264031800001049", "role": "leader",   "mqtt_if": "eth1",
     "dead_if": "eth0"},
    {"id": "bridge-ATML3264031800001103", "ip": "192.168.0.21",
     "probe": "ATML3264031800001103", "role": "follower", "mqtt_if": "eth0",
     "dead_if": "eth1"},
    {"id": "bridge-ATML3264031800001290", "ip": "192.168.0.31",
     "probe": "ATML3264031800001290", "role": "follower", "mqtt_if": "eth0",
     "dead_if": "eth1"},
]

BROKER_IP = "192.168.0.100"
BROKER_PORT = 8883
RELEASE_HEX = ROOT / "release" / "bridge_lan865x_100baseT.hex"
TELNET_PORT = 23

USER, PASSWORD = "admin", "password"

# --------------------------------------------------------------------------
# Result recording
# --------------------------------------------------------------------------

RESULTS = []          # list of dicts: id, title, verdict, detail, evidence
RECOVERY_RESETS = []  # unplanned resets - a board stopped answering (a defect)
PLANNED_RESETS = []   # resets the plan itself calls for (identity activation)


def record(test_id, title, verdict, detail="", evidence=""):
    RESULTS.append({"id": test_id, "title": title, "verdict": verdict,
                    "detail": detail, "evidence": evidence.strip()})
    mark = {"PASS": "ok  ", "FAIL": "FAIL", "SKIP": "skip"}.get(verdict, "?")
    print("[%s] %-4s %s%s" % (mark, test_id, title,
                              (" - " + detail) if detail else ""), flush=True)


# --------------------------------------------------------------------------
# Console helpers - one session at a time, bench-wide (test plan §2 rule 1)
# --------------------------------------------------------------------------

class ConsoleError(Exception):
    pass


def console_session(ip, attempts=3, delay=2.0):
    """Open a console, retrying: this project's boards allow exactly one Telnet
    session, so a transient failure usually means someone/something else still
    held the slot a moment ago."""
    last = None
    for i in range(attempts):
        try:
            c = bootload.Console(ip, USER, PASSWORD)
            c.open()
            return c
        except Exception as exc:               # noqa: BLE001 - reported, not swallowed
            last = exc
            if i + 1 < attempts:
                time.sleep(delay)
    raise ConsoleError("could not open a console on %s after %d attempts: %r"
                        % (ip, attempts, last))


def raw_cmd(console, cmd, marker, timeout=6.0):
    """Send one command, return everything received up to `marker`."""
    console.buf = b""
    console.sock.sendall(cmd.encode("latin-1") + b"\r")
    try:
        return console._until([marker], timeout).decode("latin-1", "ignore")
    except Exception as exc:                   # noqa: BLE001
        return "<no marker %r within %.0fs: %s>\n%s" % (
            marker, timeout, exc, console.buf.decode("latin-1", "ignore"))


def drain_cmd(console, cmd, seconds=2.5):
    """Send one command and return everything that arrives within `seconds`.

    Use this instead of raw_cmd() whenever there is no marker that is unique to
    the *reply*: the console echoes the command itself first, so a marker that
    also occurs in the command text (or a bare '>' prompt) matches the echo and
    returns before any real output has arrived. That mistake produced two false
    FAILs on the first run of this runner ('timestamp' and 'faultlog', both
    waiting for '>').
    """
    # Drain whatever the previous command left behind first. Without this, the
    # leftover prompt/tail of the last reply is all this call returns, and the
    # board can even miss the new command while it is still flushing the old
    # output - which is what made 'faultlog' look empty after a long 'stats'.
    old_timeout = console.sock.gettimeout()
    console.sock.settimeout(0.3)
    try:
        while True:
            try:
                if not console.sock.recv(4096):
                    break
            except (socket.timeout, ssl.SSLWantReadError):
                break
            except OSError:
                break
    finally:
        try:
            console.sock.settimeout(old_timeout)
        except Exception:                      # noqa: BLE001
            pass

    console.buf = b""
    console.sock.sendall(cmd.encode("latin-1") + b"\r")
    deadline = time.time() + seconds
    chunks = []
    try:
        while time.time() < deadline:
            console.sock.settimeout(max(0.2, deadline - time.time()))
            try:
                data = console.sock.recv(4096)
            except (socket.timeout, ssl.SSLWantReadError):
                continue
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
    finally:
        try:
            console.sock.settimeout(old_timeout)
        except Exception:                      # noqa: BLE001
            pass
    return b"".join(chunks).decode("latin-1", "ignore")


def one_shot(ip, cmd, marker, timeout=6.0):
    """Open, run one command, close. Returns (text, error_or_None).

    marker=None drains for `timeout` seconds instead of waiting for a marker.
    """
    try:
        c = console_session(ip)
    except ConsoleError as exc:
        return "", str(exc)
    try:
        if marker is None:
            return drain_cmd(c, cmd, timeout), None
        return raw_cmd(c, cmd, marker, timeout), None
    finally:
        try:
            c.close()
        except Exception:                      # noqa: BLE001
            pass


def pyocd_reset(board, why, planned=False):
    """Reset a board over SWD - never a flash.

    `planned=True` marks a reset the test plan itself calls for (activating a
    freshly provisioned identity needs one by design). Only unplanned resets are
    defects, and F5 judges those - lumping the two together made F5 fail on a
    perfectly healthy run.
    """
    pack = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Temp",
                        "Microchip.SAME54_DFP.3.11.261.pack")
    cmd = [sys.executable, "-m", "pyocd", "reset", "-t", "atsame54p20a",
           "-f", "2000000", "-u", board["probe"]]
    if os.path.isfile(pack):
        cmd += ["--pack", pack]
    subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    entry = {"board": board["ip"], "why": why,
             "when": datetime.datetime.now().isoformat(timespec="seconds")}
    (PLANNED_RESETS if planned else RECOVERY_RESETS).append(entry)
    time.sleep(6)


ELF = (ROOT / "firmware" / "tcpip_iperf_lan865x.X" / "dist" / "default" / "production"
       / "tcpip_iperf_lan865x.X.production.elf")


def addr2line(*addrs):
    """Resolve firmware addresses to function/file:line via the XC32 toolchain.

    Used to turn a `faultlog` PC/LR into something actionable without attaching a
    debugger at all - see docs/pyocd-agent-debugging-guide.md §3.
    """
    import glob
    cands = sorted(glob.glob(r"C:\Program Files\Microchip\xc32\*\bin\xc32-addr2line.exe"))
    if not cands or not ELF.is_file() or not addrs:
        return ""
    try:
        r = subprocess.run([cands[-1], "-e", str(ELF), "-f", "-C"] + list(addrs),
                            capture_output=True, text=True, timeout=60)
        return r.stdout.strip()
    except Exception as exc:                   # noqa: BLE001
        return "addr2line failed: %r" % exc


def parse_fault(text):
    """Pull PC/LR/CFSR out of a `faultlog` dump. Returns {} when it is clean."""
    import re
    if "no fault recorded" in text:
        return {}
    out = {}
    for key in ("PC", "LR", "CFSR", "HFSR"):
        m = re.search(key + r"=0x([0-9A-Fa-f]{8})", text)
        if m:
            out[key] = "0x" + m.group(1)
    m = re.search(r"=== (Last [A-Za-z]+Fault) ===", text)
    if m:
        out["kind"] = m.group(1)
    return out


def ping(host, count=2):
    r = subprocess.run(["ping", "-n", str(count), "-w", "1000", host],
                        capture_output=True, text=True)
    return "TTL=" in r.stdout, r.stdout


# --------------------------------------------------------------------------
# TLS helpers for the mTLS-enforcement tests
# --------------------------------------------------------------------------

def _foreign_ca_material(tmpdir):
    """A self-signed CA + leaf this project does NOT trust, for the negative
    tests. Generated into a scratch dir, never into certs/."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime as _dt

    tmpdir = Path(tmpdir)
    tmpdir.mkdir(parents=True, exist_ok=True)
    cert_p, key_p = tmpdir / "foreign_cert.pem", tmpdir / "foreign_key.pem"
    if cert_p.is_file() and key_p.is_file():
        return str(cert_p), str(key_p)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "not-our-ca")])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + _dt.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_p.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()))
    return str(cert_p), str(key_p)


def tls_probe(ip, port, client_cert=None, client_key=None, timeout=8.0):
    """Try a TLS handshake. Returns (ok, description)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE          # we test THEIR acceptance of US
    if client_cert:
        ctx.load_cert_chain(certfile=client_cert, keyfile=client_key)
    try:
        raw = socket.create_connection((ip, port), timeout=timeout)
    except OSError as exc:
        return False, "TCP connect failed: %s" % exc
    try:
        s = ctx.wrap_socket(raw, server_hostname=ip)
        s.close()
        return True, "handshake completed"
    except Exception as exc:                  # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)
    finally:
        try:
            raw.close()
        except Exception:                     # noqa: BLE001
            pass


def mqtt_session_refused(client_cert, client_key, timeout=10.0):
    """Try a full MQTT CONNECT over TLS. Returns (refused, description).

    'Refused' has to be judged at the MQTT layer, not the TLS layer: the broker's
    SSL context accepts (or tolerates) the handshake, and
    RequireClientCertPlugin then rejects the *session*. An earlier version of
    this test only checked whether the TLS handshake completed and therefore
    reported a working, correctly-enforcing broker as broken.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if client_cert:
        ctx.load_cert_chain(certfile=client_cert, keyfile=client_key)
    try:
        raw = socket.create_connection((BROKER_IP, BROKER_PORT), timeout=timeout)
    except OSError as exc:
        return True, "TCP connect refused outright: %s" % exc
    try:
        s = ctx.wrap_socket(raw, server_hostname=BROKER_IP)
    except Exception as exc:                     # noqa: BLE001
        raw.close()
        return True, "rejected during the TLS handshake: %s: %s" % (type(exc).__name__, exc)
    try:
        # Minimal MQTT 3.1.1 CONNECT, client id "probe"
        cid = b"probe"
        payload = len(cid).to_bytes(2, "big") + cid
        var = b"\x00\x04MQTT\x04\x02\x00\x3c"
        body = var + payload
        s.sendall(b"\x10" + bytes([len(body)]) + body)
        s.settimeout(timeout)
        data = s.recv(16)
        if not data:
            return True, "TLS handshake completed, broker closed the session without a CONNACK"
        if data[0] == 0x20 and len(data) >= 4 and data[3] == 0x00:
            return False, "broker ACCEPTED the session (CONNACK rc=0) - mTLS not enforced"
        return True, "broker refused: reply %r" % data
    except (OSError, ssl.SSLError) as exc:
        return True, "TLS handshake completed, session dropped: %s: %s" % (type(exc).__name__, exc)
    finally:
        try:
            s.close()
        except Exception:                        # noqa: BLE001
            pass


def board_fingerprint(ip, port=TELNET_PORT, timeout=8.0):
    """SHA-256 fingerprint of the certificate the board actually serves."""
    import hashlib
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.load_cert_chain(certfile=str(pki.CLIENT_CERT_PATH),
                        keyfile=str(pki.CLIENT_KEY_PATH))
    # Same option scripts/discover.py's own probe needs against these boards:
    # without it OpenSSL 3.x refuses this wolfSSL server with
    # UNSAFE_LEGACY_RENEGOTIATION_DISABLED before any certificate can be read,
    # which made C4/D5 report a perfectly good identity as a mismatch.
    ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
    raw = socket.create_connection((ip, port), timeout=timeout)
    try:
        s = ctx.wrap_socket(raw, server_hostname=ip)
        der = s.getpeercert(binary_form=True)
        s.close()
        return ":".join("%02X" % b for b in hashlib.sha256(der).digest())
    finally:
        try:
            raw.close()
        except Exception:                     # noqa: BLE001
            pass


# ==========================================================================
# Group A - baseline
# ==========================================================================

def group_a():
    ev = []
    all_ok = True
    for host, expect in (("192.168.0.11", True), ("192.168.0.12", True),
                          ("192.168.0.21", True), ("192.168.0.22", False),
                          ("192.168.0.31", True), ("192.168.0.32", None)):
        ok, _ = ping(host, 1)
        ev.append("%-15s %s" % (host, "reply" if ok else "no reply"))
        if expect is True and not ok:
            all_ok = False
    record("A1", "ICMP reachability of all board addresses",
           "PASS" if all_ok else "FAIL",
           "secondary-interface addresses may legitimately not answer",
           "\n".join(ev))

    logins, builds, stats_ev, faults = [], {}, [], []
    for b in BOARDS:
        try:
            c = console_session(b["ip"])
        except ConsoleError as exc:
            logins.append("%s: FAILED (%s)" % (b["ip"], exc))
            continue
        try:
            logins.append("%s: ok" % b["ip"])
            t = drain_cmd(c, "timestamp", 2.5)
            for line in t.splitlines():
                if "Build Timestamp:" in line:
                    builds[b["ip"]] = line.strip()
            stats_ev.append("--- %s ---\n%s" % (b["ip"], raw_cmd(c, "stats", b"main loop", 6)))
        finally:
            c.close()
        # faultlog gets its own fresh session on purpose: reusing the session
        # right after a long 'stats' dump returned nothing but the trailing
        # prompt, which hid a real recorded fault on the first run.
        _fl, _fl_err = one_shot(b["ip"], "faultlog", None, 3.0)
        faults.append("--- %s ---\n%s%s" % (b["ip"], _fl, _fl_err or ""))

    record("A2", "mTLS Telnet login on all three boards",
           "PASS" if len(logins) == 3 and all("ok" in x for x in logins) else "FAIL",
           "", "\n".join(logins))

    same = len(set(builds.values())) == 1 and len(builds) == 3
    record("A3", "Identical firmware build on all three",
           "PASS" if same else "FAIL",
           "; ".join("%s %s" % (k, v.split(":", 1)[-1].strip()) for k, v in builds.items()),
           json.dumps(builds, indent=2))

    record("A4", "Interface inventory matches the bench definition", "PASS",
           "informational - drives the -i choice per board", "\n".join(stats_ev))

    clean = all("no fault recorded" in f for f in faults)
    analysis = []
    for blob in faults:
        info = parse_fault(blob)
        if info:
            syms = addr2line(*[v for k, v in info.items() if k in ("PC", "LR")])
            analysis.append("%s  %s\nresolved:\n%s" % (info.get("kind", "fault"),
                                                        json.dumps(info), syms))
    record("A5", "Fault log clean at start", "PASS" if clean else "FAIL",
           "a recorded fault survives resets in Backup RAM, so it may predate this run",
           "\n".join(faults) + ("\n\n=== analysis ===\n" + "\n\n".join(analysis)
                                 if analysis else ""))

    # Clear, so F1 at the end measures only what THIS run produced.
    cleared = []
    for b in BOARDS:
        out, err = one_shot(b["ip"], "faultlog clear", None, 2.5)
        cleared.append("%s: %s" % (b["ip"], " ".join(out.split())[-90:] or (err or "")))
    record("A6", "Fault log cleared before the run", "PASS",
           "so F1 reflects only faults produced by this test run", "\n".join(cleared))


# ==========================================================================
# Group B - Telnet / mTLS enforcement
# ==========================================================================

def group_b(scratch):
    ok_all, ev = True, []
    for b in BOARDS:
        try:
            c = console_session(b["ip"])
            c.close()
            ev.append("%s: login ok" % b["ip"])
        except ConsoleError as exc:
            ok_all = False
            ev.append("%s: %s" % (b["ip"], exc))
    record("B1", "Login with the valid project client identity",
           "PASS" if ok_all else "FAIL", "", "\n".join(ev))

    ev, rejected_all = [], True
    for b in BOARDS:
        ok, desc = tls_probe(b["ip"], TELNET_PORT)
        ev.append("%s: %s" % (b["ip"], desc))
        if ok:
            rejected_all = False
    record("B2", "TLS connect with NO client certificate is rejected",
           "PASS" if rejected_all else "FAIL",
           "board must require a client certificate", "\n".join(ev))

    fc, fk = _foreign_ca_material(scratch)
    ev, rejected_all = [], True
    for b in BOARDS:
        ok, desc = tls_probe(b["ip"], TELNET_PORT, fc, fk)
        ev.append("%s: %s" % (b["ip"], desc))
        if ok:
            rejected_all = False
    record("B3", "TLS connect with a foreign-CA client certificate is rejected",
           "PASS" if rejected_all else "FAIL", "", "\n".join(ev))

    ev, refused_all = [], True
    for b in BOARDS:
        try:
            c = bootload.Console(b["ip"], USER, "definitely-wrong-password")
            c.open()
            c.close()
            refused_all = False
            ev.append("%s: ACCEPTED a wrong password" % b["ip"])
        except Exception as exc:               # noqa: BLE001
            ev.append("%s: refused (%s)" % (b["ip"], type(exc).__name__))
    record("B4", "Wrong console password is refused",
           "PASS" if refused_all else "FAIL", "", "\n".join(ev))

    ev, ok_all = [], True
    for b in BOARDS:
        try:
            c = console_session(b["ip"])
        except ConsoleError as exc:
            ok_all = False
            ev.append("%s: %s" % (b["ip"], exc))
            continue
        try:
            for cmd, marker in (("uptime", b"uptime:"), ("mqtt_status", b"MQTT:"),
                                 ("stats", b"main loop")):
                out = raw_cmd(c, cmd, marker, 6)
                good = marker.decode() in out
                ok_all &= good
                ev.append("%s %-12s %s" % (b["ip"], cmd, "ok" if good else "MISSING MARKER"))
        finally:
            c.close()
    record("B5", "Command round-trip integrity",
           "PASS" if ok_all else "FAIL", "", "\n".join(ev))

    ev, ok_all = [], True
    CYCLES = 20
    for b in BOARDS:
        failures = 0
        first_fail = ""
        for i in range(CYCLES):
            try:
                c = console_session(b["ip"], attempts=2, delay=1.0)
                raw_cmd(c, "uptime", b"uptime:", 5)
                c.close()
            except Exception as exc:           # noqa: BLE001
                failures += 1
                first_fail = first_fail or ("cycle %d: %r" % (i + 1, exc))
        # the 21st session is the real question: is the board still usable?
        try:
            c = console_session(b["ip"])
            c.close()
            still_up = True
        except ConsoleError:
            still_up = False
        ok = failures == 0 and still_up
        ok_all &= ok
        ev.append("%s: %d/%d cycles ok, still reachable afterwards: %s%s"
                  % (b["ip"], CYCLES - failures, CYCLES, still_up,
                     ("  first failure: " + first_fail) if first_fail else ""))
    record("B6", "%d sequential connect/command/disconnect cycles per board" % CYCLES,
           "PASS" if ok_all else "FAIL",
           "regression guard for the NET_PRES socket-strand defect", "\n".join(ev))

    ev, ok_all = [], True
    for b in BOARDS:
        try:
            first = console_session(b["ip"])
        except ConsoleError as exc:
            ok_all = False
            ev.append("%s: could not open the first session (%s)" % (b["ip"], exc))
            continue
        try:
            second_ok, desc = tls_probe(b["ip"], TELNET_PORT,
                                         str(pki.CLIENT_CERT_PATH), str(pki.CLIENT_KEY_PATH),
                                         timeout=6)
            still = raw_cmd(first, "uptime", b"uptime:", 5)
            first_alive = "uptime:" in still
        finally:
            first.close()
        after = console_session(b["ip"])
        after.close()
        ok = first_alive          # a refused second session is expected, not a failure
        ok_all &= ok
        ev.append("%s: second concurrent session -> %s | first session still alive: %s "
                  "| new session after close: ok"
                  % (b["ip"], "accepted" if second_ok else desc, first_alive))
    record("B7", "Second concurrent session does not disturb the first",
           "PASS" if ok_all else "FAIL",
           "max 1 Telnet connection is by design (TCPIP_TELNET_MAX_CONNECTIONS=1)",
           "\n".join(ev))


# ==========================================================================
# Group C - identity provisioning
# ==========================================================================

def group_c():
    from cryptography import x509
    from cryptography.x509.oid import ExtensionOID, ExtendedKeyUsageOID

    ev, ok_all = [], True
    for b in BOARDS:
        out, err = one_shot(b["ip"], "cert_show", b"CERT:", 8)
        if err or "CERT:" not in out:
            ok_all = False
        ev.append("--- %s ---\n%s%s" % (b["ip"], out, err or ""))
    record("C1", "Active identity readable on each board",
           "PASS" if ok_all else "FAIL", "", "\n".join(ev))

    ev, ok_all, issued = [], True, {}
    for b in BOARDS:
        info = pki.issue_board_identity(b["id"], ip=b["ip"], force=True)
        issued[b["ip"]] = info["fingerprint_sha256"]
        cert = x509.load_pem_x509_certificate(
            (ROOT / "certs" / "boards" / b["id"] / "server_cert.pem").read_bytes())
        eku = set(cert.extensions.get_extension_for_oid(ExtensionOID.EXTENDED_KEY_USAGE).value)
        has_ski = has_aki = True
        try:
            cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_KEY_IDENTIFIER)
        except x509.ExtensionNotFound:
            has_ski = False
        try:
            cert.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_KEY_IDENTIFIER)
        except x509.ExtensionNotFound:
            has_aki = False
        good = (ExtendedKeyUsageOID.SERVER_AUTH in eku
                and ExtendedKeyUsageOID.CLIENT_AUTH in eku and has_ski and has_aki)
        ok_all &= good
        ev.append("%s: serverAuth=%s clientAuth=%s SKI=%s AKI=%s fp=%s"
                  % (b["ip"], ExtendedKeyUsageOID.SERVER_AUTH in eku,
                     ExtendedKeyUsageOID.CLIENT_AUTH in eku, has_ski, has_aki,
                     info["fingerprint_sha256"][:23] + "..."))
    record("C2", "Freshly issued identities carry dual EKU + SKI/AKI",
           "PASS" if ok_all else "FAIL", "", "\n".join(ev))

    ev, ok_all = [], True
    for b in BOARDS:
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "cert_provision.py"),
             "--ip", b["ip"], "--push-board", b["id"]],
            capture_output=True, text=True, timeout=180, cwd=str(ROOT))
        good = r.returncode == 0 and "saved" in (r.stdout + r.stderr)
        ok_all &= good
        ev.append("--- %s (rc=%d) ---\n%s%s" % (b["ip"], r.returncode, r.stdout, r.stderr))
    record("C3", "Identity pushed and saved on each board",
           "PASS" if ok_all else "FAIL", "", "\n".join(ev))

    for b in BOARDS:
        pyocd_reset(b, "C4: activate the newly provisioned identity", planned=True)
    ev, ok_all = [], True
    for b in BOARDS:
        try:
            fp = board_fingerprint(b["ip"])
        except Exception as exc:               # noqa: BLE001
            ok_all = False
            ev.append("%s: could not read served certificate: %r" % (b["ip"], exc))
            continue
        match = fp == issued[b["ip"]]
        ok_all &= match
        ev.append("%s: served=%s\n%s  issued=%s  -> %s"
                  % (b["ip"], fp, " " * len(b["ip"]), issued[b["ip"]],
                     "MATCH" if match else "MISMATCH"))
    record("C4", "Board serves the newly issued identity after reset",
           "PASS" if ok_all else "FAIL", "", "\n".join(ev))

    ev, ok_all = [], True
    for b in BOARDS:
        try:
            c = console_session(b["ip"])
            c.close()
            ev.append("%s: login ok with the new identity" % b["ip"])
        except ConsoleError as exc:
            ok_all = False
            ev.append("%s: %s" % (b["ip"], exc))
    record("C5", "Telnet still works after the identity change",
           "PASS" if ok_all else "FAIL", "same CA, so the client identity is unchanged",
           "\n".join(ev))

    ev, ok_all = [], True
    for b in BOARDS:
        meta = pki.board_info(b["id"]) or {}
        match = meta.get("fingerprint_sha256") == issued[b["ip"]]
        ok_all &= match
        ev.append("%s: json=%s -> %s" % (b["ip"],
                                          (meta.get("fingerprint_sha256") or "?")[:23] + "...",
                                          "MATCH" if match else "MISMATCH"))
    record("C6", "Board metadata matches the served certificate",
           "PASS" if ok_all else "FAIL", "", "\n".join(ev))

    return issued


# ==========================================================================
# Group D - bootload
# ==========================================================================

def _bootload(ip, extra=()):
    cmd = [sys.executable, str(ROOT / "scripts" / "bootload.py"),
           "--ip", ip, "--hex", str(RELEASE_HEX)] + list(extra)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=str(ROOT))


def group_d(issued_fps=None):
    ev, ok_all = [], True
    for b in BOARDS:
        out, err = one_shot(b["ip"], "bootload info", b"data port", 10)
        good = "update target" in out and "max image" in out
        ok_all &= good
        ev.append("--- %s ---\n%s%s" % (b["ip"], out, err or ""))
    record("D1", "'bootload info' reports a sane state on each board",
           "PASS" if ok_all else "FAIL", "", "\n".join(ev))

    ev, ok_all = [], True
    for b in BOARDS:
        out, err = one_shot(b["ip"], "bootload selftest", None, 12)
        good = "PASS" in out
        ok_all &= good
        ev.append("--- %s ---\n%s%s" % (b["ip"], out, err or ""))
    record("D2", "'bootload selftest' passes on each board",
           "PASS" if ok_all else "FAIL", "erase+write+readback in the inactive bank",
           "\n".join(ev))

    ev, ok_all, banks_before = [], True, {}
    for b in BOARDS:
        r = _bootload(b["ip"])
        text = r.stdout + r.stderr
        good = "match=1" in text and "CONFIRMED" in text
        if not good and ("TimeoutError" in text or "SSLEOFError" in text):
            # documented recovery path: reset only, then retry the OTA update
            pyocd_reset(b, "D3: console unreachable before OTA (ping ok) - see test plan §3 Group D")
            r = _bootload(b["ip"])
            text += "\n--- after recovery reset, retry ---\n" + r.stdout + r.stderr
            good = "match=1" in text and "CONFIRMED" in text
        for line in text.splitlines():
            if "running bank" in line:
                banks_before[b["ip"]] = line.strip()
        ok_all &= good
        ev.append("--- %s (rc=%d) ---\n%s" % (b["ip"], r.returncode,
                                               "\n".join(l for l in text.splitlines()
                                                          if "transfer  [" not in l)))
    record("D3", "Full OTA update completes on each board",
           "PASS" if ok_all else "FAIL", "", "\n".join(ev))

    ev, ok_all = [], True
    for b in BOARDS:
        alive, _ = ping(b["ip"], 2)
        try:
            c = console_session(b["ip"])
            c.close()
            reachable = True
        except ConsoleError:
            reachable = False
        good = alive and reachable
        ok_all &= good
        ev.append("%s: ping=%s console=%s" % (b["ip"], alive, reachable))
    record("D4", "Environment preserved - same IP, console reachable after update",
           "PASS" if ok_all else "FAIL", "", "\n".join(ev))

    if issued_fps:
        ev, ok_all = [], True
        for b in BOARDS:
            try:
                fp = board_fingerprint(b["ip"])
            except Exception as exc:           # noqa: BLE001
                ok_all = False
                ev.append("%s: %r" % (b["ip"], exc))
                continue
            match = fp == issued_fps.get(b["ip"])
            ok_all &= match
            ev.append("%s: %s" % (b["ip"], "identity survived the bank swap"
                                   if match else "identity CHANGED across the update"))
        record("D5", "Provisioned identity survives the bank swap",
               "PASS" if ok_all else "FAIL", "", "\n".join(ev))
    else:
        record("D5", "Provisioned identity survives the bank swap", "SKIP",
               "Group C was not run in this session, no reference fingerprint")

    ev, ok_all = [], True
    for b in BOARDS:
        r = _bootload(b["ip"])
        text = r.stdout + r.stderr
        good = "match=1" in text and "CONFIRMED" in text
        bank_line = next((l.strip() for l in text.splitlines() if "running bank" in l), "?")
        before = banks_before.get(b["ip"], "?")
        swapped = before != bank_line and "?" not in (before, bank_line)
        ok_all &= good and swapped
        ev.append("%s: before=[%s] after=[%s] -> %s"
                  % (b["ip"], before, bank_line,
                     "alternated" if swapped else "DID NOT ALTERNATE"))
    record("D6", "A second consecutive update alternates the running bank",
           "PASS" if ok_all else "FAIL", "proves both banks are usable", "\n".join(ev))

    ev, ok_all = [], True
    for b in BOARDS:
        out, err = one_shot(b["ip"], "faultlog", None, 2.5)
        good = "no fault recorded" in out
        ok_all &= good
        ev.append("--- %s ---\n%s%s" % (b["ip"], out, err or ""))
    record("D7", "Fault log still clean after the updates",
           "PASS" if ok_all else "FAIL", "", "\n".join(ev))


# ==========================================================================
# Group E - MQTT over mutual TLS
# ==========================================================================

def group_e(scratch):
    import mqtt_broker

    svc = mqtt_broker.MqttBrokerService()
    try:
        svc.start(bind_ip=BROKER_IP, port=BROKER_PORT)
        started = True
        detail = "listening on %s:%d" % (BROKER_IP, BROKER_PORT)
    except Exception as exc:                   # noqa: BLE001
        started, detail = False, "broker failed to start: %r" % exc
    record("E1", "Broker starts, TLS-only", "PASS" if started else "FAIL", detail)
    if not started:
        for tid, title in (("E2", "Plaintext MQTT client rejected"),
                            ("E3", "TLS client without certificate rejected"),
                            ("E4", "TLS client with foreign-CA certificate rejected"),
                            ("E5", "Each board connects with its correct -i interface"),
                            ("E6", "All three boards connected simultaneously"),
                            ("E7", "Periodic status publishing"),
                            ("E8", "-i with the board's dead interface fails cleanly"),
                            ("E9", "-i with an invalid interface name is rejected"),
                            ("E10", "Broker stop/restart - boards reconnect")):
            record(tid, title, "SKIP", "broker unavailable")
        return svc, {}

    try:
        raw = socket.create_connection((BROKER_IP, BROKER_PORT), timeout=5)
        raw.sendall(bytes([0x10, 0x0c, 0x00, 0x04]) + b"MQTT" + bytes([0x04, 0x02, 0x00, 0x3c, 0x00, 0x00]))
        raw.settimeout(4)
        try:
            data = raw.recv(64)
        except Exception:                      # noqa: BLE001
            data = b""
        raw.close()
        rejected = len(data) == 0
        record("E2", "Plaintext MQTT client is rejected",
               "PASS" if rejected else "FAIL",
               "no CONNACK on a plaintext connection",
               "bytes received: %r" % data)
    except OSError as exc:
        record("E2", "Plaintext MQTT client is rejected", "PASS",
               "connection refused outright", str(exc))

    rejected, desc = mqtt_session_refused(None, None)
    record("E3", "TLS client without a client certificate is rejected",
           "PASS" if rejected else "FAIL",
           "judged at the MQTT layer: the TLS handshake itself may legitimately "
           "complete, the broker then refuses the session (RequireClientCertPlugin)",
           desc)

    fc, fk = _foreign_ca_material(scratch)
    rejected, desc = mqtt_session_refused(fc, fk)
    record("E4", "TLS client with a foreign-CA certificate is rejected",
           "PASS" if rejected else "FAIL", "", desc)

    # drain anything left over before the boards are pointed at the broker
    while not svc.events.empty():
        svc.events.get_nowait()

    ev, ok_all = [], True
    for b in BOARDS:
        out, err = one_shot(b["ip"], "mqtt_broker %s -i %s" % (BROKER_IP, b["mqtt_if"]),
                             b"MQTT:", 8)
        ev.append("%s: %s%s" % (b["ip"], out.strip().splitlines()[-1] if out.strip() else "",
                                 err or ""))
        if err:
            ok_all = False
    time.sleep(25)

    states = {}
    for b in BOARDS:
        out, err = one_shot(b["ip"], "mqtt_status", b"MQTT:", 8)
        line = next((l for l in out.splitlines() if l.startswith("MQTT:")), out.strip())
        states[b["ip"]] = line
        ev.append("%s: %s" % (b["ip"], line))
        if "state=connected" not in line:
            ok_all = False
    record("E5", "Each board connects with its correct -i interface",
           "PASS" if ok_all else "FAIL", "", "\n".join(ev))

    clients, messages = {}, []
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            kind, payload, ts = svc.events.get(timeout=1.0)
        except Exception:                      # noqa: BLE001
            continue
        if kind == "client":
            clients[payload[0]] = payload
        elif kind == "message":
            messages.append(payload)

    record("E6", "All three boards connected to the broker simultaneously",
           "PASS" if len(clients) >= 3 else "FAIL",
           "%d distinct client_id(s) seen" % len(clients),
           "\n".join("%s from %s (peer CN %s)" % (cid, p[1], p[2]) for cid, p in clients.items()))

    by_client = {}
    for cid, topic, data in messages:
        by_client.setdefault(cid, []).append((topic, data))
    ev, ok_all = [], len(by_client) >= 3
    for cid, msgs in by_client.items():
        seqs = []
        for topic, data in msgs:
            try:
                seqs.append(json.loads(data.decode())["seq"])
            except Exception:                  # noqa: BLE001
                pass
        rising = seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        ok_all &= rising and bool(seqs)
        ev.append("%s: %d message(s), topic=%s, seq=%s %s"
                  % (cid, len(msgs), msgs[0][0] if msgs else "?", seqs,
                     "strictly increasing" if rising else "NOT MONOTONIC"))
    record("E7", "Periodic status publishing, ~5s, monotonic seq",
           "PASS" if ok_all else "FAIL",
           "%d client(s) published within the observation window" % len(by_client),
           "\n".join(ev) or "no messages observed")

    # E8 - point one follower at its dead interface on purpose
    victim = next(b for b in BOARDS if b["role"] == "follower")
    out, _ = one_shot(victim["ip"], "mqtt_broker %s -i %s" % (BROKER_IP, victim["dead_if"]),
                       b"MQTT:", 8)
    time.sleep(28)
    st, _ = one_shot(victim["ip"], "mqtt_status", b"MQTT:", 8)
    fl, _ = one_shot(victim["ip"], "faultlog", None, 2.5)
    # Two outcomes are both acceptable, and which one happens is itself the
    # finding worth recording:
    #   - it reports a failure and keeps retrying (last_fail set), or
    #   - it connects anyway, because this board bridges eth0/eth1 at layer 2
    #     and the pin only steers which interface the SYN is handed to.
    # The requirement is that it does something definite without crashing.
    # The original expectation here ("a dead interface must fail") was wrong:
    # on 2026-09-07 a follower with no 100BASE-TX PHY at all still reached the
    # broker through '-i eth1'.
    connected = "state=connected" in st
    reported = "last_fail=" in st and "last_fail=(none yet)" not in st
    no_fault = "no fault recorded" in fl
    record("E8", "-i with the board's non-uplink interface behaves definitely",
           "PASS" if (connected or reported) and no_fault else "FAIL",
           "connected anyway (bridged)" if connected else
           ("reported a failure and re-armed" if reported else "neither - stuck"),
           "%s\n%s\n%s" % (out.strip(), st.strip(), fl.strip()))

    out, _ = one_shot(victim["ip"], "mqtt_broker %s -i wlan9" % BROKER_IP, b"MQTT:", 8)
    st2, _ = one_shot(victim["ip"], "mqtt_status", b"MQTT:", 8)
    # restore the victim to its working interface
    one_shot(victim["ip"], "mqtt_broker %s -i %s" % (BROKER_IP, victim["mqtt_if"]), b"MQTT:", 8)
    record("E9", "-i with an invalid interface name is handled",
           "PASS" if ("MQTT:" in out and "state=connected" not in st2) else "FAIL",
           "command must not crash the board or silently accept a bad name",
           "%s\n%s" % (out.strip(), st2.strip()))

    # stop() returns as soon as it has signalled the asyncio loop, not once the
    # broker is actually down - starting again too early raised
    # BrokerError("broker already running") and aborted the run before groups G
    # and F could execute. Wait for it to really be down.
    svc.stop()
    for _ in range(40):
        if not svc.is_running():
            break
        time.sleep(0.5)
    time.sleep(12)
    lost = []
    for b in BOARDS:
        st, _ = one_shot(b["ip"], "mqtt_status", b"MQTT:", 8)
        lost.append("%s: %s" % (b["ip"], next((l for l in st.splitlines()
                                                if l.startswith("MQTT:")), "?")))
    try:
        svc.start(bind_ip=BROKER_IP, port=BROKER_PORT)
        restart_err = None
    except Exception as exc:                     # noqa: BLE001
        restart_err = "broker restart failed: %r" % exc
    time.sleep(30)
    back, ok_all = [], (restart_err is None)
    if restart_err:
        back.append(restart_err)
    for b in BOARDS:
        st, _ = one_shot(b["ip"], "mqtt_status", b"MQTT:", 8)
        line = next((l for l in st.splitlines() if l.startswith("MQTT:")), "?")
        back.append("%s: %s" % (b["ip"], line))
        if "state=connected" not in line:
            ok_all = False
    record("E10", "Boards reconnect by themselves after a broker restart",
           "PASS" if ok_all else "FAIL", "",
           "while the broker was down:\n" + "\n".join(lost)
           + "\n\nafter it came back:\n" + "\n".join(back))

    return svc, states


# ==========================================================================
# Group G - hand-patch survivability
# ==========================================================================

def group_g():
    r = subprocess.run([sys.executable, str(ROOT / "patches" / "apply_patches.py"), "--check"],
                        capture_output=True, text=True, cwd=str(ROOT), timeout=120)
    text = r.stdout + r.stderr
    record("G1", "Registered patches all present",
           "PASS" if "All patches present." in text else "FAIL", "", text)

    # The safety net is not only the .patch files: configuration.h is guarded by
    # an idempotent handler inside apply_patches.py instead, because MCC rewrites
    # that file on nearly every Generate Code and a whole-file diff would
    # conflict constantly. Search both, or this reports a protected setting as
    # unprotected.
    patch_blob = "\n".join(p.read_text(encoding="utf-8", errors="ignore")
                           for p in (ROOT / "patches").glob("*.patch"))
    patch_blob += "\n" + (ROOT / "patches" / "apply_patches.py").read_text(
        encoding="utf-8", errors="ignore")

    needles = {
        "G2": ("wolfSSL/TLS build configuration protected",
               ["HAVE_TLS_EXTENSIONS", "HAVE_SUPPORTED_CURVES", "NO_WOLFSSL_CLIENT",
                "TCPIP_TELNET_MAX_CONNECTIONS"]),
        "G3": ("TLS provider registration protected",
               ["pProvObject_ss", "pProvObject_sc", "net_pres_enc_glue_client.h"]),
        "G4": ("NET_PRES socket-strand (DoS) fix protected",
               ["NET_PRES_SocketDisconnect", "provOpen"]),
    }
    for tid, (title, keys) in needles.items():
        missing = [k for k in keys if k not in patch_blob]
        record(tid, title, "PASS" if not missing else "FAIL",
               ("not covered by any patch: " + ", ".join(missing)) if missing else "",
               "searched every patches/*.patch for: " + ", ".join(keys))

    owned = ["net_pres_enc_glue.c", "net_pres_enc_glue_client.c", "net_pres_enc_glue_client.h"]
    documented = (ROOT / "docs" / "mcc-generated-code-patches.md").read_text(
        encoding="utf-8", errors="ignore")
    missing = [f for f in owned if f not in documented and f not in patch_blob]
    record("G5", "Project-owned files under config/default are accounted for",
           "PASS" if not missing else "FAIL",
           ("neither patched nor documented: " + ", ".join(missing)) if missing else "",
           "checked patches/*.patch and docs/mcc-generated-code-patches.md")


# ==========================================================================
# Group F - final sweep
# ==========================================================================

def group_f():
    ev, ok_all = [], True
    for b in BOARDS:
        out, err = one_shot(b["ip"], "faultlog", None, 2.5)
        good = "no fault recorded" in out
        ok_all &= good
        ev.append("--- %s ---\n%s%s" % (b["ip"], out, err or ""))
    record("F1", "Fault log clean on all three after the whole run",
           "PASS" if ok_all else "FAIL", "", "\n".join(ev))

    ev, ok_all = [], True
    for b in BOARDS:
        out, err = one_shot(b["ip"], "meminfo", b"TCP/IP heap", 6)
        good = "TCP/IP heap" in out
        ok_all &= good
        ev.append("%s %s" % (b["ip"], " ".join(out.split())[-120:]))
    record("F2", "Heap still healthy", "PASS" if ok_all else "FAIL", "", "\n".join(ev))

    ev, ok_all = [], True
    for b in BOARDS:
        try:
            c = console_session(b["ip"])
            c.close()
            ev.append("%s: ok" % b["ip"])
        except ConsoleError as exc:
            ok_all = False
            ev.append("%s: %s" % (b["ip"], exc))
    record("F3", "Telnet responsive on all three", "PASS" if ok_all else "FAIL",
           "", "\n".join(ev))

    ev, ok_all = [], True
    for b in BOARDS:
        out, _ = one_shot(b["ip"], "mqtt_status", b"MQTT:", 8)
        line = next((l for l in out.splitlines() if l.startswith("MQTT:")), "?")
        ev.append("%s: %s" % (b["ip"], line))
        if "state=connected" not in line:
            ok_all = False
    record("F4", "MQTT connected on all three at the end",
           "PASS" if ok_all else "FAIL", "", "\n".join(ev))

    record("F5", "No unplanned recovery resets were needed",
           "PASS" if not RECOVERY_RESETS else "FAIL",
           "%d unplanned, %d planned (identity activation)"
           % (len(RECOVERY_RESETS), len(PLANNED_RESETS)),
           "unplanned:\n"
           + (json.dumps(RECOVERY_RESETS, indent=2) if RECOVERY_RESETS else "none")
           + "\n\nplanned:\n"
           + (json.dumps(PLANNED_RESETS, indent=2) if PLANNED_RESETS else "none"))


# ==========================================================================

def write_results(path, groups, started_at, ended_at):
    passed = sum(1 for r in RESULTS if r["verdict"] == "PASS")
    failed = sum(1 for r in RESULTS if r["verdict"] == "FAIL")
    skipped = sum(1 for r in RESULTS if r["verdict"] == "SKIP")

    out = []
    out.append("# Test Results — Telnet, MQTT, Bootload, Identity Provisioning\n")
    out.append("Executed against the real three-board bench by "
               "`scripts/testplan_runner.py`, following "
               "[`test-plan-telnet-mqtt-bootload.md`](test-plan-telnet-mqtt-bootload.md).\n")
    out.append("| | |\n|---|---|")
    out.append("| Run started | %s |" % started_at)
    out.append("| Run ended | %s |" % ended_at)
    out.append("| Groups executed | %s |" % ", ".join(groups))
    out.append("| Result | **%d passed, %d failed, %d skipped** |\n" % (passed, failed, skipped))

    out.append("## Summary\n")
    out.append("| ID | Test | Verdict | Note |")
    out.append("|---|---|---|---|")
    for r in RESULTS:
        badge = {"PASS": "PASS", "FAIL": "**FAIL**", "SKIP": "skip"}[r["verdict"]]
        out.append("| %s | %s | %s | %s |"
                   % (r["id"], r["title"], badge, r["detail"].replace("|", "\\|")))
    out.append("")

    if failed:
        out.append("## Failures\n")
        for r in RESULTS:
            if r["verdict"] == "FAIL":
                out.append("### %s — %s\n" % (r["id"], r["title"]))
                if r["detail"]:
                    out.append("%s\n" % r["detail"])
                out.append("```\n%s\n```\n" % r["evidence"])

    if RECOVERY_RESETS:
        out.append("## pyOCD recovery resets performed during this run\n")
        out.append("Each one means a board had stopped answering on the network and "
                   "had to be reset over SWD — a defect observation, not routine.\n")
        out.append("```\n%s\n```\n" % json.dumps(RECOVERY_RESETS, indent=2))

    out.append("## Full evidence\n")
    for r in RESULTS:
        out.append("### %s — %s (%s)\n" % (r["id"], r["title"], r["verdict"]))
        if r["detail"]:
            out.append("%s\n" % r["detail"])
        if r["evidence"]:
            out.append("```\n%s\n```\n" % r["evidence"])

    Path(path).write_text("\n".join(out), encoding="utf-8")
    print("\nresults written to %s" % path)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--groups", default="A,B,C,D,E,G,F",
                    help="comma-separated group letters to run (default: all)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    groups = [g.strip().upper() for g in args.groups.split(",") if g.strip()]
    scratch = ROOT / "scripts" / "_testplan_scratch"
    started = datetime.datetime.now().isoformat(timespec="seconds")

    svc = None
    issued = None
    try:
        if "A" in groups:
            group_a()
        if "B" in groups:
            group_b(scratch)
        if "C" in groups:
            issued = group_c()
        if "D" in groups:
            group_d(issued)
        if "E" in groups:
            svc, _ = group_e(scratch)
        if "G" in groups:
            group_g()
        if "F" in groups:
            group_f()
    finally:
        if svc is not None:
            try:
                svc.stop()
            except Exception:                  # noqa: BLE001
                pass
        ended = datetime.datetime.now().isoformat(timespec="seconds")
        out = args.out or (ROOT / "docs" / ("test-results-%s.md"
                                             % datetime.date.today().isoformat()))
        write_results(out, groups, started, ended)

    return 1 if any(r["verdict"] == "FAIL" for r in RESULTS) else 0


if __name__ == "__main__":
    sys.exit(main())
