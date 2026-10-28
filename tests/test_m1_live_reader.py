"""
Run M1: the session reader runs all the time, not only while a tool call is served.

Background (run F8 of vms-savage, 2026-10-28, MEASURED there with the installed 0.7.0):
the server runs chuk_mcp_server's StdioSyncTransport, which blocks in
sys.stdin.readline() between requests and runs the asyncio loop only inside
loop.run_until_complete() for one request. The 0.7.0 reader was an asyncio task on
that loop, so between tool calls nothing read the socket: bytes waited in the
kernel, pyte was not fed, terminal_get_screen(wait_seconds=0) returned the screen
as of the last call, and the transcript stamped a chunk when it was READ, not when
it arrived.

These tests therefore drive the REAL MCP server over stdio, one JSON-RPC line per
message, exactly as an MCP client (Codex, Claude Code) does - an in-process call
with asyncio.run() would keep a loop running and hide the fault. The probe this is
modelled on is vms-savage's scripts/f8_mcp_idle_probe.py; nothing here depends on
that repository.

Fakes, all local (no VAX, no simulator):
  * FakeTelnetServer: a TCP socket that sends scripted byte strings at fixed delays
    after the connection is accepted, and records when each was sent.
  * a pty pair for the serial path: the test writes the master side, the server
    opens the slave device by name.

Timing bounds are generous against MCP/process overhead but far below the 0.7.0
fault's size (seconds). Each test prints the host load average.
"""

import asyncio
import json
import os
import pty
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tty
from datetime import datetime

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    with open("/proc/loadavg") as f:
        return f.read().strip()


# ============================================================================
# Fakes
# ============================================================================


class FakeTelnetServer:
    """One TCP connection; sends each (delay_after_accept, bytes) item on a timer.

    Sends nothing else and never reads, so a Telnet client sees plain data with no
    option negotiation. `sent[label]` is the time.time() just after each sendall.
    The connection is held open until stop().
    """

    def __init__(self, script):
        # script: list of (label, delay_seconds_after_accept, payload_bytes)
        self.script = script
        self.sent = {}
        self.accepted_at = None
        self._stop = threading.Event()
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        self.accepted_at = time.time()
        try:
            for label, delay, payload in self.script:
                due = self.accepted_at + delay
                while time.time() < due:
                    if self._stop.wait(min(0.01, max(0.0, due - time.time()))):
                        return
                conn.sendall(payload)
                self.sent[label] = time.time()
            self._stop.wait(120)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self):
        self._stop.set()
        try:
            self.srv.close()
        except OSError:
            pass


class Mcp:
    """The MCP server of THIS checkout, over stdio, one JSON-RPC line per message."""

    def __init__(self, log_dir):
        env = dict(os.environ, TELNET_LOG_DIR=log_dir, SERIAL_LOG_DIR=log_dir)
        self.p = subprocess.Popen(
            [sys.executable, "-m", "chuk_mcp_telnet_client.main"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=REPO_ROOT,
            text=True,
        )
        # A thread drains stdout into a queue so every call can have a deadline.
        self.lines = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.n = 0
        self.call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "m1-test", "version": "0"},
        })
        self.call("notifications/initialized", notify=True)

    def _pump(self):
        for line in self.p.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def call(self, method, params=None, notify=False, deadline=60.0):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self.n += 1
            msg["id"] = self.n
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        if notify:
            return None
        end = time.time() + deadline
        while True:
            line = self.lines.get(timeout=max(0.01, end - time.time()))
            if line is None:
                raise RuntimeError("MCP server exited")
            d = json.loads(line)
            if d.get("id") == self.n:
                return d

    def tool(self, name, args, deadline=60.0):
        r = self.call("tools/call", {"name": name, "arguments": args}, deadline=deadline)
        if "error" in r:
            raise RuntimeError(f"tool {name} error: {r['error']}")
        res = r["result"]
        txt = res["content"][0]["text"]
        if res.get("isError"):
            raise RuntimeError(f"tool {name} isError: {txt}")
        return json.loads(txt)

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        self.p.terminate()
        self.p.wait(10)


@pytest.fixture
def log_dir():
    d = tempfile.mkdtemp(prefix="m1_live_reader_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def mcp(log_dir):
    m = Mcp(log_dir)
    yield m
    m.close()


def _connect(mcp, sid, port, timestamp_chunks=False):
    return mcp.tool("telnet_client", {
        "host": "127.0.0.1",
        "port": port,
        "commands": [],
        "telnet_session_id": sid,
        "max_wait_seconds": 1,
        "timestamp_chunks": timestamp_chunks,
    })


def _sleep_until(t):
    while time.time() < t:
        time.sleep(min(0.05, max(0.0, t - time.time())))


def _chunk_stamps(log_path):
    """[(stamp_epoch, chunk_text)] for each [STREAM DATA] entry of a timestamped log."""
    text = open(log_path, encoding="latin-1").read()
    out = []
    for m in re.finditer(r"^\[([^\]]+)\] \[STREAM DATA\]\n(.*?)\n-{40}\n", text, re.S | re.M):
        out.append((datetime.fromisoformat(m.group(1)).timestamp(), m.group(2)))
    return out


# ============================================================================
# 1. Telnet: an idle session's screen is current (RED on 09dbaa0)
# ============================================================================


def test_idle_telnet_screen_is_current_over_stdio(mcp):
    """LATER arrives 2 s after the connect while the client makes NO call; a wait-0
    screen read 2.5 s later must show it."""
    srv = FakeTelnetServer([
        ("FIRST", 0.0, b"FIRST\r\n$ "),
        ("LATER", 2.0, b"\r\nLATER\r\n"),
    ])
    try:
        _connect(mcp, "idle1", srv.port)
        _sleep_until(srv.accepted_at + 4.5)  # no call during this stretch
        assert "LATER" in srv.sent, "fake server did not send LATER"
        r = mcp.tool("terminal_get_screen", {"session_id": "idle1"})
        print(f"load {_load()}; LATER sent +{srv.sent['LATER'] - srv.accepted_at:.2f} s; "
              f"read at +{time.time() - srv.accepted_at:.2f} s")
        assert "FIRST" in r["screen_text"]
        assert "LATER" in r["screen_text"], "wait-0 screen is stale: LATER not on it"
    finally:
        srv.stop()


# ============================================================================
# 2. Telnet: the transcript stamps a chunk when it ARRIVES (RED on 09dbaa0)
# ============================================================================


def test_transcript_chunk_stamped_on_arrival(mcp, log_dir):
    srv = FakeTelnetServer([
        ("FIRST", 0.0, b"FIRST\r\n$ "),
        ("LATER", 2.0, b"\r\nLATER\r\n"),
    ])
    try:
        res = _connect(mcp, "stamp1", srv.port, timestamp_chunks=True)
        _sleep_until(srv.accepted_at + 4.5)
        # A call that gives even the 0.7.0 reader time, so the chunk is logged at all.
        mcp.tool("terminal_get_screen", {"session_id": "stamp1", "wait_seconds": 1.0})
        log_path = res["log_file"]
        stamps = [(t, c) for t, c in _chunk_stamps(log_path) if "LATER" in c]
        assert stamps, f"no STREAM DATA chunk with LATER in {log_path}"
        lag = stamps[0][0] - srv.sent["LATER"]
        print(f"load {_load()}; LATER chunk stamped {lag:+.3f} s from its send")
        assert -0.05 <= lag <= 0.2, f"chunk stamped {lag:+.3f} s from its send"
    finally:
        srv.stop()


# ============================================================================
# 3. terminal_wait_for_change (absent on 09dbaa0: RED there)
# ============================================================================


def test_wait_for_change_returns_after_settle(mcp):
    """A burst L1, L2, L3 0.25 s apart (each gap under the 0.3 s settle): the wait
    must return with the WHOLE burst, no sooner than settle after L3 and within
    settle + 0.2 s of it."""
    srv = FakeTelnetServer([
        ("FIRST", 0.0, b"FIRST\r\n$ "),
        ("L1", 3.0, b"\r\nL1"),
        ("L2", 3.25, b"\r\nL2"),
        ("L3", 3.5, b"\r\nL3\r\n"),
    ])
    try:
        _connect(mcp, "wait1", srv.port)
        base = mcp.tool("terminal_get_screen", {"session_id": "wait1"})
        assert "L1" not in base["screen_text"]
        r = mcp.tool("terminal_wait_for_change", {
            "session_id": "wait1", "timeout_seconds": 10, "settle_seconds": 0.3})
        t_ret = time.time()
        for s in ("L1", "L2", "L3"):
            assert s in r["screen_text"], f"{s} missing: returned before the burst settled"
        after = t_ret - srv.sent["L3"]
        print(f"load {_load()}; returned {after:.3f} s after L3; waited {r['waited_seconds']}")
        assert r["changed"] is True
        assert r["timed_out"] is False
        assert 0.3 - 0.05 <= after <= 0.3 + 0.2, f"returned {after:.3f} s after the last byte"
    finally:
        srv.stop()


def test_wait_for_change_sees_change_made_before_the_call(mcp):
    """'Changed since the last screen read': a change that arrived while no call ran
    returns at once (the screen settled long ago)."""
    srv = FakeTelnetServer([
        ("FIRST", 0.0, b"FIRST\r\n$ "),
        ("LATER", 2.0, b"\r\nLATER\r\n"),
    ])
    try:
        _connect(mcp, "wait2", srv.port)
        mcp.tool("terminal_get_screen", {"session_id": "wait2"})
        _sleep_until(srv.accepted_at + 4.0)
        t0 = time.time()
        r = mcp.tool("terminal_wait_for_change", {
            "session_id": "wait2", "timeout_seconds": 5, "settle_seconds": 0.3})
        took = time.time() - t0
        print(f"load {_load()}; returned in {took:.3f} s")
        assert r["changed"] is True and "LATER" in r["screen_text"]
        assert took < 0.5
    finally:
        srv.stop()


def test_wait_for_change_times_out_unchanged(mcp):
    srv = FakeTelnetServer([("FIRST", 0.0, b"FIRST\r\n$ ")])
    try:
        _connect(mcp, "wait3", srv.port)
        mcp.tool("terminal_get_screen", {"session_id": "wait3"})
        t0 = time.time()
        r = mcp.tool("terminal_wait_for_change", {
            "session_id": "wait3", "timeout_seconds": 1.0, "settle_seconds": 0.3})
        took = time.time() - t0
        print(f"load {_load()}; timed out in {took:.3f} s")
        assert r["changed"] is False
        assert r["timed_out"] is True
        assert "FIRST" in r["screen_text"]
        assert 1.0 <= took <= 1.0 + 0.3
    finally:
        srv.stop()


def test_wait_for_change_pattern(mcp):
    """With a pattern: an intermediate change does not end the wait; the pattern does
    (after settle). A pattern that never appears times out with no match."""
    srv = FakeTelnetServer([
        ("FIRST", 0.0, b"FIRST\r\n$ "),
        ("WORK", 2.5, b"\r\nWorking..."),
        ("DONE", 4.0, b"\r\nDONE 42\r\n$ "),
    ])
    try:
        _connect(mcp, "wait4", srv.port)
        mcp.tool("terminal_get_screen", {"session_id": "wait4"})
        r = mcp.tool("terminal_wait_for_change", {
            "session_id": "wait4", "timeout_seconds": 10, "settle_seconds": 0.3,
            "pattern": r"DONE \d+"})
        t_ret = time.time()
        assert r["pattern_matched"] == "DONE 42", f"returned without the pattern: {r['screen_text']!r}"
        after = t_ret - srv.sent["DONE"]
        print(f"load {_load()}; returned {after:.3f} s after DONE")
        assert r["timed_out"] is False
        assert "Working..." in r["screen_text"]
        assert 0.3 - 0.05 <= after <= 0.3 + 0.2

        t0 = time.time()
        r = mcp.tool("terminal_wait_for_change", {
            "session_id": "wait4", "timeout_seconds": 1.0, "pattern": r"NEVER"})
        took = time.time() - t0
        assert r["pattern_matched"] is None
        assert r["timed_out"] is True
        assert 1.0 <= took <= 1.0 + 0.3
    finally:
        srv.stop()


def test_wait_for_change_caps_timeout():
    """The cap is enforced in code (a client cannot ask for an unbounded wait)."""
    from chuk_mcp_telnet_client import tools

    assert tools.TerminalDefaults.WAIT_FOR_CHANGE_MAX_TIMEOUT == 300.0
    assert tools._clamp_wait_timeout(10_000) == 300.0
    assert tools._clamp_wait_timeout(-5) == 0.0
    assert tools._clamp_wait_timeout(12.5) == 12.5


# ============================================================================
# 4. Close while the reader is blocked; no leaked thread after 100 cycles
# ============================================================================


def _reader_threads():
    return [t for t in threading.enumerate() if t.name.startswith(("telnet-reader-", "serial-reader-"))]


def _fd_count():
    return len(os.listdir("/proc/self/fd"))


def test_close_while_reader_blocked_100_cycles(monkeypatch):
    """Each session's reader is blocked waiting for data that never comes; close must
    end the thread (and free the socket) every time."""
    from chuk_mcp_telnet_client import tools

    monkeypatch.setattr(tools.TerminalDefaults, "INITIAL_BANNER_WAIT", 0.0)

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(128)
    port = srv.getsockname()[1]
    held = []
    stop = threading.Event()

    def accept_loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
            except (socket.timeout, OSError):
                continue
            c.sendall(b"BANNER\r\n$ ")  # then silence: the reader blocks
            held.append(c)

    acceptor = threading.Thread(target=accept_loop, daemon=True)
    acceptor.start()

    # Every session object is kept: otherwise CPython frees an unclosed socket when the
    # session is dropped, and a missing close() would not show in the descriptor count
    # (sabotage S2 measured exactly that before this list existed).
    sessions = []

    async def run():
        worst_close = 0.0
        for i in range(100):
            sid = f"cycle{i}"
            await tools.telnet_client_tool(
                host="127.0.0.1", port=port, commands=[], telnet_session_id=sid,
                enable_logging=False)
            await asyncio.sleep(0.01)
            alive = [t for t in _reader_threads() if t.name.endswith(sid)]
            assert len(alive) == 1 and alive[0].is_alive(), f"cycle {i}: no live reader thread"
            sessions.append(await tools._session_store.get(sid))
            t0 = time.monotonic()
            await tools.telnet_close_session(sid)
            worst_close = max(worst_close, time.monotonic() - t0)
            assert not alive[0].is_alive(), f"cycle {i}: reader still alive after close"
            assert sessions[-1].telnet.get_socket() is None, f"cycle {i}: socket not closed"
        return worst_close

    # Counted outside asyncio.run (whose loop holds its own descriptors), with the
    # listening socket already open; the fake server's accepted ends are closed before
    # the second count, so only the client's 100 sockets could make a difference.
    fds_before = _fd_count()
    try:
        worst_close = asyncio.run(run())
    finally:
        stop.set()
        acceptor.join(2)
        for c in held:
            c.close()
    fds_after = _fd_count()
    srv.close()
    leaked = _reader_threads()
    print(f"load {_load()}; worst close {worst_close:.3f} s; reader threads left {len(leaked)}; "
          f"fds before {fds_before} after {fds_after} (server ends: {len(held)})")
    assert len(held) == 100
    assert leaked == []
    assert worst_close < 1.0
    assert fds_after <= fds_before, "client sockets leaked"


def test_serial_close_while_reader_blocked():
    from chuk_mcp_telnet_client import tools

    master, slave = pty.openpty()
    tty.setraw(master)
    tty.setraw(slave)
    path = os.ttyname(slave)

    async def run():
        await tools.serial_client_tool(port=path, commands=[], serial_session_id="sclose",
                                       response_wait=0.0, enable_logging=False)
        await asyncio.sleep(0.05)
        alive = [t for t in _reader_threads() if t.name.endswith("sclose")]
        assert len(alive) == 1 and alive[0].is_alive()
        t0 = time.monotonic()
        await tools.serial_close_session("sclose")
        took = time.monotonic() - t0
        assert not alive[0].is_alive()
        return took

    try:
        took = asyncio.run(run())
        print(f"load {_load()}; serial close took {took:.3f} s")
        assert took < 1.0
        assert _reader_threads() == []
    finally:
        os.close(master)
        os.close(slave)


# ============================================================================
# 5. Serial: the same idle case, through a pty pair, over stdio (RED on 09dbaa0)
# ============================================================================


def test_idle_serial_screen_is_current_over_stdio(mcp):
    master, slave = pty.openpty()
    tty.setraw(master)
    tty.setraw(slave)
    path = os.ttyname(slave)
    try:
        # Nothing is written before the open: pyserial's open() discards pending input
        # (serialposix.py, _reset_input_buffer), so a pre-open banner never arrives.
        mcp.tool("serial_client", {
            "port": path, "commands": [], "serial_session_id": "sidle", "response_wait": 0.5})
        t_conn = time.time()
        time.sleep(1.5)
        os.write(master, b"\r\nLATER\r\n")
        t_sent = time.time()
        time.sleep(2.5)  # no call during this stretch
        r = mcp.tool("terminal_get_screen", {"session_id": "sidle"})
        print(f"load {_load()}; LATER sent +{t_sent - t_conn:.2f} s after connect; "
              f"read +{time.time() - t_sent:.2f} s after the send")
        stale = "LATER" not in r["screen_text"]
        # Control: a read that gives even the 0.7.0 reader time does see it, so a RED
        # above is staleness, not a session that never receives anything.
        r1 = mcp.tool("terminal_get_screen", {"session_id": "sidle", "wait_seconds": 1.0})
        assert "LATER" in r1["screen_text"], "control: LATER never reached the screen"
        assert not stale, "wait-0 serial screen is stale: LATER not on it"
        mcp.tool("serial_close_session", {"session_id": "sidle"})
    finally:
        os.close(master)
        os.close(slave)
