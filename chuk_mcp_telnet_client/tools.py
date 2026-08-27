"""
Telnet client tools for MCP server.

Provides async telnet connectivity with background session stream reading,
long-running process polling, and persistent disk logging.
"""

import asyncio
import logging
import re
import telnetlib  # nosec B401 - telnet is the purpose of this MCP server
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from chuk_mcp_server import tool
from pydantic import BaseModel, ValidationError

from chuk_mcp_telnet_client.logger import TelnetSessionLogger
from chuk_mcp_telnet_client.models import (
    CommandResponse,
    TelnetClientInput,
    TelnetClientOutput,
    TelnetReadSessionOutput,
    TelnetSendInputOutput,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Constants
# ============================================================================


class TelnetCommand(bytes, Enum):
    """Telnet protocol command constants."""

    IAC = bytes([255])  # Interpret As Command
    DONT = bytes([254])  # Don't perform option
    DO = bytes([253])  # Do perform option
    WONT = bytes([252])  # Won't perform option
    WILL = bytes([251])  # Will perform option


class TelnetDefaults:
    """Default values for telnet operations."""

    CONNECTION_TIMEOUT: int = 10
    INITIAL_BANNER_WAIT: float = 2.0
    COMMAND_DELAY: float = 1.0
    RESPONSE_WAIT: float = 1.5
    READ_TIMEOUT: int = 5
    MAX_WAIT_SECONDS: float = 20.0
    PROMPT_PATTERN: str = r"(?m)(^\$ |Username: |Password: )"


# ============================================================================
# Session Models & Dataclasses
# ============================================================================


@dataclass
class TelnetSession:
    """Represents an active telnet connection session with background stream buffering."""

    telnet: telnetlib.Telnet
    host: str
    port: int
    created_at: float
    session_id: str
    logger: Optional[TelnetSessionLogger] = None
    accumulated_output: str = ""
    unconsumed_output: str = ""
    is_active: bool = True
    last_activity: float = field(default_factory=time.time)
    current_cmd_start_index: int = 0
    reader_task: Optional[asyncio.Task] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionInfo(BaseModel):
    """Information about a telnet session."""

    session_id: str
    host: str
    port: int
    created_at: float
    age_seconds: float
    is_active: bool
    total_bytes_received: int


class SessionListResponse(BaseModel):
    """Response for listing active sessions."""

    active_sessions: int
    sessions: dict[str, SessionInfo]
    note: str = (
        "Active connections shown. Sessions persist and stream output continuously in the background."
    )


class SessionCloseResponse(BaseModel):
    """Response for closing a session."""

    success: bool
    message: str


# ============================================================================
# Session Storage & Background Reader
# ============================================================================


async def _background_reader_loop(session: TelnetSession) -> None:
    """
    Continuously reads raw bytes from telnet socket in background and buffers them.
    Ensures no data is lost while the agent processes intermediate responses.
    """
    try:
        while session.is_active:
            try:
                # Read all available bytes without blocking
                data = await asyncio.to_thread(session.telnet.read_very_eager)
            except (OSError, EOFError):
                logger.info(f"Connection closed for session {session.session_id}")
                session.is_active = False
                break
            except Exception as e:
                logger.warning(f"Error reading socket in session {session.session_id}: {e}")
                await asyncio.sleep(0.1)
                continue

            if data:
                text = data.decode("utf-8", errors="ignore")
                async with session.lock:
                    session.accumulated_output += text
                    session.unconsumed_output += text
                    session.last_activity = time.time()

                if session.logger:
                    session.logger.log_stream_chunk(
                        session_id=session.session_id,
                        host=session.host,
                        port=session.port,
                        chunk=text,
                    )
            await asyncio.sleep(0.05)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Background reader loop crashed for {session.session_id}: {e}")


class SessionStore:
    """In-memory storage for active telnet connections and background reader tasks."""

    def __init__(self):
        self._sessions: dict[str, TelnetSession] = {}

    async def store(self, session: TelnetSession) -> None:
        """Store a telnet session and launch background stream reader."""
        self._sessions[session.session_id] = session
        if session.reader_task is None or session.reader_task.done():
            session.reader_task = asyncio.create_task(_background_reader_loop(session))

    async def get(self, session_id: str) -> Optional[TelnetSession]:
        """Retrieve a telnet session."""
        return self._sessions.get(session_id)

    async def delete(self, session_id: str) -> None:
        """Delete a telnet session, stop background reader, and close connection."""
        session = self._sessions.get(session_id)
        if session:
            session.is_active = False
            if session.reader_task and not session.reader_task.done():
                session.reader_task.cancel()
                try:
                    await session.reader_task
                except asyncio.CancelledError:
                    pass
            try:
                await asyncio.to_thread(session.telnet.close)
            except Exception as e:
                logger.warning(f"Error closing telnet connection: {e}")
            del self._sessions[session_id]

    async def list_all(self) -> list[TelnetSession]:
        """List all active sessions."""
        return list(self._sessions.values())


# Global session store instance
_session_store = SessionStore()


# ============================================================================
# Telnet Operations
# ============================================================================


def _create_negotiation_callback():
    """Create a telnet option negotiation callback."""

    def callback(sock, cmd, opt):
        """Handle telnet option negotiation."""
        if cmd == TelnetCommand.DO.value:
            sock.sendall(TelnetCommand.IAC.value + TelnetCommand.WONT.value + opt)
        elif cmd == TelnetCommand.WILL.value:
            sock.sendall(TelnetCommand.IAC.value + TelnetCommand.DONT.value + opt)

    return callback


async def _connect_telnet(host: str, port: int) -> tuple[telnetlib.Telnet, str]:
    """Establish a new telnet connection."""
    tn = telnetlib.Telnet()  # nosec B312 - telnet is the purpose of this MCP server
    tn.set_option_negotiation_callback(_create_negotiation_callback())

    try:
        await asyncio.to_thread(tn.open, host, port, TelnetDefaults.CONNECTION_TIMEOUT)
    except Exception as ex:
        raise RuntimeError(f"Failed to connect to Telnet server at {host}:{port}: {ex}")

    # Read initial banner
    await asyncio.sleep(TelnetDefaults.INITIAL_BANNER_WAIT)
    initial_data = await asyncio.to_thread(tn.read_very_eager)
    if not initial_data:
        initial_data = await asyncio.to_thread(tn.read_some)

    initial_banner = initial_data.decode("utf-8", errors="ignore")
    return tn, initial_banner


def _strip_command_echo(response: str, command: str) -> str:
    """Remove command echo from telnet response."""
    variants = [
        command,
        command + "\r\n",
        command + "\n",
        "\r\n" + command,
        "\n" + command,
    ]
    for variant in variants:
        if response.startswith(variant):
            return response[len(variant) :]
    for variant in variants:
        if variant in response:
            parts = response.split(variant, 1)
            if len(parts) > 1:
                return parts[0] + parts[1]
    return response


async def _wait_for_output(
    session: TelnetSession,
    start_index: int = 0,
    max_wait_seconds: float = TelnetDefaults.MAX_WAIT_SECONDS,
    prompt_pattern: Optional[str] = None,
    idle_timeout: Optional[float] = None,
) -> tuple[str, bool, Optional[str]]:
    """
    Wait for output to arrive or prompt pattern to match in session buffer.

    Returns:
        tuple of (new_output_text, command_completed, matched_prompt_string)
    """
    start_time = time.time()
    compiled_pattern = re.compile(prompt_pattern) if prompt_pattern else None

    while time.time() - start_time < max_wait_seconds:
        if not session.is_active:
            async with session.lock:
                out = session.unconsumed_output
                session.unconsumed_output = ""
                return out, True, None

        async with session.lock:
            # Check if prompt pattern matches in output since start_index
            new_text = session.accumulated_output[start_index:]
            if compiled_pattern and compiled_pattern.search(new_text):
                m = compiled_pattern.search(new_text)
                matched = m.group(0) if m else None
                out = session.unconsumed_output
                session.unconsumed_output = ""
                return out, True, matched

            # Check idle timeout if specified
            if (
                idle_timeout
                and (time.time() - session.last_activity) >= idle_timeout
                and len(session.unconsumed_output) > 0
            ):
                out = session.unconsumed_output
                session.unconsumed_output = ""
                return out, False, None

        await asyncio.sleep(0.08)

    # Reached max_wait_seconds
    async with session.lock:
        new_text = session.accumulated_output[start_index:]
        matched = None
        completed = False
        if compiled_pattern and compiled_pattern.search(new_text):
            m = compiled_pattern.search(new_text)
            matched = m.group(0) if m else None
            completed = True

        out = session.unconsumed_output
        session.unconsumed_output = ""
        return out, completed, matched


async def _execute_command(
    session: TelnetSession,
    command: str,
    command_delay: float,
    response_wait: float,
    strip_echo: bool,
    raw_input: bool = False,
    prompt_pattern: Optional[str] = None,
) -> tuple[CommandResponse, bool, Optional[str]]:
    """Execute a single command on the session and collect initial response."""
    async with session.lock:
        start_index = len(session.accumulated_output)
        session.current_cmd_start_index = start_index
        session.unconsumed_output = ""

    if raw_input:
        cmd_bytes = command.encode("utf-8")
    else:
        cmd_bytes = command.encode("utf-8") + b"\r\n"

    await asyncio.to_thread(session.telnet.write, cmd_bytes)
    await asyncio.sleep(command_delay)

    out, completed, matched = await _wait_for_output(
        session=session,
        start_index=start_index,
        max_wait_seconds=response_wait,
        prompt_pattern=prompt_pattern,
    )

    if strip_echo:
        out = _strip_command_echo(out, command)

    return CommandResponse(command=command, response=out), completed, matched


# ============================================================================
# MCP Tools
# ============================================================================


@tool(
    name="telnet_client",
    description="Connect to a Telnet server or reuse an existing session, run commands sequentially, and return output. Supports background streaming, non-blocking execution with custom wait windows, prompt matching, and permanent disk logging with ISO-8601 timestamps.",
)
async def telnet_client_tool(
    host: str,
    port: int,
    commands: list[str],
    telnet_session_id: Optional[str] = None,
    close_session: bool = False,
    max_wait_seconds: float = TelnetDefaults.MAX_WAIT_SECONDS,
    prompt_pattern: Optional[str] = TelnetDefaults.PROMPT_PATTERN,
    command_delay: float = TelnetDefaults.COMMAND_DELAY,
    response_wait: float = TelnetDefaults.RESPONSE_WAIT,
    strip_command_echo: bool = True,
    raw_input: bool = False,
    log_dir: Optional[str] = None,
    enable_logging: bool = True,
) -> TelnetClientOutput:
    """
    Universal Telnet client tool with session persistence and long-running process support.

    Args:
        host: Host or IP to connect to
        port: Port number
        commands: List of commands to send
        telnet_session_id: Session ID for reusing an existing connection
        close_session: Whether to close the session after commands finish
        max_wait_seconds: Max seconds to wait for command output in this call (default: 20s)
        prompt_pattern: Regex pattern signaling command completion (default: r"(?m)(^\\$ |Username: |Password: )")
        command_delay: Delay in seconds after sending each command (default: 1.0s)
        response_wait: Time to wait for initial responses (default: 1.5s)
        strip_command_echo: Try to remove command echo from responses (default: True)
        raw_input: Send input as raw bytes without CRLF line endings
        log_dir: Optional directory to store session log files
        enable_logging: Whether to log session to disk (default: True)

    Returns:
        TelnetClientOutput with responses, completion status, and session handle
    """
    try:
        validated_input = TelnetClientInput(host=host, port=port, commands=commands)
    except ValidationError as e:
        raise ValueError(f"Invalid input for telnet_client_tool: {e}")

    if not telnet_session_id:
        telnet_session_id = f"telnet_{host}_{port}_{int(time.time())}"

    session_logger = TelnetSessionLogger(log_dir=log_dir) if enable_logging else None

    # Retrieve or create session
    session = await _session_store.get(telnet_session_id)
    initial_banner = ""

    if session:
        if not session.is_active:
            await _session_store.delete(telnet_session_id)
            session = None

    if not session:
        tn, initial_banner = await _connect_telnet(
            validated_input.host, validated_input.port
        )
        session = TelnetSession(
            telnet=tn,
            host=validated_input.host,
            port=validated_input.port,
            created_at=time.time(),
            session_id=telnet_session_id,
            logger=session_logger,
            accumulated_output=initial_banner,
            unconsumed_output=initial_banner,
        )
        await _session_store.store(session)

        if session_logger:
            session_logger.log_session_start(
                session_id=telnet_session_id,
                host=validated_input.host,
                port=validated_input.port,
                banner=initial_banner,
            )

    # Execute commands
    responses: list[CommandResponse] = []
    final_completed = True
    final_matched_prompt = None

    for i, cmd in enumerate(validated_input.commands):
        is_last = i == len(validated_input.commands) - 1
        wait_time = max_wait_seconds if is_last else response_wait
        pat = prompt_pattern if is_last else None

        try:
            resp, completed, matched = await _execute_command(
                session=session,
                command=cmd,
                command_delay=command_delay,
                response_wait=wait_time,
                strip_echo=strip_command_echo,
                raw_input=raw_input,
                prompt_pattern=pat,
            )
            responses.append(resp)

            if session_logger:
                session_logger.log_command(
                    session_id=telnet_session_id,
                    host=validated_input.host,
                    port=validated_input.port,
                    command=cmd,
                    response=resp.response,
                )

            if is_last:
                final_completed = completed
                final_matched_prompt = matched

        except (OSError, EOFError) as e:
            close_resp = CommandResponse(command=cmd, response="(connection closed by server)")
            responses.append(close_resp)
            final_completed = True
            session.is_active = False
            if session_logger:
                session_logger.log_session_end(
                    session_id=telnet_session_id,
                    host=validated_input.host,
                    port=validated_input.port,
                    reason=f"Server closed connection ({e})",
                )
            break

    if close_session:
        await _session_store.delete(telnet_session_id)
        if session_logger:
            session_logger.log_session_end(
                session_id=telnet_session_id,
                host=validated_input.host,
                port=validated_input.port,
                reason="Explicit close_session request",
            )

    session_active = await _session_store.get(telnet_session_id) is not None
    log_file_path = (
        session_logger.get_session_log_path(
            session_id=telnet_session_id,
            host=validated_input.host,
            port=validated_input.port,
        )
        if session_logger
        else None
    )

    return TelnetClientOutput(
        host=validated_input.host,
        port=validated_input.port,
        initial_banner=initial_banner,
        responses=responses,
        session_id=telnet_session_id,
        session_active=session_active,
        command_completed=final_completed,
        prompt_matched=final_matched_prompt,
        log_file=log_file_path,
    )


@tool(
    name="telnet_read_session",
    description="Wait for and read ongoing output from an active Telnet session without sending new commands. Ideal for polling long-running builds, processes, or command execution without hitting MCP client timeouts.",
)
async def telnet_read_session(
    session_id: str,
    max_wait_seconds: float = TelnetDefaults.MAX_WAIT_SECONDS,
    prompt_pattern: Optional[str] = TelnetDefaults.PROMPT_PATTERN,
    idle_timeout: Optional[float] = None,
    return_full_buffer: bool = False,
) -> TelnetReadSessionOutput:
    """
    Read ongoing stream output from an active telnet session.

    Args:
        session_id: Active session identifier
        max_wait_seconds: Maximum time in seconds to wait for new output (default: 20s)
        prompt_pattern: Regex pattern indicating command completion (default: r"(?m)(^\\$ |Username: |Password: )")
        idle_timeout: Optional timeout if stream becomes idle for X seconds
        return_full_buffer: If True, returns full accumulated history; otherwise returns newly received output

    Returns:
        TelnetReadSessionOutput with output text, byte counts, and completion status
    """
    session = await _session_store.get(session_id)
    if not session:
        raise ValueError(f"No active session found with ID '{session_id}'")

    new_output, completed, matched_prompt = await _wait_for_output(
        session=session,
        start_index=session.current_cmd_start_index,
        max_wait_seconds=max_wait_seconds,
        prompt_pattern=prompt_pattern,
        idle_timeout=idle_timeout,
    )

    output_to_return = session.accumulated_output if return_full_buffer else new_output
    log_file_path = (
        session.logger.get_session_log_path(
            session_id=session.session_id, host=session.host, port=session.port
        )
        if session.logger
        else None
    )

    return TelnetReadSessionOutput(
        session_id=session.session_id,
        host=session.host,
        port=session.port,
        output=output_to_return,
        new_bytes_count=len(new_output.encode("utf-8")),
        total_bytes_received=len(session.accumulated_output.encode("utf-8")),
        command_completed=completed,
        prompt_matched=matched_prompt,
        session_active=session.is_active,
        log_file=log_file_path,
    )


@tool(
    name="telnet_send_input",
    description="Send interactive input (e.g. answering prompts like [YES/NO] or passwords) to an active Telnet session and wait for a response.",
)
async def telnet_send_input(
    session_id: str,
    input_text: str,
    raw: bool = False,
    max_wait_seconds: float = 5.0,
    prompt_pattern: Optional[str] = TelnetDefaults.PROMPT_PATTERN,
) -> TelnetSendInputOutput:
    """
    Send interactive text or keystrokes to an active session.

    Args:
        session_id: Active session identifier
        input_text: Text string to send
        raw: If True, send raw bytes without trailing CRLF (for single key commands)
        max_wait_seconds: Time to wait for response (default: 5.0s)
        prompt_pattern: Regex pattern indicating completion

    Returns:
        TelnetSendInputOutput with response and session state
    """
    session = await _session_store.get(session_id)
    if not session:
        raise ValueError(f"No active session found with ID '{session_id}'")

    if not session.is_active:
        raise RuntimeError(f"Session '{session_id}' connection is closed")

    async with session.lock:
        start_index = len(session.accumulated_output)
        session.current_cmd_start_index = start_index
        session.unconsumed_output = ""

    if raw:
        payload = input_text.encode("utf-8")
    else:
        payload = input_text.encode("utf-8") + b"\r\n"

    await asyncio.to_thread(session.telnet.write, payload)
    if session.logger:
        session.logger.log_input(session.session_id, session.host, session.port, input_text)

    new_output, completed, matched_prompt = await _wait_for_output(
        session=session,
        start_index=start_index,
        max_wait_seconds=max_wait_seconds,
        prompt_pattern=prompt_pattern,
    )

    log_file_path = (
        session.logger.get_session_log_path(
            session_id=session.session_id, host=session.host, port=session.port
        )
        if session.logger
        else None
    )

    return TelnetSendInputOutput(
        session_id=session.session_id,
        input_sent=input_text,
        response=new_output,
        command_completed=completed,
        prompt_matched=matched_prompt,
        session_active=session.is_active,
        log_file=log_file_path,
    )


@tool(name="telnet_close_session", description="Close a specific Telnet session.")
async def telnet_close_session(session_id: str) -> SessionCloseResponse:
    """Close a specific Telnet session."""
    session = await _session_store.get(session_id)
    if session and session.logger:
        session.logger.log_session_end(
            session_id=session_id,
            host=session.host,
            port=session.port,
            reason="Closed via telnet_close_session",
        )
    await _session_store.delete(session_id)
    return SessionCloseResponse(
        success=True, message=f"Session {session_id} closed successfully"
    )


@tool(
    name="telnet_list_sessions",
    description="List all active Telnet sessions with connection details.",
)
async def telnet_list_sessions() -> SessionListResponse:
    """List all active Telnet sessions."""
    sessions = await _session_store.list_all()
    current_time = time.time()

    session_info_dict: dict[str, SessionInfo] = {}
    for session in sessions:
        session_info_dict[session.session_id] = SessionInfo(
            session_id=session.session_id,
            host=session.host,
            port=session.port,
            created_at=session.created_at,
            age_seconds=current_time - session.created_at,
            is_active=session.is_active,
            total_bytes_received=len(session.accumulated_output.encode("utf-8")),
        )

    return SessionListResponse(
        active_sessions=len(session_info_dict), sessions=session_info_dict
    )
