# chuk_mcp_telnet_client/models.py
from typing import Any, ClassVar, Dict, FrozenSet, List, Optional, Union
from pydantic import BaseModel, Field, model_serializer


class CompactModel(BaseModel):
    """Result model that leaves out fields that carry no information.

    The MCP server serializes a result with model_dump(), and every field it emits
    costs the calling agent tokens. A subclass lists in OMIT_IF_NONE the optional
    fields that are dropped from the result while they are None (for example
    raw_base64 when the caller did not ask for it). Booleans, counts and the
    primary text of a result are never listed, so a False, a 0 or an empty reply
    is always stated explicitly.
    """

    OMIT_IF_NONE: ClassVar[FrozenSet[str]] = frozenset()

    @model_serializer(mode="wrap")
    def _omit_unset(self, handler):
        data = handler(self)
        for name in self.OMIT_IF_NONE:
            if data.get(name, False) is None:
                del data[name]
        return data


# ============================================================================
# Telnet Models
# ============================================================================


class TelnetClientInput(BaseModel):
    host: str = Field(..., description="Host or IP address of the Telnet server.")
    port: int = Field(..., description="Port on which the Telnet server is listening.")
    commands: List[str] = Field(..., description="Commands to send sequentially.")
    newline: str = Field("\r\n", description="Line terminator to append to commands (default: '\\r\\n').")


class CommandResponse(CompactModel):
    OMIT_IF_NONE = frozenset({"raw_base64"})

    command: str
    response: str
    raw_base64: Optional[str] = None


class TelnetClientOutput(CompactModel):
    OMIT_IF_NONE = frozenset({"initial_banner_raw_base64", "final_output_raw_base64"})

    server_version: str = "0.8.0"
    host: str
    port: int
    initial_banner: str
    initial_banner_raw_base64: Optional[str] = None
    final_output: str = ""
    final_output_raw_base64: Optional[str] = None
    responses: List[CommandResponse]
    session_id: str
    session_active: bool
    command_completed: bool = True
    prompt_matched: Optional[str] = None
    log_file: Optional[str] = None


class TelnetReadSessionOutput(CompactModel):
    OMIT_IF_NONE = frozenset({"raw_base64"})

    session_id: str
    host: str
    port: int
    output: str
    raw_base64: Optional[str] = None
    new_bytes_count: int
    total_bytes_received: int
    command_completed: bool
    prompt_matched: Optional[str] = None
    session_active: bool
    log_file: Optional[str] = None


class TelnetSendInputOutput(CompactModel):
    OMIT_IF_NONE = frozenset({"raw_base64"})

    session_id: str
    input_sent: str
    response: str
    raw_base64: Optional[str] = None
    command_completed: bool
    prompt_matched: Optional[str] = None
    session_active: bool
    log_file: Optional[str] = None


# ============================================================================
# Serial Models
# ============================================================================


class SerialClientInput(BaseModel):
    port: str = Field(..., description="Serial port device path (e.g. /dev/ttyUSB0) or network URL (rfc2217://host:port, socket://host:port, or host:port).")
    commands: List[str] = Field(..., description="Commands to send sequentially.")
    baudrate: int = Field(9600, description="Serial baud rate (default: 9600).")
    bytesize: int = Field(8, description="Data bit size (default: 8).")
    parity: str = Field("N", description="Parity: N (None), E (Even), O (Odd), M (Mark), S (Space).")
    stopbits: float = Field(1.0, description="Stop bits: 1, 1.5, or 2 (default: 1.0).")
    rtscts: bool = Field(False, description="Enable hardware RTS/CTS flow control.")
    xonxoff: bool = Field(False, description="Enable software XON/XOFF flow control.")
    newline: str = Field("\r\n", description="Line terminator to append to commands (default: '\\r\\n', use '\\r' for VMS serial console).")


class SerialClientOutput(CompactModel):
    OMIT_IF_NONE = frozenset({"initial_banner_raw_base64", "final_output_raw_base64"})

    server_version: str = "0.8.0"
    port: str
    baudrate: int
    initial_banner: str
    initial_banner_raw_base64: Optional[str] = None
    final_output: str = ""
    final_output_raw_base64: Optional[str] = None
    responses: List[CommandResponse]
    session_id: str
    session_active: bool
    command_completed: bool = True
    prompt_matched: Optional[str] = None
    log_file: Optional[str] = None


class SerialReadSessionOutput(CompactModel):
    OMIT_IF_NONE = frozenset({"raw_base64"})

    session_id: str
    port: str
    baudrate: int
    output: str
    raw_base64: Optional[str] = None
    new_bytes_count: int
    total_bytes_received: int
    command_completed: bool
    prompt_matched: Optional[str] = None
    session_active: bool
    log_file: Optional[str] = None


class SerialSendInputOutput(CompactModel):
    OMIT_IF_NONE = frozenset({"raw_base64"})

    session_id: str
    input_sent: str
    response: str
    raw_base64: Optional[str] = None
    command_completed: bool
    prompt_matched: Optional[str] = None
    session_active: bool
    log_file: Optional[str] = None


class SerialSendBreakOutput(CompactModel):
    OMIT_IF_NONE = frozenset({"raw_base64"})

    session_id: str
    success: bool
    prompt_matched: Optional[str] = None
    output: str
    raw_base64: Optional[str] = None
    message: str


class SerialSetSpeedOutput(BaseModel):
    session_id: str
    old_baudrate: int
    new_baudrate: int
    framing: str
    success: bool
    message: str


class SerialPortInfo(BaseModel):
    device: str
    description: str
    hwid: str
    available: bool


class SerialListPortsOutput(BaseModel):
    total_ports: int
    ports: List[SerialPortInfo]


# ============================================================================
# Unified Session Models
# ============================================================================


class SessionInfo(BaseModel):
    """Information about an active Telnet or Serial session."""

    session_id: str
    session_type: str  # "telnet" or "serial"
    target: str  # e.g. "127.0.0.1:10023" or "/dev/ttyUSB0 (9600 8N1)"
    host: Optional[str] = None
    port: Optional[Union[int, str]] = None
    baudrate: Optional[int] = None
    created_at: float
    age_seconds: float
    is_active: bool
    total_bytes_received: int
    server_version: str = "0.8.0"


class SessionListResponse(BaseModel):
    """Response for listing all active sessions."""

    server_version: str = "0.8.0"
    active_sessions: int
    sessions: Dict[str, SessionInfo]
    note: str = (
        "Active connections shown. Sessions persist and stream output continuously in the background."
    )


class SessionCloseResponse(CompactModel):
    OMIT_IF_NONE = frozenset({"raw_base64"})

    """Final unread stream delivery at the explicit close boundary."""
    output: str = ""
    raw_base64: Optional[str] = None

    success: bool
    message: str


# ============================================================================
# Terminal Screen / Visual Emulation Models
# ============================================================================


class TerminalScreenOutput(CompactModel):
    OMIT_IF_NONE = frozenset({"annotated_text"})

    """Rendered 2D screen text matrix and visual attributes from terminal emulation."""

    session_id: str
    rows: int
    cols: int
    cursor_row: int
    cursor_col: int
    cursor_visible: bool = True
    cursor_char: Optional[str] = None
    screen_text: str
    annotated_text: Optional[str] = Field(None, description="Screen text with reverse-video runs marked * [like this] *; omitted when identical to screen_text.")
    highlighted_lines: List[int]
    server_version: str = "0.8.0"


class TerminalWaitForChangeOutput(CompactModel):
    OMIT_IF_NONE = frozenset({"annotated_text"})

    """Result of terminal_wait_for_change: why the wait ended, and the screen at that moment."""

    session_id: str
    changed: bool = Field(..., description="The screen differs from the last screen returned for this session.")
    timed_out: bool = Field(..., description="The wait ended at timeout_seconds, its condition not met.")
    pattern_matched: Optional[str] = Field(None, description="The text the pattern matched on the returned screen.")
    waited_seconds: float
    bytes_received: int = Field(..., description="Bytes that arrived during the wait.")
    session_active: bool
    rows: int
    cols: int
    cursor_row: int
    cursor_col: int
    cursor_visible: bool = True
    cursor_char: Optional[str] = None
    screen_text: str
    annotated_text: Optional[str] = Field(None, description="Screen text with reverse-video runs marked * [like this] *; omitted when identical to screen_text.")
    highlighted_lines: List[int]
    server_version: str = "0.8.0"


class TerminalSendKeyOutput(CompactModel):
    OMIT_IF_NONE = frozenset({"annotated_text"})

    """Result of sending a virtual key or escape sequence to a terminal session."""

    session_id: str
    key_sent: str
    bytes_sent: str
    success: bool
    message: str
    screen_text: Optional[str] = None
    annotated_text: Optional[str] = None
    cursor_row: Optional[int] = None
    cursor_col: Optional[int] = None
    cursor_visible: Optional[bool] = None
    server_version: str = "0.8.0"


class TerminalResizeOutput(BaseModel):
    """Result of dynamically resizing terminal dimensions."""

    session_id: str
    cols: int
    rows: int
    naws_sent: bool
    success: bool
    message: str
    server_version: str = "0.8.0"

