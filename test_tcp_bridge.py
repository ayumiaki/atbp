#!/usr/bin/env python3
"""Localhost tests for the ATBP TCP bridge.

Everything runs on this machine: the upstream is a throwaway Unix socket in a
temp directory, and the bridge binds 127.0.0.1 on an ephemeral port (port 0).
No fixed ports, no /tmp/atbp.sock, no third-party test dependency.

    python3 -m unittest test_tcp_bridge -v
"""

import os
import socket
import tempfile
import threading
import time
import unittest

from encoder import make_ACK, make_HBT
from decoder import decode_frame
from tcp_bridge import TCPBridge

TIMEOUT = 5.0


class FakeUnixUpstream:
    """Stand-in for `decoder.py --listen`: a Unix socket server with a
    swappable per-connection handler. Records what it received."""

    def __init__(self, path, handler):
        self.path = path
        self.handler = handler
        self.received = []
        self._server = None
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(8)
        self._server.settimeout(0.25)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._run_handler, args=(conn,), daemon=True).start()

    def _run_handler(self, conn):
        with conn:
            try:
                self.handler(self, conn)
            except OSError:
                pass

    def stop(self):
        self._stop.set()
        try:
            self._server.close()
        except OSError:
            pass
        if self._thread:
            self._thread.join(timeout=TIMEOUT)


def echo_handler(upstream, conn):
    """Echo every chunk back, recording it, until the client half-closes."""
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            return
        upstream.received.append(chunk)
        conn.sendall(chunk)


def atbp_ack_handler(upstream, conn):
    """Read one 8-byte frame and reply with an ACK, like the real decoder."""
    frame = conn.recv(8)
    upstream.received.append(frame)
    decoded = decode_frame(frame)
    conn.sendall(make_ACK(seq=decoded.get("seq", 0), status=0))


def push_then_close_handler(upstream, conn):
    """Write downstream without being asked, then close."""
    conn.sendall(b"UPSTREAM-PUSH")


def recv_exactly(sock, count):
    buf = b""
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def recv_until_eof(sock):
    chunks = []
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


class BridgeTestCase(unittest.TestCase):
    """Base: temp dir for the Unix socket, bridge on an ephemeral TCP port."""

    handler = staticmethod(echo_handler)
    start_upstream = True

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix="atbp-test-")
        self.addCleanup(self.tmpdir.cleanup)
        self.socket_path = os.path.join(self.tmpdir.name, "atbp-test.sock")

        self.upstream = None
        if self.start_upstream:
            self.upstream = FakeUnixUpstream(self.socket_path, self.handler).start()
            self.addCleanup(self.upstream.stop)

        self.bridge = TCPBridge(host="127.0.0.1", port=0, unix_socket=self.socket_path)
        self.bridge.start()
        self.addCleanup(self.bridge.shutdown)
        self.host, self.port = self.bridge.address

        self.server_thread = threading.Thread(target=self.bridge.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.server_thread.join, TIMEOUT)

    def connect(self):
        client = socket.create_connection((self.host, self.port), timeout=TIMEOUT)
        self.addCleanup(client.close)
        return client

    def wait_for(self, predicate, message):
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail(f"timed out waiting for {message}")


class TestEphemeralBinding(BridgeTestCase):
    def test_binds_ephemeral_port_on_loopback(self):
        self.assertEqual(self.host, "127.0.0.1")
        self.assertNotEqual(self.port, 0, "port 0 should resolve to a real port")

    def test_bridge_does_not_create_its_own_unix_socket(self):
        """The bridge is a client of the Unix socket, never a second server on
        it — it must not bind or replace the upstream path."""
        self.assertTrue(os.path.exists(self.socket_path))
        upstream_inode = os.stat(self.socket_path).st_ino
        client = self.connect()
        client.sendall(b"probe")
        self.assertEqual(recv_exactly(client, 5), b"probe")
        self.assertEqual(os.stat(self.socket_path).st_ino, upstream_inode)


class TestRelayBothDirections(BridgeTestCase):
    def test_client_to_upstream_bytes_arrive_verbatim(self):
        client = self.connect()
        client.sendall(b"client-to-upstream")
        recv_exactly(client, len(b"client-to-upstream"))
        self.wait_for(lambda: self.upstream.received, "upstream to receive data")
        self.assertEqual(b"".join(self.upstream.received), b"client-to-upstream")

    def test_round_trip_larger_than_buffer_size(self):
        payload = bytes(range(256)) * 200  # 51200B, well past BUFFER_SIZE
        client = self.connect()
        client.sendall(payload)
        client.shutdown(socket.SHUT_WR)
        self.assertEqual(recv_until_eof(client), payload)
        self.assertEqual(b"".join(self.upstream.received), payload)

    def test_many_small_writes_preserve_order(self):
        client = self.connect()
        expected = b""
        for i in range(50):
            chunk = f"frame-{i:03d};".encode()
            client.sendall(chunk)
            expected += chunk
        client.shutdown(socket.SHUT_WR)
        self.assertEqual(recv_until_eof(client), expected)


class TestUpstreamToClientDirection(BridgeTestCase):
    handler = staticmethod(push_then_close_handler)

    def test_upstream_initiated_bytes_reach_client(self):
        """Downstream relay must work even when the client never writes."""
        client = self.connect()
        self.assertEqual(recv_until_eof(client), b"UPSTREAM-PUSH")


class TestATBPFramesPassThrough(BridgeTestCase):
    handler = staticmethod(atbp_ack_handler)

    def test_hbt_over_tcp_gets_acked_by_upstream(self):
        client = self.connect()
        client.sendall(make_HBT(seq=7, state=1))

        response = recv_exactly(client, 8)
        self.assertEqual(len(response), 8)
        decoded = decode_frame(response)
        self.assertEqual(decoded["opcode"], "ACK")
        self.assertEqual(decoded["seq"], 7)

        self.assertEqual(b"".join(self.upstream.received), make_HBT(seq=7, state=1))
        upstream_view = decode_frame(self.upstream.received[0])
        self.assertEqual(upstream_view["opcode"], "HBT")
        self.assertEqual(upstream_view["seq"], 7)


class TestBrokenUpstream(BridgeTestCase):
    start_upstream = False  # nothing is listening on socket_path

    def test_client_is_closed_immediately_when_upstream_missing(self):
        client = self.connect()
        self.assertEqual(recv_until_eof(client), b"", "client should get a clean EOF")

    def test_bridge_survives_and_serves_once_upstream_appears(self):
        self.assertEqual(recv_until_eof(self.connect()), b"")

        upstream = FakeUnixUpstream(self.socket_path, echo_handler).start()
        self.addCleanup(upstream.stop)

        client = self.connect()
        client.sendall(b"late")
        self.assertEqual(recv_exactly(client, 4), b"late")


class TestClientDisconnect(BridgeTestCase):
    def test_abrupt_disconnect_does_not_kill_the_bridge(self):
        rude = socket.create_connection((self.host, self.port), timeout=TIMEOUT)
        rude.sendall(b"half-said")
        rude.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        rude.close()  # RST, not a graceful FIN

        client = self.connect()
        client.sendall(b"after")
        self.assertEqual(recv_exactly(client, 5), b"after")
        self.assertTrue(self.server_thread.is_alive())

    def test_upstream_closing_first_ends_the_client_connection(self):
        upstream = FakeUnixUpstream(
            os.path.join(self.tmpdir.name, "short.sock"), push_then_close_handler
        ).start()
        self.addCleanup(upstream.stop)
        self.bridge.unix_socket = upstream.path

        client = self.connect()
        self.assertEqual(recv_until_eof(client), b"UPSTREAM-PUSH")


class TestCleanup(BridgeTestCase):
    def test_completed_connections_are_not_tracked_or_leaked(self):
        client = self.connect()
        client.sendall(b"bye")
        recv_exactly(client, 3)
        client.shutdown(socket.SHUT_WR)
        recv_until_eof(client)
        client.close()
        self.wait_for(lambda: not self.bridge._clients, "bridge to release the client")

    def test_shutdown_stops_accepting_and_joins_the_server_thread(self):
        self.bridge.shutdown()
        self.server_thread.join(timeout=TIMEOUT)
        self.assertFalse(self.server_thread.is_alive(), "serve_forever should return")
        with self.assertRaises((ConnectionRefusedError, socket.timeout, OSError)):
            socket.create_connection((self.host, self.port), timeout=1.0).close()

    def test_shutdown_releases_in_flight_clients(self):
        client = self.connect()
        client.sendall(b"open")
        recv_exactly(client, 4)
        self.bridge.shutdown()
        client.settimeout(TIMEOUT)
        self.assertEqual(client.recv(4096), b"", "in-flight client should be closed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
