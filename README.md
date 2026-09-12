# chuk-mcp-telnet-client (Terminal MCP Server)

A unified **Model Context Protocol (MCP)** Terminal Communications Server designed for AI coding assistants and automation agents. Supports both **Telnet** (networked hosts) and **USB-Serial / UART** (hardware console ports) concurrently with asynchronous background stream buffering, non-blocking polling, interactive sessions, dynamic speed switching, hardware Break signalling, in-memory DEC VT terminal screen emulation, and clean, human-readable disk logging.

---

## Highlights & Features

* **Virtual Terminal Screen Emulation (v0.6.0)**: In-memory DEC VT100 / VT220 terminal emulation powered by `pyte` (v0.8.2), providing AI agents with visual inspection of full-screen TUI applications (OpenVMS SMG$, curses, text editors, menu systems).
* **2D Canvas Inspection (`terminal_get_screen`)**: Extracts a clean 2D text matrix (default 80×24, dynamically resizable) along with 1-based cursor coordinates (`cursor_row`, `cursor_col`) and cursor visibility.
* **DEC Special Graphics Translation**: Translates DEC line-drawing character sets into clean Unicode box characters (`┌`, `─`, `┐`, `└`, `┘`, `│`, `┼`, `├`, `┤`, `┴`, `┬`) for native TUI border and window rendering.
* **Reverse-Video Selection Tracking**: Identifies reverse-video attribute cells (`char.reverse == True`) to provide highlighted lines and formatted annotations (`* [Selected Item] *`).
* **Ergonomic Key Dispatch (`terminal_send_key`)**: Translates named keys (`UP`, `DOWN`, `LEFT`, `RIGHT`, `TAB`, `ENTER`, `ESC`, `CTRL_Z`, `F1`–`F20`) into DEC VT escape sequences and immediately returns the updated screen state.
* **Dynamic Window Resizing (`terminal_resize`)**: Resizes in-memory canvas and transmits RFC 1073 Telnet NAWS subnegotiations (`IAC SB NAWS (cols) (rows) IAC SE`) to dynamically adjust terminal width and page on remote hosts (e.g. OpenVMS).
* **Automated VT Terminal Query Responder**: Background reader loops automatically respond to Primary Device Attributes inquiries (DA1: `\x1b[c` -> VT220 response `\x1b[?62;1;2;6;7;8;9c`), Cursor Position Report queries (CPR: `\x1b[6n` -> `\x1b[rows;colsR`), and DSR queries (`\x1b[5n` -> `\x1b[0n`).
* **Unified Multi-Session Terminal Architecture**: Manages concurrent persistent connections across both Telnet and Serial interfaces within a single server instance.
* **Native Serial & USB-UART Console Support (`serial_client`)**: Direct hardware serial communications with configurable baud rate (default: 9600), byte size (8), parity (`N`), stop bits (1.0), and flow control (`rtscts`, `xonxoff`).
* **Dynamic Live Baud Rate Switching (`serial_set_speed`)**: Dynamically alters UART speed and framing parameters on an active open connection without dropping session state, disconnecting, or losing unconsumed buffer text.
* **Hardware RS-232 BREAK Condition (`serial_send_break`)**: Generates true RS-232 Break spacing (~250ms) to halt remote targets or VAX CPUs into console firmware (`>>>`).
* **Serial Port Discovery (`serial_list_ports`)**: Enumerates host hardware serial and USB-UART devices (`/dev/ttyUSB*`, `/dev/ttyS*`).
* **MCP 180-Second (3-Minute) Timeout Immunity**: Both Telnet and Serial tools implement non-blocking execution windows (`max_wait_seconds`) with background stream accumulation and interim progress returns (`command_completed: false`), preventing client tool aborts during long operations exceeding 3 minutes.
* **Clean, Human-Readable Transcript Logging (Default)**: Emits a clean, contiguous text stream identical to standard Unix `script` or `picocom` session logging (header, raw readable console text in the body, footer at exit).
* **Single Persistent File Per Session**: Each session maintains exactly one persistent log file (`<session_id>.log`). Sequential commands, streaming output chunks, interactive inputs, and status updates append contiguously without generating fragmented suffix files (`_1.log`, `_2.log`). Reconnecting with an existing session ID safely appends with a clear session start block.
* **Explicit Server Version Reporting**: The server reports its version (`server_version: "0.6.0"`) directly in tool return models and session log headers.
* **Unified Session Directory (`list_sessions`)**: Enumerates all active Telnet and Serial sessions with protocol type, target string, uptime, and byte transfer counters.

---

## Release History & Changes

### Version 0.6.1
* **Fixed the default `prompt_pattern` for real-world OpenVMS use**: the
  previous default (`r"(?m)(^\$ |Username: |Password: |>>> )"`) required a
  bare `$ ` at the start of a line, so it never matched a
  SYLOGIN.COM-customized DCL prompt like `VAX96::USER1$ ` - every
  `telnet_client`/`serial_client` call relying on the default against such
  a system silently stopped detecting command completion after login,
  executing only the first queued command. The new default also adds VMS
  subsystem/utility prompts (`TCPIP>`, `MCL>`, `NCP>`, `AUTHORIZE>`,
  `SET HOST 0>`, `ANALYZE/SYSTEM>`, ...), which the old default never
  covered at all. See `PLAN_DEFAULT_PROMPT_PATTERN.md` for the full design
  rationale, and the "Default `prompt_pattern`" note under MCP Tools
  Reference below for what it now matches.

### Version 0.6.0
* **Virtual Terminal Screen Emulation (`pyte`)**:
  * Integrated `pyte` (v0.8.2) in-memory DEC VT terminal emulation into both `TelnetSession` and `SerialSession`.
  * Configured `stream.use_utf8 = False` to parse ISO-2022 DEC Special Graphics charset sequences (`\033(0` / `\033(B`), automatically converting VT line-drawing characters into clean Unicode box glyphs (`┌───┐`, `│   │`, `└───┘`).
* **Agent Visual Inspection Tool (`terminal_get_screen`)**:
  * Returns full 2D rendered text grid, cursor position (`cursor_row`, `cursor_col`), cursor visibility (`cursor_visible`), and detected highlighted lines (`highlighted_lines`).
  * Generates `annotated_text` enclosing reverse-video selections in brackets: `* [Highlighted Item] *`.
* **Keyboard & VT Sequence Dispatch (`terminal_send_key`)**:
  * Maps high-level key names (`UP`, `DOWN`, `LEFT`, `RIGHT`, `TAB`, `ENTER`, `ESC`, `BACKSPACE`, `DELETE`, `CTRL_A`–`CTRL_Z`, `PF1`–`PF4`, `F1`–`F20`) to DEC VT escape sequences.
  * Waits a configurable delay (`wait_seconds = 0.5`) yielding to the background stream reader, returning the updated rendered screen in a single round-trip.
* **Dynamic Window Resizing & RFC 1073 Telnet NAWS (`terminal_resize`)**:
  * Resizes the in-memory emulator canvas to arbitrary dimensions (e.g. 132 columns × 50 rows).
  * Automatically constructs and transmits Telnet NAWS (Negotiate About Window Size) subnegotiation packets (`IAC SB NAWS (cols) (rows) IAC SE`) directly over the socket.
  * OpenVMS and Unix hosts automatically adapt terminal width and page settings in real-time.
* **Automated VT Terminal Handshake & Device Query Auto-Responder**:
  * Added automated query response in the asynchronous background reader loops:
    * Primary Device Attributes (`\x1b[c` / `\x1bZ`) &rarr; Responds with VT220 identification (`\x1b[?62;1;2;6;7;8;9c`).
    * Cursor Position Report (`\x1b[6n`) &rarr; Responds with dimensions (`\x1b[{rows};{cols}R`).
    * Device Status Report (`\x1b[5n`) &rarr; Responds with status OK (`\x1b[0n`).
  * Resolves login stalls on OpenVMS where terminal initialization probes require immediate DA/CPR response, allowing OpenVMS to automatically configure `Device_Type: VT200_Series`.
* **Comprehensive Test Suite**:
  * Added `tests/test_screen_emulator.py` validating 2D grid layout, DEC box drawing, reverse-video formatting, dynamic resize, key dispatch, and NAWS option negotiation. All 18 tests passing with 0 regressions.
  * Verified live on physical OpenVMS VAX 7.3 (`VAX60`) using `HELLO_SMG.C`.

### Version 0.5.1
* **Single Persistent File Per Session**: Fixed an issue where re-instantiating `SessionLogger` on sequential commands caused duplicate suffix files (`<session_id>_1.log`, `<session_id>_2.log`, etc.) to be generated on every tool invocation.
* **Deterministic Path Resolution**: `get_session_log_path()` now consistently maps each session ID to `<session_id>.log`.
* **Safe Reconnect Appending**: Starting a new session with an existing session ID cleanly appends a delimited session start header rather than truncating or fragmenting into multiple files.
* **Preserved Active Session Logger**: Active sessions in both `telnet_client_tool` and `serial_client_tool` retain their configured `session.logger` instance across tool calls.
* **Automated Regression Tests**: Added `test_telnet_single_log_file_per_session` and `test_serial_single_log_file_per_session` proving that sequential commands, interactive inputs, streaming polling, and session reconnects maintain exactly one file on disk.

### Version 0.5.0
* **Unified Terminal Architecture**: Merged serial and telnet capabilities into a single unified package with entrypoints `mcp-terminal-client`, `chuk-mcp-telnet-client`, and `mcp-telnet-client`.
* **Serial Communication Tools**: `serial_client`, `serial_read_session`, `serial_send_input`, `serial_send_break`, `serial_set_speed`, `serial_list_ports`, `serial_close_session`.
* **Unified Session Directory**: Promoted `telnet_list_sessions` to `list_sessions` returning polymorphic session details across all protocols.
* **Overhauled Session Logging**: Clean, uncorrupted terminal transcripts without noisy microsecond packet headers by default.
* **Automated Offline Test Suites**: Added comprehensive tests utilizing virtual pseudo-terminals (`pty.openpty()`) and in-process mock servers (`test_standalone_telnet.py`, `test_standalone_serial.py`, `test_timeout_3min.py`).

### Version 0.4.0
* **Continuous Background Streaming**: Dedicated asynchronous task per active session continuously buffers socket output in real time.
* **`telnet_read_session` Tool**: Non-blocking polling of buffered session output with optional regex `prompt_pattern` matching and timeout controls (`max_wait_seconds`).
* **`telnet_send_input` Tool**: Direct interactive feeding of answers to prompts into active sessions.

### Version 0.3.2
* **Automatic Session Logging**: Full transcript logging with ISO-8601 timestamps.
* **Smart Workspace Detection**: Automatically identifies active agent working directories to store logs locally.

---

## MCP Tools Reference

The server exposes 16 MCP tools across Telnet, Serial, and Visual Terminal categories:

| Tool | Category | Purpose | Key Arguments |
| :--- | :--- | :--- | :--- |
| `terminal_get_screen` | Visual | Inspects the rendered 2D terminal canvas, cursor position, and highlighted reverse-video text. | `session_id`, `wait_seconds` |
| `terminal_send_key` | Visual | Sends named keyboard keys or VT escape sequences (e.g. `UP`, `DOWN`, `TAB`, `ENTER`, `ESC`, `CTRL_Z`) and returns the updated screen. | `session_id`, `key`, `wait_seconds` |
| `terminal_resize` | Visual | Dynamically resizes the virtual terminal canvas and sends RFC 1073 Telnet NAWS window resize negotiation. | `session_id`, `cols`, `rows` |
| `telnet_client` | Telnet | Connects or sends command sequences via Telnet with prompt waiting and session logging. | `host`, `port`, `commands`, `telnet_session_id`, `prompt_pattern`, `max_wait_seconds` |
| `telnet_read_session` | Telnet | Non-blocking read/polling of the background stream buffer for an active Telnet session. | `session_id`, `prompt_pattern`, `max_wait_seconds`, `idle_timeout` |
| `telnet_send_input` | Telnet | Sends interactive input or raw keystrokes to an active Telnet session. | `session_id`, `input_text`, `raw`, `max_wait_seconds`, `prompt_pattern` |
| `telnet_close_session` | Telnet | Explicitly closes an active Telnet session and finalizes log files. | `session_id` |
| `serial_client` | Serial | Connects or sends command sequences over a serial port with timeout immunity and session logging. | `port`, `commands`, `baudrate`, `bytesize`, `parity`, `stopbits`, `serial_session_id`, `max_wait_seconds` |
| `serial_read_session` | Serial | Non-blocking read/polling of the background stream buffer for an active serial session. | `session_id`, `prompt_pattern`, `max_wait_seconds`, `idle_timeout` |
| `serial_send_input` | Serial | Sends interactive input, passwords, or raw keystrokes to an active serial session. | `session_id`, `input_text`, `raw`, `max_wait_seconds`, `prompt_pattern` |
| `serial_set_speed` | Serial | Dynamically reconfigures baud rate and framing on an active serial session without reconnecting. | `session_id`, `baudrate`, `bytesize`, `parity`, `stopbits` |
| `serial_send_break` | Serial | Sends an RS-232 hardware Break condition (~250ms) to halt a running system into console mode (`>>>`). | `session_id`, `duration`, `prompt_pattern`, `max_wait_seconds` |
| `serial_list_ports` | Serial | Enumerates available hardware serial and USB-UART ports on the host system. | *(None)* |
| `serial_close_session` | Serial | Explicitly closes an active serial session and finalizes log files. | `session_id` |
| `list_sessions` | Unified | Lists all active terminal sessions (both Telnet and Serial) with protocol and byte statistics. | *(None)* |
| `telnet_list_sessions` | Unified | Backward-compatible alias for `list_sessions`. | *(None)* |

**Default `prompt_pattern` (v0.6.1+)**: when a tool takes `prompt_pattern` but
none is given, it falls back to a default built for OpenVMS DCL and its
subsystem utilities out of the box:
* a bare `$ ` DCL prompt, **or** a SYLOGIN.COM-customized `NODE::USER$ `
  prompt (any node/username), matched at the true end of the received
  output;
* a VMS subsystem/utility prompt of the shape "*facility name*`>`" - e.g.
  `TCPIP>`, `MCL>`, `NCP>`, `AUTHORIZE>`, `SET HOST 0>`, `ANALYZE/SYSTEM>`;
* `Username: ` / `Password: ` login prompts;
* the VAX/Alpha console firmware prompt `>>> `.

Pass an explicit `prompt_pattern` to override this for a specific prompt
you already know (e.g. while inside a nested subsystem, to detect only
that subsystem's own prompt rather than "some prompt, of some kind").

---

## Visual Terminal Interaction Examples

### 1. Reading Full-Screen TUI State (`terminal_get_screen`)
```python
# Inspect the 24x80 canvas of an active SMG$ application:
screen = await terminal_get_screen(session_id="vms_session")
print(screen.screen_text)
# Output:
# ┌──────── OpenVMS SMG$ ────────┐
# │                              │
# │        Hello, world!         │
# │                              │
# └──────────────────────────────┘

# Check which lines contain highlighted / reverse-video selections:
print("Highlighted lines:", screen.highlighted_lines)
print("Annotated view:\n", screen.annotated_text)
```

### 2. Navigating with Keys (`terminal_send_key`)
```python
# Move selection down and receive the updated screen immediately:
update = await terminal_send_key(session_id="vms_session", key="DOWN")
print(update.screen_text)

# Switch panes with Tab:
await terminal_send_key(session_id="vms_session", key="TAB")

# Open popup with Enter, dismiss with ESC:
await terminal_send_key(session_id="vms_session", key="ENTER")
await terminal_send_key(session_id="vms_session", key="ESC")

# Exit cleanly with Ctrl-Z:
await terminal_send_key(session_id="vms_session", key="CTRL_Z")
```

### 3. Dynamic Terminal Resizing (`terminal_resize`)
```python
# Expand terminal to 132 columns x 50 rows via RFC 1073 Telnet NAWS:
await terminal_resize(session_id="vms_session", cols=132, rows=50)
```

---

## Installation & Deployment

### Recommended: With `pipx` under Python 3.11 / 3.12

```bash
# Install in editable mode from source repository
pipx install --python python3.12 -e /home/dfields/src/chuk-mcp-telnet-client

# Verify available commands
mcp-terminal-client --help
mcp-telnet-client --help
```

### In Development Virtual Environment

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e .
```

---

## Running the Automated Test Suite

```bash
# Run the complete test suite (all 18 unit and integration tests):
pytest tests/test_screen_emulator.py tests/test_standalone_telnet.py tests/test_standalone_serial.py
```

---

## Authors & Attribution

* **Original Author & Project**: Created by the **Chuk MCP Team** as part of the Chuk Model Context Protocol server suite ([chuk-mcp-telnet-client on PyPI](https://pypi.org/project/chuk-mcp-telnet-client/)).
* **Enhanced & Maintained by**: **Douglas P. Fields, Jr.** (`symbolics@lisp.engineer`) — Extended into unified Terminal MCP Server (v0.5.0, v0.5.1, v0.6.0) with DEC VT terminal screen emulation (`pyte`), Unicode line-drawing conversion, reverse-video attribute detection, RFC 1073 NAWS dynamic resizing, automated VT inquiry response, USB-Serial console integration, dynamic baud rate switching, hardware RS-232 BREAK signalling, single-file persistent transcript logging, and timeout-proof multi-session polling.
  * With Gemini Flash (3.6, 3.7, 3.8) via Antigravity CLI

---

## License

This project is licensed under the [MIT License](LICENSE).
