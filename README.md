# chuk-mcp-telnet-client (Terminal MCP Server)

A unified **Model Context Protocol (MCP)** Terminal Communications Server designed for AI coding assistants and automation agents. Supports both **Telnet** (networked hosts) and **USB-Serial / UART** (hardware console ports) concurrently with asynchronous background stream buffering, non-blocking polling, interactive sessions, dynamic speed switching, hardware Break signalling, in-memory DEC VT terminal screen emulation, and clean, human-readable disk logging.

> **Requires Python 3.11 or 3.12 — NOT 3.13+.** The stdlib `telnetlib` this
> package imports was removed in 3.13 (PEP 594). On a rolling distro such as
> Arch, the system interpreter is already too new, and installing under it
> produces a server that registers cleanly and then fails at first use with
> `ModuleNotFoundError: No module named 'telnetlib'`. Having `telnetlib3`
> installed does **not** change this. See
> [Installation & Deployment](#installation--deployment).

---

## Highlights & Features

* **Network-Bridged Serial Console Support (RFC 2217 & Raw Sockets, v0.7.0)**: `serial_client` seamlessly connects to remote network-bridged serial ports via `rfc2217://host:port`, `socket://host:port`, or auto-detected `host:port` (e.g. through `ser2net`).
* **Hardware RS-232 BREAK over the Network (v0.7.0)**: `serial_send_break` transmits true RS-232 Break signals over RFC 2217 network connections to halt remote targets or VAX CPUs into console firmware (`>>>`).
* **Configurable Command Line Terminators (`newline`, v0.7.0)**: Supports custom newline strings across Telnet and Serial tools (default `\r\n`, with `\r` for OpenVMS serial consoles).
* **Virtual Terminal Screen Emulation (v0.6.0)**: In-memory DEC VT100 / VT220 terminal emulation powered by `pyte` (v0.8.2), providing AI agents with visual inspection of full-screen TUI applications (OpenVMS SMG$, curses, text editors, menu systems).
* **2D Canvas Inspection (`terminal_get_screen`)**: Extracts a clean 2D text matrix (default 80×24, dynamically resizable) along with 1-based cursor coordinates (`cursor_row`, `cursor_col`) and cursor visibility.
* **Always-Current Screen (v0.7.1)**: Each session's reader runs in its own background thread, reading the socket or port as bytes arrive, whether or not a tool call is running. A screen read is therefore always current, even with `wait_seconds=0`, and transcript chunks are stamped when they arrive.
* **Wait for the Screen to Change (`terminal_wait_for_change`, v0.7.1)**: One call that returns as soon as the screen has changed (or shows a regular expression) and then stayed quiet for a settle time, or at a timeout - instead of a model turn per screen check.
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
* **Explicit Server Version Reporting**: The server reports its version (`server_version: "0.7.2"`) directly in tool return models and session log headers.
* **Unified Session Directory (`list_sessions`)**: Enumerates all active Telnet and Serial sessions with protocol type, target string, uptime, and byte transfer counters.

---

## Release History & Changes

### Version 0.7.2

* **`terminal_send_key` sends a one-character whitespace key**: a space, a TAB
  character or a CR given as `key` is sent as it is. Until now the key name was
  stripped before the lookup, so `" "` became nothing and an agent typing a line
  key by key lost every space (found by run M1L of vms-savage, 2026-10-28). A
  longer key name is still stripped of stray spaces (`" ENTER "` is ENTER).
  Test: `tests/test_m1l_send_key_whitespace.py`.
* Version strings: pyproject, `__version__`, `server_version`, uv.lock and the
  log header report `0.7.2`.

### Version 0.7.1
* **Fixed: stale screens between tool calls.** In stdio mode (the default, and
  how MCP clients such as Codex and Claude Code start the server),
  `chuk_mcp_server`'s `StdioSyncTransport` blocks in `sys.stdin.readline()`
  between requests and runs the asyncio event loop only while it serves one.
  Up to 0.7.0 each session's background reader was an asyncio task on that
  loop, so **between tool calls nothing read the socket or serial port**:
  * `terminal_get_screen` with `wait_seconds=0` returned the screen as of the
    previous call, even when new output had arrived seconds before;
  * the transcript stamped a chunk with the time the reader got round to it,
    not the time it arrived (measured: 2.5 s to 9.5 s late).
* **Now each Telnet and serial session's reader is a daemon thread**
  (`telnet-reader-<id>` / `serial-reader-<id>`) that reads whenever bytes
  arrive, answers the VT queries, feeds the pyte screen and the buffers under
  a lock, and stamps each transcript chunk on arrival. **A screen read is
  always current.** `wait_seconds` on `terminal_get_screen` still works, but is
  no longer needed to "let the reader catch up".
* Closing a session stops its reader (the blocked read is woken at once), joins
  the thread (bounded at 2 s) and then closes the socket or port.
* All writes to a session (commands, keys, VT replies, NAWS, BREAK) are
  serialised by a per-session write lock; the transcript file by a logger lock.
* **New tool `terminal_wait_for_change`** (below).
* No existing tool's name, arguments or result shape changed. `server_version`
  reports `0.7.1`.
* Design notes and the evidence (tests RED on 0.7.0, GREEN here, sabotage
  runs): [`docs/m1-live-reader.md`](docs/m1-live-reader.md).

### Version 0.7.0
* **Network-Bridged Serial Console Support (`serial_client`)**:
  * Added native network URL support via `pyserial`'s `serial_for_url`: supports `rfc2217://host:port`, `socket://host:port`, and `loop://`.
  * Automatic target URL normalization: targets supplied as `host:port` (e.g. `127.0.0.1:7001` or `vintagate:7001`) automatically default to `rfc2217://host:port` to enable full COM port control, baud rate switching, and break signalling.
  * Automatic raw TCP fallback: if connecting to `host:port` fails due to remote RFC 2217 negotiation rejection, `_connect_serial` automatically falls back to raw `socket://host:port`.
* **Hardware RS-232 BREAK over Network (`serial_send_break`)**:
  * Supports transmitting hardware BREAK signals over RFC 2217 network connections to halt remote targets and VAX CPUs into console firmware (`>>>`).
  * Explicit validation and error reporting when attempting BREAK over raw TCP sockets (`socket://`), informing the user that RFC 2217 (`accepter: telnet(rfc2217)...`) is required.
* **Configurable Command Line Terminators (`newline`)**:
  * Added `newline` parameter (default `\r\n`) across `serial_client`, `serial_send_input`, `telnet_client`, and `telnet_send_input`.
  * Allows passing `newline="\r"` for OpenVMS serial consoles (`_OPA0:`) to avoid carriage return + newline entering empty passwords or triggering invalid login errors.
* **Silent Socket & Bannerless Connection Robustness**:
  * Enhanced `_connect_telnet` with safe non-blocking banner reading to prevent hangs or unhandled timeout exceptions when connecting to quiet or bannerless remote endpoints.
* **Comprehensive Test Coverage**:
  * Added unit tests for URL normalization, raw TCP network serial bridges, RFC 2217 negotiation fallback, raw socket break rejection, and custom newline handling (45 passing tests).

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

The server exposes 17 MCP tools across Telnet, Serial, and Visual Terminal categories:

| Tool | Category | Purpose | Key Arguments |
| :--- | :--- | :--- | :--- |
| `terminal_get_screen` | Visual | Inspects the rendered 2D terminal canvas, cursor position, and highlighted reverse-video text. Always current (v0.7.1): `wait_seconds=0` shows everything received so far. | `session_id`, `wait_seconds` |
| `terminal_wait_for_change` | Visual | Waits in one call until the screen changes since the last screen read (or shows `pattern`) and has been quiet for `settle_seconds`, then returns it; or returns at `timeout_seconds` (capped at 300). | `session_id`, `timeout_seconds`, `settle_seconds`, `pattern` |
| `terminal_send_key` | Visual | Sends named keyboard keys or VT escape sequences (e.g. `UP`, `DOWN`, `TAB`, `ENTER`, `ESC`, `CTRL_Z`) and returns the updated screen. | `session_id`, `key`, `wait_seconds` |
| `terminal_resize` | Visual | Dynamically resizes the virtual terminal canvas and sends RFC 1073 Telnet NAWS window resize negotiation. | `session_id`, `cols`, `rows` |
| `telnet_client` | Telnet | Connects or sends command sequences via Telnet with prompt waiting and session logging. | `host`, `port`, `commands`, `telnet_session_id`, `prompt_pattern`, `newline`, `max_wait_seconds` |
| `telnet_read_session` | Telnet | Non-blocking read/polling of the background stream buffer for an active Telnet session. | `session_id`, `prompt_pattern`, `max_wait_seconds`, `idle_timeout` |
| `telnet_send_input` | Telnet | Sends interactive input or raw keystrokes to an active Telnet session. | `session_id`, `input_text`, `raw`, `newline`, `max_wait_seconds`, `prompt_pattern` |
| `telnet_close_session` | Telnet | Explicitly closes an active Telnet session and finalizes log files. | `session_id` |
| `serial_client` | Serial | Connects or sends command sequences over a local serial port or network serial bridge (`rfc2217://`, `socket://`) with timeout immunity and session logging. | `port`, `commands`, `baudrate`, `bytesize`, `parity`, `stopbits`, `newline`, `serial_session_id`, `max_wait_seconds` |
| `serial_read_session` | Serial | Non-blocking read/polling of the background stream buffer for an active serial session. | `session_id`, `prompt_pattern`, `max_wait_seconds`, `idle_timeout` |
| `serial_send_input` | Serial | Sends interactive input, passwords, or raw keystrokes to an active serial session. | `session_id`, `input_text`, `raw`, `newline`, `max_wait_seconds`, `prompt_pattern` |
| `serial_set_speed` | Serial | Dynamically reconfigures baud rate and framing on an active serial session without reconnecting. | `session_id`, `baudrate`, `bytesize`, `parity`, `stopbits` |
| `serial_send_break` | Serial | Sends an RS-232 hardware Break condition (~250ms) locally or over RFC 2217 to halt a running system into console mode (`>>>`). | `session_id`, `duration`, `prompt_pattern`, `max_wait_seconds` |
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

### 3. Waiting for the Screen to Change (`terminal_wait_for_change`, v0.7.1)

```python
# After a key that starts something slow, wait for the screen to change and
# settle, instead of polling terminal_get_screen once per model turn:
r = await terminal_wait_for_change(session_id="vms_session",
                                   timeout_seconds=60, settle_seconds=0.3)
if r.changed:            # the screen differs from the last one returned
    print(r.screen_text)
elif r.timed_out:        # nothing changed within 60 s
    ...

# Or wait for particular text (a Python regular expression on screen_text):
r = await terminal_wait_for_change(session_id="vms_session",
                                   timeout_seconds=120, pattern=r"DONE \d+")
print(r.pattern_matched)  # e.g. "DONE 42", or None at the timeout
```

* **Baseline**: "changed" compares with the last screen this server *returned*
  for the session - from `terminal_get_screen`, `terminal_send_key` or this
  call. Output that arrived while no call was running counts, so a change
  that has already happened returns at once. If no screen has been returned
  yet, the baseline is the screen when the wait starts. A change is any
  difference in the text, the reverse-video annotation, or the cursor's
  position or visibility; bytes that repaint the same screen are not one.
* **Settle**: "quiet" means no byte has arrived for `settle_seconds` (default
  0.3 s). A full 80x24 repaint over a 9600-baud line takes about 2 s, but
  arrives continuously, so the default settle waits for its end. Raise it
  for a program that pauses mid-repaint.
* **Pattern**: with `pattern`, the wait ends when the pattern is on the screen
  and the screen has settled, whether or not it changed (so text that is
  already there returns at once). An invalid pattern is an error before any
  wait.
* **Result**: the screen fields of `terminal_get_screen`, plus `changed`,
  `timed_out`, `pattern_matched`, `waited_seconds`, `bytes_received` (during
  the wait) and `session_active`. It returns at once if the session ends. At
  the timeout, `changed` can still be true if output never went quiet.
* **Limits**: `timeout_seconds` is clamped to 0-300 so a call cannot hang a
  client for ever; `settle_seconds` to 0-`timeout_seconds`. Keep
  `timeout_seconds` under your MCP client's own tool-call timeout. In stdio
  mode the server handles one request at a time, so while a wait runs, no
  other call to the same server (for any session) is answered; the readers
  keep reading meanwhile, so nothing is lost.

### 4. Dynamic Terminal Resizing (`terminal_resize`)
```python
# Expand terminal to 132 columns x 50 rows via RFC 1073 Telnet NAWS:
await terminal_resize(session_id="vms_session", cols=132, rows=50)
```

---

## Configuring `ser2net` for MCP Terminal Access

When serial ports on managed systems (such as VAX consoles) are connected to a remote Linux gateway server rather than the local workstation running the MCP client, `ser2net` provides serial-to-network bridging.

### Accepter Modes: RFC 2217 vs Raw TCP

`ser2net` supports two accepter modes for incoming TCP connections:

1. **RFC 2217 Mode (`accepter: telnet(rfc2217),tcp,<bind_ip>,<port>`) — STRONGLY RECOMMENDED**:
   * Enables Telnet option negotiation (RFC 2217 Telnet Com Port Control Option).
   * Telnet CLI clients automatically negotiate character-at-a-time mode and binary transmission, eliminating the need to manually switch modes via `Control-]` / `mode character` / `set binary`.
   * **Hardware BREAK Support**: Allows `serial_send_break` to transmit true RS-232 Break spacing across the network to halt remote VAX systems into console firmware (`>>>`).
   * Allows dynamic baud rate and framing adjustments across the network.
2. **Raw TCP Mode (`accepter: tcp,<bind_ip>,<port>`)**:
   * Raw byte pass-through.
   * Does NOT support RS-232 BREAK signalling (pyserial silently ignores `send_break()` on raw sockets).
   * Telnet CLI clients default to line-mode and local echo, requiring manual mode switching.

### Example `/etc/ser2net/ser2net.yaml` Configuration

```yaml
%YAML 1.1
---
define: &confver 1.0

default:
  name: local
  value: true
  class: serialdev

connection: &vax4000_60
  accepter: telnet(rfc2217),tcp,127.0.0.1,7001
  connector: serialdev,/dev/vax_4000_60,9600n81,local
  enable: on
  options:
    kickolduser: true

connection: &vax4000_96
  accepter: telnet(rfc2217),tcp,127.0.0.1,7004
  connector: serialdev,/dev/vax_4000_96,9600n81,local
  enable: on
  options:
    kickolduser: true
```

### Deterministic Serial Device Naming via Udev Rules

When multiple USB-to-UART serial adapters are connected to a Linux server, default kernel device node assignments (`/dev/ttyUSB0`, `/dev/ttyUSB1`, etc.) are non-deterministic and can shuffle across reboots, kernel updates, or USB re-plugs. If `ser2net.yaml` were configured with raw `/dev/ttyUSB*` paths, a reboot could silently swap console assignments across machines.

To ensure permanent, deterministic device nodes, configure udev rules mapping each adapter's unique USB serial number to a descriptive symlink:

1. **Add User to Serial Access Group**:
   Depending on the Linux distribution, serial ports are owned by group `uucp` (Arch, RedHat, Fedora) or `dialout` (Debian, Ubuntu):
   ```bash
   # Add your user to the appropriate group (e.g. uucp or dialout)
   sudo usermod -aG uucp $USER   # or: sudo usermod -aG dialout $USER
   ```

2. **Discover Adapter Attributes**:
   Query the physical adapter's unique serial number and USB vendor/product IDs:
   ```bash
   udevadm info -a -n /dev/ttyUSB0 | grep -E '(ATTRS{serial}|ATTRS{idVendor}|ATTRS{idProduct})'
   ```
   *Example output:*
   ```text
   ATTRS{idProduct}=="6001"
   ATTRS{idVendor}=="0403"
   ATTRS{serial}=="A9QEPKOR"
   ```

3. **Create Static Udev Rules (`/etc/udev/rules.d/99-vax-consoles.rules`)**:
   Create symlinks with group permissions (`0660`):
   ```udev
   # VAXstation 4000/60 Console
   SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", ATTRS{serial}=="A9QEPKOR", SYMLINK+="vax_4000_60", GROUP="uucp", MODE="0660"

   # VAXstation 4000/96 Console
   SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", ATTRS{serial}=="A95RTMFR", SYMLINK+="vax_4000_96", GROUP="uucp", MODE="0660"
   ```

4. **Reload and Trigger Udev**:
   ```bash
   sudo udevadm control --reload-rules && sudo udevadm trigger
   ```
   This immediately instantiates persistent symlinks at `/dev/vax_4000_60` and `/dev/vax_4000_96` that survive reboots and USB reconnections.

5. **Reference in `ser2net.yaml`**:
   The static device symlinks are then referenced directly in the `ser2net` connection definitions (as shown above):
   ```yaml
   connector: serialdev,/dev/vax_4000_60,9600n81,local
   ```

### Connecting to `ser2net` with MCP Tools

* **Target URL**: Pass `rfc2217://host:port` or simply `host:port` (which automatically defaults to RFC 2217):
  ```python
  # Connect to VAX60 console via ser2net
  res = await serial_client(
      port="rfc2217://127.0.0.1:7001",  # or "127.0.0.1:7001"
      commands=["SHOW SYSTEM"],
      newline="\r",                     # Use bare \r for OpenVMS console
  )
  ```
* **Sending BREAK**:
  ```python
  # Halts running VAX CPU into console firmware >>>
  break_res = await serial_send_break(session_id="...")
  ```
* **OpenVMS Serial Console Line Terminator Note**: OpenVMS serial consoles (`_OPA0:`) consume `\r` (carriage return) as command execution and `\n` (newline) as an immediate subsequent empty input line. Always specify `newline="\r"` when interacting with VMS serial consoles to prevent premature empty password submission or login errors.

---

## Installation & Deployment

### REQUIREMENT: Python 3.11 or 3.12. Not 3.13 or newer.

This is a hard requirement, not a preference. `pyproject.toml` enforces it
(`requires-python = ">=3.11, <3.13"`), so `pip`/`uv`/`pipx` will refuse to
install under a newer interpreter.

**Why:** `chuk_mcp_telnet_client/tools.py` imports the **standard library**
`telnetlib`, which [PEP 594](https://peps.python.org/pep-0594/) removed from
CPython in **3.13**. Under 3.13+ the import fails before any of this package's
code runs:

```
ModuleNotFoundError: No module named 'telnetlib'
```

**A point that has already caused confusion, twice:** this package also depends
on **`telnetlib3`** from PyPI (see `pyproject.toml`). Those are two different
things. `telnetlib3` being installed does **not** satisfy the stdlib
`telnetlib` import in `tools.py`, so reading the dependency list and concluding
"3.13+ is fine now" is wrong. Check the import, not the dependency list.

The failure is quiet in the worst way: the MCP server installs cleanly, the
host registers it, and everything dies at first use rather than at install
time.

### Getting a Python 3.12 on Arch Linux (and other rolling distros)

Arch ships only the current CPython — on the reference host that is
**3.14.7**, and `/usr/bin/python3.12` does not exist. There is **no `python312`
package in the official repositories** (only in the AUR). So do not expect the
system interpreter to work, and do not fight `pacman` for an old one.

The approach used on the reference hosts is to let **`uv` supply the
interpreter**. `uv` downloads and manages standalone CPython builds under
`~/.local/share/uv/python/`, entirely separate from the system Python:

```bash
# One time: fetch a managed 3.12 (no root, no AUR, no effect on system python)
uv python install 3.12
```

Everything below then works regardless of what `/usr/bin/python3` happens to be.

### Option A — `uv tool install` (recommended for MCP server use)

Installs the entry points onto your `PATH` in an isolated environment, pinned
to 3.12:

```bash
uv tool install --python 3.12 --editable /path/to/chuk-mcp-telnet-client

mcp-telnet-client --help
mcp-terminal-client --help
```

This is the right choice when the package is being consumed as an MCP server
(i.e. the host launches `mcp-telnet-client` by name).

### Option B — project virtual environment

Right when the package is vendored into another project (e.g. as a git
submodule) and that project's own scripts import it directly:

```bash
cd /path/to/chuk-mcp-telnet-client
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e .
```

Consumers that must not depend on the shell's active environment can invoke the
interpreter by absolute path instead of activating:

```bash
/path/to/chuk-mcp-telnet-client/.venv/bin/python3 your_script.py
```

### Option C — `pipx`

```bash
pipx install --python python3.12 --editable /path/to/chuk-mcp-telnet-client
```

Note this requires a `python3.12` that `pipx` can find on `PATH`, which on Arch
means you have already provided one (see above). `uv tool install` avoids that
extra step and is preferred.

### Verifying you got it right

Confirm the interpreter actually behind the installed entry point, rather than
assuming:

```bash
python3 -c "import telnetlib; print('stdlib telnetlib OK')"
```

Run that with the *same* interpreter the server will use. If it raises
`ModuleNotFoundError`, the environment is on 3.13+ and the server will fail at
first use no matter how cleanly it installed.

---

## Running the Automated Test Suite

```bash
# Run the complete test suite (all 55 unit and integration tests):
VAX_LONG_TEST_DURATION=3 pytest tests/
```

---

## Authors & Attribution

* **Original Author & Project**: Created by the **Chuk MCP Team** as part of the Chuk Model Context Protocol server suite ([chuk-mcp-telnet-client on PyPI](https://pypi.org/project/chuk-mcp-telnet-client/)).
* **Enhanced & Maintained by**: **Douglas P. Fields, Jr.** (`symbolics@lisp.engineer`) — Extended into unified Terminal MCP Server (v0.5.0, v0.5.1, v0.6.0, v0.7.0) with RFC 2217 network serial support, network RS-232 BREAK signalling, configurable newline terminators, DEC VT terminal screen emulation (`pyte`), Unicode line-drawing conversion, reverse-video attribute detection, RFC 1073 NAWS dynamic resizing, automated VT inquiry response, USB-Serial console integration, dynamic baud rate switching, single-file persistent transcript logging, and timeout-proof multi-session polling.
  * With Gemini Flash (3.6, 3.7, 3.8) via Antigravity CLI

---

## License

This project is licensed under the [MIT License](LICENSE).
