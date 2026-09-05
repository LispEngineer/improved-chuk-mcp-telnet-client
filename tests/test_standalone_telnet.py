"""
Standalone offline tests for Telnet MCP tools.
Uses an in-process mock Telnet server to test without physical VAX hardware.
"""

import asyncio
import os
import shutil
import tempfile
import pytest

from chuk_mcp_telnet_client.tools import (
    telnet_client_tool,
    telnet_read_session,
    telnet_send_input,
    telnet_close_session,
    list_sessions,
)


class MockTelnetServer:
    """Mock Telnet server simulating OpenVMS login and DCL sessions."""

    def __init__(self):
        self.server = None
        self.port = 0
        self.active_writers = set()

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.active_writers.add(writer)
        try:
            # Emulate DEC OpenVMS welcome banner
            writer.write(b"\r\n Welcome to OpenVMS (TM) VAX Operating System, Version V7.3\r\n\r\nUsername: ")
            await writer.drain()

            while not reader.at_eof():
                line = await reader.readline()
                if not line:
                    break
                cmd = line.decode("utf-8", errors="ignore").strip()

                if cmd == "SYSTEM":
                    writer.write(b"\r\nPassword: ")
                    await writer.drain()
                elif cmd == "sysvax60":
                    writer.write(b"\r\n$ ")
                    await writer.drain()
                elif cmd == "SHOW TIME":
                    writer.write(b"\r\n   4-SEP-2026 23:30:00\r\n$ ")
                    await writer.drain()
                elif cmd == "STREAM_JOB":
                    # Stream several lines with small delays
                    for i in range(1, 4):
                        await asyncio.sleep(0.1)
                        writer.write(f"\r\nStage {i} processing...".encode("utf-8"))
                        await writer.drain()
                    await asyncio.sleep(0.1)
                    writer.write(b"\r\n$ ")
                    await writer.drain()
                elif cmd == "INTERACTIVE_PROMPT":
                    writer.write(b"\r\nAre you sure you want to proceed? [NO]: ")
                    await writer.drain()
                elif cmd == "YES":
                    writer.write(b"\r\nProceeding with task...\r\n$ ")
                    await writer.drain()
                elif cmd == "LOGOUT":
                    writer.write(b"\r\n  SYSTEM logged out at 4-SEP-2026 23:30:05\r\n")
                    await writer.drain()
                    break
                else:
                    writer.write(f"\r\n%DCL-I-EXECUTED, {cmd}\r\n$ ".encode("utf-8"))
                    await writer.drain()
        finally:
            self.active_writers.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def start(self):
        self.server = await asyncio.start_server(self.handle_client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for w in list(self.active_writers):
            try:
                w.transport.close()
            except Exception:
                pass


@pytest.fixture
def temp_log_dir():
    d = tempfile.mkdtemp(prefix="telnet_test_logs_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_telnet_connect_and_login(temp_log_dir):
    """Test standard sequential login and prompt completion."""
    async def _test():
        server = MockTelnetServer()
        await server.start()
        session_id = "test_login_session"
        try:
            await telnet_close_session(session_id)

            res = await telnet_client_tool(
                host="127.0.0.1",
                port=server.port,
                commands=["SYSTEM", "sysvax60", "SHOW TIME"],
                telnet_session_id=session_id,
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

            # Verify log content contains clean transcript
            with open(res.log_file, "r") as f:
                log_content = f.read()
            assert "TELNET SESSION LOG" in log_content
            assert "SHOW TIME" in log_content

            await telnet_close_session(session_id)
        finally:
            await server.stop()

    asyncio.run(_test())


def test_telnet_read_streaming_session(temp_log_dir):
    """Test long-running background reader polling via telnet_read_session."""
    async def _test():
        server = MockTelnetServer()
        await server.start()
        session_id = "test_stream_session"
        try:
            await telnet_close_session(session_id)

            # Step 1: Login
            await telnet_client_tool(
                host="127.0.0.1",
                port=server.port,
                commands=["SYSTEM", "sysvax60"],
                telnet_session_id=session_id,
                log_dir=temp_log_dir,
                max_wait_seconds=5.0,
            )

            # Step 2: Send streaming job with very short wait
            res1 = await telnet_client_tool(
                host="127.0.0.1",
                port=server.port,
                commands=["STREAM_JOB"],
                telnet_session_id=session_id,
                max_wait_seconds=0.05,  # Return early before STREAM_JOB finishes
            )

            assert res1.session_active is True
            assert res1.command_completed is False

            # Step 3: Now poll using telnet_read_session until prompt matches
            res2 = await telnet_read_session(
                session_id=session_id,
                max_wait_seconds=3.0,
                prompt_pattern=r"(?m)^\$ ",
            )

            assert res2.command_completed is True
            assert "Stage" in res2.output

            await telnet_close_session(session_id)
        finally:
            await server.stop()

    asyncio.run(_test())


def test_telnet_send_interactive_input(temp_log_dir):
    """Test interactive prompt answering via telnet_send_input."""
    async def _test():
        server = MockTelnetServer()
        await server.start()
        session_id = "test_input_session"
        try:
            await telnet_close_session(session_id)

            # Login
            await telnet_client_tool(
                host="127.0.0.1",
                port=server.port,
                commands=["SYSTEM", "sysvax60"],
                telnet_session_id=session_id,
                log_dir=temp_log_dir,
                max_wait_seconds=5.0,
            )

            # Trigger interactive prompt
            await telnet_client_tool(
                host="127.0.0.1",
                port=server.port,
                commands=["INTERACTIVE_PROMPT"],
                telnet_session_id=session_id,
                max_wait_seconds=2.0,
                prompt_pattern=r"\[NO\]: ",
            )

            # Answer YES
            res_input = await telnet_send_input(
                session_id=session_id,
                input_text="YES",
                max_wait_seconds=2.0,
                prompt_pattern=r"(?m)^\$ ",
            )

            assert res_input.command_completed is True
            assert "Proceeding with task" in res_input.response

            await telnet_close_session(session_id)
        finally:
            await server.stop()

    asyncio.run(_test())


def test_telnet_single_log_file_per_session(temp_log_dir):
    """Test that a single persistent log file is created and maintained per session across multiple operations and reconnects."""
    async def _test():
        server = MockTelnetServer()
        await server.start()
        session_id = "test_single_session"
        try:
            await telnet_close_session(session_id)

            # 1. Initial connection and commands
            res1 = await telnet_client_tool(
                host="127.0.0.1",
                port=server.port,
                commands=["SYSTEM", "sysvax60"],
                telnet_session_id=session_id,
                log_dir=temp_log_dir,
                max_wait_seconds=5.0,
            )
            assert res1.log_file == os.path.join(temp_log_dir, f"{session_id}.log")

            # 2. Subsequent command on existing active session
            res2 = await telnet_client_tool(
                host="127.0.0.1",
                port=server.port,
                commands=["SHOW TIME"],
                telnet_session_id=session_id,
                log_dir=temp_log_dir,
                max_wait_seconds=5.0,
            )
            assert res2.log_file == os.path.join(temp_log_dir, f"{session_id}.log")

            # 3. Interactive input on existing active session
            res3 = await telnet_send_input(
                session_id=session_id,
                input_text="YES",
                max_wait_seconds=2.0,
            )
            assert res3.log_file == os.path.join(temp_log_dir, f"{session_id}.log")

            # 4. Polling read on active session
            res4 = await telnet_read_session(
                session_id=session_id,
                max_wait_seconds=1.0,
            )
            assert res4.log_file == os.path.join(temp_log_dir, f"{session_id}.log")

            # 5. Close session
            await telnet_close_session(session_id)

            # Verify that EXACTLY 1 log file exists in directory and no suffix files were created
            log_files = sorted(os.listdir(temp_log_dir))
            assert log_files == [f"{session_id}.log"], f"Expected only {[f'{session_id}.log']}, got {log_files}"
            assert not any("_1.log" in f for f in log_files)

            # Verify log content includes all sequential operations
            log_path = os.path.join(temp_log_dir, f"{session_id}.log")
            with open(log_path, "r") as f:
                content = f.read()
            assert "TELNET SESSION LOG" in content
            assert "SHOW TIME" in content
            assert "YES" in content
            assert "SESSION ENDED (Closed)" in content

            # 6. Reconnect with same session ID: should append to existing file, NOT create _1.log
            res_reconnect = await telnet_client_tool(
                host="127.0.0.1",
                port=server.port,
                commands=["SYSTEM", "sysvax60"],
                telnet_session_id=session_id,
                log_dir=temp_log_dir,
                close_session=True,
            )
            assert res_reconnect.log_file == log_path

            # Verify STILL exactly one file exists on disk
            log_files_after = sorted(os.listdir(temp_log_dir))
            assert log_files_after == [f"{session_id}.log"], f"Expected only {[f'{session_id}.log']}, got {log_files_after}"
        finally:
            await server.stop()

    asyncio.run(_test())

