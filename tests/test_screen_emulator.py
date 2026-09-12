"""
Unit tests for terminal screen emulation, DEC VT box drawing,
reverse video annotation, and key sequences in chuk-mcp-telnet-client.
"""

import asyncio
from unittest.mock import MagicMock

from chuk_mcp_telnet_client.models import (
    TerminalScreenOutput,
    TerminalSendKeyOutput,
    TerminalResizeOutput,
)
from chuk_mcp_telnet_client.tools import (
    TelnetSession,
    SerialSession,
    _extract_screen_output,
    _create_negotiation_callback,
    terminal_get_screen,
    terminal_send_key,
    terminal_resize,
    _session_store,
    KEY_SEQUENCES,
    TelnetCommand,
)


def test_screen_rendering_and_cursor():
    """Test standard cursor jumps, screen writes, and cursor positioning."""
    session = TelnetSession(
        telnet=MagicMock(),
        host="127.0.0.1",
        port=23,
        created_at=0.0,
        session_id="test_screen_render",
    )
    # Feed escape sequences to write at row 5, col 10
    session.stream.feed("\x1b[5;10HHello World")
    screen_out = _extract_screen_output(session)

    assert screen_out.rows == 24
    assert screen_out.cols == 80
    assert screen_out.cursor_row == 5
    assert screen_out.cursor_col == 21  # 10 + len("Hello World")
    assert screen_out.cursor_visible is True

    lines = screen_out.screen_text.split("\n")
    assert len(lines) == 24
    assert "Hello World" in lines[4]
    assert lines[4].index("Hello World") == 9  # 0-indexed column 9 is col 10


def test_dec_special_graphics_box_drawing():
    """Test that DEC VT line drawing characters are converted to Unicode box characters."""
    session = TelnetSession(
        telnet=MagicMock(),
        host="127.0.0.1",
        port=23,
        created_at=0.0,
        session_id="test_box_drawing",
    )
    # \x1b(0 activates DEC Special Graphics, \x1b(B returns to US-ASCII
    session.stream.feed("\x1b[1;1H\x1b(0lqqqk\x1b(B\r\n\x1b(0x\x1b(B   \x1b(0x\x1b(B\r\n\x1b(0mqqqj\x1b(B\r\n")
    screen_out = _extract_screen_output(session)

    lines = screen_out.screen_text.split("\n")
    assert lines[0] == "┌───┐"
    assert lines[1] == "│   │"
    assert lines[2] == "└───┘"


def test_reverse_video_annotation():
    """Test that reverse-video cells are correctly marked with * [ ... ] * and listed in highlighted_lines."""
    session = TelnetSession(
        telnet=MagicMock(),
        host="127.0.0.1",
        port=23,
        created_at=0.0,
        session_id="test_reverse_video",
    )
    # Write a menu: line 2 is normal, line 3 is highlighted, line 4 is normal
    session.stream.feed(
        "\x1b[1;1HMain Menu:\r\n"
        "  1. View Status\r\n"
        "\x1b[7m  2. Edit Profile  \x1b[0m\r\n"
        "  3. Exit System\r\n"
    )
    screen_out = _extract_screen_output(session)

    assert 3 in screen_out.highlighted_lines
    assert 2 not in screen_out.highlighted_lines
    assert 4 not in screen_out.highlighted_lines

    ann_lines = screen_out.annotated_text.split("\n")
    assert "* [  2. Edit Profile  ] *" in ann_lines[2]


def test_dynamic_resize():
    """Test dynamic resizing of the terminal emulator canvas."""
    async def _test():
        mock_telnet = MagicMock()
        mock_socket = MagicMock()
        mock_telnet.get_socket.return_value = mock_socket

        session = TelnetSession(
            telnet=mock_telnet,
            host="127.0.0.1",
            port=23,
            created_at=0.0,
            session_id="test_resize_session",
        )
        await _session_store.store(session)

        try:
            # Resize to 132 columns x 50 rows
            res = await terminal_resize(session_id="test_resize_session", cols=132, rows=50)

            assert res.success is True
            assert res.cols == 132
            assert res.rows == 50
            assert res.naws_sent is True
            assert session.cols == 132
            assert session.rows == 50
            assert session.screen.columns == 132
            assert session.screen.lines == 50

            # Verify RFC 1073 Telnet NAWS packet sent over socket
            # IAC(255) SB(250) NAWS(31) 0 132 0 50 IAC(255) SE(240)
            expected_naws = bytes([255, 250, 31, 0, 132, 0, 50, 255, 240])
            mock_socket.sendall.assert_called_with(expected_naws)
        finally:
            await _session_store.delete("test_resize_session")

    asyncio.run(_test())


def test_key_dispatch_and_send_key():
    """Test terminal_send_key with named keys and verify payload transmission."""
    async def _test():
        mock_telnet = MagicMock()
        session = TelnetSession(
            telnet=mock_telnet,
            host="127.0.0.1",
            port=23,
            created_at=0.0,
            session_id="test_key_session",
        )
        await _session_store.store(session)

        try:
            # Send DOWN arrow key
            res = await terminal_send_key(session_id="test_key_session", key="DOWN", wait_seconds=0.0)
            assert res.success is True
            assert res.key_sent == "DOWN"
            assert res.bytes_sent == repr(b"\x1b[B")
            mock_telnet.write.assert_called_with(b"\x1b[B")

            # Send CTRL_Z
            res = await terminal_send_key(session_id="test_key_session", key="CTRL_Z", wait_seconds=0.0)
            assert res.success is True
            mock_telnet.write.assert_called_with(b"\x1a")

            # Send ENTER
            res = await terminal_send_key(session_id="test_key_session", key="ENTER", wait_seconds=0.0)
            assert res.success is True
            mock_telnet.write.assert_called_with(b"\r")
        finally:
            await _session_store.delete("test_key_session")

    asyncio.run(_test())


def test_naws_negotiation_callback():
    """Test telnet option negotiation callback handles DO NAWS correctly."""
    cb = _create_negotiation_callback()
    mock_sock = MagicMock()

    # DO NAWS (opt 31)
    cb(mock_sock, TelnetCommand.DO.value, bytes([31]))
    # Must send IAC WILL NAWS, followed by 80x24 NAWS subnegotiation
    mock_sock.sendall.assert_any_call(
        TelnetCommand.IAC.value + TelnetCommand.WILL.value + bytes([31])
    )
    mock_sock.sendall.assert_any_call(
        bytes([255, 250, 31, 0, 80, 0, 24, 255, 240])
    )

    mock_sock.reset_mock()
    # DO other option (e.g. echo = 1) -> must send IAC WONT opt
    cb(mock_sock, TelnetCommand.DO.value, bytes([1]))
    mock_sock.sendall.assert_called_once_with(
        TelnetCommand.IAC.value + TelnetCommand.WONT.value + bytes([1])
    )


def test_terminal_resize_updates_tabstops():
    """Verify that resizing to wide geometry (e.g. 132 columns) updates tabstops beyond col 72."""
    async def _test():
        mock_telnet = MagicMock()
        mock_telnet.get_socket.return_value = MagicMock()
        session = TelnetSession(
            telnet=mock_telnet,
            host="127.0.0.1",
            port=23,
            created_at=0.0,
            session_id="test_resize_tabstops",
            cols=80,
            rows=24,
        )
        await _session_store.store(session)
        try:
            assert max(session.screen.tabstops) == 72
            await terminal_resize(session_id="test_resize_tabstops", cols=132, rows=53)
            assert session.cols == 132
            assert session.rows == 53
            assert max(session.screen.tabstops) == 128
            assert 80 in session.screen.tabstops
            assert 120 in session.screen.tabstops
        finally:
            await _session_store.delete("test_resize_tabstops")

    asyncio.run(_test())

