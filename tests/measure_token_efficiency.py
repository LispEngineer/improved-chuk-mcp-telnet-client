"""Measure the size of typical tool results (docs/TOKEN_EFFICIENCY.md).

Not a test (no test_ prefix): run it by hand, once per version, with that version's
interpreter:

    python tests/measure_token_efficiency.py out.json [--include-raw]

It drives the real stdio MCP server of this checkout against a local fake host
(no remote machine is contacted) and writes {scenario: bytes of the result text
the client receives}. --include-raw adds `include_raw: true` to the stream calls
(0.9.0 and later) so the cost of asking for exact bytes can be measured too.
"""

import json
import os
import pty
import select
import socket
import sys
import tempfile
import threading
import time
import tty

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_m1_live_reader import Mcp  # noqa: E402

SHOW_SYSTEM = (
    "OpenVMS V8.4-2L1  on node AXP1  14-NOV-2026 16:00:00.12  Uptime  41 03:12:09\r\n"
    "  Pid    Process Name    State  Pri      I/O       CPU       Page flts  Pages\r\n"
    "00000401 SWAPPER         HIB     16        0   0 00:00:00.43         0      0\r\n"
    "00000405 CONFIGURE       HIB     10        4   0 00:00:00.03       110    142\r\n"
    "00000406 LANACP          HIB      8       70   0 00:00:00.50       299    355\r\n"
    "00000407 IPCACP          HIB     10        6   0 00:00:00.02        85     99\r\n"
    "00000408 ERRFMT          HIB      8       58   0 00:00:00.14       114    137\r\n"
    "0000040C CACHE_SERVER    HIB     16        8   0 00:00:00.01        61     81\r\n"
    "0000040E OPCOM           HIB      6       97   0 00:00:00.37       126    172\r\n"
    "00000411 SECURITY_SERVER HIB      8      121   0 00:00:00.59       536    390\r\n"
    "00000419 TCPIP$INETACP   HIB     10       41   0 00:00:00.18       201    252\r\n"
    "0000041C SYSTEM          CUR      4     1033   0 00:00:03.51      1980    420\r\n"
)
PROMPT = "\r\n$ "
# A 24x80 VT100 screen: clear, a title, 12 text lines, one reverse-video line, cursor at bottom.
SCREEN = "\x1b[2J\x1b[H" + "".join(
    f"\x1b[{row};1H" + (f"\x1b[7m MENU ITEM {row:<20}\x1b[0m" if row == 5 else f"Line {row}: some terminal text")
    for row in range(1, 13)
) + "\x1b[22;1H$ "
# The same screen without the reverse-video line.
PLAIN_SCREEN = "\x1b[2J\x1b[H" + "".join(f"\x1b[{row};1HLine {row}: some terminal text" for row in range(1, 13)) + "\x1b[22;1H$ "
BINARY = b"\x80\x81\x1b[?25h\x07noise\xc3\x28\r\n$ "


class Host:
    """Telnet or serial-pty peer: replies to each line with a canned answer."""

    def __init__(self, transport):
        self.transport = transport
        self.stop = threading.Event()
        if transport == "telnet":
            self.srv = socket.socket()
            self.srv.bind(("127.0.0.1", 0))
            self.srv.listen(1)
            self.port = self.srv.getsockname()[1]
        else:
            self.master, slave = pty.openpty()
            tty.setraw(self.master)
            tty.setraw(slave)
            self.slave = slave
            self.port = os.ttyname(slave)
        threading.Thread(target=self._serve, daemon=True).start()

    def _send(self, data):
        if self.transport == "telnet":
            self.conn.sendall(data)
        else:
            os.write(self.master, data)

    def _serve(self):
        if self.transport == "telnet":
            self.conn, _ = self.srv.accept()
            self._send(b"\r\nWelcome to AXP1\r\n$ ")
        buf = b""
        while not self.stop.is_set():
            fd = self.conn if self.transport == "telnet" else self.master
            if not select.select([fd], [], [], 0.1)[0]:
                continue
            data = self.conn.recv(4096) if self.transport == "telnet" else os.read(self.master, 4096)
            if not data:
                return
            buf += data
            while b"\n" in buf or (self.transport == "serial" and b"\r" in buf):
                sep = b"\n" if b"\n" in buf else b"\r"
                line, _, buf = buf.partition(sep)
                line = line.strip(b"\r\n")
                if line == b"SCREEN":
                    self._send(SCREEN.encode())
                elif line == b"PLAIN":
                    self._send(PLAIN_SCREEN.encode())
                elif line == b"BINARY":
                    self._send(BINARY)
                else:
                    self._send(SHOW_SYSTEM.encode() + PROMPT.encode())

    def close(self):
        self.stop.set()


def size(result):
    """Bytes of the text the MCP client receives for one tool result."""
    return len(json.dumps(result, ensure_ascii=False, indent=2).encode())


def compact_size(result):
    return len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())


def main():
    out_path = sys.argv[1]
    raw = {"include_raw": True} if "--include-raw" in sys.argv else {}
    log_dir = tempfile.mkdtemp(dir=os.path.expanduser("~/tmp/vmsx-telmcp"))
    mcp = Mcp(log_dir)
    sizes, compact = {}, {}

    def rec(name, result):
        sizes[name] = size(result)
        compact[name] = compact_size(result)

    try:
        t = Host("telnet")
        r = mcp.tool("telnet_client", dict(host="127.0.0.1", port=t.port, commands=["SHOW SYSTEM"],
                                           telnet_session_id="t", response_wait=0.5, **raw))
        rec("telnet_client (connect + 1 command)", r)
        r = mcp.tool("telnet_client", dict(host="127.0.0.1", port=t.port, commands=["SHOW SYSTEM"],
                                           telnet_session_id="t", **raw))
        rec("telnet_client (existing session, 1 command)", r)
        r = mcp.tool("telnet_send_input", dict(session_id="t", input_text="SHOW SYSTEM", **raw))
        rec("telnet_send_input", r)
        r = mcp.tool("telnet_read_session", dict(session_id="t", max_wait_seconds=0, **raw))
        rec("telnet_read_session (nothing new)", r)
        mcp.tool("telnet_send_input", dict(session_id="t", input_text="SHOW SYSTEM", max_wait_seconds=0))
        time.sleep(0.5)
        r = mcp.tool("telnet_read_session", dict(session_id="t", max_wait_seconds=0, **raw))
        rec("telnet_read_session (one SHOW SYSTEM waiting)", r)
        r = mcp.tool("telnet_send_input", dict(session_id="t", input_text="BINARY", **raw))
        rec("telnet_send_input (reply has invalid UTF-8)", r)

        mcp.tool("telnet_send_input", dict(session_id="t", input_text="SCREEN", max_wait_seconds=1,
                                           prompt_pattern=r"\$ $"))
        time.sleep(0.5)
        rec("terminal_get_screen (24x80, 12 lines used, one reverse-video line)",
            mcp.tool("terminal_get_screen", dict(session_id="t")))
        rec("terminal_wait_for_change (screen unchanged)",
            mcp.tool("terminal_wait_for_change", dict(session_id="t", timeout_seconds=0.5, settle_seconds=0.1)))
        rec("terminal_send_key (ENTER)", mcp.tool("terminal_send_key", dict(session_id="t", key="ENTER",
                                                                               wait_seconds=0.3)))
        mcp.tool("telnet_send_input", dict(session_id="t", input_text="PLAIN", max_wait_seconds=1,
                                           prompt_pattern=r"\$ $"))
        time.sleep(0.5)
        rec("terminal_get_screen (24x80, 12 lines used, plain)", mcp.tool("terminal_get_screen", dict(session_id="t")))
        rec("telnet_close_session", mcp.tool("telnet_close_session", dict(session_id="t")))
        t.close()

        s = Host("serial")
        r = mcp.tool("serial_client", dict(port=s.port, commands=["SHOW SYSTEM"], serial_session_id="s",
                                           response_wait=0.5, newline="\r", **raw))
        rec("serial_client (connect + 1 command)", r)
        r = mcp.tool("serial_send_input", dict(session_id="s", input_text="SHOW SYSTEM", newline="\r", **raw))
        rec("serial_send_input", r)
        r = mcp.tool("serial_read_session", dict(session_id="s", max_wait_seconds=0, **raw))
        rec("serial_read_session (nothing new)", r)
        rec("serial_close_session", mcp.tool("serial_close_session", dict(session_id="s")))
        s.close()
        rec("list_sessions (none open)", mcp.tool("list_sessions", {}))
    finally:
        mcp.close()

    with open(out_path, "w") as f:
        json.dump({"indent2_bytes": sizes, "compact_bytes": compact}, f, indent=1)
    for k, v in sizes.items():
        print(f"{v:6d} {compact[k]:6d}  {k}")


if __name__ == "__main__":
    main()
