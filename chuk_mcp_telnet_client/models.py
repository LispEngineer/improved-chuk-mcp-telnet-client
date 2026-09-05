# chuk_mcp_telnet_client/models.py
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field


# ============================================================================
# Telnet Models
# ============================================================================


class TelnetClientInput(BaseModel):
    host: str = Field(..., description="Host or IP address of the Telnet server.")
    port: int = Field(..., description="Port on which the Telnet server is listening.")
    commands: List[str] = Field(..., description="Commands to send sequentially.")


class CommandResponse(BaseModel):
    command: str
    response: str


class TelnetClientOutput(BaseModel):
    server_version: str = "0.6.0"
    host: str
    port: int
    initial_banner: str
    responses: List[CommandResponse]
    session_id: str
    session_active: bool
    command_completed: bool = True
    prompt_matched: Optional[str] = None
    log_file: Optional[str] = None


class TelnetReadSessionOutput(BaseModel):
    session_id: str
    host: str
    port: int
    output: str
    new_bytes_count: int
    total_bytes_received: int
    command_completed: bool
    prompt_matched: Optional[str] = None
    session_active: bool
    log_file: Optional[str] = None


class TelnetSendInputOutput(BaseModel):
    session_id: str
    input_sent: str
    response: str
    command_completed: bool
    prompt_matched: Optional[str] = None
    session_active: bool
    log_file: Optional[str] = None


# ============================================================================
# Serial Models
# ============================================================================


class SerialClientInput(BaseModel):
    port: str = Field(..., description="Serial port device path (e.g. /dev/ttyUSB0).")
    commands: List[str] = Field(..., description="Commands to send sequentially.")
    baudrate: int = Field(9600, description="Serial baud rate (default: 9600).")
    bytesize: int = Field(8, description="Data bit size (default: 8).")
    parity: str = Field("N", description="Parity: N (None), E (Even), O (Odd), M (Mark), S (Space).")
    stopbits: float = Field(1.0, description="Stop bits: 1, 1.5, or 2 (default: 1.0).")
    rtscts: bool = Field(False, description="Enable hardware RTS/CTS flow control.")
    xonxoff: bool = Field(False, description="Enable software XON/XOFF flow control.")


class SerialClientOutput(BaseModel):
    server_version: str = "0.6.0"
    port: str
    baudrate: int
    initial_banner: str
    responses: List[CommandResponse]
    session_id: str
    session_active: bool
    command_completed: bool = True
    prompt_matched: Optional[str] = None
    log_file: Optional[str] = None


class SerialReadSessionOutput(BaseModel):
    session_id: str
    port: str
    baudrate: int
    output: str
    new_bytes_count: int
    total_bytes_received: int
    command_completed: bool
    prompt_matched: Optional[str] = None
    session_active: bool
    log_file: Optional[str] = None


class SerialSendInputOutput(BaseModel):
    session_id: str
    input_sent: str
    response: str
    command_completed: bool
    prompt_matched: Optional[str] = None
    session_active: bool
    log_file: Optional[str] = None


class SerialSendBreakOutput(BaseModel):
    session_id: str
    success: bool
    prompt_matched: Optional[str] = None
    output: str
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
    server_version: str = "0.6.0"


class SessionListResponse(BaseModel):
    """Response for listing all active sessions."""

    server_version: str = "0.6.0"
    active_sessions: int
    sessions: Dict[str, SessionInfo]
    note: str = (
        "Active connections shown. Sessions persist and stream output continuously in the background."
    )


class SessionCloseResponse(BaseModel):
    """Response for closing a session."""

    success: bool
    message: str


# ============================================================================
# Terminal Screen / Visual Emulation Models
# ============================================================================


class TerminalScreenOutput(BaseModel):
    """Rendered 2D screen text matrix and visual attributes from terminal emulation."""

    session_id: str
    rows: int
    cols: int
    cursor_row: int
    cursor_col: int
    cursor_visible: bool = True
    cursor_char: Optional[str] = None
    screen_text: str
    annotated_text: str
    highlighted_lines: List[int]
    server_version: str = "0.6.0"


class TerminalSendKeyOutput(BaseModel):
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
    server_version: str = "0.6.0"


class TerminalResizeOutput(BaseModel):
    """Result of dynamically resizing terminal dimensions."""

    session_id: str
    cols: int
    rows: int
    naws_sent: bool
    success: bool
    message: str
    server_version: str = "0.6.0"

