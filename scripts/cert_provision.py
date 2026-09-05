#!/usr/bin/env python3
"""
Push a new TLS server identity (or a new trusted CA) onto a board over the
network - the client side of firmware/src/cert_provision.c. Same split as
bootload.py, on purpose: control ('cert arm'/'cert show'/'cert save') goes
over the same TLS+mTLS Telnet console, the certificate/key bytes themselves
over their own binary port (default 5568) - see cert_provision.h for why
(an 80-byte Telnet line buffer cannot carry a ~1 KB certificate as hex text).

Reuses bootload.py's Console class as-is (same TLS-wrapped login, same
command/reply framing) rather than duplicating it - this project's boards
only ever have the one console.

    python scripts/cert_provision.py --ip 192.168.0.12 --board-id my-board \\
        --item server_cert --file certs/boards/my-board/server_cert.der
    python scripts/cert_provision.py --ip 192.168.0.12 --show
    python scripts/cert_provision.py --ip 192.168.0.12 --push-board my-board
        # sends server_cert + server_key for that board (from pki.py's
        # certs/boards/<id>/*.der), then 'cert save' - still needs a device
        # 'reset' afterwards to actually take effect, same as bootload.
"""

import argparse
import socket
import ssl
import sys
import zlib
from pathlib import Path

from bootload import Console, BootloadError, TELNET_PORT  # noqa: F401  (reuse, not redefine)
import pki

CERT_PORT = 5568
LOGIN_TIMEOUT = 10.0
REPLY_TIMEOUT = 5.0

TLS_CA_CERT = Path(__file__).parent.parent / "certs" / "ca" / "ca_cert.pem"
TLS_CLIENT_CERT = Path(__file__).parent.parent / "certs" / "client" / "client_cert.pem"
TLS_CLIENT_KEY = Path(__file__).parent.parent / "certs" / "client" / "client_key.pem"


def _wrap_tls(sock, host):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile=str(TLS_CA_CERT))
    ctx.load_cert_chain(certfile=str(TLS_CLIENT_CERT), keyfile=str(TLS_CLIENT_KEY))
    ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
    return ctx.wrap_socket(sock, server_hostname=host)


def send_item(console: Console, ip: str, item: str, data: bytes, log=print):
    """One 'cert arm' + the binary transfer for one item
    (server_cert|server_key|ca_cert). Raises BootloadError on anything that
    stops it short - the board's own EEPROM is untouched either way ('cert
    save' is a separate, explicit step, see push_board() / __main__)."""
    if item not in ("server_cert", "server_key", "ca_cert"):
        raise BootloadError("unknown item %r" % item)
    if len(data) == 0 or len(data) > 1280:
        raise BootloadError("%s is %d bytes, must be 1..1280 (see CERT_MAX_DER)"
                            % (item, len(data)))
    crc = zlib.crc32(data) & 0xFFFFFFFF

    reply = console.command("cert_arm %s %d %08X" % (item, len(data), crc),
                            markers=(b"CERT: ",))
    log(reply)
    if "READY" not in reply:
        raise BootloadError("board did not arm %s: %s" % (item, reply))

    data_sock_raw = socket.create_connection((ip, CERT_PORT), timeout=10.0)
    try:
        data_sock = _wrap_tls(data_sock_raw, ip)
    except Exception:
        data_sock_raw.close()
        raise
    try:
        data_sock.sendall(data)
        data_sock.settimeout(30.0)          # matches bootload.py's send_image(): the
        closing = b""                       # board may sit in WAIT_CONN/RECV for a
        while b"\n" not in closing:         # few cooperative-superloop ticks before
            part = data_sock.recv(256)      # it gets to writing the reply - no tight
            if not part:                    # outer deadline here, same as bootload.py.
                break
            closing += part
    finally:
        data_sock.close()

    text = closing.decode("latin-1", "ignore").strip()
    log(text or "(board closed the data connection without a reply)")
    if "CERT: OK" not in text:
        raise BootloadError("%s transfer/verify failed: %s" % (item, text or "no reply"))
    return text


def push_board(ip: str, board_id: str, user: str = "admin", password: str = "password",
               include_ca: bool = False, log=print):
    """Send a board's server_cert + server_key (from pki.py's
    certs/boards/<board_id>/*.der) and 'cert save' them. Still needs a
    'reset' on the device afterwards - same two-step as bootload's
    verify-then-commit, deliberately not automatic."""
    cert_der, key_der = pki.board_der(board_id)

    console = Console(ip, user, password)
    console.open()
    try:
        log(console.command("cert_show", markers=(b"CERT: ",)))
        send_item(console, ip, "server_cert", cert_der, log=log)
        send_item(console, ip, "server_key", key_der, log=log)
        if include_ca:
            ca_der, _ = pki.CA_CERT_PATH.read_bytes(), None
            send_item(console, ip, "ca_cert", ca_der, log=log)
        reply = console.command("cert_save", markers=(b"CERT: ",))
        log(reply)
        if "saved" not in reply:
            raise BootloadError("cert_save did not confirm: %s" % reply)
        log("Pushed '%s' - the board is still running its OLD identity until "
            "'reset' (existing connections, including this one, are unaffected)."
            % board_id)
    finally:
        console.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ip", required=True)
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="password")
    ap.add_argument("--show", action="store_true", help="just 'cert_show' and exit")
    ap.add_argument("--item", choices=["server_cert", "server_key", "ca_cert"])
    ap.add_argument("--file", help="DER file to send for --item")
    ap.add_argument("--push-board", metavar="BOARD_ID",
                    help="send that board's server_cert+server_key from pki.py, then cert_save")
    ap.add_argument("--include-ca", action="store_true",
                    help="with --push-board, also send certs/ca/ca_cert.der")
    args = ap.parse_args()

    try:
        if args.show:
            c = Console(args.ip, args.user, args.password)
            c.open()
            try:
                print(c.command("cert_show", markers=(b"CERT: ",)))
            finally:
                c.close()
        elif args.push_board:
            push_board(args.ip, args.push_board, args.user, args.password,
                      include_ca=args.include_ca)
        elif args.item and args.file:
            data = Path(args.file).read_bytes()
            c = Console(args.ip, args.user, args.password)
            c.open()
            try:
                send_item(c, args.ip, args.item, data)
            finally:
                c.close()
        else:
            ap.error("need --show, --push-board, or --item + --file")
    except BootloadError as e:
        print("ERROR:", e, file=sys.stderr)
        sys.exit(1)
