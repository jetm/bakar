"""http_exists / head_status: only a 2xx HEAD answer counts as present."""

from __future__ import annotations

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from bakar.probes import head_status, http_exists


def _serve(status: int):
    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize(("status", "want"), [(200, True), (204, True), (302, False), (404, False), (500, False)])
def test_only_2xx_exists(status, want):
    server = _serve(status)
    try:
        url = f"http://127.0.0.1:{server.server_port}/x"
        assert http_exists(url) is want
        assert head_status(url, 2.0) == status
    finally:
        server.shutdown()
        server.server_close()


def test_closed_port_does_not_exist_and_has_no_status():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert http_exists(f"http://127.0.0.1:{port}/x", timeout=0.5) is False
    assert head_status(f"http://127.0.0.1:{port}/x", 0.5) is None


def test_silent_listener_respects_the_deadline():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        start = time.monotonic()
        assert http_exists(f"http://127.0.0.1:{port}/x", timeout=0.5) is False
        assert time.monotonic() - start < 1.0
