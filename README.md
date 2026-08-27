# chuk-mcp-telnet-client (Enhanced with Session Logging)

MCP (Model Context Protocol) Telnet Client with **persistent session disk logging** and timestamps.

## New Features in v0.3.2:
- **Automatic Session Logging**: Every telnet session, command, and server response is recorded to a human-readable `.log` file on disk with ISO-8601 timestamps.
- **Configurable Log Directory**: Configured via the `TELNET_LOG_DIR` environment variable (e.g. `export TELNET_LOG_DIR=/home/dfields/src/vms-exploration/logs`) or via tool parameter `log_dir`. Defaults to `~/.mcp-telnet-logs/`.
- **Log Path in Output**: The returned tool response includes `log_file` pointing directly to the generated transcript file.

## Installation

### Install with pipx (Recommended)
```bash
pipx install /path/to/chuk-mcp-telnet-client
# Or from built wheel:
pipx install dist/chuk_mcp_telnet_client-0.3.2-py3-none-any.whl --force
```

### Install in development mode
```bash
pip install -e .
```

## Running the Server

```bash
# stdio mode (Default for MCP agents)
mcp-telnet-client

# HTTP mode for persistent interactive sessions
mcp-telnet-client http
```
