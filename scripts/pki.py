#!/usr/bin/env python3
"""
Certificate/PKI management for this project's boards.

Standalone and GUI-free on purpose, like bootload.py: the "Certificates" tab
in bridge_gui_telnet.py is a thin wrapper around this module, so cert
creation/inspection can be driven from a plain Python shell too.

One project CA (certs/ca/ca_cert.pem + ca_key.pem, RSA-2048, generated
with openssl earlier this session - see docs/tls-poc-report.md for why it
replaced wolfSSL's own test PKI). This module adds what that first pass
didn't have: a distinct server identity *per board* instead of one identity
shared by every flashed board, plus a JSON record per board so a fleet of
them can be told apart and managed together.

Layout:
    certs/ca/ca_cert.pem, ca_key.pem          - the one project CA, kept in
                                                 its own directory (not mixed
                                                 with any leaf identity) since
                                                 it is the one file that must
                                                 eventually move offline - see
                                                 docs/tls-poc-report.md §8/§12
    certs/client/client_cert.pem, client_key.pem - the one shared operator
                                                    (GUI/bootload.py) identity,
                                                    also its own directory
    certs/boards/<board_id>/server_cert.pem, server_key.pem, *.der
                                               - one leaf identity per board
    json/boards/<board_id>.json                - that board's metadata:
                                                  fingerprint, dates, probe
                                                  serial/IP if known

NO_ASN_TIME is set on every board (see docs/tls-poc-report.md section 2.5 -
no RTC, no NTP): the notBefore/notAfter dates below are written into every
certificate because X.509 requires them, but the firmware never checks
them. They are not a real expiry mechanism here, only documentation of
intent.
"""

import datetime
import ipaddress
import json
import shutil
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

REPO_ROOT = Path(__file__).parent.parent
CERTS_DIR = REPO_ROOT / "certs"
CA_DIR = CERTS_DIR / "ca"          # the CA only - see the module docstring for why it's separate
CLIENT_DIR = CERTS_DIR / "client"  # the one shared operator (Telnet client) identity, its own directory too
BOARDS_CERT_DIR = CERTS_DIR / "boards"
BOARDS_JSON_DIR = REPO_ROOT / "json" / "boards"

CA_CERT_PATH = CA_DIR / "ca_cert.pem"
CA_KEY_PATH = CA_DIR / "ca_key.pem"
CLIENT_CERT_PATH = CLIENT_DIR / "client_cert.pem"
CLIENT_KEY_PATH = CLIENT_DIR / "client_key.pem"

# MQTT broker identity (docs/mqtt-tls-agent-prompt.md): the broker
# (scripts/mqtt_broker.py) needs its own SERVER_AUTH leaf so boards can
# verify it against the project CA, same shape as issue_board_identity()
# below - its own directory, not certs/boards/<id>/, since the broker is not
# "a board". The board's OWN side of that mTLS handshake does NOT get a new
# identity here: it reuses whatever cert_provision.c already manages on that
# board (CERT_PROVISION_ActiveServerIdentity() / ActiveCa()) - the same
# cert/key it already presents for Telnet/bootload - rather than a second,
# unmanaged identity that cert_arm/cert_save/cert_show could never reach.
MQTT_DIR = CERTS_DIR / "mqtt"
MQTT_BROKER_CERT_PATH = MQTT_DIR / "mqtt_broker_cert.pem"
MQTT_BROKER_KEY_PATH = MQTT_DIR / "mqtt_broker_key.pem"

KEY_SIZE = 2048
VALIDITY_DAYS = 7300  # ~20 years - see NO_ASN_TIME note above
ORG_NAME = "bridge_lan865x_100baseT"


class PkiError(Exception):
    """Anything that stops a PKI operation - never leaves a half-written
    cert/key pair or a JSON file describing one that isn't actually there."""


# -----------------------------------------------------------------------
# Low-level cert/key helpers
# -----------------------------------------------------------------------

def _new_keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)


def _write_pem_cert(path: Path, cert: x509.Certificate):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _write_pem_key(path: Path, key):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,  # PKCS#1 - matches
        encryption_algorithm=serialization.NoEncryption(),      # what bridge_certs.h expects
    ))


def _write_der(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def fingerprint(cert: x509.Certificate) -> str:
    """SHA-256 fingerprint, colon-separated hex - the usual display form."""
    digest = cert.fingerprint(hashes.SHA256())
    return ":".join("%02X" % b for b in digest)


def _sign_leaf(name: str, public_key, ca_cert: x509.Certificate, ca_key,
               eku, sans=None) -> x509.Certificate:
    # eku may be a single ExtendedKeyUsageOID or a list of them - a board's
    # identity needs both SERVER_AUTH (Telnet/bootload/cert_provision) and
    # CLIENT_AUTH (this same identity is reused as the MQTT client's
    # certificate - see the comment above issue_mqtt_broker_identity()).
    eku_list = eku if isinstance(eku, (list, tuple)) else [eku]
    # SubjectKeyIdentifier/AuthorityKeyIdentifier are not optional in practice:
    # found 2026-09-06 issuing the MQTT broker identity (docs/mqtt-tls-agent-prompt.md)
    # - a leaf without them failed TLS verification on this machine's OpenSSL with
    # "Missing Authority Key Identifier", even though the chain itself was otherwise
    # correct (verified with `openssl verify`, which is lenient about this). Every
    # leaf this function has ever issued (board/client/broker identities) was missing
    # both - re-issue if a client is seen failing verification with that error.
    subject = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, ORG_NAME),
        x509.NameAttribute(NameOID.COMMON_NAME, name),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage(eku_list), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()),
            critical=False)
    )
    if sans:
        builder = builder.add_extension(x509.SubjectAlternativeName(sans), critical=False)
    return builder.sign(ca_key, hashes.SHA256())


# -----------------------------------------------------------------------
# CA
# -----------------------------------------------------------------------

def ca_exists() -> bool:
    return CA_CERT_PATH.is_file() and CA_KEY_PATH.is_file()


def load_ca():
    """Returns (ca_cert, ca_key). Raises PkiError if not present -
    create_ca() first."""
    if not ca_exists():
        raise PkiError("no project CA at %s / %s - call create_ca() first" %
                        (CA_CERT_PATH, CA_KEY_PATH))
    ca_key = serialization.load_pem_private_key(CA_KEY_PATH.read_bytes(), password=None)
    ca_cert = x509.load_pem_x509_certificate(CA_CERT_PATH.read_bytes())
    return ca_cert, ca_key


def create_ca(force: bool = False):
    """Create the one project CA. Refuses to overwrite an existing one unless
    force=True - replacing the CA invalidates every certificate it ever
    signed (every board's server identity, the shared client identity), so
    this is deliberately not the default."""
    if ca_exists() and not force:
        raise PkiError("a CA already exists at %s - pass force=True to "
                        "replace it (this invalidates every certificate it "
                        "signed, on every board)" % CA_CERT_PATH)

    key = _new_keypair()
    subject = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, ORG_NAME),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "experimental TLS bring-up"),
        x509.NameAttribute(NameOID.COMMON_NAME, "bridge-test-ca"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True,
            crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
        .sign(key, hashes.SHA256())
    )
    _write_pem_cert(CA_CERT_PATH, cert)
    _write_pem_key(CA_KEY_PATH, key)
    return cert, key


# -----------------------------------------------------------------------
# Shared client (operator) identity
# -----------------------------------------------------------------------

def issue_client_identity(name: str = "bridge-client", force: bool = False):
    """The one client identity scripts/bridge_gui_telnet.py and
    scripts/bootload.py present to every board. Overwriting it (force=True)
    means every board must also be reprovisioned with the new CA-verified
    trust - it doesn't need to be, since the CA itself doesn't change here,
    only this leaf. Existing boards keep trusting it automatically as long
    as it is still signed by the same CA."""
    if CLIENT_CERT_PATH.is_file() and not force:
        raise PkiError("a client identity already exists at %s - pass "
                        "force=True to replace it" % CLIENT_CERT_PATH)
    ca_cert, ca_key = load_ca()
    key = _new_keypair()
    cert = _sign_leaf(name, key.public_key(), ca_cert, ca_key,
                       ExtendedKeyUsageOID.CLIENT_AUTH)
    _write_pem_cert(CLIENT_CERT_PATH, cert)
    _write_pem_key(CLIENT_KEY_PATH, key)
    return cert, key


# -----------------------------------------------------------------------
# MQTT broker identity (docs/mqtt-tls-agent-prompt.md): the broker's own TLS
# SERVER identity. From the same project CA as everything else, so the
# existing trust anchor every board already has (bridge_certs.h's compiled-in
# CA / CERT_PROVISION_ActiveCa()) verifies it with no firmware-side change.
# There is deliberately no matching issue_mqtt_client_identity(): the board's
# side of this mTLS handshake reuses whatever cert_provision.c already
# manages on that board (its existing server cert/key) rather than a second,
# unmanaged identity - see the MQTT_DIR comment above.
# -----------------------------------------------------------------------

def issue_mqtt_broker_identity(name: str = "bridge-mqtt-broker", force: bool = False):
    """The broker's own TLS server identity (scripts/mqtt_broker.py) - what
    boards verify against the project CA before they trust the broker enough
    to present their own client certificate."""
    if MQTT_BROKER_CERT_PATH.is_file() and not force:
        raise PkiError("an MQTT broker identity already exists at %s - pass "
                        "force=True to replace it" % MQTT_BROKER_CERT_PATH)
    ca_cert, ca_key = load_ca()
    key = _new_keypair()
    cert = _sign_leaf(name, key.public_key(), ca_cert, ca_key,
                       ExtendedKeyUsageOID.SERVER_AUTH)
    _write_pem_cert(MQTT_BROKER_CERT_PATH, cert)
    _write_pem_key(MQTT_BROKER_KEY_PATH, key)
    return cert, key


# -----------------------------------------------------------------------
# Per-board server identity
# -----------------------------------------------------------------------

def _board_json_path(board_id: str) -> Path:
    return BOARDS_JSON_DIR / ("%s.json" % board_id)


def _board_cert_dir(board_id: str) -> Path:
    return BOARDS_CERT_DIR / board_id


def list_boards():
    """Every known board's metadata (json/boards/*.json), newest first."""
    if not BOARDS_JSON_DIR.is_dir():
        return []
    out = []
    for path in sorted(BOARDS_JSON_DIR.glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    out.sort(key=lambda b: b.get("created", ""), reverse=True)
    return out


def board_info(board_id: str) -> Optional[dict]:
    path = _board_json_path(board_id)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def delete_board(board_id: str) -> bool:
    """Forget one board: its json/boards/<id>.json record and its
    certs/boards/<id>/ key material. Returns False if there was nothing to
    delete. Purely host-side - the board itself keeps whatever identity is
    saved in its EEPROM until someone runs 'cert_reset' + 'reset' on it (see
    docs/pki-clean-start.md, which is the reason this exists: starting from
    a clean checkout means the host's list of boards has to be emptied
    first, so nothing here claims to know a board it can no longer prove
    anything about)."""
    removed = False
    path = _board_json_path(board_id)
    if path.is_file():
        path.unlink()
        removed = True
    cert_dir = _board_cert_dir(board_id)
    if cert_dir.is_dir():
        shutil.rmtree(cert_dir)
        removed = True
    return removed


def delete_all_boards() -> list:
    """Forget every board (see delete_board()). Returns the board_ids that
    were removed. Leaves the CA, the client identity and the MQTT broker
    identity alone - those are not boards."""
    gone = []
    for b in list_boards():
        if delete_board(b["board_id"]):
            gone.append(b["board_id"])
    # Also sweep certs/boards/<id>/ directories with no json record left -
    # a half-deleted state, or an identity issued before the json existed.
    if BOARDS_CERT_DIR.is_dir():
        for d in sorted(BOARDS_CERT_DIR.iterdir()):
            if d.is_dir():
                shutil.rmtree(d)
                gone.append(d.name)
    return gone


def issue_board_identity(board_id: str, ip: str = "", probe_serial: str = "",
                          force: bool = False) -> dict:
    """Generate a fresh RSA-2048 keypair + leaf certificate for ONE board,
    signed by the project CA, distinct from every other board's. Writes:

      certs/boards/<board_id>/server_cert.pem, server_key.pem
      certs/boards/<board_id>/server_cert.der, server_key.der  (for
          firmware embedding - see scripts/gen_bridge_certs.py)
      json/boards/<board_id>.json  (fingerprint + metadata, so a fleet of
          boards can be told apart and tracked from one place)

    board_id becomes both the certificate's CN and the file/JSON name -
    keep it short and filesystem-safe (e.g. a probe serial or a short
    hostname), not free text.
    """
    if not board_id or any(c in board_id for c in r'\/:*?"<>|'):
        raise PkiError("board_id %r is not a safe file name" % board_id)

    json_path = _board_json_path(board_id)
    if json_path.is_file() and not force:
        raise PkiError("board '%s' already has an identity (%s) - pass "
                        "force=True to reissue it" % (board_id, json_path))

    ca_cert, ca_key = load_ca()
    key = _new_keypair()
    sans = None
    if ip:
        try:
            sans = [x509.IPAddress(ipaddress.ip_address(ip))]
        except ValueError:
            sans = [x509.DNSName(ip)]
    cert = _sign_leaf(board_id, key.public_key(), ca_cert, ca_key,
                       [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH],
                       sans=sans)

    cert_dir = _board_cert_dir(board_id)
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    key_der = key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption())

    _write_pem_cert(cert_dir / "server_cert.pem", cert)
    _write_pem_key(cert_dir / "server_key.pem", key)
    _write_der(cert_dir / "server_cert.der", cert_der)
    _write_der(cert_dir / "server_key.der", key_der)

    info = {
        "board_id": board_id,
        "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "ip": ip,
        "probe_serial": probe_serial,
        "fingerprint_sha256": fingerprint(cert),
        "serial_number": format(cert.serial_number, "x"),
        "not_valid_before": cert.not_valid_before_utc.isoformat(),
        "not_valid_after": cert.not_valid_after_utc.isoformat(),
        "cert_pem": str((cert_dir / "server_cert.pem").relative_to(REPO_ROOT)),
        "key_pem": str((cert_dir / "server_key.pem").relative_to(REPO_ROOT)),
        "provisioned": False,  # set True once actually pushed to the board - see cert_provision.py
    }
    BOARDS_JSON_DIR.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return info


def board_der(board_id: str):
    """(cert_der_bytes, key_der_bytes) for one board - what
    scripts/cert_provision.py sends over the wire, or what a firmware
    rebuild would embed."""
    cert_dir = _board_cert_dir(board_id)
    cert_path, key_path = cert_dir / "server_cert.der", cert_dir / "server_key.der"
    if not cert_path.is_file() or not key_path.is_file():
        raise PkiError("no DER cert/key for board '%s' under %s" % (board_id, cert_dir))
    return cert_path.read_bytes(), key_path.read_bytes()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ca-status")

    p = sub.add_parser("issue-board")
    p.add_argument("board_id")
    p.add_argument("--ip", default="")
    p.add_argument("--force", action="store_true")

    sub.add_parser("list-boards")

    p = sub.add_parser("issue-client")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("delete-board")
    p.add_argument("board_id")

    # Deliberately spelled out rather than a --all flag on delete-board: this
    # is the first step of a clean start (docs/pki-clean-start.md) and should
    # be hard to type by accident.
    p = sub.add_parser("delete-all-boards")
    p.add_argument("--yes", action="store_true", help="required - confirms the deletion")

    p = sub.add_parser("issue-mqtt-broker")
    p.add_argument("--force", action="store_true")

    args = ap.parse_args()

    if args.cmd == "ca-status":
        if ca_exists():
            cert, _ = load_ca()
            print("CA present:", CA_CERT_PATH)
            print("  subject:", cert.subject.rfc4514_string())
            print("  fingerprint:", fingerprint(cert))
        else:
            print("no CA yet at", CA_CERT_PATH)
    elif args.cmd == "issue-board":
        info = issue_board_identity(args.board_id, ip=args.ip, force=args.force)
        print(json.dumps(info, indent=2))
    elif args.cmd == "list-boards":
        for b in list_boards():
            print("%-20s %-16s %s" % (b["board_id"], b.get("ip", ""), b["fingerprint_sha256"]))
    elif args.cmd == "issue-client":
        cert, _ = issue_client_identity(force=args.force)
        print("client identity:", CLIENT_CERT_PATH)
        print("  fingerprint:", fingerprint(cert))
    elif args.cmd == "delete-board":
        print("deleted" if delete_board(args.board_id) else "nothing to delete for",
              args.board_id)
    elif args.cmd == "delete-all-boards":
        if not args.yes:
            raise SystemExit("refusing without --yes: this deletes every board identity "
                             "under certs/boards/ and json/boards/")
        gone = delete_all_boards()
        print("deleted %d board identit%s" % (len(gone), "y" if len(gone) == 1 else "ies"))
        for board_id in gone:
            print(" ", board_id)
    elif args.cmd == "issue-mqtt-broker":
        cert, _ = issue_mqtt_broker_identity(force=args.force)
        print("MQTT broker identity:", MQTT_BROKER_CERT_PATH)
        print("  fingerprint:", fingerprint(cert))
