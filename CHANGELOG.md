# Changelog

## 0.9.0

Token-efficient results. **This changes the default output of the stream and screen
tools: an agent that parsed `raw_base64` must now ask for it.** Sizes before and
after: `docs/TOKEN_EFFICIENCY.md`.

### API changes

* `raw_base64`, `initial_banner_raw_base64`, `final_output_raw_base64` (also inside
  `responses[]` and the close replies) are no longer returned by default. Every tool
  that returns stream output (`telnet_client`, `serial_client`, `telnet_send_input`,
  `serial_send_input`, `telnet_read_session`, `serial_read_session`,
  `telnet_close_session`, `serial_close_session`, `serial_send_break`) takes
  `include_raw: bool = False`; `include_raw: true` returns them as in 0.8.0
  (an empty delivery is `""`).
* Lossless guarantee kept: when a delivery's text cannot stand for its bytes (invalid
  UTF-8, or a UTF-8 character split across two deliveries) the result includes
  `raw_base64` even without `include_raw`, plus `raw_included_reason`
  (`"invalid_or_split_utf8"`). The exact-byte `.log.raw` transcript is unchanged.
* `annotated_text` (terminal_get_screen, terminal_wait_for_change, terminal_send_key)
  is returned only when it differs from `screen_text`.
* Left out when null or empty: `prompt_matched`, `log_file`, `pattern_matched`,
  `cursor_char`, `highlighted_lines`, `initial_banner`, `final_output`, and a close
  reply's `output`. Booleans, counts, `output`, `response` and `screen_text` are
  always present.
* `server_version` (now `0.9.0`) only in `telnet_client`, `serial_client` and
  `list_sessions`; `list_sessions` drops its constant `note` and the per-session
  `server_version`.
* Screens: trailing blank lines are cut from `screen_text`/`annotated_text`.
  `full_screen: true` (or `include_raw: true`) on `terminal_get_screen`,
  `terminal_wait_for_change` and `terminal_send_key` returns every row. Lines were
  already right-trimmed in 0.8.0. Row numbers and `rows`/`cols` are unchanged.

### Migration

* Pass `include_raw: true` wherever exact bytes, byte hashes or binary data matter.
* Treat a missing `annotated_text` as equal to `screen_text`, and any other missing
  optional field as null/empty.
* Stream delivery semantics, logging and the session API are unchanged.
* Tests that asserted `raw_base64` by default now pass `include_raw`
  (`tests/test_lossless_stream.py`, `tests/test_utf8_prompt_offset.py`);
  `tests/test_m1_live_reader.py::test_wait_for_change_pattern` expects `pattern_matched`
  absent instead of null; `tests/test_standalone_serial.py` expects `server_version` `0.9.0`.

## 0.8.0

Unread Telnet and serial output is one byte FIFO. Sends include older unread
output first, prompt matches leave their tail pending, and EOF/close preserve
final bytes. Screen tools never consume the stream. Concurrent stream calls are
serialized; cancelled calls restore unreturned output, including command batches.
Prompt search maps the pre-send byte boundary in the full reversible text, so
a split UTF-8 character cannot hide a following prompt. Complete older prompts
remain excluded, and output after the new matched prompt remains pending.

Stream replies add base64 fields for exact binary payloads and retain readable
incremental UTF-8 text. Broad regex prompt metadata replaces invalid UTF-8 for
JSON-safe display without changing matched byte offsets or payload delivery. Telnet payload preserves NUL, XON and escaped IAC. Serial
RFC2217 EOF drains pyserial's queue after its protocol reader exits. Telnet reset recovery returns already-cooked payload,
and a failed negotiation reply cannot discard following payload. There is no
pending-size truncation or lifetime history copy.

Logging appends each received payload once, plus an exact `.log.raw` sidecar, and
does not separately append supplied input arguments. Host-echoed commands remain
in this received-output audit trail; non-echoed input, including normal password
entries, is absent. A host-generated echo of a secret is still received payload.
Logging can attach to an existing session without replaying its banner. Explicit
close replies carry final unread output.

### Migration

* **Visible behavior change:** command echoes are no longer stripped from machine
  output. They appear in replies and received-output logs when the host echoes
  them; non-echoed input is not independently recorded.
* `return_full_buffer=True` reads unread bytes without replaying delivered history.
* Screen-only inspection leaves output unread; later LOGOUT receives that output
  before logout text. Draining the stream first preserves the no-duplicate intent
  of the earlier M1L fix.
* An ended session must be explicitly closed before reusing its ID.
* A session transcript consists of `.log` and `.log.raw`, with only received
  machine bytes in the latter. This stores a second payload copy; budget roughly
  twice the payload disk space plus text-log metadata and decoding overhead.

See the README for delivery boundaries and the qualification report for evidence.
