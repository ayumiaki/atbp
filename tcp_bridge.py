#!/usr/bin/env python3
"""ATBP v0.1 TCP Bridge — expose the Unix socket transport over TCP.

The decoder (`decoder.py --listen`) owns the Unix socket at /tmp/atbp.sock.
That socket is filesystem-local: only processes on the same host can reach it.
This bridge accepts TCP clients and relays raw bytes to that Unix socket, one
upstream connection per client, so remote agents can speak ATBP unchanged.

The bridge is byte-transparent. It does not parse, validate, or reframe ATBP —
frames pass through exactly as sent, so the protocol stays owned by
encoder.py/decoder.py. The Unix socket is never handed to the client; the
bridge is the only network-facing surface.

Stdlib only, no third-party runtime dependency.

    python3 tcp_bridge.py                      # 0.0.0.0:9443 -> /tmp/atbp.sock
    python3 tcp_bridge.py --host 127.0.0.1     # loopback only
    python3 tcp_bridge.py --port 9444 --unix-socket /tmp/other.sock
"""

import argparse
import os
import selectors
import signal
import socket
import sys
import threading

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 9443
DEFAULT_UNIX_SOCKET = "/tmp/atbp.sock"

BUFFER_SIZE = 4096
UPSTREAM_CONNECT_TIMEOUT = 5.0
ACCEPT_TIMEOUT = 0.5  # how often the accept loop checks for shutdown


def _log(message: str):
    """Single-line stdout logging, flushed so journald/systemd sees it live."""
    print(f"[ATBP-BRIDGE] {message}")
    sys.stdout.flush()


def _quiet_close(sock):
    """Close a socket, ignoring the errors that a dead peer makes routine."""
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass


def _quiet_shutdown_write(sock):
    """Half-close the write side so the peer sees EOF but can still reply."""
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def relay(client: socket.socket, upstream: socket.socket) -> tuple:
    """Pump bytes both ways until both directions are done.

    Returns (bytes_to_upstream, bytes_to_client).

    Each direction is closed independently: when one side sends EOF we
    half-close the other's write side rather than tearing the pair down, so a
    client that finishes writing still receives the upstream response. The
    relay returns once both directions have ended, or immediately if a write
    fails because a peer vanished.
    """
    to_upstream = 0
    to_client = 0
    peers = {id(client): (upstream, "client->upstream"), id(upstream): (client, "upstream->client")}

    with selectors.DefaultSelector() as sel:
        sel.register(client, selectors.EVENT_READ)
        sel.register(upstream, selectors.EVENT_READ)
        open_directions = 2

        while open_directions:
            for key, _events in sel.select():
                source = key.fileobj
                destination, direction = peers[id(source)]

                try:
                    chunk = source.recv(BUFFER_SIZE)
                except OSError:
                    chunk = b""  # reset by peer reads as end-of-stream

                if not chunk:
                    sel.unregister(source)
                    open_directions -= 1
                    _quiet_shutdown_write(destination)
                    continue

                try:
                    destination.sendall(chunk)
                except OSError as exc:
                    _log(f"{direction} write failed, closing pair: {exc}")
                    return to_upstream, to_client

                if direction == "client->upstream":
                    to_upstream += len(chunk)
                else:
                    to_client += len(chunk)

    return to_upstream, to_client


class TCPBridge:
    """Accepts TCP clients and relays each to its own Unix socket connection."""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, unix_socket=DEFAULT_UNIX_SOCKET):
        self.host = host
        self.port = port
        self.unix_socket = unix_socket
        self._listener = None
        self._stop = threading.Event()
        self._clients = set()
        self._client_lock = threading.Lock()
        self._workers = []

    @property
    def address(self) -> tuple:
        """The bound (host, port). Resolves port 0 to the real ephemeral port."""
        if self._listener is None:
            raise RuntimeError("bridge is not bound; call start() first")
        return self._listener.getsockname()[:2]

    def start(self):
        """Bind and listen. Separate from serve_forever so callers (and tests)
        can learn the bound port before traffic starts."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((self.host, self.port))
        except OSError:
            _quiet_close(listener)
            raise
        listener.listen(128)
        listener.settimeout(ACCEPT_TIMEOUT)
        self._listener = listener
        _log(f"listening on {self.address[0]}:{self.address[1]} -> {self.unix_socket}")
        return self

    def serve_forever(self):
        """Accept until shutdown(). Handles each client on its own thread."""
        if self._listener is None:
            self.start()

        while not self._stop.is_set():
            try:
                client, peer = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break  # listener closed by shutdown()

            worker = threading.Thread(
                target=self._handle_client,
                args=(client, peer),
                name=f"atbp-bridge-{peer[0]}:{peer[1]}",
                daemon=True,
            )
            self._workers.append(worker)
            self._workers = [w for w in self._workers if w.is_alive()]
            worker.start()

        self._drain()

    def _handle_client(self, client: socket.socket, peer: tuple):
        """One client: open upstream, relay, always clean up both sockets."""
        peer_label = f"{peer[0]}:{peer[1]}"
        with self._client_lock:
            self._clients.add(client)

        upstream = None
        try:
            upstream = self._connect_upstream()
        except OSError as exc:
            # Broken upstream is expected when the decoder is not running.
            # Drop the client rather than hanging it; never leak the reason
            # beyond our own log.
            _log(f"{peer_label} rejected, upstream {self.unix_socket} unavailable: {exc}")

        try:
            if upstream is None:
                return
            _log(f"{peer_label} connected")
            to_upstream, to_client = relay(client, upstream)
            _log(f"{peer_label} closed (up {to_upstream}B / down {to_client}B)")
        except OSError as exc:
            _log(f"{peer_label} relay error: {exc}")
        finally:
            _quiet_close(upstream)
            _quiet_close(client)
            with self._client_lock:
                self._clients.discard(client)

    def _connect_upstream(self) -> socket.socket:
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        upstream.settimeout(UPSTREAM_CONNECT_TIMEOUT)
        try:
            upstream.connect(self.unix_socket)
        except OSError:
            _quiet_close(upstream)
            raise
        upstream.settimeout(None)
        return upstream

    def shutdown(self):
        """Stop accepting, then unblock every in-flight client."""
        self._stop.set()
        _quiet_close(self._listener)
        self._drain()

    def _drain(self):
        with self._client_lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for worker in list(self._workers):
            worker.join(timeout=2.0)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="ATBP v0.1 TCP bridge: relays TCP clients to the ATBP Unix socket."
    )
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"TCP bind address (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"TCP bind port (default: {DEFAULT_PORT})")
    parser.add_argument("--unix-socket", default=DEFAULT_UNIX_SOCKET,
                        help=f"upstream ATBP Unix socket (default: {DEFAULT_UNIX_SOCKET})")
    args = parser.parse_args(argv)

    if not os.path.exists(args.unix_socket):
        # Not fatal: the decoder may start after us, and systemd restarts are
        # cheaper than refusing to boot. Say so once, then serve.
        _log(f"warning: {args.unix_socket} does not exist yet; "
             "clients will be dropped until the decoder is listening")

    bridge = TCPBridge(host=args.host, port=args.port, unix_socket=args.unix_socket)
    try:
        bridge.start()
    except OSError as exc:
        _log(f"bind {args.host}:{args.port} failed: {exc}")
        return 1

    def _on_signal(_signum, _frame):
        _log("shutting down")
        bridge.shutdown()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        bridge.serve_forever()
    except KeyboardInterrupt:
        bridge.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
