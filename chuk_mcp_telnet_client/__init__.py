"""Terminal (Telnet & Serial) Client MCP Server package."""

__version__ = "0.6.0"

from chuk_mcp_telnet_client.tools import (
    telnet_client_tool as telnet_client_tool,
    telnet_close_session as telnet_close_session,
    telnet_list_sessions as telnet_list_sessions,
    telnet_read_session as telnet_read_session,
    telnet_send_input as telnet_send_input,
    serial_client_tool as serial_client_tool,
    serial_close_session as serial_close_session,
    serial_read_session as serial_read_session,
    serial_send_input as serial_send_input,
    serial_send_break as serial_send_break,
    serial_set_speed as serial_set_speed,
    serial_list_ports as serial_list_ports,
    list_sessions as list_sessions,
    terminal_get_screen as terminal_get_screen,
    terminal_send_key as terminal_send_key,
    terminal_resize as terminal_resize,
)

__all__ = [
    "telnet_client_tool",
    "telnet_close_session",
    "telnet_list_sessions",
    "telnet_read_session",
    "telnet_send_input",
    "serial_client_tool",
    "serial_close_session",
    "serial_read_session",
    "serial_send_input",
    "serial_send_break",
    "serial_set_speed",
    "serial_list_ports",
    "list_sessions",
    "terminal_get_screen",
    "terminal_send_key",
    "terminal_resize",
]
