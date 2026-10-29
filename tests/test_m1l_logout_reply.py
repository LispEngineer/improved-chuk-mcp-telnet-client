"""
Run M1L (vms-savage, 2026-10-28): a telnet_send_input whose command ends the session
(LOGOUT) must reply with the output that followed the command, not with every byte
received since the last telnet_send_input.

MEASURED live on PHVS5 with the installed 0.7.1 (vms-savage logs/m1l_mine_n1/m1l_mine_b.log,
23:28): after SAVAGE was driven with terminal_send_key and terminal_wait_for_change, LOGOUT
by telnet_send_input replied with all of SAVAGE's repaints since the previous
telnet_send_input, then the logout line (prompt_matched null, session_active false). That
is PT1's N14 symptom (a 36.5 KB LOGOUT reply), by another path: OpenVMS closes the
connection right after "logged out at", the reader marks the session inactive, and
_wait_for_output's inactive branch returned session.unconsumed_output, which only a
telnet_send_input / telnet_read_session ever empties. The pattern branch (the close
comes later) and the timeout branch already return the output since the command.

Driven over stdio like tests/test_m1_live_reader.py (its Mcp class), against a local fake
that sends a screenful of traffic, then answers LOGOUT and closes at once (or 1 s later).
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
def test_logout_reply_holds_only_what_followed(mcp, close_delay):
    srv = LogoutServer(close_delay)
    try:
        _connect(mcp, "lo", srv.port)
        srv.traffic()
        time.sleep(1.0)
        # The client sees the traffic only through the screen, as a seat does.
        scr = mcp.tool("terminal_get_screen", {"session_id": "lo"})
        assert "SCREEN-0039" in scr["screen_text"]
        mcp.tool("terminal_send_key", {"session_id": "lo", "key": "X", "wait_seconds": 0.3})
        r = mcp.tool("telnet_send_input", {"session_id": "lo", "input_text": "LOGOUT", "newline": "\r",
                                           "prompt_pattern": "logged out at", "max_wait_seconds": 10})
        resp = r["response"]
        print(f"close_delay {close_delay}: reply {len(resp)} bytes, prompt_matched {r['prompt_matched']}, "
              f"session_active {r['session_active']}")
        assert "logged out at" in resp
        assert "SCREEN-" not in resp, f"the reply carries {resp.count('SCREEN-')} lines received before LOGOUT"
    finally:
        srv.stop()
