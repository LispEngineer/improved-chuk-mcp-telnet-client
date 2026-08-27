# mcp_telnet_client/models.py
from pydantic import BaseModel, Field
from typing import List, Optional


class TelnetClientInput(BaseModel):
    host: str = Field(..., description="Host or IP address of the Telnet server.")
    port: int = Field(..., description="Port on which the Telnet server is listening.")
    commands: List[str] = Field(..., description="Commands to send sequentially.")


class CommandResponse(BaseModel):
    command: str
    response: str


class TelnetClientOutput(BaseModel):
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
