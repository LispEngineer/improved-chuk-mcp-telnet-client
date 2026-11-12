"""LOGOUT must retain unread screen traffic and never repeat delivered stream bytes.

The v0.7.3 regression expected screen-only views to discard earlier output on
LOGOUT. v0.8.0 intentionally changes that assertion: screen calls do not consume
stream output. The explicit-drain variant preserves the no-duplicate intent.
"""

import socket
import threading
import time

import pytest

from test_m1_live_reader import Mcp, _connect  # noqa: F401


class LogoutServer:
    """Sends a prompt, then on demand traffic; answers a LOGOUT line and closes."""

    def __init__(self, close_delay):
        self.close_delay = close_delay
        self.conn = None
        self.ready = threading.Event()
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
        self.conn = conn
        conn.sendall(b"USER9$ ")
        self.ready.set()
        buf = b""
        while True:
            try:
                b = conn.recv(4096)
            except OSError:
                return
            if not b:
                return
            buf += b
            if b"LOGOUT" in buf:
                conn.sendall(b"LOGOUT\r\n  USER9        logged out at 28-OCT-2026 23:28:16.55\r\n")
                time.sleep(self.close_delay)
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                conn.close()
                return

    def traffic(self, n=40):
        self.ready.wait(5)
        for k in range(n):
            self.conn.sendall(("\x1b[%d;1HSCREEN-%04d %s\r\n" % (k % 20 + 2, k, "x" * 60)).encode())

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


@pytest.mark.parametrize("close_delay", [0.0, 1.0])
@pytest.mark.parametrize("drain_first", [False, True])
def test_logout_reply_never_discards_or_repeats(mcp, close_delay, drain_first):
    srv = LogoutServer(close_delay)
    try:
        _connect(mcp, "lo", srv.port)
        srv.traffic()
        time.sleep(1.0)
        # The client sees the traffic only through the screen, as a seat does.
        scr = mcp.tool("terminal_get_screen", {"session_id": "lo"})
        assert "SCREEN-0039" in scr["screen_text"]
        mcp.tool("terminal_send_key", {"session_id": "lo", "key": "X", "wait_seconds": 0.3})
        prior = ""
        if drain_first:
            prior = mcp.tool("telnet_read_session", {"session_id": "lo", "max_wait_seconds": 0})["output"]
            assert prior.count("SCREEN-") == 40
        r = mcp.tool("telnet_send_input", {"session_id": "lo", "input_text": "LOGOUT", "newline": "\r",
                                           "prompt_pattern": "logged out at", "max_wait_seconds": 10})
        resp = r["response"]
        print(f"close_delay {close_delay}: reply {len(resp)} bytes, prompt_matched {r['prompt_matched']}, "
              f"session_active {r['session_active']}")
        assert "logged out at" in resp
        assert resp.count("SCREEN-") == (0 if drain_first else 40)
        assert (prior + resp).count("SCREEN-") == 40
        # The prompt ends mid-line: the remaining logout timestamp stays pending.
        tail = mcp.tool("telnet_read_session", {"session_id": "lo", "max_wait_seconds": 0})["output"]
        assert (resp + tail).count("logged out at") == 1
        assert "23:28:16.55" in resp + tail
    finally:
        srv.stop()
