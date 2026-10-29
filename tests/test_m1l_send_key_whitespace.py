"""
Run M1L (vms-savage, 2026-10-28): terminal_send_key must send a one-character key
that is whitespace - a space, a TAB character, a CR - instead of dropping it.

MEASURED on the installed 0.7.0 and 0.7.1 by vms-savage's scripts/m1l_host.py
(case H4, run q1, 22:13): terminal_send_key(key=" ") returned bytes_sent b'' and
the fake server received nothing. The cause: the tool normalises the key name with
key.strip() before looking it up, and " ".strip() is "", so an agent typing a talk
line key by key loses every space. A key name with stray spaces (" ENTER ") must
still be looked up as before.

Driven over stdio like tests/test_m1_live_reader.py (its Mcp class), against a
local fake that records every byte the client sends.
"""

import socket
import threading
import time

import pytest

from test_m1_live_reader import Mcp, _connect  # noqa: F401  (the fixtures below use Mcp)


class RecordingServer:
    """One TCP connection: sends a prompt, then records every byte received."""

    def __init__(self):
        self.got = bytearray()
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        conn.sendall(b"$ ")
        while True:
            try:
                b = conn.recv(4096)
            except OSError:
                return
            if not b:
                return
            self.got.extend(b)

    def received_after(self, mark, want, deadline=3.0):
        end = time.time() + deadline
        while time.time() < end and len(self.got) - mark < want:
            time.sleep(0.02)
        return bytes(self.got[mark:])

    def stop(self):
        try:
            self.srv.close()
        except OSError:
            pass


@pytest.fixture
def mcp(tmp_path):
    m = Mcp(str(tmp_path))
    yield m
    m.close()


@pytest.mark.parametrize("key,payload", [(" ", b" "), ("\t", b"\t"), ("\r", b"\r")])
def test_single_whitespace_key_is_sent(mcp, key, payload):
    srv = RecordingServer()
    try:
        _connect(mcp, "ws", srv.port)
        mark = len(srv.got)
        r = mcp.tool("terminal_send_key", {"session_id": "ws", "key": key, "wait_seconds": 0})
        got = srv.received_after(mark, len(payload))
        assert r["bytes_sent"] == repr(payload), f"bytes_sent {r['bytes_sent']} for key {key!r}"
        assert got == payload, f"the server received {got!r} for key {key!r}"
    finally:
        srv.stop()


def test_named_key_with_stray_spaces_still_looked_up(mcp):
    srv = RecordingServer()
    try:
        _connect(mcp, "named", srv.port)
        mark = len(srv.got)
        r = mcp.tool("terminal_send_key", {"session_id": "named", "key": " ENTER ", "wait_seconds": 0})
        got = srv.received_after(mark, 1)
        assert r["bytes_sent"] == repr(b"\r")
        assert got == b"\r"
    finally:
        srv.stop()


def test_typing_a_line_with_spaces_key_by_key(mcp):
    """An agent typing "M1L a b" one key per call: every byte arrives, spaces included."""
    srv = RecordingServer()
    try:
        _connect(mcp, "line", srv.port)
        mark = len(srv.got)
        text = "M1L a b"
        for ch in text:
            mcp.tool("terminal_send_key", {"session_id": "line", "key": ch, "wait_seconds": 0})
        got = srv.received_after(mark, len(text))
        assert got == text.encode(), f"the server received {got!r}"
    finally:
        srv.stop()
