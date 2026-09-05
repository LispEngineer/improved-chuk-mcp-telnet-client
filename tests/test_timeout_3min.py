"""
3-Minute Timeout Immunity Test for MCP Terminal Client.
Verifies that a VAX operation running for > 3 minutes (> 180 seconds)
can be executed and monitored via non-blocking polling without triggering
the MCP 180-second (3 minute) tool execution timeout limit.
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
    serial_close_session,
)


class MockLongRunningVax:
    """Simulates a VAX running a long operation (e.g. BACKUP / IMAGE) over serial."""

    def __init__(self, duration_seconds: float = 185.0):
        self.duration_seconds = duration_seconds
        self.master_fd, self.slave_fd = pty.openpty()
        tty.setraw(self.master_fd)
        tty.setraw(self.slave_fd)
        self.port = os.ttyname(self.slave_fd)
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self):
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
            elif cmd == "BACKUP/IMAGE DKA0: BULKDATA.BCK":
                # Launch long-running simulated backup job in background
                threading.Thread(target=self._run_backup_job, daemon=True).start()
            elif cmd:
                os.write(self.master_fd, f"\r\n%DCL-I-EXECUTED, {cmd}\r\n$ ".encode("utf-8"))
        except OSError:
            pass

    def _run_backup_job(self):
        """Streams progress updates over duration_seconds, then returns prompt."""
        start_time = time.time()
        step_interval = 10.0  # emit progress every 10 seconds
        next_checkpoint = start_time + step_interval
        end_time = start_time + self.duration_seconds
        records = 0

        try:
            os.write(self.master_fd, b"\r\n%BACKUP-I-STARTING, full volume image backup initiated\r\n")
            while self.running and time.time() < end_time:
                time.sleep(1.0)
                now = time.time()
                if now >= next_checkpoint:
                    records += 10000
                    elapsed = int(now - start_time)
                    msg = f"\r\n%BACKUP-I-COPIED, {records} records copied (elapsed: {elapsed}s)...\r\n"
                    os.write(self.master_fd, msg.encode("utf-8"))
                    next_checkpoint = now + step_interval

            if self.running:
                os.write(self.master_fd, b"\r\n%BACKUP-S-COPIED, 185000 records copied, volume verified.\r\n$ ")
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
    d = tempfile.mkdtemp(prefix="timeout_test_logs_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_serial_operation_exceeding_3_minutes(temp_log_dir):
    """
    Test that an operation taking > 3 minutes (185 seconds):
    1. Never causes an MCP 180-second timeout error.
    2. Allows the agent to poll iteratively using read_session with short timeouts.
    3. Successfully detects completion at > 180 seconds.
    4. Accurately logs the entire multi-minute session to disk.
    """
    duration = float(os.environ.get("VAX_LONG_TEST_DURATION", "185.0"))

    async def _test():
        vax = MockLongRunningVax(duration_seconds=duration)
        session_id = "test_long_op_session"
        start_overall = time.time()

        try:
            await serial_close_session(session_id)

            # 1. Login
            login_res = await serial_client_tool(
                port=vax.port,
                commands=["SYSTEM", "sysvax60"],
                serial_session_id=session_id,
                log_dir=temp_log_dir,
                max_wait_seconds=5.0,
            )
            assert login_res.command_completed is True

            # 2. Launch long command with short wait to return early before job finishes
            # This returns with command_completed=False, keeping the agent alive
            launch_wait = min(5.0, max(0.5, duration / 3.0))
            job_start = time.time()
            launch_res = await serial_client_tool(
                port=vax.port,
                commands=["BACKUP/IMAGE DKA0: BULKDATA.BCK"],
                serial_session_id=session_id,
                max_wait_seconds=launch_wait,
            )
            assert launch_res.command_completed is False
            assert launch_res.session_active is True

            # 3. Iteratively poll using serial_read_session
            # Each individual call has max_wait_seconds=15.0, safely below the 180s MCP timeout
            all_streamed_chunks = []
            completed = False
            poll_count = 0

            while not completed and (time.time() - job_start < duration + 30.0):
                poll_count += 1
                poll_start = time.time()

                read_res = await serial_read_session(
                    session_id=session_id,
                    max_wait_seconds=15.0,
                    prompt_pattern=r"(?m)^\$ ",
                )

                poll_duration = time.time() - poll_start
                # Ensure each individual tool call returns well within the MCP 180s limit!
                assert poll_duration <= 20.0, f"Poll {poll_count} took {poll_duration}s, exceeding tool bound!"

                if read_res.output:
                    all_streamed_chunks.append(read_res.output)

                if read_res.command_completed:
                    completed = True
                    break

            total_elapsed = time.time() - job_start

            # Assertions verifying the > 3 minute run
            assert completed is True, "Long running command did not complete successfully"
            assert total_elapsed >= duration, f"Expected elapsed >= {duration}s, got {total_elapsed:.1f}s"
            assert poll_count >= int(duration / 15.0), f"Expected at least {int(duration / 15.0)} polls, got {poll_count}"

            combined_output = "".join(all_streamed_chunks)
            assert "%BACKUP-S-COPIED" in combined_output

            # Verify disk log integrity
            log_file = launch_res.log_file
            assert log_file is not None
            assert os.path.exists(log_file)
            with open(log_file, "r") as f:
                log_text = f.read()

            assert "BACKUP/IMAGE DKA0: BULKDATA.BCK" in log_text
            assert "%BACKUP-S-COPIED" in log_text
            assert "185000 records copied" in log_text

            await serial_close_session(session_id)
        finally:
            vax.stop()

    asyncio.run(_test())
