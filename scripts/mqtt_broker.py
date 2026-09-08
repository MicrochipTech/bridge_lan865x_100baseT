#!/usr/bin/env python3
"""
TLS-only (mTLS) MQTT broker for this project's "MQTT" GUI tab, wrapping amqtt.

GUI-free and standalone on purpose, like pki.py/bootload.py: the MQTT tab in
bridge_gui_telnet.py is a thin wrapper around this module, runnable from a
plain Python shell too (see __main__ below).

Runs amqtt (asyncio) in its own background thread with its own event loop -
bridge_gui_telnet.py's Tk main loop never touches asyncio directly, it only
drains the thread-safe `events` queue via `root.after(...)` polling. One
broker per process (this module keeps module-level plugin state) - fine for
a GUI tab that starts/stops a single broker instance.

Why plaintext MQTT (port 1883) is not just "disabled by default" but
structurally absent: start_broker() only ever builds a `listeners.default`
entry with `ssl: True` - there is no second, unencrypted listener anywhere
in this module for a caller to accidentally enable.

mTLS enforcement - not just "TLS with a CA file":
amqtt's own SSL context (Broker._create_ssl_context) sets
`ssl_context.verify_mode = ssl.CERT_OPTIONAL` even when `cafile` is
configured - it validates a PRESENTED client certificate against the CA,
but does not require one to be presented at all. RequireClientCertPlugin
below closes exactly that gap: it is the ONLY auth plugin registered, and
amqtt's own aggregation rule (broker.py's _authenticate(): "authenticated if
ALL plugins return True", "no plugin responded -> reject") means a bare TCP
client that completes a TLS handshake with no certificate is rejected here,
not silently let through. A client presenting a certificate that does not
chain to the CA never reaches this plugin at all - Python's ssl module
aborts the handshake itself.
"""

import asyncio
import queue
import socket
import ssl
import threading
import time
from pathlib import Path
from typing import Optional

from cryptography import x509

from amqtt.broker import Broker
from amqtt.contexts import BaseContext
from amqtt.plugins.base import BaseAuthPlugin, BasePlugin
from amqtt.session import Session

import pki

DEFAULT_BIND_IP = "0.0.0.0"
DEFAULT_PORT = 8883

# How long to let amqtt's own graceful shutdown run before dropping the loop -
# see the comment at the end of _async_main() for what happens without a bound.
SHUTDOWN_TIMEOUT = 5.0

# Drained by MqttBrokerService.events (an alias, not a copy) - see the
# module docstring for why this is process-global rather than per-instance.
_event_queue: "queue.Queue" = queue.Queue()


class BrokerError(Exception):
    """Anything that stops the broker starting/stopping cleanly."""


def _peer_cn(ssl_obj) -> str:
    """Best-effort Subject CN of the client cert on this TLS session, for
    display only - authentication itself never depends on the CN, only on
    "did a certificate chaining to the CA get presented at all"."""
    try:
        der = ssl_obj.getpeercert(binary_form=True)
        if not der:
            return ""
        cert = x509.load_der_x509_certificate(der)
        attrs = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
        return attrs[0].value if attrs else ""
    except Exception:
        return ""


class RequireClientCertPlugin(BaseAuthPlugin):
    """The only auth plugin registered - see the module docstring for why
    this, combined with amqtt's own CERT_OPTIONAL+cafile TLS validation, is
    equivalent to ssl.CERT_REQUIRED."""

    async def authenticate(self, *, session: Session) -> bool:
        ssl_obj = getattr(session, "ssl_object", None)
        if not ssl_obj:
            return False
        return bool(ssl_obj.getpeercert())


class GuiEventsPlugin(BasePlugin[BaseContext]):
    """Forwards the broker events the MQTT tab cares about into
    _event_queue as plain, GUI-toolkit-agnostic tuples:
        ("started",      "<ip>:<port>",                         ts)
        ("stopped",      None,                                  ts)
        ("client",       (client_id, remote_address, cn),       ts)  # connected
        ("client_gone",  client_id,                             ts)  # disconnected
        ("message",      (client_id, topic, payload_bytes),     ts)
    """

    async def on_broker_client_connected(self, *, client_id: str, client_session: Session) -> None:
        cn = _peer_cn(getattr(client_session, "ssl_object", None))
        _event_queue.put(("client", (client_id, client_session.remote_address, cn), time.time()))

    async def on_broker_client_disconnected(self, *, client_id: str, client_session: Session) -> None:
        _event_queue.put(("client_gone", client_id, time.time()))

    async def on_broker_message_received(self, *, client_id: str, message) -> None:
        _event_queue.put(("message", (client_id, message.topic, bytes(message.data)), time.time()))


def port_in_use(bind_ip: str, port: int) -> bool:
    """Whether something is ALREADY listening on that address.

    Not a nicety: amqtt binds its listener with `reuse_address=True`
    (broker.py's _create_server_instance), and on Windows SO_REUSEADDR does not
    mean "share" but "bind on top of an existing listener" - the bind succeeds,
    no error is raised, and the process silently ends up with a listening socket
    that never receives anything, because established connections and (in
    practice) new ones stay with the first binder. Measured 2026-09-08: a
    console `python scripts/mqtt_broker.py` left over from hours earlier held
    all three boards, while the GUI's own broker reported "started" and showed
    an empty client list forever.

    SO_REUSEADDR is deliberately NOT set on the probe - that is exactly what
    makes the conflict visible here (same reasoning as bridge_web_telnet.py's
    port_in_use, for the web port).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((bind_ip, port))
        except OSError:
            return True
    return False


class MqttBrokerService:
    """Start/stop the broker from a normal (non-asyncio) thread - the shape
    bridge_gui_telnet.py's Tk main loop needs."""

    def __init__(self) -> None:
        self.events = _event_queue
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_evt: Optional[asyncio.Event] = None
        self._running = threading.Event()
        self._start_error: Optional[str] = None

    def is_running(self) -> bool:
        return self._running.is_set()

    def start(self, bind_ip: str = DEFAULT_BIND_IP, port: int = DEFAULT_PORT, timeout: float = 5.0) -> None:
        """Raises BrokerError if the identity is missing or the broker
        fails to bind within `timeout` seconds. Returns once the broker is
        confirmed listening, not just once the thread was spawned."""
        if self.is_running():
            raise BrokerError("broker already running")
        if not pki.CA_CERT_PATH.is_file():
            raise BrokerError("no project CA at %s - see pki.py" % pki.CA_CERT_PATH)
        if not pki.MQTT_BROKER_CERT_PATH.is_file() or not pki.MQTT_BROKER_KEY_PATH.is_file():
            raise BrokerError(
                "no MQTT broker identity - run: python scripts/pki.py issue-mqtt-broker")
        if port_in_use(bind_ip, port):
            raise BrokerError(
                "%s:%d is already in use - another MQTT broker is listening there "
                "(a second copy of this tool, or a console 'python scripts/mqtt_broker.py'). "
                "Close it first: netstat -ano | findstr :%d" % (bind_ip, port, port))

        self._start_error = None
        self._running.clear()
        self._thread = threading.Thread(
            target=self._thread_main, args=(bind_ip, port), daemon=True, name="mqtt-broker")
        self._thread.start()

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._running.is_set():
                return
            if self._start_error is not None:
                raise BrokerError(self._start_error)
            time.sleep(0.05)
        raise BrokerError("broker did not start within %.1fs" % timeout)

    def stop(self, timeout: float = 5.0) -> None:
        if not self.is_running() or self._loop is None or self._stop_evt is None:
            return
        self._loop.call_soon_threadsafe(self._stop_evt.set)
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _thread_main(self, bind_ip: str, port: int) -> None:
        try:
            asyncio.run(self._async_main(bind_ip, port))
        except Exception as exc:  # noqa: BLE001 - reported back via _start_error/events, not raised cross-thread
            self._start_error = self._start_error or str(exc)
            self._running.clear()
            self.events.put(("error", str(exc), time.time()))

    async def _async_main(self, bind_ip: str, port: int) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_evt = asyncio.Event()

        config = {
            "listeners": {
                "default": {
                    "type": "tcp",
                    "bind": "%s:%d" % (bind_ip, port),
                    "ssl": True,
                    "cafile": str(pki.CA_CERT_PATH),
                    "certfile": str(pki.MQTT_BROKER_CERT_PATH),
                    "keyfile": str(pki.MQTT_BROKER_KEY_PATH),
                },
            },
            "plugins": {
                "mqtt_broker.RequireClientCertPlugin": {},
                "mqtt_broker.GuiEventsPlugin": {},
            },
        }

        broker = Broker(config=config)
        try:
            await broker.start()
        except Exception as exc:  # noqa: BLE001 - surfaced to start() via _start_error
            self._start_error = str(exc)
            return

        self._running.set()
        self.events.put(("started", "%s:%d" % (bind_ip, port), time.time()))
        try:
            await self._stop_evt.wait()
        finally:
            # Bounded: amqtt's own shutdown() waits on its connected client
            # sessions, and a still-connected embedded client that never gets
            # to answer keeps it waiting indefinitely. Measured 2026-09-07:
            # with three boards connected, stop() had still not completed after
            # 32 s, so the broker could not be restarted at all
            # ("broker already running") - and the GUI's "Stop Broker" button
            # would hang exactly the same way, since it calls this same
            # service. Giving up on the graceful path and dropping the loop is
            # correct here: the listener is closed either way, and the clients
            # detect the dead connection on their own (they already retry).
            try:
                await asyncio.wait_for(broker.shutdown(), timeout=SHUTDOWN_TIMEOUT)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
            self._running.clear()
            self.events.put(("stopped", None, time.time()))


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bind", default=DEFAULT_BIND_IP)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()

    svc = MqttBrokerService()
    svc.start(args.bind, args.port)
    print("MQTT broker listening on %s:%d (TLS/mTLS only, Ctrl+C to stop)" % (args.bind, args.port))
    try:
        while True:
            try:
                kind, payload, ts = svc.events.get(timeout=1.0)
            except queue.Empty:
                continue
            print("[%.0f] %s %r" % (ts, kind, payload))
    except KeyboardInterrupt:
        pass
    finally:
        svc.stop()
