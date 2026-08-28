# chuk-mcp-telnet-client

An enhanced **Model Context Protocol (MCP)** Telnet Client server designed for AI coding assistants and automation agents. Features asynchronous background stream buffering, non-blocking polling, interactive sessions, regex prompt matching, and persistent disk logging with ISO-8601 timestamps.

---

## Highlights & Features

* **Asynchronous Background Stream Buffering (v0.4.0)**: Background reader tasks continuously drain and buffer incoming socket data into an in-memory queue, preventing socket buffer overflow and ensuring zero data loss during multi-step agent reasoning loops.
* **Non-Blocking Session Polling (`telnet_read_session`)**: Enables agents to poll for updates, match regex prompt patterns (e.g. `(?m)(^\$ |Username: |Password: )`), or consume streaming output with configurable timeout windows.
* **Interactive Stream Input (`telnet_send_input`)**: Send interactive inputs directly into long-running tasks without session reconnection or terminal drops.
* **Persistent Session Disk Logging**: Every session, transmitted command, interactive input, and received stream chunk is recorded to human-readable `.log` transcript files with microsecond ISO-8601 timestamps.
* **Smart Log Directory Resolution**: Automatically detects project workspaces (e.g. `<workspace>/logs/`), honors `TELNET_LOG_DIR`, or defaults to `~/.mcp-telnet-logs/`.

---

## Release History & Changes

### Version 0.4.0
* **Continuous Background Streaming**: Dedicated asynchronous task per active telnet session continuously buffers socket output in real time.
* **`telnet_read_session` Tool**: Non-blocking polling of buffered session output with optional regex `prompt_pattern` matching and timeout controls (`max_wait_seconds`).
* **`telnet_send_input` Tool**: Direct interactive feeding of answers to prompts (e.g., installer queries, interactive menus, passwords) into active sessions.
* **Stream Chunk & Input Logging**: Real-time disk logging for all incremental stream reads and interactive inputs.
* **Automated Integration Tests**: Added `test_long_running.py` for testing background buffering, non-blocking polling, and prompt patterns.

### Version 0.3.2
* **Automatic Session Logging**: Full transcript logging with ISO-8601 timestamps.
* **Smart Workspace Detection**: Automatically identifies active agent working directories to store logs locally.
* **Log File Location in Output**: Returned tool responses include `log_file` with absolute path to transcripts.

---

## MCP Tools Reference

The server exposes five MCP tools:

| Tool | Purpose | Key Arguments |
| :--- | :--- | :--- |
| `telnet_client` | Connects or sends command sequences with automatic prompt waiting and response collection. | `host`, `port`, `commands`, `telnet_session_id`, `prompt_pattern`, `max_wait_seconds`, `close_session` |
| `telnet_read_session` | Non-blocking read/polling of the background stream buffer for an active session. | `telnet_session_id`, `prompt_pattern`, `max_wait_seconds`, `response_wait` |
| `telnet_send_input` | Sends interactive input or commands to an active session without closing or reconnecting. | `telnet_session_id`, `input_text`, `send_enter` |
| `telnet_list_sessions` | Lists all active persistent sessions, unread buffer byte counts, and log file paths. | *(None)* |
| `telnet_close_session` | Explicitly closes an active session and finalizes log files. | `telnet_session_id` |

---

## Installation

### With pipx (Recommended)

```bash
# Install directly from repository directory
pipx install /path/to/chuk-mcp-telnet-client --force

# Or install from built wheel
pipx install dist/chuk_mcp_telnet_client-0.4.0-py3-none-any.whl --force
```

### In Development Mode

```bash
pip install -e .
```

---

## Configuration & Usage

### Running as an MCP Server

In stdio mode (default for MCP agents like Antigravity, Claude Code, OpenCode):

```bash
mcp-telnet-client
# or
chuk-mcp-telnet-client
```

### In MCP Settings Configuration (`settings.json` / `claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "telnet-client": {
      "command": "mcp-telnet-client",
      "args": [],
      "env": {
        "TELNET_LOG_DIR": "/path/to/project/logs"
      }
    }
  }
}
```

---

## Authors & Acknowledgments

* **Original Author & Project**: Created by the **Chuk MCP Team** as part of the Chuk Model Context Protocol server suite ([chuk-mcp-telnet-client on PyPI](https://pypi.org/project/chuk-mcp-telnet-client/)).
* **Enhanced & Maintained by**: **Douglas P. Fields, Jr.** (`symbolics@lisp.engineer`) — Added persistent disk logging, workspace log detection, background stream buffering, non-blocking polling, and interactive inputs.

---

## License

This project is licensed under the [MIT License](LICENSE).
