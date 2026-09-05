"""
Standalone offline tests for Serial MCP tools.
Uses virtual pseudo-terminals (PTY) to test without physical serial hardware.
"""

import asyncio
import os
import pty
import shutil
import tempfile
import threading
import time
import tty
import pytest

from chuk_mcp_telnet_client.tools import (
    serial_client_tool,
    serial_read_session,
    serial_send_input,
    serial_send_break,
    serial_set_speed,
    serial_list_ports,
    serial_close_session,
    telnet_client_tool,
    telnet_close_session,
    list_sessions,
)
from tests.test_standalone_telnet import MockTelnetServer


class MockVaxSerialDevice:
    """Mock VAX serial console responder running over a virtual PTY."""

    def __init__(self):
        self.master_fd, self.slave_fd = pty.openpty()
        tty.setraw(self.master_fd)
        tty.setraw(self.slave_fd)
        self.port = os.ttyname(self.slave_fd)
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self):
        # OpenVMS initial console banner
        try:
            os.write(self.master_fd, b"\r\n Welcome to OpenVMS (TM) VAX Operating System, Version V7.3\r\n\r\nUsername: ")
        except OSError:
            return

        buf = b""
        while self.running:
            try:
                chunk = os.read(self.master_fd, 1024)
                if not chunk:
                    break
                buf += chunk
                while b"\r\n" in buf or b"\n" in buf or b"\r" in buf:
                    for sep in [b"\r\n", b"\n", b"\r"]:
                        if sep in buf:
                            line, _, buf = buf.partition(sep)
                            cmd = line.decode(errors="ignore").strip()
                            self._handle_command(cmd)
                            break
            except OSError:
                break

    def _handle_command(self, cmd: str):
        try:
            if cmd == "SYSTEM":
                os.write(self.master_fd, b"\r\nPassword: ")
            elif cmd == "sysvax60":
                os.write(self.master_fd, b"\r\n$ ")
            elif cmd == "SHOW TIME":
                os.write(self.master_fd, b"\r\n   4-SEP-2026 23:30:00\r\n$ ")
            elif cmd == "STREAM_JOB":
                for i in range(1, 4):
                    time.sleep(0.1)
                    os.write(self.master_fd, f"\r\nStage {i} finished".encode("utf-8"))
                time.sleep(0.1)
                os.write(self.master_fd, b"\r\n$ ")
            elif cmd == "INTERACTIVE_PROMPT":
                os.write(self.master_fd, b"\r\nAre you sure you want to proceed? [NO]: ")
            elif cmd == "YES":
                os.write(self.master_fd, b"\r\nAction confirmed.\r\n$ ")
            elif cmd == "SPEED_TEST":
                os.write(self.master_fd, b"\r\nBaud rate changed successfully.\r\n$ ")
            elif cmd == "HALT_VAX":
                os.write(self.master_fd, b"\r\n?02 EXT HLT\r\n  PC= 80092B40\r\n>>> ")
            elif cmd:
                os.write(self.master_fd, f"\r\n%DCL-I-EXECUTED, {cmd}\r\n$ ".encode("utf-8"))
        except OSError:
            pass

    def send_raw(self, data: bytes):
        try:
            os.write(self.master_fd, data)
        except OSError:
            pass

    def stop(self):
        self.running = False
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        try:
            os.close(self.slave_fd)
        except OSError:
            pass


@pytest.fixture
def temp_log_dir():
    d = tempfile.mkdtemp(prefix="serial_test_logs_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_serial_connect_and_login(temp_log_dir):
    """Test serial port connection, login dialog, and command execution."""
    async def _test():
        vax = MockVaxSerialDevice()
        session_id = "test_ser_login"
        try:
            await serial_close_session(session_id)

            res = await serial_client_tool(
                port=vax.port,
                commands=["SYSTEM", "sysvax60", "SHOW TIME"],
                serial_session_id=session_id,
                log_dir=temp_log_dir,
                max_wait_seconds=5.0,
            )

            assert res.session_id == session_id
            assert res.session_active is True
            assert res.command_completed is True
            assert len(res.responses) == 3
            assert "4-SEP-2026" in res.responses[-1].response
            assert res.log_file is not None
            assert os.path.exists(res.log_file)

            with open(res.log_file, "r") as f:
                content = f.read()
            assert "SERIAL SESSION LOG" in content
            assert "SHOW TIME" in content

            await serial_close_session(session_id)
        finally:
            vax.stop()

    asyncio.run(_test())


def test_serial_read_session(temp_log_dir):
    """Test long-running background streaming on serial port via serial_read_session."""
    async def _test():
        vax = MockVaxSerialDevice()
        session_id = "test_ser_stream"
        try:
            await serial_close_session(session_id)

            # Login
            await serial_client_tool(
                port=vax.port,
                commands=["SYSTEM", "sysvax60"],
                serial_session_id=session_id,
                log_dir=temp_log_dir,
                max_wait_seconds=5.0,
            )

            # Launch STREAM_JOB with short wait to test asynchronous read
            res1 = await serial_client_tool(
                port=vax.port,
                commands=["STREAM_JOB"],
                serial_session_id=session_id,
                max_wait_seconds=0.05,
            )
            assert res1.session_active is True
            assert res1.command_completed is False

            # Poll until prompt returned
            res2 = await serial_read_session(
                session_id=session_id,
                max_wait_seconds=3.0,
                prompt_pattern=r"(?m)^\$ ",
            )
            assert res2.command_completed is True
            assert "Stage" in res2.output

            await serial_close_session(session_id)
        finally:
            vax.stop()

    asyncio.run(_test())


def test_serial_send_input(temp_log_dir):
    """Test interactive serial prompt responses via serial_send_input."""
    async def _test():
        vax = MockVaxSerialDevice()
        session_id = "test_ser_input"
        try:
            await serial_close_session(session_id)

            # Login
            await serial_client_tool(
                port=vax.port,
                commands=["SYSTEM", "sysvax60"],
                serial_session_id=session_id,
                log_dir=temp_log_dir,
                max_wait_seconds=5.0,
            )

            # Prompt
            await serial_client_tool(
                port=vax.port,
                commands=["INTERACTIVE_PROMPT"],
                serial_session_id=session_id,
                max_wait_seconds=2.0,
                prompt_pattern=r"\[NO\]: ",
            )

            # Answer
            res_input = await serial_send_input(
                session_id=session_id,
                input_text="YES",
                max_wait_seconds=2.0,
                prompt_pattern=r"(?m)^\$ ",
            )
            assert res_input.command_completed is True
            assert "Action confirmed" in res_input.response

            await serial_close_session(session_id)
        finally:
            vax.stop()

    asyncio.run(_test())


def test_serial_speed_switch(temp_log_dir):
    """Test dynamic baud rate reconfiguration on active serial session."""
    async def _test():
        vax = MockVaxSerialDevice()
        session_id = "test_ser_speed"
        try:
            await serial_close_session(session_id)

            # Connect at 9600 baud
            res = await serial_client_tool(
                port=vax.port,
                baudrate=9600,
                commands=["SYSTEM", "sysvax60"],
                serial_session_id=session_id,
                log_dir=temp_log_dir,
                max_wait_seconds=5.0,
            )
            assert res.command_completed is True

            # Switch baudrate to 19200
            speed_res = await serial_set_speed(
                session_id=session_id,
                baudrate=19200,
            )
            assert speed_res.success is True
            assert speed_res.old_baudrate == 9600
            assert speed_res.new_baudrate == 19200

            # Communicate at new speed
            input_res = await serial_send_input(
                session_id=session_id,
                input_text="SPEED_TEST",
                max_wait_seconds=2.0,
                prompt_pattern=r"(?m)^\$ ",
            )
            assert input_res.command_completed is True
            assert "Baud rate changed successfully" in input_res.response

            await serial_close_session(session_id)
        finally:
            vax.stop()

    asyncio.run(_test())


def test_serial_send_break(temp_log_dir):
    """Test RS-232 Break condition generation and console prompt detection."""
    async def _test():
        vax = MockVaxSerialDevice()
        session_id = "test_ser_break"
        try:
            await serial_close_session(session_id)

            # Connect
            await serial_client_tool(
                port=vax.port,
                commands=["SYSTEM", "sysvax60"],
                serial_session_id=session_id,
                log_dir=temp_log_dir,
                max_wait_seconds=5.0,
            )

            # Send break and simulate console prompt emission
            async def _send_console_prompt():
                await asyncio.sleep(0.05)
                vax.send_raw(b"\r\n?02 EXT HLT\r\n  PC= 80092B40\r\n>>> ")

            break_task = asyncio.create_task(_send_console_prompt())
            break_res = await serial_send_break(
                session_id=session_id,
                duration=0.1,
                max_wait_seconds=2.0,
                prompt_pattern=r">>> ",
            )
            await break_task

            assert break_res.success is True
            assert ">>> " in (break_res.prompt_matched or "")

            await serial_close_session(session_id)
        finally:
            vax.stop()

    asyncio.run(_test())


def test_serial_list_ports():
    """Test serial port discovery."""
    async def _test():
        ports_res = await serial_list_ports()
        assert isinstance(ports_res.ports, list)
        assert ports_res.total_ports == len(ports_res.ports)

    asyncio.run(_test())


def test_list_sessions_polymorphic(temp_log_dir):
    """Test listing both active Telnet and Serial sessions simultaneously."""
    async def _test():
        telnet_srv = MockTelnetServer()
        await telnet_srv.start()
        vax_ser = MockVaxSerialDevice()

        telnet_sid = "poly_telnet_session"
        serial_sid = "poly_serial_session"
        try:
            await telnet_close_session(telnet_sid)
            await serial_close_session(serial_sid)

            # Connect Telnet
            await telnet_client_tool(
                host="127.0.0.1",
                port=telnet_srv.port,
                commands=["SYSTEM", "sysvax60"],
                telnet_session_id=telnet_sid,
                log_dir=temp_log_dir,
            )

            # Connect Serial
            await serial_client_tool(
                port=vax_ser.port,
                commands=["SYSTEM", "sysvax60"],
                serial_session_id=serial_sid,
                log_dir=temp_log_dir,
            )

            # Call unified list_sessions
            sessions_res = await list_sessions()
            assert telnet_sid in sessions_res.sessions
            assert serial_sid in sessions_res.sessions

            t_info = sessions_res.sessions[telnet_sid]
            assert t_info.session_type == "telnet"
            assert t_info.host == "127.0.0.1"

            s_info = sessions_res.sessions[serial_sid]
            assert s_info.session_type == "serial"
            assert s_info.port == vax_ser.port

            await telnet_close_session(telnet_sid)
            await serial_close_session(serial_sid)
        finally:
            await telnet_srv.stop()
            vax_ser.stop()

    asyncio.run(_test())


def test_serial_no_overwrite_log(temp_log_dir):
    """Test that reconnecting with an existing serial session ID increments filename and does not overwrite."""
    async def _test():
        vax = MockVaxSerialDevice()
        session_id = "no_overwrite_ser"
        try:
            await serial_close_session(session_id)

            # Session 1
            res1 = await serial_client_tool(
                port=vax.port,
                commands=["SYSTEM", "sysvax60"],
                serial_session_id=session_id,
                log_dir=temp_log_dir,
                close_session=True,
            )
            log_file1 = res1.log_file

            # Session 2
            res2 = await serial_client_tool(
                port=vax.port,
                commands=["SYSTEM", "sysvax60"],
                serial_session_id=session_id,
                log_dir=temp_log_dir,
                close_session=True,
            )
            log_file2 = res2.log_file

            assert log_file1 != log_file2
            assert os.path.exists(log_file1)
            assert os.path.exists(log_file2)
            assert log_file2.endswith("_1.log")
        finally:
            vax.stop()

    asyncio.run(_test())
