# chuk-mcp-telnet-client (Terminal MCP Server)

A unified **Model Context Protocol (MCP)** Terminal Communications Server designed for AI coding assistants and automation agents. Supports both **Telnet** (networked hosts) and **USB-Serial / UART** (hardware console ports) concurrently with asynchronous background stream buffering, non-blocking polling, interactive sessions, dynamic speed switching, hardware Break signalling, and clean, human-readable disk logging.

---

## Highlights & Features

* **Unified Multi-Session Terminal Architecture (v0.5.1)**: Manages concurrent persistent connections across both Telnet and Serial interfaces within a single server instance.
* **Native Serial & USB-UART Console Support (`serial_client`)**: Direct hardware serial communications with configurable baud rate (default: 9600), byte size (8), parity (`N`), stop bits (1.0), and flow control (`rtscts`, `xonxoff`).
* **Dynamic Live Baud Rate Switching (`serial_set_speed`)**: Dynamically alters UART speed and framing parameters on an active open connection without dropping session state, disconnecting, or losing unconsumed buffer text.
* **Hardware RS-232 BREAK Condition (`serial_send_break`)**: Generates true RS-232 Break spacing (~250ms) to halt remote targets or VAX CPUs into console firmware (`>>>`).
* **Serial Port Discovery (`serial_list_ports`)**: Enumerates host hardware serial and USB-UART devices (`/dev/ttyUSB*`, `/dev/ttyS*`).
* **MCP 180-Second (3-Minute) Timeout Immunity**: Both Telnet and Serial tools implement non-blocking execution windows (`max_wait_seconds`) with background stream accumulation and interim progress returns (`command_completed: false`), preventing client tool aborts during long operations exceeding 3 minutes.
* **Clean, Human-Readable Transcript Logging (Default)**: Emits a clean, contiguous text stream identical to standard Unix `script` or `picocom` session logging (header, raw readable console text in the body, footer at exit).
* **Single Persistent File Per Session**: Each session maintains exactly one persistent log file (`<session_id>.log`). Sequential commands, streaming output chunks, interactive inputs, and status updates append contiguously without generating fragmented suffix files (`_1.log`, `_2.log`). Reconnecting with an existing session ID safely appends with a clear session start block.
* **Explicit Server Version Reporting**: The server reports its version (`server_version: "0.5.1"`) directly in tool return models and session log headers.
* **Optional Packet Markers (`timestamp_chunks = False`)**: Microsecond ISO-8601 chunk headers can be optionally enabled when diagnosing UART latency or byte-framing anomalies.
* **Unified Session Directory (`list_sessions`)**: Enumerates all active Telnet and Serial sessions with protocol type, target string, uptime, and byte transfer counters.

---

## Release History & Changes

### Version 0.5.1
* **Single Persistent File Per Session**: Fixed an issue where re-instantiating `SessionLogger` on sequential commands caused duplicate suffix files (`<session_id>_1.log`, `<session_id>_2.log`, etc.) to be generated on every tool invocation.
* **Deterministic Path Resolution**: `get_session_log_path()` now consistently maps each session ID to `<session_id>.log`.
* **Safe Reconnect Appending**: Starting a new session with an existing session ID cleanly appends a delimited session start header rather than truncating or fragmenting into multiple files.
* **Preserved Active Session Logger**: Active sessions in both `telnet_client_tool` and `serial_client_tool` retain their configured `session.logger` instance across tool calls.
* **Automated Regression Tests**: Added `test_telnet_single_log_file_per_session` and `test_serial_single_log_file_per_session` proving that sequential commands, interactive inputs, streaming polling, and session reconnects maintain exactly one file on disk.

### Version 0.5.0
* **Unified Terminal Architecture**: Merged serial and telnet capabilities into a single unified package with entrypoints `mcp-terminal-client`, `chuk-mcp-telnet-client`, and `mcp-telnet-client`.
* **Serial Communication Tools**:
  * `serial_client`: Connects and sends command sequences with configurable framing and non-blocking wait windows.
  * `serial_read_session`: Non-blocking read/polling of the background stream buffer for active serial sessions.
  * `serial_send_input`: Sends interactive inputs, passwords, or raw keystrokes to active serial sessions.
  * `serial_send_break`: Sends hardware RS-232 Break condition (~250ms) to halt systems into console prompt (`>>>`).
  * `serial_set_speed`: Reconfigures baud rate on an active connection on-the-fly without dropping connection.
  * `serial_list_ports`: Discovers and enumerates host serial ports and USB-UART adapters.
  * `serial_close_session`: Explicitly closes an active serial port and finalizes its log file.
* **Unified Session Directory**: Promoted `telnet_list_sessions` to `list_sessions` returning polymorphic session details across all protocols, with `telnet_list_sessions` retained as a backward-compatible alias.
* **Overhauled Session Logging**:
  * Defaulted to clean, uncorrupted terminal transcripts without noisy microsecond packet headers.
  * Made chunk-level timestamp headers strictly opt-in (`timestamp_chunks: bool = False`).
  * Implemented sequential numeric suffix incrementing for duplicate session IDs (`<id>_1.log`) to prevent overwriting or truncating previous transcripts.
* **Explicit Server Version Reporting**: Added `server_version` field to `TelnetClientOutput`, `SerialClientOutput`, `SessionInfo`, `SessionListResponse`, and session log transcript headers.
* **Automated Offline Test Suites**: Added comprehensive tests utilizing virtual pseudo-terminals (`pty.openpty()`) and in-process mock servers:
  * `test_standalone_telnet.py`: Standalone Telnet login, streaming, and no-overwrite tests.
  * `test_standalone_serial.py`: Raw PTY serial login, streaming, interactive input, speed shifting (9600 -> 19200), BREAK generation, and multi-session tests.
  * `test_timeout_3min.py`: Verified live execution of a long-running VAX task exceeding 3 minutes (188s elapsed) without hitting the 180s MCP timeout limit.

### Version 0.4.0
* **Continuous Background Streaming**: Dedicated asynchronous task per active telnet session continuously buffers socket output in real time.
* **`telnet_read_session` Tool**: Non-blocking polling of buffered session output with optional regex `prompt_pattern` matching and timeout controls (`max_wait_seconds`).
* **`telnet_send_input` Tool**: Direct interactive feeding of answers to prompts (e.g., installer queries, interactive menus, passwords) into active sessions.
* **Stream Chunk & Input Logging**: Real-time disk logging for all incremental stream reads and interactive inputs.

### Version 0.3.2
* **Automatic Session Logging**: Full transcript logging with ISO-8601 timestamps.
* **Smart Workspace Detection**: Automatically identifies active agent working directories to store logs locally.
* **Log File Location in Output**: Returned tool responses include `log_file` with absolute path to transcripts.

---

## MCP Tools Reference

The server exposes 13 MCP tools:

| Tool | Protocol | Purpose | Key Arguments |
| :--- | :--- | :--- | :--- |
| `serial_client` | Serial | Connects or sends command sequences over a serial port with timeout immunity and session logging. | `port`, `commands`, `baudrate`, `bytesize`, `parity`, `stopbits`, `serial_session_id`, `max_wait_seconds` |
| `serial_read_session` | Serial | Non-blocking read/polling of the background stream buffer for an active serial session. | `session_id`, `prompt_pattern`, `max_wait_seconds`, `idle_timeout` |
| `serial_send_input` | Serial | Sends interactive input, passwords, or raw keystrokes to an active serial session. | `session_id`, `input_text`, `raw`, `max_wait_seconds`, `prompt_pattern` |
| `serial_set_speed` | Serial | Dynamically reconfigures baud rate and framing on an active serial session without reconnecting. | `session_id`, `baudrate`, `bytesize`, `parity`, `stopbits` |
| `serial_send_break` | Serial | Sends an RS-232 hardware Break condition (~250ms) to halt a running system into console mode (`>>>`). | `session_id`, `duration`, `prompt_pattern`, `max_wait_seconds` |
| `serial_list_ports` | Serial | Enumerates available hardware serial and USB-UART ports on the host system. | *(None)* |
| `serial_close_session` | Serial | Explicitly closes an active serial session and finalizes log files. | `session_id` |
| `telnet_client` | Telnet | Connects or sends command sequences via Telnet with prompt waiting and session logging. | `host`, `port`, `commands`, `telnet_session_id`, `prompt_pattern`, `max_wait_seconds` |
| `telnet_read_session` | Telnet | Non-blocking read/polling of the background stream buffer for an active Telnet session. | `session_id`, `prompt_pattern`, `max_wait_seconds`, `idle_timeout` |
| `telnet_send_input` | Telnet | Sends interactive input or raw keystrokes to an active Telnet session. | `session_id`, `input_text`, `raw`, `max_wait_seconds`, `prompt_pattern` |
| `telnet_close_session` | Telnet | Explicitly closes an active Telnet session and finalizes log files. | `session_id` |
| `list_sessions` | Unified | Lists all active terminal sessions (both Telnet and Serial) with protocol and byte statistics. | *(None)* |
| `telnet_list_sessions` | Unified | Backward-compatible alias for `list_sessions`. | *(None)* |

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

## Authors & Attribution

* **Original Author & Project**: Created by the **Chuk MCP Team** as part of the Chuk Model Context Protocol server suite ([chuk-mcp-telnet-client on PyPI](https://pypi.org/project/chuk-mcp-telnet-client/)).
* **Enhanced & Maintained by**: **Douglas P. Fields, Jr.** (`symbolics@lisp.engineer`) — Extended into unified Terminal MCP Server (v0.5.0, v0.5.1) with USB-Serial console integration, dynamic baud rate switching, hardware RS-232 BREAK signalling, single-file persistent transcript logging, and timeout-proof multi-session polling.
  * With Gemini Flash (3.6, 3.7, 3.8) via Antigravity CLI

---

## License

This project is licensed under the [MIT License](LICENSE).
