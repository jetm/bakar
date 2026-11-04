"""http_reachable: any HTTP answer is reachable; silence and refusal are not."""

from __future__ import annotations

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from bakar.probes import http_reachable


def _serve(status: int):
    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.mark.parametrize("status", [200, 403, 500])
def test_any_http_response_is_reachable(status):
    server = _serve(status)
    try:
        assert http_reachable(f"http://127.0.0.1:{server.server_port}/") is True
    finally:
        server.shutdown()
        server.server_close()


def test_closed_port_is_unreachable():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert http_reachable(f"http://127.0.0.1:{port}/", timeout=0.5) is False


def test_silent_listener_times_out_within_twice_timeout():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        start = time.monotonic()
        assert http_reachable(f"http://127.0.0.1:{port}/", timeout=0.5) is False
        assert time.monotonic() - start < 1.0


def test_timeout_is_a_total_deadline_not_per_operation():
    # Server accepts, reads the request after ~0.8x the timeout, then stalls.
    # Per-operation timeouts would allow ~2x; a total deadline stays near 1x.
    timeout = 1.0
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]

        def slow():
            conn, _ = s.accept()
            with conn:
                time.sleep(timeout * 0.8)
                conn.recv(1024)
                time.sleep(timeout * 2)

        t = threading.Thread(target=slow, daemon=True)
        t.start()
        start = time.monotonic()
        assert http_reachable(f"http://127.0.0.1:{port}/", timeout=timeout) is False
        assert time.monotonic() - start < timeout + 0.5


def test_redirect_is_not_followed_and_counts_as_reachable():
    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert http_reachable(f"http://127.0.0.1:{server.server_port}/") is True
    finally:
        server.shutdown()
        server.server_close()


def test_malformed_url_is_unreachable():
    assert http_reachable("not a url") is False


def test_non_http_listener_is_unreachable_not_an_exception():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]

        def garbage():
            conn, _ = s.accept()
            with conn:
                conn.recv(1024)
                conn.sendall(b"\x00\x01 this is not http\r\n\r\n")

        t = threading.Thread(target=garbage, daemon=True)
        t.start()
        assert http_reachable(f"http://127.0.0.1:{port}/", timeout=2.0) is False
        t.join(2.0)


def test_dribbling_server_cannot_extend_the_deadline():
    timeout = 1.0
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]

        def dribble():
            conn, _ = s.accept()
            with conn:
                conn.recv(1024)
                try:
                    for _ in range(10):
                        conn.sendall(b"H")
                        time.sleep(0.4)
                except OSError:
                    pass

        threading.Thread(target=dribble, daemon=True).start()
        start = time.monotonic()
        assert http_reachable(f"http://127.0.0.1:{port}/", timeout=timeout) is False
        assert time.monotonic() - start < timeout + 0.5
