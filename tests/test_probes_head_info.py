"""head_info / head_status: status plus the parsed Content-Length of a HEAD answer."""

from __future__ import annotations

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from bakar.probes import HeadInfo, head_info, head_status


def _serve(status: int, length: str | None):
    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(status)
            if length is not None:
                self.send_header("Content-Length", length)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server


@pytest.mark.parametrize(
    ("status", "length", "want"),
    [
        (200, "5", HeadInfo(200, 5)),
        (404, "0", HeadInfo(404, 0)),
        (200, None, HeadInfo(200, None)),
        (200, "abc", HeadInfo(200, None)),
        (200, "-5", HeadInfo(200, None)),
    ],
)
def test_head_info_and_status(status, length, want):
    server = _serve(status, length)
    try:
        url = f"http://127.0.0.1:{server.server_port}/x"
        assert head_info(url, 2.0) == want
        assert head_status(url, 2.0) == status
    finally:
        server.shutdown()
        server.server_close()


def test_refused_connection_gives_none():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}/x"
    assert head_info(url, 0.5) is None
    assert head_status(url, 0.5) is None


def test_timeout_gives_none():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        url = f"http://127.0.0.1:{s.getsockname()[1]}/x"
        start = time.monotonic()
        assert head_info(url, 0.2) is None
        assert head_status(url, 0.2) is None
        assert time.monotonic() - start < 0.8
