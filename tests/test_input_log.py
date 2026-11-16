"""
v0.9.1: the opt-in input log (log_input=True) - [INPUT] records in the session transcript.

Fake hosts only (a threaded TCP server; the PTY fake of test_standalone_serial for serial).
The transcript is read back from disk, so these tests check what a playtest analysis would see.
"""

import asyncio
import os
import re
import socket
import threading
import time
from datetime import datetime

import pytest

from chuk_mcp_telnet_client.logger import INPUT_SUPPRESSED_TEXT
from chuk_mcp_telnet_client.tools import (
    PASSWORD_PROMPT_PATTERN,
    _has_password_prompt,
    serial_client_tool,
    serial_close_session,
    serial_send_input,
    telnet_client_tool,
    telnet_close_session,
    telnet_send_input,
    terminal_send_key,
)
from tests.test_standalone_serial import MockVaxSerialDevice

STAMP = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d"
INPUT_LINE = re.compile(rf"^\[({STAMP})\] \[INPUT\] (.*)$", re.M)
ANY_STAMP = re.compile(r"^\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?[+-]\d\d:\d\d)\] ", re.M)


class ScriptedHost:
    """A one-connection TCP fake that answers whole lines and records what it received."""

    def __init__(self):
        self.got = bytearray()
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.awaiting_password = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        conn.sendall(b"\r\nWelcome to the fake host\r\n$ ")
        buf = b""
        while True:
            try:
                data = conn.recv(4096)
            except OSError:
                return
            if not data:
                return
            self.got.extend(data)
            buf += data
            while b"\r" in buf or b"\n" in buf:
                for sep in (b"\r\n", b"\r", b"\n"):
                    if sep in buf:
                        line, _, buf = buf.partition(sep)
                        break
                try:
                    self._answer(conn, line.decode("utf-8", errors="replace").strip())
                except OSError:
                    return

    def _answer(self, conn, line):
        if self.awaiting_password:
            self.awaiting_password = False
            conn.sendall(b"\r\nLogged in\r\n$ ")
        elif line == "USER":
            self.awaiting_password = True
            conn.sendall(b"\r\nPassword: ")
        elif line == "LOGIN":  # the prompt arrives in one chunk with other text
            self.awaiting_password = True
            conn.sendall(b"\r\nLast login: yesterday\r\nUsername: SYSTEM\r\nPassword: ")
        elif line == "SPLITPW":  # a prompt split across two chunks
            self.awaiting_password = True
            conn.sendall(b"\r\nOld pass")
            time.sleep(0.15)
            conn.sendall(b"word: ")
        elif line in ("PWBC", "PWBC2"):  # a prompt, then unsolicited broadcast(s), then silence
            self.awaiting_password = True
            conn.sendall(b"\r\nPassword: ")
            for n in range(1 if line == "PWBC" else 2):
                time.sleep(0.15)
                conn.sendall(b"\r\n%BROADCAST 0002" + str(n).encode()
                             + b" Reply received on AXP1 from user OPER\r\n")
        elif line == "SHOW TIME":
            time.sleep(0.05)
            conn.sendall(b"\r\n   4-SEP-2026 23:30:00\r\n$ ")
        elif line:
            conn.sendall(f"\r\n%DCL-I-EXECUTED, {line}\r\n$ ".encode())

    def stop(self):
        try:
            self.srv.close()
        except OSError:
            pass


@pytest.fixture
def host():
    h = ScriptedHost()
    yield h
    h.stop()


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _inputs(path):
    return INPUT_LINE.findall(_read(path))


def _stamp(text):
    return datetime.fromisoformat(text).timestamp()


def _run(coro):
    return asyncio.run(coro)


# (1) off by default -----------------------------------------------------------------------

def test_log_off_by_default_writes_no_input_lines(host, tmp_path):
    async def go():
        sid = "inlog_off"
        res = await telnet_client_tool(
            host="127.0.0.1", port=host.port, commands=["SHOW TIME", "DIR"],
            telnet_session_id=sid, log_dir=str(tmp_path), timestamp_chunks=True,
            max_wait_seconds=3.0)
        await telnet_send_input(session_id=sid, input_text="HELLO", max_wait_seconds=3.0)
        await terminal_send_key(session_id=sid, key="UP", wait_seconds=0.05)
        await telnet_close_session(sid)
        return res.log_file

    log = _run(go())
    text = _read(log)
    assert "[INPUT]" not in text
    assert "suppressed" not in text
    assert b"[INPUT]" not in open(log + ".raw", "rb").read()


# (2) on: a record per send, ordered with the received records ------------------------------

def test_log_on_records_each_send_in_time_order(host, tmp_path):
    async def go():
        sid = "inlog_on"
        res = await telnet_client_tool(
            host="127.0.0.1", port=host.port, commands=["SHOW TIME"],
            telnet_session_id=sid, log_dir=str(tmp_path), log_input=True,
            max_wait_seconds=3.0)
        await asyncio.sleep(0.1)
        await telnet_send_input(session_id=sid, input_text="DIR", max_wait_seconds=3.0)
        await asyncio.sleep(0.1)
        await telnet_send_input(session_id=sid, input_text="SHOW TIME", max_wait_seconds=3.0)
        await telnet_close_session(sid)
        return res.log_file

    log = _run(go())
    text = _read(log)
    records = _inputs(log)
    assert [r[1] for r in records] == ["SHOW TIME", "DIR", "SHOW TIME"]

    # Every stamped record (received output included) is on the same clock: in file order
    # the stamps do not go backwards (to the millisecond the [INPUT] stamps are cut at).
    stamps = [_stamp(m) for m in ANY_STAMP.findall(text)]
    assert len(stamps) >= 6  # banner, 3 inputs, 3 replies, at least
    assert all(b >= a - 0.001 for a, b in zip(stamps, stamps[1:])), stamps

    # A reply time is the gap from an [INPUT] record to the next received record.
    lines = text.splitlines()
    idx = [i for i, l in enumerate(lines) if "[INPUT] SHOW TIME" in l][1]
    t_in = _stamp(ANY_STAMP.match(lines[idx]).group(1))
    nxt = next(l for l in lines[idx + 1:] if "[STREAM DATA]" in l)
    t_out = _stamp(ANY_STAMP.match(nxt).group(1))
    assert 0.03 < t_out - t_in < 1.0  # the fake host waits 50 ms before answering SHOW TIME

    # Input is never written to the byte-exact sidecar of received bytes.
    raw = open(log + ".raw", "rb").read()
    assert b"[INPUT]" not in raw
    assert raw.count(b"%DCL-I-EXECUTED, DIR") == 1  # only the host's own reply, which names the command


def test_log_input_can_be_switched_on_later_and_off_again(host, tmp_path):
    async def go():
        sid = "inlog_switch2"
        res = await telnet_client_tool(
            host="127.0.0.1", port=host.port, commands=["ONE"],
            telnet_session_id=sid, log_dir=str(tmp_path), max_wait_seconds=3.0)
        await telnet_send_input(session_id=sid, input_text="TWO", max_wait_seconds=3.0,
                                log_input=True)
        await telnet_send_input(session_id=sid, input_text="THREE", max_wait_seconds=3.0)
        await telnet_send_input(session_id=sid, input_text="FOUR", max_wait_seconds=3.0,
                                log_input=False)
        await telnet_send_input(session_id=sid, input_text="FIVE", max_wait_seconds=3.0)
        await telnet_close_session(sid)
        return res.log_file

    # ONE went before it was on; FOUR is the call that turns it off, so it is not logged.
    assert [r[1] for r in _inputs(_run(go()))] == ["TWO", "THREE"]


def test_raw_send_and_control_characters_are_marked_and_escaped(host, tmp_path):
    async def go():
        sid = "inlog_raw"
        res = await telnet_client_tool(
            host="127.0.0.1", port=host.port, commands=["A\tB"],
            telnet_session_id=sid, log_dir=str(tmp_path), log_input=True, max_wait_seconds=3.0)
        await telnet_send_input(session_id=sid, input_text="C\r", raw=True, max_wait_seconds=3.0)
        await telnet_close_session(sid)
        return res.log_file

    assert [r[1] for r in _inputs(_run(go()))] == ["A\\tB", "(raw) C\\r"]


# (3) password suppression -------------------------------------------------------------------

def test_send_after_password_prompt_is_suppressed(host, tmp_path):
    async def go():
        sid = "inlog_pw"
        res = await telnet_client_tool(
            host="127.0.0.1", port=host.port, commands=["USER", "hunter2", "SHOW TIME"],
            telnet_session_id=sid, log_dir=str(tmp_path), log_input=True, max_wait_seconds=3.0)
        await telnet_close_session(sid)
        return res.log_file

    log = _run(go())
    assert [r[1] for r in _inputs(log)] == ["USER", INPUT_SUPPRESSED_TEXT, "SHOW TIME"]
    assert INPUT_SUPPRESSED_TEXT == "(suppressed: password prompt)"
    assert "hunter2" not in _read(log)
    assert b"hunter2" not in open(log + ".raw", "rb").read()  # the fake does not echo it


def test_password_prompt_in_a_chunk_with_other_text(host, tmp_path):
    """The prompt arrives in the same chunk as a banner line and a Username: line."""
    async def go():
        sid = "inlog_pw_chunk"
        await telnet_client_tool(
            host="127.0.0.1", port=host.port, commands=[], telnet_session_id=sid,
            log_dir=str(tmp_path), log_input=True, max_wait_seconds=3.0)
        res = await telnet_send_input(session_id=sid, input_text="LOGIN", max_wait_seconds=3.0)
        assert "Last login: yesterday" in res.response and res.response.endswith("Password: ")
        await telnet_send_input(session_id=sid, input_text="s3cret-in-chunk", max_wait_seconds=3.0)
        await telnet_send_input(session_id=sid, input_text="SHOW TIME", max_wait_seconds=3.0)
        await telnet_close_session(sid)
        return os.path.join(str(tmp_path), f"{sid}.log")

    log = _run(go())
    assert [r[1] for r in _inputs(log)] == ["LOGIN", INPUT_SUPPRESSED_TEXT, "SHOW TIME"]
    assert "s3cret-in-chunk" not in _read(log)


def test_password_prompt_split_across_chunks(host, tmp_path):
    async def go():
        sid = "inlog_pw_split"
        await telnet_client_tool(
            host="127.0.0.1", port=host.port, commands=[], telnet_session_id=sid,
            log_dir=str(tmp_path), log_input=True, max_wait_seconds=3.0)
        await telnet_send_input(session_id=sid, input_text="SPLITPW", max_wait_seconds=3.0)
        await telnet_send_input(session_id=sid, input_text="newpw-split", max_wait_seconds=3.0)
        await telnet_close_session(sid)
        return os.path.join(str(tmp_path), f"{sid}.log")

    log = _run(go())
    assert [r[1] for r in _inputs(log)] == ["SPLITPW", INPUT_SUPPRESSED_TEXT]
    assert "newpw-split" not in _read(log)


def test_keys_after_a_password_prompt_are_suppressed(host, tmp_path):
    async def go():
        sid = "inlog_pw_keys"
        await telnet_client_tool(
            host="127.0.0.1", port=host.port, commands=[], telnet_session_id=sid,
            log_dir=str(tmp_path), log_input=True, max_wait_seconds=3.0)
        await telnet_send_input(session_id=sid, input_text="USER", max_wait_seconds=3.0)
        await terminal_send_key(session_id=sid, key="p", wait_seconds=0.05)
        await terminal_send_key(session_id=sid, key="w", wait_seconds=0.05)
        await terminal_send_key(session_id=sid, key="ENTER", wait_seconds=0.3)
        await terminal_send_key(session_id=sid, key="TAB", wait_seconds=0.05)
        await telnet_close_session(sid)
        return os.path.join(str(tmp_path), f"{sid}.log")

    log = _run(go())
    # p, w and ENTER follow Password:; the host's reply ends in "$ ", so TAB is logged.
    assert [r[1] for r in _inputs(log)] == ["USER"] + [INPUT_SUPPRESSED_TEXT] * 3 + ["<TAB>"]


@pytest.mark.parametrize("received", [
    "\r\nPassword: ",
    "\r\npassword:",
    "\r\nPASSWORD: ",
    "\r\nOld password: ",
    "\r\nNew password: ",
    "\r\nVerification: ",
    "\r\nPassword for admin: ",
    "Username: SYSTEM\r\nPassword: ",
    "\r\nPassword: ***",                  # a prompt followed by echoed characters
    "\x1b[1mPassword:\x1b[0m ",           # with display attributes around it
    "\r\nPassword: \r\n%BROADCAST 00029 Reply received on AXP1 from user OPER\r\n",  # not the last line
    "Password: \r\nWelcome\r\n$ ",         # anywhere in the text: errs toward suppressing
])
def test_password_prompts_recognised(received):
    assert _has_password_prompt(received) is True
    assert PASSWORD_PROMPT_PATTERN.search("Password: ")


@pytest.mark.parametrize("received", [
    "",
    "\r\n$ ",
    "\r\nLogged in\r\n$ ",
    "\r\n%LOGIN-I-PASSWORD changed\r\n$ ",  # no colon-ended prompt on the last line
    "\r\nUsername: ",
])
def test_non_password_text_is_not_suppressed(received):
    assert _has_password_prompt(received) is False


# (4) terminal_send_key: named keys are logged by name ---------------------------------------

def test_send_key_named_keys_logged_by_name(host, tmp_path):
    async def go():
        sid = "inlog_keys"
        res = await telnet_client_tool(
            host="127.0.0.1", port=host.port, commands=[], telnet_session_id=sid,
            log_dir=str(tmp_path), log_input=True, max_wait_seconds=3.0)
        for key in ("UP", "tab", " CTRL_Z ", "x", " ", "F1"):
            await terminal_send_key(session_id=sid, key=key, wait_seconds=0.02)
        await terminal_send_key(session_id=sid, key="ENTER", wait_seconds=0.1, log_input=False)
        await terminal_send_key(session_id=sid, key="DOWN", wait_seconds=0.02)
        await telnet_close_session(sid)
        return res.log_file

    log = _run(go())
    assert [r[1] for r in _inputs(log)] == ['<UP>', '<TAB>', '<CTRL_Z>', '"x"', '" "', '<F1>']
    assert b"[INPUT]" not in open(log + ".raw", "rb").read()


def test_send_key_log_off_by_default(host, tmp_path):
    async def go():
        sid = "inlog_keys_off"
        res = await telnet_client_tool(
            host="127.0.0.1", port=host.port, commands=[], telnet_session_id=sid,
            log_dir=str(tmp_path), max_wait_seconds=3.0)
        await terminal_send_key(session_id=sid, key="UP", wait_seconds=0.02)
        await telnet_close_session(sid)
        return res.log_file

    assert "[INPUT]" not in _read(_run(go()))


# (5) serial, through the PTY fake -----------------------------------------------------------

def test_serial_input_log_and_password_suppression(tmp_path):
    async def go():
        vax = MockVaxSerialDevice()
        sid = "inlog_serial"
        try:
            await serial_close_session(sid)
            res = await serial_client_tool(
                port=vax.port, commands=["SYSTEM", "sysvax60", "SHOW TIME"],
                serial_session_id=sid, log_dir=str(tmp_path), log_input=True,
                max_wait_seconds=5.0)
            assert res.command_completed is True
            await serial_send_input(session_id=sid, input_text="DIR", max_wait_seconds=5.0)
            await serial_send_input(session_id=sid, input_text="SYSTEM", max_wait_seconds=5.0)
            await serial_send_input(session_id=sid, input_text="sysvax60", max_wait_seconds=5.0)
            await terminal_send_key(session_id=sid, key="UP", wait_seconds=0.1)
            await serial_close_session(sid)
            return res.log_file
        finally:
            vax.stop()

    log = _run(go())
    assert [r[1] for r in _inputs(log)] == [
        "SYSTEM", INPUT_SUPPRESSED_TEXT, "SHOW TIME", "DIR", "SYSTEM", INPUT_SUPPRESSED_TEXT, "<UP>"]
    text = _read(log)
    assert "sysvax60" not in text
    stamps = [_stamp(m) for m in ANY_STAMP.findall(text)]
    assert all(b >= a - 0.001 for a, b in zip(stamps, stamps[1:])), stamps
    assert b"[INPUT]" not in open(log + ".raw", "rb").read()


def test_serial_input_log_off_by_default(tmp_path):
    async def go():
        vax = MockVaxSerialDevice()
        sid = "inlog_serial_off"
        try:
            await serial_close_session(sid)
            res = await serial_client_tool(
                port=vax.port, commands=["SHOW TIME"], serial_session_id=sid,
                log_dir=str(tmp_path), timestamp_chunks=True, max_wait_seconds=5.0)
            await serial_close_session(sid)
            return res.log_file
        finally:
            vax.stop()

    assert "[INPUT]" not in _read(_run(go()))


# Review fix (vms-exploration-orch, 15-NOV-2026): a broadcast after the prompt hid it -------

@pytest.mark.parametrize("trigger,broadcasts", [("PWBC", 1), ("PWBC2", 2)])
def test_broadcast_between_prompt_and_password(host, tmp_path, trigger, broadcasts):
    async def go():
        sid = "inlog_pw_bc_" + trigger
        await telnet_client_tool(
            host="127.0.0.1", port=host.port, commands=[], telnet_session_id=sid,
            log_dir=str(tmp_path), log_input=True, max_wait_seconds=3.0)
        await telnet_send_input(session_id=sid, input_text=trigger, max_wait_seconds=3.0)
        await asyncio.sleep(0.15 * broadcasts + 0.4)  # the broadcasts arrive after the prompt
        res = await telnet_send_input(session_id=sid, input_text="notarealpw",
                                      max_wait_seconds=3.0)
        await telnet_send_input(session_id=sid, input_text="SHOW TIME", max_wait_seconds=3.0)
        await telnet_close_session(sid)
        return os.path.join(str(tmp_path), f"{sid}.log"), res

    log, res = _run(go())
    assert "BROADCAST" in _read(log)
    assert "notarealpw" not in _read(log)
    assert [r[1] for r in _inputs(log)] == [trigger, INPUT_SUPPRESSED_TEXT, "SHOW TIME"]


def test_second_login_cycle_after_a_successful_login(host, tmp_path):
    async def go():
        sid = "inlog_pw_twice"
        await telnet_client_tool(
            host="127.0.0.1", port=host.port, commands=[], telnet_session_id=sid,
            log_dir=str(tmp_path), log_input=True, max_wait_seconds=3.0)
        for _ in range(2):
            await telnet_send_input(session_id=sid, input_text="USER", max_wait_seconds=3.0)
            await telnet_send_input(session_id=sid, input_text="pw-cycle", max_wait_seconds=3.0)
            await telnet_send_input(session_id=sid, input_text="SHOW TIME", max_wait_seconds=3.0)
        await telnet_close_session(sid)
        return os.path.join(str(tmp_path), f"{sid}.log")

    log = _run(go())
    assert "pw-cycle" not in _read(log)
    assert [r[1] for r in _inputs(log)] == ["USER", INPUT_SUPPRESSED_TEXT, "SHOW TIME"] * 2


def test_password_typed_key_by_key_across_a_broadcast(host, tmp_path):
    async def go():
        sid = "inlog_pw_keys_bc"
        await telnet_client_tool(
            host="127.0.0.1", port=host.port, commands=[], telnet_session_id=sid,
            log_dir=str(tmp_path), log_input=True, max_wait_seconds=3.0)
        await telnet_send_input(session_id=sid, input_text="PWBC", max_wait_seconds=3.0)
        await terminal_send_key(session_id=sid, key="p", wait_seconds=0.05)
        await asyncio.sleep(0.4)  # the broadcast lands between two keys
        await terminal_send_key(session_id=sid, key="w", wait_seconds=0.05)
        await terminal_send_key(session_id=sid, key="ENTER", wait_seconds=0.3)
        await terminal_send_key(session_id=sid, key="TAB", wait_seconds=0.05)
        await telnet_close_session(sid)
        return os.path.join(str(tmp_path), f"{sid}.log")

    log = _run(go())
    assert [r[1] for r in _inputs(log)] == ["PWBC"] + [INPUT_SUPPRESSED_TEXT] * 3 + ["<TAB>"]


def test_input_log_switched_on_after_a_prompt_does_not_leak(host, tmp_path):
    async def go():
        sid = "inlog_pw_late"
        await telnet_client_tool(
            host="127.0.0.1", port=host.port, commands=["USER"], telnet_session_id=sid,
            log_dir=str(tmp_path), max_wait_seconds=3.0)
        await telnet_send_input(session_id=sid, input_text="late-pw", max_wait_seconds=3.0,
                                log_input=True)
        await telnet_send_input(session_id=sid, input_text="SHOW TIME", max_wait_seconds=3.0)
        await telnet_close_session(sid)
        return os.path.join(str(tmp_path), f"{sid}.log")

    log = _run(go())
    assert "late-pw" not in _read(log)
    assert [r[1] for r in _inputs(log)] == [INPUT_SUPPRESSED_TEXT, "SHOW TIME"]
