"""
Terminal client tools (Telnet & Serial) for MCP server.

Provides async Telnet and Serial connectivity with background stream buffering,
long-running process polling without timeouts, dynamic baud rate switching,
clean transcript disk logging without overwriting, and polymorphic multi-session management.
"""

import asyncio
import logging
import re
import telnetlib  # nosec B401 - telnet is part of this MCP server
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Union

import serial
import serial.tools.list_ports
from chuk_mcp_server import tool
from pydantic import BaseModel, ValidationError

from chuk_mcp_telnet_client.logger import SessionLogger
from chuk_mcp_telnet_client.models import (
    CommandResponse,
    SerialClientOutput,
    SerialListPortsOutput,
    SerialPortInfo,
    SerialReadSessionOutput,
    SerialSendBreakOutput,
    SerialSendInputOutput,
    SerialSetSpeedOutput,
    SessionCloseResponse,
    SessionInfo,
    SessionListResponse,
    TelnetClientOutput,
    TelnetReadSessionOutput,
    TelnetSendInputOutput,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Constants & Defaults
# ============================================================================


class TelnetCommand(bytes, Enum):
    """Telnet protocol command constants."""

    IAC = bytes([255])
    DONT = bytes([254])
    DO = bytes([253])
    WONT = bytes([252])
    WILL = bytes([251])


class TerminalDefaults:
    """Default values for terminal operations."""

    CONNECTION_TIMEOUT: int = 10
    INITIAL_BANNER_WAIT: float = 1.5
    COMMAND_DELAY: float = 0.5
    RESPONSE_WAIT: float = 1.0
    READ_TIMEOUT: int = 5
    MAX_WAIT_SECONDS: float = 20.0
    PROMPT_PATTERN: str = r"(?m)(^\$ |Username: |Password: |>>> )"


# ============================================================================
# Session Models & Dataclasses
# ============================================================================


@dataclass
class TelnetSession:
    """Active telnet session with background stream buffering."""

    telnet: telnetlib.Telnet
    host: str
    port: int
    created_at: float
    session_id: str
    logger: Optional[SessionLogger] = None
    accumulated_output: str = ""
    unconsumed_output: str = ""
    is_active: bool = True
    last_activity: float = field(default_factory=time.time)
    current_cmd_start_index: int = 0
    reader_task: Optional[asyncio.Task] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    total_bytes_received: int = 0

    @property
    def session_type(self) -> str:
        return "telnet"

    @property
    def target(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass
class SerialSession:
    """Active serial connection session with background stream buffering."""

    serial: serial.Serial
    port: str
    baudrate: int
    bytesize: int
    parity: str
    stopbits: float
    created_at: float
    session_id: str
    logger: Optional[SessionLogger] = None
    accumulated_output: str = ""
    unconsumed_output: str = ""
    is_active: bool = True
    last_activity: float = field(default_factory=time.time)
    current_cmd_start_index: int = 0
    reader_task: Optional[asyncio.Task] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    total_bytes_received: int = 0

    @property
    def session_type(self) -> str:
        return "serial"

    @property
    def target(self) -> str:
        sb = int(self.stopbits) if self.stopbits == int(self.stopbits) else self.stopbits
        return f"{self.port} ({self.baudrate} {self.bytesize}{self.parity}{sb})"


# ============================================================================
# Background Reader Loops
# ============================================================================


async def _background_telnet_reader_loop(session: TelnetSession) -> None:
    """Continuously reads raw bytes from telnet socket in background and buffers them."""
    try:
        while session.is_active:
            try:
                data = await asyncio.to_thread(session.telnet.read_very_eager)
            except (OSError, EOFError):
                logger.info(f"Telnet connection closed for session {session.session_id}")
                session.is_active = False
                break
            except Exception as e:
                logger.warning(f"Error reading socket in session {session.session_id}: {e}")
                await asyncio.sleep(0.05)
                continue

            if data:
                text = data.decode("utf-8", errors="ignore")
                async with session.lock:
                    session.accumulated_output += text
                    session.unconsumed_output += text
                    session.total_bytes_received += len(data)
                    session.last_activity = time.time()

                if session.logger:
                    session.logger.log_stream_chunk(
                        session_id=session.session_id,
                        chunk=text,
                    )
            await asyncio.sleep(0.02)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Background telnet reader loop crashed for {session.session_id}: {e}")


async def _background_serial_reader_loop(session: SerialSession) -> None:
    """Continuously reads raw bytes from serial port in background and buffers them."""
    try:
        while session.is_active:
            try:
                # Read available bytes without long blocking
                to_read = max(1, session.serial.in_waiting or 1)
                data = await asyncio.to_thread(session.serial.read, to_read)
            except (serial.SerialException, OSError) as e:
                logger.info(f"Serial port closed/disconnected for session {session.session_id}: {e}")
                session.is_active = False
                break
            except Exception as e:
                logger.warning(f"Error reading serial port in session {session.session_id}: {e}")
                await asyncio.sleep(0.05)
                continue

            if data:
                text = data.decode("utf-8", errors="ignore")
                async with session.lock:
                    session.accumulated_output += text
                    session.unconsumed_output += text
                    session.total_bytes_received += len(data)
                    session.last_activity = time.time()

                if session.logger:
                    session.logger.log_stream_chunk(
                        session_id=session.session_id,
                        chunk=text,
                    )
            await asyncio.sleep(0.02)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Background serial reader loop crashed for {session.session_id}: {e}")


# ============================================================================
# Unified Session Store
# ============================================================================


class SessionStore:
    """In-memory storage for active Telnet and Serial connections."""

    def __init__(self):
        self._sessions: dict[str, Union[TelnetSession, SerialSession]] = {}

    async def store(self, session: Union[TelnetSession, SerialSession]) -> None:
        """Store a session and launch its background stream reader task."""
        self._sessions[session.session_id] = session
        if session.reader_task is None or session.reader_task.done():
            if isinstance(session, TelnetSession):
                session.reader_task = asyncio.create_task(_background_telnet_reader_loop(session))
            elif isinstance(session, SerialSession):
                session.reader_task = asyncio.create_task(_background_serial_reader_loop(session))

    async def get(self, session_id: str) -> Optional[Union[TelnetSession, SerialSession]]:
        """Retrieve a session by ID."""
        return self._sessions.get(session_id)

    async def delete(self, session_id: str) -> None:
        """Delete a session, cancel its background reader, and close port/socket."""
        session = self._sessions.get(session_id)
        if session:
            session.is_active = False

            if isinstance(session, TelnetSession):
                try:
                    await asyncio.to_thread(session.telnet.close)
                except Exception as e:
                    logger.warning(f"Error closing telnet connection: {e}")
            elif isinstance(session, SerialSession):
                try:
                    await asyncio.to_thread(session.serial.close)
                except Exception as e:
                    logger.warning(f"Error closing serial port: {e}")

            if session.reader_task and not session.reader_task.done():
                session.reader_task.cancel()
                try:
                    await session.reader_task
                except asyncio.CancelledError:
                    pass

            if session.logger:
                session.logger.log_session_end(
                    session_id=session.session_id,
                    reason="Closed",
                    total_bytes=session.total_bytes_received,
                )

            del self._sessions[session_id]

    async def list_all(self) -> list[Union[TelnetSession, SerialSession]]:
        """List all active sessions."""
        return list(self._sessions.values())


# Global session store instance
_session_store = SessionStore()


# ============================================================================
# Helper Functions
# ============================================================================


def _create_negotiation_callback():
    """Handle telnet option negotiation."""

    def callback(sock, cmd, opt):
        if cmd == TelnetCommand.DO.value:
            sock.sendall(TelnetCommand.IAC.value + TelnetCommand.WONT.value + opt)
        elif cmd == TelnetCommand.WILL.value:
            sock.sendall(TelnetCommand.IAC.value + TelnetCommand.DONT.value + opt)

    return callback


async def _connect_telnet(host: str, port: int) -> tuple[telnetlib.Telnet, str]:
    """Establish a new telnet connection."""
    tn = telnetlib.Telnet()  # nosec B312
    tn.set_option_negotiation_callback(_create_negotiation_callback())

    try:
        await asyncio.to_thread(tn.open, host, port, TerminalDefaults.CONNECTION_TIMEOUT)
    except Exception as ex:
        raise RuntimeError(f"Failed to connect to Telnet server at {host}:{port}: {ex}")

    await asyncio.sleep(TerminalDefaults.INITIAL_BANNER_WAIT)
    initial_data = await asyncio.to_thread(tn.read_very_eager)
    if not initial_data:
        initial_data = await asyncio.to_thread(tn.read_some)

    initial_banner = initial_data.decode("utf-8", errors="ignore")
    return tn, initial_banner


async def _connect_serial(
    port: str,
    baudrate: int = 9600,
    bytesize: int = 8,
    parity: str = "N",
    stopbits: float = 1.0,
    rtscts: bool = False,
    xonxoff: bool = False,
    response_wait: float = 1.0,
) -> tuple[serial.Serial, str]:
    """Establish a new serial connection."""
    parity_map = {
        "N": serial.PARITY_NONE,
        "E": serial.PARITY_EVEN,
        "O": serial.PARITY_ODD,
        "M": serial.PARITY_MARK,
        "S": serial.PARITY_SPACE,
    }
    stopbits_map = {
        1: serial.STOPBITS_ONE,
        1.0: serial.STOPBITS_ONE,
        1.5: serial.STOPBITS_ONE_POINT_FIVE,
        2: serial.STOPBITS_TWO,
        2.0: serial.STOPBITS_TWO,
    }
    bytesize_map = {
        5: serial.FIVEBITS,
        6: serial.SIXBITS,
        7: serial.SEVENBITS,
        8: serial.EIGHTBITS,
    }

    try:
        ser = await asyncio.to_thread(
            serial.Serial,
            port=port,
            baudrate=baudrate,
            bytesize=bytesize_map.get(bytesize, serial.EIGHTBITS),
            parity=parity_map.get(parity.upper(), serial.PARITY_NONE),
            stopbits=stopbits_map.get(stopbits, serial.STOPBITS_ONE),
            timeout=0.1,
            rtscts=rtscts,
            xonxoff=xonxoff,
        )
    except Exception as ex:
        raise RuntimeError(f"Failed to open serial port at {port} ({baudrate} baud): {ex}")

    await asyncio.sleep(response_wait)
    initial_bytes = b""
    try:
        if ser.in_waiting:
            initial_bytes = await asyncio.to_thread(ser.read, ser.in_waiting)
    except Exception:
        pass

    initial_banner = initial_bytes.decode("utf-8", errors="ignore")
    return ser, initial_banner


def _strip_command_echo(response: str, command: str) -> str:
    """Remove command echo from terminal response."""
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
    session: Union[TelnetSession, SerialSession],
    start_index: int = 0,
    max_wait_seconds: float = TerminalDefaults.MAX_WAIT_SECONDS,
    prompt_pattern: Optional[str] = None,
    idle_timeout: Optional[float] = None,
) -> tuple[str, bool, Optional[str]]:
    """
    Wait for output to arrive or prompt pattern to match in session buffer.
    Returns: (output_text, command_completed, matched_prompt_string)
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
            current_output = session.accumulated_output[start_index:]
            last_activity = session.last_activity

        if compiled_pattern:
            match = compiled_pattern.search(current_output)
            if match:
                matched_str = match.group(0)
                async with session.lock:
                    session.unconsumed_output = ""
                return current_output, True, matched_str

        if idle_timeout and (time.time() - last_activity >= idle_timeout):
            if current_output:
                async with session.lock:
                    session.unconsumed_output = ""
                return current_output, True, None

        await asyncio.sleep(0.1)

    # Reached max_wait_seconds
    async with session.lock:
        interim_output = session.accumulated_output[start_index:]
        session.unconsumed_output = ""
    return interim_output, False, None


# ============================================================================
# Telnet MCP Tools
# ============================================================================


@tool(name="telnet_client")
async def telnet_client_tool(
    host: str,
    port: int,
    commands: list[str],
    telnet_session_id: Optional[str] = None,
    close_session: bool = False,
    command_delay: float = TerminalDefaults.COMMAND_DELAY,
    response_wait: float = TerminalDefaults.RESPONSE_WAIT,
    max_wait_seconds: float = TerminalDefaults.MAX_WAIT_SECONDS,
    prompt_pattern: str = TerminalDefaults.PROMPT_PATTERN,
    log_dir: Optional[str] = None,
    enable_logging: bool = True,
    timestamp_chunks: bool = False,
) -> TelnetClientOutput:
    """Execute commands on a remote system via Telnet with timeout immunity and session logging."""
    actual_session_id = telnet_session_id or f"telnet_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    session = await _session_store.get(actual_session_id)
    initial_banner = ""
    session_logger = SessionLogger(log_dir) if enable_logging else None

    if session is None or not session.is_active:
        tn, initial_banner = await _connect_telnet(host, port)
        session = TelnetSession(
            telnet=tn,
            host=host,
            port=port,
            created_at=time.time(),
            session_id=actual_session_id,
            logger=session_logger,
            accumulated_output=initial_banner,
            unconsumed_output="",
            total_bytes_received=len(initial_banner.encode("utf-8")),
        )
        if session_logger:
            session_logger.log_session_start(
                session_id=actual_session_id,
                target=f"{host}:{port}",
                protocol="TELNET",
                banner=initial_banner,
                timestamp_chunks=timestamp_chunks,
            )
        await _session_store.store(session)
    else:
        if session.logger is None and session_logger:
            session.logger = session_logger
        elif session.logger and timestamp_chunks:
            session.logger._timestamp_mode[actual_session_id] = True

    responses: list[CommandResponse] = []
    all_completed = True
    last_matched_prompt: Optional[str] = None

    for i, cmd in enumerate(commands):
        async with session.lock:
            cmd_start_index = len(session.accumulated_output)
            session.current_cmd_start_index = cmd_start_index

        cmd_bytes = (cmd + "\r\n").encode("utf-8")
        try:
            await asyncio.to_thread(session.telnet.write, cmd_bytes)
        except Exception as e:
            logger.error(f"Failed to write command '{cmd}' to session {actual_session_id}: {e}")
            session.is_active = False
            break

        out, completed, prompt_match = await _wait_for_output(
            session=session,
            start_index=cmd_start_index,
            max_wait_seconds=max_wait_seconds,
            prompt_pattern=prompt_pattern,
        )

        cleaned_response = _strip_command_echo(out, cmd)
        responses.append(CommandResponse(command=cmd, response=cleaned_response))

        if session.logger:
            session.logger.log_command(
                session_id=actual_session_id,
                command=cmd,
                response=cleaned_response,
            )

        if not completed:
            all_completed = False
            break

        last_matched_prompt = prompt_match
        if i < len(commands) - 1:
            await asyncio.sleep(command_delay)

    log_path = session.logger.get_session_log_path(actual_session_id) if session.logger else None

    if close_session:
        await _session_store.delete(actual_session_id)
        is_active = False
    else:
        is_active = session.is_active

    return TelnetClientOutput(
        host=host,
        port=port,
        initial_banner=initial_banner,
        responses=responses,
        session_id=actual_session_id,
        session_active=is_active,
        command_completed=all_completed,
        prompt_matched=last_matched_prompt,
        log_file=log_path,
    )


@tool(name="telnet_read_session")
async def telnet_read_session(
    session_id: str,
    max_wait_seconds: float = TerminalDefaults.MAX_WAIT_SECONDS,
    prompt_pattern: Optional[str] = None,
    idle_timeout: Optional[float] = None,
    return_full_buffer: bool = False,
) -> TelnetReadSessionOutput:
    """Read unconsumed or buffered output from an active Telnet session."""
    session = await _session_store.get(session_id)
    if session is None or not isinstance(session, TelnetSession):
        raise ValueError(f"No active Telnet session found with ID '{session_id}'")

    start_index = session.current_cmd_start_index if return_full_buffer else len(session.accumulated_output) - len(session.unconsumed_output)

    out, completed, prompt_match = await _wait_for_output(
        session=session,
        start_index=start_index,
        max_wait_seconds=max_wait_seconds,
        prompt_pattern=prompt_pattern,
        idle_timeout=idle_timeout,
    )

    log_path = session.logger.get_session_log_path(session_id) if session.logger else None

    return TelnetReadSessionOutput(
        session_id=session_id,
        host=session.host,
        port=session.port,
        output=out,
        new_bytes_count=len(out.encode("utf-8")),
        total_bytes_received=session.total_bytes_received,
        command_completed=completed,
        prompt_matched=prompt_match,
        session_active=session.is_active,
        log_file=log_path,
    )


@tool(name="telnet_send_input")
async def telnet_send_input(
    session_id: str,
    input_text: str,
    raw: bool = False,
    max_wait_seconds: float = 5.0,
    prompt_pattern: Optional[str] = None,
) -> TelnetSendInputOutput:
    """Send interactive input or keystrokes to an active Telnet session."""
    session = await _session_store.get(session_id)
    if session is None or not isinstance(session, TelnetSession):
        raise ValueError(f"No active Telnet session found with ID '{session_id}'")

    async with session.lock:
        start_index = len(session.accumulated_output)
        session.current_cmd_start_index = start_index

    payload = input_text.encode("utf-8") if raw else (input_text + "\r\n").encode("utf-8")
    await asyncio.to_thread(session.telnet.write, payload)

    if session.logger:
        session.logger.log_input(session_id=session_id, input_text=input_text)

    out, completed, prompt_match = await _wait_for_output(
        session=session,
        start_index=start_index,
        max_wait_seconds=max_wait_seconds,
        prompt_pattern=prompt_pattern,
    )

    log_path = session.logger.get_session_log_path(session_id) if session.logger else None

    return TelnetSendInputOutput(
        session_id=session_id,
        input_sent=input_text,
        response=out,
        command_completed=completed,
        prompt_matched=prompt_match,
        session_active=session.is_active,
        log_file=log_path,
    )


@tool(name="telnet_close_session")
async def telnet_close_session(session_id: str) -> SessionCloseResponse:
    """Close an active Telnet session."""
    session = await _session_store.get(session_id)
    if session:
        await _session_store.delete(session_id)
        return SessionCloseResponse(success=True, message=f"Telnet session '{session_id}' closed successfully.")
    return SessionCloseResponse(success=False, message=f"No active session found with ID '{session_id}'.")


# ============================================================================
# Serial MCP Tools
# ============================================================================


@tool(name="serial_client")
async def serial_client_tool(
    port: str,
    commands: list[str],
    baudrate: int = 9600,
    bytesize: int = 8,
    parity: str = "N",
    stopbits: float = 1.0,
    rtscts: bool = False,
    xonxoff: bool = False,
    serial_session_id: Optional[str] = None,
    close_session: bool = False,
    command_delay: float = TerminalDefaults.COMMAND_DELAY,
    response_wait: float = TerminalDefaults.RESPONSE_WAIT,
    max_wait_seconds: float = TerminalDefaults.MAX_WAIT_SECONDS,
    prompt_pattern: str = TerminalDefaults.PROMPT_PATTERN,
    log_dir: Optional[str] = None,
    enable_logging: bool = True,
    timestamp_chunks: bool = False,
) -> SerialClientOutput:
    """Execute commands on a hardware serial/console port with timeout immunity and session logging."""
    actual_session_id = serial_session_id or f"serial_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    session = await _session_store.get(actual_session_id)
    initial_banner = ""
    session_logger = SessionLogger(log_dir) if enable_logging else None

    if session is None or not session.is_active:
        ser, initial_banner = await _connect_serial(
            port=port,
            baudrate=baudrate,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            rtscts=rtscts,
            xonxoff=xonxoff,
            response_wait=response_wait,
        )
        session = SerialSession(
            serial=ser,
            port=port,
            baudrate=baudrate,
            bytesize=bytesize,
            parity=parity.upper(),
            stopbits=stopbits,
            created_at=time.time(),
            session_id=actual_session_id,
            logger=session_logger,
            accumulated_output=initial_banner,
            unconsumed_output="",
            total_bytes_received=len(initial_banner.encode("utf-8")),
        )
        if session_logger:
            sb = int(stopbits) if stopbits == int(stopbits) else stopbits
            target_str = f"{port} ({baudrate} {bytesize}{parity.upper()}{sb})"
            session_logger.log_session_start(
                session_id=actual_session_id,
                target=target_str,
                protocol="SERIAL",
                banner=initial_banner,
                timestamp_chunks=timestamp_chunks,
            )
        await _session_store.store(session)
    else:
        if session.logger is None and session_logger:
            session.logger = session_logger
        elif session.logger and timestamp_chunks:
            session.logger._timestamp_mode[actual_session_id] = True

    responses: list[CommandResponse] = []
    all_completed = True
    last_matched_prompt: Optional[str] = None

    for i, cmd in enumerate(commands):
        async with session.lock:
            cmd_start_index = len(session.accumulated_output)
            session.current_cmd_start_index = cmd_start_index

        cmd_bytes = (cmd + "\r\n").encode("utf-8")
        try:
            await asyncio.to_thread(session.serial.write, cmd_bytes)
            await asyncio.to_thread(session.serial.flush)
        except Exception as e:
            logger.error(f"Failed to write command '{cmd}' to serial session {actual_session_id}: {e}")
            session.is_active = False
            break

        out, completed, prompt_match = await _wait_for_output(
            session=session,
            start_index=cmd_start_index,
            max_wait_seconds=max_wait_seconds,
            prompt_pattern=prompt_pattern,
        )

        cleaned_response = _strip_command_echo(out, cmd)
        responses.append(CommandResponse(command=cmd, response=cleaned_response))

        if session.logger:
            session.logger.log_command(
                session_id=actual_session_id,
                command=cmd,
                response=cleaned_response,
            )

        if not completed:
            all_completed = False
            break

        last_matched_prompt = prompt_match
        if i < len(commands) - 1:
            await asyncio.sleep(command_delay)

    log_path = session.logger.get_session_log_path(actual_session_id) if session.logger else None

    if close_session:
        await _session_store.delete(actual_session_id)
        is_active = False
    else:
        is_active = session.is_active

    return SerialClientOutput(
        port=port,
        baudrate=session.baudrate,
        initial_banner=initial_banner,
        responses=responses,
        session_id=actual_session_id,
        session_active=is_active,
        command_completed=all_completed,
        prompt_matched=last_matched_prompt,
        log_file=log_path,
    )


@tool(name="serial_read_session")
async def serial_read_session(
    session_id: str,
    max_wait_seconds: float = TerminalDefaults.MAX_WAIT_SECONDS,
    prompt_pattern: Optional[str] = None,
    idle_timeout: Optional[float] = None,
    return_full_buffer: bool = False,
) -> SerialReadSessionOutput:
    """Read unconsumed or buffered output from an active Serial session without sending new commands."""
    session = await _session_store.get(session_id)
    if session is None or not isinstance(session, SerialSession):
        raise ValueError(f"No active Serial session found with ID '{session_id}'")

    start_index = session.current_cmd_start_index if return_full_buffer else len(session.accumulated_output) - len(session.unconsumed_output)

    out, completed, prompt_match = await _wait_for_output(
        session=session,
        start_index=start_index,
        max_wait_seconds=max_wait_seconds,
        prompt_pattern=prompt_pattern,
        idle_timeout=idle_timeout,
    )

    log_path = session.logger.get_session_log_path(session_id) if session.logger else None

    return SerialReadSessionOutput(
        session_id=session_id,
        port=session.port,
        baudrate=session.baudrate,
        output=out,
        new_bytes_count=len(out.encode("utf-8")),
        total_bytes_received=session.total_bytes_received,
        command_completed=completed,
        prompt_matched=prompt_match,
        session_active=session.is_active,
        log_file=log_path,
    )


@tool(name="serial_send_input")
async def serial_send_input(
    session_id: str,
    input_text: str,
    raw: bool = False,
    max_wait_seconds: float = 5.0,
    prompt_pattern: Optional[str] = None,
) -> SerialSendInputOutput:
    """Send interactive input, passwords, or raw keystrokes to an active Serial session."""
    session = await _session_store.get(session_id)
    if session is None or not isinstance(session, SerialSession):
        raise ValueError(f"No active Serial session found with ID '{session_id}'")

    async with session.lock:
        start_index = len(session.accumulated_output)
        session.current_cmd_start_index = start_index

    payload = input_text.encode("utf-8") if raw else (input_text + "\r\n").encode("utf-8")
    await asyncio.to_thread(session.serial.write, payload)
    await asyncio.to_thread(session.serial.flush)

    if session.logger:
        session.logger.log_input(session_id=session_id, input_text=input_text)

    out, completed, prompt_match = await _wait_for_output(
        session=session,
        start_index=start_index,
        max_wait_seconds=max_wait_seconds,
        prompt_pattern=prompt_pattern,
    )

    log_path = session.logger.get_session_log_path(session_id) if session.logger else None

    return SerialSendInputOutput(
        session_id=session_id,
        input_sent=input_text,
        response=out,
        command_completed=completed,
        prompt_matched=prompt_match,
        session_active=session.is_active,
        log_file=log_path,
    )


@tool(name="serial_send_break")
async def serial_send_break(
    session_id: str,
    duration: float = 0.25,
    max_wait_seconds: float = 5.0,
    prompt_pattern: str = r">>> ",
) -> SerialSendBreakOutput:
    """Send an RS-232 hardware Break condition (~250ms) to halt a VAX into the console prompt (>>>)."""
    session = await _session_store.get(session_id)
    if session is None or not isinstance(session, SerialSession):
        raise ValueError(f"No active Serial session found with ID '{session_id}'")

    async with session.lock:
        start_index = len(session.accumulated_output)
        session.current_cmd_start_index = start_index

    try:
        await asyncio.to_thread(session.serial.send_break, duration=duration)
    except Exception as e:
        return SerialSendBreakOutput(
            session_id=session_id,
            success=False,
            prompt_matched=None,
            output="",
            message=f"Failed sending break condition: {e}",
        )

    if session.logger:
        session.logger.log_config_change(session_id, f"Sent hardware RS-232 BREAK ({duration}s)")

    out, completed, prompt_match = await _wait_for_output(
        session=session,
        start_index=start_index,
        max_wait_seconds=max_wait_seconds,
        prompt_pattern=prompt_pattern,
    )

    return SerialSendBreakOutput(
        session_id=session_id,
        success=completed or bool(out),
        prompt_matched=prompt_match,
        output=out,
        message="Break sent successfully.",
    )


@tool(name="serial_set_speed")
async def serial_set_speed(
    session_id: str,
    baudrate: int,
    bytesize: int = 8,
    parity: str = "N",
    stopbits: float = 1.0,
) -> SerialSetSpeedOutput:
    """Dynamically reconfigure baud rate and framing on an active serial session without reconnecting."""
    session = await _session_store.get(session_id)
    if session is None or not isinstance(session, SerialSession):
        raise ValueError(f"No active Serial session found with ID '{session_id}'")

    parity_map = {
        "N": serial.PARITY_NONE,
        "E": serial.PARITY_EVEN,
        "O": serial.PARITY_ODD,
        "M": serial.PARITY_MARK,
        "S": serial.PARITY_SPACE,
    }
    stopbits_map = {
        1: serial.STOPBITS_ONE,
        1.0: serial.STOPBITS_ONE,
        1.5: serial.STOPBITS_ONE_POINT_FIVE,
        2: serial.STOPBITS_TWO,
        2.0: serial.STOPBITS_TWO,
    }
    bytesize_map = {
        5: serial.FIVEBITS,
        6: serial.SIXBITS,
        7: serial.SEVENBITS,
        8: serial.EIGHTBITS,
    }

    old_baud = session.baudrate
    try:
        session.serial.baudrate = baudrate
        session.serial.bytesize = bytesize_map.get(bytesize, serial.EIGHTBITS)
        session.serial.parity = parity_map.get(parity.upper(), serial.PARITY_NONE)
        session.serial.stopbits = stopbits_map.get(stopbits, serial.STOPBITS_ONE)

        session.baudrate = baudrate
        session.bytesize = bytesize
        session.parity = parity.upper()
        session.stopbits = stopbits

        sb = int(stopbits) if stopbits == int(stopbits) else stopbits
        framing_str = f"{bytesize}{parity.upper()}{sb}"

        if session.logger:
            session.logger.log_config_change(
                session_id=session_id,
                change_description=f"Reconfigured port to {baudrate} baud, {framing_str}",
            )

        return SerialSetSpeedOutput(
            session_id=session_id,
            old_baudrate=old_baud,
            new_baudrate=baudrate,
            framing=framing_str,
            success=True,
            message=f"Session '{session_id}' speed reconfigured from {old_baud} to {baudrate} baud ({framing_str}).",
        )
    except Exception as e:
        return SerialSetSpeedOutput(
            session_id=session_id,
            old_baudrate=old_baud,
            new_baudrate=old_baud,
            framing=f"{session.bytesize}{session.parity}{session.stopbits}",
            success=False,
            message=f"Failed setting serial speed: {e}",
        )


@tool(name="serial_list_ports")
async def serial_list_ports() -> SerialListPortsOutput:
    """Scan and list all available hardware serial ports on the host system."""
    ports_info = []
    try:
        available_ports = await asyncio.to_thread(serial.tools.list_ports.comports)
        for p in available_ports:
            # Check if port can be opened
            avail = True
            try:
                test_s = serial.Serial(p.device)
                test_s.close()
            except Exception:
                avail = False

            ports_info.append(
                SerialPortInfo(
                    device=p.device,
                    description=p.description or "",
                    hwid=p.hwid or "",
                    available=avail,
                )
            )
    except Exception as e:
        logger.warning(f"Error enumerating serial ports: {e}")

    return SerialListPortsOutput(total_ports=len(ports_info), ports=ports_info)


@tool(name="serial_close_session")
async def serial_close_session(session_id: str) -> SessionCloseResponse:
    """Close an active Serial session."""
    session = await _session_store.get(session_id)
    if session:
        await _session_store.delete(session_id)
        return SessionCloseResponse(success=True, message=f"Serial session '{session_id}' closed successfully.")
    return SessionCloseResponse(success=False, message=f"No active session found with ID '{session_id}'.")


# ============================================================================
# Unified Session Directory Tools
# ============================================================================


@tool(name="list_sessions")
async def list_sessions() -> SessionListResponse:
    """List all active terminal sessions (both Telnet and Serial)."""
    sessions = await _session_store.list_all()
    sessions_dict: dict[str, SessionInfo] = {}
    now = time.time()

    for s in sessions:
        if isinstance(s, TelnetSession):
            sessions_dict[s.session_id] = SessionInfo(
                session_id=s.session_id,
                session_type="telnet",
                target=s.target,
                host=s.host,
                port=s.port,
                created_at=s.created_at,
                age_seconds=round(now - s.created_at, 1),
                is_active=s.is_active,
                total_bytes_received=s.total_bytes_received,
            )
        elif isinstance(s, SerialSession):
            sessions_dict[s.session_id] = SessionInfo(
                session_id=s.session_id,
                session_type="serial",
                target=s.target,
                port=s.port,
                baudrate=s.baudrate,
                created_at=s.created_at,
                age_seconds=round(now - s.created_at, 1),
                is_active=s.is_active,
                total_bytes_received=s.total_bytes_received,
            )

    return SessionListResponse(
        active_sessions=len(sessions_dict),
        sessions=sessions_dict,
    )


@tool(name="telnet_list_sessions")
async def telnet_list_sessions() -> SessionListResponse:
    """Deprecated alias for list_sessions."""
    return await list_sessions()
