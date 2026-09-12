"""
Tests for TerminalDefaults.PROMPT_PATTERN itself (the DEFAULT prompt regex),
as opposed to the existing standalone tests, which all pass an explicit
`prompt_pattern=r"(?m)^\\$ "` override and so never actually exercise the
default.

Motivation: OpenVMS sites commonly customize DCL's prompt via SYLOGIN.COM to
"NODE::USER$ " instead of a bare "$ ", and DCL's RUN/subsystem utilities
(TCPIP>, MCL>, NCP>, AUTHORIZE>, SET HOST 0>, ANALYZE/SYSTEM>...) use an
entirely different ">"-terminated prompt shape. The original default
(`r"(?m)(^\\$ |Username: |Password: |>>> )"`) matched neither, which meant
every telnet_client_tool()/serial_client_tool() call against a real,
customized VMS system relying on the default silently failed to detect
command completion. See PLAN_DEFAULT_PROMPT_PATTERN.md for the full
diagnosis and design rationale.
"""

import asyncio
import shutil
import tempfile

import pytest

from chuk_mcp_telnet_client.tools import TerminalDefaults, telnet_client_tool


# ============================================================================
# Part 1: Pure regex unit tests - fast, no sockets, exhaustive edge cases.
# ============================================================================

DEFAULT = TerminalDefaults.PROMPT_PATTERN


@pytest.mark.parametrize(
    "buffer,expected_match",
    [
        # --- must match ---
        ("$ ", "$ "),
        ("VAX96::USER1$ ", "$ "),
        ("VAX96::USER1$", "$"),
        ("SOMEOTHERNODE::ADMIN$ ", "$ "),
        ("TCPIP> ", "TCPIP> "),
        ("TCPIP>", "TCPIP>"),
        ("MCL> ", "MCL> "),
        ("NCP> ", "NCP> "),
        ("AUTHORIZE> ", "AUTHORIZE> "),
        ("SET HOST 0> ", "SET HOST 0> "),
        ("ANALYZE/SYSTEM> ", "ANALYZE/SYSTEM> "),
        (">>> ", ">>> "),
        ("\r\n\rUsername: ", "Username: "),
        ("\r\n\rPassword: ", "Password: "),
        # prompt is not the first line of a multi-line accumulated buffer -
        # proves the (?m:...) scoping still finds it mid-buffer
        ("some output\r\nmore output\r\nVAX96::USER1$ ", "$ "),
        ("Welcome to OpenVMS\r\nLast login: today\r\nTCPIP> ", "TCPIP> "),
    ],
)
def test_default_pattern_matches(buffer, expected_match):
    m = __import__("re").search(DEFAULT, buffer)
    assert m is not None, f"Expected default PROMPT_PATTERN to match {buffer!r}"
    assert m.group(0) == expected_match


@pytest.mark.parametrize(
    "buffer",
    [
        # A "$" appears mid-buffer (e.g. a DCL symbol value shown in output),
        # but it is NOT the true end of the buffer and more output follows -
        # the un-scoped `\$ ?$` alternative must NOT fire here.
        'SHOW SYMBOL X == "$ FOO"\r\nmore output still streaming in',
        # A word ending in "> " appears mid-line but there's trailing text
        # after it on the same line - not a real prompt.
        "TCPIP> some extra text that keeps going",
        # Ordinary output with no prompt-like tail at all.
        "Compiling SAVAGE.C...\r\nLinking SAVAGE.EXE...",
    ],
)
def test_default_pattern_does_not_false_positive(buffer):
    m = __import__("re").search(DEFAULT, buffer)
    assert m is None, f"Expected default PROMPT_PATTERN to NOT match {buffer!r}, got {m}"


# ============================================================================
# Part 2: End-to-end integration test against a mock server emitting a
# CUSTOMIZED (node::user-prefixed) DCL prompt, using telnet_client_tool()
# with NO explicit prompt_pattern override - i.e. exercising the default
# exactly as a real caller against a real VMS site would.
# ============================================================================


class MockCustomizedVmsServer:
    """Mock server simulating a SYLOGIN.COM-customized OpenVMS DCL prompt
    ("VAX96::USER1$ ", not a bare "$ ") plus a nested TCPIP> subsystem, to
    prove the new default handles both prompt shapes without an override."""

    def __init__(self):
        self.server = None
        self.port = 0
        self.active_writers = set()

    async def handle_client(self, reader, writer):
        self.active_writers.add(writer)
        try:
            writer.write(b"\r\n\rUsername: ")
            await writer.drain()
            while not reader.at_eof():
                line = await reader.readline()
                if not line:
                    break
                cmd = line.decode("utf-8", errors="ignore").strip()

                if cmd == "USER1":
                    writer.write(b"\r\n\rPassword: ")
                    await writer.drain()
                elif cmd == "user1pass":
                    writer.write(b"\r\n Welcome to OpenVMS (TM) VAX Operating System\r\nVAX96::USER1$ ")
                    await writer.drain()
                elif cmd == "SHOW TIME":
                    writer.write(b"\r\n   4-SEP-2026 23:30:00\r\nVAX96::USER1$ ")
                    await writer.drain()
                elif cmd == "RUN SYS$SYSTEM:TCPIP$TELNET":
                    writer.write(b"\r\nTCPIP> ")
                    await writer.drain()
                elif cmd == "SHOW INTERFACE":
                    writer.write(b"\r\nSE0    UP\r\nTCPIP> ")
                    await writer.drain()
                elif cmd == "EXIT":
                    writer.write(b"\r\nVAX96::USER1$ ")
                    await writer.drain()
                elif cmd == "LOGOUT":
                    writer.write(b"\r\n  USER1 logged out\r\n")
                    await writer.drain()
                    break
                else:
                    writer.write(f"\r\n%DCL-I-EXECUTED, {cmd}\r\nVAX96::USER1$ ".encode("utf-8"))
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
    d = tempfile.mkdtemp(prefix="prompt_pattern_test_logs_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_telnet_client_tool_default_prompt_handles_customized_vms_prompt(temp_log_dir):
    """The regression this whole file exists for: a full login + multi-command
    batch against a customized "NODE::USER$ " DCL prompt and a nested "TCPIP>"
    subsystem prompt, using telnet_client_tool() with its DEFAULT
    prompt_pattern (no override at all) - must complete every command, not
    just the login step."""

    async def _test():
        mock = MockCustomizedVmsServer()
        await mock.start()
        try:
            result = await telnet_client_tool(
                host="127.0.0.1",
                port=mock.port,
                commands=[
                    "USER1",
                    "user1pass",
                    "SHOW TIME",
                    "RUN SYS$SYSTEM:TCPIP$TELNET",
                    "SHOW INTERFACE",
                    "EXIT",
                    "LOGOUT",
                ],
                # deliberately NOT passing prompt_pattern - exercise the default
                max_wait_seconds=5.0,
                log_dir=temp_log_dir,
                close_session=True,
            )

            assert result.command_completed, (
                f"Expected ALL commands to complete using the default prompt_pattern, "
                f"but command_completed=False. Responses so far: {result.responses}"
            )
            assert len(result.responses) == 7, (
                f"Expected all 7 queued commands to have run, got {len(result.responses)}: "
                f"{[r.command for r in result.responses]}"
            )

            show_time_resp = result.responses[2]
            assert show_time_resp.command == "SHOW TIME"
            assert "23:30:00" in show_time_resp.response

            show_iface_resp = result.responses[4]
            assert show_iface_resp.command == "SHOW INTERFACE"
            assert "SE0    UP" in show_iface_resp.response
        finally:
            await mock.stop()

    asyncio.run(_test())
