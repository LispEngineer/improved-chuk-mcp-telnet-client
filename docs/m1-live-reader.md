# Run M1: a session reader that runs all the time, and `terminal_wait_for_change`

Run M1 of the vms-savage project, 2026-10-28, on the project's Linux host. Branch `m1-live-reader`
from `main` at 09dbaa0 (v0.7.0). Author: Douglas P. Fields, Jr.
(`symbolics@lisp.engineer`), with Claude.

Every claim below is labelled **MEASURED** (run or read here, with the evidence
named), **REPORTED** (someone else measured it; source named) or **ASSUMED**.

## 1. The cause, as read here

* **REPORTED** (vms-savage run F8, `learnings/f8-hit-repaint-notsaved.md` item 5
  and its host probe `scripts/f8_mcp_idle_probe.py`, log
  `logs/f8_mcp_idle_probe_p1.out`, 28-OCT 00:42, against the installed 0.7.0): a
  wait-0 `terminal_get_screen` did not show data that had arrived seconds before;
  the transcript stamped that chunk 3.5 s and 9.5 s after it was sent.
* **MEASURED** (read here, chuk_mcp_server 0.26.1 in this worktree's `.venv`, the
  version `uv.lock` pins): `main.py` calls `run(transport="stdio")`;
  `StdioSyncTransport.run()` (`stdio_transport.py` line 392) makes one event loop,
  then loops on a blocking `sys.stdin.readline()` (line 402) and runs the loop only
  inside `loop.run_until_complete(self._handle_message(line))` (line 411), i.e. for
  one request at a time.
* **MEASURED** (read here, `tools.py` at 09dbaa0): each session's reader was
  `_background_telnet_reader_loop` / `_background_serial_reader_loop` (lines 215 and
  273), started by `asyncio.create_task` in `SessionStore.store` (line 350/352). A
  task runs only while its loop runs, so between tool calls nobody read the socket
  or the port: bytes waited in the kernel, pyte was not fed, and
  `log_stream_chunk` stamped a chunk with the time it was read.
  `terminal_get_screen(wait_seconds=0)` (line 1428) returns without yielding to the
  loop long enough for the reader, so it showed the screen as of the last call.
* The serial reader had the same structure. **MEASURED** that it had the same
  fault: test 5 below is RED on 09dbaa0 (section 4).

## 2. The design chosen: a daemon thread per session

The brief offered two ways: a reader thread per session, or an async stdio
transport that keeps the loop running between calls. **Threads were chosen**,
because:

* The transport belongs to chuk_mcp_server, a dependency. Replacing it means
  owning a copy of its JSON-RPC framing, or monkeypatching a private class whose
  shape can change with any upgrade (`>=0.15.1` is all `pyproject.toml` asks).
* A thread fixes every transport at once: stdio and HTTP, and any in-process use
  (the tests call the tools under `asyncio.run`, which also stops the loop between
  runs).
* The fault is a property of the session ("its bytes are read when they arrive"),
  so the fix belongs to the session.

### 2.1 The reader threads

`_telnet_reader_thread(session)` and `_serial_reader_thread(session)`, started by
`SessionStore.store`, named `telnet-reader-<id>` / `serial-reader-<id>`,
`daemon=True` (a server that exits on stdin EOF is not held up by them).

* **Telnet**: `select()` on the socket with a 0.1 s timeout (the bound on how long
  a stop request waits); when readable, note the arrival time, wait 20 ms so a
  burst arrives as one chunk (the 0.7.0 loop's 20 ms sleep gave the same chunk
  size), then `read_very_eager()` (non-blocking: it reads only what is there).
  `EOFError`/`OSError` end the thread and mark the session inactive, as before.
* **Serial**: `serial.read(max(1, in_waiting))` with the port's 0.1 s timeout
  (set at open, unchanged), then the same 20 ms coalescing and a drain of
  `in_waiting`. pyserial's `read` ends at its timeout for a local port, an RFC 2217
  port and a `socket://` port alike.
* Then, for both, unchanged from 0.7.0: answer the DEC VT queries (DA, CPR, DSR),
  decode, append to `accumulated_output` and `unconsumed_output`, feed pyte,
  count bytes; and log the chunk, now stamped with the time it **arrived**
  (`log_stream_chunk(..., at=...)`).

### 2.2 Locks

Two `threading.Lock`s per session, never held together (so no lock-order
deadlock is possible between them):

* `session.lock` - the session's state: buffers, pyte screen, byte counts, the
  change sequence. The reader holds it only to append and feed one chunk. Every
  tool that reads or changes that state takes it (the 0.7.0 code took an
  `asyncio.Lock` in most places; it is now a `threading.Lock`, held only for short
  sections that never await, so blocking the event loop on it costs at most one
  chunk's pyte feed).
* `session.write_lock` - the transport's output. Every write (a command, a key, a
  VT reply from the reader, a NAWS resize, a BREAK) takes it, so two writers can
  never interleave bytes. The Telnet reader also holds it around
  `read_very_eager()`, because telnetlib answers option negotiation (`DO`/`WILL`)
  from inside the read with its own `sendall`.

`SessionLogger` gained a lock too: the reader thread and the tool calls now append
to the same transcript from different threads.

### 2.3 Stopping a reader

`SessionStore.delete` (every close path) and `SessionStore.store` (a new session
under the id of a dead one): set `is_active = False` and the session's
`stop_event`; wake the reader - Telnet: `socket.shutdown(SHUT_RDWR)`, which makes
the blocked `select()` return at once without freeing the descriptor; serial:
`cancel_read()` where pyserial has it, else the 0.1 s timeout - then `join()` the
thread with a 2 s bound, and only then close the socket or port (closing first
could let the descriptor number be reused while the reader still selects on it).
A thread still alive after the bound is logged as a warning, and the transport is
closed anyway.

### 2.4 `terminal_wait_for_change`

See the README for the call. Design points:

* "Changed" means the rendered screen (text, reverse-video annotation, cursor
  position and visibility) differs from the last screen this server **returned**
  for the session, from `terminal_get_screen`, `terminal_send_key` or
  `terminal_wait_for_change`. Bytes that repaint the same screen are not a change.
  If no screen has been returned yet, the baseline is the screen when the wait
  starts.
* "Settled" means no byte has arrived for `settle_seconds` (default 0.3).
* With a `pattern` (a Python regular expression searched in `screen_text`), the
  wait ends when the pattern is on the screen and the screen has settled, whether
  or not it changed; without one, when the screen has changed and settled.
* `timeout_seconds` is clamped to [0, 300]; `settle_seconds` to [0, timeout].
* It polls every 20 ms on the event loop (the state is now always current, so
  polling is cheap and needs no cross-thread wake-up).

## 3. The tests

`tests/test_m1_live_reader.py`. Tests 1, 2, 3 and 5 drive the real server over
stdio as an MCP client does (a subprocess running `python -m
chuk_mcp_telnet_client.main` from this checkout); test 4 calls the tools in
process, where the threads are what is counted.

(Results, RED and GREEN, in section 4.)

## 4. Results (Linux host, Python 3.12.15, chuk_mcp_server 0.26.1; all MEASURED)

Evidence files are in `docs/m1-evidence/`. Each test prints the host load
average beside its timing; the host is shared, and its load ran from 2 to 20
during this run.

| Test | 09dbaa0 (old code) | m1 |
| :-- | :-- | :-- |
| 1 `test_idle_telnet_screen_is_current_over_stdio` | RED: LATER sent at +2.00 s, not on a wait-0 screen at +4.50 s | GREEN |
| 2 `test_transcript_chunk_stamped_on_arrival` | RED: chunk stamped +2.521 s after its send | GREEN: +0.000 s |
| 3a `test_wait_for_change_returns_after_settle` | RED: unknown tool | GREEN: 0.331-0.333 s after the last byte (settle 0.3) |
| 3b `test_wait_for_change_sees_change_made_before_the_call` | RED: unknown tool | GREEN: 0.001-0.003 s |
| 3c `test_wait_for_change_times_out_unchanged` | RED: unknown tool | GREEN: 1.001 s for a 1.0 s timeout |
| 3d `test_wait_for_change_pattern` | RED: unknown tool | GREEN: 0.336-0.337 s after DONE |
| 3e `test_wait_for_change_caps_timeout` | RED: no cap | GREEN |
| 4a `test_close_while_reader_blocked_100_cycles` | RED: no reader thread | GREEN: worst close 0.000-0.002 s, 0 threads left, fds 7 before and 7 after |
| 4b `test_serial_close_while_reader_blocked` | RED: no reader thread | GREEN: 0.000 s |
| 5 `test_idle_serial_screen_is_current_over_stdio` | RED: stale at wait 0; the wait-1 control sees LATER | GREEN |
| the 45 existing tests | 45 passed | 45 passed (two asserts changed: `server_version` "0.7.0" to "0.7.1") |

* RED files: `red-09dbaa0.txt` (first run), `red-09dbaa0-serial-idle-rerun.txt`
  (after the serial test's setup fix, below), and `red-09dbaa0-final-tests.txt`
  (the final test file, sha256 `a1e5d56e2572e89d`, run against 09dbaa0 in a
  throwaway detached worktree with its own venv, since this worktree's editable
  install would have imported the new code; the worktree was removed after).
* GREEN files: `green-try1.txt` (one failure that was the test's fault, below)
  and `green-full-suite.txt` (55 passed, tools.py sha256 `67ca1226791cb705`).
* Baseline of the existing tests on 09dbaa0: `baseline-existing-tests-09dbaa0.txt`
  (45 passed).
* Serial: **MEASURED** that the serial path had the same fault (test 5 RED on
  09dbaa0, with its control GREEN).

### 4.1 Sabotage builds

A RED that says only "unknown tool" or "no thread" shows the feature is
absent, not that the test can catch a broken one. So each part was broken on
purpose in a copy of the new `tools.py`, the tests meant to catch it were run,
and the file was restored (hash checked). Runner:
`docs/m1-evidence/sabotage.py`; logs `sabotage.txt` (first) and `sabotage-2.txt` (after the test
fixes).

| Sabotage | Caught by | Result |
| :-- | :-- | :-- |
| S1 close neither stops the reader nor closes | 4a, 4b | RED: reader still alive after close |
| S2 close stops the reader, never closes the socket | 4a | first run **GREEN** (missed); after the fix, RED: socket not closed |
| S3 the wait ignores settle | 3a, 3d | RED: L2 missing; returned 0.037 s after DONE |
| S4 the wait ignores the last screen read | 3b | RED: changed false after 5.0 s |
| S5 the wait ignores the pattern | 3d | RED: returned at "Working..." without the pattern |
| S6 the reader thread is never started (0.7.0's fault) | 1, 2, 5 | RED: stale screen; no chunk logged |

## 5. Doubts and limits

* **The write lock and a full peer** (ASSUMED rare, not tested). The Telnet
  reader holds `write_lock` while `read_very_eager()` runs (telnetlib may answer
  option negotiation inside it), and the VT replies take it too. If a tool's
  write were blocked in `sendall()` because the peer had stopped reading, the
  reader would wait for that lock and stop reading too. That needs the peer's
  TCP receive window to be full, which interactive use with keys and short
  commands does not reach. 0.7.0 had no write lock (and no reading between
  calls at all).
* **HTTP transport**: not tested here (ASSUMED to work, as the threads do not
  depend on the transport's loop).
* **RFC 2217 serial**: the reader stops through pyserial's read timeout (0.1 s);
  `cancel_read()` is used where it exists. The suite covers `socket://`, a pty,
  and the RFC 2217-to-socket fallback, but not a live RFC 2217 server.
* **Event loop**: tools take a `threading.Lock` from async code. It is held
  only for one chunk's pyte feed or one screen render, so the loop blocks for
  at most that long. A very large chunk (tens of KB) could take some ms.
* **Timeouts**: the 300 s cap is the brief's figure. Which tool timeout each
  MCP client applies (Codex, Claude Code) was not measured here. In stdio mode a
  long wait delays every other call to the same server (README).
* **Every chunk waits 20 ms** before it is read, so bursts are logged as one
  chunk (as in 0.7.0). The stamp is the arrival of the chunk's first byte.
* **A replaced dead session**: `SessionStore.store` now stops and closes a dead
  session that a new connection replaces under the same ID. 0.7.0 dropped it
  without closing its socket. This is a small change beyond the brief's list,
  made because it is the same reader lifecycle.

## 6. Mistakes (for vms-savage's AGENT_MISTAKES.md)

* **M1-1** The first serial idle test wrote its FIRST banner to the pty
  before the server opened the port, and asserted it was on the screen. It
  went RED on 09dbaa0 for that reason, not for staleness: pyserial's `open()`
  discards pending input (`serialposix.py` line 344, `_reset_input_buffer`).
  Fixed by writing only after the open, and by adding a wait-1 control, so a RED
  can only mean "stale". (Rule 13: a RED from setup is not a catch.)
* **M1-2** The 100-cycle close test first counted descriptors inside
  `asyncio.run` (the loop's own fds included) and counted the fake server's
  100 accepted ends as the client's. 10 before and 107 after looked like a
  leak; the arithmetic (10 - 3 + 100) showed it was not. Fixed by counting
  outside the loop, after closing the server ends.
* **M1-3** Even then, sabotage S2 (the client socket never closed) stayed GREEN.
  Once the session is dropped, CPython's refcounting frees the unclosed socket,
  so a descriptor count cannot see a missing `close()` (the mechanism is
  ASSUMED; the equal counts are MEASURED). Fixed by keeping every session
  object and asserting `telnet.get_socket() is None` after close; S2 is then
  RED. (Rule 16: ask of every GREEN what it would look like if the thing were
  wrong.)
* **M1-4** S3 and S5 first went RED as a `KeyError`: the wait returned before
  the fake server had even sent L3/DONE, and the test read the send time
  first. Correct verdict, unreadable reason; the screen and pattern assertions
  now come first.
