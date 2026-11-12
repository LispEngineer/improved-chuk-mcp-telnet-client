"""
Terminal client tools (Telnet & Serial) for MCP server.

Provides async Telnet and Serial connectivity with background stream buffering,
long-running process polling without timeouts, dynamic baud rate switching,
clean transcript disk logging without overwriting, and polymorphic multi-session management.
"""

import asyncio
import base64
import codecs
import inspect
import logging
import queue
import re
import select
import socket
import telnetlib  # nosec B401 - telnet is part of this MCP server
import threading
import time
import uuid
import weakref
from functools import wraps
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Union

import pyte
import serial
import serial.rfc2217
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
    pending_output: bytearray = field(default_factory=bytearray)
    screen_decoder: object = field(default_factory=lambda: codecs.getincrementaldecoder("utf-8")("replace"))
    output_decoder: object = field(default_factory=lambda: codecs.getincrementaldecoder("utf-8")("replace"))
    transaction_bytes: Optional[bytearray] = None
    is_active: bool = True
    last_activity: float = field(default_factory=time.time)
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
    pending_output: bytearray = field(default_factory=bytearray)
    screen_decoder: object = field(default_factory=lambda: codecs.getincrementaldecoder("utf-8")("replace"))
    output_decoder: object = field(default_factory=lambda: codecs.getincrementaldecoder("utf-8")("replace"))
    transaction_bytes: Optional[bytearray] = None
    is_active: bool = True
    last_activity: float = field(default_factory=time.time)
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
    with session.lock:
        text = session.screen_decoder.decode(data)
        # Publish to stream consumers only after the transcript append completes.
        if session.logger:
            session.logger.log_stream_chunk(session.session_id, text, at=arrived_at, raw=data)
        session.pending_output.extend(data)
        session.total_bytes_received += len(data)
        session.last_activity = time.time()
        session.last_rx_monotonic = time.monotonic()
        session.screen_seq += 1
        if session.stream:
            try:
                session.stream.feed(text)
            except Exception as e:
                logger.warning(f"Error feeding {session.session_type} text to pyte stream: {e}")


def _finish_reader(session):
    with session.lock:
        text = session.screen_decoder.decode(b"", final=True)
        if text and session.stream:
            session.stream.feed(text)
        session.is_active = False
        if text and session.logger:
            session.logger.log_stream_chunk(session.session_id, text)


def _read_telnet_payload(tn):
    """Recover cooked payload if a later recv in read_very_eager hits reset/EOF."""
    try:
        return tn.read_very_eager(), False
    except (OSError, EOFError):
        tn.process_rawq()
        try:
            data = tn.read_very_lazy()
        except EOFError:
            data = b""
        return data, True


def _telnet_reader_thread(session: "TelnetSession") -> None:
    """Read the telnet socket whenever bytes arrive, until EOF or a stop request."""
    try:
        sock = session.telnet.get_socket()
        while not session.stop_event.is_set():
            try:
                # Wakes on data or EOF; a close request is checked within the poll interval.
                readable, _, _ = select.select([sock], [], [], TerminalDefaults.READER_POLL_SECONDS)
            except (OSError, ValueError):
                break  # the socket was closed under us
            if not readable:
                continue
            arrived_at = time.time()
            session.stop_event.wait(TerminalDefaults.READER_COALESCE_SECONDS)
            try:
                # read_very_eager() does not block. telnetlib answers option negotiation
                # with its own sendall() inside it, hence the write lock.
                with session.write_lock:
                    data, ended = _read_telnet_payload(session.telnet)
            except (OSError, EOFError, AttributeError):
                logger.info(f"Telnet connection closed for session {session.session_id}")
                break
            if data:
                _answer_vt_queries(session, data)
                _ingest_chunk(session, data, arrived_at)
            if ended:
                break
    except Exception as e:
        logger.error(f"Background telnet reader crashed for {session.session_id}: {e}")
    finally:
        if not session.stop_event.is_set():
            _finish_reader(session)


def _read_serial_payload(ser, size):
    """Drain RFC2217's receive queue even after its protocol thread reaches EOF.

    pyserial 3.5 read(n) tests thread liveness before every queued byte and may
    raise while payload remains buffered. Only our reader consumes this queue.
    Local ports and socket:// continue through their public read interface.
    """
    if isinstance(ser, serial.rfc2217.Serial):
        data = bytearray()
        for _ in range(size):
            try:
                part = ser._read_buffer.get_nowait()
            except queue.Empty:
                break
            if part is not None:
                data.extend(part)
        if data:
            return bytes(data)
    return ser.read(size)


def _serial_reader_thread(session: "SerialSession") -> None:
    """Read the serial port whenever bytes arrive, until it fails or a stop request."""
    ser = session.serial
    try:
        while not session.stop_event.is_set():
            try:
                # Blocks for at most the port's timeout (0.1 s, set at open).
                in_wait = getattr(ser, "in_waiting", 0)
                data = _read_serial_payload(ser, max(1, in_wait or 1))
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
                    data += _read_serial_payload(ser, more)
            except Exception:
                pass  # the next read reports a failed port
            _answer_vt_queries(session, data)
            _ingest_chunk(session, data, arrived_at)
    except Exception as e:
        logger.error(f"Background serial reader crashed for {session.session_id}: {e}")
    finally:
        if not session.stop_event.is_set():
            _finish_reader(session)


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
    """Stop/join, drain OS-buffered payload, then close. A busy reader requires retry.

    No shutdown discards queued TCP bytes. A stop wakes the reader's coalescing
    wait; select times out within 0.1 s and serial cancel_read wakes its read.
    """
    session.stop_event.set()
    if isinstance(session, SerialSession):
        try:
            cancel = getattr(session.serial, "cancel_read", None)
            if cancel is not None:
                cancel()
        except Exception:
            pass

    thread = session.reader_thread
    if thread is not None and thread is not threading.current_thread():
        thread.join(TerminalDefaults.READER_JOIN_SECONDS)
        if thread.is_alive():
            raise RuntimeError(
                f"Reader for {session.session_id} is still draining; retry close. "
                "Session and unread output have been retained."
            )

    # Include payload already buffered by the OS when close was requested.
    try:
        if isinstance(session, TelnetSession):
            with session.write_lock:
                data, ended = _read_telnet_payload(session.telnet)
        else:
            n = session.serial.in_waiting
            data = _read_serial_payload(session.serial, n) if n else b""
        if data:
            _ingest_chunk(session, data, time.time())
    except (OSError, EOFError, serial.SerialException, TypeError):
        pass
    _finish_reader(session)

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

        Session IDs cannot be replaced implicitly: unread output must first be
        drained by a read or the explicit close reply.
        """
        old = self._sessions.get(session.session_id)
        if old is not None and old is not session:
            raise ValueError("Close the existing session before reusing its ID")
        self._sessions[session.session_id] = session
        _start_reader(session)

    async def get(self, session_id: str) -> Optional[Union[TelnetSession, SerialSession]]:
        """Retrieve a session by ID."""
        return self._sessions.get(session_id)

    async def delete(self, session_id: str) -> None:
        """Delete a session, stop its background reader, and close port/socket."""
        session = self._sessions.get(session_id)
        if session:
            await asyncio.to_thread(_stop_session_io, session)
            self._sessions.pop(session_id, None)

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


class PayloadTelnet(telnetlib.Telnet):
    """Telnet negotiation with verbatim payload, including NUL and XON.

    Unlike stdlib process_rawq, never blocks to finish a fragmented IAC and
    never silently filters payload control bytes. IAC IAC is one payload FF.
    """

    def _notify_option(self, command, option):
        try:
            if self.option_callback:
                self.option_callback(self.sock, command, option)
            elif option != telnetlib.NOOPT:
                reject = telnetlib.WONT if command in (telnetlib.DO, telnetlib.DONT) else telnetlib.DONT
                self.sock.sendall(telnetlib.IAC + reject + option)
        except OSError as error:
            # Receive payload remains useful even if the peer stopped receiving.
            logger.warning("Telnet negotiation reply failed: %s", type(error).__name__)

    def process_rawq(self):
        cooked = bytearray()
        sub = bytearray()
        raw = self.rawq[self.irawq:]
        self.rawq = b""
        self.irawq = 0
        for value in raw:
            c = bytes([value])
            if not self.iacseq:
                if c == telnetlib.IAC:
                    self.iacseq = c
                else:
                    (sub if self.sb else cooked).append(value)
            elif len(self.iacseq) == 1:
                if c in (telnetlib.DO, telnetlib.DONT, telnetlib.WILL, telnetlib.WONT):
                    self.iacseq += c
                    continue
                self.iacseq = b""
                if c == telnetlib.IAC:
                    (sub if self.sb else cooked).append(value)
                else:
                    if c == telnetlib.SB:
                        self.sb = 1
                        self.sbdataq = b""
                    elif c == telnetlib.SE:
                        self.sb = 0
                        self.sbdataq += bytes(sub)
                        sub.clear()
                    self._notify_option(c, telnetlib.NOOPT)
            else:
                cmd = self.iacseq[1:2]
                self.iacseq = b""
                self._notify_option(cmd, c)
        self.cookedq += bytes(cooked)
        self.sbdataq += bytes(sub)


async def _connect_telnet(host: str, port: int) -> tuple[telnetlib.Telnet, str]:
    """Open only; the one background reader owns every subsequent read."""
    tn = PayloadTelnet()
    tn.set_option_negotiation_callback(_create_negotiation_callback())
    opening = asyncio.create_task(asyncio.to_thread(tn.open, host, port, TerminalDefaults.CONNECTION_TIMEOUT))
    try:
        await asyncio.shield(opening)
    except BaseException:
        try:
            await opening
        finally:
            tn.close()
        raise
    return tn, ""


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


async def _open_serial_safely(**kwargs):
    opening = asyncio.create_task(asyncio.to_thread(serial.serial_for_url, **kwargs))
    try:
        return await asyncio.shield(opening)
    except asyncio.CancelledError:
        try:
            opened = await opening
        except Exception:
            pass
        else:
            opened.close()
        raise


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
            ser = await _open_serial_safely(
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

    return ser, ""


class StreamText(str):
    """Readable incremental UTF-8 view plus this delivery's authoritative bytes."""
    def __new__(cls, text, raw=b""):
        obj = super().__new__(cls, text)
        obj.raw = raw
        return obj

    @property
    def base64(self):
        return base64.b64encode(self.raw).decode("ascii")


def _take_pending(session, length=None, final=False):
    """Caller holds session.lock. No await between dequeue and result assembly."""
    length = len(session.pending_output) if length is None else length
    raw = bytes(session.pending_output[:length])
    del session.pending_output[:length]
    if session.transaction_bytes is not None:
        session.transaction_bytes.extend(raw)
    text = session.output_decoder.decode(raw, final=final)
    if final:
        # Repeated EOF reads/close must be harmless.
        session.output_decoder = codecs.getincrementaldecoder("utf-8")("replace")
    return StreamText(text, raw)


async def _close_and_drain(session):
    # Await shutdown first: a cancelled close retains a drainable session in store.
    await asyncio.to_thread(_stop_session_io, session)
    if session.logger:
        session.logger.log_session_end(session.session_id, total_bytes=session.total_bytes_received)
    with session.lock:
        out = _take_pending(session, final=True)
    _session_store._sessions.pop(session.session_id, None)
    return out


_operation_locks = weakref.WeakValueDictionary()


def _stream_operation(id_argument="session_id"):
    """Serialize a session's consuming calls and restore output on cancellation.

    The lock is separate from reader/write locks and never blocks screen calls.
    It also covers the whole command batch, so cancelling its delay cannot drop
    earlier batch responses that have not yet reached the caller.
    """
    def decorate(func):
        signature = inspect.signature(func)
        @wraps(func)
        async def wrapped(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            sid = bound.arguments[id_argument]
            if sid is None:
                sid = f"{id_argument}_{uuid.uuid4().hex}"
                bound.arguments[id_argument] = sid
            pattern = bound.arguments.get("prompt_pattern")
            if pattern:
                re.compile(pattern)  # reject invalid expressions before sending input
            lock = _operation_locks.setdefault(sid, asyncio.Lock())
            async with lock:
                session = await _session_store.get(sid)
                state = session.output_decoder.getstate() if session else (b"", 0)
                if session:
                    session.transaction_bytes = bytearray()
                try:
                    return await func(*bound.args, **bound.kwargs)
                except BaseException:
                    session = await _session_store.get(sid)
                    if session and session.transaction_bytes is not None:
                        with session.lock:
                            session.pending_output[:0] = session.transaction_bytes
                            if state is not None:
                                session.output_decoder.setstate(state)
                    raise
                finally:
                    session = await _session_store.get(sid)
                    if session:
                        session.transaction_bytes = None
        return wrapped
    return decorate


def _prompt_text_offset(text: str, byte_offset: int) -> int:
    """First full-text character boundary at/after a reversible byte boundary.

    Decoding only the old prefix can turn an incomplete UTF-8 character into
    several surrogateescaped characters, unlike decoding the full pending text.
    Count byte widths in that full text instead; malformed bytes each count once.
    """
    if byte_offset <= 0:
        return 0
    byte_end = 0
    for index, character in enumerate(text, 1):
        byte_end += len(character.encode("utf-8", errors="surrogateescape"))
        if byte_end >= byte_offset:
            return index
    return len(text)


async def _wait_for_output(
    session: Union[TelnetSession, SerialSession],
    start_index: int = 0,
    max_wait_seconds: float = TerminalDefaults.MAX_WAIT_SECONDS,
    prompt_pattern: Optional[str] = None,
    idle_timeout: Optional[float] = None,
) -> tuple[str, bool, Optional[str]]:
    """Deliver the oldest pending prefix, retaining everything after a prompt.

    start_index limits prompt matching to newly received output; delivery still
    starts at the oldest pending byte, including bytes before that index.
    Caller serializes consumers; all queue decisions/dequeues share the reader lock.
    """
    compiled = re.compile(prompt_pattern) if prompt_pattern else None
    deadline = time.monotonic() + max(0.0, max_wait_seconds)
    while True:
        with session.lock:
            pending = bytes(session.pending_output)
            # surrogateescape makes character offsets reversible to payload offsets;
            # these private surrogate characters are never exposed through JSON.
            current = pending.decode("utf-8", errors="surrogateescape")
            offset = _prompt_text_offset(current, start_index) if compiled else 0
            match = compiled.search(current, offset) if compiled else None
            if match:
                length = len(current[:match.end()].encode("utf-8", errors="surrogateescape"))
                # Only byte-offset arithmetic uses surrogateescape. Prompt metadata
                # must be valid Unicode before the MCP serializer sees the result.
                matched_prompt = match.group(0).encode("utf-8", errors="surrogateescape").decode(
                    "utf-8", errors="replace"
                )
                return _take_pending(session, length), True, matched_prompt
            if not session.is_active:
                return _take_pending(session, final=True), True, None
            if idle_timeout is not None and pending and time.monotonic() - session.last_rx_monotonic >= idle_timeout:
                return _take_pending(session), True, None
            if time.monotonic() >= deadline:
                return _take_pending(session), False, None
        await asyncio.sleep(min(0.02, max(0.0, deadline - time.monotonic())))


# ============================================================================
# Telnet MCP Tools
# ============================================================================


@tool(name="telnet_client")
@_stream_operation("telnet_session_id")
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

    if session is not None and not session.is_active:
        raise ValueError("Session ended; read remaining output and close it before reconnecting")
    if session is None:
        tn, initial_banner = await _connect_telnet(host, port)
        session = TelnetSession(
            telnet=tn,
            host=host,
            port=port,
            created_at=time.time(),
            session_id=actual_session_id,
            logger=session_logger,
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

        session.transaction_bytes = bytearray()
        banner, _, _ = await _wait_for_output(
            session, max_wait_seconds=response_wait, prompt_pattern=prompt_pattern,
            idle_timeout=0.1,
        )
        initial_banner = banner
    else:
        if session.logger is None and session_logger:
            with session.lock:
                session_logger.log_session_start(
                    session_id=actual_session_id, target=session.target,
                    protocol=session.session_type.upper(), timestamp_chunks=timestamp_chunks,
                )
                session.logger = session_logger
        elif session.logger and timestamp_chunks:
            session.logger._timestamp_mode[actual_session_id] = True

    responses: list[CommandResponse] = []
    all_completed = True
    last_matched_prompt: Optional[str] = None

    for i, cmd in enumerate(commands):
        with session.lock:
            cmd_start_index = len(session.pending_output)

        cmd_bytes = (cmd + newline).encode("utf-8")
        try:
            await asyncio.to_thread(_write_transport, session, cmd_bytes)
        except Exception as e:
            logger.error(f"Failed to write command to session {actual_session_id}: {type(e).__name__}")
            all_completed = False
            break

        out, completed, prompt_match = await _wait_for_output(
            session=session,
            start_index=cmd_start_index,
            max_wait_seconds=max_wait_seconds,
            prompt_pattern=prompt_pattern,
        )

        cleaned_response = out
        responses.append(CommandResponse(command=cmd, response=cleaned_response, raw_base64=out.base64))

        if not completed:
            all_completed = False
            break

        last_matched_prompt = prompt_match
        if i < len(commands) - 1:
            await asyncio.sleep(command_delay)

    log_path = session.logger.get_session_log_path(actual_session_id) if session.logger else None

    final_output = StreamText("")
    if close_session:
        final_output = await _close_and_drain(session)
        is_active = False
    else:
        is_active = session.is_active

    return TelnetClientOutput(
        host=host,
        port=port,
        initial_banner=initial_banner,
        initial_banner_raw_base64=initial_banner.base64 if isinstance(initial_banner, StreamText) else "",
        final_output=final_output,
        final_output_raw_base64=final_output.base64,
        responses=responses,
        session_id=actual_session_id,
        session_active=is_active,
        command_completed=all_completed,
        prompt_matched=last_matched_prompt,
        log_file=log_path,
    )


@tool(name="telnet_read_session")
@_stream_operation("session_id")
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

    start_index = 0

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
        new_bytes_count=len(out.raw),
        raw_base64=out.base64,
        total_bytes_received=session.total_bytes_received,
        command_completed=completed,
        prompt_matched=prompt_match,
        session_active=session.is_active,
        log_file=log_path,
    )


@tool(name="telnet_send_input")
@_stream_operation("session_id")
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
        start_index = len(session.pending_output)

    payload = input_text.encode("utf-8") if raw else (input_text + newline).encode("utf-8")
    await asyncio.to_thread(_write_transport, session, payload)


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
        raw_base64=out.base64,
        command_completed=completed,
        prompt_matched=prompt_match,
        session_active=session.is_active,
        log_file=log_path,
    )


@tool(name="telnet_close_session")
@_stream_operation("session_id")
async def telnet_close_session(session_id: str) -> SessionCloseResponse:
    """Close an active Telnet session."""
    session = await _session_store.get(session_id)
    if session:
        out = await _close_and_drain(session)
        return SessionCloseResponse(success=True, message=f"Telnet session '{session_id}' closed successfully.",
                                    output=out, raw_base64=out.base64)
    return SessionCloseResponse(success=False, message=f"No active session found with ID '{session_id}'.")


# ============================================================================
# Serial MCP Tools
# ============================================================================


@tool(name="serial_client")
@_stream_operation("serial_session_id")
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

    if session is not None and not session.is_active:
        raise ValueError("Session ended; read remaining output and close it before reconnecting")
    if session is None:
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
        session.transaction_bytes = bytearray()
        banner, _, _ = await _wait_for_output(
            session, max_wait_seconds=response_wait, prompt_pattern=prompt_pattern,
            idle_timeout=0.1,
        )
        initial_banner = banner
    else:
        if session.logger is None and session_logger:
            with session.lock:
                session_logger.log_session_start(
                    session_id=actual_session_id, target=session.target,
                    protocol=session.session_type.upper(), timestamp_chunks=timestamp_chunks,
                )
                session.logger = session_logger
        elif session.logger and timestamp_chunks:
            session.logger._timestamp_mode[actual_session_id] = True

    responses: list[CommandResponse] = []
    all_completed = True
    last_matched_prompt: Optional[str] = None

    for i, cmd in enumerate(commands):
        with session.lock:
            cmd_start_index = len(session.pending_output)

        cmd_bytes = (cmd + newline).encode("utf-8")
        try:
            await asyncio.to_thread(_write_transport, session, cmd_bytes, True)
        except Exception as e:
            logger.error(f"Failed to write command to serial session {actual_session_id}: {type(e).__name__}")
            all_completed = False
            break

        out, completed, prompt_match = await _wait_for_output(
            session=session,
            start_index=cmd_start_index,
            max_wait_seconds=max_wait_seconds,
            prompt_pattern=prompt_pattern,
        )

        cleaned_response = out
        responses.append(CommandResponse(command=cmd, response=cleaned_response, raw_base64=out.base64))

        if not completed:
            all_completed = False
            break

        last_matched_prompt = prompt_match
        if i < len(commands) - 1:
            await asyncio.sleep(command_delay)

    log_path = session.logger.get_session_log_path(actual_session_id) if session.logger else None

    final_output = StreamText("")
    if close_session:
        final_output = await _close_and_drain(session)
        is_active = False
    else:
        is_active = session.is_active

    return SerialClientOutput(
        port=port,
        baudrate=session.baudrate,
        initial_banner=initial_banner,
        initial_banner_raw_base64=initial_banner.base64 if isinstance(initial_banner, StreamText) else "",
        final_output=final_output,
        final_output_raw_base64=final_output.base64,
        responses=responses,
        session_id=actual_session_id,
        session_active=is_active,
        command_completed=all_completed,
        prompt_matched=last_matched_prompt,
        log_file=log_path,
    )


@tool(name="serial_read_session")
@_stream_operation("session_id")
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

    start_index = 0

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
        new_bytes_count=len(out.raw),
        raw_base64=out.base64,
        total_bytes_received=session.total_bytes_received,
        command_completed=completed,
        prompt_matched=prompt_match,
        session_active=session.is_active,
        log_file=log_path,
    )


@tool(name="serial_send_input")
@_stream_operation("session_id")
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
        start_index = len(session.pending_output)

    payload = input_text.encode("utf-8") if raw else (input_text + newline).encode("utf-8")
    await asyncio.to_thread(_write_transport, session, payload, True)


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
        raw_base64=out.base64,
        command_completed=completed,
        prompt_matched=prompt_match,
        session_active=session.is_active,
        log_file=log_path,
    )


@tool(name="serial_send_break")
@_stream_operation("session_id")
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
        start_index = len(session.pending_output)

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
        raw_base64=out.base64,
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
@_stream_operation("session_id")
async def serial_close_session(session_id: str) -> SessionCloseResponse:
    """Close an active Serial session."""
    session = await _session_store.get(session_id)
    if session:
        out = await _close_and_drain(session)
        return SessionCloseResponse(success=True, message=f"Serial session '{session_id}' closed successfully.",
                                    output=out, raw_base64=out.base64)
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

    # A one-character key is sent as it is: stripping it would turn a space, a TAB or a
    # CR into nothing (M1L, 2026-10-28: an agent typing a talk line lost every space).
    # A longer name is stripped of stray spaces before the lookup, as before.
    key_normalized = key if len(key) == 1 else key.strip()
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
