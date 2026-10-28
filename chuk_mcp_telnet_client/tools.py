"""
Terminal client tools (Telnet & Serial) for MCP server.

Provides async Telnet and Serial connectivity with background stream buffering,
long-running process polling without timeouts, dynamic baud rate switching,
clean transcript disk logging without overwriting, and polymorphic multi-session management.
"""

import asyncio
import logging
import re
import select
import socket
import telnetlib  # nosec B401 - telnet is part of this MCP server
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Union

import pyte
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
    TerminalResizeOutput,
    TerminalScreenOutput,
    TerminalSendKeyOutput,
    TerminalWaitForChangeOutput,
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
    # Session reader threads (v0.7.1). The reader blocks for at most READER_POLL_SECONDS
    # at a time, so a stop request is seen within that; after data first arrives it waits
    # READER_COALESCE_SECONDS so a burst becomes one chunk (the 0.7.0 asyncio loop slept
    # 20 ms between reads, which gave the same chunk size); close joins the thread for at
    # most READER_JOIN_SECONDS.
    READER_POLL_SECONDS: float = 0.1
    READER_COALESCE_SECONDS: float = 0.02
    READER_JOIN_SECONDS: float = 2.0
    # terminal_wait_for_change: the longest a single call may wait, so a call cannot hang
    # a client for ever, and how often it looks at the (always current) session state.
    WAIT_FOR_CHANGE_MAX_TIMEOUT: float = 300.0
    WAIT_FOR_CHANGE_POLL_SECONDS: float = 0.02
    # Matches, in order:
    #   1. (?m:^\$ )            - a bare "$ " DCL prompt at the start of any line
    #                             (stock, uncustomized OpenVMS SYLOGIN.COM).
    #   2. \$ ?$                - "...$" / "...$ " at the TRUE END of the accumulated
    #                             buffer (no (?m) scope here - $ means "end of the whole
    #                             string", not "end of any line"). This is what actually
    #                             catches a customized DCL prompt like "VAX96::USER1$ "
    #                             regardless of the node/username prefix: real VMS sites
    #                             commonly set SYLOGIN.COM to show "NODE::USER$ " instead
    #                             of a bare "$ ", and the previous pattern (anchored to
    #                             line-start) never matched that at all. Anchoring to the
    #                             true end of the buffer (rather than (?m)'s "any line")
    #                             matters here specifically because _wait_for_output()
    #                             re-searches the whole accumulated-so-far buffer on every
    #                             poll - "the prompt is the last thing we've received" is
    #                             the correct completion signal, not "some earlier line
    #                             happened to end in $" while more output is still coming.
    #   3. (?m:^[A-Za-z][A-Za-z0-9_ /]*> ?$) - VMS subsystem/utility prompts, which are a
    #                             different shape entirely: "facility name, then >, maybe
    #                             a trailing space" - e.g. TCPIP>, MCL>, NCP>, AUTHORIZE>,
    #                             SET HOST 0>, ANALYZE/SYSTEM>. Kept deliberately narrow
    #                             (letters/digits/underscore/space/slash only) rather than
    #                             ".*" to avoid matching arbitrary program output that
    #                             happens to end a line in "word>".
    #   4/5. Username: / Password: - login prompts, unchanged.
    #   6. >>>                  - VAX/Alpha console firmware prompt, unchanged.
    # See PLAN_DEFAULT_PROMPT_PATTERN.md for the full design rationale, evidence of the
    # original gap, and remaining known limitations.
    PROMPT_PATTERN: str = (
        r"("
        r"(?m:^\$ )"
        r"|\$ ?$"
        r"|(?m:^[A-Za-z][A-Za-z0-9_ /]*> ?$)"
        r"|Username: "
        r"|Password: "
        r"|>>> "
        r")"
    )


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
    # The reader runs in its own daemon thread (see _telnet_reader_thread), so the
    # socket is read when bytes arrive, not only while a tool call is being served.
    reader_thread: Optional[threading.Thread] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    # lock: the session's state (buffers, pyte screen, counters). write_lock: the
    # transport's output, so writers never interleave. Never held together.
    lock: threading.Lock = field(default_factory=threading.Lock)
    write_lock: threading.Lock = field(default_factory=threading.Lock)
    total_bytes_received: int = 0
    cols: int = 80
    rows: int = 24
    screen: Optional[pyte.Screen] = None
    stream: Optional[pyte.Stream] = None
    # For terminal_wait_for_change: screen_seq counts chunks fed to pyte; last_rx_monotonic
    # is when the last one was fed; last_read_sig is the screen last returned to a client.
    screen_seq: int = 0
    last_rx_monotonic: float = field(default_factory=time.monotonic)
    last_read_sig: Optional[tuple] = None

    def __post_init__(self):
        if self.screen is None:
            self.screen = pyte.Screen(self.cols, self.rows)
        if self.stream is None:
            self.stream = pyte.Stream(self.screen)
            self.stream.use_utf8 = False
        if self.accumulated_output and self.stream:
            try:
                self.stream.feed(self.accumulated_output)
            except Exception as e:
                logger.warning(f"Error feeding initial output to pyte screen: {e}")

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
    # The reader runs in its own daemon thread (see _telnet_reader_thread), so the
    # socket is read when bytes arrive, not only while a tool call is being served.
    reader_thread: Optional[threading.Thread] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    # lock: the session's state (buffers, pyte screen, counters). write_lock: the
    # transport's output, so writers never interleave. Never held together.
    lock: threading.Lock = field(default_factory=threading.Lock)
    write_lock: threading.Lock = field(default_factory=threading.Lock)
    total_bytes_received: int = 0
    cols: int = 80
    rows: int = 24
    screen: Optional[pyte.Screen] = None
    stream: Optional[pyte.Stream] = None
    # For terminal_wait_for_change: screen_seq counts chunks fed to pyte; last_rx_monotonic
    # is when the last one was fed; last_read_sig is the screen last returned to a client.
    screen_seq: int = 0
    last_rx_monotonic: float = field(default_factory=time.monotonic)
    last_read_sig: Optional[tuple] = None

    def __post_init__(self):
        if self.screen is None:
            self.screen = pyte.Screen(self.cols, self.rows)
        if self.stream is None:
            self.stream = pyte.Stream(self.screen)
            self.stream.use_utf8 = False
        if self.accumulated_output and self.stream:
            try:
                self.stream.feed(self.accumulated_output)
            except Exception as e:
                logger.warning(f"Error feeding initial output to pyte screen: {e}")

    @property
    def session_type(self) -> str:
        return "serial"

    @property
    def target(self) -> str:
        sb = int(self.stopbits) if self.stopbits == int(self.stopbits) else self.stopbits
        return f"{self.port} ({self.baudrate} {self.bytesize}{self.parity}{sb})"


# ============================================================================
# Background Reader Threads
# ============================================================================
#
# Why threads (v0.7.1): in stdio mode chuk_mcp_server's StdioSyncTransport blocks in
# sys.stdin.readline() between requests and runs the asyncio loop only while it serves
# one. The 0.7.0 readers were asyncio tasks on that loop, so between tool calls nothing
# read the socket or port: a wait-0 terminal_get_screen showed the screen as of the last
# call, and the transcript stamped a chunk when it was read rather than when it arrived.
# A daemon thread per session reads all the time, whatever the transport does.
#
# Locking: session.lock guards the session's state (buffers, pyte screen, counters);
# session.write_lock serialises writes to the transport. No code holds both at once.


def _write_transport(
    session: Union["TelnetSession", "SerialSession"],
    payload: bytes,
    flush: bool = False,
) -> None:
    """Write bytes to the session's socket or port under its write lock (blocking)."""
    with session.write_lock:
        if isinstance(session, TelnetSession):
            session.telnet.write(payload)
        else:
            session.serial.write(payload)
            if flush:
                session.serial.flush()


def _answer_vt_queries(session: Union["TelnetSession", "SerialSession"], data: bytes) -> None:
    """Automatic DEC VT terminal query handling: answer DA, CPR and DSR."""
    replies = []
    if b"\x1b[c" in data or b"\x1b[0c" in data or b"\x1bZ" in data:
        replies.append(("DA", b"\x1b[?62;1;2;6;7;8;9c"))
    if b"\x1b[6n" in data:
        replies.append(("CPR", f"\x1b[{session.rows};{session.cols}R".encode("utf-8")))
    if b"\x1b[5n" in data:
        replies.append(("DSR", b"\x1b[0n"))
    for name, reply in replies:
        try:
            _write_transport(session, reply)
        except Exception as e:
            logger.warning(f"Failed to respond to VT {name} query: {e}")


def _ingest_chunk(
    session: Union["TelnetSession", "SerialSession"],
    data: bytes,
    arrived_at: float,
) -> None:
    """Append a received chunk to the buffers, feed pyte, and log it stamped on arrival."""
    text = data.decode("utf-8", errors="ignore")
    with session.lock:
        session.accumulated_output += text
        session.unconsumed_output += text
        session.total_bytes_received += len(data)
        session.last_activity = time.time()
        session.last_rx_monotonic = time.monotonic()
        session.screen_seq += 1
        if session.stream:
            try:
                session.stream.feed(text)
            except Exception as e:
                logger.warning(f"Error feeding {session.session_type} text to pyte stream: {e}")

    if session.logger:
        session.logger.log_stream_chunk(
            session_id=session.session_id,
            chunk=text,
            at=arrived_at,
        )


def _telnet_reader_thread(session: "TelnetSession") -> None:
    """Read the telnet socket whenever bytes arrive, until EOF or a stop request."""
    try:
        sock = session.telnet.get_socket()
        while not session.stop_event.is_set():
            try:
                # Wakes on data, on EOF, and on the shutdown() a close does.
                readable, _, _ = select.select([sock], [], [], TerminalDefaults.READER_POLL_SECONDS)
            except (OSError, ValueError):
                break  # the socket was closed under us
            if not readable:
                continue
            arrived_at = time.time()
            if session.stop_event.wait(TerminalDefaults.READER_COALESCE_SECONDS):
                break
            try:
                # read_very_eager() does not block. telnetlib answers option negotiation
                # with its own sendall() inside it, hence the write lock.
                with session.write_lock:
                    data = session.telnet.read_very_eager()
            except (OSError, EOFError, AttributeError):
                logger.info(f"Telnet connection closed for session {session.session_id}")
                break
            if data:
                _answer_vt_queries(session, data)
                _ingest_chunk(session, data, arrived_at)
    except Exception as e:
        logger.error(f"Background telnet reader crashed for {session.session_id}: {e}")
    finally:
        session.is_active = False


def _serial_reader_thread(session: "SerialSession") -> None:
    """Read the serial port whenever bytes arrive, until it fails or a stop request."""
    ser = session.serial
    try:
        while not session.stop_event.is_set():
            try:
                # Blocks for at most the port's timeout (0.1 s, set at open).
                in_wait = getattr(ser, "in_waiting", 0)
                data = ser.read(max(1, in_wait or 1))
            except (serial.SerialException, OSError) as e:
                if not session.stop_event.is_set():
                    logger.info(f"Serial port closed/disconnected for session {session.session_id}: {e}")
                break
            except Exception as e:
                if session.stop_event.is_set():
                    break
                logger.warning(f"Error reading serial port in session {session.session_id}: {e}")
                session.stop_event.wait(0.05)
                continue
            if not data:
                continue
            arrived_at = time.time()
            session.stop_event.wait(TerminalDefaults.READER_COALESCE_SECONDS)
            try:
                more = getattr(ser, "in_waiting", 0)
                if more:
                    data += ser.read(more)
            except Exception:
                pass  # the next read reports a failed port
            _answer_vt_queries(session, data)
            _ingest_chunk(session, data, arrived_at)
    except Exception as e:
        logger.error(f"Background serial reader crashed for {session.session_id}: {e}")
    finally:
        session.is_active = False


def _start_reader(session: Union["TelnetSession", "SerialSession"]) -> None:
    """Start the session's reader thread unless one is already running."""
    if session.reader_thread is not None and session.reader_thread.is_alive():
        return
    target = _telnet_reader_thread if isinstance(session, TelnetSession) else _serial_reader_thread
    thread = threading.Thread(
        target=target,
        args=(session,),
        name=f"{session.session_type}-reader-{session.session_id}",
        daemon=True,
    )
    session.reader_thread = thread
    thread.start()


def _stop_session_io(session: Union["TelnetSession", "SerialSession"]) -> None:
    """Stop the reader thread (bounded join), then close the socket or port. Blocking.

    The transport is closed only after the join, so the descriptor cannot be reused
    while the reader still selects on it; shutdown()/cancel_read() wake a blocked reader.
    """
    session.is_active = False
    session.stop_event.set()

    if isinstance(session, TelnetSession):
        sock = session.telnet.get_socket()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # already disconnected
    else:
        try:
            cancel = getattr(session.serial, "cancel_read", None)
            if cancel is not None:
                cancel()
        except Exception:
            pass  # not every pyserial backend has it; the read timeout still ends it

    thread = session.reader_thread
    if thread is not None and thread is not threading.current_thread():
        thread.join(TerminalDefaults.READER_JOIN_SECONDS)
        if thread.is_alive():
            logger.warning(
                f"Reader thread for session {session.session_id} did not stop within "
                f"{TerminalDefaults.READER_JOIN_SECONDS} s; closing the transport anyway"
            )

    if isinstance(session, TelnetSession):
        try:
            session.telnet.close()
        except Exception as e:
            logger.warning(f"Error closing telnet connection: {e}")
    else:
        try:
            session.serial.close()
        except Exception as e:
            logger.warning(f"Error closing serial port: {e}")


# ============================================================================
# Unified Session Store
# ============================================================================


class SessionStore:
    """In-memory storage for active Telnet and Serial connections."""

    def __init__(self):
        self._sessions: dict[str, Union[TelnetSession, SerialSession]] = {}

    async def store(self, session: Union[TelnetSession, SerialSession]) -> None:
        """Store a session and start its background reader thread.

        A session already stored under the same ID (one that has died, since callers
        only replace inactive sessions) has its reader stopped and its transport closed.
        """
        old = self._sessions.get(session.session_id)
        self._sessions[session.session_id] = session
        if old is not None and old is not session:
            await asyncio.to_thread(_stop_session_io, old)
        _start_reader(session)

    async def get(self, session_id: str) -> Optional[Union[TelnetSession, SerialSession]]:
        """Retrieve a session by ID."""
        return self._sessions.get(session_id)

    async def delete(self, session_id: str) -> None:
        """Delete a session, stop its background reader, and close port/socket."""
        session = self._sessions.pop(session_id, None)
        if session:
            await asyncio.to_thread(_stop_session_io, session)

            if session.logger:
                session.logger.log_session_end(
                    session_id=session.session_id,
                    reason="Closed",
                    total_bytes=session.total_bytes_received,
                )

    async def list_all(self) -> list[Union[TelnetSession, SerialSession]]:
        """List all active sessions."""
        return list(self._sessions.values())


# Global session store instance
_session_store = SessionStore()


# ============================================================================
# Helper Functions
# ============================================================================


def _create_negotiation_callback():
    """Handle telnet option negotiation including RFC 1073 NAWS."""

    def callback(sock, cmd, opt):
        if cmd == TelnetCommand.DO.value:
            if opt == bytes([31]):  # RFC 1073 NAWS (Negotiate About Window Size)
                # Agree to provide window size: IAC WILL NAWS
                sock.sendall(TelnetCommand.IAC.value + TelnetCommand.WILL.value + opt)
                # Transmit initial 80 columns x 24 rows subnegotiation:
                # IAC SB NAWS 0 80 0 24 IAC SE
                sock.sendall(bytes([255, 250, 31, 0, 80, 0, 24, 255, 240]))
            else:
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
    initial_data = b""
    try:
        initial_data = await asyncio.to_thread(tn.read_very_eager)
        if not initial_data:
            try:
                initial_data = await asyncio.wait_for(
                    asyncio.to_thread(tn.read_some),
                    timeout=TerminalDefaults.RESPONSE_WAIT,
                )
            except (asyncio.TimeoutError, TimeoutError, OSError):
                initial_data = b""
    except Exception as e:
        logger.debug(f"Initial banner read failed or timed out: {e}")
        initial_data = b""

    initial_banner = initial_data.decode("utf-8", errors="ignore")
    return tn, initial_banner


def _normalize_serial_target(port: str) -> str:
    """Normalize a serial port path or network address to a pyserial URL or device path."""
    port_stripped = port.strip()
    if "://" in port_stripped:
        return port_stripped
    # Check for host:port format (IPv4, hostname, or bracketed IPv6 with port)
    # Exclude filesystem device paths like /dev/... or Windows paths like C:\... or COM1
    if not port_stripped.startswith("/") and not port_stripped.startswith("\\"):
        match = re.match(r"^(\[[0-9a-fA-F:]+\]|[a-zA-Z0-9.\-_]+):(\d+)$", port_stripped)
        if match:
            return f"rfc2217://{port_stripped}"
    return port_stripped


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
    """Establish a new serial connection (local hardware or network-bridged RFC 2217 / raw socket)."""
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

    normalized_port = _normalize_serial_target(port)
    target_urls = [normalized_port]
    # If auto-prefixed with rfc2217:// (i.e. user passed host:port without scheme),
    # prepare socket:// as fallback in case the remote endpoint is a raw TCP socket without RFC 2217
    if normalized_port.startswith("rfc2217://") and "://" not in port:
        target_urls.append(f"socket://{port.strip()}")

    ser = None
    last_ex = None
    for target in target_urls:
        try:
            ser = await asyncio.to_thread(
                serial.serial_for_url,
                url=target,
                baudrate=baudrate,
                bytesize=bytesize_map.get(bytesize, serial.EIGHTBITS),
                parity=parity_map.get(parity.upper(), serial.PARITY_NONE),
                stopbits=stopbits_map.get(stopbits, serial.STOPBITS_ONE),
                timeout=0.1,
                rtscts=rtscts,
                xonxoff=xonxoff,
            )
            break
        except Exception as ex:
            last_ex = ex
            if len(target_urls) > 1 and target == target_urls[0] and "RFC2217" in str(ex):
                logger.info(f"RFC 2217 negotiation failed for {target}, attempting raw socket fallback: {ex}")
                continue
            raise RuntimeError(f"Failed to open serial port at {port} ({baudrate} baud): {ex}")

    if ser is None and last_ex:
        raise RuntimeError(f"Failed to open serial port at {port} ({baudrate} baud): {last_ex}")

    await asyncio.sleep(response_wait)
    initial_bytes = b""
    try:
        in_wait = getattr(ser, "in_waiting", 0)
        if in_wait:
            initial_bytes = await asyncio.to_thread(ser.read, in_wait)
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
            with session.lock:
                out = session.unconsumed_output
                session.unconsumed_output = ""
                return out, True, None

        with session.lock:
            current_output = session.accumulated_output[start_index:]
            last_activity = session.last_activity

        if compiled_pattern:
            match = compiled_pattern.search(current_output)
            if match:
                matched_str = match.group(0)
                with session.lock:
                    session.unconsumed_output = ""
                return current_output, True, matched_str

        if idle_timeout and (time.time() - last_activity >= idle_timeout):
            if current_output:
                with session.lock:
                    session.unconsumed_output = ""
                return current_output, True, None

        await asyncio.sleep(0.1)

    # Reached max_wait_seconds
    with session.lock:
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
    newline: str = "\r\n",
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

        # If a prompt pattern is provided for a new session, ensure the initial prompt
        # (e.g. 'Username: ' or shell prompt) has arrived before sending commands.
        if prompt_pattern and commands:
            compiled = re.compile(prompt_pattern)
            if not compiled.search(session.accumulated_output):
                await _wait_for_output(
                    session=session,
                    start_index=0,
                    max_wait_seconds=min(max_wait_seconds, 15.0),
                    prompt_pattern=prompt_pattern,
                )
    else:
        if session.logger is None and session_logger:
            session.logger = session_logger
        elif session.logger and timestamp_chunks:
            session.logger._timestamp_mode[actual_session_id] = True

    responses: list[CommandResponse] = []
    all_completed = True
    last_matched_prompt: Optional[str] = None

    for i, cmd in enumerate(commands):
        with session.lock:
            cmd_start_index = len(session.accumulated_output)
            session.current_cmd_start_index = cmd_start_index

        cmd_bytes = (cmd + newline).encode("utf-8")
        try:
            await asyncio.to_thread(_write_transport, session, cmd_bytes)
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

    with session.lock:
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
    newline: str = "\r\n",
    max_wait_seconds: float = 5.0,
    prompt_pattern: Optional[str] = None,
) -> TelnetSendInputOutput:
    """Send interactive input or keystrokes to an active Telnet session."""
    session = await _session_store.get(session_id)
    if session is None or not isinstance(session, TelnetSession):
        raise ValueError(f"No active Telnet session found with ID '{session_id}'")

    with session.lock:
        start_index = len(session.accumulated_output)
        session.current_cmd_start_index = start_index

    payload = input_text.encode("utf-8") if raw else (input_text + newline).encode("utf-8")
    await asyncio.to_thread(_write_transport, session, payload)

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
    newline: str = "\r\n",
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
        with session.lock:
            cmd_start_index = len(session.accumulated_output)
            session.current_cmd_start_index = cmd_start_index

        cmd_bytes = (cmd + newline).encode("utf-8")
        try:
            await asyncio.to_thread(_write_transport, session, cmd_bytes, True)
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

    with session.lock:
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
    newline: str = "\r\n",
    max_wait_seconds: float = 5.0,
    prompt_pattern: Optional[str] = None,
) -> SerialSendInputOutput:
    """Send interactive input, passwords, or raw keystrokes to an active Serial session."""
    session = await _session_store.get(session_id)
    if session is None or not isinstance(session, SerialSession):
        raise ValueError(f"No active Serial session found with ID '{session_id}'")

    with session.lock:
        start_index = len(session.accumulated_output)
        session.current_cmd_start_index = start_index

    payload = input_text.encode("utf-8") if raw else (input_text + newline).encode("utf-8")
    await asyncio.to_thread(_write_transport, session, payload, True)

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

    # Raw socket:// does not support RS-232 BREAK over TCP
    if session.port.startswith("socket://"):
        return SerialSendBreakOutput(
            session_id=session_id,
            success=False,
            prompt_matched=None,
            output="",
            message=(
                "Failed sending break condition: session is connected via raw TCP socket ('socket://'). "
                "Raw TCP sockets cannot transmit RS-232 BREAK signals across the network. "
                "To send BREAK over the network, enable RFC 2217 on ser2net ('accepter: telnet(rfc2217)...') "
                "and connect using 'rfc2217://host:port'."
            ),
        )

    with session.lock:
        start_index = len(session.accumulated_output)
        session.current_cmd_start_index = start_index

    try:
        def _send_break() -> None:
            with session.write_lock:
                session.serial.send_break(duration=duration)

        await asyncio.to_thread(_send_break)
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
        with s.lock:
            total_bytes = s.total_bytes_received
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
                total_bytes_received=total_bytes,
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
                total_bytes_received=total_bytes,
            )

    return SessionListResponse(
        active_sessions=len(sessions_dict),
        sessions=sessions_dict,
    )


@tool(name="telnet_list_sessions")
async def telnet_list_sessions() -> SessionListResponse:
    """Deprecated alias for list_sessions."""
    return await list_sessions()


# ============================================================================
# Terminal Screen / Visual Emulation Tools
# ============================================================================


KEY_SEQUENCES: dict[str, bytes] = {
    # Cursor / Arrow keys
    "UP": b"\x1b[A",
    "DOWN": b"\x1b[B",
    "RIGHT": b"\x1b[C",
    "LEFT": b"\x1b[D",

    # Common control keys
    "ENTER": b"\r",
    "RETURN": b"\r",
    "CR": b"\r",
    "LF": b"\n",
    "TAB": b"\t",
    "BACKSPACE": b"\x7f",
    "DELETE": b"\x1b[3~",
    "ESC": b"\x1b",
    "ESCAPE": b"\x1b",
    "SPACE": b" ",

    # DEC VT Editing keypad
    "FIND": b"\x1b[1~",
    "INSERT": b"\x1b[2~",
    "INSERT_HERE": b"\x1b[2~",
    "REMOVE": b"\x1b[3~",
    "SELECT": b"\x1b[4~",
    "PREV": b"\x1b[5~",
    "PREV_SCREEN": b"\x1b[5~",
    "PAGE_UP": b"\x1b[5~",
    "NEXT": b"\x1b[6~",
    "NEXT_SCREEN": b"\x1b[6~",
    "PAGE_DOWN": b"\x1b[6~",

    # DEC VT Numeric keypad (PF1-PF4)
    "PF1": b"\x1bOP",
    "PF2": b"\x1bOQ",
    "PF3": b"\x1bOR",
    "PF4": b"\x1bOS",
    "F1": b"\x1bOP",
    "F2": b"\x1bOQ",
    "F3": b"\x1bOR",
    "F4": b"\x1bOS",

    # DEC VT Function keys (F6-F20)
    "F6": b"\x1b[17~",
    "F7": b"\x1b[18~",
    "F8": b"\x1b[19~",
    "F9": b"\x1b[20~",
    "F10": b"\x1b[21~",
    "F11": b"\x1b[23~",
    "F12": b"\x1b[24~",
    "F13": b"\x1b[25~",
    "F14": b"\x1b[26~",
    "HELP": b"\x1b[28~",
    "F15": b"\x1b[28~",
    "DO": b"\x1b[29~",
    "F16": b"\x1b[29~",
    "F17": b"\x1b[31~",
    "F18": b"\x1b[32~",
    "F19": b"\x1b[33~",
    "F20": b"\x1b[34~",
}

# Add CTRL_A through CTRL_Z
for _i in range(1, 27):
    _ch = chr(ord("A") + _i - 1)
    KEY_SEQUENCES[f"CTRL_{_ch}"] = bytes([_i])
    KEY_SEQUENCES[f"CTRL-{_ch}"] = bytes([_i])


def _extract_screen_output(session: Union[TelnetSession, SerialSession]) -> TerminalScreenOutput:
    """Render 2D screen text and annotate reverse-video highlighted cells."""
    screen = session.screen
    if screen is None:
        return TerminalScreenOutput(
            session_id=session.session_id,
            rows=session.rows,
            cols=session.cols,
            cursor_row=1,
            cursor_col=1,
            cursor_visible=True,
            cursor_char=" ",
            screen_text="",
            annotated_text="",
            highlighted_lines=[],
        )

    screen_lines = [line.rstrip() for line in screen.display]
    annotated_lines = []
    highlighted_lines = []

    for y in range(screen.lines):
        line_chars = [screen.buffer[y][x] for x in range(screen.columns)]
        line_str = ""
        in_rev = False
        rev_buf = ""
        has_rev = False

        for ch in line_chars:
            if ch.reverse:
                has_rev = True
                if not in_rev:
                    in_rev = True
                    rev_buf = ch.data
                else:
                    rev_buf += ch.data
            else:
                if in_rev:
                    in_rev = False
                    line_str += f"* [{rev_buf}] *"
                    rev_buf = ""
                line_str += ch.data

        if in_rev:
            line_str += f"* [{rev_buf}] *"

        if has_rev:
            highlighted_lines.append(y + 1)
        annotated_lines.append(line_str.rstrip())

    cursor_char = None
    try:
        cursor_char = screen.buffer[screen.cursor.y][screen.cursor.x].data
    except Exception:
        pass

    return TerminalScreenOutput(
        session_id=session.session_id,
        rows=screen.lines,
        cols=screen.columns,
        cursor_row=screen.cursor.y + 1,
        cursor_col=screen.cursor.x + 1,
        cursor_visible=not screen.cursor.hidden,
        cursor_char=cursor_char,
        screen_text="\n".join(screen_lines),
        annotated_text="\n".join(annotated_lines),
        highlighted_lines=highlighted_lines,
    )


def _screen_sig(out: TerminalScreenOutput) -> tuple:
    """What counts as 'the screen changed': text, reverse video, cursor place and visibility."""
    return (out.screen_text, out.annotated_text, out.cursor_row, out.cursor_col, out.cursor_visible)


def _read_screen(session: Union[TelnetSession, SerialSession]) -> TerminalScreenOutput:
    """Render the screen for a client under the state lock, and remember it as the
    baseline terminal_wait_for_change compares against."""
    with session.lock:
        out = _extract_screen_output(session)
        session.last_read_sig = _screen_sig(out)
    return out


def _clamp_wait_timeout(timeout_seconds: float) -> float:
    """Clamp a wait to [0, WAIT_FOR_CHANGE_MAX_TIMEOUT]; NaN counts as 0."""
    t = float(timeout_seconds)
    if t != t:
        return 0.0
    return min(max(t, 0.0), TerminalDefaults.WAIT_FOR_CHANGE_MAX_TIMEOUT)


@tool(name="terminal_get_screen")
async def terminal_get_screen(
    session_id: str,
    wait_seconds: float = 0.0,
) -> TerminalScreenOutput:
    """Inspect the rendered 2D terminal canvas, cursor position, and highlighted reverse-video text.

    The screen is always current: a background thread reads the session as bytes arrive,
    so wait_seconds=0 shows everything received so far. To wait for the screen to change,
    use terminal_wait_for_change."""
    session = await _session_store.get(session_id)
    if session is None:
        raise ValueError(f"No active session found with ID '{session_id}'")

    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)

    return _read_screen(session)


@tool(name="terminal_send_key")
async def terminal_send_key(
    session_id: str,
    key: str,
    wait_seconds: float = 0.5,
) -> TerminalSendKeyOutput:
    """Send a keyboard key or VT escape sequence (e.g. UP, DOWN, TAB, ENTER, ESC, CTRL_Z) to a terminal session."""
    session = await _session_store.get(session_id)
    if session is None:
        raise ValueError(f"No active session found with ID '{session_id}'")

    key_normalized = key.strip()
    key_upper = key_normalized.upper()
    if key_upper in KEY_SEQUENCES:
        payload = KEY_SEQUENCES[key_upper]
    elif len(key_normalized) == 1:
        payload = key_normalized.encode("utf-8")
    else:
        # Fallback: interpret as utf-8 literal sequence
        payload = key_normalized.encode("utf-8")

    if isinstance(session, (TelnetSession, SerialSession)):
        await asyncio.to_thread(_write_transport, session, payload)
    else:
        raise ValueError(f"Unsupported session type for session '{session_id}'")

    if session.logger:
        session.logger.log_input(session_id=session_id, input_text=f"KEY:{key_normalized}")

    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)

    screen_output = _read_screen(session)

    return TerminalSendKeyOutput(
        session_id=session_id,
        key_sent=key,
        bytes_sent=repr(payload),
        success=True,
        message=f"Key '{key}' ({repr(payload)}) sent to session '{session_id}'.",
        screen_text=screen_output.screen_text,
        annotated_text=screen_output.annotated_text,
        cursor_row=screen_output.cursor_row,
        cursor_col=screen_output.cursor_col,
        cursor_visible=screen_output.cursor_visible,
    )


@tool(name="terminal_wait_for_change")
async def terminal_wait_for_change(
    session_id: str,
    timeout_seconds: float = 30.0,
    settle_seconds: float = 0.3,
    pattern: Optional[str] = None,
) -> TerminalWaitForChangeOutput:
    """Wait, in one call, for the terminal screen to change, then return it.

    Returns as soon as the screen differs from the last screen this server returned for
    the session (terminal_get_screen, terminal_send_key or this call) AND no byte has
    arrived for settle_seconds (default 0.3; a full repaint at 9600 baud takes about 2 s).
    With pattern (a Python regular expression searched in screen_text) it instead returns
    once the pattern is on the screen and the screen has settled, changed or not.
    Otherwise it returns the current screen at timeout_seconds (capped at 300) with
    timed_out true. It also returns at once if the session ends."""
    session = await _session_store.get(session_id)
    if session is None:
        raise ValueError(f"No active session found with ID '{session_id}'")

    compiled = re.compile(pattern) if pattern else None  # a bad pattern fails before waiting
    timeout = _clamp_wait_timeout(timeout_seconds)
    settle = min(_clamp_wait_timeout(settle_seconds), timeout)
    start = time.monotonic()
    deadline = start + timeout

    with session.lock:
        baseline = session.last_read_sig
        if baseline is None:
            # No screen returned yet: the baseline is the screen as the wait starts.
            baseline = _screen_sig(_extract_screen_output(session))
        bytes_at_start = session.total_bytes_received

    out: Optional[TerminalScreenOutput] = None
    seen_seq = -1
    changed = False
    matched: Optional[str] = None
    timed_out = False
    while True:
        now = time.monotonic()
        with session.lock:
            if session.screen_seq != seen_seq or out is None:
                # Render only when the reader has fed pyte since the last look.
                seen_seq = session.screen_seq
                out = _extract_screen_output(session)
                changed = _screen_sig(out) != baseline
                if compiled is not None:
                    m = compiled.search(out.screen_text)
                    matched = m.group(0) if m else None
            quiet = now - session.last_rx_monotonic >= settle
            active = session.is_active
        done = (matched is not None) if compiled is not None else changed
        if done and quiet:
            break
        if not active:
            break
        if now >= deadline:
            timed_out = True
            break
        await asyncio.sleep(min(TerminalDefaults.WAIT_FOR_CHANGE_POLL_SECONDS, max(0.0, deadline - now)))

    with session.lock:
        session.last_read_sig = _screen_sig(out)
        bytes_received = session.total_bytes_received - bytes_at_start
        active = session.is_active

    return TerminalWaitForChangeOutput(
        session_id=session_id,
        changed=changed,
        timed_out=timed_out,
        pattern_matched=matched,
        waited_seconds=round(time.monotonic() - start, 3),
        bytes_received=bytes_received,
        session_active=active,
        rows=out.rows,
        cols=out.cols,
        cursor_row=out.cursor_row,
        cursor_col=out.cursor_col,
        cursor_visible=out.cursor_visible,
        cursor_char=out.cursor_char,
        screen_text=out.screen_text,
        annotated_text=out.annotated_text,
        highlighted_lines=out.highlighted_lines,
    )


@tool(name="terminal_resize")
async def terminal_resize(
    session_id: str,
    cols: int = 80,
    rows: int = 24,
) -> TerminalResizeOutput:
    """Dynamically resize the virtual terminal canvas and send RFC 1073 Telnet NAWS window resize negotiation."""
    session = await _session_store.get(session_id)
    if session is None:
        raise ValueError(f"No active session found with ID '{session_id}'")

    if cols <= 0 or rows <= 0:
        raise ValueError(f"Invalid terminal dimensions: cols={cols}, rows={rows}")

    naws_sent = False
    with session.lock:
        session.cols = cols
        session.rows = rows
        if session.screen:
            session.screen.resize(lines=rows, columns=cols)
            session.screen.tabstops = set(range(8, cols, 8))

    if isinstance(session, TelnetSession):
        try:
            # RFC 1073: IAC SB NAWS <width_hi> <width_lo> <height_hi> <height_lo> IAC SE
            naws_bytes = bytes([
                255, 250, 31,
                (cols >> 8) & 0xff, cols & 0xff,
                (rows >> 8) & 0xff, rows & 0xff,
                255, 240,
            ])
            sock = session.telnet.get_socket()
            if sock:
                # Raw sendall, not telnet.write(): the IAC bytes must not be escaped.
                def _send_naws() -> None:
                    with session.write_lock:
                        sock.sendall(naws_bytes)

                await asyncio.to_thread(_send_naws)
                naws_sent = True
        except Exception as e:
            logger.warning(f"Failed to transmit Telnet NAWS resize packet: {e}")

    if session.logger:
        session.logger.log_config_change(
            session_id=session_id,
            change_description=f"Terminal resized to {cols}x{rows} (NAWS: {naws_sent})",
        )

    return TerminalResizeOutput(
        session_id=session_id,
        cols=cols,
        rows=rows,
        naws_sent=naws_sent,
        success=True,
        message=f"Terminal session '{session_id}' resized to {cols} columns x {rows} rows (NAWS sent: {naws_sent}).",
    )
